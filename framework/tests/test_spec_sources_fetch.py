"""framework.core.spec_sources: robots.txt (RFC 9309) and the polite
fetcher. Real robots.txt files from manufacturer hosts are parsed from
fixtures; every HTTP exchange goes through a fake transport with a fake
clock, so nothing touches the network and nothing sleeps."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from framework.core import spec_sources as ss

FX = Path(__file__).resolve().parent / "fixtures" / "spec_sources"
TOKEN = "SpecSourcesBot"


def robots(name: str) -> ss.RobotsRules:
    return ss.RobotsRules.parse((FX / "robots" / name).read_text())


# ---------------------------------------------------------------------------
# robots.txt parsing on real files
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path,allowed", [
    ("/products/jackery-explorer-1000-v2", True),
    ("/blogs/jackery-faqs/explorer-1000-v2-basic-information", True),
    ("/cart/", False),
    ("/checkouts/abc", False),
    ("/collections/power?sort_by=price", False),     # Disallow: /collections/*sort_by*
    ("/collections/a+b", False),                     # Disallow: /collections/*+*
    ("/collections/a%2Bb", False),                   # percent-encoded form of the same
    ("/zh/products/x", False),                       # an indented "    Disallow: /zh/"
    ("/account", False),
    ("/account/login", True),                        # longer Allow beats Disallow: /account
    ("/products/x?q=solar", False),                  # Disallow: /*?q=*
    ("/robots.txt", True),
])
def test_shopify_store_robots(path, allowed):
    assert robots("jackery.com.robots.txt").can_fetch("https://www.jackery.com" + path, TOKEN) is allowed


def test_manual_mirror_robots_groups_and_crawl_delay():
    r = robots("manuals.plus.robots.txt")
    base = "https://manuals.plus"
    # our token has no group of its own -> the '*' group
    assert r.can_fetch(base + "/pecron/e2000lfp-expandable-portable-power-station-manual", TOKEN)
    assert not r.can_fetch(base + "/redirect?to=x", TOKEN)
    assert r.crawl_delay(TOKEN) is None
    # a multi-agent group: every listed agent is banned site-wide
    assert not r.can_fetch(base + "/anything", "SemrushBot-BA")
    assert not r.can_fetch(base + "/anything", "ccbot")              # case-insensitive
    # Yandex: own group with Crawl-Delay and $-anchored rules
    assert r.crawl_delay("YandexBot") == 2.0
    assert not r.can_fetch(base + "/asin", "YandexBot")              # Disallow: /asin$
    assert not r.can_fetch(base + "/asin?x=1", "YandexBot")          # Disallow: /asin?
    assert r.can_fetch(base + "/asinfoo", "YandexBot")
    # a specific group does not inherit '*': GPTBot may fetch /redirect
    assert r.can_fetch(base + "/redirect", "GPTBot")
    assert not r.can_fetch(base + "/xmlrpc.php", "GPTBot")
    assert r.crawl_delay("GPTBot") is None                            # "#Crawl-Delay: 2" is a comment


@pytest.mark.parametrize("path,allowed", [
    ("/sg/deebot-robotic-vacuum-cleaner/deebot-n10", True),
    ("/sg/whatever?page=2", False),                                   # Disallow: *?page=*
    ("/sg/deebot-robotic-vacuum-cleaner?page=2", True),               # longer Allow wins
    ("/us/surprise/deal", False),                                     # "Disallow:/*surprise/" (no space)
    ("/sg/static/app.js", True),                                      # "Allow:  */static/*"
    ("/us/product-detail?id=55", False),                              # longest: Disallow */product-detail?id=*
    ("/us/faq-detail?id=3&product_id=9", True),                      # Allow *faq-detail?id=*&product_id=*
])
def test_ecovacs_robots_longest_match(path, allowed):
    assert robots("ecovacs.com.robots.txt").can_fetch("https://www.ecovacs.com" + path, TOKEN) is allowed


def test_store_json_endpoints_disallowed():
    r = robots("eufy.com.robots.txt")
    assert not r.can_fetch("https://www.eufy.com/products/t2108124.json", TOKEN)
    assert r.can_fetch("https://www.eufy.com/products/t2108124", TOKEN)


def test_manual_cdn_allows_pdfs():
    r = robots("cdn.shopify.com.robots.txt")
    assert r.can_fetch("https://cdn.shopify.com/s/files/1/0626/0241/2120/files/pecron_E2000LFP_-20230227.pdf?v=1", TOKEN)
    assert not r.can_fetch("https://cdn.shopify.com/wpm/abc.js", TOKEN)


def test_rfc9309_details():
    r = ss.RobotsRules.parse("User-agent: *\nDisallow: /a\nAllow: /a\nDisallow: /p$\nDisallow:\n")
    assert r.can_fetch("https://h.example/a/x", TOKEN)        # equal length: Allow wins
    assert not r.can_fetch("https://h.example/p", TOKEN)
    assert r.can_fetch("https://h.example/p/q", TOKEN)        # '$' anchors the end
    assert ss.RobotsRules.parse("User-agent: other\nDisallow: /\n").can_fetch("https://h.example/x", TOKEN)
    assert ss.RobotsRules.parse("").can_fetch("https://h.example/x", TOKEN)
    two = ss.RobotsRules.parse(f"User-agent: {TOKEN}\nDisallow: /one\n\nUser-agent: {TOKEN.lower()}/2.0\nDisallow: /two\n")
    assert not two.can_fetch("https://h.example/one", TOKEN)  # matching groups are merged
    assert not two.can_fetch("https://h.example/two", TOKEN)
    assert ss.RobotsRules.allow_all().can_fetch("https://h.example/x", TOKEN)
    assert not ss.RobotsRules.disallow_all().can_fetch("https://h.example/x", TOKEN)


# ---------------------------------------------------------------------------
# Fetcher harness
# ---------------------------------------------------------------------------

class Clock:
    def __init__(self):
        self.t = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
        self.mono = 1000.0
        self.slept: list[float] = []

    def now(self):
        return self.t

    def monotonic(self):
        return self.mono

    def sleep(self, s):
        self.slept.append(s)
        self.mono += s

    def advance(self, seconds):
        self.t += timedelta(seconds=seconds)
        self.mono += seconds


def resp(status=200, body=b"", ctype="text/html; charset=utf-8", **headers):
    h = {"content-type": ctype} if ctype else {}
    h.update({k.replace("_", "-"): v for k, v in headers.items()})
    return ss.HttpResponse(status, h, body if isinstance(body, bytes) else body.encode(), "")


class FakeTransport:
    """url -> response | [responses served in order, last one repeats] |
    callable(url, headers) -> response. Unknown URLs get a 404."""

    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, headers, timeout_s, max_bytes):
        self.calls.append((url, dict(headers)))
        r = self.routes.get(url)
        if r is None:
            return ss.HttpResponse(404, {}, b"", url)
        if isinstance(r, list):
            r = r.pop(0) if len(r) > 1 else r[0]
        if callable(r):
            r = r(url, headers)
        r = replace(r, url=url)
        if len(r.body) > max_bytes:
            return replace(r, body=r.body[:max_bytes], truncated=True)
        return r

    def urls(self):
        return [u for u, _ in self.calls]


HOST = "https://www.maker.example"
PAGE = HOST + "/products/station-1000"
ROBOTS = HOST + "/robots.txt"
HTML = "<html><body><h1>Station 1000</h1><table><tr><td>Capacity</td><td>1000Wh</td></tr></table></body></html>"


def fetcher(tmp_path, transport, clock=None, **cfg):
    clock = clock or Clock()
    base = {"min_interval_s": 0.0}
    base.update(cfg)
    f = ss.PoliteFetcher(base, transport=transport, cache_dir=tmp_path / "cache",
                         clock=clock.now, monotonic=clock.monotonic, sleep=clock.sleep)
    return f, clock


def robots_ok(text="User-agent: *\nDisallow: /private\n"):
    return resp(200, text, "text/plain")


# ---------------------------------------------------------------------------
# robots handling in the fetcher
# ---------------------------------------------------------------------------

def test_fetch_obeys_robots_and_never_requests_a_disallowed_page(tmp_path):
    t = FakeTransport({ROBOTS: robots_ok(), HOST + "/private/manual.pdf": resp(200, b"%PDF-1.4")})
    f, _ = fetcher(tmp_path, t)
    r = f.fetch(HOST + "/private/manual.pdf")
    assert r.status == "robots-disallowed" and not r.ok
    assert t.urls() == [ROBOTS]


@pytest.mark.parametrize("code,expect", [(404, "fetched"), (403, "fetched"), (410, "fetched"),
                                         (500, "robots-disallowed"), (503, "robots-disallowed"),
                                         (429, "robots-disallowed")])
def test_robots_status_semantics(tmp_path, code, expect):
    # RFC 9309: 4xx = unavailable (allow all); 5xx and 429 = unreachable (disallow all).
    t = FakeTransport({ROBOTS: resp(code, "", "text/plain"), PAGE: resp(200, HTML)})
    f, _ = fetcher(tmp_path, t)
    assert f.fetch(PAGE).status == expect


def test_robots_network_error_disallows(tmp_path):
    t = FakeTransport({ROBOTS: lambda u, h: ss.HttpResponse(0, {}, b"", u, error="TimeoutError: timed out"),
                       PAGE: resp(200, HTML)})
    f, _ = fetcher(tmp_path, t)
    r = f.fetch(PAGE)
    assert r.status == "robots-disallowed" and "unreachable" in r.reason
    assert PAGE not in t.urls()


def test_robots_cached_on_disk_for_ttl_then_refetched(tmp_path):
    t = FakeTransport({ROBOTS: robots_ok(), PAGE: resp(200, HTML)})
    f, clock = fetcher(tmp_path, t)
    f.fetch(PAGE)
    # a new fetcher (next run) on the same cache dir: robots.txt not requested again
    f2 = ss.PoliteFetcher({"min_interval_s": 0.0}, transport=t, cache_dir=tmp_path / "cache",
                          clock=clock.now, monotonic=clock.monotonic, sleep=clock.sleep)
    f2.fetch(PAGE, force=True)
    assert t.urls().count(ROBOTS) == 1
    clock.advance(25 * 3600)
    f3 = ss.PoliteFetcher({"min_interval_s": 0.0}, transport=t, cache_dir=tmp_path / "cache",
                          clock=clock.now, monotonic=clock.monotonic, sleep=clock.sleep)
    f3.fetch(PAGE, force=True)
    assert t.urls().count(ROBOTS) == 2


def test_unreachable_robots_falls_back_to_a_recent_copy(tmp_path):
    t = FakeTransport({ROBOTS: [robots_ok(), resp(503, "", "text/plain")], PAGE: resp(200, HTML)})
    f, clock = fetcher(tmp_path, t)
    assert f.fetch(PAGE).status == "fetched"
    clock.advance(48 * 3600)                     # past robots_ttl_h, within robots_stale_ok_h
    f2 = ss.PoliteFetcher({"min_interval_s": 0.0}, transport=t, cache_dir=tmp_path / "cache",
                          clock=clock.now, monotonic=clock.monotonic, sleep=clock.sleep)
    assert f2.fetch(PAGE, force=True).status in ("unchanged", "fetched")
    assert f2.fetch(HOST + "/private/x").status == "robots-disallowed"


def test_crawl_delay_is_honoured_and_an_excessive_one_defers(tmp_path):
    t = FakeTransport({ROBOTS: robots_ok("User-agent: *\nCrawl-delay: 30\n"), PAGE: resp(200, HTML),
                       HOST + "/p2": resp(200, HTML + " ")})
    f, clock = fetcher(tmp_path, t, min_interval_s=5.0)
    f.fetch(PAGE)
    f.fetch(HOST + "/p2")
    assert clock.slept[-1] == pytest.approx(30.0)
    t2 = FakeTransport({ROBOTS: robots_ok("User-agent: *\nCrawl-delay: 600\n"), PAGE: resp(200, HTML)})
    f2, _ = fetcher(tmp_path / "b", t2)
    r = f2.fetch(PAGE)
    assert r.status == "deferred" and "crawl-delay" in r.reason
    assert PAGE not in t2.urls()


# ---------------------------------------------------------------------------
# rate limit, conditional GET, hash short-circuit, cache
# ---------------------------------------------------------------------------

def test_per_host_rate_limit(tmp_path):
    other = "https://cdn.files.example"
    t = FakeTransport({ROBOTS: robots_ok(), PAGE: resp(200, HTML), HOST + "/p2": resp(200, HTML + "!"),
                       other + "/robots.txt": resp(404), other + "/m.pdf": resp(200, b"%PDF-1.4 x", "application/pdf")})
    f, clock = fetcher(tmp_path, t, min_interval_s=10.0, host_min_interval_s={"cdn.files.example": 2.0})
    f.fetch(PAGE)                  # robots.txt, then a 10 s wait, then the page
    assert clock.slept == [pytest.approx(10.0)]
    f.fetch(HOST + "/p2")          # same host: another 10 s
    assert clock.slept[-1] == pytest.approx(10.0)
    n = len(clock.slept)
    f.fetch(other + "/m.pdf")      # other host: only its own 2 s robots->file gap
    assert clock.slept[n:] == [pytest.approx(2.0)]


def test_host_busy_beyond_max_wait_defers(tmp_path):
    t = FakeTransport({ROBOTS: robots_ok(), PAGE: resp(200, HTML)})
    f, clock = fetcher(tmp_path, t, min_interval_s=600.0, max_wait_s=60.0)
    r = f.fetch(PAGE)              # robots.txt used the slot; the page would wait 600 s
    assert r.status == "deferred" and PAGE not in t.urls()
    assert clock.slept == []


def test_conditional_get_uses_etag_and_304_keeps_the_body(tmp_path):
    seen = []

    def page(url, headers):
        seen.append(headers.get("If-None-Match"))
        if headers.get("If-None-Match") == '"v1"':
            return resp(304, "", None)
        return resp(200, HTML, etag='"v1"', last_modified="Tue, 17 Nov 2026 10:00:00 GMT")

    t = FakeTransport({ROBOTS: robots_ok(), PAGE: page})
    f, clock = fetcher(tmp_path, t)
    first = f.fetch(PAGE)
    assert first.status == "fetched" and first.changed and first.etag == '"v1"'
    clock.advance(3600)
    second = f.fetch(PAGE, force=True)
    assert seen == [None, '"v1"']
    assert second.status == "not-modified" and not second.changed
    assert second.read_body() == HTML.encode()
    assert second.content_sha256 == first.content_sha256
    assert second.fetched_at == first.fetched_at and second.checked_at > first.checked_at


def test_content_hash_short_circuit_without_etag(tmp_path):
    t = FakeTransport({ROBOTS: robots_ok(), PAGE: [resp(200, HTML), resp(200, HTML), resp(200, HTML + "<p>v2</p>")]})
    f, _ = fetcher(tmp_path, t)
    a = f.fetch(PAGE)
    b = f.fetch(PAGE, force=True)
    c = f.fetch(PAGE, force=True)
    assert (a.status, a.changed) == ("fetched", True)
    assert (b.status, b.changed) == ("unchanged", False) and b.content_sha256 == a.content_sha256
    assert (c.status, c.changed) == ("fetched", True) and c.content_sha256 != a.content_sha256


def test_prior_record_from_a_database_row_short_circuits(tmp_path):
    t = FakeTransport({ROBOTS: robots_ok(), PAGE: resp(200, HTML)})
    f, _ = fetcher(tmp_path, t)
    prior = f.fetch(PAGE).to_record()
    fresh, _ = fetcher(tmp_path / "other-cache", FakeTransport({ROBOTS: robots_ok(), PAGE: resp(200, HTML)}))
    r = fresh.fetch(PAGE, prior=prior)
    assert r.status == "unchanged" and not r.changed


def test_validators_from_a_database_row_work_without_a_local_cache(tmp_path):
    # e.g. a new fleet host: the caller still has the spec_sources row (etag + sha)
    def page(url, headers):
        return resp(304, "", None) if headers.get("If-None-Match") == '"v7"' else resp(200, HTML, etag='"v7"')

    prior = {"url": PAGE, "final_url": PAGE, "etag": '"v7"', "content_sha256": "9" * 64,
             "fetched_at": "2026-11-10T00:00:00Z", "checked_at": "2026-11-16T00:00:00Z"}
    t = FakeTransport({ROBOTS: robots_ok(), PAGE: page})
    f, _ = fetcher(tmp_path, t)
    r = f.fetch(PAGE, prior=prior)
    assert r.status == "not-modified" and r.unchanged and not r.ok
    assert r.content_sha256 == "9" * 64 and r.fetched_at == "2026-11-10T00:00:00Z"
    assert t.calls[-1][1]["If-None-Match"] == '"v7"'


def test_unchanged_flag_for_the_callers_short_circuit(tmp_path):
    t = FakeTransport({ROBOTS: robots_ok(), PAGE: resp(200, HTML)})
    f, _ = fetcher(tmp_path, t)
    first = f.fetch(PAGE)
    assert first.ok and not first.unchanged
    again = f.fetch(PAGE, force=True)
    assert again.ok and again.unchanged


def test_pdf_served_as_octet_stream_is_sniffed(tmp_path):
    pdf = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n"
    t = FakeTransport({ROBOTS: robots_ok(), HOST + "/manual.pdf": resp(200, pdf, "binary/octet-stream"),
                       HOST + "/blob.bin": resp(200, b"\x00\x01\x02zip", "application/octet-stream")})
    f, _ = fetcher(tmp_path, t)
    r = f.fetch(HOST + "/manual.pdf")
    assert r.status == "fetched" and r.content_type == "application/pdf"
    assert f.fetch(HOST + "/blob.bin").status == "unsupported-type"


def test_a_raising_transport_becomes_a_network_error(tmp_path):
    def broken(url, headers):
        raise ConnectionResetError("peer reset")

    t = FakeTransport({ROBOTS: robots_ok(), PAGE: broken})
    f, _ = fetcher(tmp_path, t)
    r = f.fetch(PAGE)
    assert r.status == "error" and "ConnectionResetError" in r.reason


def test_robots_redirect_into_a_refused_host_disallows(tmp_path):
    t = FakeTransport({ROBOTS: resp(301, "", None, location="https://www.amazon.com/robots.txt"),
                       PAGE: resp(200, HTML)})
    f, _ = fetcher(tmp_path, t)
    r = f.fetch(PAGE)
    assert r.status == "robots-disallowed" and r.reason.startswith("robots-redirect-refused:amazon")
    assert t.urls() == [ROBOTS]


def test_recent_check_is_served_from_cache_without_a_request(tmp_path):
    t = FakeTransport({ROBOTS: robots_ok(), PAGE: resp(200, HTML)})
    f, clock = fetcher(tmp_path, t, min_refetch_h=20)
    f.fetch(PAGE)
    clock.advance(3600)
    n = len(t.calls)
    r = f.fetch(PAGE)
    assert r.status == "cached" and r.ok and len(t.calls) == n
    assert r.read_body() == HTML.encode()


def test_failed_refetch_never_discards_the_cache(tmp_path):
    t = FakeTransport({ROBOTS: robots_ok(), PAGE: [resp(200, HTML), resp(500, "oops")]})
    f, _ = fetcher(tmp_path, t)
    good = f.fetch(PAGE)
    bad = f.fetch(PAGE, force=True)
    assert bad.status == "http-error" and bad.reason == "http-500"
    rec = f.index_record(PAGE)
    assert rec["content_sha256"] == good.content_sha256
    assert Path(good.body_path).read_bytes() == HTML.encode()


def test_redirects_are_followed_and_recorded(tmp_path):
    t = FakeTransport({ROBOTS: robots_ok(), PAGE: resp(301, "", None, location="/products/station-1000-v2"),
                       HOST + "/products/station-1000-v2": resp(200, HTML)})
    f, _ = fetcher(tmp_path, t)
    r = f.fetch(PAGE)
    assert r.status == "fetched" and r.final_url == HOST + "/products/station-1000-v2"
    assert r.redirects == (HOST + "/products/station-1000-v2",)


def test_size_type_and_scheme_limits(tmp_path):
    t = FakeTransport({ROBOTS: robots_ok(), PAGE: resp(200, b"x" * 2000),
                       HOST + "/img.png": resp(200, b"\x89PNG", "image/png")})
    f, _ = fetcher(tmp_path, t, max_bytes=1000)
    assert f.fetch(PAGE).status == "too-large"
    assert f.fetch(HOST + "/img.png").status == "unsupported-type"
    assert f.fetch("ftp://files.maker.example/manual.pdf").status == "refused"


def test_identifies_itself(tmp_path):
    t = FakeTransport({ROBOTS: robots_ok(), PAGE: resp(200, HTML)})
    f, _ = fetcher(tmp_path, t, user_agent="Mozilla/5.0 (compatible; SpecSourcesBot/1.0; +https://site.example/bot)")
    f.fetch(PAGE)
    assert all("SpecSourcesBot" in h["User-Agent"] for _, h in t.calls)


# ---------------------------------------------------------------------------
# Amazon is refused before any request
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "https://www.amazon.com/dp/B0D91KV51C",
    "https://amazon.co.uk/Some-Item/dp/B0D91KV51C/ref=x",
    "https://smile.amazon.com/gp/product/B0D91KV51C",
    "https://amzn.to/3abcdef",
    "https://a.co/d/abc123",
    "https://m.media-amazon.com/images/I/71abc.jpg",
    "https://images-na.ssl-images-amazon.com/images/I/71abc.jpg",
    "https://manuals.plus/asin/B0D91KV51C",            # an Amazon-listing mirror on another host
    "https://shop.example/dp/B0D91KV51C",
])
def test_amazon_urls_are_refused_without_a_request(tmp_path, url):
    t = FakeTransport()
    f, _ = fetcher(tmp_path, t)
    r = f.fetch(url)
    assert r.status == "refused" and r.reason.startswith("amazon")
    assert t.calls == []


@pytest.mark.parametrize("url", [
    "https://mybucket.s3.amazonaws.com/manuals/station.pdf",   # manufacturers host manuals on AWS
    "https://d1abc.cloudfront.net/manual.pdf",
    "https://www.amazonia-outdoor.example/specs",
])
def test_aws_hosting_is_not_amazon_content(url):
    assert not ss.is_amazon_url(url)


def test_redirect_into_amazon_is_refused_and_not_followed(tmp_path):
    t = FakeTransport({ROBOTS: robots_ok(), PAGE: resp(302, "", None, location="https://www.amazon.com/dp/B0D91KV51C")})
    f, _ = fetcher(tmp_path, t)
    r = f.fetch(PAGE)
    assert r.status == "refused" and r.reason.startswith("redirect-amazon")
    assert all("amazon.com" not in u for u in t.urls())


def test_amazon_page_served_from_another_host_is_refused_and_not_cached(tmp_path):
    page = (FX / "amazon" / "synthetic-amazon-detail-page.html").read_text()
    t = FakeTransport({ROBOTS: robots_ok(), PAGE: resp(200, page)})
    f, _ = fetcher(tmp_path, t)
    r = f.fetch(PAGE)
    assert r.status == "refused" and r.reason == "amazon-canonical"
    assert f.index_record(PAGE) is None
    assert not (tmp_path / "cache" / "objects").exists()


def test_fetch_result_feeds_a_document(tmp_path):
    t = FakeTransport({ROBOTS: robots_ok(), PAGE: resp(200, HTML)})
    f, _ = fetcher(tmp_path, t)
    doc = ss.document_from_fetch(f.fetch(PAGE), kind="manufacturer-page")
    assert doc.url == PAGE and doc.content_sha256 == f.index_record(PAGE)["content_sha256"]
    assert ("Capacity", "1000Wh") in [(p.label, p.value) for p in doc.pairs]
    with pytest.raises(ss.SpecSourcesError):
        ss.document_from_fetch(ss.FetchResult(PAGE, "robots-disallowed"), kind="manufacturer-page")


def test_index_records_are_plain_json(tmp_path):
    t = FakeTransport({ROBOTS: robots_ok(), PAGE: resp(200, HTML, etag='"e"')})
    f, _ = fetcher(tmp_path, t)
    f.fetch(PAGE)
    files = list((tmp_path / "cache" / "index").rglob("*.json"))
    assert len(files) == 1
    rec = json.loads(files[0].read_text())
    assert rec["url"] == PAGE and rec["etag"] == '"e"' and len(rec["content_sha256"]) == 64


# ---------------------------------------------------------------------------
# Review round 1: robots.txt BOM and wildcard cost
# ---------------------------------------------------------------------------

def test_robots_with_a_byte_order_mark_keeps_its_first_group(tmp_path):
    rules = ss.RobotsRules.parse("﻿User-agent: *\nDisallow: /private\n")
    assert not rules.can_fetch("https://h.example/private/x", TOKEN)
    t = FakeTransport({ROBOTS: resp(200, b"\xef\xbb\xbfUser-agent: *\nDisallow: /private\n", "text/plain"),
                       HOST + "/private/manual.html": resp(200, HTML)})
    f, _ = fetcher(tmp_path, t)
    r = f.fetch(HOST + "/private/manual.html")
    assert r.status == "robots-disallowed"
    assert t.urls() == [ROBOTS]


def test_hostile_wildcards_cannot_stall_a_check():
    import time as _time
    rules = ss.RobotsRules.parse("User-agent: *\nDisallow: /" + "*a" * 40 + "*z\nAllow: /*b*$\n")
    t0 = _time.perf_counter()
    for _ in range(50):
        assert rules.can_fetch("https://h.example/" + "a" * 200, TOKEN)
    assert _time.perf_counter() - t0 < 0.5
    assert not rules.can_fetch("https://h.example/" + "a" * 60 + "z", TOKEN)


def _regex_reference(pattern: str, target: str) -> bool:
    """The previous regex implementation, as the reference for matching."""
    import re as _re
    p = ss._robots_decode(pattern)
    if not p.startswith(("/", "*")):
        p = "/" + p
    anchored = p.endswith("$")
    body = p[:-1] if anchored else p
    rx = "".join(".*" if ch == "*" else _re.escape(ch) for ch in body)
    return bool(_re.compile(rx + (r"\Z" if anchored else ""), _re.S).match(target))


@pytest.mark.parametrize("pattern", ["/", "/a", "/a$", "/a*", "/a*$", "*.pdf$", "/*.pdf", "/*?q=*",
                                     "/a*b*c", "/a*b*c$", "*", "*$", "/p$", "/collections/*sort_by*",
                                     "/*/x*y$", "a", "/a**b", "/%7Euser"])
@pytest.mark.parametrize("target", ["/", "/a", "/ab", "/abc", "/a/b/c", "/x.pdf", "/x.pdf?v=1",
                                    "/p?q=1", "/acb", "/aXbYc", "/aXbYcZ", "/collections/a?sort_by=x",
                                    "/k/xQy", "/~user", "/aab"])
def test_linear_matcher_agrees_with_the_regex_semantics(pattern, target):
    assert ss._RobotsPattern(pattern).matches(target) is _regex_reference(pattern, target)


# ---------------------------------------------------------------------------
# Review round 1: content identity, cache eviction, need_body
# ---------------------------------------------------------------------------

def _volatile(n: int) -> str:
    # A store page whose bytes differ per request only inside scripts.
    return (f"<html><head><script>var __st={{reqid:'{n:08x}-req'}};</script></head><body>"
            f"<main><h1>Station 1000</h1><table><tr><td>Capacity</td><td>1000Wh</td></tr></table></main>"
            f"<script>window.requestId='{n * 7919}';</script></body></html>")


def _objects(tmp_path):
    root = tmp_path / "cache" / "objects"
    return sorted(p for p in root.rglob("*") if p.is_file()) if root.exists() else []


def test_per_request_noise_is_not_a_change(tmp_path):
    t = FakeTransport({ROBOTS: robots_ok(), PAGE: [resp(200, _volatile(1)), resp(200, _volatile(2)),
                                                   resp(200, _volatile(3).replace("1000Wh", "1024Wh"))]})
    f, _ = fetcher(tmp_path, t)
    a = f.fetch(PAGE)
    b = f.fetch(PAGE, force=True)
    assert (a.status, b.status) == ("fetched", "unchanged") and b.unchanged and not b.changed
    assert b.content_sha256 == a.content_sha256 and b.body_sha256 != a.body_sha256
    assert b.fetched_at == a.fetched_at
    doc = ss.document_from_fetch(b, kind="manufacturer-page")
    assert doc.content_sha256 == a.content_sha256 and "1000Wh" in doc.text
    c = f.fetch(PAGE, force=True)
    assert c.status == "fetched" and c.content_sha256 != a.content_sha256
    # one body per URL: the superseded bodies are gone
    assert _objects(tmp_path) == [Path(c.body_path)]


def test_pdf_identity_is_the_byte_hash(tmp_path):
    import hashlib
    pdf = b"%PDF-1.4\n% manual\n"
    t = FakeTransport({ROBOTS: robots_ok(), HOST + "/m.pdf": resp(200, pdf, "application/pdf")})
    f, _ = fetcher(tmp_path, t)
    r = f.fetch(HOST + "/m.pdf")
    assert r.content_sha256 == r.body_sha256 == hashlib.sha256(pdf).hexdigest()


def test_changed_fetches_leave_one_object_and_prune_bounds_the_cache(tmp_path):
    pages = {HOST + f"/p{i}": [resp(200, HTML.replace("1000Wh", f"{1000 + i}Wh")),
                               resp(200, HTML.replace("1000Wh", f"{2000 + i}Wh"))] for i in range(3)}
    t = FakeTransport({ROBOTS: robots_ok(), **pages})
    f, clock = fetcher(tmp_path, t)
    for u in pages:
        f.fetch(u)
        clock.advance(60)
    for u in pages:
        f.fetch(u, force=True)
        clock.advance(60)
    assert len(_objects(tmp_path)) == 3
    # an orphan (e.g. left by a crash between object and index writes) and a stale temp file
    orphan = tmp_path / "cache" / "objects" / "zz" / ("0" * 64 + "-" + "1" * 64)
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b"x" * 10)
    (tmp_path / "cache" / "index" / ".tmp-crash").write_bytes(b"{")
    f2, _ = fetcher(tmp_path, t, clock, orphan_grace_s=0, max_cache_bytes=2 * len(HTML))
    stats = f2.prune_cache(now=clock.now())
    assert not orphan.exists() and not (tmp_path / "cache" / "index" / ".tmp-crash").exists()
    # over the size cap: the least recently checked URL loses its body, keeps its record
    left = _objects(tmp_path)
    assert len(left) == 2 and stats["objects_deleted"] >= 2
    rec = f2.index_record(HOST + "/p0")
    assert rec is not None and not any(str(p).endswith(rec["body_sha256"]) for p in left)
    n = len(t.calls)
    r = f2.fetch(HOST + "/p0")                    # no body: an unconditional GET refills it
    assert r.ok and len(t.calls) > n and "If-None-Match" not in t.calls[-1][1]


def test_records_unchecked_for_months_are_dropped(tmp_path):
    t = FakeTransport({ROBOTS: robots_ok(), PAGE: resp(200, HTML)})
    f, clock = fetcher(tmp_path, t)
    f.fetch(PAGE)
    clock.advance(200 * 86400)
    f2, _ = fetcher(tmp_path, t, clock)
    stats = f2.prune_cache(now=clock.now())
    assert stats["records_dropped"] == 1 and f2.index_record(PAGE) is None and _objects(tmp_path) == []
    assert stats["robots_deleted"] == 1


def test_prune_runs_automatically_at_most_once_a_day(tmp_path):
    t = FakeTransport({ROBOTS: robots_ok(), PAGE: resp(200, HTML), HOST + "/b": resp(200, HTML + " ")})
    f, clock = fetcher(tmp_path, t)
    f.fetch(PAGE)
    marker = tmp_path / "cache" / "prune.json"
    assert json.loads(marker.read_text())["at"] == "2026-11-17T12:00:00Z"
    clock.advance(3600)
    f2, _ = fetcher(tmp_path, t, clock)
    f2.fetch(HOST + "/b")
    assert json.loads(marker.read_text())["at"] == "2026-11-17T12:00:00Z"   # not due yet


def test_need_body_gets_the_body_when_the_local_cache_is_empty(tmp_path):
    # A new fleet host: the caller's spec_sources row has validators, the cache is empty.
    def page(url, headers):
        return resp(304, "", None) if headers.get("If-None-Match") == '"v7"' else resp(200, HTML, etag='"v7"')

    prior = {"url": PAGE, "final_url": PAGE, "etag": '"v7"', "content_sha256": "9" * 64,
             "fetched_at": "2026-11-10T00:00:00Z", "checked_at": "2026-11-16T00:00:00Z"}
    t = FakeTransport({ROBOTS: robots_ok(), PAGE: page})
    f, _ = fetcher(tmp_path, t)
    first = f.fetch(PAGE, prior=prior)
    assert first.status == "not-modified" and not first.ok
    assert ss.needs_extraction({"content_sha256": "9" * 64, "fingerprint": "old"},
                               content_sha256=first.content_sha256, fingerprint="new")
    second = f.fetch(PAGE, prior=prior, need_body=True)
    assert "If-None-Match" not in t.calls[-1][1]
    assert second.ok and second.read_body() == HTML.encode()
    assert ss.document_from_fetch(second, kind="manufacturer-page").pairs
    # with the body cached now, a later conditional 304 serves it
    third = f.fetch(PAGE, prior=second.to_record(), force=True)
    assert third.status == "not-modified" and third.ok and third.read_body() == HTML.encode()


def test_a_prior_without_body_hash_still_finds_the_cached_body(tmp_path):
    t = FakeTransport({ROBOTS: robots_ok(), PAGE: resp(200, HTML)})
    f, clock = fetcher(tmp_path, t)
    rec = f.fetch(PAGE).to_record()
    rec.pop("body_sha256")                         # e.g. a DB row that keeps only the content hash
    clock.advance(3600)
    r = f.fetch(PAGE, prior=rec)
    assert r.status == "cached" and r.ok and r.read_body() == HTML.encode()


def test_unparseable_urls_are_refused_not_raised(tmp_path):
    t = FakeTransport({ROBOTS: robots_ok(), PAGE: resp(302, "", None, location="https://www.maker.example:99999/x")})
    f, _ = fetcher(tmp_path, t)
    assert f.fetch("https://www.maker.example:99999/x").reason == "unparseable-url"
    assert f.fetch(PAGE).reason == "redirect-unparseable-url"
    assert ss.normalize_url("HTTP://[::1]:8080/a#frag") == "http://[::1]:8080/a"


def test_a_tampered_body_hash_cannot_escape_the_cache(tmp_path):
    t = FakeTransport({ROBOTS: robots_ok(), PAGE: resp(200, HTML)})
    f, _ = fetcher(tmp_path, t)
    r = f.fetch(PAGE, prior={"url": PAGE, "content_sha256": "1" * 64, "body_sha256": "../../../etc/passwd"},
                need_body=True)
    assert r.status == "fetched" and r.read_body() == HTML.encode()


# ---------------------------------------------------------------------------
# Review round 1: schemes, private networks, redirects
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "http://127.0.0.1/manual.pdf", "http://10.0.0.5/specs", "http://192.168.1.1/cgi-bin/reboot",
    "http://169.254.169.254/latest/meta-data/", "http://100.64.0.7/x", "http://[::1]/x", "http://[::1]:8080/x",
    "http://[::ffff:192.168.1.1]/x",
    "http://[fd00::1]/x", "http://localhost:8080/x", "http://router/admin", "http://nas.local/x",
    "http://printer.lan/x", "http://2130706433/x",
])
def test_private_and_local_hosts_are_refused_without_a_request(tmp_path, url):
    t = FakeTransport()
    f, _ = fetcher(tmp_path, t)
    r = f.fetch(url)
    assert r.status == "refused" and r.reason.split(":")[0] in ("non-public-address", "local-hostname")
    assert t.calls == []


def test_redirect_into_the_lan_is_refused_and_not_followed(tmp_path):
    t = FakeTransport({ROBOTS: robots_ok(), PAGE: resp(302, "", None, location="http://192.168.1.1/cgi-bin/reboot")})
    f, _ = fetcher(tmp_path, t)
    r = f.fetch(PAGE)
    assert r.status == "refused" and r.reason.startswith("redirect-non-public-address")
    assert t.urls() == [ROBOTS, PAGE] and f.index_record(PAGE) is None


@pytest.mark.parametrize("target", ["file:///etc/hostname", "ftp://files.maker.example/robots.txt",
                                    "data:text/plain,User-agent: *", "http://127.0.0.1/robots.txt"])
def test_robots_redirects_are_validated_like_page_redirects(tmp_path, target):
    seen = []

    def transport(url, headers, timeout_s, max_bytes):
        seen.append(url)
        if url == ROBOTS:
            return resp(302, "", None, location=target)
        return resp(200, HTML)

    f, _ = fetcher(tmp_path, transport)
    r = f.fetch(PAGE)                              # no exception, no request to the redirect target
    assert r.status == "robots-disallowed" and r.reason.startswith("robots-redirect-refused")
    assert seen == [ROBOTS]


def test_a_broken_robots_evaluation_disallows_instead_of_raising(tmp_path, monkeypatch):
    t = FakeTransport({ROBOTS: robots_ok(), PAGE: resp(200, HTML)})
    f, _ = fetcher(tmp_path, t)

    def boom(self, url, token):
        raise RuntimeError("bad rules")
    monkeypatch.setattr(ss.RobotsRules, "can_fetch", boom)
    r = f.fetch(PAGE)
    assert r.status == "robots-disallowed" and r.reason == "robots-error:RuntimeError"
    assert PAGE not in t.urls()


@pytest.mark.parametrize("url", ["file:///etc/hostname", "ftp://ftp.example.com/x", "data:text/plain,hi",
                                 "gopher://x.example/"])
def test_the_real_transport_only_speaks_http(url):
    r = ss.urllib_transport(url, {}, 5.0, 1000)
    assert r.status == 0 and r.body == b"" and r.error.startswith("unsupported-scheme")


def test_the_real_transport_refuses_a_non_public_peer_at_connect_time():
    # Covers names that resolve to the LAN or loopback (and DNS rebinding):
    # the connected address is checked before a single request byte is sent.
    import socket
    import threading
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    got = []

    def serve():
        srv.settimeout(5)
        try:
            conn, _ = srv.accept()
            conn.settimeout(2)
            try:
                got.append(conn.recv(100))
            except OSError:
                got.append(b"")
            conn.close()
        except OSError:
            pass

    th = threading.Thread(target=serve, daemon=True)
    th.start()
    try:
        r = ss.urllib_transport(f"http://localhost:{port}/robots.txt", {}, 5.0, 1000)
    finally:
        th.join(6)
        srv.close()
    assert r.status == 0 and "non-public-address" in r.error
    assert got in ([], [b""])                     # connected, then closed with nothing sent


# ---------------------------------------------------------------------------
# Review round 1: throttling, robots token, host lists, Amazon floor, charset
# ---------------------------------------------------------------------------

def test_429_and_503_back_the_host_off(tmp_path):
    t = FakeTransport({ROBOTS: robots_ok(), PAGE: resp(429, "", None, retry_after="120"),
                       HOST + "/b": resp(200, HTML), HOST + "/c": resp(503, "", None),
                       HOST + "/d": resp(503, "", None), HOST + "/e": resp(200, HTML)})
    f, clock = fetcher(tmp_path, t)
    r = f.fetch(PAGE)
    assert r.status == "http-error" and r.http_status == 429 and "120" in r.reason
    assert f.fetch(HOST + "/b").status == "fetched"           # waited out Retry-After
    f.fetch(HOST + "/c")                                      # 503: 60 s backoff
    f.fetch(HOST + "/d")                                      # another 503: doubles to 120 s
    assert f.fetch(HOST + "/e").status == "fetched"
    assert clock.slept == [pytest.approx(120.0), pytest.approx(60.0), pytest.approx(120.0)]


def test_a_long_retry_after_defers_the_host_for_the_run(tmp_path):
    t = FakeTransport({ROBOTS: robots_ok(), PAGE: resp(429, "", None, retry_after="3600"),
                       HOST + "/b": resp(200, HTML)})
    f, _ = fetcher(tmp_path, t)
    f.fetch(PAGE)
    n = len(t.calls)
    r = f.fetch(HOST + "/b")
    assert r.status == "deferred" and r.reason == "host-busy" and len(t.calls) == n


def test_retry_after_http_date():
    now = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
    assert ss._retry_after_s("Tue, 17 Nov 2026 12:05:00 GMT", now) == pytest.approx(300.0)
    assert ss._retry_after_s("90", now) == 90.0
    assert ss._retry_after_s("soon", now) is None and ss._retry_after_s("", now) is None


def test_robots_token_must_be_in_the_user_agent(tmp_path):
    t = FakeTransport({ROBOTS: robots_ok(), PAGE: resp(200, HTML)})
    f, _ = fetcher(tmp_path, t, user_agent="Mozilla/5.0 (compatible; OtherBot/1.0; +https://site.example/bot)")
    r = f.fetch(PAGE)
    assert r.status == "refused" and "robots_token" in r.reason and t.calls == []
    engine = ss.SpecSources(ss.resolve_config(file_cfg={}, site_cfg={"fetch": {"user_agent": "OtherBot/1.0"}}))
    assert any("robots_token" in p for p in engine.problems)


def test_deny_and_allow_host_lists(tmp_path):
    t = FakeTransport({ROBOTS: robots_ok(), PAGE: resp(200, HTML),
                       "https://mirror.example/robots.txt": resp(404), "https://mirror.example/m": resp(200, HTML)})
    f, _ = fetcher(tmp_path, t, deny_hosts=["mirror.example"])
    assert f.fetch("https://mirror.example/m").reason == "denied-host:mirror.example"
    assert f.fetch(PAGE).status == "fetched"
    g, _ = fetcher(tmp_path / "g", t, allow_hosts=["maker.example"])
    assert g.fetch("https://mirror.example/m").reason == "host-not-allowed:mirror.example"
    assert g.fetch(PAGE).status == "fetched"                  # www.maker.example is a subdomain


@pytest.mark.parametrize("layer", ["by_site", "by_agent_id"])
def test_no_config_layer_can_unblock_amazon(tmp_path, layer):
    weak = {"host_patterns": ["(^|\\.)amazon-mirror\\.example$"], "url_patterns": [], "html_markers": [],
            "text_markers": [], "html_min_markers": 99}
    file_cfg = {layer: {"x": {"refuse": weak}}}
    cfg = ss.resolve_config(file_cfg=file_cfg, **({"site": "x"} if layer == "by_site" else {"agent_id": "x"}))
    t = FakeTransport()
    engine = ss.SpecSources(cfg, transport=t, cache_dir=tmp_path / "c")
    for url in ("https://www.amazon.com/s?k=power+station", "https://www.amazon.com/stores/Brand/page/ABC",
                "https://shop.example/dp/B0D91KV51C", "https://amazon-mirror.example/x"):
        r = engine.fetch(url)
        assert r.status == "refused" and r.reason.startswith("amazon"), url
    assert t.calls == []
    page = (FX / "amazon" / "synthetic-amazon-detail-page.html").read_text()
    with pytest.raises(ss.AmazonContentRefused):
        ss.document_from_html(page, url="https://mirror.example/listing", kind="manufacturer-page",
                              refusal=engine.refusal)
    assert engine.refusal.html_min == 2                       # can be lowered, never raised


def test_declared_charset_survives_into_the_document(tmp_path):
    body = "<html><body><table><tr><td>Temp</td><td>0°C ~ 45°C</td></tr></table></body></html>"
    t = FakeTransport({ROBOTS: robots_ok(), PAGE: resp(200, body.encode("latin-1"), "text/html; charset=ISO-8859-1"),
                       HOST + "/meta": resp(200, ('<html><head><meta charset="windows-1252"></head>' + body[6:])
                                            .encode("cp1252"), "text/html")})
    f, clock = fetcher(tmp_path, t)
    r = f.fetch(PAGE)
    assert r.charset == "iso-8859-1" and r.to_record()["charset"] == "iso-8859-1"
    doc = ss.document_from_fetch(r, kind="manufacturer-page")
    assert ("Temp", "0°C ~ 45°C") in {(p.label, p.value) for p in doc.pairs}
    clock.advance(3600)
    cached = f.fetch(PAGE)                                    # from the cache: the charset is kept
    assert cached.status == "cached" and cached.charset == "iso-8859-1"
    assert ("Temp", "0°C ~ 45°C") in {(p.label, p.value) for p in ss.document_from_fetch(
        cached, kind="manufacturer-page").pairs}
    meta = ss.document_from_fetch(f.fetch(HOST + "/meta"), kind="manufacturer-page")
    assert ("Temp", "0°C ~ 45°C") in {(p.label, p.value) for p in meta.pairs}
