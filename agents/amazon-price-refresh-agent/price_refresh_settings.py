"""Settings for the Amazon price-refresh engine: one site.yaml block per site.

A per-site instance is a config-only directory in the site's repo. Its
manifest's entry_command points ``PRICE_REFRESH_CONFIG`` at that directory's
site.yaml (optionally ``path#dotted.key``; the default key is
``price_refresh``) and runs this engine's agent.py. Everything site-specific
is a VALUE in that block: the catalog view, the demand groups and gates, the
Creators budget line, partner tag and resources, the cache table and its
columns. This module never names a site.

Resolution: ``DEFAULTS`` (below) <- the site's block (dicts deep-merge, lists
replace). The ``demand_priority`` sub-block is the site config of
``framework.core.price_refresh_priority`` and is handed to it verbatim, so
its own storage layers (``config/price-refresh-priority-config.json``
defaults / by_site / by_agent_id, including the per-agent demand-ranking off
switch, which does not stop the refresh) still apply on top.

Validation has two layers, both run before any DB or API work:
  * the JSON schema next to this file (types, ranges, unknown keys: a typo in
    an engine-owned block fails fast instead of being ignored);
  * semantic checks the schema cannot express: SQL identifiers, bare '%' in
    trusted SQL that runs with named params, the cron period (required: the
    purge horizon that keeps stored Amazon data under 24h depends on it), a
    partner tag, an explicit resource list that covers the enabled cache
    columns, the demand-priority module's own config problems.

Two derived values: ``demand_priority.catalog.fresh_cols`` defaults to the
cache's ``fetched_at`` column (the catalog view exposes it; the priority
module's own default names a products-table column), and the purge horizon
(``cache.purge.max_age_hours: auto``) is 24h minus the cron period minus
``schedule.max_run_minutes``.

The site and the DSN env var name fall back to ``framework.core.site_registry``
(the site owning the agent id; that site's ``dsn_env``).
"""
from __future__ import annotations

import copy
import json
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from framework.core import price_refresh_priority as prp
from framework.core.amazon_creators import normalize_consumer
from framework.core.amazon_price_refresh import PRICE_RESOURCES

import price_refresh_cache as cache_mod

ENV_CONFIG = "PRICE_REFRESH_CONFIG"
DEFAULT_BLOCK_KEY = "price_refresh"
SCHEMA_PATH = Path(__file__).resolve().with_name("price-refresh-config.schema.json")

DEFAULTS: dict[str, Any] = {
    "site": "",
    "database": {
        "dsn_env": "",
        "statement_timeout_ms": 60000,
        "connect_timeout_s": 15,
        "write_retries": 3,
        "write_retry_backoff_s": 1.0,
        "application_name": "",
    },
    "creators": {
        "consumer": "",
        "partner_tag": "",
        "partner_tag_env": "",
        "marketplace": "",
        "resources": "auto",
        "expected_currency": "USD",
        "max_calls_per_run": 250,
        "max_consecutive_errors": 3,
        "min_interval_s": 1.1,
    },
    "schedule": {
        "cron_period_hours": None,       # required
        "max_run_minutes": cache_mod.DEFAULT_MAX_RUN_MINUTES,
        "tolerate_failed_runs": 1,       # demand groups
        "long_tail_tolerate_failed_runs": 0,
    },
    # framework.core.price_refresh_priority site config. On by default here
    # (the module's own default is off); storage by_agent_id.<id>.enabled=false
    # (the demand-ranking off switch) still switches this instance to plain
    # oldest-first. It does not stop the refresh or the purge.
    "demand_priority": {"enabled": True},
    "cache": {
        "mode": "cache",
        "table": "amazon_item_cache",
        "key_col": "asin",
        "fetched_at_col": "fetched_at",
        "outcome_col": "outcome",
        "consumer_col": "consumer",
        "columns": dict(cache_mod.DEFAULT_COLUMNS),
        "casts": dict(cache_mod.DEFAULT_CASTS),
        "extra_purge_columns": [],
        "max_text_chars": 1000,
        "purge": {"max_age_hours": "auto"},
        "not_found_guard": {"max_share": 0.5, "min_answered": 20},
    },
    "metrics": {
        "indexable_sql": "",
        "min_quotable_price": 1.0,
        # The longest time rendered HTML can sit in a cache (the affiliate
        # kit's maxHtmlCacheAgeSeconds: s-maxage + max-age + SSR cache; 660 at
        # its defaults). A price is quotable only within the display limit
        # minus this (amendment 2).
        "max_html_cache_age_s": 660,
        "tier_freshness": True,
    },
}

