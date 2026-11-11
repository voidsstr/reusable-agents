"""SHOP-23: the SEO agent measures traffic to the pages that sell.

data_sources.ga4.money_pages makes the collector pull engaged sessions per
page path (one country, datacenter cities out) and the finalizer sum them over
the top-N conversion_winners pages. No fresh signal means no metric, never a
fake 0 and never a hand-kept page list.
"""
from __future__ import annotations

import importlib.util as iu
import json
import sys
from pathlib import Path
from types import SimpleNamespace

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _load(name: str, rel: str):
    spec = iu.spec_from_file_location(name, _ROOT / rel)
    mod = iu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fin = _load("seo_finalizer_money_pages", "agents/seo-opportunity-agent/finalizer.py")
pull = _load("seo_pull_money_pages", "agents/seo-opportunity-agent/lib/collector/pull-data.py")

SIG = {"conversion_winners": {"pages": [
    {"path": "/blog/best-blenders-2026", "clicks": 30},
    {"path": "/blog/best-coffee-makers-2026/", "clicks": 40},
    {"path": "/kitchen/instant-pot-duo", "clicks": 5},
    {"path": "/blog/best-blenders-2026?x=1", "clicks": 2},
]}}


def _row(path, n):
    return {"dimensionValues": [{"value": path}], "metricValues": [{"value": str(n)}]}


def test_money_page_set_ranks_by_clicks_dedupes_and_filters():
    assert fin.money_page_set(SIG, top_n=20) == [
        "/blog/best-coffee-makers-2026", "/blog/best-blenders-2026", "/kitchen/instant-pot-duo"]
    assert fin.money_page_set(SIG, top_n=1) == ["/blog/best-coffee-makers-2026"]
    assert fin.money_page_set(SIG, path_prefixes=["/blog/"]) == [
        "/blog/best-coffee-makers-2026", "/blog/best-blenders-2026"]
    assert fin.money_page_set(None) == []


def test_sum_engaged_sessions_normalizes_paths():
    resp = {"rows": [_row("/blog/best-blenders-2026", 10), _row("/blog/best-blenders-2026/?utm=x", 3),
                     _row("/blog/other", 99), {"bad": True}]}
    assert fin.sum_engaged_sessions(resp, ["/blog/best-blenders-2026"]) == 13.0


def test_request_filters_country_and_cities():
    body = pull.money_pages_request({"country": "United States", "exclude_cities": ["Ashburn"],
                                     "window_days": 7})
    exprs = body["dimensionFilter"]["andGroup"]["expressions"]
    assert exprs[0]["filter"]["stringFilter"]["value"] == "United States"
    assert exprs[1]["notExpression"]["filter"]["inListFilter"]["values"] == ["Ashburn"]
    assert body["metrics"] == [{"name": "engagedSessions"}]
    assert "dimensionFilter" not in pull.money_pages_request({})


class _Cfg(dict):
    site_id = "site-x"


class _Agent:
    def __init__(self, sig):
        self.storage = SimpleNamespace()
        self.sig = sig
        self.decisions = []

    def decide(self, kind, msg):
        self.decisions.append((kind, msg))


def test_money_page_sessions_end_to_end(tmp_path, monkeypatch):
    from framework.core import demand_signal
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "ga4-money-pages.json").write_text(json.dumps(
        {"rows": [_row("/blog/best-coffee-makers-2026", 7), _row("/blog/best-blenders-2026", 5)]}))
    cfg = _Cfg(data_sources={"ga4": {"money_pages": {"enabled": True, "top_n": 20}}})
    monkeypatch.setattr(demand_signal, "read_demand", lambda storage, site, max_age_hours=72: SIG)
    out = fin._money_page_sessions(_Agent(SIG), cfg, tmp_path)
    assert out == {"money_guide_us_engaged_sessions_7d": 12.0, "money_pages_counted": 3.0}

    # no fresh signal -> no metric
    monkeypatch.setattr(demand_signal, "read_demand", lambda storage, site, max_age_hours=72: None)
    agent = _Agent(None)
    assert fin._money_page_sessions(agent, cfg, tmp_path) == {}
    assert agent.decisions and agent.decisions[0][0] == "warning"

    # disabled -> nothing
    assert fin._money_page_sessions(agent, _Cfg(data_sources={}), tmp_path) == {}
