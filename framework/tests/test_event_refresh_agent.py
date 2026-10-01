"""agents/event-refresh-agent: post-event article refresh producer
(audit issue seasonal-no-post-event-state)."""
import datetime as dt
import importlib.util as iu
import json
from pathlib import Path

import pytest

from framework.core import seasonal_calendar as sc

_ROOT = Path(__file__).resolve().parents[2]
_spec = iu.spec_from_file_location(
    "event_refresh_agent_under_test",
    _ROOT / "agents" / "event-refresh-agent" / "agent.py")
era = iu.module_from_spec(_spec)
_spec.loader.exec_module(era)

AGENT_DIR = _ROOT / "agents" / "event-refresh-agent"


def _articles():
    # site -> rows a SELECT would return for the tags asked
    return {
        "site-a": [
            {"id": 1, "slug": "best-stand-mixers-2026-prime-big-deal-days",
             "title": "Best Stand Mixers Before Prime Big Deal Days",
             "tags": ["holiday:prime-big-deal-days-kitchen", "mixers"]},
            {"id": 2, "slug": "fall-drinks", "title": "Fall Drinks",
             "tags": ["holiday:halloween"]},
        ],
        "site-b": [
            {"id": 7, "slug": "prime-big-deal-days-2026-gpu-deals",
             "title": "Prime Big Deal Days 2026 GPU Deals",
             "tags": ["holiday:prime-big-deal-days"]},
        ],
    }


@pytest.fixture
def agent_env(storage, monkeypatch):
    monkeypatch.setenv("EVENT_REFRESH_TODAY", "2026-10-08")
    monkeypatch.delenv("EVENT_REFRESH_DRY_RUN", raising=False)
    monkeypatch.setenv("DB_A", "postgresql://a")
    monkeypatch.setenv("DB_B", "postgresql://b")
    storage.write_json(era.CONFIG_KEY, {
        "sites": {
            "site-a": {"dsn_env": "DB_A",
                       "article_url_template": "https://a.example/blog/{slug}"},
            "site-b": {"dsn_env": "DB_B"},
        }})
    data = _articles()
    by_dsn = {"postgresql://a": "site-a", "postgresql://b": "site-b"}
    calls = []

    def fake_fetch(dsn, tags, site_cfg, limit=500):
        calls.append((dsn, tuple(tags)))
        return [r for r in data[by_dsn[dsn]] if set(r["tags"]) & set(tags)]

    def make():
        a = era.EventRefreshAgent(storage=storage, run_ts="20261008T103000Z")
        a.fetch_articles = fake_fetch
        return a
    return make, calls


def _queue_files(storage):
    return sorted(k for k in storage.list_prefix("agents/responder-agent/auto-queue/")
                  if k.endswith(".json"))


def test_queues_one_opus_refresh_per_tagged_article(storage, agent_env):
    make, _ = agent_env
    res = make().run()
    assert res.status == "success"
    assert res.metrics["recs_queued"] == 2
    assert res.metrics["events_ended"] == 2
    assert res.metrics["site_errors"] == 0
    doc = storage.read_json(
        "agents/event-refresh-agent/runs/20261008T103000Z/recommendations.json")
    recs = {r["article_slug"]: r for r in doc["recommendations"]}
    assert set(recs) == {"best-stand-mixers-2026-prime-big-deal-days",
                         "prime-big-deal-days-2026-gpu-deals"}  # halloween page untouched
    mixer = recs["best-stand-mixers-2026-prime-big-deal-days"]
    assert mixer["type"] == mixer["rec_type"] == "article-refresh"
    assert mixer["required_model_tier"] == "opus"
    assert mixer["site"] == "site-a"
    assert mixer["affected_url"] == "https://a.example/blog/best-stand-mixers-2026-prime-big-deal-days"
    assert mixer["event"]["end"] == "2026-10-07"
    assert mixer["next_occasion"]["id"] == "black-friday-kitchen"
    assert mixer["next_occasion"]["start"] == "2026-11-27"
    gpu = recs["prime-big-deal-days-2026-gpu-deals"]
    assert gpu["next_occasion"]["id"] == "black-friday"
    text = mixer["rationale"]
    for must in ("past tense", "October 6–7, 2026", "November 27–30, 2026",
                 "Keep the slug", "SAME UPDATE", "holiday:black-friday-kitchen",
                 "nine days"):
        assert must in text, must
    # Reader-facing name drops the calendar's audience qualifier.
    assert "(October Prime Day" not in text
    # one auto-queue file per site, with that site's rec ids only
    qs = [storage.read_json(k) for k in _queue_files(storage)]
    by_site = {q["site"]: q for q in qs}
    assert set(by_site) == {"site-a", "site-b"}
    assert by_site["site-a"]["source_agent"] == "event-refresh-agent"
    assert by_site["site-a"]["rec_ids"] == [mixer["id"]]
    assert by_site["site-b"]["rec_ids"] == [gpu["id"]]


def test_idempotent_never_queues_same_article_twice(storage, agent_env):
    make, _ = agent_env
    make().run()
    for k in _queue_files(storage):
        storage.delete(k)
    res = make().run()
    assert res.metrics["recs_queued"] == 0
    assert res.metrics["already_queued"] == 2
    assert _queue_files(storage) == []
    ledger = storage.read_json("agents/event-refresh-agent/state/event-refresh-ledger.json")
    assert "site-a|prime-big-deal-days-kitchen|2026-10-06|best-stand-mixers-2026-prime-big-deal-days" in ledger


