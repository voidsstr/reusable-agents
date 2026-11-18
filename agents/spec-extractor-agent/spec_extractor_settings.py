"""Settings for the spec-extractor engine: one site.yaml block per site.

A per-site instance is a config-only directory in the site's repo. Its
manifest's entry_command points ``SPEC_EXTRACTOR_CONFIG`` at that
directory's site.yaml (optionally ``path#dotted.key``; the default key is
``spec_extractor``) and runs this engine's agent.py. Everything about the
site is a VALUE in that block:

* ``tables``    the TABLE CONTRACT: every table and column the engine reads
                or writes (models, spec_sources, spec_values, products), so
                the engine never assumes a schema;
* ``kinds``     per product kind: an extra framework.core.spec_sources
                config layer (e.g. its own profile), the field subset that
                kind has, the fields that make it "spec-complete", and the
                typed spec table its accepted values are projected into;
* ``brands``    how the site's brand values map to spec_sources brand slugs,
                and an optional allow/deny list;
* ``sources``   fetch scheduling (which source kinds are fetched, refetch
                age, per-run caps, retry backoff);
* ``compat``    the framework.core.compat_graph layer and where fit lists
                and operator fit records come from;
* ``runtime``   the optional capacity / draw estimate phase;
* ``metrics``   which kinds are the "primary" catalog for the completeness
                goal.

The niche itself (fields, extractors, rules, connectors, rulesets) is never
here: it lives in the storage configs of the two primitives
(config/spec-sources-config.json, config/compat-graph-config.json) as named
profiles that the block selects. This module never names a site.

Validation runs before any DB, HTTP or LLM work, in two layers: the JSON
schema next to this file (types, unknown keys), then semantic checks the
schema cannot express (SQL identifiers, trusted SQL fragments, source kinds,
intervals, required runtime fields).
"""
from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from framework.core import spec_sources as ss
from framework.core.site_registry import ENV_PATTERN

ENV_CONFIG = "SPEC_EXTRACTOR_CONFIG"
DEFAULT_BLOCK_KEY = "spec_extractor"
SCHEMA_PATH = Path(__file__).resolve().with_name("spec-extractor-config.schema.json")

#: Source kinds fetched by default. measured-test and manual-entry rows carry
#: operator-entered values (spec_values rows with extractor 'manual') and are
#: never fetched unless a site lists them here.
DEFAULT_FETCH_KINDS = ["manufacturer-page", "manual-pdf", "manufacturer-faq", "cert-listing"]

