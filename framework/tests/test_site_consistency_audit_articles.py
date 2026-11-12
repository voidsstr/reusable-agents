"""Recent-article audit in framework/cli/site_consistency_audit.py.

2026-11-12 article review: the BFS crawl reached a handful of the 54 articles
published in 30 days, so it never reported what their readers saw — heroes of
344-496 px, one product photo heroing four articles, heroes hotlinked from
strangers' servers, a citation rendered as raw markdown, gift guides with no
buy button, 5000-px recipe photos. These pin the checks generalized from the
review's checker. No network, no DB: fetches and the SELECT are stubbed."""
from __future__ import annotations

import time

from framework.cli import site_consistency_audit as sca

CITATION_LEAK = ('<li>[Black Forest Labs — FLUX.2 [klein] 4B model card, Hugging Face]'
                 '(https://huggingface.co/black-forest-labs/FLUX.2-klein-4B) (accessed 2026-10-1)</li>')


def _acfg(**over):
    return sca._deep_merge(sca.ARTICLE_DEFAULTS, over)


# ── raw markdown ─────────────────────────────────────────────────────────

def test_raw_markdown_link_with_brackets_in_anchor_is_found():
    region = f"<h2>Citations and sources</h2><ul>{CITATION_LEAK}</ul>"
    hits = sca.find_raw_markdown(sca.visible_text_lines(region))
    assert [k for k, _ in hits] == ["md-link"]
    assert "huggingface.co" in hits[0][1]


def test_raw_markdown_heading_bold_and_table_leaks_in_prose():
    region = ("<p>## Best overall</p><p>The **Lodge 12-inch** wins.</p>"
              "<p>| Pan | Price | |---|---|</p>")
    kinds = {k for k, _ in sca.find_raw_markdown(sca.visible_text_lines(region))}
    assert kinds == {"heading-hash", "bold-asterisks", "table-pipes"}


def test_rendered_markup_code_and_hash_column_headers_are_clean():
    # A rendered table whose first header cell is "#" put "# Recipe Method" at
    # the start of a line (Whole30 article, 2026-11-12 dry run): not a heading.
    region = ("<h2>Best overall</h2><p>The <strong>Lodge</strong> wins. "
              "<a href='/k/lodge'>Check price</a></p>"
              "<table><tr><th>#</th><th>Recipe</th><th>Method</th></tr>"
              "<tr><td>1</td><td>Chicken</td><td>Bake</td></tr></table>"
              "<pre>def f(**kwargs): **x**</pre><p>Use <code>```</code> fences.</p>"
              "<script>var s = '**bold** [a](https://x)';</script>")
    text = sca.visible_text_lines(region)
    assert sca.find_raw_markdown(text, heading_text=sca.visible_text_lines(region, drop_tables=True)) == []


# ── image headers ─────────────────────────────────────────────────────────

def _png(w, h):
    return b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + w.to_bytes(4, "big") + h.to_bytes(4, "big") + b"\x08\x02\x00\x00\x00"


def _jpeg(w, h):
    app0 = b"\xff\xe0" + (16).to_bytes(2, "big") + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof = b"\xff\xc0" + (17).to_bytes(2, "big") + b"\x08" + h.to_bytes(2, "big") + w.to_bytes(2, "big") + b"\x03" + b"\x00" * 9
    return b"\xff\xd8" + app0 + sof + b"\xff\xda" + b"\x00" * 16


def test_image_dims_from_header_bytes():
    assert sca.image_dims(_png(1200, 800)) == (1200, 800)
    assert sca.image_dims(_jpeg(5000, 3333)) == (5000, 3333)
    assert sca.image_dims(b"GIF89a" + (320).to_bytes(2, "little") + (200).to_bytes(2, "little")) == (320, 200)
    vp8x = (b"RIFF" + b"\x00" * 4 + b"WEBP" + b"VP8X" + (10).to_bytes(4, "little") + b"\x00" * 4
            + (1999).to_bytes(3, "little") + (999).to_bytes(3, "little"))
    assert sca.image_dims(vp8x) == (2000, 1000)
    vp8 = (b"RIFF" + b"\x00" * 4 + b"WEBP" + b"VP8 " + b"\x00" * 4 + b"\x00\x00\x00\x9d\x01\x2a"
           + (640).to_bytes(2, "little") + (480).to_bytes(2, "little"))
    assert sca.image_dims(vp8) == (640, 480)
    bits = 299 | (149 << 14)
    vp8l = b"RIFF" + b"\x00" * 4 + b"WEBP" + b"VP8L" + b"\x00" * 4 + b"\x2f" + bits.to_bytes(4, "little")
    assert sca.image_dims(vp8l) == (300, 150)
    assert sca.image_dims(b"not an image") is None


