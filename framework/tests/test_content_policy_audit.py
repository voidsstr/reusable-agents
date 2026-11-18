"""framework.core.content_policy_audit + framework.cli.content_policy_scan — the
content-policy check group over RENDERED pages (build item F6a; the kit gate,
K5, calls the CLI).

Fixtures: three U.S. federal outage / generator-safety pages (public domain,
trimmed) that WARN about generators and medical devices and must pass, and
two kit-shaped rendered guides, one allowed and one that breaks the policy in
body copy, a unit-header table, JSON-LD FAQ answers and an oversized callout.
Crawling runs against an in-memory fetcher: no network.
"""
from __future__ import annotations

import io
import json
import pathlib

import pytest

from framework.cli import content_policy_scan as scan_cli
from framework.core import content_policy as cp
from framework.core import content_policy_audit as cpa

HTML = pathlib.Path(__file__).parent / "fixtures" / "content_policy" / "html"
ALL_SETS = ["electrical", "electrical-year1", "medical"]
RULE_ARGS = [a for s in ALL_SETS for a in ("--rule-set", s)]


@pytest.fixture(scope="module")
def pol():
    return cp.resolve_policy(rule_sets=ALL_SETS, config=cp.load_config())


def _read(name: str) -> str:
    return (HTML / name).read_text(encoding="utf-8")


# ── extraction ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", sorted(p.name for p in HTML.glob("gov-*.html")))
def test_official_safety_guidance_passes(pol, name):
    a = cpa.audit_html(pol, _read(name), url=name)
    assert a.violations == [], [(v.rule_id, v.excerpt) for v in a.violations]


def test_official_guidance_is_actually_about_the_banned_practices(pol):
    """The gov fixtures are only a negation test if they mention what the
    rules look for: the CPSC page's warning must be found AND exempted."""
    a = cpa.audit_html(pol, _read("gov-cpsc-carbon-monoxide.html"))
    assert any(e.rule_id == "electrical.indoor-generator" and e.reason.startswith("prohibition")
               for e in a.exemptions)
    text = " ".join(b.text for b in cpa.html_blocks(_read("gov-ready-power-outages.html"))[0])
    assert "generator" in text.lower() and "medical devices" in text.lower()


def test_allowed_guide_passes_with_callouts_lists_and_jsonld(pol):
    a = cpa.audit_html(pol, _read("rp-guide-allowed.html"))
    assert a.violations == [] and a.notes == []
    reasons = {e.reason for e in a.exemptions}
    assert {"callout", "negated-lead-in"} <= reasons
    assert any(r.startswith("prohibition") for r in reasons)


def test_rejected_guide_fails_everywhere_it_should(pol):
    a = cpa.audit_html(pol, _read("rp-guide-rejected.html"), url="https://power.example/guides/x")
    got = {(v.rule_id, v.field) for v in a.rejects}
    assert ("electrical.backfeed", "body") in got
    assert ("electrical.dryer-outlet-hookup", "body") in got
    assert ("electrical-year1.240v-cords", "body") in got
    assert ("electrical.double-male-cord", "body") in got          # inside the oversized "callout"
    assert ("electrical.licensed-electrician-notice", "title") in got
    med = [v.excerpt for v in a.rejects if v.rule_id == "medical.medical-runtime-claim"]
    assert "Oxygen concentrator | 300 Watts | 3 Hours" in med       # table cell takes its unit header
    assert "About 20 hours without the humidifier." in med          # JSON-LD answer under its question
    assert a.notes and "over 700" in a.notes[0]
    assert all(v.url == "https://power.example/guides/x" for v in a.violations)


