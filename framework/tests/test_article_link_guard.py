"""Tests for framework.core.article_link_guard — kitchen link shapes.

Regression cover for the 2026-09-25 audit (F074/F105/F124): the guard only
counted `/k/` links and told writers to link every kitchen slug as
`[Category Name](/k/<slug>)`. `/k/` is the robots-blocked affiliate
click-out, so article → kitchen links were invisible to crawlers. Sites now
list their crawlable roots (`kitchen_roots`) and link shapes (templates);
the defaults keep the old behaviour for sites that set nothing.
"""
from __future__ import annotations

import re

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


def test_repair_only_judges_the_sites_own_links():
    # Review 2026-09-25: the link pattern accepted any host, so a citation to
    # another site whose path starts with /kitchen/ or /k/ would have been
    # unlinked as an "invented slug".
    conn = _FakeConn({"kitchen_products": ["lodge-skillet"], "kitchen_categories": []})
    body = ("[ours](https://aisleprompt.com/k/made-up-thing) "
            "[ours www](https://www.aisleprompt.com/kitchen/lodge-lodge-skillet) "
            "[theirs](https://www.example-kitchen.com/kitchen/some-review) "
            "[theirs k](https://other.example/k/abc-def)")
    resolve = g.kitchen_slug_resolver_from_db(conn, body, CHECK)
    hosts = g.site_hosts_from_root("https://aisleprompt.com")
    assert hosts == {"aisleprompt.com", "www.aisleprompt.com"}
    out, notes = g.repair_kitchen_links(body, resolve, own_hosts=hosts)
    assert out.startswith("ours ")                                   # own invented slug: unlinked
    assert "[ours www](https://www.aisleprompt.com/kitchen/lodge-skillet)" in out
    assert "[theirs](https://www.example-kitchen.com/kitchen/some-review)" in out
    assert "[theirs k](https://other.example/k/abc-def)" in out
    assert len(notes) == 2
    # Without the site's hosts, absolute links are never touched.
    out2, notes2 = g.repair_kitchen_links(body, resolve)
    assert out2 == body and notes2 == []


# ── 2026-10-01 audit: soft-404 internal routes + citation quality ─────────

def test_subcategory_link_is_nested_under_its_parent():
    from framework.core.article_link_guard import kitchen_slug_resolver_from_db, repair_kitchen_links

    class _Cur:
        def __init__(self): self.q = ""
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, q, params=None): self.q = q
        def fetchall(self):
            return [("small-appliances",)] if "kitchen_categories" in self.q else []

    class _Conn:
        def cursor(self): return _Cur()

    body = ("See the [pressure cookers shop](/kitchen/category/pressure-cookers) and "
            "[small appliances](/kitchen/category/small-appliances).")
    cfg = {"product_table": "kitchen_products", "category_table": "kitchen_categories",
           "static_slugs": ["pressure-cookers"],
           "subcategory_parents": {"pressure-cookers": "small-appliances"}}
    out, notes = repair_kitchen_links(body, kitchen_slug_resolver_from_db(_Conn(), body, cfg),
                                      own_hosts={"aisleprompt.com"})
    assert "(/kitchen/category/small-appliances/pressure-cookers)" in out
    assert "(/kitchen/category/small-appliances)" in out and notes


def test_unknown_own_routes_are_rewritten_or_unlinked():
    from framework.core.article_link_guard import repair_unknown_routes
    routes = [r"/recipes/cuisine/[a-z0-9-]+", r"/recipes/category/[a-z0-9-]+",
              r"/recipes/[a-z0-9-]+", r"/blog/[a-z0-9-]+", r"/kitchen(?:/.*)?", r"/k/[a-z0-9-]+"]
    body = ("Browse the [cuisine hub](/cuisine), the [Thanksgiving hub](/holidays/thanksgiving), "
            "a [real recipe](/recipes/pie-123), a [cite](https://example.com/holidays/x) "
            "and ![img](/holidays/x.png).")
    out, notes = repair_unknown_routes(
        body, routes, own_hosts={"aisleprompt.com"},
        route_rewrites={r"/holidays/([a-z0-9-]+)": r"/recipes/category/\1"})
    assert "[Thanksgiving hub](/recipes/category/thanksgiving)" in out
    assert "Browse the cuisine hub," in out            # unlinked, anchor kept
    assert "[real recipe](/recipes/pie-123)" in out
    assert "https://example.com/holidays/x" in out      # foreign host untouched
    assert "![img](/holidays/x.png)" in out              # images untouched
    assert len(notes) == 2
    assert repair_unknown_routes(body, [], own_hosts=set())[0] == body


