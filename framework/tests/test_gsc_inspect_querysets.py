"""gsc-coverage-auditor builds its URL universe from the sites.json it shares
with indexnow-submitter. Incremental-only query sets (no bulkSql) must be
skipped, not crash the run.

Regression for 2026-11-11: aisleprompt's cyber-week-click-guides set
(incrementalOnly + incrementalSql) made inspect.py die on KeyError: 'bulkSql'
before it inspected a single URL, two days in a row."""
from __future__ import annotations

import importlib.util
from pathlib import Path

INSPECT = Path(__file__).resolve().parents[2] / "agents" / "gsc-coverage-auditor" / "inspect.py"


def _load():
    # The file is named inspect.py, which shadows the stdlib module; load it
    # under a different name.
    spec = importlib.util.spec_from_file_location("gsc_coverage_inspect", INSPECT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Cur:
    def __init__(self, rows):
        self._rows = rows
        self.description = [("slug",)]
        self.executed = []

    def execute(self, sql):
        self.executed.append(sql)

    def fetchall(self):
        return self._rows

    def close(self):
        pass


class _Conn:
    def __init__(self):
        self.cursors = []

    def cursor(self):
        c = _Cur([("best-coffee-makers-2026",), ("best-air-fryers-2026",)])
        self.cursors.append(c)
        return c

    def rollback(self):
        pass

    def close(self):
        pass


def test_incremental_only_query_sets_are_skipped(monkeypatch):
    mod = _load()
    conn = _Conn()
    monkeypatch.setattr(mod.psycopg2, "connect", lambda url: conn)
    monkeypatch.setenv("TEST_GSC_DSN", "postgres://example.invalid/db")
    site = {
        "host": "example.com",
        "databaseUrlEnv": "TEST_GSC_DSN",
        "querySets": [
            {"name": "blog", "bulkSql": "SELECT slug FROM editorial_articles", "urlPrefix": "/blog/",
             "urlTemplate": "slug"},
            {"name": "cyber-week", "incrementalOnly": True,
             "incrementalSql": "SELECT slug, now() AS lastmod FROM editorial_articles", "urlPrefix": "/blog/",
             "urlTemplate": "slug"},
        ],
    }
    urls = mod.collect_urls_from_db(site)
    assert urls == {"https://example.com/blog/best-coffee-makers-2026",
                    "https://example.com/blog/best-air-fryers-2026"}
    # Only the bulk set ran a query.
    assert len(conn.cursors) == 1