def test_html_blocks_fields_and_structure():
    html = ("<html><head><title>T One</title><meta name='description' content='Desc here'>"
            "<meta property='og:title' content='OG T'><script>var backfeed = 1;</script>"
            "<style>.x{}</style></head><body><h1>Head</h1><p>Para <b>bold</b> text<br>more</p>"
            "<ul><li>one</li><li>two</li></ul><table><tr><th>Load</th><th>Hours</th></tr>"
            "<tr><td>CPAP</td><td>20</td></tr></table><img alt='a dryer outlet adapter'>"
            "<div data-callout='safety'><p>Mentions backfeeding.</p></div></body></html>")
    blocks, notes = cpa.html_blocks(html)
    got = [(b.field, b.kind, b.text, b.callout) for b in blocks]
    assert ("title", "title", "T One", False) in got and ("excerpt", "meta", "Desc here", False) in got
    assert ("title", "title", "OG T", False) in got and ("title", "heading", "Head", False) in got
    assert ("body", "paragraph", "Para bold text more", False) in got
    assert ("body", "list_item", "one", False) in got and ("body", "table_row", "CPAP | 20 Hours", False) in got
    assert ("body", "paragraph", "a dryer outlet adapter", False) in got
    assert ("body", "paragraph", "Mentions backfeeding.", True) in got
    assert not any("var backfeed" in b.text for b in blocks) and notes == []


def test_a_page_without_closing_head_is_still_scanned(pol):
    html = ("<html><head><title>Outage tips</title><meta name=description content='x'>"
            "<body><p>Backfeed through the dryer outlet.</p></body></html>")
    assert "electrical.backfeed" in {v.rule_id for v in cpa.audit_html(pol, html).rejects}


def test_malformed_html_does_not_stop_the_scan(pol):
    a = cpa.audit_html(pol, "<p>Backfeed through the dryer outlet<div><span></p></div></td></tr>")
    assert "electrical.backfeed" in {v.rule_id for v in a.rejects}


def test_inactive_policy_finds_nothing():
    off = cp.resolve_policy("no-such-site", config=cp.load_config())
    assert cpa.audit_html(off, _read("rp-guide-rejected.html")).violations == []
    assert cpa.crawl_check_group("https://x.example", off, fetch=lambda u: cpa.Fetched(u, 500)) == []


# ── findings (the site_consistency_audit shape) ─────────────────────────────

def test_findings_shape_and_severity(pol):
    rep = cpa.scan_documents(pol, [("https://p.example/guides/bad", _read("rp-guide-rejected.html")),
                                   ("https://p.example/guides/ok", _read("rp-guide-allowed.html"))])
    rep.pages.append(cpa.PageResult("https://p.example/guides/gone", 404, None, "HTTP 404"))
    fs = cpa.findings(rep)
    kinds = {f["kind"] for f in fs}
    assert {cpa.KIND_REJECT, cpa.KIND_NOTICE, cpa.KIND_UNAVAILABLE} <= kinds
    for f in fs:
        assert set(f) >= {"severity", "kind", "url", "message", "group"} and f["group"] == cpa.CHECK_GROUP
        assert f["url"].startswith("https://p.example/guides/")
    assert {f["severity"] for f in fs if f["kind"] == cpa.KIND_REJECT} == {"error"}
    assert cpa.findings(rep, {cpa.KIND_REJECT: "warn"})[0]["severity"] in ("warn", "error")
    assert rep.to_dict()["rejects"] == rep.rejects > 0


# ── fetching / crawling (in-memory) ─────────────────────────────────────────

class FakeWeb:
    def __init__(self, pages: dict[str, str]):
        self.pages, self.calls = pages, []

    def __call__(self, url: str) -> cpa.Fetched:
        self.calls.append(url)
        if url in self.pages:
            return cpa.Fetched(url, 200, self.pages[url], url)
        return cpa.Fetched(url, 404, "", url, "HTTP 404")


def _site(bad: bool = True) -> FakeWeb:
    base = "https://power.example"
    return FakeWeb({
        f"{base}/sitemap.xml": f"<sitemapindex><sitemap><loc>{base}/sitemap-guides.xml</loc></sitemap></sitemapindex>",
        f"{base}/sitemap-guides.xml": (f"<urlset><url><loc>{base}/guides/good</loc></url>"
                                       f"<url><loc>{base}/guides/bad</loc></url>"
                                       f"<url><loc>{base}/product/B0AAAAAAA1</loc></url>"
                                       f"<url><loc>https://elsewhere.example/guides/x</loc></url></urlset>"),
        f"{base}/guides": (f"<a href='/guides/good'>g</a> <a href='{base}/guides/bad'>b</a> "
                           f"<a href='/r/B0AAAAAAA1'>buy</a> <a href='/api/outbound-click'>x</a> "
                           f"<a href='https://elsewhere.example/guides/x'>off-site</a>"),
        f"{base}/guides/good": _read("rp-guide-allowed.html"),
        f"{base}/guides/bad": _read("rp-guide-rejected.html") if bad else _read("rp-guide-allowed.html"),
    })


