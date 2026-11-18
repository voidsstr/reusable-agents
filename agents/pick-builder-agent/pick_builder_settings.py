"""Settings for the pick-builder engine: one site.yaml block per site.

A per-site instance is a config-only directory in the site's repo. Its
manifest's entry_command points ``PICK_BUILDER_CONFIG`` at that directory's
site.yaml (optionally ``path#dotted.key``; the default key is
``pick_builder``) and runs this engine's agent.py.

Everything about the site is a VALUE in that block:

* ``eligibility``  the ONE relation picks may come from (e.g. the site's
                   v_pick_eligible view), its columns, the attributes the
                   selectors may read, the attributes no stored text may
                   name (``never_store``: values from the Amazon API besides
                   price / list price / currency, which are always hidden),
                   and the engine's own price-integrity floor (applied again
                   on top of the view);
* ``compat``       optional compat_edges table for "fits the station" slots,
                   the verdicts a slot accepts by default and the edge flags
                   that disqualify an edge;
* ``ladders``      ladder / tier / slot tables and one slot PLAN per ladder
                   kind (ordered slots, each a selector);
* ``lists``        best-list tables and one selector per list kind;
* ``substitutes``  the subjects relation (every product that may need a
                   substitute), the output table and per-kind matching rules;
* ``publish``      the storage key of the pick-ASIN list that
                   framework.core.price_refresh_priority reads (a
                   ``storage_list`` group).

A SELECTOR is data: ``filters`` ({attr, op, value | expr}), an optional
``compat`` or ``via_of`` constraint against an earlier slot, ``sort`` keys
(attribute names or small arithmetic expressions such as
``usable_wh / price``), ``count``, ``max_per`` diversity caps,
``one_per_model``, ``max_budget_share``, ``budget`` (kit | own) and a
``reason`` template. Values may reference the tier / list / ladder
parameters as ``{tier.required_wh}`` or ``{list.max_price|500}``. A filter
that cannot be evaluated (a missing parameter) leaves its slot EMPTY with
that reason: policy is never loosened. This module never names a site or a
niche.
"""
from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from framework.core import compat_graph as cg
from framework.core.site_registry import ENV_PATTERN

ENV_CONFIG = "PICK_BUILDER_CONFIG"
DEFAULT_BLOCK_KEY = "pick_builder"
SCHEMA_PATH = Path(__file__).resolve().with_name("pick-builder-config.schema.json")

FILTER_OPS = ("eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in", "truthy", "falsy",
              "is_null", "not_null")
SUBJECT_OPS = ("within_pct", "gte_subject", "lte_subject", "eq_subject")
#: A ladder slot is ONE unit, so by default it accepts only the verdicts that
#: can describe one unit (the engine still checks the edge's configuration:
#: a fits_with_adapter edge that needs a series string does not qualify).
FIT_VERDICTS = [cg.FITS, cg.FITS_WITH_ADAPTER]
#: compat_graph flags that disqualify an edge for an automated pick:
#: a safety check (cold voltage, a connector rating) could not run.
EXCLUDE_FLAGS = ["computed-check-unknown"]
#: Eligibility columns from the Amazon API: never in stored text.
AMAZON_FIELDS = ("price", "list_price", "currency")
#: Subject columns a substitute rule may compare against without mapping.
SUBJECT_BASE = ("product_id", "asin", "model_id", "kind")
BUDGET_MODES = ("kit", "own")

