"""Tests for framework.core.article_link_guard — kitchen link shapes.

Regression cover for the 2026-09-25 audit (F074/F105/F124): the guard only
counted `/k/` links and told writers to link every kitchen slug as
`[Category Name](/k/<slug>)`. `/k/` is the robots-blocked affiliate
click-out, so article → kitchen links were invisible to crawlers. Sites now
list their crawlable roots (`kitchen_roots`) and link shapes (templates);
the defaults keep the old behaviour for sites that set nothing.
"""
from __future__ import annotations

from framework.core import article_link_guard as g

AP = {
    "min_recipes": 0, "min_kits": 2, "min_products": 0,
    "site_root": "https://ap.test",
    "kitchen_roots": ["/kitchen/category/", "/kitchen/", "/k/"],
    "kitchen_category_template": "{site_root}/kitchen/category/{slug}",
    "kitchen_product_template": "{site_root}/kitchen/{slug}",
    "kitchen_buy_template": "{site_root}/k/{slug}?source=amazon",
    "kitchen_link_template": "{site_root}/k/{slug}",
}


def _verify(body, proposal, cfg=AP):
    return g.verify_body(body, proposal, min_recipes=cfg["min_recipes"], min_kits=cfg["min_kits"],
                         min_products=cfg["min_products"], kitchen_roots=cfg.get("kitchen_roots"))


def test_crawlable_kitchen_links_count_when_configured():
    body = ("Use a [Dutch oven](https://ap.test/kitchen/lodge-6qt-dutch-oven) and browse "
            "[cookware](/kitchen/category/cookware) or [chef's knives](/kitchen/category/knives/chefs-knives).")
    prop = {"expected_kitchen_category_slugs": ["cookware", "chefs-knives"],
            "expected_kitchen_product_slugs": ["lodge-6qt-dutch-oven"]}
    a = _verify(body, prop)
    assert a.kitchen_links == 3
    assert a.matched_expected_kitchen == 3
    assert a.passes


def test_default_roots_still_count_only_k_links():
    body = "[a](/kitchen/category/cookware) [b](/kitchen/some-pan) [c](/k/knives)"
    a = g.verify_body(body, {}, min_recipes=0, min_kits=2)
    assert a.kitchen_links == 1
    assert not a.passes
    assert "kitchen links" in a.failure_reason()


def test_split_proposal_renders_crawlable_shapes_and_buy_link():
    prop = {"expected_kitchen_category_slugs": ["cookware"],
            "expected_kitchen_product_slugs": ["lodge-6qt-dutch-oven"]}
    txt = g.render_link_directive(prop, min_recipes=0, min_kits=2, site_root="https://ap.test", link_cfg=AP)
    assert "[Category Name](https://ap.test/kitchen/category/cookware)" in txt
    assert "[Product Name](https://ap.test/kitchen/lodge-6qt-dutch-oven)" in txt
    assert "https://ap.test/k/<slug>?source=amazon" in txt
    assert "/k/cookware" not in txt


def test_unsplit_legacy_list_keeps_k_shape_so_product_slugs_never_404():
    # Mixed categories + products in one list: a category template would
    # 404 every product slug, so the legacy shape (/k/ resolves both) stays.
    prop = {"expected_kitchen_slugs": ["cookware", "lodge-6qt-dutch-oven"]}
    txt = g.render_link_directive(prop, min_recipes=0, min_kits=2, site_root="https://ap.test", link_cfg=AP)
    assert "https://ap.test/k/cookware" in txt
    assert "https://ap.test/k/lodge-6qt-dutch-oven" in txt
    assert "/kitchen/category/lodge" not in txt


def test_no_link_cfg_is_backward_compatible():
    prop = {"expected_kitchen_slugs": ["cookware"]}
    txt = g.render_link_directive(prop, min_recipes=0, min_kits=1, site_root="https://x.test")
    assert "https://x.test/k/cookware" in txt


def test_split_kitchen_slugs_drops_unknown():
    out = g.split_kitchen_slugs(
        ["cookware", "lodge-6qt-dutch-oven", "kitchen-appliances", "cookware"],
        category_slugs={"cookware", "knives"},
        product_slugs={"lodge-6qt-dutch-oven"},
    )
    assert out == {"categories": ["cookware"], "products": ["lodge-6qt-dutch-oven"],
                   "unknown": ["kitchen-appliances"]}


def test_repo_config_resolves_site_knobs():
    ap = g.resolve_minima("aisleprompt-article-proposal-agent")
    assert "/kitchen/category/" in ap["kitchen_roots"] and "/k/" in ap["kitchen_roots"]
    assert ap["kitchen_product_template"].endswith("/kitchen/{slug}")
    sp = g.resolve_minima("specpicks-article-proposal-agent")
    assert sp["min_products"] == 3 and sp["min_kits"] == 0
    assert "kitchen_roots" not in sp


# ── Kitchen slug repair (2026-09-25 audit F101/F118) ──────────────────────

class _FakeCursor:
    def __init__(self, tables):
        self.tables = tables
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params):
        table = sql.split(" FROM ")[1].split()[0]
        wanted = set(params[0])
        self._rows = [(s,) for s in self.tables.get(table, []) if s in wanted]

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, tables):
        self.tables = tables

    def cursor(self):
        return _FakeCursor(self.tables)


CHECK = {"product_table": "kitchen_products", "product_active_column": "is_active",
         "category_table": "kitchen_categories", "static_slugs": ["woks"],
         "reserved_slugs": ["buying-guides"]}


def test_repair_unlinks_invented_slugs_and_canonicalizes_doubled_brands():
    conn = _FakeConn({"kitchen_products": ["lodge-skillet", "acme-pan"], "kitchen_categories": ["cookware"]})
    body = ("Get the [Lodge](/k/lodge-skillet?source=amazon), the [Acme pan](/kitchen/acme-acme-acme-pan), "
            "a [thermometer](/k/thermoworks-thermapen-one), [cookware](/kitchen/category/cookware), "
            "[woks](/k/woks), [bad hub](/kitchen/category/not-a-cat/x), [guides](/kitchen/buying-guides) "
            "and a [recipe](/recipes/stew-12).")
    resolve = g.kitchen_slug_resolver_from_db(conn, body, CHECK)
    out, notes = g.repair_kitchen_links(body, resolve)
    assert "[Lodge](/k/lodge-skillet?source=amazon)" in out
    assert "[Acme pan](/kitchen/acme-pan)" in out
    assert "a thermometer," in out and "thermapen" not in out
    assert "[cookware](/kitchen/category/cookware)" in out
    assert "[woks](/k/woks)" in out
    assert "bad hub" in out and "not-a-cat" not in out
    assert "[guides](/kitchen/buying-guides)" in out
    assert "[recipe](/recipes/stew-12)" in out
    assert len(notes) == 3
    # The guard then counts only links that resolve.
    a = g.verify_body(out, {}, min_recipes=0, min_kits=1, kitchen_roots=AP["kitchen_roots"])
    assert a.kitchen_links == 5


def test_resolver_rejects_non_identifier_table_names():
    import pytest
    with pytest.raises(ValueError):
        g.kitchen_slug_resolver_from_db(_FakeConn({}), "[a](/k/x)", {"product_table": "kitchen_products; drop table x"})


def test_de_stutter_handles_tripled_brand():
    assert g.de_stutter_candidates("piklohas-piklohas-piklohas-bread-slicer") == [
        "piklohas-piklohas-bread-slicer", "piklohas-bread-slicer"]
    assert g.de_stutter_candidates("lodge-skillet") == []
