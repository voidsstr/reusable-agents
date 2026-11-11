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


# ── review fixes (2026-11-11): occasion words, dispatchability, page holds,
#    deferred window, new placements, malformed LLM fields ──────────────────

# Real levers from one strategist's ledger (2026-11-08..11-10).
CYBER_LEDGER = [
    {"id": "g-probe", "state": "live",
     "title": "Probe and protect second-tier click guides before the Cyber Week window",
     "target_urls": ["/blog/best-immersion-blenders-2026", "/blog/best-stainless-steel-cookware-2026",
                     "/blog/best-pantry-organization-sets-2026", "/blog/best-kitchen-utensil-sets-2026"]},
    {"id": "g-plan", "state": "implemented",
     "title": "Date-gated Thanksgiving dinner plan with Instacart cart and gear links on the homepage",
     "target_urls": ["/", "/blog/best-carving-knives-turkey-brisket-roasts"]},
]


def test_occasion_words_never_make_two_levers_the_same_change():
    # different change, overlapping pages, only {cyber, week} in common
    picks = {"title": "Resolve open answer picks before Cyber Week: re-pick the rice cooker and kettle",
             "target_urls": ["/blog/best-pantry-organization-sets-2026",
                             "/blog/best-kitchen-utensil-sets-2026", "/blog/best-rice-cookers-2026"]}
    assert rm.match_shipped(picks, CYBER_LEDGER) is None
    badge = {"title": "Cyber Week sticky price-drop badge on the kettle, rice cooker and pantry guides",
             "target_urls": ["/blog/best-pantry-organization-sets-2026",
                             "/blog/best-kitchen-utensil-sets-2026"]}
    assert rm.match_shipped(badge, CYBER_LEDGER) is None
    # only {gear, thanksgiving} in common with the homepage plan lever
    strip = {"title": "Thanksgiving roasting gear strip on the carving knife guide",
             "target_urls": ["/blog/best-carving-knives-turkey-brisket-roasts"]}
    assert rm.match_shipped(strip, CYBER_LEDGER) is None
    # the same change re-worded still matches, occasion words or not
    again = {"title": "Probe and protect the second-tier guides ahead of Cyber Monday",
             "target_urls": ["/blog/best-stainless-steel-cookware-2026"]}
    m = rm.match_shipped(again, CYBER_LEDGER)
    assert m and m["id"] == "g-probe"


def test_occasion_names_come_from_the_calendar_as_phrases():
    phrases = rm.occasion_phrases()
    for p in ("black friday", "thanksgiving", "super bowl", "mothers day", "back to school"):
        assert p in phrases
    assert list(phrases) == sorted(phrases, key=lambda p: (-len(p), p))     # longest first
    # a phrase goes, the same word in another sense stays
    assert rm.lever_tokens("Super Bowl snack picks next to the mixing bowl guide") == \
        frozenset({"snack", "picks", "next", "mixing", "bowl"})
    assert rm.lever_tokens("Mother's Day brunch gear") == frozenset({"brunch", "gear"})
    assert rm.lever_tokens("Back-to-school lunchbox picks; back in stock alerts") == \
        frozenset({"lunchbox", "picks", "back", "stock", "alerts"})
    assert rm.lever_tokens("Black Friday / Cyber Monday deals hub") == frozenset({"hub"})
    assert rm.lever_tokens(None) == frozenset() and rm.lever_tokens(12345) == frozenset()


def test_refills_must_be_dispatchable_and_operator_levers_are_dropped():
    recs = [
        _rec("Answer picks lead the meal-prep containers guide on our checks", ships_via="operator"),
        _rec("Sticky mobile bar shows the fresh price"),
        _rec("Add the Bing Webmaster API key so per-query retitles can start", tier="smaller",
             ships_via="operator"),
        _rec("Decide the out-of-stock fallback for the pressure cooker pick", tier="smaller",
             ships_via=None),
        _rec("Stand mixer guide gets a dated price check", tier="smaller", ships_via="db"),
    ]
    sel, dropped = rm.select_levers(recs, [], max_n=3, primary_metrics=PRIMARY)
    assert [r["title"] for r in sel] == [recs[1]["title"], recs[4]["title"]]
    assert dropped[0]["rec"] is recs[0] and dropped[0]["reason"].startswith("not dispatchable")
    # no refill at all when no smaller rec says db or code
    sel, _ = rm.select_levers(recs[:4], [], max_n=3, primary_metrics=PRIMARY)
    assert [r["title"] for r in sel] == [recs[1]["title"]]
    # a lever without ships_via is still dispatched (older memos)
    sel, _ = rm.select_levers([_rec("Lever with no ships via at all", ships_via=None)], [],
                              primary_metrics=PRIMARY)
    assert len(sel) == 1