DEFAULTS: dict[str, Any] = {
    "site": "",
    "database": {
        "dsn_env": "",
        "statement_timeout_ms": 60000,
        "connect_timeout_s": 15,
        "application_name": "",
        "lock_key": "",
    },
    "eligibility": {
        "relation": "",
        "where": "",
        "columns": {"product_id": "product_id", "asin": "asin", "model_id": "model_id",
                    "kind": "kind", "price": "price", "list_price": None, "currency": None,
                    "category_confidence": None},
        "attributes": {},
        "never_store": [],
        # The CLAUDE.md pricing-integrity floor, applied again on every row
        # the view returns: a row failing it is never picked and is counted
        # (pool_integrity_rejects), because it means the view is wrong.
        "integrity": {"min_price": 1.0, "currencies": ["USD"], "list_price_not_below_price": True,
                      "min_category_confidence": 0.5, "asin_regex": "^B[0-9A-Z]{9}$"},
    },
    "compat": {
        "table": "",
        "columns": {"a_model_id": "a_model_id", "b_model_id": "b_model_id", "relation": "relation",
                    "verdict": "verdict", "via_model_id": "via_model_id", "config": "config",
                    "rule_id": "rule_id", "confidence": "confidence"},
        "fit_verdicts": list(FIT_VERDICTS),
        "exclude_flags": list(EXCLUDE_FLAGS),
    },
    "ladders": {
        "enabled": False,
        "ladders": {"table": "ladders", "where": "",
                    "columns": {"id": "id", "slug": "slug", "kind": "kind"}},
        "tiers": {"table": "ladder_tiers",
                  "columns": {"id": "id", "ladder_id": "ladder_id", "budget": "budget",
                              "params": "params", "order": "budget"},
                  "param_columns": {}},
        "slots": {"table": "ladder_slots",
                  "columns": {"tier_id": "tier_id", "role": "role", "rank": "rank",
                              "product_id": "product_id", "reason": "reason",
                              "eligibility": "eligibility", "updated_at": None}},
        "budget": {"enforce": True},
        "plans": {},
    },
    "lists": {
        "enabled": False,
        "lists": {"table": "pick_lists", "where": "",
                  "columns": {"id": "id", "slug": "slug", "kind": "kind", "params": "params"}},
        "items": {"table": "pick_list_items",
                  "columns": {"list_id": "list_id", "product_id": "product_id", "rank": "rank",
                              "reason": "reason", "updated_at": None}},
        "kinds": {},
    },
    "substitutes": {
        "enabled": False,
        "subjects": {"relation": "", "where": "",
                     "columns": {"product_id": "product_id", "asin": "asin", "model_id": "model_id",
                                 "kind": "kind", "unbuyable": None},
                     "attributes": {}},
        "output": {"table": "product_substitutes",
                   "columns": {"subject_id": "product_id", "substitute_id": "substitute_product_id",
                               "rank": "rank", "reason": "reason", "updated_at": None}},
        "rules": {},
        "max_per_subject": 3,
        "same_model": "prefer",       # prefer | allow | exclude
        "one_per_model": True,
    },
    "publish": {
        "enabled": True,
        "key": "agents/{agent_id}/state/pick-asins.json",
        "include": ["ladders", "lists"],
        "keep_dropped_runs": 1,
    },
}

REQUIRED_COLUMNS = {
    "eligibility": ("product_id", "asin", "kind", "price"),
    # config: the stored configurations and flags decide whether ONE unit works
    "compat": ("a_model_id", "b_model_id", "relation", "verdict", "config"),
    "ladders": ("id", "kind"),
    "tiers": ("id", "ladder_id"),
    "slots": ("tier_id", "role", "rank", "product_id"),
    "lists": ("id", "kind"),
    "items": ("list_id", "product_id", "rank"),
    "subjects": ("product_id", "kind"),
    "output": ("subject_id", "substitute_id", "rank"),
}

_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)?$")
_COLUMN_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
_NAMED_PARAM_RE = re.compile(r"%\([A-Za-z_][A-Za-z0-9_]*\)s")
_ATTR_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
_ENV_RE = re.compile(ENV_PATTERN)
_DSN_ENV_RULE = ("must be the NAME of an environment variable (A-Z, 0-9, _), never a "
                 "connection string")


class SettingsError(ValueError):
    def __init__(self, problems: Sequence[str], source: str = ""):
        self.problems = list(problems)
        self.source = source
        where = f"{source}: " if source else ""
        super().__init__(where + "; ".join(self.problems))


def quote_ident(name: str) -> str:
    if not _IDENT_RE.match(name or ""):
        raise SettingsError([f"invalid SQL identifier {name!r}"])
    return ".".join(f'"{p}"' for p in name.split("."))


