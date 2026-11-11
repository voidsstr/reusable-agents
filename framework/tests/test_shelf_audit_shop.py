"""shelf-audit-agent: crawler hygiene (SHOP-04) and autonomous dead-pick
remedies (SHOP-18). Offline: a local HTTP server stands in for the site."""
from __future__ import annotations

import http.server
import sys
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

_DIR = Path(__file__).resolve().parents[2] / "agents" / "shelf-audit-agent"
sys.path.insert(0, str(_DIR))

import answer_picks as ap  # noqa: E402
import shelf  # noqa: E402

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)


# ---------- SHOP-04: crawl skip, same-host redirects, probe headers ----------

class _Handler(http.server.BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        _Handler.seen.append((self.path, {k.lower(): v for k, v in self.headers.items()}))
        port = self.server.server_address[1]
        if self.path.startswith("/k/"):
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.2:%d/dp/B000000000?tag=x" % port)
            self.end_headers()
        elif self.path == "/moved":
            self.send_response(301)
            self.send_header("Location", "/page")
            self.end_headers()
        else:
            body = b'<html><a href="/page">p</a><a href="/k/thing">buy</a></html>'
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


@pytest.fixture()
def site():
    _Handler.seen = []
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield "http://127.0.0.1:%d" % srv.server_address[1]
    srv.shutdown()


def test_fetch_refuses_off_host_redirect(site):
    assert shelf.fetch(site + "/k/thing") == ""
    # only the /k/ request reached a server; the tagged Location was not followed
    assert [p for p, _ in _Handler.seen] == ["/k/thing"]


def test_fetch_follows_same_host_redirect_and_sends_probe_header(site):
    html = shelf.fetch(site + "/moved", headers={"x-site-probe": "shelf-audit"})
    assert "href" in html
    assert [p for p, _ in _Handler.seen] == ["/moved", "/page"]
    assert all(h.get("x-site-probe") == "shelf-audit" for _, h in _Handler.seen)


def test_crawl_never_enqueues_skipped_paths(site):
    pages = shelf.crawl(site, max_depth=2, per_depth=(None, 10, 10),
                        seeds=[site + "/k/seeded"], skip_path_regex=r"^/k/",
                        headers={"x-site-probe": "1"}, workers=2)
    assert not any(p.startswith("/k/") for p, _ in _Handler.seen)
    assert site + "/page" in pages


def test_crawl_without_skip_still_never_leaves_host(site):
    shelf.crawl(site, max_depth=1, per_depth=(None, 10), workers=2)
    assert "/k/thing" in [p for p, _ in _Handler.seen]  # requested, but not followed


def test_skip_path_matches_path_only():
    assert shelf.skip_path("https://x.test/k/a?b=1", r"^/k/")
    assert not shelf.skip_path("https://x.test/blog/k/a", r"^/k/")
    assert not shelf.skip_path("https://x.test/k/a", None)


def test_probe_sends_configured_headers(site):
    hdrs = ap.probe_headers({"probe_headers": {"x-site-probe": "shelf-audit"}})
    res = ap.probe(site + "/k/thing", headers=hdrs)
    assert res["status"] == 302 and "/dp/" in res["location"]
    assert _Handler.seen[0][1].get("x-site-probe") == "shelf-audit"
    assert hdrs["User-Agent"] == ap.UA["User-Agent"]


# ---------- SHOP-18: swap remedy ----------

BODY = (
    "## Best Overall\nThe [Acme Pro](/k/acme-pro) holds 6 quarts and sears well.\n"
    "For a tight budget the [Cheapo 3](/k/cheapo-3) is the best value under $40.\n"
    "Our premium splurge is the [Lux 9](/kitchen/lux-9), with a heavier pot.\n"
    "The [Dead One](/k/dead-one) was our pick.\n"
)
CANDS = [
    {"slug": "acme-pro", "title": "Acme Pro 6qt Slow Cooker", "price_cents": 9900},
    {"slug": "cheapo-3", "title": "Cheapo 3qt Slow Cooker", "price_cents": 3500},
    {"slug": "lux-9", "title": "Lux 9qt Slow Cooker", "price_cents": 29900},
    {"slug": "not-on-page", "title": "Elsewhere Slow Cooker", "price_cents": 1000},
    {"slug": "unpriced", "title": "No price Slow Cooker", "price_cents": None},
]


def test_evidence_sentence_strips_links():
    ev = ap.evidence_sentence(BODY, "cheapo-3")
    assert ev.startswith("For a tight budget the Cheapo 3 is the best value")
    assert ap.evidence_sentence(BODY, "lux-9").startswith("Our premium splurge")
    assert ap.evidence_sentence(BODY, "missing") == ""


def test_choose_swap_budget_label_takes_cheapest_with_evidence():
    sw = ap.choose_swap(page="/blog/g", label="Budget Pick", broken_slug="dead-one",
                        candidates=CANDS, body=BODY, used_slugs=["dead-one"])
    assert sw["action"] == "swap" and sw["replacement"] == "cheapo-3"
    assert "best value" in sw["evidence"]


def test_choose_swap_skips_other_picks_unpriced_and_unevidenced():
    sw = ap.choose_swap(page="/blog/g", label="Best Premium", broken_slug="dead-one",
                        candidates=CANDS, body=BODY,
                        used_slugs=["dead-one", "lux-9", "cheapo-3", "acme-pro"])
    assert sw is None  # not-on-page has no evidence, unpriced has no price
    sw = ap.choose_swap(page="/blog/g", label="Best Premium", broken_slug="dead-one",
                        candidates=CANDS, body=BODY, used_slugs=["lux-9", "cheapo-3", "acme-pro"],
                        require_evidence=False)
    assert sw["replacement"] == "not-on-page"


def test_choose_swap_requires_same_kind_of_product():
    cands = CANDS + [{"slug": "descaler", "title": "Gaggia Descaler Solution 2 Bottles",
                      "price_cents": 1400}]
    body = BODY + "Keep it clean with the [descaler](/k/descaler), a budget essential.\n"
    sw = ap.choose_swap(page="/blog/g", label="Budget Pick", broken_slug="dead-one",
                        candidates=cands, body=body, used_slugs=[],
                        broken_title="Dead One Slow Cooker 4qt")
    assert sw["replacement"] == "cheapo-3"
    sw = ap.choose_swap(page="/blog/g", label="Budget Pick", broken_slug="dead-one",
                        candidates=cands, body=body, used_slugs=[],
                        broken_title="Dead One Slow Cooker 4qt", min_title_overlap=0)
    assert sw["replacement"] == "descaler"


def test_choose_swap_prefers_person_approved_alternate():
    approved = {"/blog/g": {"Best Overall": [
        {"product_slug": "lux-9", "approved_by": "op", "evidence": "approved line"}]}}
    sw = ap.choose_swap(page="/blog/g/", label="Best Overall", broken_slug="dead-one",
                        candidates=CANDS, body=BODY, used_slugs=[], approved=approved)
    assert sw["replacement"] == "lux-9" and sw["evidence"] == "approved line"
    assert sw["approved_by"] == "op"


def _cfg(**over):
    cfg = {"picks_endpoint": "https://s.test/api/blog/{slug}", "path_prefix": "/blog/",
           "strike_gap_hours": 8}
    cfg.update(over)
    return cfg


def _doc(slug):
    return {"body_md": BODY, "answer_picks": [
        {"label": "Budget Pick", "name": "Dead One", "href": "/k/dead-one?source=amazon"},
        {"label": "Best Overall", "name": "Acme", "href": "/k/acme-pro?source=amazon"}]}


def _probe(url):
    if "dead-one" in url:
        return {"status": 302, "location": "https://www.amazon.com/s?k=dead"}
    return {"status": 302, "location": "https://www.amazon.com/dp/B000000001"}


def _old_state():
    return {"/k/dead-one?source=amazon": {"first_seen": (NOW - timedelta(hours=30)).isoformat(),
                                          "runs_seen": 3}}


def _unlink_lookup(slug):
    return {"slug": slug, "title": "Dead One 4qt Slow Cooker", "image": ""}, []


def test_run_check_swaps_before_unlinking():
    calls = []

    def swap_lookup(slug, page):
        calls.append((slug, page))
        return CANDS

    res = ap.run_check(_cfg(swap={"enabled": True}), origin="https://s.test",
                       pages=["/blog/g"], state=_old_state(), now=NOW,
                       fetch_json=_doc, probe_fn=_probe, lookup=_unlink_lookup,
                       same_picture=lambda a, b: ("same", 0), swap_lookup=swap_lookup,
                       occasions=[])
    (rem,) = res["confirmed"]
    assert rem["action"] == "swap" and rem["replacement"] == "cheapo-3"
    assert rem["prior_action"] == "unlink"
    assert calls == [("dead-one", "g")]
    recs = ap.to_recs(res, "T", dispatch_kind="article-refresh")
    assert len(recs) == 1 and recs[0]["dispatch_kind"] == "article-refresh"
    assert "SWAP" in recs[0]["fix"] and "page evidence" in recs[0]["rationale"]


def test_run_check_swap_off_by_default():
    res = ap.run_check(_cfg(), origin="https://s.test", pages=["/blog/g"],
                       state=_old_state(), now=NOW, fetch_json=_doc, probe_fn=_probe,
                       lookup=_unlink_lookup, same_picture=lambda a, b: ("same", 0),
                       swap_lookup=lambda s, p: CANDS, occasions=[])
    assert res["confirmed"][0]["action"] == "unlink"
    recs = ap.to_recs(res, "T")
    assert "dispatch_kind" not in recs[0] and "SWAP" not in recs[0]["fix"]


def test_relink_wins_over_swap():
    def lookup(slug):
        return ({"slug": slug, "title": "Dead One 4qt", "image": "i"},
                [{"slug": "dead-one-b", "title": "Dead One 4qt", "image": "i"}])

    res = ap.run_check(_cfg(swap={"enabled": True}), origin="https://s.test",
                       pages=["/blog/g"], state=_old_state(), now=NOW, fetch_json=_doc,
                       probe_fn=_probe, lookup=lookup, same_picture=lambda a, b: ("same", 0),
                       swap_lookup=lambda s, p: CANDS, occasions=[])
    assert res["confirmed"][0]["action"] == "relink"


def test_swap_on_held_page_goes_to_person_review():
    res = ap.run_check(_cfg(swap={"enabled": True}), origin="https://s.test",
                       pages=["/blog/g"], state=_old_state(), now=NOW, fetch_json=_doc,
                       probe_fn=_probe, lookup=_unlink_lookup,
                       same_picture=lambda a, b: ("same", 0),
                       swap_lookup=lambda s, p: CANDS, occasions=[])
    recs = ap.to_recs(res, "T", held_pages=["/blog/g"], dispatch_kind="article-refresh")
    assert [r["id"] for r in recs] == ["shelf-T-answer-pick-review"]
    assert "dispatch_kind" not in recs[0]


def test_review_escalates_once_and_reports_age():
    def lookup(slug):  # near-same but different numbers -> review
        return ({"slug": slug, "title": "Dead One 4qt", "image": ""},
                [{"slug": "x", "title": "Dead One 6qt", "image": ""}])

    res = ap.run_check(_cfg(), origin="https://s.test", pages=["/blog/g"],
                       state=_old_state(), now=NOW, fetch_json=_doc, probe_fn=_probe,
                       lookup=lookup, same_picture=lambda a, b: ("same", 0), occasions=[])
    assert res["confirmed"][0]["action"] == "review"
    assert ap.review_max_age_h(res, NOW) == 30.0
    esc = ap.escalations(res, {}, NOW)
    assert [e["href"] for e in esc] == ["/k/dead-one?source=amazon"]
    assert ap.escalations(res, {}, NOW) == []  # marked in state: once only
    assert res["state"]["/k/dead-one?source=amazon"]["escalated_at"]


def test_runs_seen_counts_strikes():
    st, _ = ap.apply_strikes({}, {"h": {"pages": []}}, set(), NOW, 8)
    assert st["h"]["runs_seen"] == 1
    st, conf = ap.apply_strikes(st, {"h": {"pages": []}}, set(), NOW + timedelta(hours=9), 8)
    assert st["h"]["runs_seen"] == 2 and conf == ["h"]


def test_fast_window_from_calendar_retail_deals():
    from framework.core import seasonal_calendar as sc
    occ = sc.Occasion(id="deal-x", label="Deal X", month_day="11-27", audience="food",
                      length_days=4, kind=sc.RETAIL_DEAL_KIND)
    other = sc.Occasion(id="holiday-y", label="Y", month_day="11-20", audience="food")
    cfg = {"fast_windows_from_calendar": {"enabled": True, "audience": "food",
                                          "lead_days": 7, "trail_days": 1,
                                          "one_strike_actions": ["relink", "swap"]}}
    w = ap.active_fast_window(cfg, datetime(2026, 11, 20, tzinfo=timezone.utc), [occ, other])
    assert w["occasion"] == "deal-x" and w["one_strike_actions"] == ["relink", "swap"]
    assert w["start"] == "2026-11-20" and w["end"] == "2026-12-01"
    assert ap.active_fast_window(cfg, datetime(2026, 11, 19, tzinfo=timezone.utc), [occ]) is None
    assert ap.active_fast_window({}, datetime(2026, 11, 28, tzinfo=timezone.utc), [occ]) is None


# ---------- agent wiring ----------

def _agent(tmp_path, monkeypatch):
    import importlib.util as iu
    spec = iu.spec_from_file_location("shelf_audit_agent_mod", _DIR / "agent.py")
    mod = iu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    a = mod.ShelfAuditAgent.__new__(mod.ShelfAuditAgent)
    a.agent_id, a.run_ts = "site-shelf-audit-agent", "20261111T120000Z"
    a.decisions = []
    a.decide = lambda kind, msg, **k: a.decisions.append((kind, msg))
    monkeypatch.setattr(mod.Path, "expanduser", lambda self: tmp_path, raising=False)
    monkeypatch.setattr(mod.os.path, "expanduser", lambda p: str(tmp_path))
    return mod, a


def test_article_refresh_picks_dispatch_from_their_own_run_dir(tmp_path, monkeypatch):
    from framework.core import dispatch
    mod, a = _agent(tmp_path, monkeypatch)
    seen = {}

    def fake(**kw):
        seen.update(kw)
        return object()

    monkeypatch.setattr(dispatch, "gated_dispatch_now", fake)
    recs = [{"id": "shelf-T-answer-pick-dead", "tier": "lever",
             "dispatch_kind": "article-refresh"},
            {"id": "shelf-T-answer-pick-review", "tier": "smaller"}]
    assert a._maybe_dispatch_picks(recs, {"answer_pick_check": {"dispatch": True},
                                          "site_id": "s"}) == "dispatched"
    rd = Path(seen["run_dir"])
    assert rd.name == "20261111T120000Z-answer-picks"
    assert dispatch.dispatch_kind_for(str(rd)) == "article-refresh"
    assert seen["rec_ids"] == ["shelf-T-answer-pick-dead"]


def test_priority_findings_published_for_repricing(tmp_path, monkeypatch):
    mod, a = _agent(tmp_path, monkeypatch)
    written = {}

    class S:
        def write_json(self, k, v):
            written[k] = v

    a.storage = S()
    findings = [{"asin": "B0A", "issues": ["price_drift:+10%:site=1:amazon=2"]},
                {"asin": "B0B", "issues": ["image_mismatch:hamming=40"]},
                {"asin": "B0C", "issues": ["out_of_stock_on_amazon"]}]
    cfg = {"verify_priority": {"publish_key": "agents/x/state/reprice.json"}}
    assert a._publish_priority_findings(cfg, {"B0A", "B0B"}, findings) == {"B0A"}
    assert written["agents/x/state/reprice.json"]["asins"] == ["B0A"]
    assert a._publish_priority_findings({"verify_priority": {}}, {"B0A"}, findings) == set()