def test_levers_on_held_pages_are_dropped_and_refilled():
    from framework.core import page_hold
    held = "/blog/best-pressure-cookers-2026"
    holds = page_hold.holds_from_site_config({"page_holds": [
        {"path": held, "reason": "picks need a person", "until": "2099-12-31"}]})
    recs = [
        _rec("Re-pick the pressure cooker guide's answer picks", ships_via="db", target_urls=[held]),
        _rec("Dated price checks on every buying guide", ships_via="db", target_urls=["/blog/best-*"]),
        _rec("Sticky mobile bar shows the fresh price", target_urls=["/product/*"]),
        _rec("Cookware guides lead with in-stock picks", tier="smaller", ships_via="db",
             target_urls=["/blog/best-*"]),                                   # held prefix: skipped
        _rec("Recipe pages link their exact gear", tier="smaller", ships_via="db",
             target_urls=["/recipes/*"]),
    ]
    sel, dropped = rm.select_levers(recs, [], max_n=3, primary_metrics=PRIMARY, holds=holds)
    assert [r["title"] for r in sel] == [recs[2]["title"], recs[4]["title"]]
    hold_drops = [d for d in dropped if d.get("held")]
    assert [d["rec"]["title"] for d in hold_drops] == [recs[0]["title"], recs[1]["title"]]
    assert hold_drops[0]["held"] == [held] and hold_drops[0]["reason"].startswith("page-hold")
    summary = rm.selection_summary(sel, dropped, {"status": "red"})
    assert summary["held_dropped"] == 2 and summary["repeats_dropped"] == 0
    assert summary["deploy_gate"] == "red" and summary["selected"][0]["target_urls"] == ["/product/*"]
    # no holds: nothing dropped for a hold
    sel, dropped = rm.select_levers(recs, [], max_n=3, primary_metrics=PRIMARY)
    assert not any(d.get("held") for d in dropped)
    # a memo rec cannot release its own hold (hold_override is a person's call)
    self_released = [{**recs[0], "hold_override": True}]
    sel, dropped = rm.select_levers(self_released, [], primary_metrics=PRIMARY, holds=holds)
    assert sel == [] and dropped[0]["held"] == [held]


def test_deferred_levers_block_repeats_only_for_48_hours():
    deferred = {"id": "g-def", "state": "deferred", "run_ts": "20261108T101201Z",
                "title": "Holiday baking cluster: cookie recipes link the baking gear guides",
                "deferred_reason": "click-audit gate failed: fix the filter first"}
    again = _rec("Cookie recipes link the baking gear guides (baking cluster)")
    soon = NOW.replace(day=9)                         # 2026-11-09: within 48h of the run
    sel, dropped = rm.select_levers([again], [deferred], primary_metrics=PRIMARY, now=soon)
    assert sel == [] and dropped[0]["matched"]["id"] == "g-def"
    sel, _ = rm.select_levers([again], [deferred], primary_metrics=PRIMARY, now=NOW)  # 3+ days later
    assert sel == [again]
    # deferred_at, when recorded, is the clock, not the run_ts
    recent = {**deferred, "deferred_at": (NOW - timedelta(hours=10)).isoformat()}
    sel, _ = rm.select_levers([again], [recent], primary_metrics=PRIMARY, now=NOW)
    assert sel == []
    assert rm.blocking_levers([recent, {**deferred}], now=NOW) == [recent]
    # live / pre-existing levers keep blocking for the whole ledger window
    live = {**deferred, "state": "live"}
    assert rm.select_levers([again], [live], primary_metrics=PRIMARY, now=NOW)[0] == []


def test_shipped_levers_records_deferred_at(storage):
    _write_run(storage, NOW - timedelta(days=2), [
        {"id": "d1", "title": "Deferred lever", "implemented": False, "deferred": True,
         "deferred_at": "2026-11-09T19:00:00+00:00", "deferred_reason": "x"}])
    led = rm.shipped_levers(storage, AGENT, now=NOW)
    assert led[0]["deferred_at"] == "2026-11-09T19:00:00+00:00"


