"""agents/recall-gate-agent end to end: fake CPSC HTTP serving the real
fixtures, a throwaway Postgres (skips without docker), LocalFilesystemStorage
and a mocked mail sender. Never touches the network, production storage, a
production database or an LLM; failure paths call run() directly so no
agent-doctor trigger is ever written."""
from __future__ import annotations

import importlib.util as iu
import subprocess
from pathlib import Path

import pytest

from framework.core import recall_gate as rg
from framework.tests.test_recall_gate import POWER_GOLDEN
from framework.tests.test_recall_gate_support import (
    POWER_CATALOG_SQL, FakeHttp, catalog_rows, fresh_database, make_send_recorder,
    offline_guard, query, repo_config_doc, seed_site_db,
)

ROOT = Path(__file__).resolve().parents[2]
_spec = iu.spec_from_file_location("recall_gate_agent_under_test",
                                   ROOT / "agents" / "recall-gate-agent" / "agent.py")
rga = iu.module_from_spec(_spec)
_spec.loader.exec_module(rga)

NOW = "2026-11-17T12:00:00+00:00"
SITE = "powersite"
DSN_ENV = "RG_AGENT_TEST_DSN"


def write_config(storage, **site_over):
    doc = repo_config_doc()
    doc["http"] = {"retries": 0, "retry_base_delay_s": 0.0, "cache_dir": ""}
    for name in (rg.SOURCE_RECALL_CSV, rg.SOURCE_WARNING_CSV):
        doc["sources"][name]["min_rows"] = 1
    email = site_over.pop("_email", None)
    if email:
        doc["email"] = {**doc["email"], **email}
    entry = {"enabled": True, "profile": "home-backup-power", "catalog_sql": POWER_CATALOG_SQL,
             "dsn_env": DSN_ENV}
    entry.update(site_over)
    doc["by_site"] = {SITE: entry}
    storage.write_json(rg.CONFIG_KEY, doc)
    return doc