DEFAULTS: dict[str, Any] = {
    "site": "",
    "database": {
        "dsn_env": "",
        "statement_timeout_ms": 120000,
        "connect_timeout_s": 15,
        "application_name": "",
        # One run per instance at a time: a session-level advisory lock in
        # the site DB, keyed by this string (default "spec-extractor:<agent id>").
        "lock_key": "",
    },
    "tables": {
        "models": {
            # Read from `relation` (a table or a view that joins the brand);
            # status columns are written to `write_table` (default: relation).
            "relation": "models",
            "write_table": "",
            "where": "",
            "columns": {"id": "id", "kind": "kind", "brand": "brand", "name": "name",
                        "model_number": None, "aliases": None},
            "status_columns": {"status": None, "completeness": None, "checked_at": None},
        },
        "sources": {
            "table": "spec_sources",
            "columns": {"id": "id", "model_id": "model_id", "kind": "kind", "url": "url",
                        "publisher": "publisher", "fetched_at": "fetched_at",
                        "content_sha256": "content_sha256", "state": "fetch_state"},
        },
        "values": {
            "table": "spec_values",
            "columns": {"id": "id", "model_id": "model_id", "field": "field",
                        "value_num": "value_num", "value_text": "value_text", "unit": "unit",
                        "source_id": "source_id", "extractor": "extractor",
                        "extractor_id": "extractor_id", "confidence": "confidence",
                        "status": "status", "snippet": None, "qualifier": None,
                        "conflict_id": None},
        },
        "products": {
            "table": "",
            "where": "",
            "columns": {"id": "id", "model_id": "model_id", "kind": None},
        },
    },
    # Common framework.core.spec_sources site_cfg layer (profile, fetch user
    # agent, llm / adjudication switches). Fetching, refusal and
    # adjudication settings are read from this layer only.
    "spec_sources": {},
    # {kind: {spec_sources: {...extra layer...}, fields: [...], required: [...],
    #         typed: {table, key, columns: {field: column}, completeness, updated_at}}}
    "kinds": {},
    "brands": {"map": {}, "only": [], "skip": []},
    "sources": {
        "fetch_kinds": list(DEFAULT_FETCH_KINDS),
        "refetch_hours": 20.0,
        "max_fetches_per_run": 300,
        "max_run_minutes": 40.0,
        "retry_base_hours": 6.0,
        "retry_max_hours": 168.0,
        "gone_retry_hours": 168.0,
        # Trusted SQL whose first column is a model id: those models' sources
        # are fetched first, in the query's order (e.g. by demand).
        "priority_sql": "",
    },
    "consolidate": {
        "revisit_after_runs": 14,
        "max_models_per_run": 5000,
        "batch_models": 100,
    },
    "compat": {
        "enabled": False,
        # framework.core.compat_graph site_cfg layer (profile, writer table...).
        "graph": {},
        "batch_size": 10,
        "full_rebuild_every_runs": 14,
        "number_fields": ["model_number"],
        # [{field, kinds, relation, subject_side, verdict, list_kinds}]
        "fit_lists": [],
        "fit_records": {
            "table": "",
            "where": "",
            "columns": {"a": "a_model_id", "b": "b_model_id", "relation": "relation",
                        "verdict": "verdict", "origin": "origin", "config": "config",
                        "via": "via_model_id", "source_url": "source_url",
                        "source_kind": "source_kind", "publisher": "publisher",
                        "source_id": "source_id", "statement": "statement"},
        },
        "connectors": {
            "table": "",
            "columns": {"id": "id", "label": "label", "family": "family",
                        "max_a": "max_a", "max_v": "max_v"},
        },
        # On a full rebuild, rows of rulesets the profile has DISABLED are
        # deleted ("disabled", the default: disabling a ruleset withdraws its
        # verdicts); "all" also deletes rows of rulesets the profile does
        # not know; "none" only counts them. Manual rows are never touched.
        "purge_orphaned": "disabled",
    },
    "runtime": {
        "enabled": False,
        "kinds": [],
        "require_complete": True,
        "energy": {
            "rated_field": "",
            "measured_field": "",
            # The usable share of a RATED capacity is niche data (e.g. an
            # inverter's losses): no default, required with rated_field.
            "rated_factor": None,
            "rated_interval": None,
            "measured_interval": [1.0, 1.0],
            "basis_labels": {"measured": "measured", "rated": "rated-x-{factor}"},
        },
        "continuous_field": "",
        "peak_field": "",
        "round_hours": 2,
        "presets": {
            "table": "",
            "where": "",
            "columns": {"id": "id", "power_w": "power_w", "peak_w": None, "duty_cycle": None,
                        "energy_per_day_wh": None},
        },
        "output": {
            "table": "runtime_estimates",
            "columns": {"model_id": "model_id", "preset_id": "preset_id", "basis": "basis",
                        "usable_energy": "usable_energy", "hours_est": "hours_est",
                        "hours_low": "hours_low", "hours_high": "hours_high",
                        "peak_ok": "peak_ok", "computed_at": None},
        },
    },
    "metrics": {"primary_kinds": []},
}

#: Columns a table contract must name (null is not allowed for these).
REQUIRED_COLUMNS = {
    "models": ("id", "kind"),
    "sources": ("id", "model_id", "kind", "url", "fetched_at", "content_sha256", "state"),
    "values": ("model_id", "field", "value_num", "value_text", "unit", "source_id", "extractor",
               "extractor_id", "confidence", "status"),
    "products": ("id", "model_id"),
    "fit_records": ("a", "b", "relation"),
    "connectors": ("id",),
    "presets": ("id", "power_w"),
    "output": ("model_id", "preset_id", "basis", "usable_energy", "hours_est", "hours_low",
               "hours_high", "peak_ok"),
}

_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)?$")
_COLUMN_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
_NAMED_PARAM_RE = re.compile(r"%\([A-Za-z_][A-Za-z0-9_]*\)s")
_ENV_RE = re.compile(ENV_PATTERN)
_DSN_ENV_RULE = ("must be the NAME of an environment variable (A-Z, 0-9, _), never a "
                 "connection string")