# ── page facts ───────────────────────────────────────────────────────────

AP_ACFG = _acfg(
    buy_link_re=r"^/k/[A-Za-z0-9-]+|amazon\.[a-z.]+/(?:dp|gp/product)/",
    product_link_re=r"^/kitchen/(?!category/)[A-Za-z0-9-]+/?$")


def test_page_facts_count_buy_and_product_links_inside_the_article_only():
    html = ("<html><head><meta property='og:image' content='/img/hero.jpg'>"
            "<meta name='robots' content='noindex, follow'></head><body>"
            "<nav><a href='/k/nav-deal'>deal</a></nav>"
            "<article><h1>Gifts for Bakers</h1>"
            "<a href='/kitchen/oxo-scale'>OXO</a> <a href='https://aisleprompt.com/kitchen/nordic-sheet'>Nordic</a>"
            "<a href='/kitchen/category/bakeware'>bakeware</a>"
            "<a href='https://www.aisleprompt.com/k/oxo-scale?source=amazon'>Check price</a>"
            "<a href='/k/oxo-scale'>again</a> <a href='https://other.example/k/zzz'>cite</a>"
            "<img src='/img/a.jpg'><img src='/k/clickout.jpg'><img src='data:image/png;base64,xx'>"
            "</article></body></html>")
    f = sca.article_page_facts({"title": "", "categories": ["buying-guide"]},
                               "https://aisleprompt.com/blog/gifts-for-bakers", html, AP_ACFG,
                               {"never_fetch_path_re": sca.DEFAULT_NEVER_FETCH})
    assert f["buy_links"] == 1                     # one distinct own /k/ target; nav + foreign excluded
    assert f["product_links"] == 2                 # category pages are not product pages
    assert f["hero"] == "https://aisleprompt.com/img/hero.jpg"
    assert f["imgs"] == ["https://aisleprompt.com/img/a.jpg"]   # never fetch a click-out
    assert f["title"] == "Gifts for Bakers" and f["noindex"] is True


# ── the checks ───────────────────────────────────────────────────────────

def _fact(slug, hero, **kw):
    f = {"url": f"https://s.test/blog/{slug}", "title": slug, "categories": [], "hero": hero,
         "db_hero": "", "imgs": [], "raw_markdown": [], "buy_links": 1, "product_links": 0,
         "noindex": False}
    f.update(kw)
    return f


