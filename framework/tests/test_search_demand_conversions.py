"""search-demand-agent WHAT SELLS producer (data_sources.affiliate_clicks):
the pure aggregation, the read-only DB collector against a fake driver, and
the run() wiring into the published demand signal. No DB, no network."""
from __future__ import annotations

import importlib.util as iu
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
AGENT = _ROOT / "agents" / "search-demand-agent" / "agent.py"
NOW = datetime(2026, 11, 11, 19, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def sda():
    spec = iu.spec_from_file_location("search_demand_agent_under_test", AGENT)
    mod = iu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _w(m30, m7=None, m14=None):
    """{window: counts} in the producer's window order (30, 7, 14)."""
    return {30: m30, 7: m7 or {}, 14: m14 or {}}


CLUSTERS = [{"name": "Monitors", "pattern": "monitor"},
            {"name": "LLM", "pattern": "llm|local-ai"}]
TEMPLATES = [{"name": "product-page", "pattern": "^/product/"},
             {"name": "best-x-article", "pattern": "^/reviews/best-"},
             {"name": "other-article", "pattern": "^/reviews/"}]
ARTICLES = [
    {"slug": "best-monitor-2026", "title": "Best Monitor", "published_at": NOW - timedelta(days=40)},
    {"slug": "best-oled-monitor-2026", "title": "OLED", "published_at": NOW - timedelta(days=3)},
    {"slug": "llm-rig-2026", "title": "LLM rig", "published_at": NOW - timedelta(days=2)},
    {"slug": "local-ai-box", "title": "box", "published_at": (NOW - timedelta(days=20)).isoformat()},
    {"slug": "retro-pc", "title": "Retro", "published_at": None},
]


def _summary(sda, **over):
    kw = dict(
        page_counts=_w({"/product/B01": 10, "https://site.test/reviews/best-monitor-2026?x=1": 4,
                        "/reviews/best-monitor-2026/": 2, "/reviews/llm-rig-2026": 1, "": 3},
                       {"/product/B01": 6, "/reviews/best-monitor-2026": 2, "": 1},
                       {"/product/B01": 8, "/reviews/best-monitor-2026": 5, "": 2}),
        product_counts=_w({"B01": 12, "B02": 5, "": 3}, {"B01": 7, "B02": 1}, {"B01": 9, "B02": 4}),
        placement_counts=_w({"pdp-hero": 9, "inline": 6, "": 5}, {"pdp-hero": 5}, {"pdp-hero": 7}),
        product_meta={"B01": {"key": "B01", "title": "Monitor A", "category": "monitors",
                              "price": 300, "link": "/product/B01"},
                      "B02": {"key": "B02", "title": "Cable", "category": "accessories",
                              "price": None}},
        articles=ARTICLES, clusters=CLUSTERS, article_path_template="/reviews/{slug}",
        templates=TEMPLATES, label="Amazon", definition="d", now=NOW)
    kw.update(over)
    return sda.summarize_conversions(**kw)


def test_totals_and_trend_cover_every_click_including_null_pages(sda):
    cw, metrics = _summary(sda)
    assert cw["total"] == {"d30": 20, "d7": 9, "prev7": 6}     # 14d 15 − 7d 9
    assert (cw["window_days"], cw["trend_days"], cw["label"]) == (30, 7, "Amazon")
    assert metrics["conversion_clicks_30d"] == 20
    assert metrics["conversion_clicks_7d"] == 9
    assert metrics["conversion_clicks_prev7d"] == 6


def test_pages_merge_full_urls_query_strings_and_trailing_slashes(sda):
    cw, _ = _summary(sda)
    pages = {p["path"]: p for p in cw["pages"]}
    assert pages["/reviews/best-monitor-2026"] == {
        "path": "/reviews/best-monitor-2026", "template": "best-x-article",
        "clicks": 6, "d7": 2, "prev7": 3}
    assert cw["pages"][0]["path"] == "/product/B01"
    assert "" not in pages
    assert len(_summary(sda, top_pages=1)[0]["pages"]) == 1


def test_templates_carry_article_yield_and_keep_zero_click_formats(sda):
    cw, _ = _summary(sda)
    t = {r["template"]: r for r in cw["templates"]}
    assert t["product-page"] == {"template": "product-page", "clicks": 10, "d7": 6,
                                 "prev7": 2, "pages": 1}
    # 2 best-* articles published, 6 clicks → 300/100
    assert t["best-x-article"]["articles"] == 2
    assert t["best-x-article"]["clicks_per_100"] == 300.0
    # other-article: 3 published (llm-rig clicked once, local-ai-box, retro-pc)
    assert t["other-article"]["articles"] == 3 and t["other-article"]["clicks"] == 1
    assert [r["template"] for r in cw["templates"]][0] == "product-page"


def test_clusters_are_clicks_per_100_published_articles(sda):
    cw, _ = _summary(sda)
    c = {r["cluster"]: r for r in cw["clusters"]}
    assert c["Monitors"] == {"cluster": "Monitors", "articles": 2, "clicks": 6, "d7": 2,
                             "clicks_per_100": 300.0}
    assert c["LLM"]["articles"] == 2 and c["LLM"]["clicks"] == 1
    assert c["other"]["articles"] == 1
    assert cw["clusters"][0]["cluster"] == "Monitors"
    no_articles, _ = _summary(sda, articles=[])
    assert no_articles["clusters"] == []


def test_products_resolve_meta_and_categories_sum_value(sda):
    cw, _ = _summary(sda)
    assert cw["products"][0] == {"key": "B01", "title": "Monitor A", "category": "monitors",
                                 "clicks": 12, "d7": 7, "prev7": 2, "price": 300.0,
                                 "link": "/product/B01"}
    assert "price" not in cw["products"][1] and "link" not in cw["products"][1]
    cats = {r["category"]: r for r in cw["categories"]}
    assert cats["monitors"] == {"category": "monitors", "clicks": 12, "d7": 7, "prev7": 2,
                                "products": 1, "value": 3600.0}
    assert "value" not in cats["accessories"]
    assert len(_summary(sda, top_products=1)[0]["products"]) == 1


def test_category_column_overrides_product_derived_categories(sda):
    cw, _ = _summary(sda, category_counts=_w({"knives": 4, "": 2}, {"knives": 1}, {"knives": 3}))
    assert cw["categories"] == [{"category": "knives", "clicks": 4, "d7": 1, "prev7": 2}]


def test_placements_drop_unset_slots(sda):
    cw, _ = _summary(sda)
    assert cw["placements"] == [
        {"placement": "pdp-hero", "clicks": 9, "d7": 5, "prev7": 2},
        {"placement": "inline", "clicks": 6, "d7": 0, "prev7": 0}]


def test_steering_metrics_use_published_at(sda):
    _, m = _summary(sda)
    # articles on 7 clicks: best-monitor (6) + llm-rig (1)
    assert m["conversion_article_clicks_30d"] == 7
    # published in the last 30d: best-oled (0), llm-rig (1), local-ai-box (0)
    assert m["conversion_new_article_clicks_30d"] == 1
    assert m["conversion_recent_articles_7d"] == 2
    # Both clusters are small samples (2 articles, under 10 clicks), so no
    # cluster qualifies as a top WHAT SELLS cluster and the share is omitted
    # rather than reported as 0.
    assert "conversion_top5_cluster_share_7d_pct" not in m


def test_steering_share_counts_recent_articles_in_top_clusters(sda):
    pages = _w({"/reviews/best-monitor-2026": 40, "/reviews/llm-rig-2026": 1}, {}, {})
    _, m = _summary(sda, page_counts=pages)
    # Monitors 40 clicks (judged by clicks) is the only above-average cluster;
    # of the 2 recent articles one (best-oled-monitor) is in it.
    assert m["conversion_top5_cluster_share_7d_pct"] == 50.0


def test_share_metric_counts_the_clusters_the_block_names(sda):
    pages = _w({"/reviews/best-monitor-2026": 40, "/reviews/llm-rig-2026": 1}, {}, {})
    cw, m = _summary(sda, page_counts=pages)
    assert sda.TOP_CLUSTERS_FOR_SHARE == sda.demand_signal.WHAT_SELLS_TOP_CLUSTERS
    block = sda.demand_signal.build_conversion_block({"conversion_winners": cw})
    rule = next(l for l in block.splitlines() if l.startswith("RULE: "))
    assert rule.startswith("RULE: WHAT SELLS clusters: Monitors. ")
    # LLM (1 click on 2 articles) is not a lane, in the block or the metric
    assert "  - LLM" not in block
    assert m["conversion_top5_cluster_share_7d_pct"] == 50.0


def test_template_yield_counts_only_published_article_paths(sda):
    # /reviews/legacy-guide matches other-article's pattern but is not a
    # published article (e.g. a legacy table): its clicks stay in the template
    # totals and out of the per-article yield.
    pages = _w({"/reviews/llm-rig-2026": 1, "/reviews/legacy-guide": 11}, {}, {})
    cw, m = _summary(sda, page_counts=pages)
    t = {r["template"]: r for r in cw["templates"]}["other-article"]
    assert (t["clicks"], t["pages"]) == (12, 2)
    assert (t["articles"], t["article_clicks"]) == (3, 1)
    assert t["clicks_per_100"] == 33.3                     # 1 / 3, not 12 / 3
    assert m["conversion_article_clicks_30d"] == 1
    best = {r["template"]: r for r in _summary(sda)[0]["templates"]}["best-x-article"]
    assert best["article_clicks"] == best["clicks"] == 6


def test_inactive_products_keep_their_clicks_but_lose_the_link(sda):
    meta = {"B01": {"key": "B01", "title": "Monitor A", "category": "monitors", "price": 300,
                    "link": "/product/B01", "active": False},
            "B02": {"key": "B02", "title": "Cable", "category": "accessories", "price": None,
                    "link": "/product/B02", "active": True}}
    cw, _ = _summary(sda, product_meta=meta)
    p = {r["key"]: r for r in cw["products"]}
    assert p["B01"]["active"] is False and "link" not in p["B01"] and p["B01"]["clicks"] == 12
    assert p["B02"]["link"] == "/product/B02" and "active" not in p["B02"]
    cats = {r["category"]: r for r in cw["categories"]}
    assert (cats["monitors"]["clicks"], cats["monitors"]["value"]) == (12, 3600.0)
    assert sda._inactive(False) and sda._inactive("f") and sda._inactive(0)
    assert not (sda._inactive(None) or sda._inactive(True) or sda._inactive("t"))
    block = sda.demand_signal.build_conversion_block({"conversion_winners": cw})
    assert "Monitor A" not in block
    assert "  - Cable [accessories] /product/B02: 5 clicks (7d 1 vs 3)" in block


def test_payload_caps_default_above_the_render_caps(sda):
    pages = _w({f"/product/P{i:03d}": 100 - i for i in range(80)}, {}, {})
    prods = _w({f"P{i:03d}": 100 - i for i in range(80)}, {}, {})
    cw, _ = _summary(sda, page_counts=pages, product_counts=prods, product_meta={})
    assert len(cw["products"]) == sda.DEFAULT_TOP_PRODUCTS == 50
    assert len(cw["pages"]) == sda.DEFAULT_TOP_PAGES == 25
    assert cw["products"][0]["key"] == "P000"


def test_no_published_at_means_no_dated_metrics(sda):
    undated = [{"slug": a["slug"], "title": a["title"]} for a in ARTICLES]
    _, m = _summary(sda, articles=undated)
    assert "conversion_article_clicks_30d" in m
    assert "conversion_new_article_clicks_30d" not in m
    assert "conversion_recent_articles_7d" not in m


def test_definition_names_table_match_and_profile(sda):
    assert sda._conversion_definition("clicks", {"target": "amazon", "via": ["beacon"]},
                                      "x-agent") == (
        "verified-human clicks (framework/core/human_clicks.py verdict 'human', "
        "profile x-agent) on clicks where target=amazon, via=beacon")


def test_click_path_rendering(sda):
    assert sda._click_path("https://a.test/blog/x/?q=1#f") == "/blog/x"
    assert sda._click_path("B0ABC", "/product/{value}") == "/product/B0ABC"
    assert sda._click_path("") == "" and sda._click_path(None) == ""
    assert sda._click_path("not-a-path") == ""


# ── the DB collector, against a fake psycopg2 ────────────────────────────────

class _Cur:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.conn.sql.append((sql, params))
        self._rows = []
        if "FROM products" in sql:
            self._rows = [{"key": k, "title": f"T{k}", "category": "c", "price": 10,
                           "link": f"/p/{k}"} for k in (params or {}).get("keys", [])]
        elif "FROM articles" in sql:
            self._rows = [{"slug": "a1", "title": "Monitor", "published_at": None}]
        if self.conn.fail_on and self.conn.fail_on in sql:
            raise RuntimeError("boom")

    def fetchall(self):
        return self._rows


class _Conn:
    def __init__(self, fail_on=None):
        self.sql, self.readonly, self.closed, self.rollbacks = [], None, False, 0
        self.fail_on = fail_on

    def set_session(self, readonly=False, **kw):
        self.readonly = readonly

    def cursor(self, cursor_factory=None):
        return _Cur(self)

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


@pytest.fixture
def fake_db(monkeypatch):
    state = {"conn": None, "fail_on": None, "calls": []}

    def connect(dsn, **kw):
        state["conn"] = _Conn(state["fail_on"])
        state["dsn"] = dsn
        return state["conn"]

    pg = types.ModuleType("psycopg2")
    pg.connect = connect
    extras = types.ModuleType("psycopg2.extras")
    extras.RealDictCursor = object
    pg.extras = extras
    monkeypatch.setitem(sys.modules, "psycopg2", pg)
    monkeypatch.setitem(sys.modules, "psycopg2.extras", extras)

    from framework.core import human_clicks
    monkeypatch.setattr(human_clicks, "load_config", lambda storage=None: {
        "by_profile": {"site-search-demand-agent": {"extra_bot_ua_terms": ["zzfleet"]}}})

    def counts_by(conn, spec, *, table, group_col, window_days, match=None):
        state["calls"].append((table, group_col, window_days, match, spec))
        return {"source_page": {"/product/B01": 5, "": 1},
                "asin": {"B01": 5, "B02": 1}, "placement": {"pdp-hero": 6}}[group_col]
    monkeypatch.setattr(human_clicks, "human_counts_by", counts_by)
    return state


BLOCK = {"label": "Amazon", "dsn_env": "SITE_DSN", "table": "affiliate_click_log",
         "spec": {"time_col": "clicked_at", "referer_col": "source_page"},
         "match": {"target": "amazon", "via": "beacon"},
         "page_col": "source_page", "product_col": "asin", "placement_col": "placement",
         "products_query": "SELECT ... FROM products WHERE asin = ANY(%(keys)s)",
         "articles_query": "SELECT slug, title, published_at FROM articles"}


def _agent(decisions):
    return types.SimpleNamespace(agent_id="site-search-demand-agent",
                                 decide=lambda kind, msg, **kw: decisions.append((kind, msg)))


def _cfg(block=None, **extra):
    ds = {"db": {"dsn_env": "SITE_DSN"},
          "ai_traffic": {"article_path_template": "/reviews/{slug}",
                         "clusters": [{"name": "Monitors", "pattern": "monitor"}]}}
    if block is not None:
        ds["affiliate_clicks"] = block
    return {"site_id": "site", "data_sources": ds, "templates": TEMPLATES, **extra}


def test_collector_reads_read_only_with_timeouts_profile_and_windows(sda, fake_db, monkeypatch):
    monkeypatch.setenv("SITE_DSN", "postgresql://fake")
    dec = []
    cw, metrics = sda.SearchDemandAgent._conversion_winners(_agent(dec), _cfg(BLOCK))
    conn = fake_db["conn"]
    assert conn.readonly is True and conn.closed is True
    assert dec == []
    # three facets × windows (30, 7, 14), same table + match every time
    assert [(c[1], c[2]) for c in fake_db["calls"]] == [
        (col, w) for col in ("source_page", "asin", "placement") for w in (30, 7, 14)]
    assert all(c[0] == "affiliate_click_log" and c[3] == {"target": "amazon", "via": "beacon"}
               for c in fake_db["calls"])
    # storage by_profile[<agent id>] reached the spec the counts ran with
    assert "zzfleet" in fake_db["calls"][0][4]["bot_ua_terms"]
    timeouts = [s for s, _ in conn.sql if s.startswith("SET LOCAL statement_timeout")]
    assert len(timeouts) == 9 + 2       # every facet query + products + articles
    products_sql = next(p for s, p in conn.sql if "FROM products" in s)
    assert products_sql == {"keys": ["B01", "B02"]}
    assert cw["total"]["d30"] == 6 and cw["products"][0]["link"] == "/p/B01"
    assert cw["clusters"][0]["cluster"] == "Monitors"       # clusters inherited from ai_traffic
    assert "via=beacon" in cw["definition"] and "site-search-demand-agent" in cw["definition"]
    assert metrics["conversion_clicks_30d"] == 6


def test_group_col_alias_and_optional_facets(sda, fake_db, monkeypatch):
    monkeypatch.setenv("SITE_DSN", "postgresql://fake")
    block = {"table": "affiliate_click_log", "group_col": "source_page"}
    cw, _ = sda.SearchDemandAgent._conversion_winners(_agent([]), _cfg(block))
    assert {c[1] for c in fake_db["calls"]} == {"source_page"}
    assert cw["products"] == [] and cw["placements"] == [] and cw["label"] == "affiliate"


def test_collector_off_or_misconfigured_returns_none(sda, fake_db, monkeypatch):
    monkeypatch.setenv("SITE_DSN", "postgresql://fake")
    dec = []
    assert sda.SearchDemandAgent._conversion_winners(_agent(dec), _cfg()) is None
    assert sda.SearchDemandAgent._conversion_winners(
        _agent(dec), _cfg({**BLOCK, "enabled": False})) is None
    assert dec == [] and fake_db["conn"] is None            # never connected
    assert sda.SearchDemandAgent._conversion_winners(
        _agent(dec), _cfg({"table": "t"})) is None
    assert dec[-1] == ("warning", "affiliate_clicks skipped: needs table and page_col")
    monkeypatch.delenv("SITE_DSN")
    assert sda.SearchDemandAgent._conversion_winners(_agent(dec), _cfg(BLOCK)) is None
    assert dec[-1] == ("warning", "affiliate_clicks skipped: $SITE_DSN not set")


def test_collector_failure_degrades_to_a_warning(sda, fake_db, monkeypatch):
    monkeypatch.setenv("SITE_DSN", "postgresql://fake")
    fake_db["fail_on"] = "FROM products"
    dec = []
    assert sda.SearchDemandAgent._conversion_winners(_agent(dec), _cfg(BLOCK)) is None
    assert dec and dec[-1][0] == "warning" and "signal ships without WHAT SELLS" in dec[-1][1]
    assert fake_db["conn"].closed is True


@pytest.mark.parametrize("over, at_over, needle", [
    ({"templates": [{"name": "bad-tpl", "pattern": "["}]}, {}, "bad templates regex 'bad-tpl'"),
    ({}, {"clusters": [{"name": "Bad", "pattern": "("}]}, "bad clusters regex 'Bad'"),
    ({"top_pages": "many"}, {}, "invalid literal for int()"),
    ({"statement_timeout_ms": "soon"}, {}, "invalid literal for int()"),
])
def test_bad_config_degrades_to_a_warning_before_connecting(sda, fake_db, monkeypatch,
                                                            over, at_over, needle):
    monkeypatch.setenv("SITE_DSN", "postgresql://fake")
    cfg = _cfg({**BLOCK, **over})
    cfg["data_sources"]["ai_traffic"].update(at_over)
    dec = []
    assert sda.SearchDemandAgent._conversion_winners(_agent(dec), cfg) is None
    assert dec[-1][0] == "warning" and needle in dec[-1][1]
    assert "signal ships without WHAT SELLS" in dec[-1][1]
    assert fake_db["conn"] is None                 # failed before any DB work


def test_non_mapping_block_degrades_to_a_warning(sda, fake_db, monkeypatch):
    monkeypatch.setenv("SITE_DSN", "postgresql://fake")
    dec = []
    assert sda.SearchDemandAgent._conversion_winners(_agent(dec), _cfg("yes")) is None
    assert dec[-1][0] == "warning" and fake_db["conn"] is None


def test_collector_passes_the_blocks_payload_caps(sda, fake_db, monkeypatch):
    monkeypatch.setenv("SITE_DSN", "postgresql://fake")
    cw, _ = sda.SearchDemandAgent._conversion_winners(
        _agent([]), _cfg({**BLOCK, "top_products": 1, "top_pages": 1}))
    assert [p["key"] for p in cw["products"]] == ["B01"] and len(cw["pages"]) == 1


# ── run(): the block reaches the published signal, metrics and summary ───────

def test_run_publishes_conversion_winners(sda, monkeypatch, tmp_path):
    cfg_path = tmp_path / "site.yaml"
    cfg_path.write_text("site_id: site\ndata_sources:\n  gsc: {site_url: 'sc-domain:x'}\n"
                        "  ga4: {property_id: '1'}\nauth: {oauth_file: '/dev/null'}\n")
    monkeypatch.setenv("SEARCH_DEMAND_CONFIG", str(cfg_path))
    monkeypatch.setattr(sda, "_mint_token", lambda f: "ya29.fake")
    written = {}
    monkeypatch.setattr(sda.demand_signal, "write_demand",
                        lambda storage, site, payload: written.update(site=site, payload=payload)
                        or "framework/demand-signal/site.json")
    cw = {"label": "Amazon", "window_days": 30, "trend_days": 7, "definition": "d",
          "total": {"d30": 5, "d7": 2, "prev7": 1}, "clusters": [], "templates": []}
    fake = types.SimpleNamespace(
        agent_id="site-search-demand-agent", storage=None,
        status=lambda *a, **k: None, decide=lambda *a, **k: None,
        _pull_gsc=lambda token, url: [], _pull_ga4=lambda token, pid: [],
        _pull_ga4_template_totals=lambda token, pid, t: {},
        _pull_ga4_top_paths=lambda *a, **k: [], _resolve_vs_pairs=lambda dsn, p: [],
        _category_vocab=lambda dsn: [], _steered_published_7d=lambda dsn, s: 0,
        _ai_assistant_demand=lambda cfg: None,
        _conversion_winners=lambda cfg: (cw, {"conversion_clicks_30d": 5}))
    result = sda.SearchDemandAgent.run(fake)
    assert written["site"] == "site"
    assert written["payload"]["conversion_winners"] is cw
    assert result.metrics["conversion_clicks_30d"] == 5
    assert result.summary.endswith("what_sells=5")
    fake._conversion_winners = lambda cfg: None
    result = sda.SearchDemandAgent.run(fake)
    assert "conversion_winners" not in written["payload"]
    assert "what_sells" not in result.summary and "conversion_clicks_30d" not in result.metrics


def test_run_still_publishes_when_the_what_sells_config_is_broken(sda, fake_db, monkeypatch,
                                                                  tmp_path):
    """A typo in the click block (here a template regex) costs WHAT SELLS
    only: run() still writes the GSC/GA4/AI signal and succeeds."""
    cfg_path = tmp_path / "site.yaml"
    cfg_path.write_text(
        "site_id: site\ndata_sources:\n  gsc: {site_url: 'sc-domain:x'}\n"
        "  ga4: {property_id: '1'}\n"
        "  affiliate_clicks: {dsn_env: SITE_DSN, table: t, page_col: source_page,\n"
        "                     templates: [{name: bad-tpl, pattern: '['}]}\n"
        "auth: {oauth_file: '/dev/null'}\n")
    monkeypatch.setenv("SEARCH_DEMAND_CONFIG", str(cfg_path))
    monkeypatch.setenv("SITE_DSN", "postgresql://fake")
    monkeypatch.setattr(sda, "_mint_token", lambda f: "ya29.fake")
    written = {}
    monkeypatch.setattr(sda.demand_signal, "write_demand",
                        lambda storage, site, payload: written.update(site=site, payload=payload)
                        or "framework/demand-signal/site.json")
    dec = []
    fake = types.SimpleNamespace(
        agent_id="site-search-demand-agent", storage=None,
        status=lambda *a, **k: None, decide=lambda kind, msg, **kw: dec.append((kind, msg)),
        _pull_gsc=lambda token, url: [], _pull_ga4=lambda token, pid: [],
        _pull_ga4_template_totals=lambda token, pid, t: {},
        _pull_ga4_top_paths=lambda *a, **k: [], _resolve_vs_pairs=lambda dsn, p: [],
        _category_vocab=lambda dsn: [], _steered_published_7d=lambda dsn, s: 0,
        _ai_assistant_demand=lambda cfg: None)
    fake._conversion_winners = lambda cfg: sda.SearchDemandAgent._conversion_winners(fake, cfg)
    result = sda.SearchDemandAgent.run(fake)
    assert result.status == "success"
    assert written["site"] == "site" and "conversion_winners" not in written["payload"]
    assert any(k == "warning" and "bad templates regex 'bad-tpl'" in m for k, m in dec)
    assert fake_db["conn"] is None