def test_resolver_carries_route_policy_into_repair_kitchen_links():
    from framework.core.article_link_guard import kitchen_slug_resolver_from_db, repair_kitchen_links

    class _Conn:
        def cursor(self):
            raise AssertionError("no kitchen links -> no query")

    body = "Read the [cuisine hub](/cuisine)."
    resolve = kitchen_slug_resolver_from_db(_Conn(), body, {"known_routes": [r"/recipes/.*"]})
    out, notes = repair_kitchen_links(body, resolve, own_hosts={"aisleprompt.com"})
    assert out == "Read the cuisine hub." and notes == ["unlinked unknown route /cuisine"]


def test_citation_audit_rejects_roots_hubs_and_storefronts():
    from framework.core.article_link_guard import audit_citations
    a = audit_citations([
        "https://www.instantpot.com/", "https://nchfp.uga.edu/",
        "https://thekitchen-supply.myshopify.com/products/x", "https://sca.coffee/research",
        "https://en.wikipedia.org/wiki/Burr_mill", "https://www.seriouseats.com/best-burr-coffee-grinders",
        "https://www.seriouseats.com/best-burr-coffee-grinders/", "https://aisleprompt.com/blog/x",
    ], own_hosts={"aisleprompt.com"})
    assert a.kept == ["https://en.wikipedia.org/wiki/Burr_mill",
                      "https://www.seriouseats.com/best-burr-coffee-grinders"]
    reasons = dict(a.rejected)
    assert "homepage" in reasons["https://www.instantpot.com/"]
    assert "storefront" in reasons["https://thekitchen-supply.myshopify.com/products/x"]
    assert reasons["https://aisleprompt.com/blog/x"] == "own site"
    assert not a.passes and "min 3" in a.failure_reason()
    ok = audit_citations(["https://www.rtings.com/monitor/reviews/best/4k",
                          "https://www.tomshardware.com/reviews/gpu-hierarchy,4388.html",
                          "https://en.wikipedia.org/wiki/OLED"])
    assert ok.passes and ok.non_wikipedia == 2


def test_filter_proposal_citations_trims_in_place():
    from framework.core.article_link_guard import filter_proposal_citations
    props = [{"id": "art-001", "outbound_citations": [
        "https://www.instantpot.com/", "https://www.seriouseats.com/best-instant-pots",
        "https://nchfp.uga.edu/how/can/pressure"]}, {"id": "art-002"}]
    notes = []
    short = filter_proposal_citations(props, {"min_qualifying": 2}, {"aisleprompt.com"}, on_note=notes.append)
    assert props[0]["outbound_citations"] == ["https://www.seriouseats.com/best-instant-pots",
                                              "https://nchfp.uga.edu/how/can/pressure"]
    assert short == 0 and len(notes) == 1 and "instantpot.com" in notes[0]


# ── Direct buy links (2026-11-12 article review) ──────────────────────────
# Seven aisleprompt guides named every product only through its /kitchen/<slug>
# page and shipped with zero buy buttons: the guard counted /kitchen/ as a
# kitchen link and nothing asked for a buy link.

AP_BUY = dict(AP, buy_link_patterns=[r"^/k/[A-Za-z0-9][A-Za-z0-9-]*"],
              min_buy_links=0,
              min_buy_links_by_bucket={"buying-guide": 2, "kitchen-buying-guide": 2},
              min_buy_links_by_title={r"\bgifts?\b": 2, r"\bchecklist\b": 2})

PRODUCT_PAGES_ONLY = ("Our pick is the [OXO scale](/kitchen/oxo-scale) and the "
                      "[Nordic Ware sheet](https://ap.test/kitchen/nordic-half-sheet), "
                      "see [bakeware](/kitchen/category/bakeware).")


def test_product_page_links_are_not_buy_links():
    a = g.verify_body(PRODUCT_PAGES_ONLY, {}, min_recipes=0, min_kits=2,
                      kitchen_roots=AP["kitchen_roots"], min_buy_links=2,
                      own_hosts={"ap.test"})
    assert a.kitchen_links == 3 and a.buy_links == 0
    assert not a.passes
    assert "direct buy links" in a.failure_reason() and "(min 2)" in a.failure_reason()


def test_buy_links_distinct_own_host_only_and_never_images():
    body = (PRODUCT_PAGES_ONLY
            + " [Check price](https://ap.test/k/oxo-scale?source=amazon)"
            + " [OXO on Amazon](/k/oxo-scale)"                        # same target: counts once
            + " ![photo](/k/nordic-half-sheet)"                       # an image is not a link
            + " [a cite](https://other.example/k/abc-def)"            # another site's /k/ path
            + " [FLUX.2 [klein] card](/k/nordic-half-sheet#deal)")    # brackets in anchor text
    assert g.buy_link_targets(body, AP_BUY["buy_link_patterns"], {"ap.test"}) == {
        "/k/oxo-scale", "/k/nordic-half-sheet"}
    a = g.verify_body(body, {}, min_recipes=0, min_kits=2, kitchen_roots=AP["kitchen_roots"],
                      min_buy_links=2, buy_link_patterns=AP_BUY["buy_link_patterns"],
                      own_hosts={"ap.test"})
    assert a.buy_links == 2 and a.passes
    # Without the site's hosts an absolute own link is not recognised.
    assert g.buy_link_targets("[x](https://ap.test/k/oxo-scale)") == set()
    # A bad pattern is skipped, not fatal.
    assert g.buy_link_targets("[x](/k/oxo)", ["(", r"^/k/"]) == {"/k/oxo"}


