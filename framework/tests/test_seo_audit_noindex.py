"""The SEO LLM audit never audits a noindexed page.

Regression for 2026-09-22..24: one site's SEO agent spent 14 of 47 commits
re-titling grocery product pages its own server serves with
`noindex, follow` (meta robots AND X-Robots-Tag). They were 1-impression rows
in the GSC top-10 that seeded the audit crawl, and nothing told the audit
they were noindexed.
"""
from __future__ import annotations

import importlib
import importlib.util as iu
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SPEC = iu.spec_from_file_location(
    "llm_audit_noindex_under_test",
    _ROOT / "agents" / "seo-opportunity-agent" / "lib" / "analyzer" / "llm_audit.py",
)
llm_audit = iu.module_from_spec(_SPEC)
_SPEC.loader.exec_module(llm_audit)

PI_DIR = str(_ROOT / "agents" / "progressive-improvement-agent")


@pytest.mark.parametrize("page,expected", [
    ({"robots_meta": "noindex, follow"}, True),
    ({"robots_meta": "NOINDEX"}, True),
    ({"robots_meta": "none"}, True),
    ({"robots_meta": "googlebot: noindex"}, True),
    ({"x_robots_tag": "noindex, follow"}, True),
    ({"robots_meta": "index, follow", "x_robots_tag": "noindex"}, True),
    ({"robots_meta": "index, follow, max-image-preview:large"}, False),
    ({"robots_meta": "noarchive"}, False),
    ({"robots_meta": ""}, False),
    ({}, False),
])
def test_page_is_noindex(page, expected):
    assert llm_audit.page_is_noindex(page) is expected


def test_run_llm_audit_never_sends_noindexed_pages():
    sent: list[str] = []

    def fake_chat(messages, **_kw):
        sent.append(messages[-1]["content"])
        return "[]"

    pages = [
        {"url": "https://site.test/kitchen/fiji-water-6-pack",
         "robots_meta": "noindex, follow", "title": "FIJI", "body_text": "x"},
        {"url": "https://site.test/kitchen/meat-sticks",
         "x_robots_tag": "noindex, follow", "title": "Sticks", "body_text": "x"},
        {"url": "https://site.test/blog/best-air-fryers",
         "robots_meta": "index, follow", "title": "Best", "body_text": "x"},
    ]
    llm_audit.run_llm_audit(pages=pages, site_label="site",
                            ai_chat_callable=fake_chat, batch_size=10)
    blob = "\n".join(sent)
    assert "best-air-fryers" in blob
    assert "fiji-water" not in blob and "meat-sticks" not in blob


def test_all_noindexed_means_no_llm_call():
    calls = []
    out = llm_audit.run_llm_audit(
        pages=[{"url": "https://site.test/a", "robots_meta": "noindex"}],
        site_label="site", ai_chat_callable=lambda *a, **k: calls.append(1) or "[]")
    assert out == [] and calls == []


def test_crawler_records_x_robots_tag(monkeypatch):
    sys.path.insert(0, PI_DIR)
    try:
        crawler = importlib.import_module("crawler")
    except ImportError as e:  # bs4 etc. not installed here
        pytest.skip(f"crawler deps unavailable: {e}")
    finally:
        sys.path.remove(PI_DIR)
    html = ("<html><head><title>P</title><meta name='robots' content='noindex, follow'>"
            "</head><body><h1>P</h1><p>text</p></body></html>")

    def fake_fetch(url, **_kw):
        resp = SimpleNamespace(url=url, status_code=200, text=html,
                               headers={"Content-Type": "text/html",
                                        "X-Robots-Tag": "noindex, follow"},
                               elapsed=SimpleNamespace(total_seconds=lambda: 0.01))
        return resp, None, 5, 1

    monkeypatch.setattr(crawler, "_fetch_with_retry", fake_fetch)
    pages = list(crawler.crawl(base_url="https://site.test", seed_urls=["/p"],
                               use_sitemap=False, max_depth=0, max_pages=1,
                               throttle_ms=0))
    assert pages[0].x_robots_tag == "noindex, follow"
    assert pages[0].robots_meta == "noindex, follow"
    assert llm_audit.page_is_noindex(pages[0].to_dict())
