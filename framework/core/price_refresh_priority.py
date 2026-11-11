"""Demand-ranked Amazon price refresh: which prices to re-check first.

WHY THIS EXISTS
---------------
Several agents re-price Amazon listings out of ONE shared Creators budget
(framework/core/amazon_creators.py ledger): a site's price verifier, the
hydration agent's price pass, a catalog-freshness tail sweep, a kitchen
scraper. Each had its own selector, and none of them ordered by where buyers
land. Measured 2026-11-11 on the reference deployments:

  * About half of one site's verified-human Amazon clicks (630 of 1,279 in
    30 days) landed on long-tail product pages that were in no priority group
    until AFTER that click. Those pages showed a price ~18-21% of the time.
  * The hydration agent spent ~5.6k re-prices a day ordered by review_count,
    i.e. on stale high-review rows, while 83% of clicked long-tail products
    have fewer than 1,000 reviews.
  * The other site's kitchen-product pages had no clicked-listing group at
    all: 11 of 42 clicks there saw a price older than 24h.

This module is the one definition of refresh priority. Its signals come from
the framework's own readers (``human_clicks`` = "a person clicked a buy
link", ``ai_traffic`` = "an assistant used this page"), so every consumer
means the same thing by "demand". Consumers keep their own writers, budget
lines and cadence; this module only decides the ORDER and the GATES.

MODEL
-----
``catalog``   where the prices live: ``table``, ``asin_col``, ``fresh_cols``
              (the newest of these timestamps is the row's last check), a
              trusted ``where`` fragment with named ``params``, and
              ``asin_regex`` (only rows whose ASIN matches are ranked).
``sources``   how this site's signals are read:
                human_clicks: table, spec, match, group_col, catalog_key,
                              profile (human_clicks by_profile key; default
                              the consumer's agent id)
                ai_traffic:   config (ai_traffic overrides), prefixes,
                              catalog_key, max_paths
``groups``    the ordered demand tiers. Every group has ``name``, ``kind``
              and ``gate_hours``; optional ``cap`` (members kept, best
              first) and ``enabled``. Kinds:
                human_clicks  verified-human clicks per key over window_days
                ai_traffic    answer-time AI fetches + AI referrals per page
                sql           a site SELECT: first column a catalog key,
                              optional second column a score (trusted config)
                storage_list  a JSON list in framework storage (``key``), or a
                              {"keys"|"asins": [...]} document — e.g. a set a
                              shelf audit publishes
``long_tail`` every other catalog row, oldest check first
              (``enabled``, ``gate_hours``).

``catalog_key`` names the catalog column a signal's keys match. Empty means
the catalog's ``asin_col``; anything else (a listing id, a product id) is
mapped to ASINs through the catalog table.

An ASIN's TIER is the first group (in order) that contains it; its GATE is
the smallest ``gate_hours`` of all groups that contain it. It is DUE when it
was never checked, or last checked more than GATE hours ago.
:func:`rank_refresh_candidates` returns due ASINs in refresh order: tier
order, then never-checked first, then oldest check first, then demand score;
then the long tail the same way.

A gate means "due after". A consumer on a fixed cron of period P re-checks a
due row on its next run, so it sees roughly ceil(gate / P) * P hours between
checks: on a 6-hourly cron a 12h gate behaves like 18h, on a 2-hourly one
like 12-14h.

CONFIG (lowest to highest precedence)
-------------------------------------
    DEFAULTS (this module)
      <- file["defaults"]
      <- file["by_site"][<site>]
      <- the caller's site_cfg (site.yaml block / site code)
      <- file["by_agent_id"][<agent id>]

``file`` is the storage config ``config/price-refresh-priority-config.json``,
else the repo default of the same name (a storage copy replaces the repo
file wholesale). Dicts deep-merge and lists replace, except ``extra_groups``,
which appends to ``groups`` at every layer, and ``group_overrides``
({name: partial group}), which merges into the named group.

``enabled`` defaults to False: nothing changes until a consumer's site
config, or the storage file, turns it on. ``by_agent_id.<id>.enabled=false``
in storage switches one consumer back to its old selector without a commit.

CLI: ``python3 -m framework.cli.price_refresh_priority --help`` (config,
tiers, rank, explain <ASIN>; read-only).
"""
from __future__ import annotations

import copy
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, NamedTuple, Optional, Sequence

log = logging.getLogger("framework.price_refresh_priority")

CONFIG_KEY = "config/price-refresh-priority-config.json"
_REPO_CONFIG = (Path(__file__).resolve().parents[2]
                / "config" / "price-refresh-priority-config.json")