_NAMED_PARAM_RE = re.compile(r"%\([A-Za-z_][A-Za-z0-9_]*\)s")


class SettingsError(ValueError):
    """The site's price-refresh config cannot be used; ``problems`` says why."""

    def __init__(self, problems: Sequence[str], source: str = ""):
        self.problems = list(problems)
        self.source = source
        where = f"{source}: " if source else ""
        super().__init__(where + "; ".join(self.problems))


@dataclass(frozen=True)
class Settings:
    """One validated per-site configuration."""

    agent_id: str
    site: str
    source: str
    dsn_env: str
    consumer: str
    partner_tag: str
    partner_tag_env: str
    marketplace: str
    resources: tuple[str, ...]
    expected_currency: str
    max_calls_per_run: int
    max_consecutive_errors: int
    min_interval_s: float
    statement_timeout_ms: int
    connect_timeout_s: int
    write_retries: int
    write_retry_backoff_s: float
    application_name: str
    demand_priority: dict
    cron_period_hours: float
    max_run_minutes: float
    tolerate_failed_runs: int
    long_tail_tolerate_failed_runs: int
    cache: cache_mod.CacheSpec
    not_found_max_share: float
    not_found_min_answered: int
    indexable_sql: str
    min_quotable_price: float
    max_html_cache_age_s: int
    tier_freshness: bool
    warnings: tuple[str, ...] = ()
    block: dict = field(default_factory=dict, compare=False, repr=False)

    def quotable_hours(self, display_hours: Any = None) -> float:
        """The window in which a price may still be quoted: the display limit
        (``demand_priority.display_hours``, at most 24h) minus the longest
        time rendered HTML can sit in a cache (amendment 2; the kit's
        quotableMaxAgeSeconds)."""
        try:
            limit = float(display_hours) if display_hours not in (None, "") else None
        except (TypeError, ValueError):
            limit = None
        if limit is None or limit <= 0:
            limit = cache_mod.MAX_LICENSED_AGE_HOURS
        limit = min(limit, cache_mod.MAX_LICENSED_AGE_HOURS)
        return round(max(0.0, limit - self.max_html_cache_age_s / 3600.0), 4)

    def gate_problems(self, priority_cfg: Mapping[str, Any]) -> list[str]:
        """gate_buffer_problems() for a resolved priority config with this
        instance's schedule, quotable window and purge horizon."""
        return gate_buffer_problems(
            priority_cfg, period_hours=self.cron_period_hours,
            failed_runs=self.tolerate_failed_runs,
            long_tail_failed_runs=self.long_tail_tolerate_failed_runs,
            display_hours=self.quotable_hours(priority_cfg.get("display_hours")),
            run_margin_hours=self.max_run_minutes / 60.0,
            purge_age_hours=self.cache.max_age_hours)

    def partner_tag_value(self, environ: Optional[Mapping[str, str]] = None) -> str:
        """The Associates tag requests go out under: the literal, else the
        value of the env var named by ``partner_tag_env``."""
        if self.partner_tag:
            return self.partner_tag
        env = os.environ if environ is None else environ
        return (env.get(self.partner_tag_env, "") if self.partner_tag_env else "").strip()

    def describe(self) -> dict:
        """A JSON-safe view for logs and the --config CLI (names, never values
        of secrets: the DSN is shown only as its env var name)."""
        return {
            "agent_id": self.agent_id, "site": self.site, "source": self.source,
            "dsn_env": self.dsn_env, "consumer": self.consumer,
            "partner_tag": self.partner_tag or None,
            "partner_tag_env": self.partner_tag_env or None,
            "marketplace": self.marketplace or None, "resources": list(self.resources),
            "expected_currency": self.expected_currency,
            "max_calls_per_run": self.max_calls_per_run,
            "max_consecutive_errors": self.max_consecutive_errors,
            "schedule": {"cron_period_hours": self.cron_period_hours,
                         "max_run_minutes": self.max_run_minutes,
                         "tolerate_failed_runs": self.tolerate_failed_runs,
                         "long_tail_tolerate_failed_runs": self.long_tail_tolerate_failed_runs},
            "cache": {"table": self.cache.table, "key_col": self.cache.key_col,
                      "fetched_at_col": self.cache.fetched_at_col,
                      "outcome_col": self.cache.outcome_col,
                      "consumer_col": self.cache.consumer_col,
                      "columns": dict(self.cache.columns), "casts": dict(self.cache.casts),
                      "api_columns_purged": list(self.cache.api_columns),
                      "purge_horizon_hours": self.cache.max_age_hours,
                      "worst_case_stored_age_hours": cache_mod.worst_case_stored_age_hours(
                          self.cache.max_age_hours, self.cron_period_hours,
                          self.max_run_minutes)},
            "not_found_guard": {"max_share": self.not_found_max_share,
                                "min_answered": self.not_found_min_answered},
            "metrics": {"indexable_sql": self.indexable_sql or None,
                        "min_quotable_price": self.min_quotable_price,
                        "max_html_cache_age_s": self.max_html_cache_age_s,
                        "quotable_hours": self.quotable_hours(
                            self.demand_priority.get("display_hours")),
                        "tier_freshness": self.tier_freshness},
            "warnings": list(self.warnings),
        }


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def _dig(doc: Any, dotted: str) -> Any:
    for part in [p for p in (dotted or "").split(".") if p]:
        doc = doc.get(part) if isinstance(doc, Mapping) else None
    return doc


