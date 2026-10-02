"""framework.core.gsc_coverage: the URL Inspection sampler and rollups."""
import json
from datetime import datetime, timedelta, timezone

from framework.core import gsc_coverage as gc

NOW = datetime(2026, 10, 2, 13, 0, tzinfo=timezone.utc)


def _stamp(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _site(n_blog=50, n_kitchen=500, n_recipes=5000):
    h = "https://example.com"
    return ([f"{h}/blog/post-{i}" for i in range(n_blog)]
            + [f"{h}/kitchen/item-{i}" for i in range(n_kitchen)]
            + [f"{h}/recipes/dish-{i}" for i in range(n_recipes)]
            + [f"{h}/"])


def test_never_inspected_urls_are_not_taken_alphabetically():
    # The bug: every never-inspected URL tied at the epoch watermark, so a
    # 150-URL run took /blog/*, then /kitchen/*, and never reached /recipes/*.
    targets = gc.pick_targets(_site(), {}, 150, now=NOW)
    sections = {gc.section_of(u) for u in targets}
    assert {"blog", "kitchen", "recipes", "root"} <= sections
    recipes = [u for u in targets if "/recipes/" in u]
    assert recipes != sorted(recipes)[: len(recipes)]  # a hashed sample, not the A-prefix


def test_sections_share_each_run_equally():
    targets = gc.pick_targets(_site(), {}, 31, now=NOW)
    counts = {}
    for u in targets:
        counts[gc.section_of(u)] = counts.get(gc.section_of(u), 0) + 1
    # root has one URL; the other 30 slots split evenly across three sections
    assert counts == {"root": 1, "blog": 10, "kitchen": 10, "recipes": 10}


def test_order_is_stable_between_runs():
    assert gc.pick_targets(_site(), {}, 40, now=NOW) == gc.pick_targets(list(reversed(_site())), {}, 40, now=NOW)


def test_recently_inspected_urls_sit_out_and_stale_ones_come_back():
    urls = _site(n_blog=3, n_kitchen=0, n_recipes=0)[:3]
    last = {urls[0]: _stamp(1), urls[1]: _stamp(30)}
    targets = gc.pick_targets(urls, last, 10, now=NOW, min_age_days=14)
    assert urls[0] not in targets                    # inspected yesterday
    assert targets == [urls[2], urls[1]]             # never-inspected first, then the stale one


def test_limit_and_empty():
    assert gc.pick_targets(_site(), {}, 0, now=NOW) == []
    assert len(gc.pick_targets(_site(n_blog=2, n_kitchen=2, n_recipes=2), {}, 1000, now=NOW)) == 7


def test_labelled_sitemap_urls_form_their_own_section():
    urls = [f"https://example.com/recipes/dish-{i}" for i in range(100)]
    curated = {u: "recipes-priority" for u in urls[:5]}
    targets = gc.pick_targets(urls, {}, 10, now=NOW, section_urls=curated)
    assert sum(1 for u in targets if u in curated) == 5   # 5 curated + 5 bulk, not 10 bulk
    assert gc.section_of(urls[0], curated) == "recipes-priority"
    assert gc.section_of(urls[50], curated) == "recipes"


def test_section_of_root_and_paths():
    assert gc.section_of("https://example.com/") == "root"
    assert gc.section_of("https://example.com") == "root"
    assert gc.section_of("https://example.com/features") == "features"
    assert gc.section_of("https://example.com/recipes/cuisine/thai") == "recipes"


def test_is_indexed_covers_both_states_and_the_verdict():
    assert gc.is_indexed("Submitted and indexed")
    assert gc.is_indexed("Indexed, not submitted in sitemap")
    assert gc.is_indexed("", "PASS")
    assert not gc.is_indexed("Crawled - currently not indexed", "NEUTRAL")
    assert not gc.is_indexed(gc.UNKNOWN_STATE, "NEUTRAL")
    assert not gc.is_indexed(None)


def test_latest_by_url_and_section_summary():
    rows = [
        {"url": "https://e.com/blog/a", "inspected_at": "2026-08-01T00:00:00Z", "coverageState": gc.UNKNOWN_STATE},
        {"url": "https://e.com/blog/a", "inspected_at": "2026-09-30T00:00:00Z", "coverageState": "Submitted and indexed", "verdict": "PASS"},
        {"url": "https://e.com/blog/b", "inspected_at": "2026-09-30T00:00:00Z", "coverageState": "Crawled - currently not indexed"},
        {"url": "https://e.com/recipes/x-1", "inspected_at": "2026-09-30T00:00:00Z",
         "coverageState": "Indexed, not submitted in sitemap", "section": "recipes-priority"},
        {"url": "https://e.com/recipes/y-2", "inspected_at": "2026-09-30T00:00:00Z", "coverageState": gc.UNKNOWN_STATE},
    ]
    lines = [json.dumps(r) for r in rows] + ["not json", ""]
    latest = gc.latest_by_url(lines)
    assert latest["https://e.com/blog/a"]["state"] == "Submitted and indexed"   # newest row wins
    summary = gc.section_summary(latest)
    assert summary["blog"] == {"inspected": 2, "indexed": 1, "unknown": 0, "crawled_not_indexed": 1}
    assert summary["recipes-priority"]["indexed"] == 1                          # recorded label wins
    assert summary["recipes"] == {"inspected": 1, "indexed": 0, "unknown": 1, "crawled_not_indexed": 0}


def test_metric_key():
    assert gc.metric_key("recipes-priority") == "recipes_priority"
    assert gc.metric_key("root") == "root"
    assert gc.metric_key("") == "root"


# ---- inspect.py render_url: template fields are COLUMN NAMES (2026-10-02) ----

def _inspect_module(tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path
    monkeypatch.setenv("GSC_INSPECT_STATE_DIR", str(tmp_path))
    path = Path(__file__).resolve().parents[2] / "agents" / "gsc-coverage-auditor" / "inspect.py"
    spec = importlib.util.spec_from_file_location("gsc_inspect_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_render_url_reads_template_fields_by_name(tmp_path, monkeypatch):
    m = _inspect_module(tmp_path, monkeypatch)
    # The recipe querySet selects `id, title`; positional reading rendered
    # /recipes/26383-Central Plains Rice Bowl with Miso Glaze for 141k recipes.
    row = {"id": 26383, "title": "Central Plains Rice Bowl with Miso Glaze", "lastmod": None}
    assert (m.render_url("example.com", "/recipes/", "slugify:title|-|id", row)
            == "https://example.com/recipes/central-plains-rice-bowl-with-miso-glaze-26383")
    assert m.render_url("example.com", "/p/", "slug", {"slug": "B0ABC"}) == "https://example.com/p/B0ABC"
    assert (m.render_url("example.com", "/compare/", "compose:left_ref|-vs-|right_ref",
                         {"left_ref": "a", "right_ref": "b"}) == "https://example.com/compare/a-vs-b")


def test_render_url_skips_rows_missing_a_field(tmp_path, monkeypatch):
    m = _inspect_module(tmp_path, monkeypatch)
    assert m.render_url("example.com", "/r/", "slugify:title|-|id", {"title": "x"}) is None
    assert m.render_url("example.com", "/r/", "slugify:title|-|id", {"title": None, "id": 1}) is None
    assert m.render_url("example.com", "/r/", "slug", {"slug": ""}) is None


def test_slugify_matches_the_indexnow_submitter(tmp_path, monkeypatch):
    m = _inspect_module(tmp_path, monkeypatch)
    assert m.slugify("Sautéed Rösti & Gruyère") == "sauteed-rosti-gruyere"
    assert m.slugify("  --Hello,  World!--  ") == "hello-world"
    assert m.slugify(42) == "42"
    assert len(m.slugify("a" * 200)) == 80
