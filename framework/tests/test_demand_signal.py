"""Tests for framework.core.demand_signal prompt rendering (no storage)."""
from framework.core import demand_signal as ds

AI = {
    "referral_days": 90, "fetch_days": 30,
    "clusters": [
        {"cluster": "LLM", "articles": 800, "referrals": 150, "fetches": 20, "referrals_per_100": 18.8},
        {"cluster": "Memory", "articles": 9, "referrals": 1, "fetches": 0, "referrals_per_100": 11.1},
        {"cluster": "Monitors", "articles": 110, "referrals": 6, "fetches": 9, "referrals_per_100": 5.5},
        {"cluster": "empty", "articles": 0, "referrals": 0, "fetches": 0, "referrals_per_100": 0},
    ],
    "top_products": [{"key": "B0AAAAAAAA", "title": "Some Desk", "category": "gaming-desks",
                      "referrals": 9, "fetches": 0}],
}


def test_block_without_ai_demand_is_unchanged():
    sig = {"generated_at": "2026-09-25T08:00:00+00:00",
           "steer_topics": [{"topic": "GPUs", "template": "article", "impressions": 10}]}
    block = ds.build_prompt_block(sig)
    assert "AI ASSISTANT DEMAND" not in block
    assert "STEER" in block


def test_ai_demand_ranks_by_yield_and_demotes_small_samples():
    block = ds.build_prompt_block({"generated_at": "2026-09-25T08:00:00+00:00",
                                   "ai_assistant_demand": AI})
    assert "AI ASSISTANT DEMAND" in block
    lines = block.splitlines()
    llm = next(i for i, l in enumerate(lines) if "- LLM:" in l)
    mon = next(i for i, l in enumerate(lines) if "- Monitors:" in l)
    mem = next(i for i, l in enumerate(lines) if "- Memory:" in l)
    assert llm < mon < mem, "small-sample cluster must rank after judged ones"
    assert "too few to judge" in lines[mem]
    assert "empty" not in block
    assert "not a reason to widen the topic scope" in block
    assert "Some Desk [gaming-desks]: 9 referrals" in block


def test_ai_demand_alone_renders():
    assert ds._ai_demand_lines({}) == []
    assert ds._ai_demand_lines({"top_products": AI["top_products"]})[0].startswith(
        "AI ASSISTANT DEMAND")


# ── WHAT SELLS (conversion_winners) ─────────────────────────────────────────

CW = {
    "label": "Amazon", "window_days": 30, "trend_days": 7,
    "definition": "verified-human clicks on clicks where target=amazon",
    "total": {"d30": 1351, "d7": 769, "prev7": 435},
    "templates": [
        {"template": "product-page", "clicks": 1096, "d7": 650, "prev7": 380, "pages": 880},
        {"template": "best-x-article", "clicks": 83, "d7": 50, "prev7": 26, "pages": 46,
         "articles": 791, "clicks_per_100": 10.5},
        {"template": "x-vs-y-article", "clicks": 0, "d7": 0, "prev7": 0, "pages": 0,
         "articles": 654, "clicks_per_100": 0.0},
    ],
    "placements": [{"placement": "pdp-hero", "clicks": 833, "d7": 400, "prev7": 300},
                   {"placement": "sticky-mobile", "clicks": 117, "d7": 60, "prev7": 40}],
    "clusters": [
        {"cluster": "Monitors", "articles": 92, "clicks": 21, "d7": 21, "clicks_per_100": 22.8},
        {"cluster": "Builds", "articles": 299, "clicks": 56, "d7": 16, "clicks_per_100": 18.7},
        {"cluster": "Tiny", "articles": 3, "clicks": 2, "d7": 0, "clicks_per_100": 66.7},
        {"cluster": "LLM", "articles": 807, "clicks": 37, "d7": 22, "clicks_per_100": 4.6},
        {"cluster": "other", "articles": 531, "clicks": 3, "d7": 0, "clicks_per_100": 0.6},
        {"cluster": "Retro", "articles": 403, "clicks": 2, "d7": 0, "clicks_per_100": 0.5},
    ],
    "categories": [{"category": "home-lab", "clicks": 57, "d7": 36, "prev7": 17,
                    "products": 34, "value": 81423.44}],
    "products": [{"key": "B0FBF7FCZW", "title": "KOORUI 27in 4K Monitor", "category": "monitors",
                  "price": 499.99, "link": "/product/B0FBF7FCZW", "clicks": 11, "d7": 9,
                  "prev7": 1}],
    "pages": [{"path": "/reviews/gaming-pc-build-2000-2026", "template": "build-price-ladder",
               "clicks": 8, "d7": 1, "prev7": 0}],
}
SIG = {"generated_at": "2026-11-11T19:00:00+00:00",
       "template_winners": [{"template": "product", "views": 117011, "pages": 40000,
                             "views_per_page": 2.9, "clicks": 3}],
       "steer_topics": [{"topic": "GPUs", "template": "article", "impressions": 10}],
       "ai_assistant_demand": AI, "conversion_winners": CW}


def test_what_sells_leads_the_block_with_the_rule():
    lines = ds.build_prompt_block(SIG).splitlines()
    assert lines[0].startswith("WHAT SELLS (verified-human Amazon clicks, 30d; "
                               "last 7d vs prior 7d): 1,351 clicks, 7d 769 vs 435")
    rule = next(i for i, l in enumerate(lines) if l.startswith("RULE: a proposal that extends "
                                                                "a WHAT SELLS cluster"))
    assert "AI-citation-only lane" in lines[rule] and "link the converting page or product" in lines[rule]
    proven = next(i for i, l in enumerate(lines) if l.startswith("PROVEN SEARCH DEMAND"))
    ai = next(i for i, l in enumerate(lines) if l.startswith("AI ASSISTANT DEMAND"))
    assert rule < proven < ai