def read_block(spec: str) -> tuple[dict, str]:
    """``path[#dotted.key]`` -> (block, source). YAML or JSON by extension;
    the key defaults to ``price_refresh``."""
    path, _, key = str(spec).partition("#")
    key = key or DEFAULT_BLOCK_KEY
    p = Path(path).expanduser()
    try:
        text = p.read_text()
    except OSError as exc:
        raise SettingsError([f"cannot read config file: {exc.strerror or exc}"], str(p)) from None
    try:
        if p.suffix.lower() in (".yaml", ".yml"):
            import yaml
            doc = yaml.safe_load(text) or {}
        else:
            doc = json.loads(text)
    except Exception as exc:  # noqa: BLE001 - any parse error is a config error
        raise SettingsError([f"cannot parse config: {type(exc).__name__}: {exc}"[:300]], str(p)) from None
    block = _dig(doc, key)
    if not isinstance(block, Mapping):
        raise SettingsError([f"no '{key}' mapping in the file"], str(p))
    return dict(block), f"{p}#{key}"


def merge(base: Mapping[str, Any], over: Optional[Mapping[str, Any]]) -> dict:
    """Deep-merge: dicts merge, everything else (lists included) replaces."""
    out = copy.deepcopy(dict(base))
    for k, v in (over or {}).items():
        if isinstance(v, Mapping) and isinstance(out.get(k), dict):
            out[k] = merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def schema_problems(block: Mapping[str, Any]) -> list[str]:
    """JSON-schema problems of the site's (unmerged) block, as 'path: message'."""
    try:
        import jsonschema
    except ImportError:  # pragma: no cover - jsonschema is a framework dependency
        return []
    schema = json.loads(SCHEMA_PATH.read_text())
    validator = jsonschema.Draft7Validator(schema)
    out = []
    for err in sorted(validator.iter_errors(dict(block)), key=lambda e: list(e.absolute_path)):
        path = ".".join(str(p) for p in err.absolute_path) or "(block)"
        out.append(f"{path}: {err.message}"[:300])
    return out


