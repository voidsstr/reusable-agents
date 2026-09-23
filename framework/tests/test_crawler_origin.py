"""The shared crawler (agents/progressive-improvement-agent/crawler.py) treats
`example.com` and `www.example.com` as one site.

Regression for 2026-09-18 → 09-23: once the crawler began reporting redirected
pages at their final URL, an apex that 301s to www was dropped as "off-site",
so competitor-research crawled zero pages from every such competitor and
compared the site against nothing.
"""
import importlib
import os
import sys
from types import SimpleNamespace

import pytest

PI_DIR = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "..", "agents", "progressive-improvement-agent"))


@pytest.fixture
def crawler():
    sys.path.insert(0, PI_DIR)
    try:
        return importlib.import_module("crawler")
    except ImportError as e:  # bs4 etc. not installed here
        pytest.skip(f"crawler deps unavailable: {e}")
    finally:
        sys.path.remove(PI_DIR)


def test_www_and_apex_are_one_site(crawler):
    assert crawler._same_origin("https://rtings.com/", "https://www.rtings.com/tv")
    assert crawler._same_origin("https://WWW.Example.com/a", "https://example.com/")
    assert not crawler._same_origin("https://rtings.com/", "https://amazon.com/")
    assert not crawler._same_origin("https://example.com/", "https://shop.example.com/")


def test_apex_to_www_redirect_is_kept_at_final_url(crawler, monkeypatch):
    html = "<html><head><title>Home</title></head><body><h1>Hi</h1><p>Body text here.</p></body></html>"

    def fake_fetch(url, **_kw):
        final = url.replace("https://example.com", "https://www.example.com")
        resp = SimpleNamespace(url=final, status_code=200, text=html,
                               headers={"Content-Type": "text/html; charset=utf-8"})
        return resp, None, 5, 1

    monkeypatch.setattr(crawler, "_fetch_with_retry", fake_fetch)
    pages = list(crawler.crawl(base_url="https://example.com", seed_urls=["/"],
                               use_sitemap=False, max_depth=0, max_pages=2,
                               throttle_ms=0))
    assert len(pages) == 1
    assert pages[0].url.startswith("https://www.example.com")
    assert pages[0].redirected_from.startswith("https://example.com")


def test_off_site_redirect_still_dropped(crawler, monkeypatch):
    def fake_fetch(url, **_kw):
        resp = SimpleNamespace(url="https://www.amazon.com/dp/B000", status_code=200,
                               text="<html></html>", headers={"Content-Type": "text/html"})
        return resp, None, 5, 1

    monkeypatch.setattr(crawler, "_fetch_with_retry", fake_fetch)
    pages = list(crawler.crawl(base_url="https://example.com", seed_urls=["/go"],
                               use_sitemap=False, max_depth=0, max_pages=2,
                               throttle_ms=0))
    assert pages == []
