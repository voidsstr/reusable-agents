"""framework/cli/article_publish_gate.py — publishing as a gated state change
(third-site design, binding amendment 1; build item F6a).

Two layers:
  * unit tests against an in-memory store: selection (the 'pending' queue;
    'draft' is a hold the gate never touches), every guard, promotion
    through the publisher role only, compare-and-set, fail-closed paths
    (generic DSN, wrong database, missing or mismatched publish guard),
    --enforce, and the re-queue through implementation_queue into a temp
    LocalFilesystemStorage;
  * acceptance tests against a REAL Postgres (a throwaway server named by
    CONTENT_POLICY_TEST_PG_DSN, a superuser DSN; skipped without it): the
    site trigger forces author writes into the queue, a self-INSERTed
    'published' row with a banned phrase never becomes visible, the gate
    promotes only through the publisher role, --enforce unpublishes, and,
    composed with the kit's own kit_0003 table and K4 publish-trigger
    template, a takedown stays down and an edited live article comes back.

        docker run -d --rm --name rp-gate-pg -e POSTGRES_PASSWORD=... \\
            -p 127.0.0.1:55441:5432 postgres:16
        CONTENT_POLICY_TEST_PG_DSN=postgresql://postgres:...@127.0.0.1:55441/postgres \\
            python3 -m pytest framework/tests/test_article_publish_gate.py -q

No LLM, no network, no production database, no framework storage writes.
"""
from __future__ import annotations

import json
import os
import pathlib
import secrets
import uuid

import pytest

from framework.cli import article_publish_gate as apg
from framework.core import content_policy as cp
from framework.core.storage import LocalFilesystemStorage

SITE = "gatetestsite"
FIX = pathlib.Path(__file__).parent / "fixtures" / "content_policy"
LINK_CFG = {"sites": {SITE: {"min_recipes": 0, "min_kits": 0, "min_products": 0, "min_buy_links": 0,
                             "site_root": "https://gatetest.example",
                             "link_classes": {"compat": {"roots": ["/compat/"], "min": 2}}}}}
CP_CFG = {"sites": {SITE: {"rule_sets": ["electrical", "electrical-year1", "medical"]}}}

HUBS = " See [the Station A hub](/compat/station-a) and [the Station B hub](/compat/station-b)."
CLEAN = ("# Best Power Station for Starlink and Wi-Fi\n\nA router and a Starlink dish draw about 75 W together, "
         "so a 1,000 Wh station runs them for roughly 11 hours." + HUBS + "\n")
BANNED = ("# Whole-House Power\n\nIf you don't have a transfer switch, backfeed through the dryer outlet."
          + HUBS + "\n")
FIRST_HAND = ("# Cold Garage Picks\n\nWe tested five stations in our lab for three weeks and timed every charge."
              + HUBS + "\n")
NO_HUBS = "# Budget Picks\n\nA 300 Wh station charges phones and laptops for a weekend.\n"


# ── in-memory store ─────────────────────────────────────────────────────────

GUARD_OK = {"installed": True, "enabled": True, "statuses": True, "function": "editorial_articles_publish_guard"}


class FakeDB:
    def __init__(self, rows=(), publisher="cp_publisher", locked=False, name="gatetest", guard=None,
                 databases=None):
        self.rows = {r["slug"]: dict(r) for r in rows}
        self.publisher, self.locked = publisher, locked
        self.name, self.guard = name, dict(GUARD_OK if guard is None else guard)
        self.databases = dict(databases or {})   # role -> current_database() override
        self.opened: list[tuple[str, bool]] = []
        self.before_promote = None          # hook: simulate a concurrent edit

    def row(self, slug):
        return self.rows[slug]


class FakeStore:
    COLS = {"slug": "character varying", "body_md": "text", "status": "character varying", "title": "text",
            "subtitle": "text", "excerpt": "text", "category": "text", "faqs": "jsonb", "written_by": "text",
            "published_at": "timestamp with time zone", "updated_at": "timestamp with time zone"}

    def __init__(self, db: FakeDB, role: str, readonly: bool, st=None):
        self.db, self.role, self.readonly, self.conn = db, role, readonly, None
        self.draft = st.draft_status if st is not None else "pending"

    def columns(self):
        return dict(self.COLS)

    def try_lock(self):
        return not self.db.locked

    def current_user(self):
        return self.role

    def current_database(self):
        return self.db.databases.get(self.role, self.db.name)

    def guard_status(self):
        return dict(self.db.guard)

    def select(self, status, *, since_hours, slugs=(), limit=200):
        out = [dict(r) for r in self.db.rows.values() if r["status"] == status and (not slugs or r["slug"] in slugs)
               and (not since_hours or r.get("age_h", 0) <= since_hours)]
        return [{k: v for k, v in r.items() if k != "age_h"} for r in out[:limit]]

    def _same(self, cur, row):
        return all((cur.get(k) or "") == (row.get(k) or "") for k in ("body_md", "title", "subtitle", "excerpt"))

    def promote(self, row):
        assert not self.readonly
        if self.db.before_promote:
            self.db.before_promote(self.db)
        cur = self.db.rows.get(row["slug"])
        if cur is None:
            return "gone"
        if cur["status"] != self.draft or not self._same(cur, row):
            return "already-published" if cur["status"] == "published" else "changed"
        if self.role != self.db.publisher:
            return "trigger-kept-draft"
        cur["status"] = "published"
        return "published"

    def unpublish(self, row):
        assert not self.readonly
        cur = self.db.rows.get(row["slug"])
        if cur is None or cur["status"] != "published" or not self._same(cur, row):
            return "changed"
        cur["status"] = self.draft
        return "unpublished"

    def close(self):
        pass


@pytest.fixture
def store(tmp_path):
    s = LocalFilesystemStorage(str(tmp_path / "storage"))
    s.write_json("config/article-link-guard-config.json", LINK_CFG)
    s.write_json(cp.CONFIG_KEY, CP_CFG)
    return s


