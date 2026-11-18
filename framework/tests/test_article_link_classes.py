"""framework.core.article_link_classes — configurable link classes (build item F5a).

The point of F5a is what does NOT change: `article_link_guard._LINK_RE` keeps
its roots, every existing site's counts stay exactly what they were, and the
new matcher is separate, so a site counts /compat/ links only by configuring
`link_classes`. Proven here on real article bodies from the two existing
sites (fixtures/content_policy/link_corpus, with golden counts recorded from
the guard before this module existed) and on generated bodies. No network,
no DB; storage is a temp LocalFilesystemStorage.
"""
from __future__ import annotations

import json
import pathlib
import random

import pytest

from framework.core import article_link_guard as g
from framework.core import article_link_classes as lc
from framework.core.storage import LocalFilesystemStorage

CORPUS = sorted((pathlib.Path(__file__).parent / "fixtures" / "content_policy" / "link_corpus").glob("corpus-*.json"))
GUARD_ROOTS = ["/recipes/", "/k/", "/product/", "/reviews/", "/blog/", "/kitchen/category/", "/kitchen/"]
POWER = {
    "site_root": "https://power.example",
    "min_recipes": 0, "min_kits": 0, "min_products": 0,
    "link_classes": {"product": {"roots": ["/product/"], "min": 5},
                     "compat": {"roots": ["/compat/"], "min": 2}},
}


def _articles():
    for f in CORPUS:
        doc = json.loads(f.read_text())
        for art in doc["articles"]:
            yield doc["site_hint"], art


ARTICLES = list(_articles())


# ── the guard is untouched ───────────────────────────────────────────────────

def test_link_re_keeps_its_roots_and_never_sees_compat():
    pat = g._LINK_RE.pattern
    assert "(?:recipes|k|product|reviews|blog|kitchen/category|kitchen)" in pat
    assert "compat" not in pat
    body = "See [the hub](/compat/station-a) and [another](/compat/station-b)."
    assert not any(g._count_by_root(body).values())
    assert lc.count_link_classes(body, lc.link_classes_from_guard_config(POWER)) == {"product": 0, "compat": 2}


@pytest.mark.parametrize("site_hint, art", ARTICLES, ids=[a["file"].split("/")[-1][:40] for _s, a in ARTICLES])
def test_existing_corpora_counts_are_unchanged(site_hint, art):
    """Golden counts recorded from the guard before link classes existed."""
    exp = art["expected"]
    by_root = g._count_by_root(art["body_md"])
    assert {r: len(by_root.get(r, ())) for r in GUARD_ROOTS} == exp["by_root"]
    cfg = g.resolve_minima(site_hint, None)
    a = g.verify_body_with_config(art["body_md"], {}, cfg)
    got = {k: getattr(a, k) for k in ("recipe_links", "kitchen_links", "product_links", "blog_links",
                                      "buy_links", "product_page_links", "min_buy_links")}
    got["passes"] = a.passes
    assert got == {k: exp[k] for k in got}
    # The existing sites configure no link classes: the new layer is a no-op.
    assert lc.link_classes_from_guard_config(cfg) == ()
    audit = lc.verify_with_guard_config(art["body_md"], cfg)
    assert audit.passes and audit.counts == {}


@pytest.mark.parametrize("site_hint, art", ARTICLES, ids=[a["file"].split("/")[-1][:40] for _s, a in ARTICLES])
def test_a_class_on_a_guard_root_counts_what_the_guard_counts(site_hint, art):
    body = art["body_md"]
    by_root = g._count_by_root(body)
    # one class per guard root, configured together (the longest configured
    # root wins, as in _LINK_RE's alternation)
    classes = [lc.LinkClass(f"r{i}", (r,)) for i, r in enumerate(GUARD_ROOTS)]
    got = lc.count_link_classes(body, classes)
    assert {r: got[f"r{i}"] for i, r in enumerate(GUARD_ROOTS)} == {r: len(by_root.get(r, ())) for r in GUARD_ROOTS}
    # kitchen roots are a union of slugs, as verify_body counts them
    roots = ("/kitchen/category/", "/kitchen/", "/k/")
    want = g.verify_body(body, {}, min_recipes=0, min_kits=0, kitchen_roots=roots).kitchen_links
    assert lc.count_link_classes(body, [lc.LinkClass("kit", roots)]) == {"kit": want}


def test_parity_holds_on_generated_bodies():
    rnd = random.Random(20261117)
    hosts = ["", "https://site.example", "https://www.site.example", "https://other.example"]
    roots = GUARD_ROOTS + ["/compat/", "/kits/"]
    tails = ["", "?tag=x-20", "#specs", "?a=1#b", "/", "/sub-page"]
    for _ in range(300):
        parts = []
        for _j in range(rnd.randint(0, 12)):
            slug = rnd.choice(["b0abc12345", "B08W8BFG9B", "cookware", "chef-knife", "x", "a-b-c", "-bad", "ok-1"])
            parts.append(f"[{rnd.choice(['Anchor', 'a b', '[x]'])}]({rnd.choice(hosts)}{rnd.choice(roots)}{slug}"
                         f"{rnd.choice(tails)}) {rnd.choice(['text', '', 'and'])}")
        body = " ".join(parts)
        by_root = g._count_by_root(body)
        got = lc.count_link_classes(body, [lc.LinkClass(f"r{i}", (r,)) for i, r in enumerate(GUARD_ROOTS)]
                                    + [lc.LinkClass("compat", ("/compat/",))])
        for i, r in enumerate(GUARD_ROOTS):
            assert got[f"r{i}"] == len(by_root.get(r, ())), (r, body)