def bare_percent(sql: str) -> bool:
    """True when ``sql`` holds a '%' that is neither '%%' nor a named
    parameter. Such SQL raises inside psycopg2 when it runs with named
    params (the reference verifier's rule: double any literal percent)."""
    stripped = _NAMED_PARAM_RE.sub("", (sql or "").replace("%%", ""))
    return "%" in stripped


def _demand_problems(dp: Any, *, agent_id: str, site: str) -> list[str]:
    if not isinstance(dp, Mapping):
        return ["demand_priority must be a mapping (the price_refresh_priority site config)"]
    out: list[str] = []
    cat = dp.get("catalog")
    if not isinstance(cat, Mapping) or not cat.get("table"):
        out.append("demand_priority.catalog.table is required (the site's refresh catalog "
                   "view, e.g. products LEFT JOIN the cache table)")
        cat = cat if isinstance(cat, Mapping) else {}
    if cat.get("table") and not cache_mod.TABLE_RE.match(str(cat.get("table"))):
        out.append("demand_priority.catalog.table must be a SQL identifier")
    if cat.get("asin_col") and not cache_mod.IDENT_RE.match(str(cat.get("asin_col"))):
        out.append("demand_priority.catalog.asin_col must be a SQL identifier")
    fresh = cat.get("fresh_cols")
    if fresh is not None and (not isinstance(fresh, list) or not fresh or not all(
            isinstance(c, str) and cache_mod.IDENT_RE.match(c) for c in fresh)):
        out.append("demand_priority.catalog.fresh_cols must be a non-empty list of SQL identifiers")
    if bare_percent(str(cat.get("where") or "")):
        out.append("demand_priority.catalog.where holds a bare '%': double it ('%%') or use a "
                   "named parameter")
    params = cat.get("params") or {}
    if not isinstance(params, Mapping):
        out.append("demand_priority.catalog.params must be a mapping")
        params = {}
    clash = sorted(k for k in params if str(k).startswith(("prp_", "prf_")))
    if clash:
        out.append(f"demand_priority.catalog.params may not use the reserved prp_/prf_ prefixes: {clash}")
    for key in ("groups", "extra_groups"):
        for i, g in enumerate(dp.get(key) or []):
            if (isinstance(g, Mapping) and g.get("kind") == "sql" and g.get("params")
                    and bare_percent(str(g.get("sql") or ""))):
                out.append(f"demand_priority.{key}[{i}].sql holds a bare '%' and runs with params")
    # The module's own checks (unknown kind, duplicate or reserved names, gates).
    try:
        resolved = prp.resolve_config(agent_id=agent_id, site=site, site_cfg=dict(dp), file_cfg={})
        out.extend(f"demand_priority: {p}" for p in resolved.get("config_problems") or [])
    except Exception as exc:  # noqa: BLE001 - surfaced as a config problem
        out.append(f"demand_priority does not resolve: {type(exc).__name__}: {exc}"[:300])
    return out


def _schedule(merged: Mapping[str, Any]) -> tuple[Optional[float], float]:
    """(cron_period_hours or None, max_run_minutes) of a merged block."""
    sched = merged.get("schedule") or {}
    period = sched.get("cron_period_hours")
    run_min = sched.get("max_run_minutes")
    return (float(period) if isinstance(period, (int, float)) and not isinstance(period, bool)
            else None,
            float(run_min) if isinstance(run_min, (int, float)) and not isinstance(run_min, bool)
            else cache_mod.DEFAULT_MAX_RUN_MINUTES)


def _cache_spec(merged: Mapping[str, Any]) -> cache_mod.CacheSpec:
    period, run_min = _schedule(merged)
    return cache_mod.build_cache_spec(merged.get("cache") or {}, period_hours=period,
                                      max_run_minutes=run_min)