def test_nothing_before_event_ends(storage, agent_env, monkeypatch):
    make, calls = agent_env
    monkeypatch.setenv("EVENT_REFRESH_TODAY", "2026-10-07")
    res = make().run()
    assert res.metrics["recs_queued"] == 0 and _queue_files(storage) == []
    # last deal before Oct 7 in the 60-day horizon: none ended -> no DB call
    assert calls == []


def test_catch_up_within_lookback_but_not_after(storage, agent_env, monkeypatch):
    make, _ = agent_env
    monkeypatch.setenv("EVENT_REFRESH_TODAY", "2026-10-14")
    assert make().run().metrics["recs_queued"] == 2
    for k in _queue_files(storage):
        storage.delete(k)
    storage.delete("agents/event-refresh-agent/state/event-refresh-ledger.json")
    monkeypatch.setenv("EVENT_REFRESH_TODAY", "2026-10-20")
    res = make().run()
    assert res.metrics["recs_queued"] == 0
    # still counted as a live page framed around an ended sale
    assert res.metrics["ended_event_articles_live"] == 2


def test_dry_run_writes_nothing(storage, agent_env, monkeypatch):
    make, _ = agent_env
    monkeypatch.setenv("EVENT_REFRESH_DRY_RUN", "1")
    res = make().run()
    assert res.metrics["recs_queued"] == 0
    assert res.metrics["unqueued_ended_event_articles"] == 2
    assert _queue_files(storage) == []
    assert storage.read_json("agents/event-refresh-agent/state/event-refresh-ledger.json") is None


def test_one_site_failing_does_not_block_other(storage, agent_env):
    make, _ = agent_env
    a = make()
    good = a.fetch_articles

    def flaky(dsn, tags, site_cfg, limit=500):
        if dsn.endswith("//b"):
            raise RuntimeError("connection refused")
        return good(dsn, tags, site_cfg)
    a.fetch_articles = flaky
    a.state[a.SIGNALS_STATE_KEY] = "abc"
    res = a.run()
    assert res.metrics["recs_queued"] == 1 and res.metrics["site_errors"] == 1
    assert a.SIGNALS_STATE_KEY not in res.next_state  # next run retries


def test_per_site_cap(storage, agent_env):
    make, _ = agent_env
    cfg = storage.read_json(era.CONFIG_KEY)
    cfg["max_recs_per_site"] = 0
    storage.write_json(era.CONFIG_KEY, cfg)
    res = make().run()
    assert res.metrics["recs_queued"] == 0
    assert res.metrics["unqueued_ended_event_articles"] == 2


def test_signals_short_circuit_only_when_nothing_ended(storage, agent_env, monkeypatch):
    make, _ = agent_env
    monkeypatch.setenv("EVENT_REFRESH_TODAY", "2026-10-08")
    assert make().signals() is None
    # Jan 2027: Black Friday (Nov 27-30) ended > 7d ago but within 60d -> still runs
    monkeypatch.setenv("EVENT_REFRESH_TODAY", "2027-01-20")
    assert make().signals() is None
    monkeypatch.setenv("EVENT_REFRESH_TODAY", "2027-03-15")
    sig = make().signals()
    assert sig == {"ended_events": [], "enabled": True}


def test_resolve_sites_discovers_env_when_unconfigured():
    cfg = era.load_config(None, repo_config=Path("/nonexistent"))
    sites = era.resolve_sites(cfg, {"DATABASE_URL_FOO_BAR": "x", "DATABASE_URL": "y",
                                    "DATABASE_URL_EMPTY": ""})
    assert list(sites) == ["foo-bar"]
    assert sites["foo-bar"]["dsn_env"] == "DATABASE_URL_FOO_BAR"
    assert sites["foo-bar"]["table"] == "editorial_articles"


def test_repo_config_is_valid_and_site_names_stay_out_of_code():
    cfg = era.load_config(None)
    assert cfg["kinds"] == ["retail-deal"]
    for site, s in era.resolve_sites(cfg, {}).items():
        assert "{slug}" in s["article_url_template"]
        assert s["dsn_env"].startswith("DATABASE_URL")
    src = (AGENT_DIR / "agent.py").read_text().lower()
    assert "aisleprompt" not in src and "specpicks" not in src


def test_manifest_and_goals_bind_emitted_metrics(storage, agent_env):
    make, _ = agent_env
    metrics = make().run().metrics
    goals = json.loads((AGENT_DIR / "goals.json").read_text())["goals"]
    assert 3 <= len(goals) <= 5
    for g in goals:
        assert g["target_metric"] in metrics
        assert g["metric"]["name"] == g["target_metric"]
    man = json.loads((AGENT_DIR / "manifest.json").read_text())
    assert man["id"] == "event-refresh-agent"
    assert man["entry_command"].endswith("agents/event-refresh-agent/agent.py")
    assert man["cron_expr"]


def test_article_refresh_routes_to_implementer():
    from framework.core.work_types import handler_for
    assert handler_for("article-refresh") == ("body_md_edit", "")


def test_rec_without_next_occasion_goes_evergreen():
    w = sc.event_window(next(o for o in sc.DEFAULT_OCCASIONS
                             if o.id == "prime-day"), 2026)
    rec = era.build_refresh_rec("evt-001", site="s", article={"slug": "x", "title": "X"},
                                event=w, next_event=None)
    assert "evergreen" in rec["rationale"] and rec["next_occasion"] is None
    assert "Remove `holiday:prime-day`" in rec["rationale"]
