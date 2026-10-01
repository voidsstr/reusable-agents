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


# ── answer_names_pick (2026-10-01 audit, answer-first-omits-pick-names) ──
_ROUNDUP_PICKS = (
    "## Best Overall: [Ninja AF141 5-Quart Air Fryer](/kitchen/ninja-af141)\n\nWhy it wins.\n\n"
    "## Best Value: [Instant Pot Duo 7-in-1, 6-Quart](/kitchen/instant-pot-duo-6qt)\n\n"
    "[See full details →](/kitchen/instant-pot-duo-6qt)\n")


def test_answer_naming_only_product_families_is_flagged():
    body = ("The kitchen items most worth buying this sale fall into four families: air fryers "
            "(under $100), multicookers like the Instant Pot, stand mixers and knife sets.\n\n"
            + _ROUNDUP_PICKS)
    hits = G.find_answer_without_pick(body)
    assert hits and "four families" in hits[0]


def test_answer_naming_a_pick_passes_by_model_token_or_lead_words():
    by_model = ("For most kitchens the air fryer to buy is the Ninja AF141, a 5-quart basket "
                "under $100 that crisps evenly.\n\n" + _ROUNDUP_PICKS)
    by_name = ("Buy the Instant Pot Duo if you want one pot for beans, rice and stock, "
               "in the under-$100 tier.\n\n" + _ROUNDUP_PICKS)
    assert G.find_answer_without_pick(by_model) == []
    assert G.find_answer_without_pick(by_name) == []


def test_quick_answer_section_is_the_answer_and_units_are_not_models():
    body = ("*As an Amazon Associate, we earn from qualifying purchases.*\n\n"
            "## Quick Answer\n\nBuy a 16GB card at the 5-quart tier of the market.\n\n"
            "## Picks\n\n[MSI RTX 5060 Ti 16G](/product/B0A) and [ZOTAC RTX 3060 12GB](/product/B0B)\n")
    assert G.opening_answer(body).startswith("Buy a 16GB card")
    assert G.find_answer_without_pick(body)  # "16gb" / "5-quart" are units, not models
    named = body.replace("Buy a 16GB card", "Buy the RTX 5060 Ti 16GB")
    assert G.find_answer_without_pick(named) == []


def test_answer_check_skips_non_roundups_and_cta_only_links():
    one = "A rice cooker is a pot with a thermostat.\n\n## Pick\n\n[Zojirushi NS-ZCC10](/kitchen/z)\n"
    ctas = ("Buy the right size.\n\n## Picks\n\n[See full details →](/product/B1)\n"
            "[Check current price on Amazon →](/product/B2)\n")
    assert G.find_answer_without_pick(one) == []
    assert G.find_answer_without_pick(ctas) == []


def test_answer_check_runs_only_for_answer_buckets():
    cfg = {"default": {"price_buckets": ["kitchen-buying-guide"]}, "sites": {}}
    assert G.resolve_policy("x", "kitchen-buying-guide", config=cfg)["answer_names_pick"] == "warn"
    assert G.resolve_policy("x", "recipe-cluster", config=cfg)["answer_names_pick"] == "off"
    cfg2 = {"default": {"price_buckets": [], "answer_buckets": ["seasonal-occasion"]}, "sites": {}}
    assert G.resolve_policy("x", "seasonal-occasion", config=cfg2)["answer_names_pick"] == "warn"
    body = "Deals fall into four families: fryers and mixers.\n\n" + _ROUNDUP_PICKS
    audit = G.check(body, policy=G.resolve_policy("x", "kitchen-buying-guide", config=cfg))
    assert audit.passes and any("opening answers" in w for w in audit.warnings())