def semantic_problems(merged: Mapping[str, Any], *, agent_id: str = "", site: str = "") -> list[str]:
    """Checks the schema cannot express (see the module docstring)."""
    out: list[str] = []
    cr = merged.get("creators") or {}
    if not (str(cr.get("partner_tag") or "").strip() or str(cr.get("partner_tag_env") or "").strip()):
        out.append("creators.partner_tag (or creators.partner_tag_env) is required: requests and "
                   "the stored detailPageURL must carry this site's own Associates tag, never "
                   "the host's default")
    if cr.get("partner_tag_env") and not cache_mod.IDENT_RE.match(str(cr.get("partner_tag_env"))):
        out.append("creators.partner_tag_env must be an environment variable name")
    if _schedule(merged)[0] is None:
        out.append("schedule.cron_period_hours is required (the instance's cron period, e.g. 4 "
                   "for '5 */4 * * *'): the purge runs once per run, so the horizon that keeps "
                   "stored Amazon data under 24h depends on it")
    db = merged.get("database") or {}
    if db.get("dsn_env") and not cache_mod.IDENT_RE.match(str(db.get("dsn_env"))):
        out.append("database.dsn_env must be an environment variable NAME (never a DSN)")
    spec: Optional[cache_mod.CacheSpec] = None
    try:
        spec = _cache_spec(merged)
    except cache_mod.CacheConfigError as exc:
        out.extend(exc.problems)
    res = cr.get("resources", "auto")
    if res != "auto":
        if not isinstance(res, list) or not all(isinstance(r, str) and r for r in res):
            out.append("creators.resources must be 'auto' or a list of Creators resource names")
        else:
            missing = [r for r in PRICE_RESOURCES if r not in res]
            if missing:
                out.append(f"creators.resources must include the classifier's resources: {missing}")
            if spec is not None:
                # Every answer replaces every enabled column, so a resource the
                # list leaves out would write NULL over that column each run.
                need = [r for r in cache_mod.resources_for(spec)
                        if r not in PRICE_RESOURCES and r not in res]
                for r in need:
                    cols = [f for f in spec.fields if cache_mod.FIELD_RESOURCES.get(f) == r]
                    out.append(f"creators.resources lacks {r}, which the enabled cache column(s) "
                               f"{', '.join(cols)} need: add it, or map the column(s) to null")
    met = merged.get("metrics") or {}
    if bare_percent(str(met.get("indexable_sql") or "")):
        out.append("metrics.indexable_sql holds a bare '%': double it ('%%')")
    out.extend(_demand_problems(merged.get("demand_priority"), agent_id=agent_id, site=site))
    return out


def _registry(storage):
    from framework.core import site_registry
    try:
        return site_registry.load_registry(storage)
    except Exception:  # noqa: BLE001 - the explicit config still works without it
        return None


