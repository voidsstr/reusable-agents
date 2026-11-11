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
    rule = next(i for i, l in enumerate(lines) if l.startswith("RULE: "))
    # The RULE names exactly the qualifying clusters (the metric's set) ...
    assert lines[rule].startswith("RULE: WHAT SELLS clusters: Monitors · Builds. Within the "
                                  "topics this prompt allows, a proposal that extends a WHAT "
                                  "SELLS cluster or a product category listed above ranks above "
                                  "an AI-citation-only lane")
    assert ds.top_conversion_clusters(SIG) == ["Monitors", "Builds"]
    assert "link the converting page or product" in lines[rule]
    # ... and defers to the consumer's own scope (an operator's BLOCKED list).
    assert lines[rule].endswith("A BLOCKED or do-not-propose list elsewhere in this prompt "
                                "still wins over this rule.")
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


def test_what_sells_clusters_are_the_metric_set_and_below_average_is_split_out():
    lines = ds.build_conversion_block(SIG).splitlines()
    head = next(i for i, l in enumerate(lines) if l.startswith("WHAT SELLS clusters ("))
    # 121 clicks / 2135 articles = 5.67
    assert "at or above the site average of 5.7" in lines[head]
    assert lines[head + 1] == "  - Monitors: 22.8/100 (21 clicks, 7d 21, 92 articles)"
    assert lines[head + 2] == "  - Builds: 18.7/100 (56 clicks, 7d 16, 299 articles)"
    listed = [l.split(":")[0].strip("- ").strip() for l in lines[head + 1:]
              if l.startswith("  - ")][:2]
    assert listed == ds.top_conversion_clusters(SIG)
    # A judged cluster below the site average (LLM 4.6) is never in the WHAT
    # SELLS list, even with more clicks than a listed one; it is on its own
    # line that says it is not a WHAT SELLS lane.
    assert not any(l.startswith("  - LLM") for l in lines)
    assert lines[head + 3] == ("  Below the site average (not WHAT SELLS lanes): LLM 4.6/100 "
                               "(807 articles) · other 0.6/100 (531 articles) · Retro 0.5/100 "
                               "(403 articles)")
    # 2 clicks on 3 articles: shown as unproven, never as a lane
    assert lines[head + 4] == ("  Too few articles and clicks to judge yet: Tiny 66.7/100 "
                               "(2 clicks, 3 articles)")
    one = {"conversion_winners": {**CW, "pages": [{**CW["pages"][0], "clicks": 1}]}}
    assert "[build-price-ladder]: 1 click (7d 1 vs 0)" in ds.build_conversion_block(one)


def test_cluster_cap_moves_the_rest_off_the_what_sells_list_and_out_of_the_rule():
    lines = ds.build_conversion_block(SIG, max_clusters=1).splitlines()
    head = next(i for i, l in enumerate(lines) if l.startswith("WHAT SELLS clusters ("))
    assert lines[head + 1].startswith("  - Monitors:")
    # Builds is above the average but past the cap: "not WHAT SELLS lanes"
    # without claiming it is below the average.
    assert lines[head + 2].startswith("  Not WHAT SELLS lanes: Builds 18.7/100 (299 articles)")
    assert next(l for l in lines if l.startswith("RULE: ")).startswith(
        "RULE: WHAT SELLS clusters: Monitors. Within")


def test_rule_separates_cluster_names_that_contain_commas():
    cw = {**CW, "clusters": [
        {"cluster": "Knives, prep & storage", "articles": 25, "clicks": 75, "d7": 10,
         "clicks_per_100": 300.0},
        {"cluster": "Coffee", "articles": 8, "clicks": 68, "d7": 7, "clicks_per_100": 850.0},
        {"cluster": "Recipes", "articles": 210, "clicks": 0, "d7": 0, "clicks_per_100": 0.0}]}
    rule = next(l for l in ds.build_conversion_block({"conversion_winners": cw}).splitlines()
                if l.startswith("RULE: "))
    assert rule.startswith("RULE: WHAT SELLS clusters: Coffee · Knives, prep & storage. Within")


