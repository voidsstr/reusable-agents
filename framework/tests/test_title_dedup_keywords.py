"""title_dedup.filter_keyword_collisions — the published-catalog keyword
check every article proposer runs before queueing (2026-10-01 AI-reach
audit, issue near-duplicate-cannibalizing-articles)."""
from framework.core import title_dedup as T

PUBLISHED = [
    {"slug": "best-gpu-for-gpt-oss-20b-2026", "title": "Best GPU for gpt-oss 20B in 2026",
     "primary_keyword": "best gpu for gpt-oss 20b", "secondary_keywords": [], "tags": []},
    {"slug": "prime-big-deal-days-2026-gpu-deals-local-llm-12gb-vs-16gb",
     "title": "Prime Big Deal Days 2026 GPU Deals for Local LLMs",
     "primary_keyword": "prime big deal days gpu deals", "tags": ["holiday:prime-big-deal-days"],
     "related_product_asins": ["B0F7WB6LSH", "B0FLHBFFX4"]},
    {"slug": "prime-big-deal-days-2026-prebuilt-gaming-pc-rtx-5060-vs-5060-ti-vs-5070",
     "title": "Prime Big Deal Days 2026 Prebuilt Gaming PC Deals",
     "primary_keyword": "prime big deal days prebuilt gaming pc deals",
     "tags": ["holiday:prime-big-deal-days"], "related_product_asins": ["B0GSZC8QGB", "B0H1V3S5DB"]},
    {"slug": "intel-arc-pro-b60-vs-rtx-3060-12gb-local-llm-2026",
     "title": "Intel Arc Pro B60 vs RTX 3060 12GB for Local LLM",
     "primary_keyword": "intel arc pro b60 vs rtx 3060", "tags": []},
]


def _run(props, **kw):
    dropped, stripped = [], []
    kept = T.filter_keyword_collisions(
        props, PUBLISHED, on_drop=lambda p, r: dropped.append((p.get("slug"), r)),
        on_strip=lambda p, k, o: stripped.append((p.get("slug"), k, o)), **kw)
    return kept, dropped, stripped


def test_normalize_keyword_folds_years_plurals_and_punctuation():
    assert T.normalize_keyword("Best GPUs for LLMs (2026)") == "best gpu for llm"
    assert T.normalize_keyword("ASUS glass") == "asus glass"
    assert T.normalize_keyword("best gpu for gpt-oss 20b") == T.normalize_keyword("Best GPU for GPT OSS 20B")


def test_primary_equal_to_published_primary_is_rejected():
    kept, dropped, _ = _run([{"slug": "gpt-oss-20b-rtx-3060-vs-5600g", "title": "gpt-oss 20B: RTX 3060 vs Ryzen 5600G",
                              "primary_keyword": "Best GPU for GPT-OSS 20B"}])
    assert kept == [] and "best-gpu-for-gpt-oss-20b-2026" in dropped[0][1]


def test_secondary_owned_by_a_sibling_is_stripped_by_default():
    p = {"slug": "pbdd-hub", "title": "Prime Big Deal Days 2026: Local AI + Gaming Setup Picks",
         "primary_keyword": "prime big deal days 2026", "tags": ["holiday:prime-big-deal-days"],
         "secondary_keywords": ["october prime day 2026", "prime big deal days gpu deals"],
         "related_product_asins": ["B0FB9BVH47"]}
    kept, dropped, stripped = _run([p])
    assert dropped == []
    assert kept[0]["secondary_keywords"] == ["october prime day 2026"]
    assert p["secondary_keywords"][1] == "prime big deal days gpu deals"  # input not mutated
    assert stripped[0][2].startswith("prime-big-deal-days-2026-gpu-deals")


def test_secondary_reject_mode():
    p = {"slug": "x", "title": "Something new", "primary_keyword": "something new",
         "secondary_keywords": ["prime big deal days gpu deals"]}
    kept, dropped, _ = _run([p], secondary_action="reject")
    assert kept == [] and "secondary" in dropped[0][1]


def test_same_holiday_duplicate_top_pick_is_rejected_but_distinct_spokes_pass():
    setup = {"slug": "pbdd-setup", "title": "Prime Big Deal Days 2026 Local AI Setup Picks",
             "primary_keyword": "prime big deal days 2026", "tags": ["holiday:prime-big-deal-days"],
             "related_product_asins": ["B0GSZC8QGB", "B08W8DGK3X"]}
    monitor = {"slug": "pbdd-monitors", "title": "Prime Big Deal Days 2026 Monitor Deals",
               "primary_keyword": "prime big deal days monitor deals",
               "tags": ["holiday:prime-big-deal-days"], "related_product_asins": ["B0GV3NPMHW"]}
    kept, dropped, _ = _run([setup, monitor])
    assert [k["slug"] for k in kept] == ["pbdd-monitors"]
    assert "top pick b0gszc8qgb" in dropped[0][1]


def test_holiday_proposals_are_exempt_from_title_similarity():
    p = {"slug": "pbdd-gpu-2", "title": "Prime Big Deal Days 2026 GPU Deals for Local LLMs",
         "primary_keyword": "october prime day gpu picks", "tags": ["holiday:prime-big-deal-days"],
         "related_product_asins": ["B0DV9GC9SR"]}
    kept, dropped, _ = _run([p])
    assert kept and not dropped


def test_near_identical_title_twin_is_rejected():
    p = {"slug": "intel-arc-pro-b60-vs-rtx-3060-12gb-local-llm", "primary_keyword": "arc pro b60 local llms",
         "title": "Intel Arc Pro B60 vs RTX 3060 12GB for Local LLMs"}
    kept, dropped, _ = _run([p])
    assert kept == [] and "near-identical" in dropped[0][1]
    # A real sub-intent (bread dough + bowl-lift) is not a twin of the parent guide.
    pub = [{"slug": "best-stand-mixers-2026", "title": "Best Stand Mixers 2026", "primary_keyword": "best stand mixer"}]
    sub = {"slug": "best-stand-mixers-bread-dough-2026", "title": "Best Stand Mixers for Bread Dough 2026: Bowl-Lift Picks",
           "primary_keyword": "best stand mixer for bread dough"}
    assert T.filter_keyword_collisions([sub], pub) == [sub]


def test_intra_batch_duplicate_primary_keeps_the_first():
    a = {"slug": "a", "title": "Alpha guide", "primary_keyword": "budget nas 2026"}
    b = {"slug": "b", "title": "Beta roundup", "primary_keyword": "Budget NAS"}
    kept, dropped, _ = _run([a, b])
    assert [k["slug"] for k in kept] == ["a"] and dropped[0][0] == "b"
