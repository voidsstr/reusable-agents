"""Tests for framework.core.bing_webmaster (audit F090).

No agent read Bing's index/crawl data although Bing's index serves ChatGPT
search and ~96% of aisleprompt's human search arrivals. The client must
degrade to "unavailable" without a key and never leak the key in errors.
"""
from __future__ import annotations

import http.server
import json
import threading
import urllib.parse

import pytest

from framework.core import bing_webmaster as bwt

DAY_MS = 86_400_000
T0 = 1_758_758_400_000  # 2025-09-25T00:00Z


def _d(ms: int) -> str:
    return f"/Date({ms}-0700)/"


@pytest.fixture()
def api(monkeypatch):
    seen: list[dict] = []

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            u = urllib.parse.urlsplit(self.path)
            q = dict(urllib.parse.parse_qsl(u.query))
            seen.append({"method": u.path.rsplit("/", 1)[-1], **q})
            if q.get("apikey") != "k123":
                self.send_response(401); self.end_headers(); self.wfile.write(b"bad key"); return
            method = u.path.rsplit("/", 1)[-1]
            payload = {
                "GetCrawlStats": [
                    {"Date": _d(T0 - DAY_MS), "InIndex": 900, "CrawledPages": 40, "CrawlErrors": 3, "Code5xx": 2},
                    {"Date": _d(T0), "InIndex": 1000, "CrawledPages": 50, "CrawlErrors": 1, "Code5xx": 0},
                ],
                "GetRankAndTrafficStats": [
                    {"Date": _d(T0 - i * DAY_MS), "Clicks": 2, "Impressions": 10} for i in range(40)
                ],
                "GetQueryStats": [{"Query": "a", "Impressions": 5}, {"Query": "b", "Impressions": 50}],
                "GetPageStats": [],
                "GetFeeds": [{"Url": "https://s.test/sitemap.xml", "UrlCount": 123}],
                "GetCrawlIssues": [{"Url": "https://s.test/x", "HttpCode": 500}],
                "GetUrlSubmissionQuota": {"DailyQuota": 100, "MonthlyQuota": 2800},
                "GetUrlInfo": {"Url": q.get("url"), "HttpStatus": 200},
            }.get(method)
            body = json.dumps({"d": payload}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.end_headers(); self.wfile.write(body)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("BING_WEBMASTER_API_BASE", f"http://127.0.0.1:{srv.server_address[1]}/json")
    yield seen
    srv.shutdown()


def test_no_key_is_unavailable_not_an_error(monkeypatch):
    monkeypatch.delenv("BING_WEBMASTER_API_KEY", raising=False)
    out = bwt.collect("https://s.test/")
    assert out["available"] is False and out["metrics"] == {}
    assert "BING_WEBMASTER_API_KEY" in out["error"]


def test_quoted_key_from_secrets_env_is_unquoted(monkeypatch):
    monkeypatch.setenv("BING_WEBMASTER_API_KEY", "'k123'")
    assert bwt.api_key() == "k123"


def test_collect_and_metrics(api, monkeypatch):
    monkeypatch.setenv("BING_WEBMASTER_API_KEY", "'k123'")
    out = bwt.collect("https://s.test/")
    assert out["available"] is True, out
    m = out["metrics"]
    assert m["in_index"] == 1000 and m["crawled_pages_1d"] == 50 and m["crawl_errors_1d"] == 1
    assert m["clicks_28d"] == 56 and m["impressions_28d"] == 280     # 28 of 40 days
    assert m["crawl_issues"] == 1 and m["url_submission_quota_daily"] == 100
    assert m["sitemap_urls"] == 123
    assert out["top_queries"][0]["Query"] == "b"                         # by impressions
    assert {c["siteUrl"] for c in api} == {"https://s.test/"}
    assert bwt.url_info("https://s.test/", "https://s.test/p")["HttpStatus"] == 200


def test_bad_key_marks_unavailable_and_never_leaks_key(api, monkeypatch):
    monkeypatch.setenv("BING_WEBMASTER_API_KEY", "wrong-key-xyz")
    out = bwt.collect("https://s.test/")
    assert out["available"] is False
    assert "wrong-key-xyz" not in json.dumps(out)