@pytest.fixture
def env(storage, monkeypatch):
    offline_guard(monkeypatch)
    for var in ("RECALL_GATE_DRY_RUN", "RECALL_GATE_SITES", "FRAMEWORK_OPERATOR_EMAIL",
                "IMPLEMENTER_FROM", "AGENT_DEFAULT_OWNER_EMAIL", "PROVIDER", "AGENT_ID",
                "AGENT_FORCE_RUN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("RECALL_GATE_NOW", NOW)
    monkeypatch.setenv("OPERATOR_EMAIL", "ops@example.com")
    monkeypatch.setenv("OPERATOR_FROM_EMAIL", "bot@example.com")
    return storage


def _no_subprocess(*a, **k):
    raise AssertionError(f"subprocess started during an agent run: {a[:1]}")


class Harness:
    def __init__(self, storage):
        self.storage = storage
        self.http = FakeHttp()
        self.sent, self.send = make_send_recorder()
        self.n = 0

    def agent(self):
        self.n += 1
        a = rga.RecallGateAgent(storage=self.storage, run_ts=f"20261117T12000{self.n}Z")
        a.auto_recovery_enabled = False  # never queue an agent-doctor run from a test
        a.http_get = self.http
        a.send_email = self.send
        return a

    def _guarded(self, fn):
        """Run the agent with subprocesses forbidden (no msmtp, no claude CLI,
        no docker): everything it does must go through the injected seams."""
        orig = subprocess.Popen
        subprocess.Popen = _no_subprocess
        try:
            return fn()
        finally:
            subprocess.Popen = orig

    def run_once(self):
        a = self.agent()
        try:
            return self._guarded(a.run_once)
        finally:
            a._stop_heartbeat()

    def run(self, state=None):
        a = self.agent()
        a.state = dict(state or {})
        try:
            return self._guarded(a.run)
        finally:
            a._stop_heartbeat()


@pytest.fixture
def site(env, monkeypatch):
    dsn = fresh_database("rgagent")
    seed_site_db(dsn, catalog_rows("home_backup_power"))
    monkeypatch.setenv(DSN_ENV, dsn)
    write_config(env)
    return dsn


def test_full_run_writes_rows_and_emails_each_new_match_once(env, site):
    from framework.core import goals as goals_mod
    import json
    goals_doc = json.loads((ROOT / "agents/recall-gate-agent/goals.json").read_text())
    goals_mod.init_goals("recall-gate-agent", goals_doc["goals"], storage=env)
    h = Harness(env)
    res = h.run_once()
    assert res.status == "success", res.summary
    m = res.metrics
    assert m["sites_synced"] == 1 and m["site_errors"] == 0 and m["source_errors"] == 0
    assert m["matches_new"] == len(POWER_GOLDEN) and m["llm_calls"] == 0
    assert m["recall_sync_age_h"] == 0.0 and m["manual_queue_size"] == 5
    # No buy-button check configured: nothing verifies the 6 blocked SKUs, so
    # they count as live (the goal never reads green unmeasured).
    assert (m["recalled_live_buy_buttons"], m["recalled_buy_buttons_unverified"],
            m["buy_button_unchecked_sites"]) == (6, 6, 1)
    match_mails = [s for s in h.sent if "could not be verified" not in s["subject"]]
    unverified_alerts = [s for s in h.sent if "could not be verified" in s["subject"]]
    assert len(unverified_alerts) == 1 and "no buy-button check configured" in \
        unverified_alerts[0]["body_html"]
    # One email per new match, to the operator pair from the env.
    assert len(match_mails) == len(POWER_GOLDEN) == m["emails_sent"]
    assert all(s["to"] == ["ops@example.com"] and s["sender"] == "bot@example.com"
               and s["bypass_digest"] is True and s["msmtp_account"] == "automation"
               for s in h.sent)
    subjects = [s["subject"] for s in match_mails]
    assert any("REPAIR-HOLD - EcoFlow DELTA Max 2000 / CPSC recall 26-010" in s for s in subjects)
    assert any("STOP-USE - Aeiusny" in s and "CPSC warning 25-454" in s for s in subjects)
    outbound = env.list_prefix("agents/recall-gate-agent/outbound-emails/")
    assert len(outbound) == len(POWER_GOLDEN)
    queue = env.read_json("agents/recall-gate-agent/manual-queue.json")
    assert queue["size"] == 5
    assert {r["product_id"] for r in queue["sites"][SITE]} == {2, 22, 14, 13, 72}
    state = env.read_json("agents/recall-gate-agent/state/latest.json")["state"]
    assert set(state["sources"]) == set(rg.SOURCES)
    assert all(s["last_success_at"] == NOW for s in state["sources"].values())
    # Goals declared in goals.json were auto-tracked from RunResult.metrics.
    tracked = {g["id"]: g for g in goals_mod.read_active_goals("recall-gate-agent", storage=env)}
    assert tracked["goal-recall-sync-fresh"]["metric"]["current"] == 0.0
    assert tracked["goal-recall-gate-sites-clean"]["metric"]["current"] == 0.0
    assert tracked["goal-no-recalled-live-buy-buttons"]["metric"]["current"] == 6.0
    assert state["sites"][SITE]["api_backfilled_at"] == NOW
    # Re-run with identical inputs: nothing new, nothing re-sent.
    res2 = h.run_once()
    assert res2.status == "success" and res2.metrics["matches_new"] == 0
    assert res2.summary.startswith("no new recall matches")
    assert len(h.sent) == len(POWER_GOLDEN) + 1
    # The incremental API window starts 21 days back after a success.
    assert h.http.api_queries()[-1]["LastPublishDateStart"] == ["2026-10-27"]
    assert _matches_count(site) == len(POWER_GOLDEN)


def _matches_count(dsn):
    return query(dsn, "SELECT count(*) AS n FROM recall_matches")[0]["n"]


def test_no_llm_is_ever_called(env, site, monkeypatch):
    from framework.core import ai_providers
    calls = []

    def trap(*a, **k):
        calls.append(a[:1])
        raise AssertionError("LLM called")
    monkeypatch.setattr(ai_providers, "chat_with_fallback", trap)
    monkeypatch.setattr(ai_providers, "ai_client_for", trap)
    h = Harness(env)
    assert h.run_once().status == "success"
    assert calls == []
    agent = h.agent()
    try:
        for fn in (agent.ai_chat, agent.ai_chat_resolved, agent.ai_client):
            with pytest.raises(rga.RecallGateLLMForbidden):
                fn([{"role": "user", "content": "match this recall"}])
    finally:
        agent._stop_heartbeat()


def test_one_failed_source_is_reported_and_others_still_sync(env, site):
    h = Harness(env)
    h.http.fail[rg.SOURCE_API] = 503
    prior = {"sources": {rg.SOURCE_API: {"last_success_at": "2026-11-16T06:15:00+00:00"}}}
    res = h.run(prior)
    assert res.status == "success" and res.metrics["source_errors"] == 1
    assert res.metrics["recall_sync_age_h"] == 29.75  # the API's last success, not now
    api_state = res.next_state["sources"][rg.SOURCE_API]
    assert api_state["last_success_at"] == "2026-11-16T06:15:00+00:00"
    assert "503" in api_state["last_error"]
    # The CSVs alone still produce every golden match except API-only facts.
    assert _matches_count(site) == len(POWER_GOLDEN)


def test_all_sources_down_is_a_failure_but_known_recalls_are_rechecked(env, site):
    h = Harness(env)
    first = h.run()
    assert first.status == "success"
    for name in rg.SOURCES:
        h.http.fail[name] = 503
    res = h.run(first.next_state)
    assert res.status == "failure" and res.metrics["source_errors"] == 3
    assert res.metrics["sites_synced"] == 1 and res.metrics["matches_total"] == len(POWER_GOLDEN)
    assert res.summary.startswith("recall sync problems")


def test_email_cap_sends_a_summary_for_the_rest(env, site):
    write_config(env, _email={"max_per_run": 3})
    h = Harness(env)
    res = h.run()
    assert res.metrics["emails_sent"] == 4  # 3 individual + 1 summary
    assert sum("more recall matches" in s["subject"] for s in h.sent) == 1
    assert query(site, "SELECT count(*) AS n FROM recall_matches WHERE notified_at IS NULL")[0][
        "n"] == 0


def test_missing_recipients_fail_and_retry_next_run(env, site, monkeypatch):
    monkeypatch.delenv("OPERATOR_EMAIL")
    h = Harness(env)
    res = h.run()
    assert res.metrics["emails_failed"] == len(POWER_GOLDEN) and h.sent == []
    # A gate that cannot tell anyone is a failed run, never a quiet success.
    assert res.status == "failure" and "email config" in res.summary
    assert "OPERATOR_EMAIL" in res.error_text
    monkeypatch.setenv("FRAMEWORK_OPERATOR_EMAIL", "first@example.com")
    res2 = h.run(res.next_state)
    assert res2.metrics["emails_sent"] == len(POWER_GOLDEN)
    assert {tuple(s["to"]) for s in h.sent} == {("first@example.com",)}


def test_failed_send_is_retried_and_never_duplicated(env, site):
    h = Harness(env)
    attempts = []

    def flaky(**kw):
        attempts.append(kw["subject"])
        return (len(attempts) % 2 == 0), "transport hiccup"
    h.send = flaky
    res = h.run()
    assert res.metrics["emails_sent"] + res.metrics["emails_failed"] == len(POWER_GOLDEN)
    res2 = h.run(res.next_state)
    assert res2.metrics["emails_sent"] + res2.metrics["emails_failed"] == res.metrics[
        "emails_failed"]
    delivered = [s for i, s in enumerate(attempts, 1) if i % 2 == 0]
    assert len(delivered) == len(set(delivered))  # never the same match twice


PAGE = "https://power.example/product/"


def test_live_buy_button_probe_and_alert_dedupe(env, site):
    write_config(env, buy_button={"http": {
        "enabled": True, "allowed_hosts": ["power.example"],
        "markers": [r"data-cta-placement=", r"amazon\.com/dp/"]}})
    h = Harness(env)
    for slug in ("aeiusny-portable-power-station-500w", "aeiusny-400w-portable-power-station"):
        h.http.pages[PAGE + slug] = (200, "<main><p>Recall notice</p></main>")
    h.http.pages[PAGE + "halo-halo-1000"] = (
        200, '<a data-cta-placement="pdp-hero" href="/r/B0RGTEST61">Check price</a>')
    # A PDP that redirects straight to the store IS a buy button (never followed).
    h.http.pages[PAGE + "klein-tools-blackfire-power-station"] = (
        302, "", {"location": "https://www.amazon.com/dp/B0RGTEST81?tag=x-20"})
    # A bot wall: nobody verified this one, so it counts as live.
    h.http.pages[PAGE + "predator-2000-watt-power-station"] = (403, "challenge")
    res = h.run()
    m = res.metrics
    assert (m["recalled_live_buy_buttons"], m["recalled_buy_buttons_unverified"],
            m["buy_button_unchecked_sites"]) == (3, 1, 0)
    assert not any("amazon" in u for u, _ in h.http.calls)
    alerts = [s for s in h.sent if "buy button" in s["subject"]]
    assert len(alerts) == 1 and "HALO" in alerts[0]["body_html"]
    assert "Blackfire" in alerts[0]["body_html"] and "HTTP 403" in alerts[0]["body_html"]
    res2 = h.run(res.next_state)
    assert res2.metrics["recalled_live_buy_buttons"] == 3
    assert len([s for s in h.sent if "buy button" in s["subject"]]) == 1  # same set: no repeat
    for slug in ("halo-halo-1000", "klein-tools-blackfire-power-station",
                 "predator-2000-watt-power-station"):
        h.http.pages[PAGE + slug] = (200, "<p>Recalled</p>")
    res3 = h.run(res2.next_state)
    assert res3.metrics["recalled_live_buy_buttons"] == 0
    assert SITE not in res3.next_state["buy_button_alerts"]


BLOCKED_SLUGS = ("aeiusny-portable-power-station-500w", "aeiusny-400w-portable-power-station",
                 "halo-halo-1000", "klein-tools-blackfire-power-station",
                 "predator-2000-watt-power-station", "goal-zero-yeti-3000x")
PROBE = {"http": {"enabled": True, "allowed_hosts": ["power.example"],
                  "markers": [r"data-cta-placement=", r"amazon\.com/dp/"]}}


def _execute(dsn, sql):
    from framework.tests.test_recall_gate_support import connect
    conn = connect(dsn)
    with conn, conn.cursor() as cur:
        cur.execute(sql)
    conn.close()


def _probed_harness(env):
    write_config(env, buy_button=PROBE)
    h = Harness(env)
    for slug in BLOCKED_SLUGS:
        h.http.pages[PAGE + slug] = (200, "<main><p>Recall notice</p></main>")
    return h


def test_a_retired_blocked_product_does_not_stick_the_goal(env, site):
    """Round-2 finding: the site retires a recalled SKU, so catalog_sql (WHERE
    p.is_active) stops returning it. Its page is still probed at the URL
    stored with the match: a recall notice or a 404 is not a live buy
    button, so the goal reads 0 again instead of 1 forever, and a retired
    page that still sells is caught."""
    h = _probed_harness(env)
    first = h.run()
    assert first.metrics["recalled_live_buy_buttons"] == 0
    _execute(site, "UPDATE products SET is_active = false WHERE id = 11")
    page = PAGE + "aeiusny-portable-power-station-500w"
    calls = len(h.http.calls)
    second = h.run(first.next_state)
    m = second.metrics
    assert (m["recalled_live_buy_buttons"], m["recalled_buy_buttons_unverified"],
            m["recalled_blocked_unlisted"], m["matches_blocking"]) == (0, 0, 0, 6)
    assert page in [u for u, _ in h.http.calls[calls:]]
    del h.http.pages[page]  # the page is gone: FakeHttp answers 404
    third = h.run(second.next_state)
    assert third.metrics["recalled_live_buy_buttons"] == 0
    assert not [s for s in h.sent if "buy button" in s["subject"] or "verified" in s["subject"]]
    h.http.pages[page] = (200, '<a data-cta-placement="pdp-hero" href="/r/B0RGTEST11">Buy</a>')
    fourth = h.run(third.next_state)
    assert fourth.metrics["recalled_live_buy_buttons"] == 1
    [alert] = [s for s in h.sent if "buy button" in s["subject"]]
    assert "Aeiusny Portable Power Station 500W" in alert["body_html"]


def test_a_retired_blocked_product_with_no_stored_page_is_reported_apart(env, site):
    h = _probed_harness(env)
    first = h.run()
    _execute(site, "UPDATE products SET is_active = false WHERE id = 11")
    _execute(site, "UPDATE recall_matches SET item_locator = NULL WHERE product_id = 11")
    res = h.run(first.next_state)
    m = res.metrics
    assert (m["recalled_live_buy_buttons"], m["recalled_buy_buttons_unverified"],
            m["recalled_blocked_unlisted"], m["matches_blocking"]) == (0, 0, 1, 6)
    assert res.status == "success" and "left the catalog" in res.summary_md
    # With no check configured, the listed blocked SKUs count as live (an
    # unmeasured goal never reads green); the retired one is reported apart.
    write_config(env)
    res = h.run(res.next_state)
    assert (res.metrics["recalled_live_buy_buttons"], res.metrics["recalled_blocked_unlisted"],
            res.metrics["buy_button_unchecked_sites"]) == (5, 1, 1)


def test_dsn_comes_from_the_site_registry(env, site, monkeypatch):
    from framework.core import site_registry
    site_registry.clear_cache()
    env.write_json(site_registry.CONFIG_KEY, {"schema_version": "1", "sites": [{
        "name": SITE, "domain": "powersite.example", "repo_path": "/srv/powersite",
        "branch": "main", "dsn_env": "POWERSITE_REGISTRY_DSN"}]})
    write_config(env, dsn_env="")
    monkeypatch.setenv("POWERSITE_REGISTRY_DSN", site)
    try:
        res = Harness(env).run()
        assert res.status == "success" and res.metrics["sites_synced"] == 1
        monkeypatch.delenv("POWERSITE_REGISTRY_DSN")
        site_registry.clear_cache()
        res2 = Harness(env).run()
        assert res2.status == "failure" and res2.metrics["site_errors"] == 1
        assert "POWERSITE_REGISTRY_DSN" in res2.next_state["sites"][SITE]["last_error"]
        assert site not in str(res2.next_state)  # the DSN value is never recorded
    finally:
        site_registry.clear_cache()


def test_dry_run_writes_and_sends_nothing(env, site, monkeypatch):
    write_config(env, buy_button={"sql": "SELECT product_id, model_id FROM v_recall_buy_blocks",
                                  "http": {"enabled": True, "allowed_hosts": ["power.example"],
                                           "markers": ["data-cta-placement="]}})
    monkeypatch.setenv("RECALL_GATE_DRY_RUN", "1")
    h = Harness(env)
    prior = {"sources": {}, "sites": {}, "marker": "kept"}
    res = h.run(prior)
    assert res.status == "success" and res.summary.startswith("[dry run]")
    assert res.metrics["matches_new"] == len(POWER_GOLDEN)
    assert h.sent == [] and _matches_count(site) == 0
    assert env.read_json("agents/recall-gate-agent/manual-queue.json") is None
    # It reports the queue the real run would leave ...
    assert res.metrics["manual_queue_size"] == 5
    # ... never probes a page or runs the site's buy-button SQL ...
    assert all(h.http.source_of(u) for u, _ in h.http.calls)
    # ... feeds no goal and leaves the state exactly as it was.
    assert not set(rga.GOAL_METRICS) & set(res.metrics)
    assert "dry_run_recall_sync_age_h" in res.metrics
    assert res.next_state == prior


def test_config_problems_and_missing_tables_are_site_errors(env, monkeypatch):
    dsn = fresh_database("rgempty")  # no tables at all
    monkeypatch.setenv(DSN_ENV, dsn)
    write_config(env)
    res = Harness(env).run()
    assert res.status == "failure" and res.metrics["site_errors"] == 1
    assert "UndefinedTable" in res.next_state["sites"][SITE]["last_error"]
    write_config(env, profile="nope")
    res = Harness(env).run()
    assert "unknown profile" in res.next_state["sites"][SITE]["last_error"]


def test_no_subscribed_site_or_disabled_is_a_quiet_no_op(env):
    h = Harness(env)
    res = h.run()  # repo config: no site
    assert res.status == "success" and res.summary == "no subscribed sites: nothing to do"
    assert h.http.calls == []  # nothing fetched
    doc = repo_config_doc()
    doc["enabled"] = False
    env.write_json(rg.CONFIG_KEY, doc)
    assert h.run().summary == "recall gate disabled in config"


def test_site_filter_env(env, site, monkeypatch):
    monkeypatch.setenv("RECALL_GATE_SITES", "some-other-site")
    res = Harness(env).run()
    assert res.summary == "no subscribed sites: nothing to do"


def test_a_dry_run_does_not_use_up_the_backfill(env, site, monkeypatch):
    """The documented D6 order is dry run first, then a real run: the real
    run must still read the 730-day API history (it used to get 21 days)."""
    monkeypatch.setenv("RECALL_GATE_DRY_RUN", "1")
    h = Harness(env)
    assert h.run_once().status == "success"
    monkeypatch.delenv("RECALL_GATE_DRY_RUN")
    assert h.run_once().status == "success"
    assert h.run_once().status == "success"
    starts = [q["LastPublishDateStart"][0] for q in h.http.api_queries()]
    assert starts == ["2024-11-17", "2024-11-17", "2026-10-27"]
    eco = query(site, "SELECT url_derived, sources FROM recalls WHERE id = 'cpsc-recall-26010'")[0]
    assert eco["url_derived"] is False and "saferproducts_api" in eco["sources"]


def test_a_site_subscribed_later_gets_the_backfill(env, site, monkeypatch):
    h = Harness(env)
    assert h.run_once().status == "success"
    dsn2 = fresh_database("rgagent2")
    seed_site_db(dsn2, catalog_rows("home_backup_power"))
    monkeypatch.setenv("RG_AGENT_TEST_DSN_2", dsn2)
    doc = write_config(env)
    doc["by_site"]["powersite2"] = {**doc["by_site"][SITE], "dsn_env": "RG_AGENT_TEST_DSN_2"}
    env.write_json(rg.CONFIG_KEY, doc)
    res = h.run_once()
    assert res.status == "success" and res.metrics["sites_synced"] == 2
    starts = [q["LastPublishDateStart"][0] for q in h.http.api_queries()]
    assert starts == ["2024-11-17", "2024-11-17"]
    eco = query(dsn2, "SELECT url_derived FROM recalls WHERE id = 'cpsc-recall-26010'")[0]
    assert eco["url_derived"] is False  # the API record reached the new site
    h.run_once()
    assert h.http.api_queries()[-1]["LastPublishDateStart"] == ["2026-10-27"]


def test_an_unreadable_storage_config_fails_closed(env, site):
    h = Harness(env)
    assert h.run_once().status == "success"
    calls = len(h.http.calls)
    env.write_bytes(rg.CONFIG_KEY, b"{archived or corrupt")
    res = h.run_once()
    assert res.status == "failure" and "config unreadable" in res.summary
    assert res.metrics["site_errors"] == 1 and res.metrics["recall_sync_age_h"] == 0.0
    assert len(h.http.calls) == calls  # nothing fetched, nothing synced
    state = env.read_json("agents/recall-gate-agent/state/latest.json")["state"]
    assert SITE in state["sites"] and set(state["sources"]) == set(rg.SOURCES)  # kept


def test_a_config_that_lost_a_synced_site_fails_closed(env, site):
    h = Harness(env)
    assert h.run_once().status == "success"
    env.write_json(rg.CONFIG_KEY, repo_config_doc())  # by_site {}: the site vanished
    res = h.run_once()
    assert res.status == "failure" and "no by_site entry for powersite" in res.summary
    assert res.metrics["site_errors"] == 1
    # Unsubscribing on purpose (enabled: false) is quiet and forgets the site.
    write_config(env, enabled=False)
    res = h.run_once()
    assert res.status == "success" and res.summary == "no subscribed sites: nothing to do"
    assert SITE not in env.read_json("agents/recall-gate-agent/state/latest.json")["state"][
        "sites"]
    env.write_json(rg.CONFIG_KEY, repo_config_doc())
    assert h.run_once().status == "success"


def test_sync_age_counts_each_sites_last_commit(env, site, monkeypatch):
    """A site that stops syncing ages the goal even while every source still
    answers, and the API window does not move past data no site stored."""
    h = Harness(env)
    first = h.run()
    monkeypatch.delenv(DSN_ENV)
    monkeypatch.setenv("RECALL_GATE_NOW", "2026-11-18T18:00:00+00:00")
    res = h.run(first.next_state)
    assert res.status == "failure" and res.metrics["source_errors"] == 0
    assert res.metrics["recall_sync_age_h"] == 30.0
    assert res.next_state["sources"][rg.SOURCE_API]["last_success_at"] == NOW

