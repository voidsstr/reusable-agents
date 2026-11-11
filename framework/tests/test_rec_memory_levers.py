"""Tests for the shipped-lever ledger in framework.core.rec_memory.

Storage is LocalFilesystemStorage (conftest `storage` fixture); the deploy
gate state is a tmp dir via DEPLOY_GATE_STATE_DIR.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from framework.core import rec_memory as rm

NOW = datetime(2026, 11, 11, 18, 0, tzinfo=timezone.utc)
AGENT = "site-a-user-growth-strategist"


def _ts(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%SZ")


def _write_run(storage, when: datetime, recs: list[dict], agent: str = AGENT) -> str:
    run_ts = _ts(when)
    storage.write_json(f"agents/{agent}/runs/{run_ts}/recommendations.json",
                       {"run_ts": run_ts, "mode": "growth", "recommendations": recs})
    return run_ts


def test_load_run_recs_reads_only_the_window(storage):
    old = _write_run(storage, NOW - timedelta(days=20), [{"id": "o", "title": "Old lever"}])
    mid = _write_run(storage, NOW - timedelta(days=5), [{"id": "m1", "title": "Mid lever"},
                                                        {"id": "m2", "title": ""}])
    new = _write_run(storage, NOW - timedelta(hours=5), [{"id": "n", "title": "New lever"}])
    storage.write_json(f"agents/{AGENT}/runs/not-a-run/recommendations.json",
                       {"recommendations": [{"id": "x", "title": "junk"}]})
    # a run dir without recommendations.json (nothing dispatched) is skipped
    storage.write_json(f"agents/{AGENT}/runs/{_ts(NOW - timedelta(days=1))}/recs.json", [])
    got = rm.load_run_recs(storage, AGENT, days=14, now=NOW)
    assert [(r["id"], r["run_ts"]) for r in got] == [("m1", mid), ("n", new)]
    assert old not in {r["run_ts"] for r in got}
    assert rm.load_run_recs(storage, "nobody", now=NOW) == []


def test_lever_status_states():
    shipped = {"implemented": True, "shipped": True, "shipped_at": "2026-11-10T20:53:22+00:00"}
    assert rm.lever_status(shipped) == ("live", "2026-11-10T20:53:22+00:00")
    assert rm.lever_status({"implemented": True, "implemented_via": "pre-existing",
                            "shipped": True, "shipped_at": "2026-11-10T20:53:22+00:00"}) == \
        ("pre-existing", None)
    db = {"implemented": True, "ships_via": "DB", "implemented_at": "2026-11-11T01:20:35.338330+00:00"}
    assert rm.lever_status(db) == ("live", "2026-11-11T01:20:35.338330+00:00")
    assert rm.lever_status({"implemented": True, "implemented_via": "commit 34fc3b21"}) == \
        ("implemented", None)
    assert rm.lever_status({"implemented": False, "deferred": True}) == ("deferred", None)
    assert rm.lever_status({}) == ("dispatched", None)


def test_shipped_levers_ledger_fields_and_order(storage):
    a = _write_run(storage, NOW - timedelta(days=3), [
        {"id": "g-a-01", "title": "Route unavailable AI-fetched PDPs to live eBay counterpart",
         "tier": "lever", "expected_metric": "human_amazon_clicks_30d",
         "implemented": True, "implemented_at": "2026-11-08T06:00:00+00:00",
         "implemented_commit": "3564a73b9", "shipped": True,
         "shipped_at": "2026-11-08T06:10:00+00:00", "shipped_tag": "20261108-0605",
         "target_urls": "/product/B0AAA", "placements": ["pdp-hero"]},
        {"id": "g-a-02", "title": "Refresh decayed cited pages", "implemented": False,
         "deferred": True, "deferred_reason": "needs web research"},
        {"id": "g-a-03", "title": "In flight lever"},
    ])
    b = _write_run(storage, NOW - timedelta(hours=6), [
        {"id": "g-b-01", "title": "Black Friday rows click out", "implemented": True,
         "implemented_via": "commit d56e5068"},
        {"id": "g-b-02", "title": "Answer-first openings", "implemented": True,
         "implemented_via": "pre-existing", "implemented_commits": ["aaaa1111", "bbbb2222"]},
    ])
    led = rm.shipped_levers(storage, AGENT, now=NOW)
    assert [e["id"] for e in led] == ["g-b-02", "g-b-01", "g-a-02", "g-a-01"]   # newest first
    by = {e["id"]: e for e in led}
    assert by["g-a-01"]["state"] == "live" and by["g-a-01"]["commit"] == "3564a73b9"
    assert by["g-a-01"]["target_urls"] == ["/product/B0AAA"]      # a bare string becomes a list
    assert by["g-a-01"]["placements"] == ["pdp-hero"]
    assert by["g-a-02"]["state"] == "deferred" and by["g-a-02"]["deferred_reason"]
    assert by["g-b-01"]["state"] == "implemented" and by["g-b-01"]["commit"] == "d56e5068"
    assert by["g-b-02"]["commit"] == "aaaa1111,bbbb2222"
    assert by["g-b-01"]["run_ts"] == b and by["g-a-01"]["run_ts"] == a
    assert "g-a-03" not in by                                  # in flight: not shipped yet
    live_only = rm.shipped_levers(storage, AGENT, now=NOW, include_states=("live",))
    assert [e["id"] for e in live_only] == ["g-a-01"]


def _outcome(before, after, site_before, site_after, after_days=7.0, scoped=True):
    return {"before": before, "after": after, "site_before": site_before,
            "site_after": site_after, "days": 7, "after_days": after_days, "scoped": scoped}


def test_lever_verdict():
    assert rm.lever_verdict(None) == "n/a"
    assert rm.lever_verdict(_outcome(1, 9, 10, 10, scoped=False)) == "unscoped"
    assert rm.lever_verdict(_outcome(1, 9, 10, 10, after_days=3.5)) == "too-early"
    assert rm.lever_verdict(_outcome(10, 20, 100, 110)) == "lift"
    assert rm.lever_verdict(_outcome(0, 3, 100, 100)) == "lift"
    assert rm.lever_verdict(_outcome(10, 11, 100, 100)) == "no-lift"     # +1 < min_gain
    assert rm.lever_verdict(_outcome(10, 15, 100, 200)) == "no-lift"     # site doubled
    assert rm.lever_verdict(_outcome(5, 2, 100, 100)) == "no-lift"


# Real titles from one strategist's 2026-11-08..11-11 memos.
SHIPPED = [
    {"id": "g1", "state": "live", "title": "Route unavailable AI-fetched PDPs to live eBay counterpart or in-stock substitute"},
    {"id": "g2", "state": "pre-existing", "title": "Priced one-click Amazon cart on the gaming-PC build cluster"},
    {"id": "g3", "state": "live", "title": "Black Friday page: dated, discount-aware answer sentence plus live data",
     "target_urls": ["/blog/black-friday-kitchen-deals-2026"]},
]


def test_match_shipped_catches_rewordings():
    m = rm.match_shipped({"title": "In-stock Amazon substitute takes the hero CTA on AI-fetched "
                                   "unavailable/not-found PDPs"}, SHIPPED)
    assert m and m["id"] == "g1" and m["match_reason"] == "title"
    m = rm.match_shipped({"title": "One-click whole-build Amazon cart on gaming-pc-build cluster"},
                         SHIPPED)
    assert m and m["id"] == "g2"
    assert rm.match_shipped({"title": "Rebuild the 12GB VRAM local-LLM hub and link the spokes"},
                            SHIPPED) is None


def test_match_shipped_pages_rule_ignores_the_pages_own_words():
    # same page, different change: only "black friday" (the page's slug) in common
    other_change = {"title": "Black Friday page: per-pick price-history label from our own checks",
                    "target_urls": ["https://site.test/blog/black-friday-kitchen-deals-2026?x=1"]}
    assert rm.match_shipped(other_change, SHIPPED) is None
    # same page, same change re-worded
    same_change = {"title": "Black Friday answer sentence becomes discount-aware with dated data",
                   "target_urls": ["/blog/black-friday-kitchen-deals-2026/"]}
    m = rm.match_shipped(same_change, SHIPPED)
    assert m and m["id"] == "g3"


def test_page_overlap_with_prefixes():
    assert rm.page_overlap(["/reviews/*"], ["/reviews/a", "/x"]) == 1.0
    assert rm.page_overlap(["/a", "/b"], ["https://s.test/a/", "/c", "/d"]) == 0.5
    assert rm.page_overlap([], ["/a"]) == 0.0


def _rec(title, tier="lever", metric="human_amazon_clicks_30d", ships_via="code", **kw):
    return {"title": title, "tier": tier, "expected_metric": metric, "ships_via": ships_via, **kw}


PRIMARY = ("human_amazon_clicks_30d",)


def test_select_levers_drops_repeats_and_refills_with_next_amazon_rec():
    recs = [
        _rec("Swap the hero CTA on live-fetched unavailable PDPs to an in-stock substitute plus eBay"),
        _rec("Refresh decayed cited local-LLM pages", metric="ai_referral_landings_30d"),
        _rec("Sticky mobile buy bar shows the fresh price on product pages"),
        _rec("Monitor roundup comparison table gains per-row buy buttons", tier="smaller"),
        _rec("Answer-first lead on benchmark pages", tier="smaller",
             metric="ai_referral_landings_30d"),
    ]
    sel, dropped = rm.select_levers(recs, SHIPPED, max_n=3, primary_metrics=PRIMARY)
    titles = [r["title"] for r in sel]
    # the repeat is gone, its slot went to the next AMAZON rec, AI lever last
    assert titles == [recs[2]["title"], recs[3]["title"], recs[1]["title"]]
    assert dropped[0]["reason"] == "repeats a shipped lever"
    assert dropped[0]["matched"]["id"] == "g1"


def test_select_levers_secondary_metric_only_in_the_last_slot():
    recs = [_rec("AI lever one", metric="ai_referrals_30d"),
            _rec("AI lever two", metric="ai_referrals_30d"),
            _rec("Amazon lever one")]
    sel, dropped = rm.select_levers(recs, [], max_n=3, primary_metrics=PRIMARY)
    assert [r["title"] for r in sel] == ["Amazon lever one", "AI lever one"]
    assert dropped[0]["rec"]["title"] == "AI lever two"
    assert "secondary metric" in dropped[0]["reason"]


def test_select_levers_prefers_db_only_while_the_gate_is_red():
    recs = [_rec("Code lever A"), _rec("Code lever B"),
            _rec("DB lever C", ships_via="db"),
            _rec("Smaller DB lever D", tier="smaller", ships_via="db"),
            _rec("Smaller code lever E", tier="smaller")]
    sel, dropped = rm.select_levers(recs, [], max_n=3, primary_metrics=PRIMARY,
                                    prefer_db=True, max_code=1)
    assert [r["title"] for r in sel] == ["DB lever C", "Smaller DB lever D", "Code lever A"]
    held = [d for d in dropped if d["reason"].startswith("deploy gate red")]
    assert [d["rec"]["title"] for d in held] == ["Code lever B"]
    assert "DB-only" in held[0]["reason"]
    # gate green: memo order, no hold
    sel, _ = rm.select_levers(recs, [], max_n=3, primary_metrics=PRIMARY)
    assert [r["title"] for r in sel] == ["Code lever A", "Code lever B", "DB lever C"]


def test_select_levers_caps_and_handles_no_refill():
    recs = [_rec("Priced one-click Amazon cart on the gaming-PC build ladder pages")]
    sel, dropped = rm.select_levers(recs, SHIPPED, max_n=3, primary_metrics=PRIMARY)
    assert sel == [] and dropped[0]["matched"]["id"] == "g2"
    many = [_rec(f"Distinct lever number {w}") for w in ("alpha", "beta", "gamma", "delta")]
    sel, dropped = rm.select_levers(many, [], max_n=3, primary_metrics=PRIMARY)
    assert len(sel) == 3 and dropped[-1]["reason"].startswith("over the per-run cap")


def test_shipped_levers_prompt_block_renders_outcomes():
    ledger = [
        {"id": "g-1", "title": "Sticky mobile price", "state": "live",
         "live_since": (NOW - timedelta(days=9)).isoformat(), "expected_metric": "human_amazon_clicks_30d",
         "target_urls": ["/product/a", "/product/b", "/product/c", "/product/d", "/product/e"],
         "placements": ["sticky-mobile"],
         "outcome": _outcome(10, 11, 100, 120)},
        {"id": "g-2", "title": "Same thing again", "state": "pre-existing"},
        {"id": "g-3", "title": "Needs research", "state": "deferred", "deferred_reason": "web research"},
        {"id": "g-4", "title": "Waiting", "state": "implemented"},
    ]
    block = rm.shipped_levers_prompt_block(ledger, now=NOW)
    assert block.startswith("SHIPPED LEVERS (last 14 days")
    assert rm.SHIPPED_LEVER_RULE in block
    assert "[live since 2026-11-02 18:00Z, 9.0d live] g-1" in block
    assert "pages=/product/a,/product/b,/product/c,/product/d (+1)" in block
    assert "placements=sticky-mobile" in block
    assert "before=10 after(7.0d)=11 | site before=100 after=120 | verdict=no-lift" in block
    assert "[pre-existing] g-2" in block
    assert "[deferred: web research] g-3" in block
    assert "[implemented, not deployed yet] g-4" in block
    assert "1 no-lift after 7d live" in block
    assert "(none in the window)" in rm.shipped_levers_prompt_block([], now=NOW)


def test_deploy_gate_status_and_prompt_line(tmp_path, monkeypatch):
    monkeypatch.setenv("DEPLOY_GATE_STATE_DIR", str(tmp_path))
    assert rm.deploy_gate_status("site-a")["status"] == "unknown"
    red_since = (datetime.now(timezone.utc) - timedelta(hours=50)).isoformat()
    (tmp_path / "site-a.json").write_text(json.dumps({
        "site": "site-a", "status": "red", "red_since": red_since,
        "last_reason": "code: 5 test(s) failed", "last_deployed_tag": "20261109-1348"}))
    g = rm.deploy_gate_status("site-a")
    assert g["status"] == "red" and 49.9 <= g["hours_red"] <= 50.1
    line = rm.deploy_gate_prompt_line(g)
    assert "RED" in line and "DB-ONLY" in line and "20261109-1348" in line
    (tmp_path / "site-b.json").write_text(json.dumps({
        "site": "site-b", "status": "green", "last_deployed_tag": "20261111-1737"}))
    g = rm.deploy_gate_status("site-b")
    assert g["status"] == "green" and g["hours_red"] is None
    assert "green" in rm.deploy_gate_prompt_line(g)
    assert rm.deploy_gate_status("")["status"] == "unknown"


def test_no_site_literals_in_module():
    import inspect
    src = inspect.getsource(rm).lower()
    for site in ("aisleprompt", "specpicks"):
        assert site not in src