REAL_ASIN_REGEX = r"^([0-9]{9}[0-9X]|[A-Z][A-Z0-9]{9})$"
LONG_TAIL = "long-tail"
KINDS = ("human_clicks", "ai_traffic", "sql", "storage_list")

DEFAULTS: dict[str, Any] = {
    "schema_version": "1",
    "enabled": False,
    # Age at which a site stops showing a price (and its Offer JSON-LD).
    # Used only to report fresh/stale shares; gates decide what is due.
    "display_hours": 24,
    "statement_timeout_ms": 60000,
    "catalog": {
        "table": "",                 # e.g. "products"; "" = cannot rank
        "asin_col": "asin",
        "fresh_cols": ["price_updated_at"],
        "where": "",                 # trusted SQL; double any literal '%'
        "params": {},                # named params the where fragment uses
        "asin_regex": REAL_ASIN_REGEX,
    },
    "sources": {
        "human_clicks": {
            "table": "",             # the site's click log; "" = signal off
            "spec": {},              # human_clicks column spec
            "match": {},             # e.g. {"target": "amazon"}
            "group_col": "asin",
            "catalog_key": "",
            "profile": "",
        },
        "ai_traffic": {
            "prefixes": [],          # e.g. ["/product/"]; [] = signal off
            "catalog_key": "",
            "max_paths": 20000,
            # Answer-time fetchers only. OAI-SearchBot is ChatGPT search's
            # INDEX crawler: counting it filled 1,702 of a 6,000-ASIN cap with
            # pages no assistant had used for a person (2026-11-11).
            "config": {
                "live_fetch_sources": ["chatgpt-user", "perplexity-user",
                                       "claude-user"],
            },
        },
    },
    # Recently clicked first, then clicked this fortnight, then pages AI
    # assistants used at answer time, then the rest of the month's clicks.
    # The AI gate sits above a 6-hourly verifier's ~18h re-check so a 2-hourly
    # consumer only catches what that run missed (a failed run), instead of
    # pricing the same rows twice.
    "groups": [
        {"name": "clicked-48h", "kind": "human_clicks", "window_days": 2,
         "gate_hours": 5},
        {"name": "clicked-14d", "kind": "human_clicks", "window_days": 14,
         "gate_hours": 12},
        {"name": "ai-answer-time", "kind": "ai_traffic", "gate_hours": 20},
        {"name": "clicked-30d", "kind": "human_clicks", "window_days": 30,
         "gate_hours": 12},
    ],
    "extra_groups": [],
    "group_overrides": {},
    "long_tail": {"enabled": True, "gate_hours": 24},
}

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")
_LIST_APPENDS = {"extra_groups"}


class RefreshCandidate(NamedTuple):
    """One due ASIN. ``asin, tier, gate_hours`` is the contract; the rest is
    context for logs and --explain."""
    asin: str
    tier: str                      # group name, or "long-tail"
    gate_hours: float
    age_hours: Optional[float]     # None = never checked
    score: float                   # demand score inside its tier
    rank: int                      # tier position; the long tail is last


@dataclass
class Tiers:
    """Signal membership, before any freshness check."""
    groups: list[dict] = field(default_factory=list)
    members: dict[str, dict] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)   # tier -> members
    sizes: dict[str, int] = field(default_factory=dict)    # group -> raw size
    errors: dict[str, str] = field(default_factory=dict)   # group -> reason

    def asins(self, *, max_gate_hours: Optional[float] = None,
              tiers: Optional[Iterable[str]] = None) -> list[str]:
        """Member ASINs in tier order, best score first inside a tier."""
        want = set(tiers) if tiers is not None else None
        rows = [(m["rank"], -float(m["score"]), m["order"], a)
                for a, m in self.members.items()
                if (max_gate_hours is None or m["gate_hours"] <= max_gate_hours)
                and (want is None or m["tier"] in want)]
        return [a for *_, a in sorted(rows)]

    def summary(self) -> dict:
        return {"counts": dict(self.counts), "sizes": dict(self.sizes),
                "errors": dict(self.errors)}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _merge(base: Mapping, over: Optional[Mapping]) -> dict:
    out = copy.deepcopy(dict(base))
    for k, v in (over or {}).items():
        if isinstance(v, Mapping) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        elif k in _LIST_APPENDS:
            out[k] = list(out.get(k) or []) + copy.deepcopy(list(v or []))
        else:
            out[k] = copy.deepcopy(v)
    return out


