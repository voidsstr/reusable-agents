"""Database side of the spec-extractor engine: the TABLE CONTRACT in SQL.

Every table and column name comes from the site's settings (validated
lower-case identifiers, emitted double-quoted); trusted WHERE fragments come
from the same reviewed config. Nothing here knows a niche or a site.

Ownership rules the SQL enforces:

* ``spec_sources.<state>`` (jsonb) is engine-owned bookkeeping: the last
  fetch record, the extraction fingerprint, the source's candidate values
  (deterministic and LLM) and retry backoff. ``fetched_at`` and
  ``content_sha256`` describe the content the stored candidates came from.
  Rows themselves (model, kind, url, publisher) are seeded by the site.
* ``spec_values`` rows are engine output, rewritten per model, EXCEPT
  operator rows: ``extractor = 'manual'`` with ``extractor_id`` NULL, empty,
  or starting with ``manual-entry``. Those are read as manual candidates
  (they always win consolidation), never deleted, and only their ``status``
  is updated. Engine rows always carry a non-empty ``extractor_id`` that
  does not start with ``manual-entry`` (an operator's adjudication decision
  is stored as an engine row with extractor 'manual' and extractor_id
  'manual:<...>').
* Typed spec tables, model status columns and runtime estimates are fully
  engine-owned projections. A typed table holds rows only for models of
  the kinds mapped to it (:meth:`SpecStore.sweep_typed`).
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from spec_extractor_settings import KindSettings, Settings, quote_ident as q

OPERATOR_PREFIX = "manual-entry"


def _sql_list(cols: Sequence[str]) -> str:
    return ", ".join(cols)


def num(v: Any) -> Optional[float]:
    """Decimal / int / float -> float (None stays None)."""
    if v is None:
        return None
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float, Decimal)):
        f = float(v)
        return None if math.isnan(f) else f
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def iso(v: Any) -> str:
    if v is None or v == "":
        return ""
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return str(v)


@dataclass
class ModelRow:
    id: Any
    kind: str
    brand: str = ""
    name: str = ""
    model_number: str = ""
    aliases: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        return str(self.id)


@dataclass
class SourceRow:
    """One spec_sources row. ``state`` is the engine bookkeeping without the
    candidate list; ``candidates`` is loaded only when a model is
    consolidated. ``new_candidates`` (when set) replaces the stored list on
    save; ``column_update`` carries fetched_at / content_sha256 changes."""
    id: Any
    model_id: Any
    kind: str
    url: str
    publisher: str = ""
    fetched_at: str = ""
    content_sha256: str = ""
    state: dict = field(default_factory=dict)
    candidates: Optional[list[dict]] = None
    new_candidates: Optional[list[dict]] = None
    column_update: dict = field(default_factory=dict)
    dirty: bool = False

    @property
    def key(self) -> str:
        return str(self.id)

    @property
    def model_key(self) -> str:
        return str(self.model_id)

    def current_candidates(self) -> list[dict]:
        if self.new_candidates is not None:
            return self.new_candidates
        return list(self.candidates or [])


class ConnectionLost(RuntimeError):
    """The site DB connection dropped and could not be replaced safely."""


class SpecStore:
    """Reads and writes one site's spec tables through a DB-API connection
    the caller owns (psycopg2). Each write method is its own transaction.

    ``reconnect`` (optional): a callable returning a fresh connection.
    :meth:`ensure_open` uses it between phases, after long HTTP / LLM work
    that an idle-timeout may have outlived, and takes the run lock again on
    the new session (the old session's lock died with it)."""

    def __init__(self, conn: Any, settings: Settings, *, reconnect: Optional[Callable[[], Any]] = None):
        self.conn = conn
        self.s = settings
        self.m = settings.models
        self.src = settings.sources
        self.val = settings.values
        self._reconnect = reconnect
        self._held_lock: Optional[str] = None

    # ------------------------------------------------------------------
    # plumbing
    # ------------------------------------------------------------------
    def _rows(self, sql: str, params: Optional[Mapping[str, Any]] = None) -> list[dict]:
        with self.conn.cursor() as cur:
            cur.execute(sql, dict(params or {}))
            names = [d[0] for d in cur.description]
            return [dict(zip(names, r)) for r in cur.fetchall()]

    def end_read(self) -> None:
        """Close the implicit read transaction (no session idles in one
        across slow HTTP or LLM work)."""
        try:
            self.conn.rollback()
        except Exception:  # noqa: BLE001
            pass

    def try_lock(self, key: str) -> bool:
        with self.conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", (key,))
            got = bool(cur.fetchone()[0])
        self.conn.commit()
        if got:
            self._held_lock = key
        return got

    def unlock(self, key: str) -> None:
        try:
            self.conn.rollback()
            with self.conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (key,))
            self.conn.commit()
        except Exception:  # noqa: BLE001 - closing the session releases it anyway
            pass
        if self._held_lock == key:
            self._held_lock = None

    def ensure_open(self) -> bool:
        """Probe the connection (framework.core.pg_conn.is_open); when it
        dropped, open a new one and take the run lock again. True when the
        connection was replaced. Raises ConnectionLost when there is no way
        to reconnect or another run took the lock in the meantime."""
        from framework.core import pg_conn
        if pg_conn.is_open(self.conn):
            return False
        if self._reconnect is None:
            raise ConnectionLost("the site DB connection dropped (no reconnect configured)")
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass
        self.conn = self._reconnect()
        if self._held_lock is not None:
            key = self._held_lock
            self._held_lock = None
            if not self.try_lock(key):
                raise ConnectionLost("the site DB connection dropped and another run took the lock "
                                     "before this one reconnected: stopping")
        return True

    def _mcol(self, logical: str, alias: str, prefix: str = "m") -> str:
        col = self.m.col(logical)
        return f"{prefix}.{q(col)} AS {alias}" if col else f"NULL AS {alias}"

    def _operator_pred(self, alias: str = "v") -> str:
        c = self.val.columns
        ext, eid = q(c["extractor"]), q(c["extractor_id"])
        return (f"({alias}.{ext} = 'manual' AND ({alias}.{eid} IS NULL OR {alias}.{eid} = '' "
                f"OR {alias}.{eid} LIKE '{OPERATOR_PREFIX}%%'))")

    # ------------------------------------------------------------------
    # models and sources
    # ------------------------------------------------------------------
    def models_sql(self) -> str:
        cols = [self._mcol(k, k) for k in ("id", "kind", "brand", "name", "model_number", "aliases")]
        where = f"m.{q(self.m.col('kind'))} = ANY(%(kinds)s)"
        if self.m.where:
            where += f" AND ({self.m.where})"
        return (f"SELECT {_sql_list(cols)} FROM {q(self.m.name)} m WHERE {where} "
                f"ORDER BY m.{q(self.m.col('id'))}")

    def load_models(self, kinds: Iterable[str]) -> list[ModelRow]:
        kinds = sorted(set(kinds))
        if not kinds:
            return []
        out = []
        for r in self._rows(self.models_sql(), {"kinds": kinds}):
            aliases = r.get("aliases")
            if isinstance(aliases, str):
                aliases = [a.strip() for a in aliases.split(",") if a.strip()]
            out.append(ModelRow(id=r["id"], kind=str(r["kind"] or ""), brand=str(r.get("brand") or ""),
                                name=str(r.get("name") or ""),
                                model_number=str(r.get("model_number") or ""),
                                aliases=tuple(str(a) for a in (aliases or []) if a)))
        return out

    def sources_sql(self) -> str:
        c = self.src.columns
        state = q(c["state"])
        pub = f"s.{q(c['publisher'])}" if c.get("publisher") else "NULL"
        return (f"SELECT s.{q(c['id'])} AS id, s.{q(c['model_id'])} AS model_id, "
                f"s.{q(c['kind'])} AS kind, s.{q(c['url'])} AS url, {pub} AS publisher, "
                f"s.{q(c['fetched_at'])} AS fetched_at, s.{q(c['content_sha256'])} AS content_sha256, "
                f"(COALESCE(s.{state}, '{{}}'::jsonb) - 'candidates') AS state "
                f"FROM {q(self.src.name)} s WHERE s.{q(c['model_id'])} = ANY(%(ids)s) "
                f"ORDER BY s.{q(c['id'])}")

    def load_sources(self, model_ids: Sequence[Any]) -> list[SourceRow]:
        out: list[SourceRow] = []
        for chunk in _chunks(list(model_ids), 1000):
            for r in self._rows(self.sources_sql(), {"ids": chunk}):
                st = r.get("state") if isinstance(r.get("state"), dict) else {}
                out.append(SourceRow(id=r["id"], model_id=r["model_id"], kind=str(r["kind"] or ""),
                                     url=str(r["url"] or ""), publisher=str(r.get("publisher") or ""),
                                     fetched_at=iso(r.get("fetched_at")),
                                     content_sha256=str(r.get("content_sha256") or ""),
                                     state=dict(st)))
        return out

    def load_candidates(self, source_ids: Sequence[Any]) -> dict[str, list[dict]]:
        c = self.src.columns
        sql = (f"SELECT s.{q(c['id'])} AS id, "
               f"COALESCE(s.{q(c['state'])} -> 'candidates', '[]'::jsonb) AS candidates "
               f"FROM {q(self.src.name)} s WHERE s.{q(c['id'])} = ANY(%(ids)s)")
        out: dict[str, list[dict]] = {}
        for chunk in _chunks(list(source_ids), 500):
            for r in self._rows(sql, {"ids": chunk}):
                cands = r.get("candidates")
                out[str(r["id"])] = list(cands) if isinstance(cands, list) else []
        return out

    def save_sources(self, rows: Sequence[SourceRow]) -> int:
        """Persist the bookkeeping of every dirty source (one transaction)."""
        dirty = [r for r in rows if r.dirty]
        if not dirty:
            return 0
        c = self.src.columns
        state = q(c["state"])
        with self.conn:
            with self.conn.cursor() as cur:
                for r in dirty:
                    # jsonb || replaces top-level keys: the stored candidate
                    # list survives unless this run replaced it.
                    patch = {k: v for k, v in r.state.items() if k != "candidates"}
                    if r.new_candidates is not None:
                        patch["candidates"] = r.new_candidates
                    sets = [f"{state} = COALESCE({state}, '{{}}'::jsonb) || %(patch)s::jsonb"]
                    params: dict[str, Any] = {"patch": json.dumps(patch, default=str), "id": r.id}
                    if "fetched_at" in r.column_update:
                        sets.append(f"{q(c['fetched_at'])} = %(fetched_at)s::timestamptz")
                        params["fetched_at"] = r.column_update["fetched_at"] or None
                    if "content_sha256" in r.column_update:
                        sets.append(f"{q(c['content_sha256'])} = %(sha)s")
                        params["sha"] = r.column_update["content_sha256"] or None
                    cur.execute(f"UPDATE {q(self.src.name)} SET {', '.join(sets)} "
                                f"WHERE {q(c['id'])} = %(id)s", params)
        for r in dirty:
            if r.new_candidates is not None:
                r.candidates = r.new_candidates
                r.new_candidates = None
            for k, v in r.column_update.items():
                setattr(r, k, v or "")
            r.column_update = {}
            r.dirty = False
        return len(dirty)

    def priority_model_ids(self, sql: str) -> list[str]:
        if not sql:
            return []
        with self.conn.cursor() as cur:
            cur.execute(sql)
            return [str(r[0]) for r in cur.fetchall() if r and r[0] is not None]

    # ------------------------------------------------------------------
    # spec_values
    # ------------------------------------------------------------------
    def _value_select(self, alias: str = "v") -> str:
        c = self.val.columns
        parts = []
        for logical in ("id", "model_id", "field", "value_num", "value_text", "unit", "source_id",
                        "extractor", "extractor_id", "confidence", "status", "snippet", "qualifier",
                        "conflict_id"):
            col = c.get(logical)
            parts.append(f"{alias}.{q(col)} AS {logical}" if col else f"NULL AS {logical}")
        return _sql_list(parts)

    def load_operator_values(self, model_ids: Sequence[Any]) -> list[dict]:
        if not model_ids:
            return []
        c = self.val.columns
        sql = (f"SELECT {self._value_select()} FROM {q(self.val.name)} v "
               f"WHERE v.{q(c['model_id'])} = ANY(%(ids)s) AND {self._operator_pred()}")
        out = []
        for chunk in _chunks(list(model_ids), 1000):
            out += self._rows(sql, {"ids": chunk})
        return out

    def load_engine_values(self, model_ids: Sequence[Any]) -> dict[str, list[dict]]:
        c = self.val.columns
        sql = (f"SELECT {self._value_select()} FROM {q(self.val.name)} v "
               f"WHERE v.{q(c['model_id'])} = ANY(%(ids)s) AND NOT {self._operator_pred()}")
        out: dict[str, list[dict]] = {}
        for chunk in _chunks(list(model_ids), 1000):
            for r in self._rows(sql, {"ids": chunk}):
                out.setdefault(str(r["model_id"]), []).append(r)
        return out

    def value_row_key(self, row: Mapping[str, Any]) -> tuple:
        """Comparable identity of an engine row (only the mapped columns)."""
        c = self.val.columns
        key = [row.get("field"), _round(num(row.get("value_num")), 6), row.get("value_text") or None,
               row.get("unit") or None, str(row.get("source_id")), row.get("extractor"),
               row.get("extractor_id"), _round(num(row.get("confidence")), 4), row.get("status")]
        for opt in ("snippet", "qualifier", "conflict_id"):
            if c.get(opt):
                key.append(row.get(opt) or None)
        return tuple(key)

    def write_model(self, model: ModelRow, engine_rows: Sequence[Mapping[str, Any]],
                    existing: Sequence[Mapping[str, Any]],
                    operator_status: Mapping[Any, str],
                    operator_rows: Sequence[Mapping[str, Any]],
                    typed: Optional[tuple[KindSettings, Mapping[str, Any], float]] = None,
                    status: Optional[tuple[str, float]] = None,
                    other_typed: Sequence[KindSettings] = ()) -> dict:
        """One model's outputs in one transaction. Engine rows are replaced
        only when they differ from what is stored; operator rows only get
        their status updated. ``other_typed``: the typed tables of the other
        configured kinds; a row of this model there (left by a kind change)
        is deleted."""
        c = self.val.columns
        stats = {"values_inserted": 0, "values_deleted": 0, "operator_status_updates": 0,
                 "typed_upserts": 0, "typed_deletes": 0, "status_updates": 0}
        own = typed[0].typed.name if typed is not None and typed[0].typed is not None else None
        want = sorted((self.value_row_key(r) for r in engine_rows), key=repr)
        have = sorted((self.value_row_key(r) for r in existing), key=repr)
        with self.conn:
            with self.conn.cursor() as cur:
                if want != have:
                    cur.execute(f"DELETE FROM {q(self.val.name)} v WHERE v.{q(c['model_id'])} = %(id)s "
                                f"AND NOT {self._operator_pred()}", {"id": model.id})
                    stats["values_deleted"] = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
                    if engine_rows:
                        stats["values_inserted"] = self._insert_values(cur, engine_rows)
                by_id = {str(r["id"]): r for r in operator_rows}
                for rid, st in operator_status.items():
                    cur_row = by_id.get(str(rid))
                    if cur_row is None or cur_row.get("status") == st or not c.get("id"):
                        continue
                    cur.execute(f"UPDATE {q(self.val.name)} SET {q(c['status'])} = %(st)s "
                                f"WHERE {q(c['id'])} = %(id)s", {"st": st, "id": cur_row["id"]})
                    stats["operator_status_updates"] += 1
                if typed is not None:
                    stats["typed_upserts"] = self._upsert_typed(cur, model, *typed)
                done = {own} if own else set()
                for ks in other_typed:
                    if ks.typed is None or ks.typed.name in done:
                        continue
                    done.add(ks.typed.name)
                    cur.execute(f"DELETE FROM {q(ks.typed.name)} WHERE {q(ks.typed_key)} = %(id)s",
                                {"id": model.id})
                    stats["typed_deletes"] += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
                if status is not None:
                    stats["status_updates"] = self._update_status(cur, model, *status)
        return stats

    def sweep_typed(self, tables: Mapping[tuple[str, str], Sequence[str]]) -> int:
        """Delete every typed row whose model is now of a kind NOT mapped to
        that table: a kind change to any other kind, configured or not
        (:meth:`write_model` only cleans up after models it consolidates).
        ``tables``: {(typed table, key column): the kinds mapped to it}.
        ``tables.models.where`` is not applied, so a retired model of a
        mapped kind keeps its row; a row whose model the relation does not
        show, or whose kind is NULL, is left alone. One transaction; returns
        the number of rows deleted."""
        if not tables:
            return 0
        mid, mkind = q(self.m.col("id")), q(self.m.col("kind"))
        deleted = 0
        with self.conn:
            with self.conn.cursor() as cur:
                for (table, key), kinds in sorted(tables.items()):
                    cur.execute(f"DELETE FROM {q(table)} t USING {q(self.m.name)} m "
                                f"WHERE m.{mid}::text = t.{q(key)}::text "
                                f"AND NOT (m.{mkind} = ANY(%(kinds)s))", {"kinds": sorted(set(kinds))})
                    deleted += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        return deleted

    def _insert_values(self, cur, rows: Sequence[Mapping[str, Any]]) -> int:
        from psycopg2.extras import execute_values
        c = self.val.columns
        logical = ["model_id", "field", "value_num", "value_text", "unit", "source_id", "extractor",
                   "extractor_id", "confidence", "status"]
        logical += [k for k in ("snippet", "qualifier", "conflict_id") if c.get(k)]
        cols = _sql_list(q(c[k]) for k in logical)
        data = [tuple(r.get(k) for k in logical) for r in rows]
        execute_values(cur, f"INSERT INTO {q(self.val.name)} ({cols}) VALUES %s", data, page_size=500)
        return len(data)

    def _upsert_typed(self, cur, model: ModelRow, ks: KindSettings, values: Mapping[str, Any],
                      completeness: float) -> int:
        t = ks.typed
        assert t is not None
        key = q(ks.typed_key)
        cols = [(q(col), values.get(f)) for f, col in sorted(t.columns.items())]
        if ks.typed_completeness:
            cols.append((q(ks.typed_completeness), round(float(completeness), 4)))
        names = [key] + [c for c, _ in cols]
        params = [model.id] + [_adapt(v) for _, v in cols]
        compare = ", ".join(f"{q(t.name)}.{c}" for c, _ in cols) or "NULL"
        excluded = ", ".join(f"EXCLUDED.{c}" for c, _ in cols) or "NULL"
        sets = [f"{c} = EXCLUDED.{c}" for c, _ in cols]
        placeholders = ["%s"] * len(names)
        if ks.typed_updated_at:
            names.append(q(ks.typed_updated_at))
            placeholders.append("now()")
            sets.append(f"{q(ks.typed_updated_at)} = now()")
        sql = (f"INSERT INTO {q(t.name)} ({_sql_list(names)}) VALUES ({_sql_list(placeholders)}) "
               f"ON CONFLICT ({key}) DO UPDATE SET {_sql_list(sets)} "
               f"WHERE ROW({compare}) IS DISTINCT FROM ROW({excluded})")
        cur.execute(sql, params)
        return 1 if cur.rowcount and cur.rowcount > 0 else 0

    def _update_status(self, cur, model: ModelRow, status: str, completeness: float) -> int:
        sc = self.s.model_status_columns
        sets, params = [], {"id": model.id}
        cmp_cols, cmp_vals = [], []
        if sc.get("status"):
            sets.append(f"{q(sc['status'])} = %(status)s")
            params["status"] = status
            cmp_cols.append(q(sc["status"]))
            cmp_vals.append("%(status)s")
        if sc.get("completeness"):
            sets.append(f"{q(sc['completeness'])} = %(completeness)s")
            params["completeness"] = round(float(completeness), 4)
            cmp_cols.append(q(sc["completeness"]))
            cmp_vals.append("%(completeness)s")
        if not sets:
            return 0
        guard = ""
        if sc.get("checked_at"):
            sets.append(f"{q(sc['checked_at'])} = now()")
        elif cmp_cols:
            guard = (f" AND ROW({_sql_list(cmp_cols)}) IS DISTINCT FROM ROW({_sql_list(cmp_vals)})")
        cur.execute(f"UPDATE {q(self.s.models_write_table)} SET {_sql_list(sets)} "
                    f"WHERE {q(self.m.col('id'))} = %(id)s{guard}", params)
        return 1 if cur.rowcount and cur.rowcount > 0 else 0

    # ------------------------------------------------------------------
    # reads for compat, runtime and metrics
    # ------------------------------------------------------------------
    def accepted_rows(self, model_ids: Sequence[Any], fields: Optional[Iterable[str]] = None) -> list[dict]:
        """Accepted spec_values rows (engine and operator) with their source
        metadata: the shape compat_graph.ModelSpec.from_db_rows reads."""
        if not model_ids:
            return []
        c, sc = self.val.columns, self.src.columns
        pub = f"s.{q(sc['publisher'])}" if sc.get("publisher") else "NULL"
        where = f"v.{q(c['model_id'])} = ANY(%(ids)s) AND v.{q(c['status'])} = 'accepted'"
        params: dict[str, Any] = {}
        if fields is not None:
            where += f" AND v.{q(c['field'])} = ANY(%(fields)s)"
            params["fields"] = sorted(set(fields))
        sql = (f"SELECT v.{q(c['model_id'])} AS model_id, v.{q(c['field'])} AS field, "
               f"v.{q(c['value_num'])} AS value_num, v.{q(c['value_text'])} AS value_text, "
               f"v.{q(c['unit'])} AS unit, v.{q(c['source_id'])} AS source_id, "
               f"v.{q(c['status'])} AS status, s.{q(sc['url'])} AS source_url, "
               f"s.{q(sc['kind'])} AS source_kind, {pub} AS publisher, "
               f"s.{q(sc['content_sha256'])} AS content_sha256 "
               f"FROM {q(self.val.name)} v LEFT JOIN {q(self.src.name)} s "
               f"ON s.{q(sc['id'])} = v.{q(c['source_id'])} WHERE {where} "
               f"ORDER BY v.{q(c['model_id'])}, v.{q(c['field'])}")
        out = []
        for chunk in _chunks(list(model_ids), 1000):
            for r in self._rows(sql, dict(params, ids=chunk)):
                r["value_num"] = num(r.get("value_num"))
                out.append(r)
        return out

    def open_conflicts(self, model_ids: Sequence[Any]) -> list[tuple[str, str]]:
        """(model id, field) pairs whose stored status is conflict."""
        if not model_ids:
            return []
        c = self.val.columns
        sql = (f"SELECT DISTINCT v.{q(c['model_id'])} AS model_id, v.{q(c['field'])} AS field "
               f"FROM {q(self.val.name)} v WHERE v.{q(c['model_id'])} = ANY(%(ids)s) "
               f"AND v.{q(c['status'])} = 'conflict'")
        out = []
        for chunk in _chunks(list(model_ids), 1000):
            out += [(str(r["model_id"]), str(r["field"])) for r in self._rows(sql, {"ids": chunk})]
        return out

    def value_counts_by_source(self, model_ids: Sequence[Any]) -> dict[str, int]:
        if not model_ids:
            return {}
        c = self.val.columns
        sql = (f"SELECT v.{q(c['source_id'])} AS source_id, count(*) AS n FROM {q(self.val.name)} v "
               f"WHERE v.{q(c['model_id'])} = ANY(%(ids)s) GROUP BY 1")
        out: dict[str, int] = {}
        for chunk in _chunks(list(model_ids), 1000):
            for r in self._rows(sql, {"ids": chunk}):
                out[str(r["source_id"])] = out.get(str(r["source_id"]), 0) + int(r["n"])
        return out

    def load_products(self) -> list[dict]:
        p = self.s.products
        if p is None:
            return []
        kind = f"p.{q(p.col('kind'))}" if p.has("kind") else "NULL"
        sql = (f"SELECT p.{q(p.col('id'))} AS id, p.{q(p.col('model_id'))} AS model_id, {kind} AS kind "
               f"FROM {q(p.name)} p" + (f" WHERE ({p.where})" if p.where else ""))
        return self._rows(sql)

    # ------------------------------------------------------------------
    # compat inputs
    # ------------------------------------------------------------------
    def fit_records(self) -> list[dict]:
        fr = self.s.compat.get("fit_records") or {}
        if not fr.get("table"):
            return []
        cols = fr.get("columns") or {}
        parts = [f"f.{q(col)} AS {logical}" if col else f"NULL AS {logical}"
                 for logical, col in sorted(cols.items())]
        sql = f"SELECT {_sql_list(parts)} FROM {q(fr['table'])} f"
        if fr.get("where"):
            sql += f" WHERE ({fr['where']})"
        return self._rows(sql)

    def connectors(self) -> list[dict]:
        cn = self.s.compat.get("connectors") or {}
        if not cn.get("table"):
            return []
        cols = cn.get("columns") or {}
        parts = [f"c.{q(col)} AS {logical}" for logical, col in sorted(cols.items()) if col]
        rows = self._rows(f"SELECT {_sql_list(parts)} FROM {q(cn['table'])} c")
        out = []
        for r in rows:
            d = {k: (num(v) if k in ("max_a", "max_v") else v) for k, v in r.items() if v is not None}
            out.append(d)
        return out

    # ------------------------------------------------------------------
    # runtime
    # ------------------------------------------------------------------
    def load_presets(self) -> list[dict]:
        pr = self.s.runtime.get("presets") or {}
        cols = pr.get("columns") or {}
        parts = [f"p.{q(col)} AS {logical}" if col else f"NULL AS {logical}"
                 for logical, col in sorted(cols.items())]
        sql = f"SELECT {_sql_list(parts)} FROM {q(pr['table'])} p"
        if pr.get("where"):
            sql += f" WHERE ({pr['where']})"
        rows = self._rows(sql + " ORDER BY 1")
        for r in rows:
            for k in ("power_w", "peak_w", "duty_cycle", "energy_per_day_wh"):
                r[k] = num(r.get(k))
        return rows

    def _runtime_cols(self) -> dict[str, str]:
        return {k: v for k, v in (self.s.runtime["output"]["columns"] or {}).items() if v}

    def load_runtime_rows(self, model_ids: Sequence[Any]) -> dict[str, list[dict]]:
        if not model_ids:
            return {}
        out_t = self.s.runtime["output"]["table"]
        cols = self._runtime_cols()
        parts = [f"r.{q(col)} AS {logical}" for logical, col in sorted(cols.items())]
        sql = (f"SELECT {_sql_list(parts)} FROM {q(out_t)} r "
               f"WHERE r.{q(cols['model_id'])} = ANY(%(ids)s)")
        out: dict[str, list[dict]] = {}
        for chunk in _chunks(list(model_ids), 1000):
            for r in self._rows(sql, {"ids": chunk}):
                out.setdefault(str(r["model_id"]), []).append(r)
        return out

    def runtime_models_live(self, model_ids: Sequence[Any]) -> int:
        """Models of ``model_ids`` (the in-scope runtime models) that have
        estimate rows."""
        if not model_ids:
            return 0
        out_t = self.s.runtime["output"]["table"]
        col = self._runtime_cols()["model_id"]
        n = 0
        for chunk in _chunks(list(model_ids), 1000):
            rows = self._rows(f"SELECT count(DISTINCT r.{q(col)}) AS n FROM {q(out_t)} r "
                              f"WHERE r.{q(col)} = ANY(%(ids)s)", {"ids": chunk})
            n += int(rows[0]["n"]) if rows else 0
        return n

    def runtime_model_ids(self) -> list[Any]:
        """Every model id with estimate rows (native values, for deletes)."""
        out_t = self.s.runtime["output"]["table"]
        col = self._runtime_cols()["model_id"]
        return [r["m"] for r in self._rows(f"SELECT DISTINCT r.{q(col)} AS m FROM {q(out_t)} r ORDER BY 1")]

    def replace_runtime_rows(self, changes: Mapping[Any, Sequence[Mapping[str, Any]]]) -> dict:
        """{model id: desired rows} for every model whose rows changed (an
        empty list deletes them). One transaction."""
        if not changes:
            return {"runtime_rows_written": 0, "runtime_rows_deleted": 0}
        from psycopg2.extras import execute_values
        out_t = self.s.runtime["output"]["table"]
        cols = self._runtime_cols()
        logical = [k for k in ("model_id", "preset_id", "basis", "usable_energy", "hours_est",
                               "hours_low", "hours_high", "peak_ok") if k in cols]
        written = deleted = 0
        with self.conn:
            with self.conn.cursor() as cur:
                for mid, rows in changes.items():
                    cur.execute(f"DELETE FROM {q(out_t)} WHERE {q(cols['model_id'])} = %(id)s",
                                {"id": mid})
                    deleted += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
                    if not rows:
                        continue
                    names = [q(cols[k]) for k in logical]
                    template = "(" + ", ".join(["%s"] * len(logical))
                    if cols.get("computed_at"):
                        names.append(q(cols["computed_at"]))
                        template += ", now()"
                    template += ")"
                    execute_values(cur, f"INSERT INTO {q(out_t)} ({_sql_list(names)}) VALUES %s",
                                   [tuple(r.get(k) for k in logical) for r in rows],
                                   template=template)
                    written += len(rows)
        return {"runtime_rows_written": written, "runtime_rows_deleted": deleted}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _chunks(items: list, n: int) -> list[list]:
    return [items[i:i + n] for i in range(0, len(items), n)] if items else []


def _round(v: Optional[float], nd: int) -> Optional[float]:
    return None if v is None else round(float(v), nd)


def _adapt(v: Any) -> Any:
    """Python values for typed columns: lists stay lists (numeric[]), dicts
    become JSON."""
    if isinstance(v, dict):
        return json.dumps(v, sort_keys=True)
    return v