def test_no_qualifying_cluster_names_none_in_the_rule():
    weak = {"conversion_winners": {**CW, "clusters": [
        {"cluster": "LLM", "articles": 807, "clicks": 37, "d7": 22, "clicks_per_100": 4.6},
        {"cluster": "other", "articles": 531, "clicks": 60, "d7": 0, "clicks_per_100": 11.3},
        {"cluster": "Tiny", "articles": 3, "clicks": 2, "d7": 0, "clicks_per_100": 66.7}]}}
    lines = ds.build_conversion_block(weak).splitlines()
    assert ds.top_conversion_clusters(weak) == []
    assert any("none sells at or above the average on enough articles or clicks to judge, so "
               "no cluster is a WHAT SELLS lane yet." in l for l in lines)
    assert not any(l.startswith("WHAT SELLS clusters (") for l in lines)
    # "other" sits above the average, so the line does not say "below"
    assert "  Not WHAT SELLS lanes: other 11.3/100 (531 articles) · LLM 4.6/100 (807 articles)" \
        in lines
    rule = next(l for l in lines if l.startswith("RULE: "))
    assert rule.startswith("RULE: Within the topics this prompt allows")
    unclicked = {"conversion_winners": {**CW, "clusters": [
        {"cluster": "LLM", "articles": 807, "clicks": 0, "d7": 0, "clicks_per_100": 0}]}}
    assert "Article topic clusters: no published article earned a verified click in 30d, so no " \
           "cluster is a WHAT SELLS lane yet." in ds.build_conversion_block(unclicked)


def test_template_yield_says_when_some_clicks_are_not_on_published_articles():
    cw = {**CW, "templates": [
        {"template": "other-article", "clicks": 50, "d7": 26, "prev7": 11, "pages": 18,
         "articles": 1568, "article_clicks": 36, "clicks_per_100": 2.3},
        {"template": "best-x-article", "clicks": 83, "d7": 50, "prev7": 26, "pages": 46,
         "articles": 791, "article_clicks": 83, "clicks_per_100": 10.5}]}
    block = ds.build_conversion_block({"conversion_winners": cw})
    assert ("  - other-article: 50 clicks (7d 26 vs 11) from 18 pages; 2.3 per 100 of 1,568 "
            "published articles (36 of the clicks are on them)") in block
    assert block.count("of the clicks are on them") == 1     # equal counts say nothing extra


def test_products_list_skips_inactive_and_unresolved_rows():
    rows = [
        {"key": "61595", "title": "Slow Cooker", "category": "small-appliances", "active": False,
         "clicks": 16, "d7": 0, "prev7": 3},
        {"key": "99999", "title": "", "category": None, "clicks": 9, "d7": 1, "prev7": 1},
        {"key": "962", "title": "Coffee Maker", "category": "small-appliances",
         "link": "/k/coffee-maker", "clicks": 19, "d7": 4, "prev7": 8},
        {"key": "B0X", "title": "", "category": None, "link": "/product/B0X", "clicks": 2,
         "d7": 0, "prev7": 0}]
    block = ds.build_conversion_block({"conversion_winners": {**CW, "products": rows}})
    prods = [l for l in block.splitlines()
             if l.startswith("  - ") and ("/k/" in l or "/product/B0X" in l
                                          or "Slow Cooker" in l or "99999" in l)]
    assert prods == ["  - Coffee Maker [small-appliances] /k/coffee-maker: 19 clicks (7d 4 vs 8)",
                     "  - B0X /product/B0X: 2 clicks (7d 0 vs 0)"]
    # the facet helper still reports the inactive product's clicks
    assert ds.conversion_clicks({"conversion_winners": {"products": rows}}, "products")["61595"] == 16
    only_inactive = {"conversion_winners": {**CW, "products": rows[:2]}}
    assert "Top converting products" not in ds.build_conversion_block(only_inactive)


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
