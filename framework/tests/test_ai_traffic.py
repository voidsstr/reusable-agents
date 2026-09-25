"""Tests for framework.core.ai_traffic (no DB — a fake connection)."""
import pytest

from framework.core import ai_traffic as at


class _Cur:
    def __init__(self, conn):
        self.conn = conn
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.conn.executed.append((sql, params))
        if "information_schema.columns" in sql:
            self.rows = [(c,) for c in self.conn.columns]
        elif "statement_timeout" in sql:
            self.rows = []
        else:
            self.rows = list(self.conn.rows)

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class _Conn:
    def __init__(self, columns=(), rows=()):
        self.columns = list(columns)
        self.rows = list(rows)
        self.executed = []

    def cursor(self):
        return _Cur(self)


SP_COLS = ["id", "ts", "kind", "source", "path", "status_code", "user_agent",
           "referer", "ip_address", "site_id"]
AP_COLS = ["id", "ts", "kind", "source", "path", "method", "user_agent",
           "referer", "ip"]


def _main_sql(conn):
    return [s for s, _ in conn.executed if "WITH hits" in s][0]


def test_resolve_columns_per_site_schema():
    sp = at.resolve_columns(_Conn(SP_COLS), at.config())
    assert (sp["ip_column"], sp["status_column"], sp["verified_column"]) == \
        ("ip_address", "status_code", "")
    ap = at.resolve_columns(_Conn(AP_COLS), at.config())
    assert (ap["ip_column"], ap["status_column"]) == ("ip", "")


def test_sql_spoof_filter_status_and_prefix():
    cfg = at.resolve_columns(_Conn(SP_COLS), at.config())
    sql, params = at.build_landed_paths_sql(cfg, prefixes=["/product/"])
    assert "spoof AS MATERIALIZED" in sql
    assert "l.status_code = 200" in sql
    assert "LIKE ANY(%(prefix_likes)s)" in sql
    assert params["prefix_likes"] == ["/product/%"]
    # training crawlers are never landings
    assert "gptbot" not in params["fetch_sources"]
    assert "chatgpt-user" in params["fetch_sources"]
    # no literal '%' outside placeholders: psycopg2 would choke on it
    stripped = sql
    for k in params:
        stripped = stripped.replace(f"%({k})s", "")
    stripped = stripped.replace("%(limit)s", "")
    assert "%" not in stripped


def test_null_status_rows_still_count():
    # A status column added to a live table leaves every older row NULL.
    # Those rows are unknown, not failures; `status = 200` alone would drop
    # the whole history the moment the column appears (aisleprompt, 2026-09-25).
    cfg = at.resolve_columns(_Conn(AP_COLS + ["status_code", "duration_ms"]), at.config())
    assert cfg["status_column"] == "status_code"
    sql, _ = at.build_landed_paths_sql(cfg)
    assert "(l.status_code IS NULL OR l.status_code = 200)" in sql
    rsql, _ = at.build_referral_counts_sql(cfg)
    assert "(l.status_code IS NULL OR l.status_code = 200)" in rsql


def test_sql_without_status_column_skips_status_filter():
    cfg = at.resolve_columns(_Conn(AP_COLS), at.config())
    sql, _ = at.build_landed_paths_sql(cfg)
    assert "spoof AS MATERIALIZED" in sql          # ip column is `ip` here
    assert "SELECT s.ip AS ip" in sql
    assert "status" not in sql.split("FROM ai_traffic_log l", 1)[1]


def test_sql_without_ip_column_has_no_spoof_cte():
    cols = [c for c in AP_COLS if c != "ip"]
    cfg = at.resolve_columns(_Conn(cols), at.config())
    sql, _ = at.build_landed_paths_sql(cfg)
    assert "spoof" not in sql
    assert "NULL::text AS ip" in sql


def test_verified_column_is_honored_when_present():
    cfg = at.resolve_columns(_Conn(SP_COLS + ["ip_verified"]), at.config())
    sql, _ = at.build_landed_paths_sql(cfg)
    assert "l.ip_verified IS NOT FALSE" in sql


def test_like_prefix_escapes_wildcards():
    assert at._like_prefix("/a_b%/") == "/a\\_b\\%/%"


def test_unsafe_identifier_rejected():
    cfg = at.config({"table": "ai_traffic_log; DROP TABLE x", "ip_column": "",
                     "status_column": "", "verified_column": ""})
    with pytest.raises(ValueError):
        at.build_landed_paths_sql(cfg)