PURGE_MODES = ("disabled", "all", "none")


class SettingsError(ValueError):
    """The site's spec-extractor config cannot be used; ``problems`` says why."""

    def __init__(self, problems: Sequence[str], source: str = ""):
        self.problems = list(problems)
        self.source = source
        where = f"{source}: " if source else ""
        super().__init__(where + "; ".join(self.problems))


def quote_ident(name: str) -> str:
    """A validated identifier (``table``, ``schema.table`` or ``column``),
    double-quoted. Identifiers are lower-case only, so quoting never changes
    which object they name."""
    if not _IDENT_RE.match(name or ""):
        raise SettingsError([f"invalid SQL identifier {name!r}"])
    return ".".join(f'"{p}"' for p in name.split("."))


def bare_percent(sql: str) -> bool:
    """True when ``sql`` holds a '%' that is neither '%%' nor a named
    parameter (psycopg2 would fail on it: trusted fragments run with params)."""
    stripped = _NAMED_PARAM_RE.sub("", (sql or "").replace("%%", ""))
    return "%" in stripped


def merge(base: Mapping[str, Any], over: Optional[Mapping[str, Any]]) -> dict:
    """Deep-merge: dicts merge, everything else (lists included) replaces."""
    out = copy.deepcopy(dict(base))
    for k, v in (over or {}).items():
        if isinstance(v, Mapping) and isinstance(out.get(k), dict):
            out[k] = merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _dig(doc: Any, dotted: str) -> Any:
    for part in [p for p in (dotted or "").split(".") if p]:
        doc = doc.get(part) if isinstance(doc, Mapping) else None
    return doc


def read_block(spec: str) -> tuple[dict, str]:
    """``path[#dotted.key]`` -> (block, source). YAML or JSON by extension;
    the key defaults to ``spec_extractor``."""
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
        raise SettingsError([f"cannot parse config: {type(exc).__name__}: {exc}"[:300]],
                            str(p)) from None
    block = _dig(doc, key)
    if not isinstance(block, Mapping):
        raise SettingsError([f"no '{key}' mapping in the file"], str(p))
    return dict(block), f"{p}#{key}"


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
        if list(err.absolute_path)[-1:] == ["dsn_env"]:
            # never echo the value: it may be a pasted connection string
            out.append(f"{path}: {_DSN_ENV_RULE}")
            continue
        out.append(f"{path}: {err.message}"[:300])
    return out


@dataclass(frozen=True)
class TableSpec:
    """One contract table: its name, logical -> physical column map (None =
    the table has no such column) and an optional trusted WHERE fragment."""
    name: str
    columns: Mapping[str, Optional[str]]
    where: str = ""

    def col(self, logical: str) -> Optional[str]:
        return self.columns.get(logical)

    def has(self, logical: str) -> bool:
        return bool(self.columns.get(logical))


@dataclass(frozen=True)
class KindSettings:
    """One product kind."""
    kind: str
    spec_sources: Mapping[str, Any]
    fields: Optional[tuple[str, ...]]
    required: Optional[tuple[str, ...]]
    typed: Optional[TableSpec]
    typed_key: str = "model_id"
    typed_completeness: str = ""
    typed_updated_at: str = ""