def test_sitemap_urls_filters_by_host_and_path():
    web = _site()
    urls = cpa.sitemap_urls("https://power.example", web, path_re=r"^/guides/")
    assert urls == ["https://power.example/guides/good", "https://power.example/guides/bad"]
    with pytest.raises(RuntimeError):
        cpa.sitemap_urls("https://nothing.example", web)


def test_crawl_never_fetches_click_outs_or_other_hosts():
    web = _site()
    got = cpa.crawl(["https://power.example/guides"], web, path_re=r"^/guides/", follow_re=r"^/guides")
    assert sorted(f.url for f in got) == ["https://power.example/guides/bad", "https://power.example/guides/good"]
    assert not any("/r/" in u or "/api/" in u or "elsewhere" in u for u in web.calls)


def test_crawl_check_group_sitemap_then_crawl_fallback(pol):
    fs = cpa.crawl_check_group("https://power.example", pol, fetch=_site())
    assert {f["url"] for f in fs} == {"https://power.example/guides/bad"}
    assert cpa.crawl_check_group("https://power.example", pol, fetch=_site(bad=False)) == []
    no_sitemap = _site()
    del no_sitemap.pages["https://power.example/sitemap.xml"]
    fs2 = cpa.crawl_check_group("https://power.example", pol, fetch=no_sitemap)
    assert {f["url"] for f in fs2} == {"https://power.example/guides/bad"}
    empty = cpa.crawl_check_group("https://empty.example", pol, fetch=FakeWeb({}))
    assert [f["kind"] for f in empty] == [cpa.KIND_UNAVAILABLE]


# ── the CLI the kit gate calls ──────────────────────────────────────────────

def _cli(argv, capsys, stdin: str | None = None, monkeypatch=None):
    if stdin is not None:
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    rc = scan_cli.main(argv)
    out, err = capsys.readouterr()
    return rc, out, err


def test_cli_exit_codes_on_files_and_directories(capsys, tmp_path):
    rc, out, err = _cli([*RULE_ARGS, "--no-storage", str(HTML / "rp-guide-allowed.html")], capsys)
    assert rc == 0 and out == "" and "1 page(s) scanned, 0 reject" in err
    rc, out, _err = _cli([*RULE_ARGS, "--no-storage", str(HTML / "rp-guide-rejected.html")], capsys)
    assert rc == 1 and "electrical.backfeed" in out and "[reject]" in out
    rc, _out, err = _cli([*RULE_ARGS, "--no-storage", str(HTML)], capsys)          # a directory of pages
    assert rc == 1 and f"{len(list(HTML.glob('*.html')))} page(s) scanned" in err
    rep = tmp_path / "r" / "report.json"
    rc, out, _err = _cli([*RULE_ARGS, "--no-storage", "--json", "--report-json", str(rep),
                          str(HTML / "rp-guide-rejected.html")], capsys)
    lines = [json.loads(x) for x in out.splitlines()]
    assert rc == 1 and lines[0]["passes"] is False and json.loads(rep.read_text())["rejects"] >= 6


def test_cli_strict_makes_warnings_fail(capsys, tmp_path):
    page = tmp_path / "p.html"
    page.write_text("<p>The panel kit adds 240 V split-phase output.</p>")
    assert _cli([*RULE_ARGS, "--no-storage", str(page)], capsys)[0] == 0
    assert _cli([*RULE_ARGS, "--no-storage", "--strict", str(page)], capsys)[0] == 1


def test_cli_markdown_and_stdin(capsys, monkeypatch, tmp_path):
    md = tmp_path / "a.md"
    md.write_text("# Guide\n\nBackfeed through the dryer outlet.\n")
    rc, out, _ = _cli([*RULE_ARGS, "--no-storage", str(md)], capsys)
    assert rc == 1 and str(md) in out
    rc, out, _ = _cli([*RULE_ARGS, "--no-storage", "--stdin"], capsys,
                      stdin="<p>Never backfeed your home.</p>", monkeypatch=monkeypatch)
    assert rc == 0
    rc, out, _ = _cli([*RULE_ARGS, "--no-storage", "--stdin", "--format", "markdown"], capsys,
                      stdin="> **Safety:** Backfeed only with the main off.\n", monkeypatch=monkeypatch)
    assert rc == 1 and "<stdin>" in out