def merge_config(base: Mapping, over: Optional[Mapping]) -> dict:
    """Deep-merge two config layers the way resolve_config does (dicts merge,
    lists replace, ``extra_groups`` appends). For a consumer that stacks its
    own site.yaml block over built-in site values before resolving."""
    return _merge(base or {}, over)


def load_file_config(storage=None) -> dict:
    """The storage copy when present (it replaces the repo default wholesale),
    else the repo default file, else {}."""
    if storage is None:
        try:
            from framework.core.storage import get_storage
            storage = get_storage()
        except (Exception, SystemExit) as e:  # unknown backend raises SystemExit
            log.warning("price_refresh_priority: storage unavailable, repo default "
                        "config only: %s", e)
            storage = None
    if storage is not None:
        try:
            doc = storage.read_json(CONFIG_KEY)
            if isinstance(doc, dict) and doc:
                return doc
        except Exception as e:
            log.warning("price_refresh_priority: storage config unreadable: %s", e)
    try:
        doc = json.loads(_REPO_CONFIG.read_text())
        return doc if isinstance(doc, dict) else {}
    except Exception:
        return {}


def _effective_groups(cfg: dict) -> tuple[list[dict], list[str]]:
    """groups + extra_groups, group_overrides applied, disabled and invalid
    groups dropped. Returns (groups, problems)."""
    overrides = cfg.get("group_overrides") or {}
    out: list[dict] = []
    problems: list[str] = []
    seen: set[str] = set()
    for g in list(cfg.get("groups") or []) + list(cfg.get("extra_groups") or []):
        if not isinstance(g, Mapping):
            problems.append(f"group is not an object: {g!r}"[:200])
            continue
        g = _merge(g, overrides.get(g.get("name")) if g.get("name") else None)
        name = str(g.get("name") or "").strip()
        if not name or name == LONG_TAIL:
            problems.append(f"group needs a name other than {LONG_TAIL!r}: {g!r}"[:200])
            continue
        if name in seen:
            problems.append(f"duplicate group {name!r} (first one kept)")
            continue
        seen.add(name)
        if g.get("enabled") is False:
            continue
        if g.get("kind") not in KINDS:
            problems.append(f"group {name!r}: unknown kind {g.get('kind')!r}")
            continue
        try:
            g["gate_hours"] = max(0.0, float(g.get("gate_hours")))
        except (TypeError, ValueError):
            problems.append(f"group {name!r}: gate_hours must be a number")
            continue
        g["name"] = name
        out.append(g)
    return out, problems


def resolve_config(*, agent_id: str = "", site: str = "",
                   site_cfg: Optional[Mapping] = None, storage=None,
                   file_cfg: Optional[Mapping] = None) -> dict:
    """DEFAULTS <- file.defaults <- file.by_site[site] <- site_cfg <-
    file.by_agent_id[agent_id]; groups resolved (see module docstring).

    Pass ``file_cfg`` to skip the storage read (tests / offline use)."""
    doc = dict(file_cfg) if file_cfg is not None else load_file_config(storage)
    cfg = _merge(DEFAULTS, doc.get("defaults"))
    if site:
        cfg = _merge(cfg, (doc.get("by_site") or {}).get(site))
    cfg = _merge(cfg, site_cfg)
    if agent_id:
        cfg = _merge(cfg, (doc.get("by_agent_id") or {}).get(agent_id))
    groups, problems = _effective_groups(cfg)
    cfg["groups"] = groups
    cfg["extra_groups"] = []
    cfg["group_overrides"] = {}
    cfg["config_problems"] = problems
    cfg["agent_id"] = agent_id
    cfg["site"] = site
    return cfg


# ---------------------------------------------------------------------------
# SQL helpers
# ---------------------------------------------------------------------------

def _ident(name: Any, what: str) -> str:
    if not isinstance(name, str) or not _IDENT_RE.match(name):
        raise ValueError(f"price_refresh_priority: unsafe {what} identifier {name!r}")
    return name


def _cell(row: Any, i: int, name: str) -> Any:
    """Column `i` / `name` of a tuple row or a dict row (RealDictCursor)."""
    if isinstance(row, Mapping):
        return row.get(name)
    return row[i] if len(row) > i else None