@dataclass(frozen=True)
class Settings:
    """One validated per-site configuration."""
    agent_id: str
    site: str
    source: str
    dsn_env: str
    block: Mapping[str, Any]
    kinds: Mapping[str, KindSettings]
    warnings: tuple[str, ...] = ()

    # -- convenience ----------------------------------------------------
    @property
    def database(self) -> Mapping[str, Any]:
        return self.block["database"]

    @property
    def lock_key(self) -> str:
        return str(self.database.get("lock_key") or f"spec-extractor:{self.agent_id}")

    @property
    def models(self) -> TableSpec:
        m = self.block["tables"]["models"]
        return TableSpec(str(m["relation"]), dict(m["columns"]), str(m.get("where") or ""))

    @property
    def models_write_table(self) -> str:
        m = self.block["tables"]["models"]
        return str(m.get("write_table") or m["relation"])

    @property
    def model_status_columns(self) -> Mapping[str, Optional[str]]:
        return dict(self.block["tables"]["models"].get("status_columns") or {})

    @property
    def sources(self) -> TableSpec:
        s = self.block["tables"]["sources"]
        return TableSpec(str(s["table"]), dict(s["columns"]))

    @property
    def values(self) -> TableSpec:
        v = self.block["tables"]["values"]
        return TableSpec(str(v["table"]), dict(v["columns"]))

    @property
    def products(self) -> Optional[TableSpec]:
        p = self.block["tables"]["products"]
        if not p.get("table"):
            return None
        return TableSpec(str(p["table"]), dict(p["columns"]), str(p.get("where") or ""))

    @property
    def fetching(self) -> Mapping[str, Any]:
        return self.block["sources"]

    @property
    def compat(self) -> Mapping[str, Any]:
        return self.block["compat"]

    @property
    def runtime(self) -> Mapping[str, Any]:
        return self.block["runtime"]

    def describe(self) -> dict:
        """The resolved settings (what --config prints)."""
        return {"agent_id": self.agent_id, "site": self.site, "source": self.source,
                "dsn_env": self.dsn_env, "kinds": sorted(self.kinds),
                "warnings": list(self.warnings), "block": copy.deepcopy(dict(self.block))}


# ---------------------------------------------------------------------------
# Semantic validation
# ---------------------------------------------------------------------------

def _ident_problems(where: str, name: Any, *, column: bool = False) -> list[str]:
    rx = _COLUMN_RE if column else _IDENT_RE
    if not isinstance(name, str) or not rx.match(name):
        what = "column" if column else "table"
        return [f"{where}: invalid {what} name {name!r} (lower-case SQL identifier)"]
    return []


def _columns_problems(where: str, cols: Any, required: Sequence[str]) -> list[str]:
    out: list[str] = []
    if not isinstance(cols, Mapping):
        return [f"{where}.columns: must be a mapping"]
    for logical, phys in cols.items():
        if phys is None:
            if logical in required:
                out.append(f"{where}.columns.{logical}: required by the table contract")
            continue
        out += _ident_problems(f"{where}.columns.{logical}", phys, column=True)
    for logical in required:
        if logical not in cols:
            out.append(f"{where}.columns.{logical}: required by the table contract")
    return out


def _fragment_problems(where: str, sql: Any) -> list[str]:
    if not sql:
        return []
    if not isinstance(sql, str):
        return [f"{where}: must be a string of trusted SQL"]
    out = []
    if ";" in sql:
        out.append(f"{where}: a fragment may not contain ';'")
    if bare_percent(sql):
        out.append(f"{where}: double every literal '%' (the fragment runs with named params)")
    return out