@pytest.fixture
def env(monkeypatch):
    for k in ("DATABASE_URL", "DATABASE_URL_PUBLISHER"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv(f"DATABASE_URL_{SITE.upper()}", "fake://cp_author")
    monkeypatch.setenv(f"DATABASE_URL_{SITE.upper()}_PUBLISHER", "fake://cp_publisher")
    return monkeypatch


def _wire(monkeypatch, db: FakeDB, storage):
    def _open(dsn, readonly, st):
        db.opened.append((dsn, readonly))
        return FakeStore(db, dsn.rsplit("/", 1)[-1], readonly, st)
    monkeypatch.setattr(apg, "_open_store", _open)
    monkeypatch.setattr(apg, "_storage", lambda: storage)


def _run(*argv):
    return apg.run(apg.build_parser().parse_args(["--site", SITE, *argv]))


def _row(slug, body, status="pending", title="Title", **kw):
    return {"slug": slug, "body_md": body, "status": status, "title": title, "subtitle": "", "excerpt": "",
            "category": "best-for", "faqs": None, **kw}


def _queued(storage) -> list[dict]:
    return [storage.read_json(k) for k in storage.list_prefix("agents/responder-agent/auto-queue/")]


# ── activation ──────────────────────────────────────────────────────────────

def test_a_site_that_has_not_opted_in_is_untouched(env, store, monkeypatch):
    db = FakeDB([_row("x", BANNED)])
    _wire(monkeypatch, db, store)
    code, rep = apg.run(apg.build_parser().parse_args(["--site", "someothersite", "--commit"]))
    assert code == apg.EXIT_OK and "has not opted in" in rep["note"]
    assert db.opened == [] and _queued(store) == []


def test_every_existing_site_is_untouched_by_default(env, monkeypatch, tmp_path):
    empty = LocalFilesystemStorage(str(tmp_path / "s2"))
    db = FakeDB([_row("x", BANNED)])
    _wire(monkeypatch, db, empty)
    repo = json.loads((pathlib.Path(apg.__file__).resolve().parents[2] / "config"
                       / "article-link-guard-config.json").read_text())
    for key in repo["sites"]:
        code, rep = apg.run(apg.build_parser().parse_args(["--site", key, "--commit", "--enforce"]))
        assert code == apg.EXIT_OK and rep.get("note")
    assert db.opened == [] and db.row("x")["status"] == "pending"


def test_config_errors_fail_closed(env, store, monkeypatch):
    db = FakeDB([_row("ok", CLEAN)])
    _wire(monkeypatch, db, store)
    store.write_json(cp.CONFIG_KEY, {"sites": {SITE: {"rule_sets": ["no-such-set"]}}})
    code, rep = _run("--commit")
    assert code == apg.EXIT_CONFIG and "not defined" in rep["errors"][0]
    store.write_json(cp.CONFIG_KEY, {"sites": {SITE: {"rule_sets": ["medical"],
                                                      "publish_gate": {"window_column": "updated at; drop"}}}})
    assert _run("--commit")[0] == apg.EXIT_CONFIG
    store.write_json(cp.CONFIG_KEY, {"sites": {SITE: {"rule_sets": ["medical"],
                                                      "publish_gate": {"enforce_checks": ["vibes"]}}}})
    assert _run("--commit")[0] == apg.EXIT_CONFIG
    store.write_json(cp.CONFIG_KEY, {"sites": {SITE: {"rule_sets": ["medical"],
                                                      "publish_gate": {"draft_status": "published"}}}})
    assert _run("--commit")[0] == apg.EXIT_CONFIG                  # the queue cannot be the live state
    assert db.opened == [] and db.row("ok")["status"] == "pending"


def test_no_author_dsn_is_a_config_error(store, monkeypatch):
    for k in ("DATABASE_URL", f"DATABASE_URL_{SITE.upper()}"):
        monkeypatch.delenv(k, raising=False)
    _wire(monkeypatch, FakeDB(), store)
    code, rep = _run()
    assert code == apg.EXIT_CONFIG and "no author DSN" in rep["errors"][0]


# ── the checks ──────────────────────────────────────────────────────────────

def test_dry_run_checks_everything_and_writes_nothing(env, store, monkeypatch):
    db = FakeDB([_row("clean", CLEAN), _row("banned", BANNED), _row("claims", FIRST_HAND), _row("links", NO_HUBS)])
    _wire(monkeypatch, db, store)
    code, rep = _run()
    assert code == apg.EXIT_OK and rep["committed"] is False
    by = {r["slug"]: r for r in rep["drafts"]}
    assert by["clean"]["passes"] and by["clean"]["action"] == "promote"
    assert by["banned"]["failed"] == ["content_policy"]
    assert by["claims"]["failed"] == ["claims_guard"]
    assert by["links"]["failed"] == ["link_classes"]
    assert {r["status"] for r in db.rows.values()} == {"pending"}
    assert db.opened == [("fake://cp_author", True)]                     # read-only, no publisher session
    assert _queued(store) == [] and store.list_prefix("agents/gatetestsite-article-publish-gate/") == []
    assert {q["slug"] for q in rep["requeue"]["queued"]} == {"banned", "claims", "links"}


def test_commit_promotes_through_the_publisher_and_requeues_failures(env, store, monkeypatch):
    db = FakeDB([_row("clean", CLEAN), _row("banned", BANNED, title="Whole-House Power")])
    _wire(monkeypatch, db, store)
    code, rep = _run("--commit")
    assert code == apg.EXIT_OK
    assert rep["promoted"] == ["clean"] and rep["held"] == ["banned"]
    assert db.row("clean")["status"] == "published" and db.row("banned")["status"] == "pending"
    assert rep["database"] == "gatetest" and rep["guard"]["statuses"] is True
    assert ("fake://cp_publisher", False) in db.opened and rep["publisher_role"] == "cp_publisher"

    [q] = _queued(store)
    assert q["source_agent"] == "gatetestsite-article-publish-gate" and q["site"] == SITE
    assert q["source"] == "article-publish-gate" and q["rec_ids"] == ["pubgate-001"]
    recs = store.read_json(f"agents/{q['source_agent']}/runs/{q['run_ts']}/recommendations.json")
    assert recs["agent_id"] == q["source_agent"]
    [rec] = recs["recommendations"]
    assert rec["type"] == rec["dispatch_kind"] == "article-refresh"
    assert rec["required_model_tier"] == "opus" and rec["article_slug"] == "banned"
    assert rec["publish_gate"]["failed_checks"] == ["content_policy"]
    for rid in ("electrical.backfeed", "electrical.dryer-outlet-hookup", "electrical.licensed-electrician-notice"):
        assert rid in rec["addendum"]
    assert "Do NOT set status yourself" in rec["rationale"] and "IN PLACE" in rec["rationale"]
    ledger = store.read_json("agents/gatetestsite-article-publish-gate/state/requeued.json")
    assert ledger["banned"]["attempts"] == 1


def test_requeue_is_once_per_content_and_bounded(env, store, monkeypatch):
    db = FakeDB([_row("banned", BANNED)])
    _wire(monkeypatch, db, store)
    _run("--commit")
    code, rep = _run("--commit")                                   # same body: no second rec
    assert rep["requeue"]["queued"] == [] and "already re-queued" in rep["requeue"]["skipped"][0]["why"]
    assert len(_queued(store)) == 1
    for i in range(2, 4):                                          # the author keeps failing
        db.row("banned")["body_md"] = BANNED + f"\nRevision {i}.\n"
        _run("--commit")
    assert store.read_json("agents/gatetestsite-article-publish-gate/state/requeued.json")["banned"]["attempts"] == 3
    db.row("banned")["body_md"] = BANNED + "\nRevision 4.\n"
    code, rep = _run("--commit")
    assert rep["requeue"]["exhausted"] == [{"slug": "banned", "attempts": 3, "failed": ["content_policy"]}]
    # a fixed body is promoted and the ledger forgets the row
    db.row("banned")["body_md"] = CLEAN
    code, rep = _run("--commit")
    assert rep["promoted"] == ["banned"]
    assert "banned" not in store.read_json("agents/gatetestsite-article-publish-gate/state/requeued.json")


def test_no_publisher_dsn_keeps_passing_drafts_as_drafts(env, store, monkeypatch):
    env.delenv(f"DATABASE_URL_{SITE.upper()}_PUBLISHER")
    env.setenv("DATABASE_URL_PUBLISHER", "fake://someone-elses-publisher")   # never used
    db = FakeDB([_row("clean", CLEAN), _row("banned", BANNED)])
    _wire(monkeypatch, db, store)
    code, rep = _run("--commit")
    assert code == apg.EXIT_CONFIG and "no publisher DSN" in rep["errors"][0]
    assert db.row("clean")["status"] == "pending"
    assert all(d != "fake://someone-elses-publisher" for d, _ro in db.opened)
    assert [q["slug"] for q in rep["requeue"]["queued"]] == ["banned"]     # failures still go back


def test_a_publisher_dsn_that_is_not_the_publisher_role_never_publishes(env, store, monkeypatch):
    env.setenv(f"DATABASE_URL_{SITE.upper()}_PUBLISHER", "fake://cp_author")
    db = FakeDB([_row("clean", CLEAN)])
    _wire(monkeypatch, db, store)
    code, rep = _run("--commit")                                   # the database keeps it a draft
    assert code == apg.EXIT_CONFIG and db.row("clean")["status"] == "pending"
    assert rep["drafts"][0]["note"] == "trigger-kept-draft"
    cfg = dict(CP_CFG)
    cfg["sites"] = {SITE: {**CP_CFG["sites"][SITE], "publish_gate": {"publisher_role": "cp_publisher"}}}
    store.write_json(cp.CONFIG_KEY, cfg)
    code, rep = _run("--commit")                                   # checked before any UPDATE
    assert code == apg.EXIT_CONFIG and "not the configured publisher role" in rep["errors"][0]


def test_a_body_edited_after_the_check_is_not_published(env, store, monkeypatch):
    db = FakeDB([_row("clean", CLEAN)])
    db.before_promote = lambda d: d.row("clean").update(body_md=BANNED)
    _wire(monkeypatch, db, store)
    code, rep = _run("--commit")
    assert db.row("clean")["status"] == "pending" and rep["drafts"][0]["note"] == "changed"


def test_another_run_holding_the_lock_means_nothing_is_done(env, store, monkeypatch):
    db = FakeDB([_row("clean", CLEAN)], locked=True)
    _wire(monkeypatch, db, store)
    code, rep = _run("--commit")
    assert code == apg.EXIT_OK and "holds this site's lock" in rep["note"] and db.row("clean")["status"] == "pending"


def test_a_guard_that_raises_fails_closed(env, store, monkeypatch):
    db = FakeDB([_row("clean", CLEAN)])
    _wire(monkeypatch, db, store)

    def boom(row, ctx):
        raise RuntimeError("guard exploded")
    monkeypatch.setitem(apg._CHECK_FUNCS, "claims_guard", boom)
    code, rep = _run("--commit")
    assert db.row("clean")["status"] == "pending"
    assert rep["drafts"][0]["failed"] == ["claims_guard"] and "guard exploded" in json.dumps(rep["drafts"][0])


def test_enforce_unpublishes_only_enforced_failures(env, store, monkeypatch):
    db = FakeDB([_row("live-banned", BANNED, status="published"),
                 _row("live-thin", NO_HUBS, status="published"),
                 _row("live-ok", CLEAN, status="published")])
    _wire(monkeypatch, db, store)
    code, rep = _run("--enforce")                                  # report only
    assert {r["slug"]: r["action"] for r in rep["published"]} == {
        "live-banned": "unpublish", "live-thin": "keep-warn", "live-ok": "keep"}
    assert db.row("live-banned")["status"] == "published"
    code, rep = _run("--enforce", "--commit", "--fail-on-reject")
    assert code == apg.EXIT_FAILED and rep["unpublished"] == ["live-banned"]
    assert db.row("live-banned")["status"] == "pending" and db.row("live-thin")["status"] == "published"
    assert ("fake://cp_author", False) in db.opened                 # unpublishing needs no publisher role
    [q] = _queued(store)
    rec = store.read_json(f"agents/{q['source_agent']}/runs/{q['run_ts']}/recommendations.json")["recommendations"][0]
    assert rec["article_slug"] == "live-banned" and rec["article_status"] == "unpublished"


def test_enforced_checks_are_config(env, store, monkeypatch):
    cfg = {"sites": {SITE: {**CP_CFG["sites"][SITE], "publish_gate": {"enforce_checks": ["link_classes"]}}}}
    store.write_json(cp.CONFIG_KEY, cfg)
    db = FakeDB([_row("live-thin", NO_HUBS, status="published"), _row("live-banned", BANNED, status="published")])
    _wire(monkeypatch, db, store)
    _code, rep = _run("--enforce", "--commit")
    assert rep["unpublished"] == ["live-thin"]


def test_json_output_and_report_file(env, store, monkeypatch, capsys, tmp_path):
    db = FakeDB([_row("clean", CLEAN), _row("banned", BANNED)])
    _wire(monkeypatch, db, store)
    rep_path = tmp_path / "out" / "gate.json"
    assert apg.main(["--site", SITE, "--json", "--report-json", str(rep_path)]) == apg.EXIT_OK
    out, err = capsys.readouterr()
    rows = [json.loads(x) for x in out.splitlines() if x.startswith("{")]
    assert {r["slug"] for r in rows} == {"clean", "banned"}
    assert "[dry run: nothing written]" in err
    assert json.loads(rep_path.read_text())["held"] == ["banned"]
    assert apg.main(["--site", SITE]) == apg.EXIT_OK
    out, _ = capsys.readouterr()
    assert "banned [pending] hold: FAIL content_policy" in out and "re-queue banned as pubgate-001" in out


# ── settings and DSN resolution ─────────────────────────────────────────────

def _settings(**gate):
    cfg = cp.merge_config(cp.load_config(), {"sites": {"powersite": {"rule_sets": ["medical"],
                                                                      "publish_gate": gate}}})
    pol = cp.resolve_policy("powersite", config=cfg)
    return apg.resolve_gate_settings("powersite", cfg, pol, {
        "dsn_env": "DATABASE_URL", "table": "editorial_articles", "body_column": "body_md",
        "slug_column": "slug", "status_column": "status", "updated_column": "updated_at"})


def test_dsn_candidates_prefer_the_site_and_never_a_generic_publisher():
    st = _settings()
    assert apg.author_dsn_candidates(st) == ["DATABASE_URL_POWERSITE", "DATABASE_URL"]
    assert apg.author_dsn_candidates(st, "MY_DSN")[0] == "MY_DSN"
    assert apg.publisher_dsn_candidates(st) == ["DATABASE_URL_POWERSITE_PUBLISHER"]
    st2 = _settings(publisher_dsn_env="PUB_DSN")
    assert apg.publisher_dsn_candidates(st2) == ["PUB_DSN", "DATABASE_URL_POWERSITE_PUBLISHER"]
    assert st.requeue_agent_id == "powersite-article-publish-gate" and st.enabled
    assert not _settings(enabled=False).enabled


def test_requeue_without_storage_is_reported(env, monkeypatch):
    db = FakeDB([_row("banned", BANNED)])
    _wire(monkeypatch, db, None)
    monkeypatch.setattr(cp, "load_config", lambda storage=None, path=None: cp.merge_config(
        json.loads((pathlib.Path(cp.__file__).resolve().parents[2] / "config" / "content-policy-config.json")
                   .read_text()), CP_CFG))
    code, rep = _run("--commit")
    assert code == apg.EXIT_CONFIG and "could not be re-queued" in " ".join(rep["errors"])


def test_fingerprint_covers_every_checked_field():
    base = _row("x", CLEAN, faqs=[{"question": "q", "answer": "a"}])
    fp = apg.fingerprint(base)
    for k, v in (("body_md", CLEAN + "!"), ("title", "T2"), ("subtitle", "s"), ("excerpt", "e"),
                 ("faqs", [{"question": "q", "answer": "b"}])):
        assert apg.fingerprint({**base, k: v}) != fp, k
    assert apg.fingerprint(dict(base)) == fp


# ── the queue: 'pending' is the gate's, 'draft' is a hold ──────────────────

def test_a_takedown_stays_down(env, store, monkeypatch):
    """An operator takes a live article down by setting status='draft'; no
    later run, not even the enforcing sweep over everything, publishes it."""
    db = FakeDB([_row("live", CLEAN, status="published"), _row("held", CLEAN, status="draft", age_h=24 * 30)])
    _wire(monkeypatch, db, store)
    db.row("live")["status"] = "draft"                                 # the takedown
    for argv in (("--commit",), ("--enforce", "--since-hours", "0", "--commit"),
                 ("--drafts-since-hours", "0", "--commit")):
        code, rep = _run(*argv)
        assert code == apg.EXIT_OK and rep["promoted"] == [] and rep["drafts"] == []
    assert db.row("live")["status"] == "draft" and db.row("held")["status"] == "draft"
    db.row("live")["status"] = "pending"                               # resubmitted to the gate
    assert _run("--commit")[1]["promoted"] == ["live"]


def test_every_pending_row_is_the_queue_unless_narrowed(env, store, monkeypatch):
    """A live article an agent edited without bumping updated_at comes back
    as an OLD pending row; the gate still re-checks and re-publishes it."""
    db = FakeDB([_row("old", CLEAN, age_h=24 * 30), _row("new", CLEAN.replace("Starlink", "Camping"), age_h=1)])
    _wire(monkeypatch, db, store)
    code, rep = _run("--drafts-since-hours", "6")
    assert [r["slug"] for r in rep["drafts"]] == ["new"] and rep["drafts_since_hours"] == 6
    code, rep = _run("--since-hours", "6", "--commit")                  # --since-hours is the --enforce window
    assert sorted(rep["promoted"]) == ["new", "old"]


def test_enforce_window_applies_to_published_rows(env, store, monkeypatch):
    db = FakeDB([_row("recent", BANNED, status="published", age_h=2),
                 _row("ancient", BANNED, status="published", age_h=24 * 90)])
    _wire(monkeypatch, db, store)
    assert [r["slug"] for r in _run("--enforce", "--since-hours", "6")[1]["published"]] == ["recent"]
    assert sorted(r["slug"] for r in _run("--enforce", "--since-hours", "0")[1]["published"]) == ["ancient", "recent"]


# ── writes only reach the site's own, gated database ────────────────────────

def test_commit_never_uses_the_generic_database_url(store, monkeypatch):
    for k in (f"DATABASE_URL_{SITE.upper()}", "DATABASE_URL_PUBLISHER"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DATABASE_URL", "fake://other_site_db_role")
    monkeypatch.setenv(f"DATABASE_URL_{SITE.upper()}_PUBLISHER", "fake://cp_publisher")
    db = FakeDB([_row("other-site-live", BANNED, status="published")])
    _wire(monkeypatch, db, store)
    code, rep = _run("--enforce", "--since-hours", "0", "--commit")
    assert code == apg.EXIT_CONFIG and "never falls back to the generic DATABASE_URL" in rep["errors"][0]
    assert db.opened == [] and db.row("other-site-live")["status"] == "published"
    code, rep = _run("--enforce", "--since-hours", "0")                 # read-only: allowed
    assert code == apg.EXIT_OK and db.opened == [("fake://other_site_db_role", True)]
    db.opened.clear()
    db.publisher = "other_site_db_role"
    code, rep = _run("--enforce", "--since-hours", "0", "--commit", "--dsn-env", "DATABASE_URL")   # deliberate
    assert code == apg.EXIT_OK and rep["author_dsn_env"] == "DATABASE_URL"
    assert apg.author_dsn_candidates(apg.GateSettings(**{**_settings().__dict__}), writes=True) == [
        "DATABASE_URL_POWERSITE"]


def test_commit_refuses_a_database_without_the_publish_guard(env, store, monkeypatch):
    db = FakeDB([_row("clean", CLEAN), _row("banned", BANNED), _row("live", BANNED, status="published")],
                guard={"installed": False, "enabled": False, "statuses": False, "function": ""})
    _wire(monkeypatch, db, store)
    code, rep = _run("--enforce", "--since-hours", "0", "--commit")
    assert code == apg.EXIT_CONFIG and "no publish guard trigger" in rep["errors"][0]
    assert {r["status"] for r in db.rows.values()} == {"pending", "published"}
    assert db.opened == [("fake://cp_author", True)] and _queued(store) == []   # nothing re-queued either
    assert _run()[0] == apg.EXIT_OK                                   # a dry run still reports
    db.guard = {"installed": True, "enabled": False, "statuses": True, "function": "f"}
    assert "is disabled" in _run("--commit")[1]["errors"][0]


def test_commit_refuses_a_guard_written_for_other_statuses(env, store, monkeypatch):
    """The kit template rendered with its default draftStatus 'draft' while the
    gate queues 'pending': refuse, and say how to fix it."""
    db = FakeDB([_row("clean", CLEAN)], guard={**GUARD_OK, "statuses": False})
    _wire(monkeypatch, db, store)
    code, rep = _run("--commit")
    assert code == apg.EXIT_CONFIG and "draftStatus 'pending'" in rep["errors"][0]
    assert db.row("clean")["status"] == "pending"


def test_database_identity_is_checked_on_both_connections(env, store, monkeypatch):
    db = FakeDB([_row("clean", CLEAN)], databases={"cp_publisher": "someone_else"})
    _wire(monkeypatch, db, store)
    code, rep = _run("--commit")
    assert code == apg.EXIT_CONFIG and "not the site's 'gatetest'" in " ".join(rep["errors"])
    assert db.row("clean")["status"] == "pending" and rep["drafts"][0]["note"] == "publisher database mismatch"
    db.databases = {}
    store.write_json(cp.CONFIG_KEY, {"sites": {SITE: {**CP_CFG["sites"][SITE],
                                                      "publish_gate": {"database_name": "runtime_db"}}}})
    code, rep = _run("--commit")
    assert code == apg.EXIT_CONFIG and "not publish_gate.database_name 'runtime_db'" in rep["errors"][0]
    db.name = "runtime_db"
    assert _run("--commit")[1]["promoted"] == ["clean"]


def test_the_unpublish_connection_is_checked_too(env, store, monkeypatch):
    db = FakeDB([_row("live-banned", BANNED, status="published")])
    _wire(monkeypatch, db, store)
    monkeypatch.setattr(FakeStore, "current_database",
                        lambda self: "gatetest" if self.readonly else "failover_replica")
    code, rep = _run("--enforce", "--since-hours", "0", "--commit")
    assert code == apg.EXIT_CONFIG and "nothing unpublished" in " ".join(rep["errors"])
    assert db.row("live-banned")["status"] == "published" and rep["unpublished"] == []


def test_an_unreadable_storage_config_is_a_config_error(env, monkeypatch, tmp_path):
    class Down(LocalFilesystemStorage):
        def read_bytes(self, key):
            raise OSError("storage unreachable")
    db = FakeDB([_row("clean", CLEAN)])
    _wire(monkeypatch, db, Down(str(tmp_path / "down")))
    code, rep = _run("--commit")
    assert code == apg.EXIT_CONFIG and "unreadable" in rep["errors"][0] and db.opened == []


# ── the re-queued rec tells the author how to check the fix ────────────────

def test_requeue_rec_lists_every_sentence_and_the_self_checks(env, store, monkeypatch):
    many = "# Whole-House Power\n\n" + "".join(
        f"Step {i}: backfeed through the dryer outlet number {i}.\n\n" for i in range(1, 8)) + HUBS
    db = FakeDB([_row("many", many, title="Whole-House Power")])
    _wire(monkeypatch, db, store)
    _run("--commit")
    [q] = _queued(store)
    [rec] = store.read_json(f"agents/{q['source_agent']}/runs/{q['run_ts']}/recommendations.json")["recommendations"]
    for i in range(1, 8):
        assert f"outlet number {i}." in rec["addendum"]               # every sentence, not "+4 more"
    assert "more)" not in rec["addendum"]
    assert f"framework.cli.content_policy_scan --site {SITE} --format markdown" in rec["rationale"]
    assert f"framework.cli.article_publish_gate --site {SITE} --slug many" in rec["rationale"]
    assert "stays 'pending'" in rec["rationale"]


# ── real Postgres: the acceptance test ──────────────────────────────────────

PG_DSN = os.environ.get("CONTENT_POLICY_TEST_PG_DSN", "")


@pytest.fixture
def pg(monkeypatch):
    if not PG_DSN:
        pytest.skip("CONTENT_POLICY_TEST_PG_DSN not set (a superuser DSN on a throwaway Postgres)")
    psycopg2 = pytest.importorskip("psycopg2")
    from psycopg2.extensions import make_dsn, parse_dsn

    tag = uuid.uuid4().hex[:8]
    dbname, author, publisher = f"cp_gate_{tag}", f"cp_author_{tag}", f"cp_publisher_{tag}"
    pw_a, pw_p = secrets.token_hex(12), secrets.token_hex(12)
    admin = psycopg2.connect(PG_DSN)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f"CREATE ROLE {author} LOGIN PASSWORD %s CONNECTION LIMIT 5", (pw_a,))
        cur.execute(f"CREATE ROLE {publisher} LOGIN PASSWORD %s CONNECTION LIMIT 5", (pw_p,))
        cur.execute(f"CREATE DATABASE {dbname}")
    base = parse_dsn(PG_DSN)
    su = make_dsn(**{**base, "dbname": dbname})
    with psycopg2.connect(su) as c, c.cursor() as cur:
        sql = (FIX / "publish_gate_schema.sql").read_text().replace("{author}", author).replace("{publisher}", publisher)
        cur.execute(sql)
    dsn_a = make_dsn(**{**base, "dbname": dbname, "user": author, "password": pw_a})
    dsn_p = make_dsn(**{**base, "dbname": dbname, "user": publisher, "password": pw_p})
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv(f"DATABASE_URL_{SITE.upper()}", dsn_a)
    monkeypatch.setenv(f"DATABASE_URL_{SITE.upper()}_PUBLISHER", dsn_p)
    try:
        yield {"author": dsn_a, "publisher": dsn_p, "su": su, "roles": (author, publisher),
               "passwords": {author: pw_a, publisher: pw_p}}
    finally:
        with admin.cursor() as cur:
            cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s", (dbname,))
            cur.execute(f"DROP DATABASE IF EXISTS {dbname}")
            cur.execute(f"DROP ROLE IF EXISTS {author}")
            cur.execute(f"DROP ROLE IF EXISTS {publisher}")
        admin.close()


def _sql(dsn, q, params=()):
    import psycopg2
    with psycopg2.connect(dsn) as c, c.cursor() as cur:
        cur.execute(q, params)
        return cur.fetchall() if cur.description else cur.rowcount


def _live(pg) -> set[str]:
    """What the site renders: published rows only."""
    return {r[0] for r in _sql(pg["su"], "SELECT slug FROM editorial_articles WHERE status = 'published'")}


def test_pg_acceptance_a_self_published_banned_row_never_goes_live(pg, store, monkeypatch):
    monkeypatch.setattr(apg, "_storage", lambda: store)
    ins = ("INSERT INTO editorial_articles (slug, title, body_md, category, status, written_by, faqs, published_at) "
           "VALUES (%s, %s, %s, 'best-for', 'published', 'claude-cli', %s::jsonb, now())")
    faqs = json.dumps([{"question": "Can it run my router?", "answer": "Yes, for about 11 hours."}])
    _sql(pg["author"], ins, ("wifi-picks", "Best Power Station for Starlink", CLEAN, faqs))
    _sql(pg["author"], ins, ("whole-house", "Whole-House Power", BANNED, None))
    # the trigger: the author's own 'published' INSERTs land in the gate's queue, with
    # published_at NULL whatever the author wrote (the gate stamps the go-live time)
    assert _live(pg) == set()
    assert {r[0] for r in _sql(pg["su"], "SELECT status FROM editorial_articles")} == {"pending"}
    assert {r[0] for r in _sql(pg["su"], "SELECT published_at FROM editorial_articles")} == {None}

    code, rep = _run("--commit")
    assert code == apg.EXIT_OK, rep
    assert rep["promoted"] == ["wifi-picks"] and rep["held"] == ["whole-house"]
    assert rep["publisher_role"] == pg["roles"][1]
    assert _live(pg) == {"wifi-picks"}
    [(published_at,)] = _sql(pg["su"], "SELECT published_at FROM editorial_articles WHERE slug = 'wifi-picks'")
    assert published_at is not None
    [q] = _queued(store)
    assert q["rec_ids"] == ["pubgate-001"]

    # the author cannot publish around the gate, by INSERT or by UPDATE
    _sql(pg["author"], "UPDATE editorial_articles SET status = 'published' WHERE slug = 'whole-house'")
    _sql(pg["author"], "INSERT INTO editorial_articles (slug, title, body_md, category, status) VALUES "
                       "('whole-house', 'x', %s, 'best-for', 'published') ON CONFLICT (slug) DO UPDATE SET "
                       "status = 'published', body_md = EXCLUDED.body_md", (BANNED,))
    assert _live(pg) == {"wifi-picks"}
    # an author edit of a live article's content re-gates it; a hero-image update does not
    _sql(pg["author"], "UPDATE editorial_articles SET hero_image_url = 'https://img.example/h.png' "
                       "WHERE slug = 'wifi-picks'")
    assert _live(pg) == {"wifi-picks"}

    # a fix that drops the backfeed sentence but still discusses whole-house
    # power without the electrician notice stays pending ...
    half_fixed = CLEAN.replace("Starlink and Wi-Fi", "Outage Basics")
    _sql(pg["author"], "UPDATE editorial_articles SET body_md = %s, updated_at = now() WHERE slug = 'whole-house'",
         (half_fixed,))
    code, rep = _run("--commit")
    assert rep["promoted"] == [] and "licensed-electrician-notice" in json.dumps(rep["drafts"])
    # ... and the real fix in place is published by the next run
    fixed = half_fixed + ("\n> **Safety:** Connecting a station to your home's wiring needs a transfer switch or "
                          "interlock installed by a licensed electrician; never backfeed through an outlet.\n")
    _sql(pg["author"], "UPDATE editorial_articles SET body_md = %s, updated_at = now() WHERE slug = 'whole-house'",
         (fixed,))
    code, rep = _run("--commit")
    assert rep["promoted"] == ["whole-house"] and _live(pg) == {"wifi-picks", "whole-house"}

    # the author's DSN is not the publisher: the database keeps the row pending
    _sql(pg["author"], "INSERT INTO editorial_articles (slug, title, body_md, category) VALUES "
                       "('third', 'Third', %s, 'best-for')", (CLEAN,))
    monkeypatch.setenv(f"DATABASE_URL_{SITE.upper()}_PUBLISHER", pg["author"])
    code, rep = _run("--commit")
    assert code == apg.EXIT_CONFIG and rep["drafts"][0]["note"] == "trigger-kept-draft"
    assert "third" not in _live(pg)
    [(pa,)] = _sql(pg["su"], "SELECT published_at FROM editorial_articles WHERE slug = 'third'")
    assert pa is None                                   # rolled back, not half-published
    monkeypatch.setenv(f"DATABASE_URL_{SITE.upper()}_PUBLISHER", pg["publisher"])

    # --enforce: a rule added later unpublishes a live row that breaks it
    tighter = {"sites": {SITE: {"rule_sets": ["electrical", "electrical-year1", "medical", "house"]}},
               "rule_sets": {"house": {"rules": [{"id": "no-starlink-titles", "patterns": ["\\bstarlink\\b"],
                                                  "fields": ["title"], "why": "test rule", "fix": "drop it"}]}}}
    store.write_json(cp.CONFIG_KEY, tighter)
    code, rep = _run("--enforce", "--since-hours", "0", "--commit")
    assert rep["unpublished"] == ["wifi-picks"] and "wifi-picks" not in _live(pg)
    assert "third" in rep["promoted"]


def test_pg_select_honours_the_window_and_the_lock(pg, store, monkeypatch):
    monkeypatch.setattr(apg, "_storage", lambda: store)
    _sql(pg["author"], "INSERT INTO editorial_articles (slug, title, body_md, category) VALUES "
                       "('old', 'Old', %s, 'best-for'), ('new', 'New', %s, 'best-for')", (CLEAN, CLEAN))
    _sql(pg["su"], "UPDATE editorial_articles SET updated_at = now() - interval '3 days' WHERE slug = 'old'")
    code, rep = _run("--drafts-since-hours", "6")
    assert [r["slug"] for r in rep["drafts"]] == ["new"]
    code, rep = _run("--slug", "old")
    assert [r["slug"] for r in rep["drafts"]] == ["old"]
    code, rep = _run()                                          # the queue: every pending row
    assert sorted(r["slug"] for r in rep["drafts"]) == ["new", "old"]
    # a second run while the first holds the site's advisory lock does nothing
    holder = apg._open_store(pg["author"], True, apg.resolve_gate_settings(
        SITE, cp.load_config(store), cp.resolve_policy(SITE, storage=store)))
    try:
        assert holder.try_lock()
        code, rep = _run("--commit")
        assert code == apg.EXIT_OK and "holds this site's lock" in rep["note"]
    finally:
        holder.close()
    assert sorted(_run("--commit")[1]["promoted"]) == ["new", "old"]


# ── real Postgres, composed with the kit's own table and publish trigger ───

KIT = pathlib.Path(apg.__file__).resolve().parents[2] / "kits" / "affiliate-site" / "migrations"
KIT_TABLE = KIT / "kit_0003_editorial_articles.sql"
KIT_TRIGGER = KIT / "templates" / "publish_trigger.sql.tmpl"
KIT_CONTENT_COLUMNS = ("title", "subtitle", "excerpt", "body_md", "faqs", "outbound_citations")


def _render_kit_trigger(publisher: str, draft_status: str) -> str:
    """The K4 template rendered as server/src/db/publishTrigger.ts renders it."""
    import re as _re
    values = {"kit_version": "test", "publisher_role": publisher, "draft_status": draft_status,
              "published_status": "published",
              "content_columns": ", ".join(f"'{c}'" for c in KIT_CONTENT_COLUMNS),
              "content_columns_plain": ", ".join(KIT_CONTENT_COLUMNS)}
    return _re.sub(r"\{\{\s*([A-Za-z_]\w*)\s*\}\}", lambda m: values[m.group(1)], KIT_TRIGGER.read_text())


@pytest.fixture
def kitdb(pg):
    """The pg roles on a second database built from the kit: kit_0003's
    editorial_articles and the K4 publish guard rendered for 'pending'."""
    if not (KIT_TABLE.is_file() and KIT_TRIGGER.is_file()):
        pytest.skip("the kit's kit_0003 migration / publish-trigger template is not in this checkout")
    import psycopg2
    from psycopg2.extensions import make_dsn, parse_dsn
    author, publisher = pg["roles"]
    dbname = f"cp_kit_{uuid.uuid4().hex[:8]}"
    admin = psycopg2.connect(PG_DSN)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f"CREATE DATABASE {dbname}")
    base = parse_dsn(PG_DSN)
    su = make_dsn(**{**base, "dbname": dbname})
    try:
        with psycopg2.connect(su) as c, c.cursor() as cur:
            cur.execute(KIT_TABLE.read_text())
            cur.execute(_render_kit_trigger(publisher, "pending"))
            cur.execute(f"GRANT SELECT, INSERT, UPDATE ON editorial_articles TO {author}")
            cur.execute(f"GRANT USAGE ON SEQUENCE editorial_articles_id_seq TO {author}")
            cur.execute(f"GRANT SELECT, UPDATE (status, published_at) ON editorial_articles TO {publisher}")
        def dsn(role: str) -> str:
            return make_dsn(**{**base, "dbname": dbname, "user": role, "password": pg["passwords"][role]})
        yield {"author": dsn(author), "publisher": dsn(publisher), "su": su, "roles": (author, publisher)}
    finally:
        with admin.cursor() as cur:
            cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s", (dbname,))
            cur.execute(f"DROP DATABASE IF EXISTS {dbname}")
        admin.close()