def test_evaluate_articles_flags_every_review_finding():
    acfg = _acfg(allowed_image_hosts=["cdn.test", "s.test"], monetized_buckets=["buying-guide"],
                 monetized_title_re=r"\b(gifts?|checklist)\b", product_link_re=r"^/kitchen/",
                 severity={"small-hero": "error"})
    facts = [
        _fact("rtx-spark", "https://cdn.test/I/81ubl5FITQL._AC_SL1500_.jpg"),
        _fact("dgx-spark", "https://cdn.test/I/81ubl5FITQL._AC_SX679_.jpg"),       # same photo, other size
        _fact("rtx-3060", "https://cdn.test/I/31crgwIcrLL._AC_SL1500_.jpg"),       # 436 px
        _fact("ricer", "https://sftimes.example/1/2/x.jpg"),                       # hotlinked
        _fact("coffee-gifts", "https://cdn.test/article-coffee.jpg", title="Gifts for Coffee Lovers",
              buy_links=0, product_links=15, imgs=["https://cdn.test/38959.jpg"]),
        _fact("moccamaster", "https://cdn.test/article-moccamaster.jpg"),          # same bytes as coffee
        _fact("flux", "https://cdn.test/I/71OF.jpg", raw_markdown=[("md-link", "Face](https://hf.co/x)")]),
        _fact("whole30", "https://cdn.test/w30.jpg", noindex=True, buy_links=0, product_links=3),
        _fact("diwali", "https://cdn.test/diwali.jpg", buy_links=0, product_links=0),  # names no product
    ]
    big = {"w": 1500, "h": 1000, "bytes": 180_000}
    probes = {f["hero"]: dict(big, sha1=f["hero"]) for f in facts}
    probes["https://cdn.test/I/31crgwIcrLL._AC_SL1500_.jpg"] = {"w": 436, "h": 172, "bytes": 11_983}
    probes["https://cdn.test/article-coffee.jpg"] = dict(big, sha1="same-bytes")
    probes["https://cdn.test/article-moccamaster.jpg"] = dict(big, sha1="same-bytes")
    probes["https://cdn.test/38959.jpg"] = {"w": 5000, "h": 3333, "bytes": 1_119_660}
    out = sca.evaluate_articles(facts, probes, acfg)
    by = {}
    for f in out:
        by.setdefault(f["kind"], []).append(f["url"].rsplit("/", 1)[-1])
        assert f["recent_article"] is True
    assert sorted(by["duplicate-hero"]) == ["coffee-gifts", "dgx-spark", "moccamaster", "rtx-spark"]
    assert by["small-hero"] == ["rtx-3060"]
    assert by["hero-offsite-host"] == ["ricer"]
    assert sorted(by["no-buy-link-on-monetized-article"]) == ["coffee-gifts", "whole30"]
    assert by["raw-markdown"] == ["flux"]
    assert by["recent-article-noindex"] == ["whole30"]
    assert by["heavy-image"] == ["38959.jpg"]
    sev = {f["kind"]: f["severity"] for f in out}
    assert sev["small-hero"] == "error"                      # config override
    assert sev["raw-markdown"] == "error" and sev["duplicate-hero"] == "warn"
    heavy = next(f for f in out if f["kind"] == "heavy-image")
    assert heavy["on_page"].endswith("/coffee-gifts") and heavy["pages"] == 1


def test_unknown_image_sizes_and_empty_allow_list_are_not_findings():
    facts = [_fact("a", "https://anywhere.example/a.jpg", imgs=["https://anywhere.example/b.jpg"])]
    probes = {"https://anywhere.example/a.jpg": {"status": 0, "error": "URLError"}}
    assert sca.evaluate_articles(facts, probes, _acfg()) == []


# ── config ───────────────────────────────────────────────────────────────

def test_repo_config_configures_both_sites_by_host():
    cfg = sca.load_config()
    for base, route in (("https://www.aisleprompt.com", "/blog/"), ("https://specpicks.com/", "/reviews/")):
        site = sca.site_config(cfg, base)
        acfg = site["article_audit"]
        assert acfg["configured"] and acfg["enabled"] and route in acfg["url_template"]
        assert "%(days)s" in acfg["query"] and acfg["dsn_env"].startswith("DATABASE_URL_")
        assert "nscagentstorage.blob.core.windows.net" in acfg["allowed_image_hosts"]
        assert acfg["hero_min_width"] >= 1000 and acfg["max_image_width"] == 3000
        assert sca._never_fetch("/k/some-product", site) and not sca._never_fetch("/img/k/x.jpg", site)
    other = sca.site_config(cfg, "http://localhost:4001")
    assert other["article_audit"]["configured"] is False and other["matched_site"] == ""


def test_storage_copy_and_config_file_layer_over_the_repo_default(tmp_path):
    class _Store:
        def read_json(self, key):
            assert key == "config/site-consistency-audit-config.json"
            return {"sites": {"specpicks.com": {"article_audit": {"window_days": 14,
                                                                   "severity": {"small-hero": "info"}}}}}
    extra = tmp_path / "extra.json"
    extra.write_text('{"sites": {"specpicks.com": {"article_audit": {"hero_min_width": 1200}}}}')
    acfg = sca.site_config(sca.load_config(str(extra), _Store()), "https://specpicks.com")["article_audit"]
    assert acfg["window_days"] == 14 and acfg["hero_min_width"] == 1200
    assert acfg["severity"]["small-hero"] == "info" and acfg["severity"]["raw-markdown"] == "error"
    assert acfg["url_template"].endswith("/reviews/{slug}")      # repo value kept


