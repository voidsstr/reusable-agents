"""framework.core.recall_gate against a throwaway Postgres (one postgres:16
container per test process, one database per test; skips without docker).
Covers the write path: golden rows, idempotency, the buy-button views,
human-review precedence, the gate never lifting a block by itself, review
requests, stale flags, notification bookkeeping, repair-hold anchors, dry run,
the advisory lock and the review CLI."""
from __future__ import annotations

import datetime as dt

import pytest

from framework.core import recall_gate as rg
from framework.tests.test_recall_gate import POWER_GOLDEN
from framework.tests.test_recall_gate_support import (
    POWER_CATALOG_SQL, catalog_rows, connect, fixture_records, fresh_database, offline_guard,
    query, seed_site_db, settings_for,
)

NOW = dt.datetime(2026, 11, 17, 6, 15, tzinfo=dt.timezone.utc)
SITE = "power-site"


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    offline_guard(monkeypatch)


@pytest.fixture(scope="module")
def views():
    return {rid: rg.RecallView(rec) for rid, rec in fixture_records().items()}


@pytest.fixture
def site_db():
    dsn = fresh_database("rgdb")
    seed_site_db(dsn, catalog_rows("home_backup_power"))
    return dsn


def _settings(**over):
    return settings_for(SITE, "home-backup-power", catalog_sql=POWER_CATALOG_SQL, **over)


def _sync(dsn, views, settings=None, **kw):
    conn = connect(dsn)
    try:
        return rg.sync_site(conn, settings or _settings(), views, now=kw.pop("now", NOW), **kw)
    finally:
        conn.close()


def _matches(dsn):
    rows = query(dsn, """SELECT recall_id, product_id, match_kind, evidence->>'rule' AS rule,
                                tier, status, hold_until
                           FROM recall_matches""")
    return {(r["recall_id"], r["product_id"], r["match_kind"], r["rule"], r["tier"], r["status"],
             r["hold_until"].isoformat() if r["hold_until"] else None) for r in rows}


def _snapshot(dsn):
    return (query(dsn, "SELECT id, updated_at, content_sha FROM recalls ORDER BY id"),
            query(dsn, "SELECT match_key, updated_at, status, tier FROM recall_matches "
                       "ORDER BY match_key"))


def test_first_sync_writes_the_golden_rows(site_db, views):
    res = _sync(site_db, views)
    assert res.ok and res.catalog_items == 19 and res.catalog_problems == []
    assert _matches(site_db) == POWER_GOLDEN
    assert res.matches_new == len(POWER_GOLDEN) and res.matches_updated == 0
    eco = query(site_db, "SELECT * FROM recalls WHERE id = 'cpsc-recall-26010'")[0]
    assert eco["tier"] == "repair-hold" and eco["hold_until"] == dt.date(2026, 1, 7)
    assert eco["categories"] == ["station"] and eco["url_derived"] is False
    assert eco["number"] == "26-010" and eco["remedy_options"] == ["repair"]
    assert eco["record"]["id"] == "cpsc-recall-26010"
    aeiusny = query(site_db, "SELECT kind, source, tier FROM recalls "
                             "WHERE id = 'cpsc-warning-25454'")[0]
    assert aeiusny == {"kind": "warning", "source": "cpsc-warning", "tier": "stop-use"}
    # Only niche-relevant (or matched) recalls are stored: no baby robes.
    ids = {r["id"] for r in query(site_db, "SELECT id FROM recalls")}
    assert "cpsc-recall-26013" not in ids and "cpsc-recall-21104" in ids  # a cable recall
    assert res.recalls_inserted == len(ids) and res.recalls_relevant == len(ids)
    log = query(site_db, "SELECT * FROM recall_sync_log")
    assert len(log) == 1 and log[0]["matches_new"] == len(POWER_GOLDEN)
    assert log[0]["dry_run"] is False


def test_second_sync_with_the_same_inputs_writes_nothing(site_db, views):
    _sync(site_db, views)
    before = _snapshot(site_db)
    res = _sync(site_db, views, now=NOW + dt.timedelta(days=1))
    assert (res.matches_new, res.matches_updated, res.recalls_inserted,
            res.recalls_updated, res.matches_stale) == (0, 0, 0, 0, 0)
    assert _snapshot(site_db) == before
    assert len(query(site_db, "SELECT id FROM recall_sync_log")) == 2  # only the log grows


