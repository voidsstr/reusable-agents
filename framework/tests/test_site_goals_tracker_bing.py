"""site-goals-tracker: Bing property from per-site config + GetUrlInfo for
recently published articles (2026-10-01 audit, bing-webmaster-not-configured)."""
from __future__ import annotations

import importlib.util as iu
from pathlib import Path

import pytest

_AGENT = Path(__file__).resolve().parents[2] / "agents" / "site-goals-tracker" / "agent.py"


@pytest.fixture()
def mod(monkeypatch):
    for k in ("SITE_GOALS_SITE_YAML", "SITE_GOALS_REPOS_ROOT", "INDEXNOW_SITE_REPOS_ROOT",
              "SITE_GOALS_BING_SITE_URL", "BING_WEBMASTER_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    spec = iu.spec_from_file_location("site_goals_tracker_agent", _AGENT)
    m = iu.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


_YAML = """
site: {id: s, domain: s.test}
data_sources:
  bing: {site_url: "https://s.test/"}
articles:
  domain: s.test
  url_template: "https://{domain}/blog/{slug}"
  sources: []
"""


def test_no_site_url_is_hardcoded_in_profiles(mod):
    assert all("bing_site_url" not in p for p in mod.SITE_PROFILES.values())


def test_bing_site_url_comes_from_site_yaml(mod, tmp_path, monkeypatch):
    profile = dict(mod.SITE_PROFILES["specpicks"])
    (tmp_path / "specpicks" / "agents" / "seo-opportunity-agent").mkdir(parents=True)
    (tmp_path / "specpicks" / "agents" / "seo-opportunity-agent" / "site.yaml").write_text(_YAML)
    monkeypatch.setenv("SITE_GOALS_REPOS_ROOT", str(tmp_path))
    y = mod.load_site_yaml(profile)
    assert mod.bing_site_url(profile, y) == "https://s.test/"
    # disabled → not configured
    y["data_sources"]["bing"]["enabled"] = False
    assert mod.bing_site_url(profile, y) == ""
    # env override wins
    monkeypatch.setenv("SITE_GOALS_BING_SITE_URL", "https://override.test/")
    assert mod.bing_site_url(profile, {}) == "https://override.test/"


def test_missing_site_yaml_is_empty_not_an_error(mod, tmp_path, monkeypatch):
    monkeypatch.setenv("SITE_GOALS_SITE_YAML", str(tmp_path / "nope.yaml"))
    profile = dict(mod.SITE_PROFILES["aisleprompt"])
    assert mod.load_site_yaml(profile) == {}
    assert mod.bing_site_url(profile, {}) == ""


def test_recent_article_urls_use_url_template(mod, monkeypatch):
    import yaml
    seen = {}

    class Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params):
            seen["sql"], seen["params"] = sql, params
        def fetchall(self): return [("new-one",), ("older",)]

    class Conn:
        def cursor(self): return Cur()
        def close(self): pass

    monkeypatch.setattr(mod.psycopg2, "connect", lambda *a, **k: Conn())
    monkeypatch.setenv("SPECPICKS_DATABASE_URL", "postgresql://fake")
    urls = mod.recent_article_urls(mod.SITE_PROFILES["specpicks"], yaml.safe_load(_YAML),
                                   days=14, limit=30)
    assert urls == ["https://s.test/blog/new-one", "https://s.test/blog/older"]
    assert "published_at <= NOW()" in seen["sql"] and seen["params"] == (14, 30)
    assert mod.recent_article_urls(mod.SITE_PROFILES["specpicks"], {}) == []


def test_bing_recent_url_info_buckets(mod, monkeypatch):
    from framework.core import bing_webmaster
    urls = ["https://s.test/blog/a", "https://s.test/blog/b",
            "https://s.test/blog/c", "https://s.test/blog/d"]
    monkeypatch.setattr(mod, "recent_article_urls", lambda *a, **k: urls)
    answers = {
        urls[0]: {"Url": urls[0], "HttpStatus": 200,
                  "DiscoveryDate": "/Date(1759200000000)/",
                  "LastCrawledDate": "/Date(1759300000000)/"},
        urls[1]: {"Url": urls[1], "HttpStatus": 0,
                  "DiscoveryDate": "/Date(1759200000000)/",
                  "LastCrawledDate": "/Date(-62135596800000)/"},
        urls[2]: {"error": 'HTTP 400 {"ErrorCode":14,"Message":"ERROR!!! NotFound"}'},
        urls[3]: {"error": "TimeoutError: timed out"},
    }
    monkeypatch.setattr(bing_webmaster, "url_info", lambda site, url, **k: answers[url])
    out = mod.bing_recent_url_info(mod.SITE_PROFILES["specpicks"], {}, "https://s.test/")
    assert out == {"bing-recent-articles-checked": 3.0, "bing-recent-articles-unknown": 1.0,
                   "bing-recent-articles-discovered": 2.0, "bing-recent-articles-crawled": 1.0,
                   "bing-recent-articles-errors": 1.0}


def test_bing_recent_url_info_stops_on_auth_error(mod, monkeypatch):
    from framework.core import bing_webmaster
    calls = []
    monkeypatch.setattr(mod, "recent_article_urls",
                        lambda *a, **k: [f"https://s.test/blog/{i}" for i in range(10)])
    monkeypatch.setattr(bing_webmaster, "url_info",
                        lambda site, url, **k: calls.append(url) or {"error": "HTTP 401 InvalidApiKey"})
    out = mod.bing_recent_url_info(mod.SITE_PROFILES["specpicks"], {}, "https://s.test/")
    assert len(calls) == 1 and out["bing-recent-articles-errors"] == 1.0