def test_verify_body_defaults_keep_the_old_verdict():
    a = g.verify_body(PRODUCT_PAGES_ONLY, {}, min_recipes=0, min_kits=2,
                      kitchen_roots=AP["kitchen_roots"])
    assert a.min_buy_links == 0 and a.passes


def test_min_buy_links_for_bucket_and_title():
    assert g.min_buy_links_for(None) == 0
    assert g.min_buy_links_for(AP_BUY, bucket="Kitchen-Buying-Guide") == 2
    assert g.min_buy_links_for(AP_BUY, bucket="recipe-cluster") == 0
    # gift / checklist pages ride several buckets: the title catches them
    assert g.min_buy_links_for(AP_BUY, bucket="seasonal-occasion",
                               title="Gifts for Bakers 2026: The Sheet Pan, Scale and Mixer") == 2
    assert g.min_buy_links_for(AP_BUY, bucket="seasonal-occasion",
                               title="Thanksgiving Kitchen Gear Checklist") == 2
    assert g.min_buy_links_for(AP_BUY, bucket="seasonal-occasion",
                               title="Christmas Cookie Recipes: 25 Classics") == 0
    # several bucket names (a row's bucket and category can differ): highest wins
    assert g.min_buy_links_for(AP_BUY, bucket=["seasonal-occasion", "buying-guide"]) == 2
    # a listed bucket REPLACES the site default, so 0 exempts it; titles still raise
    cfg = dict(AP_BUY, min_buy_links=1, min_buy_links_by_bucket={"diet-guide": 0})
    assert g.min_buy_links_for(cfg, bucket="meal-prep") == 1
    assert g.min_buy_links_for(cfg, bucket="diet-guide") == 0
    assert g.min_buy_links_for(cfg, bucket="diet-guide", title="Whole30 gifts") == 2
    # a bad title regex in config is skipped
    assert g.min_buy_links_for(dict(AP_BUY, min_buy_links_by_title={"(": 5}), title="x (") == 0


def test_verify_body_with_config_reads_bucket_and_title_from_the_proposal():
    prop = {"bucket": "seasonal-occasion", "category": "buying-guide",
            "title": "Gifts for Home Bartenders 2026"}
    a = g.verify_body_with_config(PRODUCT_PAGES_ONLY, prop, AP_BUY)
    assert (a.min_buy_links, a.buy_links, a.passes) == (2, 0, False)
    ok = PRODUCT_PAGES_ONLY + " [Check price](/k/oxo-scale) [Check price](/k/nordic-half-sheet?source=amazon)"
    assert g.verify_body_with_config(ok, prop, AP_BUY).passes
    # explicit bucket/title override the proposal
    assert g.verify_body_with_config(PRODUCT_PAGES_ONLY, prop, AP_BUY,
                                     bucket="recipe-cluster", title="Diwali Recipes").passes


def test_repo_config_buy_link_minima():
    ap = g.resolve_minima("aisleprompt-article-proposal-agent")
    for bucket in ("buying-guide", "kitchen-buying-guide"):
        assert g.min_buy_links_for(ap, bucket=bucket) >= 2
    assert g.min_buy_links_for(ap, bucket="seasonal-occasion", title="Gifts for Coffee Lovers 2026") >= 2
    assert g.min_buy_links_for(ap, bucket="seasonal-occasion",
                               title="Thanksgiving Kitchen Gear Checklist 2026") >= 2
    assert g.min_buy_links_for(ap, bucket="recipe-cluster", title="Diwali Recipes") == 0
    assert any(re.search(p, "/k/oxo-scale") for p in ap["buy_link_patterns"])
    sp = g.resolve_minima("specpicks-article-proposal-agent")
    assert g.min_buy_links_for(sp, bucket="buying-guide", title="Best Black Friday GPU Gifts") == 0


