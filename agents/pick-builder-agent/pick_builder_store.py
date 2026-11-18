"""Database side of the pick-builder engine: the TABLE CONTRACT in SQL.

Reads the eligibility relation (the only place a pick may come from), the
optional compat_edges table, the ladder / tier / list definitions and the
substitute subjects; writes ladder slots, list items and substitutes as
whole groups (one tier, one list, one subject) and only the groups that
changed. Every table and column name comes from settings (lower-case
identifiers, emitted double-quoted); WHERE fragments are trusted config.
Aliases inside fragments: ``e`` (eligibility), ``l`` (ladders), ``pl``
(pick lists), ``s`` (substitute subjects).
"""
from __future__ import annotations

import json
import math
from decimal import Decimal
from typing import Any, Iterable, Mapping, Optional, Sequence

from pick_builder_engine import CompatIndex, Product, Subject, integrity_problem
from pick_builder_settings import Settings, quote_ident as q


def num(v: Any) -> Optional[float]:
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


def plain(v: Any) -> Any:
    """DB values -> plain JSON-able Python (Decimal -> float)."""
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, dict):
        return {k: plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [plain(x) for x in v]
    return v


def _cols(mapping: Mapping[str, Optional[str]], alias: str) -> str:
    return ", ".join(f"{alias}.{q(col)} AS {logical}" if col else f"NULL AS {logical}"
                     for logical, col in mapping.items())


class PickStore:
    def __init__(self, conn: Any, settings: Settings):
        self.conn = conn
        self.s = settings
        self.b = settings.block

    # -- plumbing --------------------------------------------------------
    def _rows(self, sql: str, params: Optional[Mapping[str, Any]] = None) -> list[dict]:
        with self.conn.cursor() as cur:
            cur.execute(sql, dict(params or {}))
            names = [d[0] for d in cur.description]
            return [dict(zip(names, r)) for r in cur.fetchall()]

    def begin_snapshot(self, *, read_only: bool = False) -> None:
        """One REPEATABLE READ transaction: the eligibility snapshot the picks
        are chosen from is the one they are written against (now() is fixed
        for the whole transaction, so a 24h freshness window cannot move).
        ``read_only`` (--plan): the server refuses any write in it."""
        self.conn.rollback()
        with self.conn.cursor() as cur:
            cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"
                        + (", READ ONLY" if read_only else ""))

    def try_lock(self, key: str) -> bool:
        with self.conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", (key,))
            got = bool(cur.fetchone()[0])
        self.conn.commit()
        return got

    def unlock(self, key: str) -> None:
        try:
            self.conn.rollback()
            with self.conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (key,))
            self.conn.commit()
        except Exception:  # noqa: BLE001
            pass

    # -- eligibility pool ------------------------------------------------
    def pool_sql(self) -> str:
        el = self.b["eligibility"]
        cols = dict(el["columns"])
        parts = [_cols(cols, "e")]
        for a, col in sorted((el.get("attributes") or {}).items()):
            parts.append(f"e.{q(col)} AS {q('attr__' + a)}")
        sql = f"SELECT {', '.join(parts)} FROM {q(el['relation'])} e"
        if el.get("where"):
            sql += f" WHERE ({el['where']})"
        return sql

    def integrity_cfg(self) -> dict:
        el = self.b["eligibility"]
        cfg = dict(el.get("integrity") or {})
        cfg["_mapped"] = {k for k, v in (el.get("columns") or {}).items() if v}
        return cfg

    def load_pool(self) -> tuple[list[Product], dict[str, int], int]:
        """(eligible products, {integrity reason: rejected rows}, duplicates)."""
        cfg = self.integrity_cfg()
        rejected: dict[str, int] = {}
        out: dict[str, Product] = {}
        dup = 0
        for r in self._rows(self.pool_sql()):
            attrs = {k[len("attr__"):]: plain(v) for k, v in r.items() if k.startswith("attr__")}
            p = Product(product_id=r["product_id"], asin=str(r.get("asin") or ""),
                        kind=str(r.get("kind") or ""), price=num(r.get("price")),  # type: ignore[arg-type]
                        model_id=r.get("model_id"), list_price=num(r.get("list_price")),
                        currency=r.get("currency"), category_confidence=num(r.get("category_confidence")),
                        attrs=attrs)
            why = integrity_problem(p, cfg)
            if why:
                rejected[why] = rejected.get(why, 0) + 1
                continue
            if p.key in out:
                dup += 1
                continue
            out[p.key] = p
        return [out[k] for k in sorted(out)], rejected, dup

    # -- compat ----------------------------------------------------------
    def compat_sql(self) -> str:
        cp = self.b["compat"]
        cols = dict(cp["columns"])
        order = [f"c.{q(cols[k])}" for k in ("a_model_id", "b_model_id", "relation", "rule_id", "verdict")
                 if cols.get(k)]
        return (f"SELECT {_cols(cols, 'c')} FROM {q(cp['table'])} c "
                f"WHERE c.{q(cols['relation'])} = ANY(%(rels)s) ORDER BY {', '.join(order)}")

    def load_compat(self, relations: Iterable[str]) -> CompatIndex:
        """The edges of the relations the plans use. Every row of a pair is
        loaded (one per rule id) and the index merges them by precedence;
        ``config`` (jsonb) carries the configurations and flags that decide
        whether ONE unit works."""
        cp = self.b["compat"]
        rels = sorted(set(relations))
        if not cp.get("table") or not rels:
            return CompatIndex()
        rows = self._rows(self.compat_sql(), {"rels": rels})
        for r in rows:
            cfg = r.get("config")
            if isinstance(cfg, str):
                try:
                    cfg = json.loads(cfg)
                except ValueError:
                    cfg = {}
            r["config"] = plain(cfg) if isinstance(cfg, dict) else {}
        return CompatIndex(rows)

    # -- ladders ---------------------------------------------------------
    def load_ladders(self) -> list[dict]:
        lb = self.b["ladders"]["ladders"]
        cols = {k: v for k, v in lb["columns"].items()}
        sql = f"SELECT {_cols(cols, 'l')} FROM {q(lb['table'])} l"
        if lb.get("where"):
            sql += f" WHERE ({lb['where']})"
        return sorted(self._rows(sql), key=lambda r: (str(r.get("slug") or ""), str(r["id"])))

    def load_tiers(self, ladder_ids: Sequence[Any]) -> list[dict]:
        if not ladder_ids:
            return []
        tb = self.b["ladders"]["tiers"]
        cols = dict(tb["columns"])
        pcols = dict(tb.get("param_columns") or {})
        parts = [_cols(cols, "t")] + [f"t.{q(col)} AS {q('param__' + p)}" for p, col in sorted(pcols.items())]
        sql = (f"SELECT {', '.join(parts)} FROM {q(tb['table'])} t "
               f"WHERE t.{q(cols['ladder_id'])} = ANY(%(ids)s)")
        rows = []
        for r in self._rows(sql, {"ids": list(ladder_ids)}):
            params = r.get("params")
            if isinstance(params, str):
                try:
                    params = json.loads(params)
                except ValueError:
                    params = {}
            params = dict(plain(params) or {})
            for k, v in list(r.items()):
                if k.startswith("param__") and v is not None:
                    params[k[len("param__"):]] = plain(v)
            r["params"] = params
            r["budget"] = num(r.get("budget"))
            rows.append(r)
        return sorted(rows, key=lambda r: (str(r["ladder_id"]), num(r.get("order")) if num(r.get("order")) is not None
                                           else float("inf"), str(r["id"])))

    def load_slots(self, tier_ids: Sequence[Any]) -> dict[str, list[dict]]:
        if not tier_ids:
            return {}
        sb = self.b["ladders"]["slots"]
        cols = {k: v for k, v in sb["columns"].items() if k != "updated_at"}
        sql = (f"SELECT {_cols(cols, 's')} FROM {q(sb['table'])} s "
               f"WHERE s.{q(cols['tier_id'])} = ANY(%(ids)s)")
        out: dict[str, list[dict]] = {}
        for r in self._rows(sql, {"ids": list(tier_ids)}):
            out.setdefault(str(r["tier_id"]), []).append(plain(r))
        return out

    # -- lists -----------------------------------------------------------
    def load_lists(self) -> list[dict]:
        lb = self.b["lists"]["lists"]
        cols = dict(lb["columns"])
        sql = f"SELECT {_cols(cols, 'pl')} FROM {q(lb['table'])} pl"
        if lb.get("where"):
            sql += f" WHERE ({lb['where']})"
        rows = []
        for r in self._rows(sql):
            params = r.get("params")
            if isinstance(params, str):
                try:
                    params = json.loads(params)
                except ValueError:
                    params = {}
            r["params"] = dict(plain(params) or {})
            rows.append(r)
        return sorted(rows, key=lambda r: (str(r.get("slug") or ""), str(r["id"])))

    def load_items(self, list_ids: Sequence[Any]) -> dict[str, list[dict]]:
        if not list_ids:
            return {}
        ib = self.b["lists"]["items"]
        cols = {k: v for k, v in ib["columns"].items() if k != "updated_at"}
        sql = (f"SELECT {_cols(cols, 'i')} FROM {q(ib['table'])} i "
               f"WHERE i.{q(cols['list_id'])} = ANY(%(ids)s)")
        out: dict[str, list[dict]] = {}
        for r in self._rows(sql, {"ids": list(list_ids)}):
            out.setdefault(str(r["list_id"]), []).append(plain(r))
        return out

    # -- substitutes -----------------------------------------------------
    def load_subjects(self) -> list[Subject]:
        sj = self.b["substitutes"]["subjects"]
        cols = dict(sj["columns"])
        parts = [_cols(cols, "s")]
        for a, col in sorted((sj.get("attributes") or {}).items()):
            parts.append(f"s.{q(col)} AS {q('attr__' + a)}")
        sql = f"SELECT {', '.join(parts)} FROM {q(sj['relation'])} s"
        if sj.get("where"):
            sql += f" WHERE ({sj['where']})"
        out = {}
        for r in self._rows(sql):
            attrs = {k[len("attr__"):]: plain(v) for k, v in r.items() if k.startswith("attr__")}
            unbuyable = r.get("unbuyable")
            out[str(r["product_id"])] = Subject(product_id=r["product_id"], kind=str(r.get("kind") or ""),
                                                asin=str(r.get("asin") or ""), model_id=r.get("model_id"),
                                                unbuyable=None if unbuyable is None else bool(unbuyable),
                                                attrs=attrs)
        return [out[k] for k in sorted(out)]

    def load_substitutes(self) -> dict[str, list[dict]]:
        ob = self.b["substitutes"]["output"]
        cols = {k: v for k, v in ob["columns"].items() if k != "updated_at"}
        out: dict[str, list[dict]] = {}
        for r in self._rows(f"SELECT {_cols(cols, 'o')} FROM {q(ob['table'])} o"):
            out.setdefault(str(r["subject_id"]), []).append(plain(r))
        return out

    # -- writes (inside the snapshot transaction) ------------------------
    def replace_groups(self, table: str, columns: Mapping[str, Optional[str]], group_col: str,
                       groups: Mapping[Any, Sequence[Mapping[str, Any]]]) -> tuple[int, int]:
        """DELETE + INSERT each group (all rows of one tier / list / subject).
        Returns (rows inserted, rows deleted). The caller commits."""
        from psycopg2.extras import execute_values
        logical = [k for k, v in columns.items() if v and k != "updated_at"]
        names = [q(columns[k]) for k in logical]
        template = "(" + ", ".join(["%s"] * len(logical))
        if columns.get("updated_at"):
            names.append(q(columns["updated_at"]))
            template += ", now()"
        template += ")"
        ins = dels = 0
        with self.conn.cursor() as cur:
            for gid, rows in groups.items():
                cur.execute(f"DELETE FROM {q(table)} WHERE {q(columns[group_col])} = %(g)s", {"g": gid})
                dels += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
                if rows:
                    data = [tuple(_adapt(r.get(k)) for k in logical) for r in rows]
                    execute_values(cur, f"INSERT INTO {q(table)} ({', '.join(names)}) VALUES %s", data,
                                   template=template)
                    ins += len(data)
        return ins, dels

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        try:
            self.conn.rollback()
        except Exception:  # noqa: BLE001
            pass


def _adapt(v: Any) -> Any:
    if isinstance(v, (dict, list)):
        return json.dumps(v, sort_keys=True, default=str)
    return v