def bare_percent(sql: str) -> bool:
    stripped = _NAMED_PARAM_RE.sub("", (sql or "").replace("%%", ""))
    return "%" in stripped


def merge(base: Mapping[str, Any], over: Optional[Mapping[str, Any]]) -> dict:
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
    except Exception as exc:  # noqa: BLE001
        raise SettingsError([f"cannot parse config: {type(exc).__name__}: {exc}"[:300]], str(p)) from None
    block = _dig(doc, key)
    if not isinstance(block, Mapping):
        raise SettingsError([f"no '{key}' mapping in the file"], str(p))
    return dict(block), f"{p}#{key}"


def schema_problems(block: Mapping[str, Any]) -> list[str]:
    try:
        import jsonschema
    except ImportError:  # pragma: no cover
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


# ---------------------------------------------------------------------------
# Semantic validation
# ---------------------------------------------------------------------------

def _ident(where: str, name: Any, column: bool = False) -> list[str]:
    rx = _COLUMN_RE if column else _IDENT_RE
    if not isinstance(name, str) or not rx.match(name):
        return [f"{where}: invalid {'column' if column else 'table'} name {name!r} "
                "(lower-case SQL identifier)"]
    return []


def _columns(where: str, cols: Any, required: Sequence[str]) -> list[str]:
    out = []
    if not isinstance(cols, Mapping):
        return [f"{where}.columns: must be a mapping"]
    for logical, phys in cols.items():
        if phys is None:
            if logical in required:
                out.append(f"{where}.columns.{logical}: required by the table contract")
            continue
        out += _ident(f"{where}.columns.{logical}", phys, column=True)
    for logical in required:
        if logical not in cols:
            out.append(f"{where}.columns.{logical}: required by the table contract")
    return out


def _fragment(where: str, sql: Any) -> list[str]:
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


def _reason_problems(where: str, template: Any, hidden: set[str]) -> list[str]:
    """A stored reason may not name a value from the Amazon API (or another
    never-stored attribute): it outlives the 24h display window."""
    from pick_builder_engine import hidden_name, reason_names
    if not template:
        return []
    try:
        names = reason_names(template)
    except ValueError as e:
        return [f"{where}: cannot parse the template: {e}"]
    bad = sorted({n for n in names if hidden_name(n, hidden)})
    if bad:
        return [f"{where}: names {bad}, which come from the Amazon API (or eligibility.never_store) "
                "and may never be stored; show our own attributes instead"]
    return []


def _verdict_problems(where: str, verdicts: Sequence[Any]) -> list[str]:
    """A slot holds ONE unit: only verdicts that can describe one unit."""
    out = []
    bad = sorted(str(v) for v in verdicts if v not in cg.FIT_VERDICTS)
    if bad:
        out.append(f"{where}: {bad} are not fit verdicts ({sorted(cg.FIT_VERDICTS)})")
    multi = sorted(str(v) for v in verdicts if v in (cg.FITS_IN_SERIES, cg.FITS_IN_PARALLEL))
    if multi:
        out.append(f"{where}: {multi} need several units in series / parallel and a slot is one "
                   f"unit, so they can never qualify (use {FIT_VERDICTS})")
    return out


