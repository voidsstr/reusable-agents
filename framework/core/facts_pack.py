"""Facts pack: the only numbers an LLM author may write, built by SQL.

Why: an Opus guide that quotes a runtime, a wattage or a fit from memory is
wrong as often as it is right, and a guide read during an outage cannot be
wrong. So the author never researches: the engine runs the site's own
read-only SQL (config, not code) against the site database, hands the rows to
the model as JSON, and afterwards checks every number in the prose against
the same rows (``framework.core.guide_claims_guard``).

Two hard rules are enforced HERE, not trusted to config:

  * **No prices and no Amazon text, ever.** A facts pack is stored in run
    records and shown to a model; Amazon's licence lets a site show a price
    or title only next to its timestamp and notice, and never store it past
    its answer. A column whose NAME looks like commerce data (price, currency,
    offer, availability, rating, review, image, title, amazon...), a query
    that names a forbidden relation (the site's Amazon cache tables), or a
    VALUE that holds a money amount or an Amazon URL makes :func:`build`
    raise :class:`FactsPackViolation`. The run fails closed.
  * **Read-only.** :func:`build` runs every query in a READ ONLY transaction
    and rolls it back.

Config shape (one entry per query, run in order):

    facts:
      forbidden_relations: [amazon_item_cache, v_offer_display]
      queries:
        - name: runtimes
          sql: "SELECT ... WHERE re.preset_id = ANY(%(presets)s) AND re.model_id = ANY(%(stations__model_id)s)"
          units: {usable_wh: Wh, hours_est: h, hours_low: h, hours_high: h}
          paths: [product_path]          # site paths a guide may link (checked live later)
          names: [model_name, brand]     # text stripped before numbers are compared
          entities: [model_name]         # a sentence naming this model may only use THIS row's numbers
                                         # (plus numbers of rows with no entity, e.g. load presets)
          required: true                 # 0 rows -> the topic's data is not ready
          max_rows: 200

Parameters: the topic's scope (``%(presets)s``, ``%(lists)s``, ...) plus,
for every earlier query ``q`` and column ``c``, ``%(q__c)s`` = the list of
that column's values (so a later query can scope itself to the stations an
earlier one chose). A literal ``%`` in SQL must be written ``%%``.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import decimal
import json
import re
from typing import Any, Iterable, Mapping, Optional, Sequence

#: Column names that can only hold commerce data. Matched per underscore
#: segment, so "usable_wh" passes and "list_price" / "offer_state" / "title" fail.
#: "detail_page" (Amazon's detail_page_url) is matched as a pair: a plain
#: "detail" key (a compat assumption's params.detail) is our own text.
DEFAULT_FORBIDDEN_COLUMN_RE = (r"(?i)(?:^|_)(?:price|prices|pricing|currency|offer|offers|availability|"
                               r"available|buybox|rating|ratings|review|reviews|image|images|title|titles|"
                               r"amazon|detail_page|discount|sale|deal|deals|msrp|cost)(?:_|$)")
#: Values that are prices or Amazon text / hosts.
MONEY_RE = re.compile(r"(?:[$€£¥₹]\s?\d|\b(?:USD|CAD|GBP|EUR|INR)\s?\d|\d\s?(?:USD|dollars?)\b)", re.I)
AMAZON_RE = re.compile(r"(?i)(?:amazon\.[a-z.]{2,}|amzn\.(?:to|com|eu|asia)|media-amazon|images-amazon|"
                       r"ssl-images-amazon|amazon-adsystem|assoc-amazon|\ba\.co/)")

UNIT_ALIASES = {
    "w": "W", "watt": "W", "watts": "W", "kw": "kW",
    "wh": "Wh", "watt-hour": "Wh", "watt-hours": "Wh", "kwh": "kWh",
    "h": "h", "hr": "h", "hrs": "h", "hour": "h", "hours": "h",
    "v": "V", "volt": "V", "volts": "V", "a": "A", "amp": "A", "amps": "A",
    "d": "d", "day": "d", "days": "d", "wk": "wk", "week": "wk", "weeks": "wk",
    "c": "C", "°c": "C", "degc": "C", "celsius": "C",
}
#: unit -> (base unit, factor): numbers are compared in the base unit. Days
#: and weeks are hours (a runtime written as "3 days" is checked as 72 h);
#: C is a temperature in degrees Celsius (Fahrenheit is converted by the
#: claims guard, not here: it is not a factor).
BASE_UNITS = {"W": ("W", 1.0), "kW": ("W", 1000.0), "Wh": ("Wh", 1.0), "kWh": ("Wh", 1000.0),
              "h": ("h", 1.0), "d": ("h", 24.0), "wk": ("h", 168.0), "V": ("V", 1.0), "A": ("A", 1.0),
              "C": ("C", 1.0)}


class FactsPackError(ValueError):
    """The facts config cannot be used (bad query block)."""


class FactsPackViolation(RuntimeError):
    """The pack would carry a price or Amazon text. Never ignore this."""


def canonical_unit(raw: str) -> str:
    u = str(raw or "").strip()
    if u in BASE_UNITS:
        return u
    got = UNIT_ALIASES.get(u.lower())
    if not got:
        raise FactsPackError(f"unknown unit {raw!r} (known: {sorted(BASE_UNITS)})")
    return got


@dataclasses.dataclass(frozen=True)
class FactQuery:
    name: str
    sql: str
    units: Mapping[str, str] = dataclasses.field(default_factory=dict)
    paths: tuple[str, ...] = ()
    names: tuple[str, ...] = ()
    entities: tuple[str, ...] = ()     # columns naming the row's subject (a model): scopes its numbers
    required: bool = False
    max_rows: int = 200
    description: str = ""


_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")


def parse_queries(block: Optional[Mapping[str, Any]]) -> tuple[tuple[FactQuery, ...], tuple[str, ...]]:
    """(queries, forbidden_relations) from a ``facts`` block. Raises
    FactsPackError on a malformed entry."""
    block = dict(block or {})
    out: list[FactQuery] = []
    seen: set[str] = set()
    for i, q in enumerate(block.get("queries") or []):
        if not isinstance(q, Mapping):
            raise FactsPackError(f"facts.queries[{i}]: expected an object")
        name = str(q.get("name") or "")
        if not _NAME_RE.match(name) or "__" in name:
            raise FactsPackError(f"facts.queries[{i}].name: {name!r} is not a snake_case name without '__'")
        if name in seen:
            raise FactsPackError(f"facts.queries: duplicate name {name!r}")
        seen.add(name)
        sql = str(q.get("sql") or "").strip()
        if not re.match(r"(?is)^\s*(with|select)\b", sql) or ";" in sql.rstrip(";"):
            raise FactsPackError(f"facts.queries.{name}.sql: one SELECT (or WITH ... SELECT) statement")
        units = {str(k): canonical_unit(v) for k, v in (q.get("units") or {}).items()}
        try:
            max_rows = max(1, min(2000, int(q.get("max_rows") or 200)))
        except (TypeError, ValueError):
            raise FactsPackError(f"facts.queries.{name}.max_rows: not an integer") from None
        out.append(FactQuery(name=name, sql=sql.rstrip(";"), units=units,
                             paths=tuple(str(x) for x in (q.get("paths") or ())),
                             names=tuple(str(x) for x in (q.get("names") or ())),
                             entities=tuple(str(x) for x in (q.get("entities") or ())),
                             required=bool(q.get("required")), max_rows=max_rows,
                             description=str(q.get("description") or "")))
    rel = tuple(str(r).lower() for r in (block.get("forbidden_relations") or ()))
    return tuple(out), rel


def _jsonable(v: Any) -> Any:
    if isinstance(v, decimal.Decimal):
        f = float(v)
        return int(f) if f.is_integer() else f
    if isinstance(v, (_dt.datetime, _dt.date)):
        return v.isoformat()
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, Mapping):
        return {str(k): _jsonable(x) for k, x in v.items()}
    return v


def _walk_strings(obj: Any, path: str = "") -> Iterable[tuple[str, str]]:
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, Mapping):
        for k, v in obj.items():
            yield from _walk_strings(v, f"{path}.{k}" if path else str(k))
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            yield from _walk_strings(v, f"{path}[{i}]")


def _walk_keys(obj: Any, path: str = "") -> Iterable[tuple[str, str]]:
    if isinstance(obj, Mapping):
        for k, v in obj.items():
            here = f"{path}.{k}" if path else str(k)
            yield here, str(k)
            yield from _walk_keys(v, here)
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            yield from _walk_keys(v, f"{path}[{i}]")


def commerce_violations(obj: Any, *, forbidden_column_re: str = DEFAULT_FORBIDDEN_COLUMN_RE) -> list[str]:
    """Every key that names commerce data and every string value that holds
    a money amount or Amazon text / host, anywhere in `obj` (nested keys of
    jsonb columns included)."""
    rx = re.compile(forbidden_column_re)
    out = [f"{p}: forbidden column name {k!r}" for p, k in _walk_keys(obj) if rx.search(k)]
    for p, s in _walk_strings(obj):
        if MONEY_RE.search(s):
            out.append(f"{p}: holds a money amount ({s[:60]!r})")
        if AMAZON_RE.search(s):
            out.append(f"{p}: holds Amazon text or an Amazon host ({s[:60]!r})")
    return out


def assert_no_commerce(obj: Any, *, forbidden_column_re: str = DEFAULT_FORBIDDEN_COLUMN_RE) -> None:
    bad = commerce_violations(obj, forbidden_column_re=forbidden_column_re)
    if bad:
        raise FactsPackViolation("facts pack would carry commerce data: " + "; ".join(bad[:8]))


def forbidden_relation_hits(sql: str, relations: Sequence[str]) -> list[str]:
    low = sql.lower()
    return [r for r in relations if re.search(rf"(?<![a-z0-9_]){re.escape(r)}(?![a-z0-9_])", low)]


@dataclasses.dataclass
class NumericFacts:
    """Every number the pack states, per base unit, plus the names to strip.
    ``by_entity`` keeps each subject's own numbers (a model's specs and
    runtimes); ``global_values`` the numbers of rows about no subject (a load
    preset). A sentence that names a subject is checked against its numbers
    and the global ones only (framework.core.guide_claims_guard)."""
    values: dict[str, set[float]] = dataclasses.field(default_factory=dict)
    names: set[str] = dataclasses.field(default_factory=set)
    by_entity: dict[str, dict[str, set[float]]] = dataclasses.field(default_factory=dict)
    global_values: dict[str, set[float]] = dataclasses.field(default_factory=dict)
    #: (entity or "", column) -> base unit -> values: which column a value came
    #: from, for claims that depend on what a figure measures (a charging
    #: temperature limit is not a discharging one).
    by_column: dict[tuple[str, str], dict[str, set[float]]] = dataclasses.field(default_factory=dict)

    def add(self, unit: str, value: Any, entities: Iterable[str] = (), column: str = "") -> None:
        try:
            f = float(value)
        except (TypeError, ValueError):
            return
        base, factor = BASE_UNITS[canonical_unit(unit)]
        v = round(f * factor, 6)
        self.values.setdefault(base, set()).add(v)
        ents = [e.strip().lower() for e in entities if isinstance(e, str) and len(e.strip()) >= 3]
        if not ents:
            self.global_values.setdefault(base, set()).add(v)
        for e in ents:
            self.by_entity.setdefault(e, {}).setdefault(base, set()).add(v)
        if column:
            for e in (ents or [""]):
                self.by_column.setdefault((e, column), {}).setdefault(base, set()).add(v)

    def column_values(self, base: str, columns: Iterable[str], entities: Iterable[str] = ()) -> set[float]:
        """Values of `columns` in base unit `base`: of the given entities (and
        of rows about no entity); every entity's when none is given."""
        cols, ents = set(columns), set(entities)
        out: set[float] = set()
        for (e, c), d in self.by_column.items():
            if c in cols and (not ents or e in ents or e == ""):
                out |= d.get(base, set())
        return out

    def merge(self, other: "NumericFacts") -> None:
        for u, vs in other.values.items():
            self.values.setdefault(u, set()).update(vs)
        for u, vs in other.global_values.items():
            self.global_values.setdefault(u, set()).update(vs)
        for e, d in other.by_entity.items():
            for u, vs in d.items():
                self.by_entity.setdefault(e, {}).setdefault(u, set()).update(vs)
        for k, d in other.by_column.items():
            for u, vs in d.items():
                self.by_column.setdefault(k, {}).setdefault(u, set()).update(vs)
        self.names.update(other.names)

    def to_dict(self) -> dict:
        return {"values": {u: sorted(v) for u, v in sorted(self.values.items())}, "names": sorted(self.names),
                "entities": sorted(self.by_entity)}