def test_exclude_ips_from_env(monkeypatch):
    monkeypatch.setenv("AI_TRAFFIC_EXCLUDE_IPS", "1.2.3.4, 5.6.7.8")
    cfg = at.resolve_columns(_Conn(SP_COLS), at.config({"exclude_ips": ["9.9.9.9"]}))
    sql, params = at.build_landed_paths_sql(cfg)
    assert params["exclude_ips"] == ["1.2.3.4", "5.6.7.8", "9.9.9.9"]
    assert "NOT (l.ip_address = ANY(%(exclude_ips)s))" in sql


def test_landed_paths_normalizes_merges_and_scores():
    conn = _Conn(SP_COLS, rows=[
        ("/product/B0AAAAAAAA", 1, 2),
        ("/product/B0AAAAAAAA/", 0, 3),      # trailing slash → same page
        ("/reviews/x", 0, 12),
        ("/reviews/y", 0, 0),
    ])
    # dict rows (RealDictCursor) are accepted too
    conn.rows[3] = {"path": "/reviews/y", "referrals": 2, "fetches": 0}
    rows = at.landed_paths(conn, limit=10)
    by = {r["path"]: r for r in rows}
    assert by["/product/B0AAAAAAAA"]["referrals"] == 1
    assert by["/product/B0AAAAAAAA"]["fetches"] == 5
    assert by["/product/B0AAAAAAAA"]["score"] == 1 * 5 + 5
    assert [r["path"] for r in rows] == ["/reviews/x", "/product/B0AAAAAAAA",
                                         "/reviews/y"]
    # a statement_timeout is always set before the heavy query
    assert any("statement_timeout" in s for s, _ in conn.executed)
    assert "LIMIT %(limit)s" in _main_sql(conn)


def test_path_keys_extracts_first_segment_and_filters():
    rows = [{"path": "/product/B0AAAAAAAA"}, {"path": "/product/EBAY_123"},
            {"path": "/product/B0AAAAAAAA?x=1"}, {"path": "/reviews/z"},
            {"path": "/product/B0BBBBBBBB/specs"}]
    assert at.path_keys(rows, "/product/") == ["B0AAAAAAAA", "EBAY_123", "B0BBBBBBBB"]
    assert at.path_keys(rows, "/product/",
                        key_regex=r"[A-Z0-9]{10}") == ["B0AAAAAAAA", "B0BBBBBBBB"]


def test_referral_counts_sql_and_parse():
    cfg = at.resolve_columns(_Conn(SP_COLS), at.config({"referral_sources": ["chatgpt"]}))
    sql, params = at.build_referral_counts_sql(cfg, windows=(30, 7))
    assert "AS last_7d" in sql and "AS last_30d" in sql
    assert params["max_days"] == 30
    assert "l.status_code = 200" in sql
    assert "l.source = ANY(%(ref_sources)s)" in sql

    conn = _Conn(SP_COLS, rows=[(3, 11)])
    assert at.referral_counts(conn, windows=(7, 30)) == {"last_7d": 3, "last_30d": 11}
    conn.rows = [{"last_7d": 1, "last_30d": 2}]
    assert at.referral_counts(conn) == {"last_7d": 1, "last_30d": 2}


def test_cluster_yield_normalises_by_article_count():
    articles = [{"slug": f"qwen-{i}", "title": "Run Qwen locally"} for i in range(10)] + \
               [{"slug": f"best-mouse-{i}", "title": "Best gaming mouse"} for i in range(40)] + \
               [{"slug": "misc-thing", "title": "Something else"}]
    landed = [
        {"path": "/reviews/qwen-1", "referrals": 3, "fetches": 5},
        {"path": "/reviews/qwen-2/", "referrals": 1, "fetches": 0},
        {"path": "/reviews/best-mouse-3", "referrals": 4, "fetches": 20},
        {"path": "/product/B0AAAAAAAA", "referrals": 9, "fetches": 9},   # not an article
        {"path": "/reviews/unknown-slug", "referrals": 5, "fetches": 5},  # not published
    ]
    clusters = [{"name": "llm", "pattern": r"qwen|llama|llm"},
                {"name": "peripherals", "pattern": r"mouse|keyboard"}]
    out = at.cluster_yield(landed, articles, clusters, path_template="/reviews/{slug}")
    by = {c["cluster"]: c for c in out}
    assert by["llm"]["articles"] == 10 and by["llm"]["referrals"] == 4
    assert by["llm"]["referrals_per_100"] == 40.0
    assert by["peripherals"]["referrals"] == 4 and by["peripherals"]["referrals_per_100"] == 10.0
    assert by["other"]["articles"] == 1 and by["other"]["referrals"] == 0
    # ranked by yield, not by raw hits (both clusters have 4 referrals)
    assert [c["cluster"] for c in out][:2] == ["llm", "peripherals"]