def test_cli_policy_selection_and_usage_errors(capsys, tmp_path):
    page = str(HTML / "rp-guide-rejected.html")
    rc, _out, err = _cli(["--site", "no-such-site", "--no-storage", page], capsys)
    assert rc == 2 and "no rule set applies" in err
    assert _cli(["--site", "no-such-site", "--no-storage", "--allow-empty-policy", page], capsys)[0] == 0
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"sites": {"powersite": {"rule_sets": ["medical"]}}}))
    rc, out, _err = _cli(["--site", "powersite", "--no-storage", "--config", str(cfg), page], capsys)
    assert rc == 1 and "medical.medical-runtime-claim" in out and "electrical.backfeed" not in out
    assert _cli(["--rule-set", "nope", "--no-storage", page], capsys)[0] == 2
    assert _cli(["--no-storage", *RULE_ARGS], capsys)[0] == 2                     # nothing to scan
    assert _cli(["--no-storage", *RULE_ARGS, "--sitemap"], capsys)[0] == 2        # --sitemap needs --base
    assert _cli(["--no-storage", *RULE_ARGS, "--header", "nocolon", "--url", "http://x"], capsys)[0] == 2
    rc, out, _ = _cli(["--no-storage", *RULE_ARGS, str(tmp_path / "missing.html")], capsys)
    assert rc == 3 and "ERROR" in out


def test_cli_sitemap_mode_uses_the_fetcher(capsys, monkeypatch):
    web = _site()
    monkeypatch.setattr(scan_cli, "http_fetcher", lambda **kw: web)
    rc, out, err = _cli([*RULE_ARGS, "--no-storage", "--base", "https://power.example", "--sitemap",
                         "--path-re", "^/guides/"], capsys)
    assert rc == 1 and "https://power.example/guides/bad" in out and "2 page(s) scanned" in err
    rc, out, err = _cli([*RULE_ARGS, "--no-storage", "--base", "https://power.example", "--crawl",
                         "--url", "https://power.example/guides", "--path-re", "^/guides/",
                         "--follow-re", "^/guides"], capsys)
    assert rc == 1 and not any("/r/" in u for u in web.calls)


# ── review round 1: lead-ins, JSON-LD sections, skip markers ───────────────

@pytest.mark.parametrize("html, passes", [
    ("<h2>Never lose power again</h2><ul><li>Backfeed through the dryer outlet</li></ul>", False),
    ("<p>Dangerous storms are coming, so here is the plan:</p><ul><li>Backfeed through the dryer outlet</li></ul>",
     False),
    ("<h2>What not to do</h2><ol><li>Backfeed through the dryer outlet</li></ol>", False),
    ("<h2>What not to do</h2><ul><li>Backfeed through the dryer outlet</li></ul>", True),
])
def test_html_lists_need_a_prohibition_lead_in_and_never_numbered(pol, html, passes):
    a = cpa.audit_html(pol, f"<html><body>{html}</body></html>")
    assert ("electrical.backfeed" not in {v.rule_id for v in a.rejects}) is passes


def test_jsonld_sections_end_with_their_answer_and_document(pol):
    q = ('{"@type":"Question","name":"What about medical devices like a CPAP?",'
         '"acceptedAnswer":{"@type":"Answer","text":"ASK"}}')
    faq = '{"@type":"FAQPage","mainEntity":[' + q + ']}'
    article = '{"@type":"Article","headline":"Outage guide","description":"Runs a fridge for 20 hours."}'
    product = '{"@type":"Product","name":"Station for CPAP, 1000Wh","description":"Runs a CPAP for 20 hours."}'

    def page(*docs):
        return "".join(f"<script type='application/ld+json'>{d}</script>" for d in docs)

    ok = faq.replace("ASK", "Ask your doctor.")
    for html in (page("[" + ok + "," + article + "]"),          # after the answer, same document
                 page(ok, article),                             # a later document
                 page(ok, product)):                            # product data is not copy
        a = cpa.audit_html(pol, f"<html><head>{html}</head><body><p>Hi.</p></body></html>")
        assert a.violations == [], [(v.rule_id, v.excerpt) for v in a.violations]
    # the answer itself is still under its question
    a = cpa.audit_html(pol, page(faq.replace("ASK", "About 20 hours.")))
    assert [v.excerpt for v in a.rejects] == ["About 20 hours."]