def test_known_recalls_are_rechecked_when_every_source_failed(site_db, views):
    _sync(site_db, views)
    before = _snapshot(site_db)
    res = _sync(site_db, {}, now=NOW + dt.timedelta(days=1))
    assert len(res.matches) == len(POWER_GOLDEN) and res.matches_stale == 0
    assert _snapshot(site_db) == before
    # A product added to the catalog while the sources were down is still caught.
    conn = connect(site_db)
    with conn, conn.cursor() as cur:
        cur.execute("INSERT INTO models VALUES (190, 1, 'DELTA Max (2000)', 'EFD-310', "
                    "'DELTA Max', 'ecoflow-delta-max-2000-renewed')")
        cur.execute("INSERT INTO products VALUES (90, 190, 'B0RGTEST90', 'station', true)")
    conn.close()
    res = _sync(site_db, {}, now=NOW + dt.timedelta(days=2))
    assert res.matches_new == 1
    row = query(site_db, "SELECT match_kind, tier FROM recall_matches WHERE product_id = 90")[0]
    assert row == {"match_kind": "exact", "tier": "repair-hold"}


def test_views_implement_the_buy_button_and_pick_rules(site_db, views):
    _sync(site_db, views)
    today = query(site_db, "SELECT CURRENT_DATE AS d")[0]["d"]
    blocks = {r["product_id"] for r in query(site_db, "SELECT product_id FROM v_recall_buy_blocks")}
    stop_use = {11, 12, 61, 71, 81}
    repair_holds = {1: dt.date(2026, 1, 7), 6: dt.date(2026, 1, 7), 21: dt.date(2026, 11, 18)}
    expected = stop_use | {p for p, until in repair_holds.items() if until > today}
    assert blocks == expected
    picks = {r["product_id"] for r in query(site_db,
                                            "SELECT product_id FROM v_recall_pick_exclusions")}
    # Design 0008_views: no active OR manual-review match is pick-eligible, so
    # the EcoFlow rows (1, 6) stay out of picks after their repair hold ended.
    assert picks == stop_use | set(repair_holds) | {2, 22, 14, 13, 72}
    notices = {r["product_id"] for r in query(site_db, "SELECT product_id FROM v_recall_notices")}
    assert notices == stop_use | set(repair_holds)  # every confirmed match keeps its notice


def test_human_review_is_never_overwritten(site_db, views):
    _sync(site_db, views)
    st = _settings()
    key_1600 = "cpsc-recall-26010|m:102|p:2"
    key_nodetail = "cpsc-warning-25454|m:114|p:14"
    conn = connect(site_db)
    try:
        row = rg.review_match(conn, st, key_1600, "confirm", reviewer="ops", note="same unit",
                              now=NOW)
        assert (row["status"], row["tier"], row["reviewer"]) == ("active", "repair-hold", "ops")
        assert row["hold_until"] == dt.date(2026, 1, 7)  # the recall's hold, recall-date anchored
        assert rg.review_match(conn, st, key_nodetail, "clear", reviewer="ops", now=NOW)[
            "status"] == "cleared"
        assert rg.review_match(conn, st, "nope", "clear", reviewer="ops") is None
        with pytest.raises(ValueError):
            rg.review_match(conn, st, key_1600, "confirm", reviewer="")
        with pytest.raises(ValueError):
            rg.review_match(conn, st, key_1600, "delete", reviewer="ops")
    finally:
        conn.close()
    _sync(site_db, views, now=NOW + dt.timedelta(days=1))
    rows = {r["match_key"]: r for r in query(site_db, "SELECT * FROM recall_matches")}
    assert (rows[key_1600]["status"], rows[key_1600]["tier"],
            rows[key_1600]["match_kind"], rows[key_1600]["reviewer"]) == (
        "active", "repair-hold", "sibling", "ops")
    assert rows[key_nodetail]["status"] == "cleared"
    # reopen hands the row back to the gate, which queues it again.
    conn = connect(site_db)
    try:
        rg.review_match(conn, st, key_1600, "reopen", reviewer="", now=NOW)
    finally:
        conn.close()
    _sync(site_db, views, now=NOW + dt.timedelta(days=2))
    again = query(site_db, "SELECT status, tier, reviewer FROM recall_matches "
                           "WHERE match_key = %s", (key_1600,))[0]
    assert again == {"status": "manual-review", "tier": "review", "reviewer": None}


def test_confirm_with_an_explicit_tier(site_db, views):
    _sync(site_db, views)
    conn = connect(site_db)
    try:
        row = rg.review_match(conn, _settings(), "cpsc-recall-26712|m:122|p:22", "confirm",
                              reviewer="ops", tier="stop-use", now=NOW)
    finally:
        conn.close()
    assert (row["tier"], row["status"], row["hold_until"]) == ("stop-use", "active", None)