def selector_problems(where: str, spec: Any, *, attrs: set[str], subject: bool = False,
                      ladder: bool = False, hidden: Optional[set[str]] = None) -> list[str]:
    """Static checks of one selector (or substitute rule)."""
    from pick_builder_engine import Expr, ExprError, template_names
    out: list[str] = []
    if not isinstance(spec, Mapping):
        return [f"{where}: must be a mapping"]
    hidden = set(hidden or ()) | set(AMAZON_FIELDS)
    known = attrs | {"price", "list_price", "kind", "asin", "model_id", "product_id",
                     "category_confidence", "currency"}
    key = "constraints" if subject else "filters"
    for i, f in enumerate(spec.get(key) or []):
        w = f"{where}.{key}[{i}]"
        op = f.get("op")
        ops = FILTER_OPS + (SUBJECT_OPS if subject else ())
        if op not in ops:
            out.append(f"{w}.op: {op!r} is not one of {list(ops)}")
        if f.get("attr") not in known:
            out.append(f"{w}.attr: {f.get('attr')!r} is not an eligibility column or attribute")
        if "expr" in f:
            try:
                names = Expr(str(f["expr"])).names
                bad = sorted(names - known)
                if bad:
                    out.append(f"{w}.expr: unknown attribute(s) {bad}")
            except ExprError as e:
                out.append(f"{w}.expr: {e}")
        if op in ("eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in") and "value" not in f and "expr" not in f:
            out.append(f"{w}: needs 'value' or 'expr'")
        if op == "within_pct" and not isinstance(f.get("value"), (int, float)):
            out.append(f"{w}.value: within_pct needs a number (percent)")
        for ref in template_names(f.get("value")):
            ns = ref.split(".", 1)[0]
            if ns not in ("tier", "ladder", "list", "slot"):
                out.append(f"{w}.value: unknown template namespace {ns!r}")
    for i, s in enumerate(spec.get("sort") or []):
        w = f"{where}.sort[{i}]"
        try:
            bad = sorted(Expr(str(s.get("key", ""))).names - known)
            if bad:
                out.append(f"{w}.key: unknown attribute(s) {bad}")
        except ExprError as e:
            out.append(f"{w}.key: {e}")
        if s.get("dir", "asc") not in ("asc", "desc"):
            out.append(f"{w}.dir: asc or desc")
    for i, mp in enumerate(spec.get("max_per") or []):
        if mp.get("attr") not in known:
            out.append(f"{where}.max_per[{i}].attr: unknown attribute {mp.get('attr')!r}")
    out += _reason_problems(f"{where}.reason", spec.get("reason"), hidden)
    if not ladder:
        for k in ("compat", "via_of", "max_budget_share", "budget"):
            if k in spec and not subject:
                out.append(f"{where}.{k}: only ladder slots can depend on other slots or a budget")
    elif spec.get("budget", "kit") not in BUDGET_MODES:
        out.append(f"{where}.budget: kit (spends the tier's remaining budget) or own (an alternative "
                   "judged against the tier budget alone)")
    return out