def test_skip_marker_hides_third_party_data_on_rendered_pages(pol):
    data = ("<h1 data-content-policy='source'>Station 240V L14-30 Outlet, 1000Wh for CPAP, Whole Home Backup</h1>"
            "<table data-content-policy='source'><tr><th>Load</th><th>Hours</th></tr>"
            "<tr><td>CPAP</td><td>25</td></tr><tr><td>Outlet</td><td>NEMA L14-30R adapter</td></tr></table>")
    head = ("<title data-content-policy='source'>CPAP 1000Wh 20 hours</title>"
            "<meta name='description' data-content-policy='source' content='Runs a CPAP for 20 hours'>")
    a = cpa.audit_html(pol, f"<html><head>{head}</head><body>{data}<p>Plug the fridge in.</p></body></html>")
    assert a.violations == [], [(v.rule_id, v.excerpt) for v in a.violations]
    unmarked = data.replace(" data-content-policy='source'", "")
    got = {v.rule_id for v in cpa.audit_html(pol, f"<body>{unmarked}</body>").rejects}
    assert {"medical.medical-runtime-claim", "electrical-year1.240v-cords-headline"} <= got
    unclosed = cpa.audit_html(pol, "<div data-content-policy='source'><p>x</p><p>Backfeed it.</p>")
    assert unclosed.violations == [] and "unclosed" in unclosed.notes[0]


# ── review round 1: never-fetch in sitemap mode and on redirects ───────────

def _clickout_site() -> FakeWeb:
    base = "https://power.example"
    web = _site()
    web.pages[f"{base}/sitemap.xml"] = (
        f"<urlset><url><loc>{base}/guides/good</loc></url><url><loc>{base}/go/B0AAAAAAA1</loc></url>"
        f"<url><loc>{base}/r/B0AAAAAAA2</loc></url><url><loc>{base}/api/outbound-click</loc></url>"
        f"<url><loc>{base}/k/B0AAAAAAA3</loc></url><url><loc>{base}/guides/bad</loc></url></urlset>")
    return web


def test_sitemap_mode_never_fetches_click_outs(pol, capsys, monkeypatch):
    web = _clickout_site()
    assert cpa.sitemap_urls("https://power.example", web) == ["https://power.example/guides/good",
                                                             "https://power.example/guides/bad"]
    web.calls.clear()
    cpa.crawl_check_group("https://power.example", pol, fetch=web, path_re="^/")
    assert not any(seg in u for u in web.calls for seg in ("/r/", "/go/", "/k/", "/api/"))
    web.calls.clear()
    monkeypatch.setattr(scan_cli, "http_fetcher", lambda **kw: web)
    rc, out, err = _cli([*RULE_ARGS, "--no-storage", "--base", "https://power.example", "--sitemap"], capsys)
    assert rc == 1 and not any(seg in u for u in web.calls for seg in ("/r/", "/go/", "/k/", "/api/"))


def test_http_fetcher_refuses_click_outs_and_off_host_redirects():
    import http.server
    import threading

    seen: list[str] = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            seen.append(self.path)
            to = {"/guides/a": "/r/B0AAAAAAA1", "/guides/b": "http://elsewhere.invalid/guides/x",
                  "/guides/c": "/guides/d"}.get(self.path)
            if to:
                self.send_response(302)
                self.send_header("Location", to)
                self.end_headers()
                return
            body = b"<p>page d</p>" if self.path == "/guides/d" else b"<p>other</p>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        fetch = cpa.http_fetcher(timeout=5)
        a, b, c = fetch(f"{base}/guides/a"), fetch(f"{base}/guides/b"), fetch(f"{base}/guides/c")
        direct = fetch(f"{base}/r/B0AAAAAAA9")
    finally:
        srv.shutdown()
    assert a.error and "refused" in a.error and b.error and "refused" in b.error
    assert c.status == 200 and "page d" in c.text and c.final_url.endswith("/guides/d")
    assert direct.error.startswith("refused") and seen == ["/guides/a", "/guides/b", "/guides/c", "/guides/d"]