def test_directive_states_the_buy_link_requirement_up_front():
    prop = {"bucket": "kitchen-buying-guide", "title": "Best Sheet Pans",
            "expected_kitchen_category_slugs": ["bakeware"],
            "expected_kitchen_product_slugs": ["nordic-half-sheet", "oxo-scale"]}
    txt = g.render_link_directive(prop, min_recipes=0, min_kits=2, site_root="https://ap.test",
                                  link_cfg=AP_BUY)
    assert "and 2 distinct direct buy links" in txt
    assert "DIRECT BUY LINKS — at least 2 distinct" in txt
    assert "[Check price](https://ap.test/k/nordic-half-sheet?source=amazon)" in txt
    assert "[Check price](https://ap.test/k/oxo-scale?source=amazon)" in txt
    assert "(https://ap.test/kitchen/<slug>) names the product but is NOT a buy link" in txt
    # explicit min_buy_links=0 (or a bucket with no minimum) leaves the old text
    off = g.render_link_directive(prop, min_recipes=0, min_kits=2, site_root="https://ap.test",
                                  link_cfg=AP_BUY, min_buy_links=0)
    assert "DIRECT BUY LINKS" not in off
    assert "2 distinct kitchen links, and 0 distinct /product/ links — the article" in off


def test_failure_addendum_names_the_buy_link_gap_only_when_required():
    prop = {"bucket": "buying-guide"}
    a = g.verify_body_with_config(PRODUCT_PAGES_ONLY, prop, AP_BUY)
    txt = g.render_failure_addendum(a)
    assert "0/2 required direct buy links" in txt
    plain = g.render_failure_addendum(g.verify_body("", {}, min_recipes=1, min_kits=0))
    assert "buy links" not in plain and "kitchen links, and 0/0 required /product/" in plain


class _FakeStorage:
    def __init__(self, doc):
        self.doc = doc

    def read_json(self, key):
        assert key == "config/article-link-guard-config.json"
        return self.doc


def test_storage_copy_overlays_the_repo_default_per_block():
    # The live storage copy (2026-11-12) predated kitchen_slug_check and the
    # buy-link knobs. Laid over the repo default, it must not drop them.
    stale = {"default": {"min_recipes": 5, "min_kits": 2, "min_products": 0},
             "sites": {"aisleprompt": {"kitchen_roots": ["/kitchen/category/", "/kitchen/", "/k/"]},
                       "specpicks": {"min_products": 3}}}
    ap = g.resolve_minima("aisleprompt-article-proposal-agent", _FakeStorage(stale))
    assert ap.get("kitchen_slug_check", {}).get("product_table")
    assert g.min_buy_links_for(ap, bucket="kitchen-buying-guide") >= 2
    # a key the storage copy names wins
    over = {"sites": {"aisleprompt": {"min_buy_links_by_bucket": {"kitchen-buying-guide": 3}}}}
    ap3 = g.resolve_minima("aisleprompt", _FakeStorage(over))
    assert g.min_buy_links_for(ap3, bucket="kitchen-buying-guide") == 3
    assert ap3["kitchen_roots"]                                       # repo value kept
    # an unreadable storage copy falls back to the repo default
    class _Broken:
        def read_json(self, key):
            raise OSError("blob down")
    assert g.resolve_minima("aisleprompt", _Broken()) == g.resolve_minima("aisleprompt")


def test_merge_guard_config_adds_new_sites_and_replaces_top_level_keys():
    base = {"schema_version": "1", "default": {"a": 1}, "sites": {"x": {"k": 1}}}
    out = g.merge_guard_config(base, {"schema_version": "2", "default": {"b": 2},
                                      "sites": {"y": {"k": 9}, "x": {"j": 3}}})
    assert out == {"schema_version": "2", "default": {"a": 1, "b": 2},
                   "sites": {"x": {"k": 1, "j": 3}, "y": {"k": 9}}}
    assert base == {"schema_version": "1", "default": {"a": 1}, "sites": {"x": {"k": 1}}}


def test_malformed_buy_link_config_never_raises():
    # run.sh treats a guard exception as "continue without the guard", so a
    # config typo must degrade to the site default, not crash (fail-open).
    cfg = dict(AP_BUY, min_buy_links_by_bucket=["buying-guide"], min_buy_links_by_title="gifts",
               buy_link_patterns=r"^/k/")
    assert g.min_buy_links_for(cfg, bucket="buying-guide", title="Gifts") == 0
    assert g.verify_body_with_config("[x](/k/abc)", {"bucket": "buying-guide"}, cfg).buy_links == 1


def test_directive_reads_the_category_as_well_as_the_bucket():
    prop = {"bucket": "seasonal-occasion", "category": "buying-guide", "title": "Thanksgiving Pies",
            "expected_kitchen_product_slugs": ["pie-dish"]}
    txt = g.render_link_directive(prop, min_recipes=0, min_kits=2, site_root="https://ap.test",
                                  link_cfg=AP_BUY)
    assert "DIRECT BUY LINKS — at least 2 distinct" in txt