def test_a_vanished_match_is_flagged_stale_never_cleared(site_db, views):
    _sync(site_db, views)
    conn = connect(site_db)
    with conn, conn.cursor() as cur:
        cur.execute("UPDATE products SET is_active = false WHERE id = 11")
    conn.close()
    res = _sync(site_db, views, now=NOW + dt.timedelta(days=1))
    assert res.matches_stale == 1
    row = query(site_db, "SELECT status, tier, stale_since FROM recall_matches "
                         "WHERE product_id = 11")[0]
    assert row["status"] == "active" and row["tier"] == "stop-use"  # still blocked
    assert row["stale_since"] is not None
    conn = connect(site_db)
    with conn, conn.cursor() as cur:
        cur.execute("UPDATE products SET is_active = true WHERE id = 11")
    conn.close()
    _sync(site_db, views, now=NOW + dt.timedelta(days=2))
    assert query(site_db, "SELECT stale_since FROM recall_matches WHERE product_id = 11")[0][
        "stale_since"] is None


def test_notification_bookkeeping(site_db, views):
    _sync(site_db, views)
    st = _settings()
    conn = connect(site_db)
    try:
        pending = rg.pending_notifications(conn, st)
        assert len(pending) == len(POWER_GOLDEN) and all(rg.needs_email(r) for r in pending)
        rg.mark_notified(conn, st, pending, NOW)
        assert rg.pending_notifications(conn, st) == []
    finally:
        conn.close()
    # The catalog learns DELTA Max 1600 shares the recalled model number: the
    # sibling becomes an exact repair-hold match, an escalation worth an email.
    conn = connect(site_db)
    with conn, conn.cursor() as cur:
        cur.execute("UPDATE models SET model_number = 'EFD310' WHERE id = 102")
    conn.close()
    _sync(site_db, views, now=NOW + dt.timedelta(days=1))
    conn = connect(site_db)
    try:
        [row] = rg.pending_notifications(conn, st)
        assert row["match_key"] == "cpsc-recall-26010|m:102|p:2"
        assert (row["match_kind"], row["tier"]) == ("exact", "repair-hold")
        assert rg.needs_email(row)
        rg.mark_notified(conn, st, [row], NOW)
    finally:
        conn.close()
    # And back: the gate never demotes a confirmed (active) match by itself.
    # It keeps the row, asks a human and emails.
    conn = connect(site_db)
    with conn, conn.cursor() as cur:
        cur.execute("UPDATE models SET model_number = 'EFD311T' WHERE id = 102")
    conn.close()
    _sync(site_db, views, now=NOW + dt.timedelta(days=2))
    conn = connect(site_db)
    try:
        [row] = rg.pending_notifications(conn, st)
        assert (row["match_kind"], row["status"]) == ("exact", "active")
        assert row["review_requested_at"] is not None
        assert row["gate_proposal"]["match_kind"] == "sibling"
        assert rg.needs_email(row, NOW.date())
    finally:
        conn.close()


def test_dry_run_rolls_everything_back(site_db, views):
    res = _sync(site_db, views, dry_run=True)
    assert res.dry_run and res.matches_new == len(POWER_GOLDEN)
    # It still reports the queue and blocks the real run would leave.
    assert {r["product_id"] for r in res.queue} == {2, 22, 14, 13, 72}
    assert res.blocking == 6
    for table in ("recalls", "recall_matches", "recall_sync_log"):
        assert query(site_db, f"SELECT count(*) AS n FROM {table}")[0]["n"] == 0


def test_the_advisory_lock_stops_a_concurrent_run(site_db, views):
    holder = connect(site_db)
    try:
        with holder.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (rg.advisory_lock_id(SITE),))
        with pytest.raises(rg.SiteLockedError):
            _sync(site_db, views)
    finally:
        holder.rollback()
        holder.close()
    assert _sync(site_db, views).matches_new == len(POWER_GOLDEN)


def test_manual_queue_lists_review_rows_with_their_age(site_db, views):
    _sync(site_db, views)
    conn = connect(site_db)
    try:
        q = rg.manual_queue(conn, _settings(), NOW + dt.timedelta(hours=5))
    finally:
        conn.close()
    assert {r["product_id"] for r in q} == {2, 22, 14, 13, 72}
    assert all(r["age_h"] == 5.0 and r["site"] == SITE for r in q)
    assert {r["match_kind"] for r in q} == {"sibling", "ambiguous"}


