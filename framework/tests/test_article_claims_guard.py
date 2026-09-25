"""article_claims_guard — the honesty backstop for article bodies.

2026-09-25 AI-visibility audit F119/F075/F100/F108/F120: kitchen guides that
AI assistants quote carried invented first-hand tests, copied Amazon star
counts and stale point prices. The writer prompts were fixed; this guard
refuses the INSERT when a body still carries them.
"""
from framework.core import article_claims_guard as G

POLICY = {"first_hand_claims": "reject", "star_ratings": "reject", "point_prices": "reject"}


def test_flags_invented_protocols_and_long_term_use():
    body = (
        "## How we tested\n\n"
        "We ran five external sealers through the same four-part protocol.\n\n"
        "After testing the 2026 lineup against 4,000 cracked eggs, the HA1 won.\n\n"
        "The set survived 47 dishwasher cycles in our test rotation.\n\n"
        "Our picks are based on six months of in-kitchen testing of all six scales side by side.\n"
    )
    hits = G.find_first_hand_claims(body)
    assert len(hits) >= 4, hits
    assert not G.check(body, policy=POLICY).passes


def test_third_party_attribution_and_disclaimers_pass():
    body = (
        "America's Test Kitchen found the D3 resisted warping in its abuse tests.\n\n"
        "Owners report the handles stay cool after months of use.\n\n"
        "The steel grades quoted here are not our own measurements; we did not test these knives.\n\n"
        "Every product links to the listing in our kitchen catalog.\n"
    )
    assert G.find_first_hand_claims(body) == []


def test_copied_star_ratings_flagged():
    body = "The Lodge pan holds 4.6 stars across 4,318 ratings.\n\n| Pan | Rating |\n|---|---|\n| Lodge | 4.6 (4,318) |\n"
    assert len(G.find_star_ratings(body)) == 2
    assert G.find_star_ratings("A 4-quart pot for a family of four.") == []


def test_point_prices_in_structured_places_only():
    body = (
        "| Pick | Price (Sept 2026) | Best for |\n|---|---|---|\n"
        "| Lodge | $34.90 | Searing |\n| Budget | under $30 | Starter |\n\n"
        "**Street price:** $449.99\n\n"
        "### #1 Best Overall: Breville Barista Express — ~$750\n\n"
        "Recipes cost about $2.50 per serving, and bands like $50–$100 are fine.\n"
    )
    hits = G.find_point_prices(body)
    assert any("$34.90" in h for h in hits)
    assert any("Street price" in h for h in hits)
    assert any("~$750" in h for h in hits)
    assert not any("under $30" in h for h in hits)
    assert not any("per serving" in h for h in hits)


def test_price_buckets_scope_point_prices():
    body = "| Item | Price |\n|---|---|\n| Chicken | $6.99 |\n"
    pol = G.resolve_policy("aisleprompt-article-proposal-agent", bucket="recipe-cluster")
    assert pol["point_prices"] == "off"
    assert G.check(body, policy=pol).passes
    pol = G.resolve_policy("aisleprompt-article-proposal-agent", bucket="kitchen-buying-guide")
    assert pol["point_prices"] == "reject"
    assert not G.check(body, policy=pol).passes


def test_failure_reason_names_the_fix():
    audit = G.check("We measured the lid at 4.2 lb and it lost 6 oz of water.", policy=POLICY)
    assert not audit.passes
    reason = audit.failure_reason()
    assert "first-hand" in reason and "How we picked" in reason


def test_subtitle_claims_count():
    audit = G.check("Clean body.", subtitle="We tested 14 bakeware sets and ranked the top 5", policy=POLICY)
    assert not audit.passes


def test_warn_mode_never_rejects():
    pol = {"first_hand_claims": "warn", "star_ratings": "warn", "point_prices": "off"}
    audit = G.check("We ran every blender for 30 seconds. 4.8 stars across 9,000 reviews.", policy=pol)
    assert audit.passes
    assert len(audit.warnings()) == 2