class _Isolated:
    """Run read-only queries without disturbing the caller's transaction.

    A savepoint wraps each signal read and is always rolled back afterwards:
    a failed or timed-out query cannot poison the caller's transaction, and
    the SET LOCAL statement_timeout is undone with it. On an autocommit
    connection there is no transaction to protect (and SET LOCAL would not
    stick), so the statements just run."""

    def __init__(self, conn, timeout_ms: int):
        self.conn = conn
        self.timeout_ms = int(timeout_ms or 0)
        self.tx = not bool(getattr(conn, "autocommit", False))

    def __enter__(self):
        if self.tx:
            with self.conn.cursor() as cur:
                cur.execute("SAVEPOINT price_refresh_priority")
                if self.timeout_ms > 0:
                    cur.execute(f"SET LOCAL statement_timeout = {self.timeout_ms}")
        return self

    def __exit__(self, *exc):
        if self.tx:
            try:
                with self.conn.cursor() as cur:
                    cur.execute("ROLLBACK TO SAVEPOINT price_refresh_priority")
                    cur.execute("RELEASE SAVEPOINT price_refresh_priority")
            except Exception:
                # The connection itself is gone; the caller will find out.
                log.warning("price_refresh_priority: savepoint cleanup failed",
                            exc_info=True)
        return False


def _fresh_expr(cat: Mapping, alias: str = "c") -> str:
    cols = [_ident(c, "fresh column") for c in (cat.get("fresh_cols") or [])]
    if not cols:
        raise ValueError("price_refresh_priority: catalog.fresh_cols is empty")
    parts = [f"{alias}.{c}" for c in cols]
    return parts[0] if len(parts) == 1 else f"GREATEST({', '.join(parts)})"


def _catalog_where(cat: Mapping) -> str:
    where = str(cat.get("where") or "").strip()
    return f"({where})" if where else "TRUE"


def _catalog_params(cat: Mapping) -> dict:
    params = dict(cat.get("params") or {})
    clash = [k for k in params if str(k).startswith("prp_")]
    if clash:
        raise ValueError(f"price_refresh_priority: catalog.params may not use the "
                         f"reserved prp_ prefix: {clash}")
    return params


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------

def _signal_human_clicks(conn, cfg: Mapping, group: Mapping, *, storage=None,
                         human_clicks_config: Optional[Mapping] = None
                         ) -> tuple[list[tuple[str, float]], str]:
    from framework.core import human_clicks
    src = dict((cfg.get("sources") or {}).get("human_clicks") or {})
    src = _merge(src, group.get("source"))
    table = src.get("table")
    if not table:
        raise LookupError("sources.human_clicks.table is not configured")
    spec = human_clicks.resolve_spec(src.get("spec") or {},
                                     profile=src.get("profile") or cfg.get("agent_id") or "",
                                     storage=storage, config=human_clicks_config)
    counts = human_clicks.human_counts_by(
        conn, spec, table=table, group_col=src.get("group_col") or "asin",
        window_days=max(1, int(group.get("window_days") or 30)),
        match=src.get("match") or {})
    rows = [(str(k), float(n)) for k, n in counts.items() if k not in ("", None) and n > 0]
    return rows, str(src.get("catalog_key") or "")


def _signal_ai_traffic(conn, cfg: Mapping, group: Mapping, **_kw
                       ) -> tuple[list[tuple[str, float]], str]:
    from framework.core import ai_traffic
    src = dict((cfg.get("sources") or {}).get("ai_traffic") or {})
    src = _merge(src, group.get("source"))
    prefixes = [p for p in (src.get("prefixes") or []) if p]
    if not prefixes:
        raise LookupError("sources.ai_traffic.prefixes is not configured")
    at_cfg = ai_traffic.config(src.get("config") or {})
    if "statement_timeout_ms" not in (src.get("config") or {}) and cfg.get("statement_timeout_ms"):
        at_cfg["statement_timeout_ms"] = int(cfg["statement_timeout_ms"])
    rows = ai_traffic.landed_paths(conn, cfg=at_cfg, prefixes=prefixes,
                                   limit=max(1, int(src.get("max_paths") or 20000)))
    out: list[tuple[str, float]] = []
    seen: set[str] = set()
    for r in rows:
        path = ai_traffic.normalize_path(str(r.get("path") or ""))
        for prefix in prefixes:
            if not path.startswith(prefix):
                continue
            key = path[len(prefix):].split("/", 1)[0]
            if key and key not in seen:
                seen.add(key)
                out.append((key, float(r.get("score") or 0)))
            break
    return out, str(src.get("catalog_key") or "")