def test_only_the_configured_roots_decide_precedence():
    """With /kitchen/ alone configured, a category link is a /kitchen/ link
    (slug "category/<x>"); configure /kitchen/category/ too and it moves
    there, exactly as the guard assigns it."""
    body = "[c](/kitchen/category/cookware) [p](/kitchen/lodge-pan)"
    assert lc.class_slugs(body, [lc.LinkClass("k", ("/kitchen/",))])["k"] == {"category/cookware", "lodge-pan"}
    both = lc.class_slugs(body, [lc.LinkClass("k", ("/kitchen/",)), lc.LinkClass("c", ("/kitchen/category/",))])
    assert both == {"k": {"lodge-pan"}, "c": {"cookware"}}
    assert both["k"] == g._count_by_root(body)["/kitchen/"] and both["c"] == g._count_by_root(body)["/kitchen/category/"]


# ── the new matcher ──────────────────────────────────────────────────────────

def test_compat_links_count_distinct_in_every_link_shape():
    body = ("[Station A](/compat/station-a), [again](/compat/station-a?tag=x-20), "
            "[abs](https://power.example/compat/station-b#panels), [nested](/compat/station-c/panels), "
            "[www](https://www.power.example/compat/station-d/), "
            "[relative, renders under /guides](compat/station-e), [bare /compat/station-f], "
            "![image](/compat/station-g)")
    classes = lc.link_classes_from_guard_config(POWER)
    found = lc.class_slugs(body, classes)
    # Exactly _LINK_RE's shape rules: a trailing slash after the slug does not
    # match, a path without its leading slash or a bare mention is not a link,
    # and image syntax counts (the guard has no lookbehind for "!").
    assert found["compat"] == {"station-a", "station-b", "station-c/panels", "station-g"}
    same_shapes_under_a_guard_root = body.replace("/compat/", "/product/")
    assert lc.class_slugs(same_shapes_under_a_guard_root, [lc.LinkClass("p", ("/product/",))])["p"] == \
        g._count_by_root(same_shapes_under_a_guard_root)["/product/"]


def test_own_host_only_ignores_other_hosts():
    body = "[ours](https://power.example/compat/a) [theirs](https://rival.example/compat/b) [rel](/compat/c)"
    loose = lc.LinkClass("compat", ("/compat/",), 0, False)
    strict = lc.LinkClass("compat", ("/compat/",), 0, True)
    hosts = {"power.example", "www.power.example"}
    assert lc.count_link_classes(body, [loose], hosts) == {"compat": 3}
    assert lc.count_link_classes(body, [strict], hosts) == {"compat": 2}


def test_longest_root_wins_like_the_guard():
    classes = [lc.LinkClass("cat", ("/kitchen/category/",)), lc.LinkClass("page", ("/kitchen/",))]
    body = "[c](/kitchen/category/cookware) [p](/kitchen/lodge-pan)"
    assert lc.count_link_classes(body, classes) == {"cat": 1, "page": 1}


def test_verify_and_addendum_for_a_power_site():
    body = " ".join(f"[P{i}](/product/B0AAAAAAA{i})" for i in range(5)) + " [hub](/compat/station-a)"
    audit = lc.verify_with_guard_config(body, POWER)
    assert not audit.passes
    assert audit.counts == {"product": 5, "compat": 1}
    assert audit.failure_reason() == "only 1 compat links under /compat/ (min 2)"
    add = audit.addendum()
    assert add.startswith("LINK CLASSES — VIOLATED.") and "1/2 required distinct links under /compat/" in add
    assert audit.summary() == "product=5/5 compat=1/2"
    assert lc.verify_with_guard_config(body + " [hub2](/compat/station-b)", POWER).passes


def test_directive_tells_the_writer_up_front():
    txt = lc.render_link_class_directive(lc.link_classes_from_guard_config(POWER), "https://power.example/")
    assert "at least 5 distinct links under /product/" in txt
    assert "[Anchor](https://power.example/compat/<slug>)" in txt
    assert lc.render_link_class_directive([lc.LinkClass("x", ("/x/",), 0)]) == ""


@pytest.mark.parametrize("raw, msg", [
    ([], None), ({}, None), (None, None),
    ("compat", "must be an object"),
    ({"Compat!": {"roots": ["/compat/"]}}, "bad class name"),
    ({"compat": {"roots": []}}, "at least one root"),
    ({"compat": {"roots": ["compat"]}}, "not a path root"),
    ({"compat": {"roots": ["/compat"]}}, "not a path root"),
    ({"compat": {"roots": ["/compat/"], "min": -1}}, ">= 0"),
    ({"compat": {"roots": ["/compat/"], "min": "two"}}, "not an integer"),
    ({"compat": "x"}, "expected an object"),
])
def test_parse_validates(raw, msg):
    if msg is None:
        assert lc.parse_link_classes(raw) == ()
    else:
        with pytest.raises(lc.LinkClassConfigError, match=msg):
            lc.parse_link_classes(raw)


def test_resolve_from_the_link_guard_config_storage_copy(tmp_path):
    s = LocalFilesystemStorage(str(tmp_path))
    s.write_json("config/article-link-guard-config.json", {"sites": {"powersite": dict(POWER)}})
    got = lc.resolve_link_classes("powersite-article-proposal-agent", s)
    assert [(c.name, c.roots, c.min) for c in got] == [("product", ("/product/",), 5), ("compat", ("/compat/",), 2)]
    # every site the repo default knows still resolves to no classes
    repo = json.loads((pathlib.Path(g.__file__).resolve().parents[2] / "config"
                       / "article-link-guard-config.json").read_text())
    for key in repo["sites"]:
        assert lc.resolve_link_classes(key, s) == ()
