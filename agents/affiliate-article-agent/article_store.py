"""Site database access for the article engine. Every SQL string except the
article INSERT / UPDATE comes from the site's config; every read runs on a
READ ONLY session. The DSN is read from the env var the settings name and
never leaves :func:`connect`.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Iterable, Mapping, Optional, Sequence

from framework.core import facts_pack as fp

_ROOT_KEY = re.compile(r"^(/[a-z0-9-]+/)([A-Za-z0-9][A-Za-z0-9-]*)/?$")


class SiteDBError(RuntimeError):
    """The site DB could not be reached (text never carries the DSN)."""


def connect(dsn_env: str, *, readonly: bool, statement_timeout_ms: int = 30000, application_name: str = "",
            environ: Optional[Mapping[str, str]] = None):
    env = os.environ if environ is None else environ
    dsn = str(env.get(dsn_env, "") or "")
    if not dsn:
        raise SiteDBError(f"${dsn_env} is not set (the site's Postgres DSN)")
    from framework.core import pg_conn
    conn, failure = None, ""
    try:
        conn = pg_conn.connect(dsn, connect_timeout=15, application_name=(application_name or "article-engine")[:63],
                               options=f"-c statement_timeout={int(statement_timeout_ms)}")
    except Exception as exc:  # noqa: BLE001 - re-raised without the driver text (it can echo the DSN)
        failure = f"cannot connect to the site DB (${dsn_env}): {type(exc).__name__}"
    if conn is None:
        raise SiteDBError(failure or f"cannot connect to the site DB (${dsn_env})")
    if readonly:
        conn.set_session(readonly=True, autocommit=True)
    return conn


class SiteStore:
    """Reads (read-only connection) and the one write path (INSERT / UPDATE of
    the article row, on a separate read-write connection opened on demand)."""

    def __init__(self, settings, ro_conn, rw_factory=None, ro_factory=None):
        self.s = settings
        self.ro = ro_conn
        self._rw_factory = rw_factory
        self._ro_factory = ro_factory
        self._rw = None

    def _ro(self):
        """The read-only connection, reopened when a long model call let the
        server's idle timeout close it."""
        if self._ro_factory is not None:
            from framework.core import pg_conn
            if not pg_conn.is_open(self.ro):
                try:
                    self.ro.close()
                except Exception:  # noqa: BLE001
                    pass
                self.ro = self._ro_factory()
        return self.ro

    # ---- reads -------------------------------------------------------------
    def _rows(self, sql: str, params: Mapping[str, Any] | None = None) -> list[tuple]:
        with self._ro().cursor() as cur:
            cur.execute(sql, dict(params or {}))
            return cur.fetchall() if cur.description else []

    def count(self, check: str, params: Mapping[str, Any]) -> int:
        sql = self.s.readiness_checks.get(check)
        if not sql:
            raise KeyError(f"readiness check {check!r} is not defined in readiness.checks")
        rows = self._rows(sql, params)
        return int(rows[0][0] or 0) if rows else 0

    def done_slugs(self) -> list[str]:
        return sorted(str(r[0]) for r in self._rows(self.s.signals["published_sql"]) if r and r[0])

    def data_signal(self) -> list:
        return [fp._jsonable(v) for v in (self._rows(self.s.signals["data_sql"]) or [()])[0]]

    def facts(self, params: Mapping[str, Any]) -> fp.FactsPack:
        return fp.build(self._ro(), self.s.fact_queries, params, forbidden_relations=self.s.forbidden_relations)

    def indexable_paths(self, paths: Iterable[str]) -> set[str]:
        """The subset of `paths` that are live, indexable pages (the site's
        indexable view) or configured static pages."""
        out, want = set(), {}
        statics = {p.rstrip("/") or "/" for p in self.s.static_paths}
        for p in set(paths):
            clean = p.split("#", 1)[0].split("?", 1)[0]
            if (clean.rstrip("/") or "/") in statics:
                out.add(p)
                continue
            m = _ROOT_KEY.match(clean)
            if m and m.group(1) in self.s.link_families:
                want.setdefault((self.s.link_families[m.group(1)], m.group(2)), []).append(p)
        if want:
            fams = sorted({f for f, _ in want})
            keys = sorted({k for _, k in want})
            for fam, key in self._rows(self.s.indexable_sql, {"families": fams, "keys": keys}):
                for p in want.get((str(fam), str(key)), []):
                    out.add(p)
        return out

    def placeholder_resolves(self, kind: str, key: str) -> bool:
        spec = self.s.placeholder_kinds.get(kind) or {}
        if not spec.get("resolve_sql"):
            return False
        return bool(self._rows(spec["resolve_sql"], {"key": key}))

    def eligible_asins(self, asins: Sequence[str]) -> set[str]:
        sql = self.s.buystrip.get("eligible_sql")
        if not sql or not asins:
            return set()
        return {str(r[0]) for r in self._rows(sql, {"asins": list(asins)})}

    def own_pending_slugs(self, agent_id: str, limit: int = 2) -> list[str]:
        """This engine's own rows still in the queue status (held by the
        publish gate, or never evaluated), oldest first."""
        a = self.s.articles
        rows = self._rows(f"SELECT slug FROM {a['table']} WHERE written_by = %(w)s AND status = %(st)s "
                          "ORDER BY created_at, slug LIMIT %(n)s",
                          {"w": agent_id[:100], "st": a["insert_status"], "n": int(limit)})
        return [str(r[0]) for r in rows]

    def row_status(self, slug: str) -> str:
        t = self.s.articles["table"]
        rows = self._rows(f"SELECT status FROM {t} WHERE slug = %(slug)s", {"slug": slug})
        return str(rows[0][0]) if rows else ""

    # ---- writes ------------------------------------------------------------
    def rw(self):
        if self._rw is None:
            if self._rw_factory is None:
                raise SiteDBError("no write connection (dry run?)")
            self._rw = self._rw_factory()
        return self._rw

    def insert_article(self, row: Mapping[str, Any]) -> bool:
        """INSERT the draft as the queue status. False when the slug exists
        (nothing written). The site's publish guard forces the status anyway."""
        a = self.s.articles
        conn = self.rw()
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {a['table']} (slug, title, subtitle, excerpt, body_md, category, tags, "
                "related_product_asins, faqs, outbound_citations, author, written_by, status, hero_image_url, "
                "primary_keyword, estimated_read_time) "
                "VALUES (%(slug)s, %(title)s, %(subtitle)s, %(excerpt)s, %(body_md)s, %(category)s, %(tags)s, "
                "%(asins)s, %(faqs)s::jsonb, %(citations)s, %(author)s, %(written_by)s, %(status)s, NULL, "
                "%(primary_keyword)s, %(read_time)s) ON CONFLICT (slug) DO NOTHING RETURNING id",
                {**row, "faqs": json.dumps(row.get("faqs") or []), "category": a["category"],
                 "author": a["byline"], "status": a["insert_status"]})
            got = cur.fetchone()
        conn.commit()
        return got is not None

    def update_article(self, slug: str, row: Mapping[str, Any]) -> int:
        """Rewrite a held draft's text (it stays in the queue status)."""
        a = self.s.articles
        conn = self.rw()
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE {a['table']} SET title = %(title)s, subtitle = %(subtitle)s, excerpt = %(excerpt)s, "
                "body_md = %(body_md)s, tags = %(tags)s, related_product_asins = %(asins)s, faqs = %(faqs)s::jsonb, "
                "outbound_citations = %(citations)s WHERE slug = %(slug)s AND status = %(status)s",
                {**row, "slug": slug, "faqs": json.dumps(row.get("faqs") or []), "status": a["insert_status"]})
            n = cur.rowcount
        conn.commit()
        return n

    def close(self) -> None:
        for c in (self.ro, self._rw):
            try:
                if c is not None:
                    c.close()
            except Exception:  # noqa: BLE001
                pass