# ── review round 1: unread pages fail the CLI; --sitemap needs a base ───────

def test_cli_fails_when_pages_could_not_be_read(capsys, monkeypatch):
    web = _site()
    for u in ("https://power.example/guides/good",):
        del web.pages[u]                                         # 404 now
    web.pages["https://power.example/guides/bad"] = _read("rp-guide-allowed.html")
    monkeypatch.setattr(scan_cli, "http_fetcher", lambda **kw: web)
    argv = [*RULE_ARGS, "--no-storage", "--base", "https://power.example", "--sitemap", "--path-re", "^/guides/"]
    rc, _out, err = _cli(argv, capsys)
    assert rc == 3 and "1 page(s) could not be read" in err
    assert _cli([*argv, "--allow-errors"], capsys)[0] == 0
    # --sitemap with --url and no --base: the base is the URL's site root
    rc, _out, err = _cli([*RULE_ARGS, "--no-storage", "--sitemap", "--url", "https://power.example/guides/bad",
                          "--path-re", "^/guides/", "--allow-errors"], capsys)
    assert rc == 0 and "scanned" in err
    assert _cli([*RULE_ARGS, "--no-storage", "--sitemap", "--url", "ftp://power.example/x"], capsys)[0] == 2
    assert _cli([*RULE_ARGS, "--no-storage", "--url", "https://p.example/x", "--path-re", "("], capsys)[0] == 2


def test_site_check_group_reports_config_trouble_and_retunes_severity(pol, tmp_path):
    from framework.core.storage import LocalFilesystemStorage

    class Down(LocalFilesystemStorage):
        def read_bytes(self, key):
            raise OSError("storage unreachable")
    fs = cpa.site_check_group("https://power.example", "powersite", storage=Down(str(tmp_path)), fetch=_site())
    assert [f["kind"] for f in fs] == [cpa.KIND_UNAVAILABLE] and "unavailable" in fs[0]["message"]
    assert cpa.site_check_group("https://power.example", "no-such-site", config=cp.load_config(), fetch=_site()) == []
    cfg = cp.merge_config(cp.load_config(), {"sites": {"powersite": {"rule_sets": ALL_SETS}}})
    fs = cpa.site_check_group("https://power.example", "powersite", config=cfg, fetch=_site())
    assert {f["url"] for f in fs} == {"https://power.example/guides/bad"}
    empty = cpa.crawl_check_group("https://empty.example", pol, fetch=FakeWeb({}),
                                  severity={cpa.KIND_UNAVAILABLE: "error"})
    assert [(f["kind"], f["severity"]) for f in empty] == [(cpa.KIND_UNAVAILABLE, "error")]


# ── review round 3: the publish gate and the rendered scan agree ────────────

