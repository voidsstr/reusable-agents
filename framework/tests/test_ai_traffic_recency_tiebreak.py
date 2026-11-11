"""ai_traffic.landed_paths: equal scores are ordered by the most recent hit.

2026-11-11: 4,071 of 5,642 answer-time /product/ pages on one site scored
exactly 1, and the old `ORDER BY score, path` tie-break made every cap keep the
alphabetically-first ASINs, so the price verifier's 6,000-ASIN AI arm dropped
the NEWEST products (B0DW... onward). Recency means a cap drops the oldest.
"""
from datetime import datetime, timedelta, timezone

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
        elif "WITH hits" in sql:
            self.rows = list(self.conn.rows)
        else:
            self.rows = []

    def fetchall(self):
        return self.rows


class _Conn:
    def __init__(self, columns=(), rows=()):
        self.columns = list(columns)
        self.rows = list(rows)
        self.executed = []

    def cursor(self):
        return _Cur(self)


SP_COLS = ["id", "ts", "kind", "source", "path", "status_code", "user_agent",
           "referer", "ip_address", "site_id"]
NOW = datetime(2026, 11, 11, 17, 0, tzinfo=timezone.utc)


def test_sql_breaks_score_ties_by_latest_hit_then_path():
    cfg = at.resolve_columns(_Conn(SP_COLS), at.config())
    sql, params = at.build_landed_paths_sql(cfg, prefixes=["/product/"])
    assert "l.ts AS hit_ts" in sql
    assert "MAX(hit_ts)                             AS last_hit" in sql
    assert "MAX(hit_ts) DESC NULLS LAST, path" in sql
    # a site whose timestamp column is named differently still works
    cfg2 = at.resolve_columns(_Conn(SP_COLS), at.config({"ts_column": "logged_at"}))
    sql2, _ = at.build_landed_paths_sql(cfg2)
    assert "l.logged_at AS hit_ts" in sql2
    stripped = sql
    for k in list(params) + ["limit"]:
        stripped = stripped.replace(f"%({k})s", "")
    assert "%" not in stripped


def test_equal_scores_newest_first_and_cap_drops_oldest():
    rows = [
        ("/product/B0AAAAAAA1", 0, 1, NOW - timedelta(days=20)),   # oldest
        ("/product/B0HZZZZZZ9", 0, 1, NOW - timedelta(hours=2)),   # newest
        ("/product/B0DMMMMMM5", 0, 1, NOW - timedelta(days=3)),
        ("/product/B0TOPTOP01", 0, 4, NOW - timedelta(days=60)),   # higher score wins
    ]
    rows_out = at.landed_paths(_Conn(SP_COLS, rows), limit=10)
    assert [r["path"] for r in rows_out] == [
        "/product/B0TOPTOP01", "/product/B0HZZZZZZ9", "/product/B0DMMMMMM5",
        "/product/B0AAAAAAA1"]
    capped = at.landed_paths(_Conn(SP_COLS, rows), limit=3)
    assert "/product/B0AAAAAAA1" not in [r["path"] for r in capped]
    # the output contract is unchanged: no timestamp leaks into the rows
    assert set(rows_out[0]) == {"path", "referrals", "fetches", "score"}


def test_merged_paths_take_their_newest_hit_and_undated_rows_sort_last():
    rows = [
        {"path": "/product/B0AAAAAAA1", "referrals": 0, "fetches": 1,
         "last_hit": NOW - timedelta(days=9)},
        {"path": "/product/B0AAAAAAA1/", "referrals": 0, "fetches": 1,
         "last_hit": NOW - timedelta(hours=1)},          # merges; newest hit wins
        {"path": "/product/B0BBBBBBB2", "referrals": 0, "fetches": 2,
         "last_hit": NOW - timedelta(days=2)},
        ("/product/B0CCCCCCC3", 0, 2),                   # 3-tuple: no timestamp
        {"path": "/product/B0DDDDDDD4", "referrals": 0, "fetches": 2,
         "last_hit": (NOW - timedelta(days=1)).isoformat()},
    ]
    out = [r["path"] for r in at.landed_paths(_Conn(SP_COLS, rows), limit=10)]
    assert out == ["/product/B0AAAAAAA1", "/product/B0DDDDDDD4",
                   "/product/B0BBBBBBB2", "/product/B0CCCCCCC3"]


def test_without_timestamps_the_old_path_order_holds():
    rows = [("/reviews/y", 0, 3), ("/product/B0AAAAAAA1", 0, 3), ("/a", 0, 3)]
    out = [r["path"] for r in at.landed_paths(_Conn(SP_COLS, rows), limit=10)]
    assert out == ["/a", "/product/B0AAAAAAA1", "/reviews/y"]