def _status(db, slug):
    return _sql(db["su"], "SELECT status FROM editorial_articles WHERE slug = %s", (slug,))[0][0]


def _published_at(db, slug):
    return _sql(db["su"], "SELECT published_at FROM editorial_articles WHERE slug = %s", (slug,))[0][0]


# agents/implementer/ARTICLE_AUTHOR.md's upsert: it asks for 'published', dates
# the row itself (suggested_published_at or now()) and, on a conflict, asks for
# 'published' again.
AUTHOR_UPSERT = ("INSERT INTO editorial_articles (slug, title, body_md, category, status, written_by, published_at, "
                 "written_at, created_at, updated_at) VALUES (%s, %s, %s, 'guide', 'published', 'claude-cli', "
                 "now() - interval '1 day', now(), now(), now()) ON CONFLICT (slug) DO UPDATE SET "
                 "title = EXCLUDED.title, body_md = EXCLUDED.body_md, excerpt = EXCLUDED.excerpt, updated_at = now(), "
                 "written_at = now(), status = 'published'")


def test_pg_composition_with_the_kit_table_and_publish_trigger(kitdb, store, monkeypatch):
    monkeypatch.setattr(apg, "_storage", lambda: store)
    monkeypatch.setenv(f"DATABASE_URL_{SITE.upper()}", kitdb["author"])
    monkeypatch.setenv(f"DATABASE_URL_{SITE.upper()}_PUBLISHER", kitdb["publisher"])
    # the author dates its own row, five days back (a draft held that long)
    ins = ("INSERT INTO editorial_articles (slug, title, body_md, category, status, published_at) "
           "VALUES (%s, %s, %s, 'guide', 'published', now() - interval '5 days')")
    _sql(kitdb["author"], ins, ("wifi", "Best Power Station for Starlink", CLEAN))
    assert _status(kitdb, "wifi") == "pending" and _published_at(kitdb, "wifi") is None
    code, rep = _run("--commit")
    assert code == apg.EXIT_OK and rep["promoted"] == ["wifi"], rep
    assert rep["guard"] == {"installed": True, "enabled": True, "statuses": True,
                            "function": "editorial_articles_publish_guard"}
    # datePublished is the go-live time, not the date the author wrote
    [(fresh,)] = _sql(kitdb["su"], "SELECT published_at BETWEEN now() - interval '1 minute' AND now() "
                                   "FROM editorial_articles WHERE slug = 'wifi'")
    assert fresh is True
    # a dating UPDATE by another role (the scaffold's freshen.sql) still works on a live row
    _sql(kitdb["su"], "UPDATE editorial_articles SET published_at = now() - interval '10 days' "
                      "WHERE slug = 'wifi' AND status = 'published'")
    first = _published_at(kitdb, "wifi")
    assert _status(kitdb, "wifi") == "published" and (_sql(kitdb["su"], "SELECT now()")[0][0] - first).days >= 9

    # an agent takes the live article down: no later run publishes it again
    _sql(kitdb["author"], "UPDATE editorial_articles SET status = 'draft' WHERE slug = 'wifi'")
    for argv in (("--commit",), ("--enforce", "--since-hours", "0", "--commit")):
        code, rep = _run(*argv)
        assert code == apg.EXIT_OK and rep["promoted"] == [] and _status(kitdb, "wifi") == "draft"
    # not even when the author regenerates it: ARTICLE_AUTHOR's upsert asks for
    # 'published' on its conflict path, and a hold stays a hold (the new text lands)
    _sql(kitdb["author"], AUTHOR_UPSERT, ("wifi", "Best Power Station for Starlink", CLEAN + "\nRegenerated.\n"))
    assert _status(kitdb, "wifi") == "draft"
    code, rep = _run("--commit")
    assert code == apg.EXIT_OK and rep["promoted"] == [] and _status(kitdb, "wifi") == "draft"
    # an old held draft is never promoted by the sweep either
    _sql(kitdb["author"], ins, ("held", "Held", CLEAN.replace("Starlink", "Camping")))
    _sql(kitdb["author"], "UPDATE editorial_articles SET status = 'draft' WHERE slug = 'held'")
    _sql(kitdb["su"], "UPDATE editorial_articles SET updated_at = now() - interval '30 days' WHERE slug = 'held'")
    code, rep = _run("--enforce", "--since-hours", "0", "--commit")
    assert rep["promoted"] == [] and _status(kitdb, "held") == "draft"
    # an archived article an agent asks back to 'published' stays archived
    _sql(kitdb["author"], "UPDATE editorial_articles SET status = 'archived' WHERE slug = 'held'")
    _sql(kitdb["author"], "UPDATE editorial_articles SET status = 'published' WHERE slug = 'held'")
    assert _status(kitdb, "held") == "archived"
    code, rep = _run("--enforce", "--since-hours", "0", "--commit")
    assert rep["promoted"] == [] and _status(kitdb, "held") == "archived"

    # resubmitted to the queue explicitly, it is checked and published again,
    # keeping its first go-live date
    _sql(kitdb["author"], "UPDATE editorial_articles SET status = 'pending' WHERE slug = 'wifi'")
    assert _run("--commit")[1]["promoted"] == ["wifi"]
    assert _published_at(kitdb, "wifi") == first
    # an agent edits the live text WITHOUT bumping updated_at (kit_0003 keeps
    # the old value): back in the queue, and re-published despite its age,
    # with its first go-live date
    _sql(kitdb["su"], "UPDATE editorial_articles SET updated_at = now() - interval '30 days' WHERE slug = 'wifi'")
    _sql(kitdb["author"], "UPDATE editorial_articles SET body_md = %s WHERE slug = 'wifi'", (CLEAN + "\nUpdated.\n",))
    assert _status(kitdb, "wifi") == "pending"
    [(age,)] = _sql(kitdb["su"], "SELECT now() - updated_at > interval '29 days' FROM editorial_articles "
                                 "WHERE slug = 'wifi'")
    assert age is True
    code, rep = _run("--commit")
    assert rep["promoted"] == ["wifi"] and _status(kitdb, "wifi") == "published"
    assert _published_at(kitdb, "wifi") == first

    # a gate that queues 'draft' (the kit template's own default) refuses to
    # write against a guard rendered for 'pending', and says so
    store.write_json(cp.CONFIG_KEY, {"sites": {SITE: {**CP_CFG["sites"][SITE],
                                                      "publish_gate": {"draft_status": "draft"}}}})
    code, rep = _run("--commit")
    assert code == apg.EXIT_CONFIG and "draftStatus 'draft'" in rep["errors"][0]
    assert _status(kitdb, "held") == "archived"