# ── sources ──────────────────────────────────────────────────────────────

class _Cur:
    description = [("slug",), ("title",), ("category",), ("bucket",), ("hero_image_url",), ("published_at",)]

    def __init__(self, rows):
        self.rows, self.sql, self.params = rows, None, None

    def execute(self, sql, params):
        self.sql, self.params = sql, params

    def fetchall(self):
        return self.rows


class _Conn:
    def __init__(self, rows):
        self.cur, self.closed = _Cur(rows), False

    def cursor(self):
        return self.cur

    def close(self):
        self.closed = True


def test_db_source_builds_urls_and_passes_window_params(monkeypatch):
    monkeypatch.setenv("DSN_TEST_SITE", "postgresql://ro@x/db")
    conn = _Conn([("gifts-for-bakers", "Gifts for Bakers", "buying-guide", "seasonal-occasion",
                   "https://cdn.test/h.jpg", "2026-11-11 10:00:00"), ("", "no slug", None, None, None, None)])
    seen = []
    acfg = _acfg(dsn_env="DSN_TEST_SITE", query="SELECT … %(days)s %(limit)s",
                 url_template="https://s.test/blog/{slug}", window_days=7)
    arts, src, err = sca.recent_articles("https://s.test", acfg, 5,
                                         connect=lambda d: seen.append(d) or conn)
    assert (src, err, seen) == ("db", "", ["postgresql://ro@x/db"])
    assert conn.cur.params == {"days": 7, "limit": 200} and conn.closed
    assert arts == [{"url": "https://s.test/blog/gifts-for-bakers", "slug": "gifts-for-bakers",
                     "title": "Gifts for Bakers", "categories": ["buying-guide", "seasonal-occasion"],
                     "db_hero": "https://cdn.test/h.jpg", "published_at": "2026-11-11 10:00:00"}]


def test_sitemap_source_filters_by_path_and_lastmod(monkeypatch):
    now = time.mktime(time.strptime("2026-11-12", "%Y-%m-%d"))
    index = "<sitemapindex><sitemap><loc>https://s.test/sitemap-blog.xml</loc></sitemap></sitemapindex>"
    blog = ("<urlset>"
            "<url><loc>https://s.test/blog/new-guide</loc><lastmod>2026-11-10</lastmod></url>"
            "<url><loc>https://s.test/blog/old-guide</loc><lastmod>2026-08-01</lastmod></url>"
            "<url><loc>https://s.test/recipes/soup-1</loc><lastmod>2026-11-11</lastmod></url>"
            "<url><loc>https://s.test/blog/no-date</loc></url></urlset>")
    docs = {"https://s.test/sitemap.xml": index, "https://s.test/sitemap-blog.xml": blog}
    calls = []

    def fake_get(url, timeout, method="GET", ua=sca.UA, retries=1, headers=None):
        calls.append((url, headers))
        return 200, {}, docs[url], 0.01
    monkeypatch.setattr(sca, "_get", fake_get)
    acfg = _acfg(sitemap_path_re=r"^/blog/[a-z0-9-]+$", window_days=30)
    arts = sca.recent_articles_from_sitemap("https://s.test", acfg, 5, headers={"x-probe": "1"}, now=now)
    assert [a["url"] for a in arts] == ["https://s.test/blog/new-guide"]
    assert all(h == {"x-probe": "1"} for _u, h in calls)


def test_no_usable_source_reports_the_audit_unavailable(monkeypatch):
    monkeypatch.delenv("DSN_MISSING_SITE", raising=False)
    site = {"article_audit": _acfg(dsn_env="DSN_MISSING_SITE", query="SELECT 1",
                                   url_template="https://s.test/blog/{slug}", configured=True)}
    out, summary = sca.audit_recent_articles("https://s.test", site, {}, 5, 2)
    assert [f["kind"] for f in out] == ["article-audit-unavailable"]
    assert "no usable source" in out[0]["message"] and summary["articles"] == 0


