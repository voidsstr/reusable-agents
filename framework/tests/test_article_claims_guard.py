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
        "### Best value — MEGAWISE Vacuum Sealer with starter kit, $33.99\n\n"
        "Recipes cost about $2.50 per serving, and bands like $50–$100 are fine.\n"
    )
    hits = G.find_point_prices(body)
    assert any("$34.90" in h for h in hits)
    assert any("Street price" in h for h in hits)
    assert any("~$750" in h for h in hits)
    assert any("$33.99" in h for h in hits)
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


def test_storage_copy_of_the_policy_overrides_the_repo_default():
    """The storage copy is the no-commit knob the operator uses to flip a
    site between warn and reject; run.sh and the sweep CLI must read it."""
    class _Store:
        def read_json(self, key):
            assert key == "config/article-claims-guard-config.json"
            return {"default": {"first_hand_claims": "reject", "star_ratings": "reject",
                                "point_prices": "off"},
                    "sites": {"specpicks": {"first_hand_claims": "reject"}}}

    body = "We ran every GPU through the same benchmark suite for a week."
    # Repo default: specpicks is warn-only.
    assert G.check(body, site_hint="specpicks-article-proposal-agent", bucket="buying-guide").passes
    # Storage override: reject.
    assert not G.check(body, site_hint="specpicks-article-proposal-agent", bucket="buying-guide",
                       storage=_Store()).passes
    cfg = G.load_config(_Store())
    assert G.resolve_policy("specpicks-x", "buying-guide", config=cfg)["first_hand_claims"] == "reject"

    class _Empty:
        def read_json(self, key):
            return None
    assert G.load_config(_Empty())["sites"]["specpicks"]["first_hand_claims"] == "warn"


# ── 2026-10-01 AI-reach audit additions ────────────────────────────────────

def test_dated_and_list_anchor_prices_are_point_prices():
    body = ("Crock-Pot 7-quart (about $52 as of September 2026) is the pick.\n\n"
            "The MSI RTX 3060 Ventus 2X (list $629) is fine.\n\n"
            "It sits below its $629 list price.\n\n"
            "Expect ~$500 at last check.\n\n"
            "Anything under $50 is a band, and MSRP $429 is launch context.\n")
    hits = G.find_point_prices(body)
    assert len(hits) == 4, hits
    assert not any("under $50" in h for h in hits)


def test_stock_claims_flagged_but_check_stock_advice_passes():
    hits = G.find_stock_claims(
        "Every pick below is an in-stock listing. Check stock on the live listing before you buy. "
        "These can sell through in November.")
    assert hits == ["Every pick below is an in-stock listing.", "These can sell through in November."]
    # Cooking stock, dated selection filters and hypotheticals are not claims.
    assert G.find_stock_claims(
        "Braise the lamb shoulder in stock for two hours. "
        "We kept only listings with an in-stock Amazon offer as of late September 2026. "
        "If the 9800X3D slips out of stock, buy the 7800X3D.") == []


def test_known_stale_return_window_claim_rejects_and_names_the_fix():
    pol = G.resolve_policy("specpicks-article-proposal-agent", "buying-guide",
                           config={"default": {}, "sites": {}})
    assert pol["stale_claims"] == "reject" and pol["stock_claims"] == "warn"
    bad = G.check("October purchases get January returns, so buy early.", policy=pol)
    assert not bad.passes and "Nov 1" in bad.failure_reason()
    good = G.check("Holiday returns cover purchases from Nov 1 through Dec 31, returnable until "
                   "January 31; October Prime Big Deal Days orders get the standard 30-day window.",
                   policy=pol)
    assert good.passes and good.stale_claims == []