def _signal_sql(conn, cfg: Mapping, group: Mapping, **_kw
                ) -> tuple[list[tuple[str, float]], str]:
    sql = str(group.get("sql") or "").strip()
    if not sql:
        raise LookupError("sql group has no sql")
    with conn.cursor() as cur:
        cur.execute(sql, dict(group.get("params") or {}) or None)
        rows = cur.fetchall()
    out: list[tuple[str, float]] = []
    seen: set[str] = set()
    for i, r in enumerate(rows):
        if isinstance(r, Mapping):
            vals = list(r.values())
        else:
            vals = list(r)
        key = vals[0] if vals else None
        if key in (None, ""):
            continue
        key = str(key)
        if key in seen:
            continue
        seen.add(key)
        try:
            score = float(vals[1]) if len(vals) > 1 and vals[1] is not None else float(len(rows) - i)
        except (TypeError, ValueError):
            score = float(len(rows) - i)
        out.append((key, score))
    return out, str(group.get("catalog_key") or "")


def _signal_storage_list(conn, cfg: Mapping, group: Mapping, *, storage=None, **_kw
                         ) -> tuple[list[tuple[str, float]], str]:
    key = str(group.get("key") or "").strip()
    if not key:
        raise LookupError("storage_list group has no key")
    if storage is None:
        from framework.core.storage import get_storage
        storage = get_storage()
    doc = storage.read_json(key)
    if isinstance(doc, Mapping):
        doc = doc.get("keys") or doc.get("asins") or doc.get("items") or []
    if not isinstance(doc, list):
        raise LookupError(f"storage key {key} holds no list")
    out: list[tuple[str, float]] = []
    seen: set[str] = set()
    n = len(doc)
    for i, item in enumerate(doc):
        k = item.get("asin") or item.get("key") if isinstance(item, Mapping) else item
        if k in (None, "") or str(k) in seen:
            continue
        seen.add(str(k))
        out.append((str(k), float(n - i)))
    return out, str(group.get("catalog_key") or "")


_READERS = {
    "human_clicks": _signal_human_clicks,
    "ai_traffic": _signal_ai_traffic,
    "sql": _signal_sql,
    "storage_list": _signal_storage_list,
}


def _map_keys_to_asins(conn, cfg: Mapping, catalog_key: str,
                       keyed: list[tuple[str, float]]) -> list[tuple[str, float]]:
    """Signal keys -> catalog ASINs, best first. A key on the catalog's ASIN
    column only has to look like an ASIN; any other key column is joined
    through the catalog table (one key may name several ASINs)."""
    cat = cfg.get("catalog") or {}
    asin_col = cat.get("asin_col") or "asin"
    rx = re.compile(str(cat.get("asin_regex") or REAL_ASIN_REGEX))
    if not catalog_key or catalog_key == asin_col:
        return [(k, s) for k, s in keyed if rx.search(k)]
    table = cat.get("table")
    if not table:
        raise LookupError(f"keys on {catalog_key!r} need catalog.table to map to ASINs")
    params = _catalog_params(cat)
    params.update({"prp_keys": [k for k, _ in keyed],
                   "prp_asin_re": str(cat.get("asin_regex") or REAL_ASIN_REGEX)})
    sql = (f"SELECT c.{_ident(catalog_key, 'catalog_key')}::text AS k, "
           f"c.{_ident(asin_col, 'asin_col')} AS asin "
           f"FROM {_ident(table, 'catalog table')} c "
           f"WHERE c.{catalog_key}::text = ANY(%(prp_keys)s) "
           f"AND {_catalog_where(cat)} AND c.{asin_col} ~ %(prp_asin_re)s")
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    by_key: dict[str, list[str]] = {}
    for r in rows:
        k, a = _cell(r, 0, "k"), _cell(r, 1, "asin")
        if k is not None and a:
            by_key.setdefault(str(k), []).append(str(a))
    out: list[tuple[str, float]] = []
    seen: set[str] = set()
    for k, s in keyed:
        for a in sorted(by_key.get(k, [])):
            if a not in seen:
                seen.add(a)
                out.append((a, s))
    return out