def test_buy_button_sql_check_and_post_sync_hooks(site_db, views):
    conn = connect(site_db)
    with conn, conn.cursor() as cur:
        cur.execute("CREATE TABLE hook_log (n integer)")
    conn.close()
    st = _settings(
        post_sync_sql=["INSERT INTO hook_log SELECT count(*) FROM recall_matches"],
        buy_button={"sql": "SELECT b.product_id, b.model_id FROM v_recall_buy_blocks b "
                           "JOIN products p ON p.id = b.product_id WHERE p.is_active "
                           "AND b.tier = 'stop-use'"})
    _sync(site_db, views, settings=st, dry_run=True)
    assert query(site_db, "SELECT count(*) AS n FROM hook_log")[0]["n"] == 0  # rolled back
    _sync(site_db, views, settings=st)
    assert query(site_db, "SELECT n FROM hook_log")[0]["n"] == len(POWER_GOLDEN)
    conn = connect(site_db)
    try:
        rows = rg.buy_button_sql_violations(conn, st)
    finally:
        conn.close()
    assert {r["product_id"] for r in rows} == {11, 12, 61, 71, 81}
    assert rg.buy_button_sql_violations(connect(site_db), _settings()) is None


def test_custom_schema_qualified_tables(views):
    dsn = fresh_database("rgschema")
    conn = connect(dsn)
    with conn, conn.cursor() as cur:
        cur.execute("CREATE SCHEMA safety")
    conn.close()
    tables = {"recalls": "safety.recalls", "matches": "safety.recall_matches",
              "sync_log": "safety.recall_sync_log"}
    seed_site_db(dsn, catalog_rows("home_backup_power"), tables=tables)
    res = _sync(dsn, views, settings=_settings(tables=tables))
    assert res.matches_new == len(POWER_GOLDEN)
    assert query(dsn, "SELECT count(*) AS n FROM safety.v_recall_buy_blocks")[0]["n"] >= 5