# ── end to end (stubbed network) ────────────────────────────────────────────

PAGE_CFG = dict(min_words=20, slow_s=8.0, title_max=65, min_internal_links=0, leak_tokens=[],
                monetized_re="", editorial_re="", imgs_per_page=6, dupe_title_cap=3,
                check_sitemap=False, max_images=0, vision_hero_check=False, recheck_delay_s=0,
                tiny_img_bytes=0, never_fetch_path_re=sca.DEFAULT_NEVER_FETCH)


def _article_html(title, words, hero, extra=""):
    return (f"<html><head><title>{title}</title><link rel='canonical' href='x'>"
            f"<meta name='description' content='{'d' * 80}'><meta property='og:image' content='{hero}'>"
            f"</head><body><article><h1>{title}</h1><p>{'word ' * words}</p>{extra}</article></body></html>")


def test_audit_recent_articles_covers_uncrawled_articles(monkeypatch):
    monkeypatch.setenv("DSN_E2E_SITE", "postgresql://ro@x/db")
    rows = [("crawled", "Crawled Guide", "kitchen-buying-guide", None, None, None),
            ("not-crawled", "Gifts for Bakers", "seasonal-occasion", None, None, None)]
    pages = {
        "https://s.test/blog/crawled": _article_html("Crawled Guide", 5, "https://cdn.test/h1.jpg",
                                                     "<a href='/k/pan'>Check price</a>"),
        "https://s.test/blog/not-crawled": _article_html("Gifts for Bakers", 5, "https://cdn.test/h2.jpg",
                                                         "<a href='/kitchen/oxo-scale'>OXO</a>"
                                                         "<img src='https://cdn.test/huge.jpg'>"),
    }
    calls = []

    def fake_get(url, timeout, method="GET", ua=sca.UA, retries=1, headers=None):
        calls.append((url, headers))
        return 200, {"Content-Type": "text/html"}, pages[url], 0.1

    def fake_probe(url, timeout, full=False, probe_bytes=65536, max_bytes=0):
        return {"https://cdn.test/h1.jpg": {"w": 1500, "bytes": 200_000, "sha1": "a"},
                "https://cdn.test/h2.jpg": {"w": 470, "bytes": 22_000, "sha1": "b"},
                "https://cdn.test/huge.jpg": {"w": 5000, "bytes": 2_000_000}}[url]
    monkeypatch.setattr(sca, "_get", fake_get)
    monkeypatch.setattr(sca, "probe_image", fake_probe)
    site = sca.site_config({"sites": {"s.test": {
        "request_headers": {"x-probe": "audit"},
        "article_audit": {"dsn_env": "DSN_E2E_SITE", "query": "SELECT", "url_template": "https://s.test/blog/{slug}",
                          "monetized_buckets": ["kitchen-buying-guide"], "monetized_title_re": r"\bgifts?\b",
                          "product_link_re": r"^/kitchen/(?!category/)", "buy_link_re": r"^/k/"}}}},
        "https://s.test")
    out, summary = sca.audit_recent_articles("https://s.test", site, PAGE_CFG, 5, 2,
                                             crawled={"https://s.test/blog/crawled"},
                                             connect=lambda d: _Conn(rows))
    kinds = sorted((f["kind"], f["url"].rsplit("/", 1)[-1]) for f in out)
    # the thin page check runs only for the article the crawl did not reach
    assert ("thin-or-shell", "not-crawled") in kinds and ("thin-or-shell", "crawled") not in kinds
    assert ("no-buy-link-on-monetized-article", "not-crawled") in kinds
    assert ("small-hero", "not-crawled") in kinds and ("heavy-image", "huge.jpg") in kinds
    assert not any(k == "no-buy-link-on-monetized-article" and u == "crawled" for k, u in kinds)
    assert summary == {"source": "db", "window_days": 30, "articles": 2, "audited": 2,
                       "not_in_crawl": 1, "error": "", "images_probed": 3}
    assert all(h == {"x-probe": "audit"} for _u, h in calls)