def semantic_problems(merged: Mapping[str, Any]) -> list[str]:
    out: list[str] = []
    t = merged["tables"]
    m = t["models"]
    out += _ident_problems("tables.models.relation", m.get("relation"))
    if m.get("write_table"):
        out += _ident_problems("tables.models.write_table", m.get("write_table"))
    out += _columns_problems("tables.models", m.get("columns"), REQUIRED_COLUMNS["models"])
    out += _fragment_problems("tables.models.where", m.get("where"))
    for k, col in (m.get("status_columns") or {}).items():
        if col is not None:
            out += _ident_problems(f"tables.models.status_columns.{k}", col, column=True)
    s = t["sources"]
    out += _ident_problems("tables.sources.table", s.get("table"))
    out += _columns_problems("tables.sources", s.get("columns"), REQUIRED_COLUMNS["sources"])
    v = t["values"]
    out += _ident_problems("tables.values.table", v.get("table"))
    out += _columns_problems("tables.values", v.get("columns"), REQUIRED_COLUMNS["values"])
    p = t["products"]
    if p.get("table"):
        out += _ident_problems("tables.products.table", p.get("table"))
        out += _columns_problems("tables.products", p.get("columns"), REQUIRED_COLUMNS["products"])
        out += _fragment_problems("tables.products.where", p.get("where"))

    kinds = merged.get("kinds") or {}
    if not kinds:
        out.append("kinds: name at least one product kind to extract specs for")
    for kind, kc in kinds.items():
        where = f"kinds.{kind}"
        for key in ("fields", "required"):
            lst = kc.get(key)
            if lst is not None and (not isinstance(lst, list) or not all(isinstance(x, str) for x in lst)):
                out.append(f"{where}.{key}: must be a list of field names")
        if kc.get("fields") and kc.get("required"):
            extra = sorted(set(kc["required"]) - set(kc["fields"]))
            if extra:
                out.append(f"{where}.required: {extra} not in {where}.fields")
        typed = kc.get("typed")
        if typed:
            out += _ident_problems(f"{where}.typed.table", typed.get("table"))
            out += _ident_problems(f"{where}.typed.key", typed.get("key") or "model_id", column=True)
            for f, col in (typed.get("columns") or {}).items():
                out += _ident_problems(f"{where}.typed.columns.{f}", col, column=True)
            for k in ("completeness", "updated_at"):
                if typed.get(k):
                    out += _ident_problems(f"{where}.typed.{k}", typed[k], column=True)
            if not typed.get("columns"):
                out.append(f"{where}.typed.columns: map at least one field to a column")

    src = merged["sources"]
    bad = sorted(set(src.get("fetch_kinds") or []) - set(ss.SOURCE_KINDS))
    if bad:
        out.append(f"sources.fetch_kinds: unknown source kinds {bad} (known: {list(ss.SOURCE_KINDS)})")
    if "manual-entry" in (src.get("fetch_kinds") or []):
        out.append("sources.fetch_kinds: manual-entry sources carry operator values and are never fetched")
    if float(src.get("retry_base_hours") or 0) > float(src.get("retry_max_hours") or 0):
        out.append("sources.retry_base_hours: must not exceed sources.retry_max_hours")
    out += _fragment_problems("sources.priority_sql", src.get("priority_sql"))

    cp = merged["compat"]
    if cp.get("enabled"):
        fr = cp.get("fit_records") or {}
        if fr.get("table"):
            out += _ident_problems("compat.fit_records.table", fr.get("table"))
            out += _columns_problems("compat.fit_records", fr.get("columns"),
                                     REQUIRED_COLUMNS["fit_records"])
            out += _fragment_problems("compat.fit_records.where", fr.get("where"))
        cn = cp.get("connectors") or {}
        if cn.get("table"):
            out += _ident_problems("compat.connectors.table", cn.get("table"))
            out += _columns_problems("compat.connectors", cn.get("columns"),
                                     REQUIRED_COLUMNS["connectors"])
        for i, fl in enumerate(cp.get("fit_lists") or []):
            if fl.get("subject_side", "b") not in ("a", "b"):
                out.append(f"compat.fit_lists[{i}].subject_side: must be 'a' or 'b'")
        if cp.get("purge_orphaned", "disabled") not in PURGE_MODES:
            out.append(f"compat.purge_orphaned: one of {list(PURGE_MODES)}")

    rt = merged["runtime"]
    if rt.get("enabled"):
        if not rt.get("kinds"):
            out.append("runtime.kinds: name the kinds to estimate (runtime is enabled)")
        missing_kinds = sorted(set(rt.get("kinds") or []) - set(kinds))
        if missing_kinds:
            out.append(f"runtime.kinds: {missing_kinds} are not configured under kinds")
        en = rt.get("energy") or {}
        if not en.get("rated_field") and not en.get("measured_field"):
            out.append("runtime.energy: set rated_field and/or measured_field")
        if not rt.get("continuous_field"):
            out.append("runtime.continuous_field: required (a load is estimated only when the "
                       "model's continuous output covers it)")
        keys = ["measured_interval"]
        if en.get("rated_field"):
            keys.append("rated_interval")
            if not isinstance(en.get("rated_factor"), (int, float)) or isinstance(en.get("rated_factor"), bool):
                out.append("runtime.energy.rated_factor: required with rated_field (the usable share of a "
                           "rated capacity is niche data: the engine has no default)")
            if en.get("rated_interval") is None:
                out.append("runtime.energy.rated_interval: required with rated_field ([low, high] around "
                           "rated_factor)")
                keys.remove("rated_interval")
        for key in keys:
            iv = en.get(key) or []
            if len(iv) != 2 or not all(isinstance(x, (int, float)) for x in iv) or not (0 < iv[0] <= iv[1]):
                out.append(f"runtime.energy.{key}: must be [low, high] with 0 < low <= high")
        fac = en.get("rated_factor")
        iv = en.get("rated_interval") or [0, 0]
        if isinstance(fac, (int, float)) and len(iv) == 2 and not (iv[0] <= fac <= iv[1]):
            out.append("runtime.energy.rated_factor: must lie inside rated_interval")
        pr = rt.get("presets") or {}
        if not pr.get("table"):
            out.append("runtime.presets.table: required (runtime is enabled)")
        else:
            out += _ident_problems("runtime.presets.table", pr.get("table"))
            out += _columns_problems("runtime.presets", pr.get("columns"), REQUIRED_COLUMNS["presets"])
            out += _fragment_problems("runtime.presets.where", pr.get("where"))
        op = rt.get("output") or {}
        out += _ident_problems("runtime.output.table", op.get("table"))
        out += _columns_problems("runtime.output", op.get("columns"), REQUIRED_COLUMNS["output"])
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
    problems = semantic_problems(merged)
    warnings: list[str] = []

    site = str(merged.get("site") or "").strip()
    dsn_env = str((merged.get("database") or {}).get("dsn_env") or "").strip()
    reg = None
    if not site or not dsn_env:
        reg = _registry(storage)
    if not site and reg is not None:
        site = reg.site_for_agent(agent_id) or ""
    if not dsn_env:
        entry = reg.get_site(site) if (reg is not None and site) else None
        if entry is not None:
            dsn_env = entry.dsn_env
        else:
            problems.append("database.dsn_env is not set and the site registry has no entry for "
                            f"{site or 'this agent'}: name the env var holding the site's DSN")
    if dsn_env and not _ENV_RE.match(dsn_env):
        problems.append(f"database.dsn_env: {_DSN_ENV_RULE}")
        dsn_env = ""
    if problems:
        raise SettingsError(problems, source)

    kinds: dict[str, KindSettings] = {}
    for kind, kc in (merged.get("kinds") or {}).items():
        typed = kc.get("typed") or None
        tspec = None
        if typed:
            tspec = TableSpec(str(typed["table"]), dict(typed.get("columns") or {}))
        kinds[str(kind)] = KindSettings(
            kind=str(kind), spec_sources=dict(kc.get("spec_sources") or {}),
            fields=tuple(kc["fields"]) if kc.get("fields") else None,
            required=tuple(kc["required"]) if kc.get("required") is not None else None,
            typed=tspec,
            typed_key=str((typed or {}).get("key") or "model_id"),
            typed_completeness=str((typed or {}).get("completeness") or ""),
            typed_updated_at=str((typed or {}).get("updated_at") or ""))
    primary = list((merged.get("metrics") or {}).get("primary_kinds") or [])
    if not primary:
        warnings.append("metrics.primary_kinds is empty: primary_spec_complete_pct covers every kind")
    elif sorted(set(primary) - set(kinds)):
        warnings.append(f"metrics.primary_kinds {sorted(set(primary) - set(kinds))} are not "
                        "configured under kinds (they count as never complete)")
    llm_on = bool(((merged.get("spec_sources") or {}).get("llm") or {}).get("enabled"))
    if llm_on and not merged["sources"].get("fetch_kinds"):
        warnings.append("llm.enabled with no fetch_kinds: nothing will be sent")
    if merged["compat"].get("enabled"):
        for i, fl in enumerate(merged["compat"].get("fit_lists") or []):
            missing = sorted(set(fl.get("kinds") or []) - set(kinds))
            if missing:
                warnings.append(f"compat.fit_lists[{i}].kinds {missing} are not configured under kinds: "
                                f"their {fl.get('field')!r} values are never extracted (only operator "
                                "values can feed this list)")
    return Settings(agent_id=agent_id, site=site, source=source, dsn_env=dsn_env,
                    block=merged, kinds=kinds, warnings=tuple(warnings))


def load_settings(spec: Optional[str] = None, *, agent_id: str, storage=None,
                  environ: Optional[Mapping[str, str]] = None) -> Settings:
    """Settings from ``spec`` (``path[#key]``), else from $SPEC_EXTRACTOR_CONFIG."""
    import os
    env = os.environ if environ is None else environ
    spec = spec or env.get(ENV_CONFIG, "")
    if not spec:
        raise SettingsError([f"${ENV_CONFIG} is not set: point it at the site's site.yaml "
                             "(path[#dotted.key], default key spec_extractor)"])
    block, source = read_block(spec)
    return build_settings(block, agent_id=agent_id, storage=storage, source=source)