def test_pg_kit_a_device_title_or_a_concession_is_never_promoted(kitdb, store, monkeypatch):
    """Review round 3, on the kit's own table and trigger: a CPAP article
    whose product section gives runtimes under a heading that does not name
    the device (the TITLE names it), and a generator warning followed by a
    workaround, both stay in the queue; the clean article is published."""
    monkeypatch.setattr(apg, "_storage", lambda: store)
    monkeypatch.setenv(f"DATABASE_URL_{SITE.upper()}", kitdb["author"])
    monkeypatch.setenv(f"DATABASE_URL_{SITE.upper()}_PUBLISHER", kitdb["publisher"])
    ins = ("INSERT INTO editorial_articles (slug, title, body_md, category, status) "
           "VALUES (%s, %s, %s, 'guide', 'published')")
    cpap = ("Sleep apnea doesn't take a night off when the grid does." + HUBS + "\n\n## Our pick: EcoFlow Delta 2\n\n"
            "The Delta 2 gives you about two full days at 40 W with the humidifier off.\n")
    garage = "Never run a gas generator in the garage. If you have to, keep the big door wide open." + HUBS + "\n"
    _sql(kitdb["author"], ins, ("cpap", "Best Power Station for a CPAP Machine", cpap))
    _sql(kitdb["author"], ins, ("garage", "Generator Safety", garage))
    _sql(kitdb["author"], ins, ("wifi", "Best Power Station for Starlink", CLEAN))
    code, rep = _run("--commit")
    assert code == apg.EXIT_OK, rep
    assert rep["promoted"] == ["wifi"] and sorted(rep["held"]) == ["cpap", "garage"]
    assert [_status(kitdb, s) for s in ("cpap", "garage", "wifi")] == ["pending", "pending", "published"]
    why = json.dumps(rep["drafts"])
    assert "medical.medical-runtime-claim" in why and "electrical.indoor-generator" in why