def semantic_problems(merged: Mapping[str, Any]) -> list[str]:
    out: list[str] = []
    el = merged["eligibility"]
    if not el.get("relation"):
        out.append("eligibility.relation: name the eligibility view (every pick comes from it)")
    else:
        out += _ident("eligibility.relation", el["relation"])
    out += _columns("eligibility", el.get("columns"), REQUIRED_COLUMNS["eligibility"])
    out += _fragment("eligibility.where", el.get("where"))
    attrs = set()
    for a, col in (el.get("attributes") or {}).items():
        if not _ATTR_RE.match(str(a)):
            out.append(f"eligibility.attributes: invalid attribute name {a!r}")
        out += _ident(f"eligibility.attributes.{a}", col, column=True)
        attrs.add(str(a))
    hidden = set(AMAZON_FIELDS)
    for a in el.get("never_store") or []:
        if a not in attrs:
            out.append(f"eligibility.never_store: {a!r} is not an eligibility attribute")
        hidden.add(str(a))
    try:
        re.compile(str((el.get("integrity") or {}).get("asin_regex") or ".*"))
    except re.error as e:
        out.append(f"eligibility.integrity.asin_regex: {e}")

    cp = merged["compat"]
    uses_compat = False
    lad = merged["ladders"]
    if lad.get("enabled"):
        for blk in ("ladders", "tiers", "slots"):
            b = lad.get(blk) or {}
            out += _ident(f"ladders.{blk}.table", b.get("table"))
            out += _columns(f"ladders.{blk}", b.get("columns"), REQUIRED_COLUMNS[blk])
        out += _fragment("ladders.ladders.where", (lad.get("ladders") or {}).get("where"))
        for p, col in ((lad.get("tiers") or {}).get("param_columns") or {}).items():
            out += _ident(f"ladders.tiers.param_columns.{p}", col, column=True)
        if not lad.get("plans"):
            out.append("ladders.plans: define at least one plan (ladders are enabled)")
        for kind, plan in (lad.get("plans") or {}).items():
            roles: list[str] = []
            if not isinstance(plan, list) or not plan:
                out.append(f"ladders.plans.{kind}: must be a non-empty list of slots")
                continue
            for i, slot in enumerate(plan):
                w = f"ladders.plans.{kind}[{i}]"
                role = slot.get("role") if isinstance(slot, Mapping) else None
                if not role:
                    out.append(f"{w}.role: required")
                elif role in roles:
                    out.append(f"{w}.role: {role!r} is used twice in this plan")
                out += selector_problems(w, slot, attrs=attrs, ladder=True, hidden=hidden)
                for k in ("compat", "via_of"):
                    if isinstance(slot, Mapping) and slot.get(k):
                        uses_compat = True
                        dep = slot[k]
                        refs = [dep.get("with")] if k == "compat" else [dep.get("a_role"), dep.get("b_role")]
                        for r in refs:
                            if r not in roles:
                                out.append(f"{w}.{k}: refers to role {r!r}, which is not an earlier slot")
                        if k == "compat":
                            out += _verdict_problems(f"{w}.compat.verdicts", dep.get("verdicts") or [])
                if role:
                    roles.append(role)
    lst = merged["lists"]
    if lst.get("enabled"):
        for blk in ("lists", "items"):
            b = lst.get(blk) or {}
            out += _ident(f"lists.{blk}.table", b.get("table"))
            out += _columns(f"lists.{blk}", b.get("columns"), REQUIRED_COLUMNS[blk])
        out += _fragment("lists.lists.where", (lst.get("lists") or {}).get("where"))
        if not lst.get("kinds"):
            out.append("lists.kinds: define a selector per list kind (lists are enabled)")
        for kind, spec in (lst.get("kinds") or {}).items():
            out += selector_problems(f"lists.kinds.{kind}", spec, attrs=attrs, hidden=hidden)
    sub = merged["substitutes"]
    if sub.get("enabled"):
        sj = sub.get("subjects") or {}
        if not sj.get("relation"):
            out.append("substitutes.subjects.relation: required (substitutes are enabled)")
        else:
            out += _ident("substitutes.subjects.relation", sj["relation"])
        out += _columns("substitutes.subjects", sj.get("columns"), REQUIRED_COLUMNS["subjects"])
        out += _fragment("substitutes.subjects.where", sj.get("where"))
        for a, col in (sj.get("attributes") or {}).items():
            out += _ident(f"substitutes.subjects.attributes.{a}", col, column=True)
        op = sub.get("output") or {}
        out += _ident("substitutes.output.table", op.get("table"))
        out += _columns("substitutes.output", op.get("columns"), REQUIRED_COLUMNS["output"])
        if sub.get("same_model") not in ("prefer", "allow", "exclude"):
            out.append("substitutes.same_model: prefer, allow or exclude")
        if not sub.get("rules"):
            out.append("substitutes.rules: define a rule per subject kind (substitutes are enabled)")
        subject_attrs = set(sj.get("attributes") or {}) | set(SUBJECT_BASE)
        for kind, rule in (sub.get("rules") or {}).items():
            out += selector_problems(f"substitutes.rules.{kind}", rule, attrs=attrs, subject=True,
                                     hidden=hidden)
            for i, c in enumerate(rule.get("constraints") or []):
                if c.get("op") in SUBJECT_OPS and c.get("attr") not in subject_attrs:
                    out.append(f"substitutes.rules.{kind}.constraints[{i}]: the subject has no "
                               f"attribute {c.get('attr')!r} (map it under substitutes.subjects.attributes)")
    if uses_compat:
        if not cp.get("table"):
            out.append("compat.table: required (a ladder slot uses compat / via_of)")
        else:
            out += _ident("compat.table", cp["table"])
            out += _columns("compat", cp.get("columns"), REQUIRED_COLUMNS["compat"])
        out += _verdict_problems("compat.fit_verdicts", cp.get("fit_verdicts") or [])
    pub = merged["publish"]
    if pub.get("enabled"):
        bad = sorted(set(pub.get("include") or []) - {"ladders", "lists", "substitutes"})
        if bad:
            out.append(f"publish.include: unknown parts {bad}")
        if not str(pub.get("key") or "").strip():
            out.append("publish.key: required (the price refresh's storage_list key)")
    return out