def test_crawl_never_requests_click_outs(monkeypatch):
    home = ("<html><head><title>Home page title</title></head><body><h1>Home</h1>"
            "<a href='/k/some-pan?source=amazon'>buy</a><a href='/r/recipe-out'>go</a>"
            "<a href='/blog/a-guide'>guide</a></body></html>")
    calls = []

    def fake_get(url, timeout, method="GET", ua=sca.UA, retries=1, headers=None):
        calls.append(url)
        return 200, {"Content-Type": "text/html"}, home, 0.1
    monkeypatch.setattr(sca, "_get", fake_get)
    sca.crawl("https://s.test", 2, 10, 5, 2, dict(PAGE_CFG))
    assert "https://s.test/blog/a-guide" in calls
    assert not [u for u in calls if "/k/" in u or "/r/" in u]


# ── hidden article body, shared fallback cards, detection = prevention ────────
# 2026-11-12 re-review: aisleprompt ships its guide body in
# <main id="ap-seo" style="display:none">, visible only with JS off, so the
# audit's verdicts described a panel Google's renderer never shows (R2-T01).

def _page(main_attrs, inner="<p>body</p>", before=""):
    return (f"<html><head><meta property='og:image' content='/h.jpg'></head><body>{before}"
            f"<main{main_attrs}><article><h1>Gifts</h1>{inner}"
            "<svg aria-hidden=\"true\"></svg><input type=\"hidden\" name=\"x\"></article></main>"
            "<noscript><style>#ap-seo{display:block !important}</style></noscript>"
            "<div id=\"root\"></div></body></html>")


def _facts_of(html, acfg=None):
    return sca.article_page_facts({"title": "Gifts", "categories": []}, "https://s.test/blog/gifts",
                                  html, acfg or _acfg(), {})


def test_hidden_article_body_is_found_on_the_region_or_any_ancestor():
    hidden = _facts_of(_page(' id="ap-seo" style="display:none" aria-hidden="false"'))
    assert hidden["hidden_by"].startswith('<main id="ap-seo" style="display:none"')
    assert hidden["hidden_by"].endswith("(display:none)")
    # the R2-T01 fix ships the panel shown: no finding
    assert _facts_of(_page(' id="ap-seo" aria-hidden="false"'))["hidden_by"] == ""
    # svg / input "hidden" inside the article, and unclosed <p>/<li> before it, are not the article
    assert _facts_of(_page("", before="<p>intro<ul><li>a<li>b</ul><br><img src=x>"))["hidden_by"] == ""
    assert "(hidden attribute)" in _facts_of(_page("", before="<div hidden>"))["hidden_by"]
    assert "(inside <noscript>)" in _facts_of(_page("", before="<noscript>"))["hidden_by"]
    assert 'aria-hidden="true"' in _facts_of(_page(' aria-hidden="true"'))["hidden_by"]
    assert "visibility:hidden" in _facts_of(_page(' style="visibility: hidden"'))["hidden_by"]
    # a comment or a script mentioning <main style="display:none"> is not markup
    assert _facts_of(_page("", before="<!-- <div style='display:none'> -->"
                                      "<script>var s='<div hidden>';</script>"))["hidden_by"] == ""


def test_hidden_article_body_finding_and_the_opt_out():
    f = _fact("gifts", "https://cdn.test/h.jpg", hidden_by='<main id="ap-seo" style="display:none"> (display:none)')
    out = sca.evaluate_articles([f], {}, _acfg())
    assert [(x["kind"], x["severity"]) for x in out] == [("hidden-article-body", "error")]
    assert "display:none" in out[0]["message"]
    assert sca.evaluate_articles([f], {}, _acfg(hidden_body_ok=True)) == []


FALLBACK = [f"/recipes/{s}" for s in ("spaghetti-64137", "omelet-muffins-16368", "quinoa-bowl-165970",
                                      "chickpea-salad-1", "lentil-soup-2", "tuna-wrap-3")]