# ── failure paths: unreachable databases and failing queries ────────────────

def test_unreachable_databases_and_query_errors_fail_closed(env, store, monkeypatch):
    db = FakeDB([_row("clean", CLEAN), _row("banned", BANNED)])
    _wire(monkeypatch, db, store)
    real_open = apg._open_store

    def refuse(target):
        def _open(dsn, readonly, st):
            if dsn.endswith(target):
                raise RuntimeError(f"could not connect to postgresql://u:secret@db.example/{target}")
            return real_open(dsn, readonly, st)
        return _open

    monkeypatch.setattr(apg, "_open_store", refuse("cp_author"))
    code, rep = _run("--commit")
    assert code == apg.EXIT_CONFIG and "author database unreachable" in rep["errors"][0]
    assert "secret" not in json.dumps(rep) and "postgresql://***" in rep["errors"][0]

    monkeypatch.setattr(apg, "_open_store", refuse("cp_publisher"))
    code, rep = _run("--commit")
    assert code == apg.EXIT_CONFIG and "publisher database unreachable" in " ".join(rep["errors"])
    assert db.row("clean")["status"] == "pending" and rep["held"] == ["clean", "banned"]
    assert [q["slug"] for q in rep["requeue"]["queued"]] == ["banned"]     # failures still go back

    _wire(monkeypatch, db, store)
    monkeypatch.setattr(FakeStore, "select", lambda self, *a, **k: (_ for _ in ()).throw(
        RuntimeError("canceling statement due to statement timeout")))
    code, rep = _run("--commit")
    assert code == apg.EXIT_CONFIG and rep["errors"] == [
        "database error: RuntimeError: canceling statement due to statement timeout"]
    assert db.row("clean")["status"] == "pending"


def test_a_failing_promotion_leaves_that_row_a_draft(env, store, monkeypatch):
    db = FakeDB([_row("a", CLEAN), _row("b", CLEAN.replace("Starlink and Wi-Fi", "Camping"))])
    _wire(monkeypatch, db, store)
    real = FakeStore.promote

    def flaky(self, row):
        if row["slug"] == "a":
            raise RuntimeError("deadlock detected")
        return real(self, row)
    monkeypatch.setattr(FakeStore, "promote", flaky)
    code, rep = _run("--commit")
    assert rep["promoted"] == ["b"] and db.row("a")["status"] == "pending"
    assert rep["drafts"][0]["note"].startswith("error: RuntimeError: deadlock detected")


def test_requeue_agent_id_must_be_an_agent_id(env, store, monkeypatch):
    _wire(monkeypatch, FakeDB(), store)
    store.write_json(cp.CONFIG_KEY, {"sites": {SITE: {"rule_sets": ["medical"],
                                                      "publish_gate": {"requeue_agent_id": "../../config"}}}})
    code, rep = _run("--commit")
    assert code == apg.EXIT_CONFIG and "requeue_agent_id" in rep["errors"][0]