def collect_tiers(conn, cfg: Mapping, *, storage=None,
                  max_gate_hours: Optional[float] = None,
                  human_clicks_config: Optional[Mapping] = None) -> Tiers:
    """Read every enabled group's signal and assign each ASIN its tier.

    ``max_gate_hours`` reads only the groups at or below that gate (a
    consumer that only wants the hottest tiers skips the expensive reads).
    A failing signal empties its group and lands in ``Tiers.errors``; it never
    raises. Read-only, and it leaves the caller's transaction as it found it.
    """
    tiers = Tiers()
    groups = [g for g in (cfg.get("groups") or [])
              if max_gate_hours is None or float(g["gate_hours"]) <= max_gate_hours]
    tiers.groups = [dict(g) for g in groups]
    timeout_ms = int(cfg.get("statement_timeout_ms") or 0)
    order = 0
    for rank, g in enumerate(groups):
        name = g["name"]
        tiers.counts.setdefault(name, 0)
        try:
            with _Isolated(conn, timeout_ms):
                keyed, catalog_key = _READERS[g["kind"]](
                    conn, cfg, g, storage=storage,
                    human_clicks_config=human_clicks_config)
                asins = _map_keys_to_asins(conn, cfg, catalog_key, keyed)
        except Exception as e:
            tiers.errors[name] = f"{type(e).__name__}: {e}"[:300]
            tiers.sizes[name] = 0
            continue
        cap = int(g.get("cap") or 0)
        if cap > 0:
            asins = asins[:cap]
        tiers.sizes[name] = len(asins)
        gate = float(g["gate_hours"])
        for asin, score in asins:
            m = tiers.members.get(asin)
            if m is None:
                tiers.members[asin] = {"tier": name, "rank": rank, "gate_hours": gate,
                                       "score": score, "groups": [name], "order": order}
                tiers.counts[name] += 1
                order += 1
            else:
                m["gate_hours"] = min(m["gate_hours"], gate)
                if name not in m["groups"]:
                    m["groups"].append(name)
    return tiers


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------

def _tier_arrays(tiers: Tiers) -> dict:
    asins, ranks, gates, scores = [], [], [], []
    for a, m in tiers.members.items():
        asins.append(a)
        ranks.append(int(m["rank"]))
        gates.append(float(m["gate_hours"]))
        scores.append(float(m["score"]))
    return {"prp_t_asins": asins, "prp_t_ranks": ranks,
            "prp_t_gates": gates, "prp_t_scores": scores}


def build_rank_sql(cfg: Mapping, tiers: Tiers, *, limit: Optional[int] = None,
                   only_asin: Optional[str] = None) -> tuple[str, dict]:
    """(sql, params) returning due ASINs in refresh order. Pure: no DB.

    Columns: asin, rnk, gate_h, score, age_h (NULL = never checked)."""
    cat = cfg.get("catalog") or {}
    table = _ident(cat.get("table"), "catalog table")
    asin = _ident(cat.get("asin_col") or "asin", "asin_col")
    fresh = _fresh_expr(cat)
    tail = cfg.get("long_tail") or {}
    tail_on = bool(tail.get("enabled", True))
    tail_gate = max(0.0, float(tail.get("gate_hours") or 0))
    gates = [float(m["gate_hours"]) for m in tiers.members.values()]
    if tail_on:
        gates.append(tail_gate)
    params = _catalog_params(cat)
    params.update(_tier_arrays(tiers))
    params.update({
        "prp_asin_re": str(cat.get("asin_regex") or REAL_ASIN_REGEX),
        "prp_tail_rank": len(tiers.groups),
        "prp_tail_gate": tail_gate,
        "prp_tail_on": tail_on,
        # A row checked more recently than the smallest gate can never be due.
        "prp_min_gate_s": (min(gates) if gates else 0.0) * 3600.0,
        "prp_limit": int(limit) if limit else None,
    })
    narrow = ""
    if not tail_on:
        narrow = f"\n         AND c.{asin} = ANY(%(prp_t_asins)s::text[])"
    if only_asin:
        params["prp_only"] = only_asin
        narrow += f"\n         AND c.{asin} = %(prp_only)s"
    sql = f"""
    WITH t AS MATERIALIZED (
      SELECT u.asin, MIN(u.rnk) AS rnk, MIN(u.gate_h) AS gate_h, MAX(u.score) AS score
        FROM unnest(%(prp_t_asins)s::text[], %(prp_t_ranks)s::int[],
                    %(prp_t_gates)s::float8[], %(prp_t_scores)s::float8[])
             AS u(asin, rnk, gate_h, score)
       GROUP BY u.asin
    ), c AS MATERIALIZED (
      SELECT c.{asin} AS asin, MIN({fresh}) AS fresh, bool_or({fresh} IS NULL) AS never
        FROM {table} c
       WHERE {_catalog_where(cat)}
         AND c.{asin} ~ %(prp_asin_re)s
         AND ({fresh} IS NULL
              OR {fresh} < NOW() - make_interval(secs => %(prp_min_gate_s)s)){narrow}
       GROUP BY c.{asin}
    )
    SELECT c.asin,
           COALESCE(t.rnk, %(prp_tail_rank)s) AS rnk,
           COALESCE(t.gate_h, %(prp_tail_gate)s) AS gate_h,
           COALESCE(t.score, 0) AS score,
           CASE WHEN c.never THEN NULL
                ELSE EXTRACT(EPOCH FROM (NOW() - c.fresh)) / 3600.0 END AS age_h
      FROM c LEFT JOIN t ON t.asin = c.asin
     WHERE (t.asin IS NOT NULL OR %(prp_tail_on)s)
       AND (c.never
            OR c.fresh < NOW() - make_interval(
                   secs => COALESCE(t.gate_h, %(prp_tail_gate)s) * 3600.0))
     ORDER BY 2, c.never DESC, c.fresh ASC NULLS FIRST, 4 DESC, c.asin
     LIMIT %(prp_limit)s
    """
    return sql, params