def build_settings(block: Mapping[str, Any], *, agent_id: str, storage=None,
                   source: str = "") -> Settings:
    """Validate the site's block and resolve it into Settings.

    Raises SettingsError listing every problem."""
    problems = schema_problems(block)
    if problems:
        raise SettingsError(problems, source)
    merged = merge(DEFAULTS, block)
    warnings: list[str] = []
    # The catalog view's freshness column is the cache's fetched_at unless the
    # site says otherwise. Left unset, price_refresh_priority would read its
    # own default (a products-table column the view does not have): every
    # ranking query would fail, or rank on a column this engine never updates.
    cat = (merged.get("demand_priority") or {}).get("catalog")
    if isinstance(cat, dict) and cat.get("fresh_cols") is None:
        cat["fresh_cols"] = [str((merged.get("cache") or {}).get("fetched_at_col") or "fetched_at")]

    reg = None
    site = str(merged.get("site") or "").strip()
    dsn_env = str((merged.get("database") or {}).get("dsn_env") or "").strip()
    if not site or not dsn_env:
        reg = _registry(storage)
    if not site and reg is not None:
        site = reg.site_for_agent(agent_id) or ""
    problems = semantic_problems(merged, agent_id=agent_id, site=site)
    if not dsn_env:
        entry = reg.get_site(site) if (reg is not None and site) else None
        if entry is not None:
            dsn_env = entry.dsn_env
        else:
            problems.append("database.dsn_env is not set and the site registry has no entry for "
                            f"{site or 'this agent'}: name the env var holding the site's DSN")
    cr = merged["creators"]
    consumer = normalize_consumer(cr.get("consumer") or (f"{site}-price-refresh" if site else ""))
    if consumer == normalize_consumer(""):
        problems.append("creators.consumer is required (the Creators budget line this site's "
                        "refresh is charged to)")
    if problems:
        raise SettingsError(problems, source)

    spec = _cache_spec(merged)
    res = cr.get("resources", "auto")
    resources = cache_mod.resources_for(spec) if res == "auto" else tuple(dict.fromkeys(res))
    db = merged["database"]
    sched = merged.get("schedule") or {}
    guard = (merged["cache"].get("not_found_guard") or {})
    met = merged.get("metrics") or {}
    period, run_min = _schedule(merged)
    return Settings(
        agent_id=agent_id, site=site, source=source, dsn_env=dsn_env, consumer=consumer,
        partner_tag=str(cr.get("partner_tag") or "").strip(),
        partner_tag_env=str(cr.get("partner_tag_env") or "").strip(),
        marketplace=str(cr.get("marketplace") or "").strip(),
        resources=tuple(resources),
        expected_currency=str(cr.get("expected_currency") or "USD").upper(),
        max_calls_per_run=int(cr.get("max_calls_per_run")),
        max_consecutive_errors=int(cr.get("max_consecutive_errors")),
        min_interval_s=float(cr.get("min_interval_s")),
        statement_timeout_ms=int(db.get("statement_timeout_ms") or 0),
        connect_timeout_s=int(db.get("connect_timeout_s") or 15),
        write_retries=int(db.get("write_retries")),
        write_retry_backoff_s=float(db.get("write_retry_backoff_s")),
        application_name=str(db.get("application_name") or "").strip(),
        demand_priority=copy.deepcopy(dict(merged.get("demand_priority") or {})),
        cron_period_hours=float(period),
        max_run_minutes=run_min,
        tolerate_failed_runs=int(sched.get("tolerate_failed_runs", 1)),
        long_tail_tolerate_failed_runs=int(sched.get("long_tail_tolerate_failed_runs", 0)),
        cache=spec,
        not_found_max_share=float(guard.get("max_share", 0.5)),
        not_found_min_answered=int(guard.get("min_answered", 20)),
        indexable_sql=str(met.get("indexable_sql") or ""),
        min_quotable_price=float(met.get("min_quotable_price", 1.0)),
        max_html_cache_age_s=int(met.get("max_html_cache_age_s", 660)),
        tier_freshness=bool(met.get("tier_freshness", True)),
        warnings=tuple(warnings),
        block=merged,
    )


def load_settings(spec: Optional[str] = None, *, agent_id: str, storage=None,
                  environ: Optional[Mapping[str, str]] = None) -> Settings:
    """Settings from ``spec`` (``path[#key]``), else from $PRICE_REFRESH_CONFIG."""
    env = os.environ if environ is None else environ
    spec = spec or env.get(ENV_CONFIG, "")
    if not spec:
        raise SettingsError([f"${ENV_CONFIG} is not set: point it at the site's site.yaml "
                             "(path[#dotted.key], default key price_refresh)"])
    block, source = read_block(spec)
    return build_settings(block, agent_id=agent_id, storage=storage, source=source)


# ---------------------------------------------------------------------------
# Schedule check (binding amendment 10)
# ---------------------------------------------------------------------------

def worst_case_age_hours(gate_hours: float, period_hours: float, failed_runs: int = 0) -> float:
    """How old a row gets before a fixed cron of ``period_hours`` re-checks it.

    A row answered a little after tick T is due at T + gate; the first tick
    that sees it due is T + (floor(gate / P) + 1) * P (the reference verifier
    measured exactly this off-by-minutes effect: an 18h gate on a 6h cron
    re-priced at 24h). Each failed run in a row adds one period."""
    p = float(period_hours)
    if p <= 0:
        raise ValueError("period_hours must be positive")
    return (math.floor(float(gate_hours) / p) + 1 + max(0, int(failed_runs))) * p