def integrity_warnings(merged: Mapping[str, Any]) -> list[str]:
    """The floor can only check what the site maps: say which checks of the
    CLAUDE.md pricing-integrity rule are left to the view alone."""
    el = merged["eligibility"]
    cols = el.get("columns") or {}
    integ = el.get("integrity") or {}
    out = []
    if integ.get("currencies") and not cols.get("currency"):
        out.append("eligibility.columns.currency is not mapped: the integrity floor cannot reject "
                   "foreign-marketplace prices (the view alone must)")
    if integ.get("list_price_not_below_price", True) and not cols.get("list_price"):
        out.append("eligibility.columns.list_price is not mapped: the integrity floor cannot reject a "
                   "list price below the price (the view alone must)")
    if integ.get("min_category_confidence") is not None and not cols.get("category_confidence"):
        out.append("eligibility.columns.category_confidence is not mapped: the integrity floor cannot "
                   "apply the category-confidence minimum (the view alone must)")
    return out


@dataclass(frozen=True)
class Settings:
    agent_id: str
    site: str
    source: str
    dsn_env: str
    block: Mapping[str, Any]
    warnings: tuple[str, ...] = ()

    @property
    def database(self) -> Mapping[str, Any]:
        return self.block["database"]

    @property
    def lock_key(self) -> str:
        return str(self.database.get("lock_key") or f"pick-builder:{self.agent_id}")

    @property
    def publish_key(self) -> str:
        return str(self.block["publish"]["key"]).replace("{agent_id}", self.agent_id)

    @property
    def hidden(self) -> frozenset[str]:
        """Values no stored reason may show."""
        return frozenset(AMAZON_FIELDS) | frozenset(self.block["eligibility"].get("never_store") or ())

    def describe(self) -> dict:
        return {"agent_id": self.agent_id, "site": self.site, "source": self.source,
                "dsn_env": self.dsn_env, "publish_key": self.publish_key,
                "warnings": list(self.warnings), "block": copy.deepcopy(dict(self.block))}


def _registry(storage):
    from framework.core import site_registry
    try:
        return site_registry.load_registry(storage)
    except Exception:  # noqa: BLE001
        return None


def build_settings(block: Mapping[str, Any], *, agent_id: str, storage=None, source: str = "") -> Settings:
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
    if not any(merged[k].get("enabled") for k in ("ladders", "lists", "substitutes")):
        problems.append("nothing to build: enable ladders, lists and/or substitutes")
    if problems:
        raise SettingsError(problems, source)
    pub = merged["publish"]
    if pub.get("enabled") and not merged["ladders"].get("enabled") and "ladders" in (pub.get("include") or []):
        warnings.append("publish.include names ladders but ladders are disabled")
    warnings += integrity_warnings(merged)
    return Settings(agent_id=agent_id, site=site, source=source, dsn_env=dsn_env, block=merged,
                    warnings=tuple(warnings))


def load_settings(spec: Optional[str] = None, *, agent_id: str, storage=None,
                  environ: Optional[Mapping[str, str]] = None) -> Settings:
    import os
    env = os.environ if environ is None else environ
    spec = spec or env.get(ENV_CONFIG, "")
    if not spec:
        raise SettingsError([f"${ENV_CONFIG} is not set: point it at the site's site.yaml "
                             "(path[#dotted.key], default key pick_builder)"])
    block, source = read_block(spec)
    return build_settings(block, agent_id=agent_id, storage=storage, source=source)