def _kit_page(title: str, body_html: str, *, description: str = "", faq: tuple = ()) -> str:
    """A page shaped like the kit's SSR layout: the article title as the
    page's one <h1>, the disclosure right after it, the body, then the FAQ
    both visible and as FAQPage JSON-LD."""
    import html as _h

    ld = json.dumps({"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": [
        {"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}} for q, a in faq]})
    head = (f"<title>{_h.escape(title)} | Example Power Picks</title>"
            f"<meta name=\"description\" content=\"{_h.escape(description, quote=True)}\">"
            + (f"<script type=\"application/ld+json\">{ld}</script>" if faq else ""))
    visible = ("<h2>FAQ</h2>" + "".join(f"<h3>{_h.escape(q)}</h3><p>{_h.escape(a)}</p>" for q, a in faq)
               if faq else "")
    return (f"<!doctype html><html><head>{head}</head><body><main><article><h1>{_h.escape(title)}</h1>"
            "<p class=\"affiliate-disclosure\">As an Amazon Associate we earn from qualifying purchases.</p>"
            f"{body_html}{visible}</article></main></body></html>")


PARITY = [
    # (id, title, body_md, body_html, excerpt, faqs, passes)
    ("cpap-title-over-a-neutral-h2", "Best Power Stations for CPAP Machines (2026)",
     "Sleep apnea doesn't take a night off when the grid does.\n\n## Our pick: EcoFlow Delta 2\n\n"
     "The Delta 2 gives you about two full days at 40 W with the humidifier off.\n",
     "<p>Sleep apnea doesn't take a night off when the grid does.</p><h2>Our pick: EcoFlow Delta 2</h2>"
     "<p>The Delta 2 gives you about two full days at 40 W with the humidifier off.</p>", "", (), False),
    ("cpap-title-into-the-excerpt", "Best Power Station for a CPAP Machine",
     "Plan medical backup with your doctor or equipment supplier.\n",
     "<p>Plan medical backup with your doctor or equipment supplier.</p>",
     "Which stations last two nights with the humidifier off.", (), False),
    ("cpap-title-into-an-faq-answer", "Best Power Station for a CPAP Machine",
     "Plan medical backup with your doctor or equipment supplier.\n",
     "<p>Plan medical backup with your doctor or equipment supplier.</p>", "",
     (("Which station should I buy?", "The Delta 2 lasts two nights."),), False),
    ("cpap-title-planning-only", "Power Outage Planning for CPAP Users",
     "Plan your backup power with your doctor or equipment supplier.\n\nWe don't publish runtimes for CPAP "
     "machines.\n",
     "<p>Plan your backup power with your doctor or equipment supplier.</p><p>We don't publish runtimes for CPAP "
     "machines.</p>", "What to ask your doctor and your utility before storm season.", (), True),
    ("freezer-title-runtime-is-fine", "Best Power Station for a Chest Freezer",
     "## Our pick\n\nThe Delta 2 runs a chest freezer for about 30 hours.\n",
     "<h2>Our pick</h2><p>The Delta 2 runs a chest freezer for about 30 hours.</p>",
     "Which stations keep a chest freezer cold for two days.", (("How long?", "About 30 hours."),), True),
]


@pytest.mark.parametrize("case", PARITY, ids=[c[0] for c in PARITY])
def test_check_article_and_the_rendered_page_give_the_same_verdict(pol, case):
    _id, title, body_md, body_html, excerpt, faq, passes = case
    gate = cp.check_article(pol, title=title, body_md=body_md, excerpt=excerpt,
                            faqs=[{"question": q, "answer": a} for q, a in faq])
    page = cpa.audit_html(pol, _kit_page(title, body_html, description=excerpt, faq=faq))
    assert gate.passes is passes, [(v.rule_id, v.excerpt) for v in gate.violations]
    assert page.passes is passes, [(v.rule_id, v.excerpt) for v in page.violations]
    assert {v.rule_id for v in gate.rejects} == {v.rule_id for v in page.rejects}


def test_the_h1_carries_into_the_meta_description_and_json_ld(pol):
    ld = ('<script type="application/ld+json">{"@type":"FAQPage","mainEntity":[{"@type":"Question",'
          '"name":"Which station?","acceptedAnswer":{"@type":"Answer","text":"The Delta 2 lasts two nights."}}]}'
          '</script>')
    meta = '<meta name="description" content="Stations that last two nights.">'
    for head, excerpt in ((meta, "Stations that last two nights."), (ld, "The Delta 2 lasts two nights.")):
        with_h1 = cpa.audit_html(pol, f"<html><head>{head}</head><body><h1>Best Station for a CPAP</h1>"
                                      "<p>Talk to your doctor.</p></body></html>")
        assert [v.excerpt for v in with_h1.rejects] == [excerpt]
        other_h1 = cpa.audit_html(pol, f"<html><head>{head}</head><body><h1>Best Station for a Freezer</h1>"
                                       "<p>Talk to your doctor.</p></body></html>")
        assert other_h1.passes, [(v.rule_id, v.excerpt) for v in other_h1.violations]


def test_rendered_concessions_fail_the_scan(pol):
    html = ("<html><body><h1>Generator Safety</h1><p>Never run a gas generator in the garage. If you have to, keep "
            "the big door wide open.</p><p>Backfeeding is dangerous. Here's how to do it as safely as possible:</p>"
            "<ol><li>Shut off the main breaker.</li><li>Connect the cord.</li></ol></body></html>")
    got = {v.rule_id for v in cpa.audit_html(pol, html).rejects}
    assert got == {"electrical.indoor-generator", "electrical.backfeed"}