def gate_buffer_problems(priority_cfg: Mapping[str, Any], *, period_hours: Optional[float],
                         failed_runs: int = 1,
                         display_hours: Optional[float] = None,
                         long_tail_failed_runs: Optional[int] = None,
                         run_margin_hours: float = 0.0,
                         purge_age_hours: Optional[float] = None) -> list[str]:
    """Every group (and the long tail) whose rows would stop being quotable
    before the cron re-checks them, or that leaves no run between falling
    due and the purge.

    A row is re-checked at up to ``worst_case_age_hours(gate, P, f)`` plus
    ``run_margin_hours`` (rows are answered minutes into a run), where ``f``
    is the failed runs in a row the gate must absorb: ``failed_runs`` for the
    demand groups, ``long_tail_failed_runs`` (default the same) for the long
    tail. That age must stay inside ``display_hours`` (the caller passes the
    quotable window: 24h minus the HTML cache age; default the config's
    display_hours, else 24). On a 4h cron with 30-minute runs and a 660 s
    HTML cache: 12h groups and a 15.5h long tail re-check at ~16.5h, and at
    ~20.5h after one failed run; a 16h long tail at ~20.5h, but ~24.5h after
    one failed run; the draft's 22h long tail at ~24.5h with no failure at all.

    Against ``purge_age_hours`` (the horizon): a gate within one run of it
    means rows are purged before they ever fall due. A gate whose FIRST
    re-check (no failed run) already lands past it leaves no slack: a row
    that run cannot answer (the budget runs out before it) is NULLed by that
    same run's purge, while it is still displayable. The 16h long tail on a
    4h cron is that case (first re-check ~20.5h, horizon 19.5h); a gate at
    least a run under 16h (15.5h) first re-checks at ~16.5h, so a row the
    budget misses keeps its values until the next run re-asks it."""
    if not period_hours:
        return []
    limit = float(display_hours or priority_cfg.get("display_hours") or 24)
    margin = max(0.0, float(run_margin_hours or 0.0))
    tail_runs = failed_runs if long_tail_failed_runs is None else long_tail_failed_runs
    gates = [(str(g.get("name")), float(g.get("gate_hours") or 0), int(failed_runs))
             for g in priority_cfg.get("groups") or []]
    tail = priority_cfg.get("long_tail") or {}
    if tail.get("enabled", True):
        gates.append((prp.LONG_TAIL, float(tail.get("gate_hours") or 0), int(tail_runs)))
    out = []
    for name, gate, runs in gates:
        normal = worst_case_age_hours(gate, period_hours, 0) + margin
        worst = worst_case_age_hours(gate, period_hours, runs) + margin
        if worst > limit:
            after = f", ~{worst:g}h after {runs} failed run(s)" if runs else ""
            out.append(f"{name}: a {gate:g}h gate on a {period_hours:g}h cron re-checks rows at "
                       f"up to ~{normal:g}h{after}, past the {limit:g}h quotable window")
        if purge_age_hours is None:
            continue
        horizon = float(purge_age_hours)
        if gate + margin >= horizon:
            out.append(f"{name}: a {gate:g}h gate is not a run ({margin * 60:g} min) below the "
                       f"{horizon:g}h purge horizon: rows are purged before they fall due")
        elif normal > horizon:
            out.append(f"{name}: a {gate:g}h gate on a {period_hours:g}h cron first re-checks rows "
                       f"at ~{normal:g}h, past the {horizon:g}h purge horizon: a row that run "
                       "cannot answer (the budget runs out before it) is NULLed by its own purge "
                       f"while still displayable; {_slack_hint(period_hours, margin, horizon)}")
    return out


def _slack_hint(period_hours: float, margin: float, horizon: float) -> str:
    """How to give a gate one run of slack before the purge horizon."""
    p = float(period_hours)
    # The first re-check of a gate g is (floor(g / P) + 1) * P; it must be at
    # most horizon - margin, so g < ceiling.
    ceiling = math.floor((horizon - margin) / p) * p
    if ceiling <= 0:
        return "shorten the cron: no gate leaves a run before the purge horizon"
    if 0 < margin < ceiling:
        return (f"lower the gate below {ceiling:g}h by at least a run (e.g. {ceiling - margin:g}h) "
                "or shorten the cron")
    return f"lower the gate below {ceiling:g}h or shorten the cron"
