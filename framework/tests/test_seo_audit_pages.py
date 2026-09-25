"""Tests for seo-opportunity-agent/lib/analyzer/audit_pages.py — which pages
the SEO LLM audit looks at (AI-landed seeds first, per-URL cooldown, latency
rec on AI-landed pages). Pure functions: no network, no DB."""
from __future__ import annotations

import importlib.util as iu
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
_SPEC = iu.spec_from_file_location(
    "audit_pages",
    _ROOT / "agents" / "seo-opportunity-agent" / "lib" / "analyzer" / "audit_pages.py",
)
ap = iu.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ap)

BASE = "https://example.com"
STATS = {
    "ai_landed_pages": [
        {"path": "/reviews/a", "referrals": 10, "fetches": 26, "score": 76},
        {"path": "/product/B0AAAAAAAA/", "referrals": 9, "fetches": 13},
        {"path": "https://www.example.com/product/B0AAAAAAAA?x=1"},   # dup
        {"path": "https://other.site/x"},                              # off-site
        {"url": "/buying-guide/gpus", "fetches": 77},
        "not-a-row",
    ],
    "clicks_pages": [{"path": "/product/B0CCCCCCCC", "clicks": 3}],
    "scalar_block": {"path": "/vs/x-vs-y"},
}


def test_seed_rows_order_dedupe_and_host_filter():
    rows = ap.seed_rows(STATS, ["ai_landed_pages", "missing", "scalar_block"], BASE)
    assert [r["url"] for r in rows] == [
        "https://example.com/reviews/a",
        "https://example.com/product/B0AAAAAAAA",
        "https://example.com/buying-guide/gpus",
        "https://example.com/vs/x-vs-y",
    ]
    assert rows[0]["referrals"] == 10 and rows[0]["fetches"] == 26
    assert rows[0]["seed_query"] == "ai_landed_pages"
    assert ap.seed_rows(STATS, ["ai_landed_pages"], BASE, limit=2)[-1]["url"] \
        .endswith("/product/B0AAAAAAAA")
    assert ap.seed_rows(STATS, [], BASE) == []


def test_prioritize_puts_seeds_first_and_annotates():
    seeds = ap.seed_rows(STATS, ["ai_landed_pages"], BASE)
    pages = [
        {"url": "https://example.com/about"},
        {"url": "https://example.com/privacy"},
        {"url": "https://example.com/buying-guide/gpus/"},
        # crawler followed a redirect from a seed URL
        {"url": "https://example.com/reviews/a-new", "redirected_from": "https://example.com/reviews/a"},
    ]
    out = ap.prioritize(pages, seeds)
    assert [p["url"] for p in out] == [
        "https://example.com/reviews/a-new",
        "https://example.com/buying-guide/gpus/",
        "https://example.com/about",
        "https://example.com/privacy",
    ]
    assert out[0]["ai_referrals"] == 10 and out[0]["ai_live_fetches"] == 26
    assert out[1]["ai_live_fetches"] == 77 and "ai_referrals" not in out[1]
    assert "audit_seed" not in out[2]


def test_url_rec_counts_window_and_llm_only():
    now = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)
    llm = {"llm_check_id": "meta-title-length", "data_refs": ["https://example.com/p/1"]}
    docs = [
        ("20260925T090000Z", {"recommendations": [llm, dict(llm),
                                                   {"type": "top5-target-page", "data_refs": ["https://example.com/p/1"]}]}),
        ("20260901T000000Z", {"recommendations": [llm]}),
        ("20260801T000000Z", {"recommendations": [llm]}),      # outside 30d
        ("garbage", {"recommendations": [llm]}),
    ]
    assert ap.url_rec_counts(docs, now=now, window_days=30) == {"https://example.com/p/1": 3}


def test_cooldown_filters_pages_and_recs_with_exemptions():
    cfg = ap.cooldown_config({"audit_url_cooldown": {"max_recs_per_url": 2,
                                                     "max_recs_per_exempt_url": 4}})
    exempt = ap.exempt_urls(STATS, ["clicks_pages"], BASE)
    counts = {"https://example.com/p/cold": 2, "https://example.com/p/warm": 1,
              "https://example.com/product/B0CCCCCCCC": 3}
    pages = [{"url": "https://example.com/p/cold"}, {"url": "https://example.com/p/warm"},
             {"url": "https://example.com/product/B0CCCCCCCC"}, {"url": "https://example.com/new"}]
    kept, dropped = ap.cooldown_filter_pages(pages, counts, cfg, exempt)
    assert dropped == ["https://example.com/p/cold"]
    assert [p["url"] for p in kept][0] == "https://example.com/p/warm"

    def rec(u):
        return {"llm_check_id": "x", "data_refs": [u]}
    recs = [rec("https://example.com/p/warm"), rec("https://example.com/p/warm"),
            rec("https://example.com/product/B0CCCCCCCC"),
            rec("https://example.com/product/B0CCCCCCCC"), {"type": "no-url"}]
    kept_r, n = ap.cooldown_filter_recs(recs, counts, cfg, exempt)
    # warm had 1 of 2 → one more; the exempt PDP had 3 of 4 → one more
    assert n == 2 and len(kept_r) == 3
    # cap 0 = unlimited
    cfg0 = ap.cooldown_config({"audit_url_cooldown": {"max_recs_per_url": 0}})
    assert ap.cooldown_filter_pages(pages, counts, cfg0, set())[1] == []


def test_slow_ai_landed_rec_only_for_ai_landed_pages_over_budget():
    pages = [
        {"url": "https://example.com/product/B0AAAAAAAA", "audit_seed": "ai_landed_pages",
         "ai_referrals": 9, "ttfb_ms": 14800},
        {"url": "https://example.com/reviews/a", "audit_seed": "ai_landed_pages",
         "ai_live_fetches": 3, "ttfb_ms": 900},
        {"url": "https://example.com/about", "ttfb_ms": 20000},           # not AI-landed
        {"url": "https://example.com/vs/x", "audit_seed": "ai_landed_pages",
         "ai_live_fetches": 2, "ttfb_ms": 0, "fetch_ms": 5200},          # falls back to fetch_ms
    ]
    rec = ap.slow_ai_landed_rec(pages, budget_ms=3000, rec_id="rec-007")
    assert rec["type"] == "cwv-ttfb-slow" and rec["id"] == "rec-007"
    assert rec["sample_urls"] == ["https://example.com/product/B0AAAAAAAA",
                                  "https://example.com/vs/x"]
    assert "2 AI-landed page(s)" in rec["title"]
    assert ap.slow_ai_landed_rec(pages, budget_ms=0, rec_id="r") is None
    assert ap.slow_ai_landed_rec(pages[1:3], budget_ms=3000, rec_id="r") is None


def test_run_ts_datetime():
    assert ap.run_ts_datetime("20260925T043000Z") == datetime(2026, 9, 25, 4, 30, tzinfo=timezone.utc)
    assert ap.run_ts_datetime("rundir-x") is None