def _tier_name(tiers: Tiers, rank: int) -> str:
    return tiers.groups[rank]["name"] if 0 <= rank < len(tiers.groups) else LONG_TAIL


def rank_refresh_candidates(conn, cfg: Mapping, *, limit: Optional[int] = None,
                            storage=None, tiers: Optional[Tiers] = None
                            ) -> list[RefreshCandidate]:
    """Due ASINs in refresh order (see the module docstring), at most `limit`.

    Needs ``catalog.table``. Pass ``tiers`` from :func:`collect_tiers` to reuse
    a membership read. Raises on a catalog query error (the caller falls back
    to its old selector); signal failures only empty their group."""
    if tiers is None:
        tiers = collect_tiers(conn, cfg, storage=storage)
    sql, params = build_rank_sql(cfg, tiers, limit=limit)
    with _Isolated(conn, int(cfg.get("statement_timeout_ms") or 0)):
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    out: list[RefreshCandidate] = []
    for r in rows:
        rank = int(_cell(r, 1, "rnk"))
        age = _cell(r, 4, "age_h")
        out.append(RefreshCandidate(
            asin=str(_cell(r, 0, "asin")), tier=_tier_name(tiers, rank),
            gate_hours=float(_cell(r, 2, "gate_h")),
            age_hours=None if age is None else round(float(age), 2),
            score=float(_cell(r, 3, "score") or 0), rank=rank))
    return out


def tier_counts(candidates: Sequence[RefreshCandidate]) -> dict[str, int]:
    """{tier: n} in first-seen (refresh) order, for logs and metrics."""
    out: dict[str, int] = {}
    for c in candidates:
        out[c.tier] = out.get(c.tier, 0) + 1
    return out


# ---------------------------------------------------------------------------
# Reporting (CLI)
# ---------------------------------------------------------------------------

def tier_freshness(conn, cfg: Mapping, tiers: Tiers) -> list[dict]:
    """Per tier: members, in catalog, fresh (< display_hours), stale, never
    checked, due now; plus the long tail. Read-only."""
    cat = cfg.get("catalog") or {}
    table = _ident(cat.get("table"), "catalog table")
    asin = _ident(cat.get("asin_col") or "asin", "asin_col")
    fresh = _fresh_expr(cat)
    tail = cfg.get("long_tail") or {}
    params = _catalog_params(cat)
    params.update(_tier_arrays(tiers))
    params.update({"prp_asin_re": str(cat.get("asin_regex") or REAL_ASIN_REGEX),
                   "prp_display_s": float(cfg.get("display_hours") or 24) * 3600.0,
                   "prp_tail_rank": len(tiers.groups),
                   "prp_tail_gate": max(0.0, float(tail.get("gate_hours") or 0))})
    sql = f"""
    WITH t AS MATERIALIZED (
      SELECT u.asin, MIN(u.rnk) AS rnk, MIN(u.gate_h) AS gate_h
        FROM unnest(%(prp_t_asins)s::text[], %(prp_t_ranks)s::int[],
                    %(prp_t_gates)s::float8[]) AS u(asin, rnk, gate_h)
       GROUP BY u.asin
    ), c AS MATERIALIZED (
      SELECT c.{asin} AS asin, MIN({fresh}) AS fresh, bool_or({fresh} IS NULL) AS never
        FROM {table} c
       WHERE {_catalog_where(cat)} AND c.{asin} ~ %(prp_asin_re)s
       GROUP BY c.{asin}
    ), j AS (
      SELECT COALESCE(t.rnk, %(prp_tail_rank)s) AS rnk,
             COALESCE(t.gate_h, %(prp_tail_gate)s) AS gate_h,
             t.asin IS NOT NULL AS member, c.asin IS NOT NULL AS in_catalog,
             c.never, c.fresh
        FROM c FULL JOIN t ON t.asin = c.asin
    )
    SELECT rnk, count(*) FILTER (WHERE member) AS members,
           count(*) FILTER (WHERE in_catalog) AS in_catalog,
           count(*) FILTER (WHERE in_catalog AND NOT never
                              AND fresh >= NOW() - make_interval(secs => %(prp_display_s)s)) AS fresh,
           count(*) FILTER (WHERE in_catalog AND NOT never
                              AND fresh < NOW() - make_interval(secs => %(prp_display_s)s)) AS stale,
           count(*) FILTER (WHERE in_catalog AND never) AS never,
           count(*) FILTER (WHERE in_catalog AND (never OR fresh < NOW()
                              - make_interval(secs => gate_h * 3600.0))) AS due
      FROM j GROUP BY rnk ORDER BY rnk
    """
    with _Isolated(conn, int(cfg.get("statement_timeout_ms") or 0)):
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    cols = ("rnk", "members", "in_catalog", "fresh", "stale", "never", "due")
    out = []
    for r in rows:
        d = {c: _cell(r, i, c) for i, c in enumerate(cols)}
        rank = int(d.pop("rnk"))
        tier = _tier_name(tiers, rank)
        d = {k: int(v or 0) for k, v in d.items()}
        if tier == LONG_TAIL:
            d["members"] = d["in_catalog"]
        gate = (tiers.groups[rank]["gate_hours"] if rank < len(tiers.groups)
                else float(tail.get("gate_hours") or 0))
        out.append({"tier": tier, "gate_hours": gate, **d,
                    "fresh_pct": round(100.0 * d["fresh"] / d["in_catalog"], 1)
                    if d["in_catalog"] else None})
    return out