def test_shared_link_set_flags_one_fallback_module_on_many_articles():
    acfg = _acfg(shared_link_re=r"^/recipes/[A-Za-z0-9-]+-[0-9]+/?$", shared_link_set_min=6)
    html = ("<article><h1>Christmas Cookie Recipes</h1>"
            + "".join(f"<a href='/recipes/cookie-{i}-{i}'>c</a>" for i in range(5))
            + "<a href='/recipes/category/cookies'>hub</a>"
            + "".join(f"<a href='https://s.test{u}?ref=card'>card</a>" for u in FALLBACK) + "</article>")
    f = sca.article_page_facts({"title": "x"}, "https://s.test/blog/cookies", html, acfg, {})
    assert f["link_set"] == sorted([f"/recipes/cookie-{i}-{i}" for i in range(5)] + FALLBACK)
    facts = [
        _fact("cookies", "https://cdn.test/1.jpg", link_set=f["link_set"]),
        _fact("coffee-gifts", "https://cdn.test/2.jpg", link_set=FALLBACK + ["/recipes/latte-9"]),
        _fact("whole30", "https://cdn.test/3.jpg", link_set=FALLBACK[:5] + ["/recipes/salmon-7"]),
    ]
    out = [x for x in sca.evaluate_articles(facts, {}, acfg) if x["kind"] == "shared-link-set"]
    assert sorted(x["url"].rsplit("/", 1)[-1] for x in out) == ["coffee-gifts", "cookies"]
    assert out[0]["severity"] == "warn" and "6 of its card links" in out[0]["message"]
    assert [x for x in sca.evaluate_articles(facts, {}, _acfg()) if x["kind"] == "shared-link-set"] == []


def test_product_link_pattern_matches_a_link_with_a_query():
    acfg = _acfg(product_link_re=r"^/kitchen/(?!category/)[A-Za-z0-9-]+/?$", buy_link_re=r"^/k/")
    html = ("<article><a href='/kitchen/oxo-scale?ref=gift'>OXO</a><a href='/kitchen/nordic/'>N</a>"
            "<a href='/kitchen/category/bakeware?x=1'>cat</a><a href='/k/oxo-scale?source=amazon'>buy</a></article>")
    f = sca.article_page_facts({}, "https://s.test/blog/x", html, acfg, {})
    assert (f["product_links"], f["buy_links"]) == (2, 1)


def test_audit_reports_buy_link_gaps_exactly_where_the_guard_asks_for_buy_links():
    # The audit flags no-buy-link-on-monetized-article on titles and product
    # pages; the link guard must ask the author for buy links on the same ones
    # (2026-11-12 re-review: they disagreed on product pages and on best/vs).
    from framework.core import article_link_guard as g
    acfg = sca.site_config(sca.load_config(), "https://aisleprompt.com")["article_audit"]
    guard = g.resolve_minima("aisleprompt")
    title_rx = sca._rx(acfg["monetized_title_re"])
    for title in ("Gifts for Bakers 2026", "Thanksgiving Kitchen Gear Checklist",
                  "Black Friday Kitchen Deals 2026", "Best Christmas Cookie Recipes",
                  "Butter vs Margarine in Cookies", "Is Sous Vide Worth It?", "Diwali Recipes"):
        audit_flags = bool(title_rx.search(title))
        guard_asks = g.min_buy_links_for(guard, bucket="recipe-cluster", title=title) > 0
        assert audit_flags == guard_asks, title
    for bucket in acfg["monetized_buckets"]:
        assert g.min_buy_links_for(guard, bucket=bucket) > 0, bucket
    prod_rx = sca._rx(acfg["product_link_re"])
    for path in ("/kitchen/oxo-scale", "/kitchen/oxo-scale/", "/kitchen/category/bakeware",
                 "/kitchen/compare", "/kitchen/buying-guides", "/kitchen/"):
        guard_names = bool(g.product_page_targets(f"[x]({path})", guard["product_page_patterns"]))
        assert bool(prod_rx.search(path)) == guard_names, path
    assert acfg["monetized_min_product_links"] == 1 and guard["min_buy_links_if_product_pages"] >= 1