def test_template_winners_relabelled_as_ga4_views_including_crawlers():
    block = ds.build_prompt_block(SIG)
    assert "Page-template GA4 views (includes crawlers" in block
    assert "  - product: 117,011 GA4 views / 40000 pages" in block
    assert "the format that wins" not in block


def test_no_or_zero_conversion_data_renders_no_what_sells():
    base = {k: v for k, v in SIG.items() if k != "conversion_winners"}
    assert "WHAT SELLS" not in ds.build_prompt_block(base)
    zero = {**base, "conversion_winners": {**CW, "total": {"d30": 0, "d7": 0, "prev7": 0}}}
    assert "WHAT SELLS" not in ds.build_prompt_block(zero)
    assert ds.build_prompt_block(base).startswith("PROVEN SEARCH DEMAND")


def test_what_sells_sections_render_with_trends_and_yields():
    block = ds.build_conversion_block(SIG)
    assert "  - best-x-article: 83 clicks (7d 50 vs 26) from 46 pages; 10.5 per 100 of 791 " \
           "published articles" in block
    # zero-click article formats stay visible below the cut
    assert "Other article templates: x-vs-y-article 0/100 of 654 articles" in block
    assert "home-lab: 57 clicks (7d 36 vs 17), 34 products, $81,423 clicked list value" in block
    assert "/reviews/gaming-pc-build-2000-2026 [build-price-ladder]: 8 clicks (7d 1 vs 0)" in block
    assert "Click placements (the on-page slot that got the click): pdp-hero 833 · " \
           "sticky-mobile 117" in block
    assert "PROVEN SEARCH DEMAND" not in block and "AI ASSISTANT DEMAND" not in block


def test_product_line_cites_link_and_never_a_price():
    block = ds.build_conversion_block(SIG)
    line = next(l for l in block.splitlines() if "KOORUI" in l)
    assert line == "  - KOORUI 27in 4K Monitor [monitors] /product/B0FBF7FCZW: 11 clicks (7d 9 vs 1)"
    assert "$" not in line
    no_link = {"conversion_winners": {**CW, "products": [{**CW["products"][0], "link": None}]}}
    assert "[monitors] B0FBF7FCZW: 11 clicks" in ds.build_conversion_block(no_link)


def test_conversion_clusters_rank_by_yield_and_flag_small_samples():
    lines = ds.build_conversion_block(SIG, max_clusters=3).splitlines()
    head = next(l for l in lines if l.startswith("Article topic clusters"))
    assert "(site average 5.7)" in head   # 121 clicks / 2135 articles = 5.67
    mon = next(i for i, l in enumerate(lines) if "- Monitors: 22.8/100" in l)
    bld = next(i for i, l in enumerate(lines) if "- Builds: 18.7/100" in l)
    llm = next(i for i, l in enumerate(lines) if "- LLM: 4.6/100" in l)
    assert mon < bld < llm
    assert lines[mon] == "  - Monitors: 22.8/100 (21 clicks, 7d 21, 92 articles)"
    one = {"conversion_winners": {**CW, "pages": [{**CW["pages"][0], "clicks": 1}]}}
    assert "[build-price-ladder]: 1 click (7d 1 vs 0)" in ds.build_conversion_block(one)
    below = next(l for l in lines if l.strip().startswith("Below those:"))
    assert "other 0.6/100 (531 articles)" in below and "Retro 0.5/100 (403 articles)" in below
    # 2 clicks on 3 articles: listed after every judged cluster, flagged
    full = ds.build_conversion_block(SIG, max_clusters=10).splitlines()
    tiny = next(i for i, l in enumerate(full) if "- Tiny:" in l)
    assert "too few articles and clicks to judge" in full[tiny]
    assert tiny > next(i for i, l in enumerate(full) if "- Retro:" in l)


def test_conversion_clicks_by_facet():
    assert ds.conversion_clicks(SIG, "products") == {"B0FBF7FCZW": 11}
    assert ds.conversion_clicks(SIG, "pages") == {"/reviews/gaming-pc-build-2000-2026": 8}
    assert ds.conversion_clicks(SIG, "templates")["x-vs-y-article"] == 0
    assert ds.conversion_clicks(None, "categories") == {}
    assert ds.conversion_clicks({"generated_at": "x"}, "clusters") == {}
    try:
        ds.conversion_clicks(SIG, "queries")
    except ValueError as e:
        assert "unknown conversion facet" in str(e)
    else:
        raise AssertionError("unknown facet must raise")


def test_top_conversion_clusters_need_clicks_sample_and_above_average_yield():
    # site average 5.7/100: LLM (4.6) is below it, Tiny is too small to judge,
    # "other" is the catch-all bucket.
    assert ds.top_conversion_clusters(SIG) == ["Monitors", "Builds"]
    assert ds.top_conversion_clusters(SIG, above_average=False) == [
        "Monitors", "Builds", "LLM", "Retro"]
    assert ds.top_conversion_clusters(SIG, 1) == ["Monitors"]
    assert ds.top_conversion_clusters(SIG, min_articles=1, min_clicks=1) == [
        "Tiny", "Monitors", "Builds"]
    assert ds.top_conversion_clusters({}) == []