def explain(conn, cfg: Mapping, asin: str, *, storage=None,
            tiers: Optional[Tiers] = None) -> dict:
    """Why an ASIN is (or is not) refreshed next: every group that holds it,
    its tier, gate, age, whether it is due, and its position in the queue."""
    asin = str(asin or "").strip()
    if tiers is None:
        tiers = collect_tiers(conn, cfg, storage=storage)
    m = tiers.members.get(asin)
    out: dict[str, Any] = {
        "asin": asin, "enabled": bool(cfg.get("enabled")),
        "agent_id": cfg.get("agent_id"), "site": cfg.get("site"),
        "groups": list(m["groups"]) if m else [],
        "tier": m["tier"] if m else None,
        "gate_hours": m["gate_hours"] if m else None,
        "score": m["score"] if m else None,
        "signal_errors": dict(tiers.errors),
    }
    if not (cfg.get("catalog") or {}).get("table"):
        out["note"] = "catalog.table not configured: membership only"
        return out
    with _Isolated(conn, int(cfg.get("statement_timeout_ms") or 0)):
        cat = cfg["catalog"]
        tbl = _ident(cat["table"], "catalog table")
        acol = _ident(cat.get("asin_col") or "asin", "asin_col")
        fresh = _fresh_expr(cat)
        params = _catalog_params(cat)
        params["prp_asin"] = asin
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT count(*) AS n, MIN({fresh}) AS fresh, bool_or({fresh} IS NULL) AS never, "
                f"EXTRACT(EPOCH FROM (NOW() - MIN({fresh}))) / 3600.0 AS age_h "
                f"FROM {tbl} c WHERE c.{acol} = %(prp_asin)s AND {_catalog_where(cat)}",
                params)
            row = cur.fetchone()
    n = int(_cell(row, 0, "n") or 0) if row else 0
    out["catalog_rows"] = n
    if not n:
        out["note"] = "not in the catalog (or filtered out by catalog.where)"
        return out
    never = bool(_cell(row, 2, "never"))
    age = _cell(row, 3, "age_h")
    tail = cfg.get("long_tail") or {}
    if m is None:
        out["tier"] = LONG_TAIL if tail.get("enabled", True) else None
        out["gate_hours"] = float(tail.get("gate_hours") or 0) if out["tier"] else None
    out["age_hours"] = None if never or age is None else round(float(age), 2)
    out["never_checked"] = never
    if out["tier"] is None:
        out["due"] = False
        out["note"] = "in no group and the long tail is disabled"
        return out
    out["due"] = never or (out["age_hours"] is not None
                           and out["age_hours"] >= float(out["gate_hours"]))
    if out["due"]:
        queue = rank_refresh_candidates(conn, cfg, tiers=tiers)
        out["queue_total"] = len(queue)
        out["position"] = next((i + 1 for i, c in enumerate(queue) if c.asin == asin), None)
    return out