def test_cli_queue_and_review(site_db, views, monkeypatch, tmp_path, capsys):
    import json
    from framework.tests.test_recall_gate_support import repo_config_doc
    _sync(site_db, views)
    doc = repo_config_doc()
    doc["by_site"] = {SITE: {"enabled": True, "profile": "home-backup-power",
                             "catalog_sql": POWER_CATALOG_SQL, "dsn_env": "RG_CLI_TEST_DSN"}}
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps(doc))
    monkeypatch.setattr(rg, "REPO_CONFIG_PATH", path)
    monkeypatch.setenv("RG_CLI_TEST_DSN", site_db)
    assert rg.main(["--no-storage", "queue", "--site", SITE, "--json"]) == 0
    queue = json.loads(capsys.readouterr().out)
    assert len(queue) == 5
    key = next(r["match_key"] for r in queue if r["product_id"] == 22)
    assert rg.main(["--no-storage", "review", "--site", SITE, "--match-key", key,
                    "--decision", "clear", "--reviewer", "ops", "--note", "different model"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "cleared"
    assert rg.main(["--no-storage", "review", "--site", SITE, "--match-key", "missing",
                    "--decision", "clear", "--reviewer", "ops"]) == 1
    monkeypatch.delenv("RG_CLI_TEST_DSN")
    assert rg.main(["--no-storage", "queue", "--site", SITE]) == 2  # no DSN


def test_cli_queue_finds_the_dsn_through_the_storage_registry(site_db, views, storage,
                                                              monkeypatch, capsys):
    """The deferred-wiring recipe: the site is registered only in storage and
    its by_site entry leaves dsn_env empty. The documented review CLI must
    still find the DSN (it used to read the built-in registry only)."""
    import json
    from framework.core import site_registry
    from framework.tests.test_recall_gate_support import repo_config_doc
    _sync(site_db, views)
    site_registry.clear_cache()
    storage.write_json(site_registry.CONFIG_KEY, {"schema_version": "1", "sites": [{
        "name": SITE, "domain": "powersite.example", "repo_path": "/srv/powersite",
        "branch": "main", "dsn_env": "RG_CLI_REGISTRY_DSN"}]})
    doc = repo_config_doc()
    doc["by_site"] = {SITE: {"enabled": True, "profile": "home-backup-power",
                             "catalog_sql": POWER_CATALOG_SQL, "dsn_env": ""}}
    storage.write_json(rg.CONFIG_KEY, doc)
    monkeypatch.setenv("RG_CLI_REGISTRY_DSN", site_db)
    try:
        assert rg.main(["queue", "--site", SITE, "--json"]) == 0
        assert len(json.loads(capsys.readouterr().out)) == 5
        assert rg.main(["--no-storage", "queue", "--site", SITE]) == 1  # no by_site entry
    finally:
        site_registry.clear_cache()


@pytest.mark.parametrize("anchor", ["later", "first_match"])
def test_a_first_match_anchored_hold_blocks_from_the_first_sync(site_db, views, anchor):
    """hold_anchor later/first_match: an old repair recall matched today is
    held from TODAY, on the very first sync (it used to count from the recall
    date on day 0, leave the buy button live for a day and never email the
    later block)."""
    rules = [dict(r) for r in rg.DEFAULTS["tiers"]["rules"]]
    for r in rules:
        if r["id"] == "repair-remedy":
            r["hold_anchor"] = anchor
    st = _settings(tiers={"rules": rules})
    _sync(site_db, views, settings=st)
    row = query(site_db, "SELECT hold_until FROM recall_matches WHERE product_id = 1")[0]
    assert row["hold_until"] == dt.date(2027, 2, 15)  # 2026-11-17 + 90 days
    blocks = {r["product_id"] for r in query(site_db, "SELECT product_id FROM v_recall_buy_blocks")}
    assert {1, 6} <= blocks
    conn = connect(site_db)
    try:
        pend = {r["product_id"]: r for r in rg.pending_notifications(conn, st)}
        subject, body = rg.render_match_email(SITE, pend[1], today=NOW.date())
        assert "Buy button blocked" in body
        rg.mark_notified(conn, st, list(pend.values()), NOW)
    finally:
        conn.close()
    res = _sync(site_db, views, settings=st, now=NOW + dt.timedelta(days=1))
    assert res.matches_updated == 0  # the hold is anchored once, not every day


def test_the_gate_never_lifts_a_block_by_itself(site_db, views):
    """Klein Tools Blackfire (stop-use): a catalog edit makes the matcher see
    only a sibling. The block stays, a review is requested and emailed; a
    human clear is what lifts it."""
    _sync(site_db, views)
    conn = connect(site_db)
    with conn, conn.cursor() as cur:
        cur.execute("UPDATE models SET model_name = 'Blackfire Pro Power Station' WHERE id = 181")
    conn.close()
    res = _sync(site_db, views, now=NOW + dt.timedelta(hours=2))
    assert res.review_requested == 1
    row = query(site_db, "SELECT * FROM recall_matches WHERE product_id = 81")[0]
    assert (row["match_kind"], row["tier"], row["status"]) == ("exact", "stop-use", "active")
    assert row["review_requested_at"] is not None
    assert row["gate_proposal"]["why"] == "gate-would-lift"
    assert row["gate_proposal"]["match_kind"] == "sibling"
    assert 81 in {r["product_id"] for r in query(site_db,
                                                 "SELECT product_id FROM v_recall_buy_blocks")}
    st = _settings()
    conn = connect(site_db)
    try:
        assert [r["product_id"] for r in rg.pending_notifications(conn, st)
                if r["review_requested_at"]] == [81]
        q = {r["product_id"]: r for r in rg.manual_queue(conn, st, NOW + dt.timedelta(hours=5))}
        assert q[81]["age_h"] == 3.0  # since the review was requested
        out = rg.review_match(conn, st, row["match_key"], "clear", reviewer="ops", now=NOW)
        assert out["status"] == "cleared" and out["review_requested_at"] is None
    finally:
        conn.close()
    assert 81 not in {r["product_id"] for r in query(site_db,
                                                     "SELECT product_id FROM v_recall_buy_blocks")}


def test_a_vanished_match_for_a_listed_row_asks_a_human(site_db, views):
    """The matcher no longer finds the Aeiusny 500W (renamed in the catalog;
    the 400W still maps the warning to the brand) while the row is still
    listed: it stays blocked and a review is asked. A row that left the
    catalog (HALO 1000 deactivated) only gets stale_since."""
    _sync(site_db, views)
    conn = connect(site_db)
    with conn, conn.cursor() as cur:
        cur.execute("UPDATE models SET model_name = 'Portable Power Station 500W Pro' "
                    "WHERE id = 111")
        cur.execute("UPDATE products SET is_active = false WHERE id = 61")
    conn.close()
    _sync(site_db, views, now=NOW + dt.timedelta(days=1))
    rows = {r["product_id"]: r for r in query(site_db, "SELECT * FROM recall_matches "
                                                       "WHERE product_id IN (11, 61)")}
    assert rows[11]["stale_since"] is not None and rows[11]["review_requested_at"] is not None
    assert rows[11]["gate_proposal"]["why"] == "gate-no-longer-matches"
    assert rows[61]["stale_since"] is not None and rows[61]["review_requested_at"] is None
    blocks = {r["product_id"] for r in query(site_db, "SELECT product_id FROM v_recall_buy_blocks")}
    assert {11, 61} <= blocks
    conn = connect(site_db)
    try:
        pending = {r["product_id"]: r for r in rg.pending_notifications(conn, _settings())}
    finally:
        conn.close()
    assert 11 in pending and 61 not in pending


def test_a_cleared_match_with_stronger_evidence_returns_to_the_queue(site_db, views):
    _sync(site_db, views)
    st = _settings()
    key = "cpsc-recall-26010|m:102|p:2"
    conn = connect(site_db)
    try:
        rg.review_match(conn, st, key, "clear", reviewer="ops", note="different unit", now=NOW)
        rg.mark_notified(conn, st, rg.pending_notifications(conn, st), NOW)
    finally:
        conn.close()
    conn = connect(site_db)
    with conn, conn.cursor() as cur:
        cur.execute("UPDATE models SET model_number = 'EFD310' WHERE id = 102")  # now exact
    conn.close()
    for day in (1, 2):  # and it stays queued: the gate does not block over the human
        _sync(site_db, views, now=NOW + dt.timedelta(days=day))
        row = query(site_db, "SELECT * FROM recall_matches WHERE match_key = %s", (key,))[0]
        assert (row["status"], row["tier"], row["reviewer"], row["match_kind"]) == (
            "manual-review", "review", None, "exact")
        assert row["gate_proposal"]["why"] == "stronger-evidence-after-clear"
        assert row["gate_proposal"]["cleared_by"] == "ops"
    conn = connect(site_db)
    try:
        [pend] = [r for r in rg.pending_notifications(conn, st) if r["match_key"] == key]
        assert rg.needs_email(pend, NOW.date())
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# One-directional: a stricter block is always applied, over a pending
# request or a review decision too (review finding, round 2)
# ---------------------------------------------------------------------------

ECO_KEY = "cpsc-recall-26010|m:101|p:1"      # exact, repair-hold ended 2026-01-07
ECO_1600 = "cpsc-recall-26010|m:102|p:2"     # sibling
AEIUSNY_BARE = "cpsc-warning-25454|m:114|p:14"  # ambiguous (no model detail), stop-use


def _refund_views(views):
    """CPSC re-announces EcoFlow 26-010 with a refund remedy: stop-use."""
    import dataclasses
    rec = views["cpsc-recall-26010"].recall
    refund = rg.with_remedy_options(dataclasses.replace(
        rec, remedy="Stop using the recalled units and contact EcoFlow for a full refund.",
        remedy_options_raw=("Refund",), consumer_action=""), rg.DEFAULTS["remedy_options"])
    assert rg.decide_tier(refund, rg.DEFAULTS["tiers"]["rules"]).tier == "stop-use"
    return {**views, refund.id: rg.RecallView(refund)}


def _execute(dsn, sql, params=None):
    conn = connect(dsn)
    with conn, conn.cursor() as cur:
        cur.execute(sql, params)
    conn.close()


def _row(dsn, key):
    return query(dsn, "SELECT * FROM recall_matches WHERE match_key = %s", (key,))[0]


def _blocked(dsn, product_id):
    return bool(query(dsn, "SELECT 1 FROM v_recall_buy_blocks WHERE product_id = %s",
                      (product_id,)))


def _emails_for(dsn, key, today):
    """The pending notifications for one key that the agent would email, and
    marks them sent (what the agent does after sending)."""
    st = _settings()
    conn = connect(dsn)
    try:
        rows = [r for r in rg.pending_notifications(conn, st) if r["match_key"] == key]
        rg.mark_notified(conn, st, rows, NOW)
        return [r for r in rows if rg.needs_email(r, today)]
    finally:
        conn.close()


def _mark_all_sent(dsn):
    st = _settings()
    conn = connect(dsn)
    try:
        rg.mark_notified(conn, st, rg.pending_notifications(conn, st), NOW)
    finally:
        conn.close()


@pytest.mark.parametrize("restored", [True, False])
def test_a_remedy_escalation_is_applied_over_a_pending_gate_request(site_db, views, restored):
    """The reviewer's probe: the EcoFlow row's repair hold has ended; for one
    run the catalog row stops matching (a review is requested); then CPSC
    re-announces the recall as a refund. The product must be blocked and the
    operator emailed, whether the model is restored (the gate matches it
    again) or still renamed (the gate only holds the stored decision)."""
    _sync(site_db, views)
    _mark_all_sent(site_db)
    assert not _blocked(site_db, 1)
    _execute(site_db, "UPDATE models SET model_number = NULL, model_name = 'Renamed Unit', "
                      "family = NULL WHERE id = 101")
    _sync(site_db, views, now=NOW + dt.timedelta(days=1))
    row = _row(site_db, ECO_KEY)
    assert row["gate_proposal"]["why"] == "gate-no-longer-matches"
    assert len(_emails_for(site_db, ECO_KEY, NOW.date())) == 1
    if restored:
        _execute(site_db, "UPDATE models SET model_number = 'EFD310', "
                          "model_name = 'DELTA Max 2000', family = 'DELTA Max' WHERE id = 101")
    day2 = NOW + dt.timedelta(days=2)
    _sync(site_db, _refund_views(views), now=day2)
    row = _row(site_db, ECO_KEY)
    assert (row["tier"], row["status"], row["hold_until"]) == ("stop-use", "active", None)
    assert row["proposed_tier"] == "stop-use"
    assert _blocked(site_db, 1)
    if restored:  # the gate agrees with the stored decision again: request withdrawn
        assert row["review_requested_at"] is None and row["stale_since"] is None
    else:         # still not found: the request stays, the block is stricter
        assert row["review_requested_at"] is not None and row["stale_since"] is not None
    [mail] = _emails_for(site_db, ECO_KEY, day2.date())
    assert mail["tier"] == "stop-use"
    # Idempotent: the next run changes nothing and sends nothing.
    res = _sync(site_db, _refund_views(views), now=day2 + dt.timedelta(hours=1))
    assert res.matches_updated == 0 and _emails_for(site_db, ECO_KEY, day2.date()) == []


def test_a_remedy_escalation_reaches_a_confirmed_repair_hold(site_db, views):
    """A human confirmed DELTA Max 1600 (a sibling) as recalled: repair-hold,
    already over. The recall becomes stop-use: the confirmed row is blocked
    at once, the reviewer stays on record, a review is requested and the
    operator is emailed (reviewed rows used to be filtered out)."""
    _sync(site_db, views)
    st = _settings()
    conn = connect(site_db)
    try:
        row = rg.review_match(conn, st, ECO_1600, "confirm", reviewer="ops", now=NOW)
    finally:
        conn.close()
    assert (row["tier"], row["status"]) == ("repair-hold", "active")
    _mark_all_sent(site_db)
    assert not _blocked(site_db, 2)
    day1 = NOW + dt.timedelta(days=1)
    _sync(site_db, _refund_views(views), now=day1)
    row = _row(site_db, ECO_1600)
    assert (row["match_kind"], row["tier"], row["status"], row["reviewer"]) == (
        "sibling", "stop-use", "active", "ops")
    assert _blocked(site_db, 2) and row["review_requested_at"] is not None
    prop = row["gate_proposal"]
    assert prop["why"] == "gate-escalated-over-review"
    assert prop["human_decision"]["reviewer"] == "ops"
    assert (prop["recall_tier_was"], prop["recall_tier"]) == ("repair-hold", "stop-use")
    [mail] = _emails_for(site_db, ECO_1600, day1.date())
    subject, body = rg.render_match_email(SITE, mail, today=day1.date())
    assert "REVIEW REQUESTED" in subject and "over a review decision" in body
    assert "ops confirmed it" in body
    conn = connect(site_db)
    try:
        assert ECO_1600 in {r["match_key"] for r in rg.manual_queue(conn, st, day1)}
        # The human keeps the stricter tier; the request is resolved.
        rg.review_match(conn, st, ECO_1600, "confirm", reviewer="ops", now=day1)
    finally:
        conn.close()
    res = _sync(site_db, _refund_views(views), now=day1 + dt.timedelta(days=1))
    row = _row(site_db, ECO_1600)
    assert (row["tier"], row["review_requested_at"]) == ("stop-use", None)
    assert res.matches_updated == 0


def test_stronger_blocking_evidence_is_applied_over_a_clear(site_db, views):
    """A human cleared the Aeiusny row with no model detail (ambiguous). The
    catalog learns it is the 400W model, named by the stop-use warning: the
    product is blocked over the clear, the clear is recorded, a review is
    requested and emailed; it stays requested until a human decides."""
    _sync(site_db, views)
    st = _settings()
    conn = connect(site_db)
    try:
        rg.review_match(conn, st, AEIUSNY_BARE, "clear", reviewer="ops", now=NOW)
    finally:
        conn.close()
    _mark_all_sent(site_db)
    _execute(site_db, "UPDATE models SET model_name = 'Portable Power Station 400W' "
                      "WHERE id = 114")
    day1 = NOW + dt.timedelta(days=1)
    for day in (day1, day1 + dt.timedelta(days=1)):
        _sync(site_db, views, now=day)
        row = _row(site_db, AEIUSNY_BARE)
        assert (row["match_kind"], row["tier"], row["status"], row["reviewer"]) == (
            "exact", "stop-use", "active", None)
        assert _blocked(site_db, 14) and row["review_requested_at"] is not None
        assert row["gate_proposal"]["why"] == "gate-escalated-over-review"
        assert row["gate_proposal"]["cleared_by"] == "ops"
    [mail] = _emails_for(site_db, AEIUSNY_BARE, day1.date())
    assert "ops cleared it" in rg.render_match_email(SITE, mail, today=day1.date())[1]
    # A second clear on the now-exact evidence is final: no loop.
    conn = connect(site_db)
    try:
        rg.review_match(conn, st, AEIUSNY_BARE, "clear", reviewer="ops", now=day1)
    finally:
        conn.close()
    _sync(site_db, views, now=day1 + dt.timedelta(days=2))
    assert _row(site_db, AEIUSNY_BARE)["status"] == "cleared" and not _blocked(site_db, 14)


def test_a_blocked_product_that_leaves_the_catalog_keeps_its_page_locator(site_db, views):
    """item_locator is stored with every match; a blocked product that
    catalog_sql no longer returns is still handed to the buy-button probe,
    with its stored page URL."""
    _sync(site_db, views)
    row = _row(site_db, ECO_KEY)
    assert row["item_locator"] == {"url": "https://power.example/product/ecoflow-delta-max-2000",
                                   "asin": "B0RGTEST01"}
    _execute(site_db, "UPDATE products SET is_active = false WHERE id IN (11, 12)")
    res = _sync(site_db, views, now=NOW + dt.timedelta(days=1))
    assert res.blocked_unlisted == ["m:111|p:11", "m:112|p:12"]
    stand_in = {it.key: it for it in res.blocked_items}["m:111|p:11"]
    assert stand_in.url == "https://power.example/product/aeiusny-portable-power-station-500w"
    assert stand_in.asin == "B0RGTEST11"
    assert stand_in.display == "Aeiusny Portable Power Station 500W"
    assert res.summary()["blocked_unlisted"] == 2
    # A url_template naming a field the stand-in lacks yields no URL (never
    # the listing page); one it has works.
    assert rg._item_url(stand_in, "https://power.example/product/{slug}") == ""
    assert rg._item_url(stand_in, "https://power.example/dp/{asin}") == (
        "https://power.example/dp/B0RGTEST11")


def test_notification_key_sql_and_python_agree(site_db, views):
    _sync(site_db, views)
    conn = connect(site_db)
    with conn, conn.cursor() as cur:
        cur.execute("UPDATE recall_matches SET review_requested_at = now() WHERE product_id = 1")
    conn.close()
    rows = query(site_db, f"SELECT m.*, ({rg.NOTIFICATION_KEY_SQL}) AS sql_key "
                          "FROM recall_matches m")
    assert rows and all(r["sql_key"] == rg.notification_key(r) for r in rows)
    assert any(r["sql_key"].endswith("/review") for r in rows)


def test_review_waits_for_a_running_sync_then_gives_up(site_db, views):
    _sync(site_db, views)
    holder = connect(site_db)
    try:
        with holder.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (rg.advisory_lock_id(SITE),))
        conn = connect(site_db)
        try:
            with pytest.raises(rg.SiteLockedError):
                rg.review_match(conn, _settings(), "cpsc-recall-26010|m:102|p:2", "clear",
                                reviewer="ops", lock_timeout_s=0.3)
        finally:
            conn.close()
    finally:
        holder.rollback()
        holder.close()


def test_buy_button_sql_runs_read_only(site_db, views):
    _sync(site_db, views)
    st = _settings(buy_button={"sql": "DELETE FROM recall_matches RETURNING product_id"})
    conn = connect(site_db)
    try:
        with pytest.raises(Exception, match="read-only"):
            rg.buy_button_sql_violations(conn, st)
        assert conn.get_transaction_status() == 0  # idle: nothing left open
    finally:
        conn.close()
    assert len(query(site_db, "SELECT id FROM recall_matches")) == len(POWER_GOLDEN)