def test_dollar_delta_must_match_a_gap_between_stated_prices():
    setup = ("The Shadow 5 anchors at $1,899.99 and the Azure 3 at $1,199.99, so the Azure is $700 "
             "cheaper than the Shadow 5. On Sept 8 the Shadow 5 listed at $1,499.99. Spend the extra "
             "$200 on the Shadow 5 now instead of $400+ on a GPU swap later.")
    hits = G.find_numeric_inconsistencies(setup)
    assert len(hits) == 1 and "$200" in hits[0]
    assert G.find_numeric_inconsistencies(
        "NVIDIA launched it at $429, only $50 above the 8GB version's $379.") == []
    # A body that states price tiers only cannot anchor a delta: skipped.
    assert G.find_numeric_inconsistencies(
        "Tri-ply sits in the $100–$200 band and 5-ply runs $300 and up; if you don't sear, "
        "save $100 and buy 3-ply.") == []
    # Range edges and approximate deltas are not checked.
    assert G.find_numeric_inconsistencies(
        "The Breville is $2,195.00 and the Stilosa $99.95. Budget another $150 to $300 for a "
        "grinder. The KF saves ~$20 versus the K.") == []
    # Per-serving savings are not product deltas.
    assert G.find_numeric_inconsistencies("It costs $2.50 per serving, saving $1 over takeout at $9 or $12.") == []


def test_universal_ratio_claim_is_flagged():
    hits = G.find_numeric_inconsistencies(
        "That's about 3× the 4060 Ti at every context length, for roughly 5× the price.")
    assert hits and "universal ratio" in hits[0]


MINI = """## Quick Answer
The KAMRUI AM21 is the pick.

### 🏆 Best Overall: [KAMRUI AM21](/product/B0FX3P76CJ?tag=specpicks-articles-20)
Text.

### 💰 Best Value: [Kron K1](/product/B0CY4ZM18N)
Text.

### 🧪 Budget Pick: [GMKtec G3 PRO](/product/B0GHDTHM53)
It is the cheapest mini PC on this list.

## Who should wait
The [Ryzen 7 9800X3D](/product/B0DKFMSMYK) listing was above its $479 MSRP when last seen.
"""
PRICES = {"/product/B0FX3P76CJ": 559.0, "/product/B0CY4ZM18N": 419.98,
          "/product/B0GHDTHM53": 499.99, "/product/b0dkfmsmyk": 444.0}


def test_price_conflicts_against_live_prices():
    hits = G.find_price_conflicts(MINI, PRICES)
    assert any(h.startswith("budget pick /product/b0ghdthm53") for h in hits), hits
    assert any("'cheapest'" in h for h in hits), hits
    assert any("above the $479 MSRP" in h for h in hits), hits
    # Consistent prices: nothing to report.
    ok = dict(PRICES, **{"/product/B0GHDTHM53": 249.0, "/product/b0dkfmsmyk": 519.0})
    assert G.find_price_conflicts(MINI, ok) == []
    assert G.find_price_conflicts(MINI, None) == []


def test_new_checks_warn_by_default_and_explicit_policy_keeps_them_off():
    pol = G.resolve_policy("specpicks-article-proposal-agent", "buying-guide")
    audit = G.check(MINI + "\nEvery pick is in stock.\n", policy=pol, prices=PRICES)
    assert audit.passes  # warn-only
    w = " ".join(audit.warnings())
    assert "stock" in w and "live prices contradict" in w
    # A caller-built policy without the new keys behaves as before.
    old = G.check(MINI + "\nEvery pick is in stock.\n",
                  policy={"first_hand_claims": "warn"}, prices=PRICES)
    assert old.stock_claims == [] and old.price_conflicts == []


def test_sweep_rows_reports_only_rows_with_findings():
    rows = [{"slug": "mini", "category": "buying-guide", "body_md": MINI},
            {"slug": "clean", "category": "buying-guide", "body_md": "Nothing to see."},
            {"slug": "stale", "category": "recipe-cluster",
             "body_md": "Prime Big Deal Days orders can be returned until January 31."}]
    out = G.sweep_rows(rows, site_hint="specpicks-x", prices=PRICES,
                       config={"default": {}, "sites": {}})
    by = {r["slug"]: r for r in out}
    assert set(by) == {"mini", "stale"}
    assert by["mini"]["passes"] and by["mini"]["price"]
    assert not by["stale"]["passes"] and by["stale"]["stale"]