def test_a_new_placement_is_judged_on_its_pages():
    def o(before, after, pb, pa, sb=100, sa=100, pages_scoped=True):
        return {**_outcome(before, after, sb, sa), "pages_before": pb, "pages_after": pa,
                "pages_scoped": pages_scoped}
    # 6 clicks on the new strip, but its pages did not gain: moved off pdp-hero
    assert rm.lever_verdict(o(0, 6, 40, 41)) == "no-lift"
    assert rm.lever_verdict(o(0, 6, 40, 52)) == "lift"
    assert rm.lever_verdict(o(0, 6, 40, 52, sb=100, sa=200)) == "no-lift"    # site doubled
    assert rm.lever_verdict(o(0, 1, 40, 52)) == "no-lift"                   # the strip itself
    assert rm.lever_verdict(o(0, 6, 0, 0, pages_scoped=False)) == "new-surface"
    assert rm.lever_verdict(o(0, 6, 0, 6)) == "lift"                        # pages had nothing
    assert rm.lever_verdict(o(3, 9, 40, 41)) == "lift"                      # had clicks: own counts
    # a brand-new page with no clicks before must beat the site's own growth
    assert rm.lever_verdict(_outcome(0, 2, 50, 200)) == "no-lift"
    ledger = [{"id": "g-n", "title": "New strip", "state": "live",
               "live_since": (NOW - timedelta(days=8)).isoformat(), "placements": ["strip"],
               "target_urls": ["/product/a"], "outcome": o(0, 6, 40, 41)}]
    block = rm.shipped_levers_prompt_block(ledger, now=NOW)
    assert "judged on its pages, all placements: before=40 after=41" in block
    assert "verdict=no-lift" in block


def test_malformed_llm_fields_never_raise(storage):
    bad = [_rec("Numeric targets lever", target_urls=5, placements=7),
           _rec("Dict targets lever", target_urls={"a": 1}),
           {"title": 12345, "tier": "lever", "expected_metric": "human_amazon_clicks_30d"},
           _rec("Nested targets lever", target_urls=[["/a"], None, "/b"])]
    ledger = [{"id": "x", "state": "live", "title": "Numeric targets lever", "target_urls": 9}]
    sel, dropped = rm.select_levers(bad, ledger, max_n=5, primary_metrics=PRIMARY)
    assert len(sel) + len(dropped) == 4
    assert rm._as_list(5) == ["5"] and rm._as_list({"a": 1}) == [] and rm._as_list(True) == []
    assert rm._as_list([["/a"], None, "/b"]) == ["/b"]
    # one malformed rec in storage does not switch the ledger off
    _write_run(storage, NOW - timedelta(days=1), [
        {"id": "m1", "title": 777, "implemented": True, "shipped": True,
         "shipped_at": "2026-11-10T00:00:00+00:00", "target_urls": 5, "placements": {"p": 1}},
        {"id": "m2", "title": "Good lever", "implemented": True,
         "implemented_via": "pre-existing"}])
    led = rm.shipped_levers(storage, AGENT, now=NOW)
    by = {e["id"]: e for e in led}
    assert by["m2"]["state"] == "pre-existing"
    assert by["m1"]["target_urls"] == ["5"] and by["m1"]["placements"] == []


def test_ledger_metrics_and_selection_summary():
    ledger = [{"state": "pre-existing"},
              {"state": "live", "outcome": _outcome(10, 11, 100, 100)},
              {"state": "live", "outcome": _outcome(10, 20, 100, 100)},
              {"state": "implemented"}, {"state": "deferred"}, "junk"]
    m = rm.ledger_metrics(ledger)
    assert m == {"pre_existing_lever_share_14d": 0.25, "shipped_levers_no_lift_14d": 1.0,
                 "shipped_levers_lift_14d": 1.0}
    assert rm.ledger_metrics([]) == {} and rm.ledger_metrics(None) == {}
    s = rm.selection_summary([], [{"rec": {"title": "t", "tier": "lever"}, "reason": "r",
                                   "matched": {"id": "g"}}, "junk"])
    assert s["dropped"] == [{"title": "t", "tier": "lever", "reason": "r",
                             "matched": {"id": "g"}, "held": None}]
    assert s["repeats_dropped"] == 1 and s["held_dropped"] == 0 and s["deploy_gate"] is None


def test_red_gate_line_never_invites_hand_written_prices():
    line = rm.deploy_gate_prompt_line({"site": "site-a", "status": "red"})
    assert "Noneh" not in line and "price /" not in line
    assert "Never write a price by hand" in line and "price-refresh pipeline" in line
    assert "At most one code lever is" in line
    assert "No code lever is" in rm.deploy_gate_prompt_line({"status": "red"}, max_code=0)
    assert "At most 2 code levers are" in rm.deploy_gate_prompt_line({"status": "red"}, max_code=2)


def test_shipped_levers_section_lists_held_pages():
    from framework.core import page_hold
    holds = page_hold.holds_from_site_config({"page_holds": [
        {"path": "/blog/best-a", "reason": "needs a person", "until": "2099-01-01"}]})
    body = rm.shipped_levers_section([], {"site": "site-a", "status": "green",
                                          "last_deployed_tag": "t1"}, holds, max_chars=50)
    first, held, block = body.split("\n\n")
    assert first.startswith("DEPLOY GATE: green")
    assert held.startswith("HELD PAGES") and "/blog/best-a (needs a person; until 2099-01-01)" in held
    assert block == rm.shipped_levers_prompt_block([])[:50]
    assert "HELD PAGES" not in rm.shipped_levers_section([], {}, [])