@dataclasses.dataclass
class FactsPack:
    rows: dict[str, list[dict]]
    numeric: NumericFacts
    paths: set[str]
    empty_required: list[str]
    queries: tuple[FactQuery, ...] = ()

    @property
    def ready(self) -> bool:
        return not self.empty_required

    def restrict_paths(self, live: Iterable[str]) -> "FactsPack":
        """A copy for the prompt whose site paths that are not in `live` are
        None (so ``to_prompt_json`` drops them): the model is only offered live,
        indexable pages as link targets. Names and figures are unchanged, and
        the original pack (what the claims guard checks against) is untouched."""
        keep = set(live)

        def fix(r: dict) -> dict:
            return {k: (None if isinstance(v, str) and v in self.paths and v not in keep else v) for k, v in r.items()}

        return dataclasses.replace(self, rows={n: [fix(r) for r in rs] for n, rs in self.rows.items()},
                                   paths={p for p in self.paths if p in keep})

    def to_prompt_json(self) -> str:
        """Compact JSON for the prompt: None values dropped."""
        slim = {n: [{k: v for k, v in r.items() if v is not None} for r in rows] for n, rows in self.rows.items()}
        return json.dumps(slim, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    def to_dict(self) -> dict:
        return {"rows": self.rows, "numeric": self.numeric.to_dict(), "paths": sorted(self.paths),
                "empty_required": list(self.empty_required)}


def collect(rows_by_query: Mapping[str, list[dict]], queries: Sequence[FactQuery]) -> tuple[NumericFacts, set[str]]:
    """The numeric index and linkable paths of already-fetched rows."""
    nf, paths = NumericFacts(), set()
    for q in queries:
        for r in rows_by_query.get(q.name) or []:
            ents = [r.get(c) for c in q.entities if isinstance(r.get(c), str)]
            for col, unit in q.units.items():
                if r.get(col) is not None:
                    nf.add(unit, r[col], ents, column=col)
            for col in q.paths:
                v = r.get(col)
                for p in (v if isinstance(v, (list, tuple)) else [v]):
                    if isinstance(p, str) and p.startswith("/"):
                        paths.add(p)
            for col in q.names:
                v = r.get(col)
                if isinstance(v, str) and v.strip():
                    nf.names.add(v.strip())
    return nf, paths


def build(conn, queries: Sequence[FactQuery], params: Mapping[str, Any], *,
          forbidden_relations: Sequence[str] = (),
          forbidden_column_re: str = DEFAULT_FORBIDDEN_COLUMN_RE) -> FactsPack:
    """Run every query (read-only, rolled back) and return the pack. Raises
    FactsPackViolation before returning anything that carries commerce data."""
    for q in queries:
        hits = forbidden_relation_hits(q.sql, forbidden_relations)
        if hits:
            raise FactsPackViolation(f"facts query {q.name!r} reads forbidden relation(s) {hits}")
    rows_by: dict[str, list[dict]] = {}
    bound: dict[str, Any] = dict(params)
    empty: list[str] = []
    rx = re.compile(forbidden_column_re)
    # Never toggle conn.autocommit: psycopg2 re-applies the session defaults
    # when it changes, which drops a read-only session set by the caller.
    autocommit = bool(getattr(conn, "autocommit", False))
    try:
        if not autocommit:
            conn.rollback()            # SET TRANSACTION must open a fresh transaction
        with conn.cursor() as cur:
            cur.execute("BEGIN TRANSACTION READ ONLY" if autocommit else "SET TRANSACTION READ ONLY")
            for q in queries:
                cur.execute(q.sql, bound)
                cols = [d[0] for d in cur.description]
                bad = [c for c in cols if rx.search(c)]
                if bad:
                    raise FactsPackViolation(f"facts query {q.name!r} selects forbidden column(s) {bad}")
                rows = [{c: _jsonable(v) for c, v in zip(cols, r)} for r in cur.fetchmany(q.max_rows)]
                rows_by[q.name] = rows
                if q.required and not rows:
                    empty.append(q.name)
                for c in cols:
                    bound[f"{q.name}__{c}"] = [r[c] for r in rows if r.get(c) is not None]
    finally:
        if autocommit:
            try:
                with conn.cursor() as cur:
                    cur.execute("ROLLBACK")
            except Exception:  # noqa: BLE001 - an aborted transaction is already over
                pass
        else:
            conn.rollback()
    assert_no_commerce(rows_by, forbidden_column_re=forbidden_column_re)
    nf, paths = collect(rows_by, queries)
    return FactsPack(rows=rows_by, numeric=nf, paths=paths, empty_required=empty, queries=tuple(queries))


def from_rows(rows_by_query: Mapping[str, list[dict]], queries: Sequence[FactQuery], *,
              forbidden_column_re: str = DEFAULT_FORBIDDEN_COLUMN_RE) -> FactsPack:
    """A pack from rows already in hand (tests, replays); same commerce assertion."""
    rows = {k: [dict(r) for r in v] for k, v in rows_by_query.items()}
    assert_no_commerce(rows, forbidden_column_re=forbidden_column_re)
    nf, paths = collect(rows, queries)
    empty = [q.name for q in queries if q.required and not rows.get(q.name)]
    return FactsPack(rows=rows, numeric=nf, paths=paths, empty_required=empty, queries=tuple(queries))


__all__ = ["FactQuery", "FactsPack", "NumericFacts", "FactsPackError", "FactsPackViolation", "parse_queries",
           "build", "from_rows", "collect", "commerce_violations", "assert_no_commerce", "canonical_unit",
           "BASE_UNITS", "DEFAULT_FORBIDDEN_COLUMN_RE", "MONEY_RE", "AMAZON_RE"]
