"""Config model for the Amazon catalog-discovery engine.

WHERE IT COMES FROM
-------------------
A site's discovery config is a YAML (or JSON) document: the whole file named
by ``CATALOG_DISCOVERY_CONFIG`` (or ``--config``), or the ``catalog_discovery``
block of a larger site.yaml. Relative paths inside it (seed files, the golden
file) resolve against the directory of that file.

VALIDATION
----------
Two passes, each reporting every problem at once:

1. the JSON Schema ``catalog-discovery-config.schema.json`` next to this
   package: shape, types, enums and typos (unknown keys are errors, so a
   misspelt knob fails at load instead of being silently ignored);
2. :func:`build_config`: what a schema cannot express. Regexes compile, ids
   are unique, lane / measure references resolve, every seed scopes its
   search somehow, price bands are ordered, the item-page ceiling holds, and
   the SQL templates use only the parameters the engine binds.

The second pass is the contract that keeps Amazon content out of the
catalog: ``insert_sql`` may only name :data:`INSERT_PARAMS`, none of which
carries Amazon text, prices or images.

Two API facts are hard limits a config can tighten but never loosen: no
itemPage above :data:`CREATORS_MAX_ITEM_PAGE`, and no pricing floor below
:data:`MIN_PRICE_FLOOR` (the pricing-integrity rule: no $0 or sub-$1 offers).
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

CONFIG_ENV = "CATALOG_DISCOVERY_CONFIG"
CONFIG_BLOCK = "catalog_discovery"
SCHEMA_VERSION = "1"
SCHEMA_PATH = Path(__file__).resolve().parent.parent / "catalog-discovery-config.schema.json"

#: Creators API facts the shared loop does not encode yet (W3). itemPage 11+
#: is refused, and totalResultCount is clipped at 306 (not the 1,000 the
#: amazon_creators docstring mentions); below the cap it is an exact count.
CREATORS_MAX_ITEM_PAGE = 10
CREATORS_RESULT_COUNT_CAP = 306
#: searchItems page size ceiling (amazon_creators.MAX_SEARCH_ITEM_COUNT).
ITEMS_PER_PAGE = 10
#: The pricing-integrity floor (CLAUDE.md "Pricing integrity";
#: amazon_discovery.DEFAULT_MIN_PRICE). A config may raise it, never lower it.
MIN_PRICE_FLOOR = 1.0

DEFAULT_ASIN_REGEX = r"^B[0-9A-Z]{9}$"
DEFAULT_PARTNER_TAG_ENV = "AMAZON_CREATORS_PARTNER_TAG"
REJECT_LABEL = "reject"

MATCH_MODES = ("title", "node", "title_and_node", "title_or_node")
PICK_MODES = ("first", "max", "min")
PRICE_UNITS = ("cents", "dollars")

#: The only names insert_sql may use. Every one is our own value: the ASIN
#: (Amazon allows storing it indefinitely) and labels the engine derives.
INSERT_PARAMS = ("asin", "lane", "lane_kind", "lane_category", "confidence",
                 "via", "seed", "run_ts", "site_id")
KNOWN_PARAMS = ("asins",)

_PARAM_RE = re.compile(r"%\((\w+)\)s")
# A '%' that is not '%%' and does not start a named parameter.
_BAD_PERCENT_RE = re.compile(r"%(?!%|\(\w+\)s)")
_SELECT_RE = re.compile(r"^\s*(?:--[^\n]*\n\s*)*(select|with)\b", re.IGNORECASE)
_SLUG_RE = re.compile(r"[^a-z0-9]+")


class ConfigError(ValueError):
    """The config is unusable. ``problems`` lists every reason found."""

    def __init__(self, problems: Sequence[str], *, source: str = "") -> None:
        self.problems = list(problems) or ["unknown problem"]
        self.source = source
        where = f" ({source})" if source else ""
        super().__init__(f"invalid catalog-discovery config{where}: " + "; ".join(self.problems))


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PatternRule:
    """A compiled, case-insensitive title regex with a stable id for metrics."""

    id: str
    regex: re.Pattern

    def search(self, text: str) -> bool:
        return self.regex.search(text) is not None


@dataclass(frozen=True)
class MeasurePattern:
    """One way a measure is written: group 1 of ``regex`` times ``scale``.

    ``dot_thousands`` reads '1.024' (one digit, a dot, three digits) as 1024.
    It belongs to a pattern, not to the measure: '1.024Wh' means 1024 Wh, but
    '3.072kWh' means 3.072 kWh (3072 Wh), so a scaled pattern must not use it.
    """

    regex: re.Pattern
    scale: float = 1.0
    dot_thousands: bool = False


@dataclass(frozen=True)
class MeasureSpec:
    """How to read one number (e.g. a capacity) out of a listing title."""

    name: str
    patterns: tuple[MeasurePattern, ...]
    strip: tuple[re.Pattern, ...] = ()
    pick: str = "first"


@dataclass(frozen=True)
class MeasureRule:
    """A lane's constraint on a measure."""

    measure: str
    min: Optional[float] = None
    max: Optional[float] = None
    missing_min_price: Optional[float] = None
    required: bool = False


@dataclass(frozen=True)
class PricePartition:
    """A [lo, hi) dollar band searched with minPrice/maxPrice filters."""

    lo: Optional[float]
    hi: Optional[float]

    @property
    def label(self) -> str:
        def fmt(v: float) -> str:
            return str(int(v)) if float(v).is_integer() else f"{v:g}"
        if self.lo is None and self.hi is None:
            return "any"
        if self.hi is None:
            return f"{fmt(self.lo)}+"
        if self.lo is None:
            return f"-{fmt(self.hi)}"
        return f"{fmt(self.lo)}-{fmt(self.hi)}"

    def contains(self, price: float, *, tolerance: float = 0.0) -> bool:
        lo = None if self.lo is None else self.lo * (1.0 - tolerance)
        hi = None if self.hi is None else self.hi * (1.0 + tolerance)
        return (lo is None or price >= lo) and (hi is None or price < hi)

    def filters(self, unit: str) -> dict[str, int]:
        """searchItems filter kwargs (amazon_creators turns min_price into minPrice)."""
        def conv(v: float) -> int:
            return int(round(v * 100)) if unit == "cents" else int(round(v))
        out: dict[str, int] = {}
        if self.lo:
            out["min_price"] = conv(self.lo)
        if self.hi is not None:
            out["max_price"] = conv(self.hi)
        return out


@dataclass(frozen=True)
class Seed:
    """One configured search, before brand and price-band expansion."""

    id: str
    query: str
    brands: tuple[str, ...]
    browse_node: Optional[str]
    sort_by: Optional[str]
    search_index: str
    min_price: Optional[float]
    partitions: tuple[PricePartition, ...]
    max_pages: int


@dataclass(frozen=True)
class Lane:
    id: str
    kind: str
    category: str
    phase: Optional[str]
    enabled: bool
    active: bool
    browse_nodes: frozenset
    search_browse_node: Optional[str]
    match: str
    include_all: tuple[PatternRule, ...]
    include_any: tuple[PatternRule, ...]
    exclude: tuple[PatternRule, ...]
    context_required: tuple[PatternRule, ...]
    min_price: Optional[float]
    measure_rules: tuple[MeasureRule, ...]
    price_partitions: tuple[PricePartition, ...]
    search_index: str
    max_pages: int
    seeds: tuple[Seed, ...]

    @property
    def has_title_rules(self) -> bool:
        return bool(self.include_all or self.include_any)


@dataclass(frozen=True)
class Query:
    """One searchable unit: a seed, optionally narrowed to a brand and a price band.

    ``key`` is stable across runs while the config ids stay the same; the
    rotation state is keyed on it.
    """

    key: str
    lane: str
    seed_id: str
    text: str
    brand: Optional[str]
    browse_node: Optional[str]
    sort_by: Optional[str]
    search_index: str
    min_price: Optional[float]
    partition: Optional[PricePartition]
    max_pages: int

    def filters(self, price_unit: str) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.browse_node:
            out["browse_node_id"] = self.browse_node
        if self.brand:
            out["brand"] = self.brand
        if self.sort_by:
            out["sort_by"] = self.sort_by
        if self.partition is not None:
            out.update(self.partition.filters(price_unit))
        return out


@dataclass(frozen=True)
class CreatorsSettings:
    partner_tag: Optional[str] = None
    partner_tag_env: str = DEFAULT_PARTNER_TAG_ENV
    search_index: str = "All"
    max_item_page: int = CREATORS_MAX_ITEM_PAGE
    result_count_cap: int = CREATORS_RESULT_COUNT_CAP
    price_filter_unit: str = "cents"


@dataclass(frozen=True)
class Limits:
    max_calls_per_run: int = 40
    max_new_per_run: int = 300
    max_pages_per_query: int = CREATORS_MAX_ITEM_PAGE
    max_seed_asins_per_run: int = 5000
    #: Low-yield stop (the sizing run's ``stop_if_new_inlane_lt``): a page at
    #: or past ``low_yield_from_page`` that adds fewer new in-lane ASINs than
    #: this ends its query for the cycle. 0 = off.
    stop_if_new_in_lane_below: int = 0
    low_yield_from_page: int = 2


@dataclass(frozen=True)
class BudgetSettings:
    protect_consumers: tuple[str, ...] = ()
    keep_unused_fraction_of_day: float = 0.0


@dataclass(frozen=True)
class PricingSettings:
    min_price: float = 1.0
    currencies: tuple[str, ...] = ("USD",)
    require_new_condition: bool = True


@dataclass(frozen=True)
class CatalogSettings:
    known_sql: str
    insert_sql: str
    dsn_env: Optional[str] = None
    statement_timeout_ms: int = 30000
    connect_timeout_s: int = 15
    asin_regex: str = DEFAULT_ASIN_REGEX
    site_id: Optional[str] = None

    @property
    def asin_re(self) -> re.Pattern:
        return re.compile(self.asin_regex)

    @property
    def insert_params(self) -> frozenset:
        return frozenset(_PARAM_RE.findall(self.insert_sql))


@dataclass(frozen=True)
class SeedFileSource:
    """A JSON file of seed ASINs (see seeds.py for the shapes and the rules).

    Trust: a record that carries a lane label but no title cannot be checked
    against the lane rules, so its label is used only when the lane is in
    ``lanes`` or ``trust_labels`` is set. ``title_field`` names a transient
    title to screen records with (never stored).
    """

    name: str
    path: Path
    json_path: Optional[str] = None
    asin_field: str = "asin"
    lane_field: str = "lane"
    phase_field: str = "phase"
    title_field: Optional[str] = None
    where: Mapping[str, Any] = field(default_factory=dict)
    where_not: Mapping[str, Any] = field(default_factory=dict)
    lanes: Optional[frozenset] = None
    lane_map: Mapping[str, str] = field(default_factory=dict)
    trust_labels: bool = False
    default_lane: Optional[str] = None
    confidence: Optional[float] = None
    enabled: bool = True


@dataclass(frozen=True)
class SeedQuerySource:
    """A read-only SELECT on another database (see seeds.py).

    ``sql`` is one SELECT statement without a trailing ';'. It runs wrapped
    as ``SELECT * FROM (<sql>) ... WHERE asin > %(after)s ORDER BY asin
    LIMIT %(limit)s`` (keyset pages of ``max_rows``), so a literal '%' in it
    is escaped before execution. ``lane_map`` / ``trust_labels`` /
    ``confidence`` as for files.
    """

    name: str
    sql: str
    site: Optional[str] = None
    dsn_env: Optional[str] = None
    lanes: Optional[frozenset] = None
    lane_map: Mapping[str, str] = field(default_factory=dict)
    trust_labels: bool = False
    default_lane: Optional[str] = None
    confidence: Optional[float] = None
    classify_titles: bool = False
    refresh_hours: float = 168.0
    statement_timeout_ms: int = 30000
    max_rows: int = 5000
    enabled: bool = True

    def paged_sql(self) -> str:
        """The executed statement: the source's SELECT as a subquery, one
        keyset page at a time. Single statement by construction (validation
        rejects ';'), and run in a READ ONLY session."""
        inner = self.sql.replace("%", "%%")
        return ("SELECT * FROM (\n" + inner + "\n) AS seed_rows"
                " WHERE asin > %(after)s ORDER BY asin LIMIT %(limit)s")


@dataclass(frozen=True)
class QASettings:
    golden: Mapping[str, str] = field(default_factory=dict)
    window: int = 500
    min_observations: int = 10


@dataclass(frozen=True)
class ConfidenceSettings:
    node_and_title: float = 0.9
    title_only: float = 0.7
    node_only: float = 0.6
    seed: float = 0.8


@dataclass(frozen=True)
class RotationSettings:
    cycle_cooldown_hours: float = 0.0


@dataclass(frozen=True)
class DiscoveryConfig:
    consumer: str
    catalog: CatalogSettings
    lanes: tuple[Lane, ...]
    site: Optional[str] = None
    enabled: bool = True
    creators: CreatorsSettings = CreatorsSettings()
    limits: Limits = Limits()
    budget: BudgetSettings = BudgetSettings()
    pricing: PricingSettings = PricingSettings()
    active_phases: Optional[frozenset] = None
    reject_rules: tuple[PatternRule, ...] = ()
    measures: Mapping[str, MeasureSpec] = field(default_factory=dict)
    rotation: RotationSettings = RotationSettings()
    seed_files: tuple[SeedFileSource, ...] = ()
    seed_queries: tuple[SeedQuerySource, ...] = ()
    qa: QASettings = QASettings()
    confidence: ConfidenceSettings = ConfidenceSettings()
    source: str = ""
    fingerprint: str = ""

    # -- lookups -------------------------------------------------------------

    def lane(self, lane_id: str) -> Optional[Lane]:
        for lane in self.lanes:
            if lane.id == lane_id:
                return lane
        return None

    @property
    def lane_ids(self) -> tuple[str, ...]:
        return tuple(lane.id for lane in self.lanes)

    @property
    def active_lanes(self) -> tuple[Lane, ...]:
        return tuple(lane for lane in self.lanes if lane.active)

    @property
    def max_pages(self) -> int:
        """Deepest itemPage any query may request: the config can lower the
        API's ceiling, never raise it."""
        return max(1, min(self.limits.max_pages_per_query, self.creators.max_item_page,
                          CREATORS_MAX_ITEM_PAGE))

    def queries(self) -> list[Query]:
        """Every searchable unit of the ACTIVE lanes, in config order:
        lane, then seed, then brand, then price band."""
        out: list[Query] = []
        for lane in self.active_lanes:
            for seed in lane.seeds:
                brands: Sequence[Optional[str]] = seed.brands or (None,)
                bands: Sequence[Optional[PricePartition]] = seed.partitions or (None,)
                for brand in brands:
                    for band in bands:
                        key = f"{lane.id}/{seed.id}"
                        if brand:
                            key += f"/brand={_slug(brand)}"
                        if band is not None:
                            key += f"/price={band.label}"
                        out.append(Query(
                            key=key, lane=lane.id, seed_id=seed.id, text=seed.query,
                            brand=brand, browse_node=seed.browse_node, sort_by=seed.sort_by,
                            search_index=seed.search_index, min_price=seed.min_price,
                            partition=band, max_pages=min(seed.max_pages, self.max_pages)))
        return out

    def describe(self) -> dict[str, Any]:
        """A small, value-free summary for logs and ``--validate``."""
        queries = self.queries()
        return {
            "source": self.source,
            "fingerprint": self.fingerprint,
            "site": self.site,
            "enabled": self.enabled,
            "consumer": self.consumer,
            "lanes": [{"id": lane.id, "phase": lane.phase, "active": lane.active,
                       "match": lane.match, "seeds": len(lane.seeds),
                       "browse_nodes": sorted(lane.browse_nodes),
                       "price_partitions": [p.label for p in lane.price_partitions]}
                      for lane in self.lanes],
            "active_phases": sorted(self.active_phases) if self.active_phases is not None else None,
            "queries": len(queries),
            "max_slots_per_cycle": sum(q.max_pages for q in queries),
            "limits": {"max_calls_per_run": self.limits.max_calls_per_run,
                       "max_new_per_run": self.limits.max_new_per_run,
                       "max_pages_per_query": self.max_pages,
                       "max_seed_asins_per_run": self.limits.max_seed_asins_per_run,
                       "stop_if_new_in_lane_below": self.limits.stop_if_new_in_lane_below,
                       "low_yield_from_page": self.limits.low_yield_from_page},
            "reject_rules": len(self.reject_rules),
            "seed_sources": ([s.name for s in self.seed_files]
                             + [s.name for s in self.seed_queries]),
            "golden_labels": len(self.qa.golden),
        }


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def _slug(text: str) -> str:
    return _SLUG_RE.sub("-", (text or "").lower()).strip("-")[:60]


def _rule_id(pattern: str) -> str:
    """A readable, stable id for an unnamed pattern: the pattern itself, or
    its head plus a short hash when long (two long patterns may share a head)."""
    text = " ".join(pattern.split())
    if len(text) <= 48:
        return text
    return text[:40] + "…" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:6]


def config_path(path: Optional[os.PathLike | str] = None,
                environ: Optional[Mapping[str, str]] = None) -> Path:
    env = os.environ if environ is None else environ
    raw = str(path) if path else env.get(CONFIG_ENV, "")
    if not raw.strip():
        raise ConfigError([f"no config file: pass --config or set {CONFIG_ENV}"])
    return Path(raw).expanduser()


def read_document(path: Path) -> Any:
    """Parse the file (JSON by extension, else YAML) and pick the block."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError([f"cannot read {path}: {exc.strerror or exc}"], source=str(path)) from None
    try:
        if path.suffix.lower() == ".json":
            doc = json.loads(text)
        else:
            import yaml  # framework dependency (pyyaml)
            doc = yaml.safe_load(text)
    except Exception as exc:  # noqa: BLE001 -- any parse error is a config error
        raise ConfigError([f"cannot parse {path.name}: {exc}"], source=str(path)) from None
    if isinstance(doc, dict) and CONFIG_BLOCK in doc:
        doc = doc[CONFIG_BLOCK]
    return doc


def schema_problems(doc: Any, schema_path: Path = SCHEMA_PATH) -> list[str]:
    """JSON-Schema problems, '<path>: <message>' each, in document order."""
    try:
        import jsonschema  # framework dependency
    except ImportError:  # pragma: no cover - jsonschema ships with the framework
        return []
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = jsonschema.Draft7Validator(schema)
    out = []
    for err in sorted(validator.iter_errors(doc), key=lambda e: [str(p) for p in e.absolute_path]):
        where = "/".join(str(p) for p in err.absolute_path) or "(root)"
        message = err.message
        if err.validator == "additionalProperties" and isinstance(err.instance, Mapping):
            # jsonschema's wording ("does not match any of the regexes") hides
            # the point: a key the engine does not know, usually a typo.
            allowed = set((err.schema or {}).get("properties") or {})
            extras = sorted(k for k in err.instance if k not in allowed and not str(k).startswith("_"))
            if extras:
                message = "unknown key(s): " + ", ".join(map(str, extras))
        out.append(f"{where}: {message}")
    return out


def load_config(path: Optional[os.PathLike | str] = None, *,
                environ: Optional[Mapping[str, str]] = None) -> DiscoveryConfig:
    """Read, schema-validate and build the config. Raises ConfigError."""
    p = config_path(path, environ)
    doc = read_document(p)
    return build_config(doc, base_dir=p.resolve().parent, source=str(p))


def build_config(doc: Any, *, base_dir: Optional[Path] = None, source: str = "",
                 check_schema: bool = True) -> DiscoveryConfig:
    """Validate ``doc`` and return the typed config. Raises ConfigError."""
    if not isinstance(doc, dict):
        raise ConfigError(["the config must be a mapping"], source=source)
    if check_schema:
        problems = schema_problems(doc)
        if problems:
            raise ConfigError(problems, source=source)
    builder = _Builder(base_dir or Path.cwd())
    cfg = builder.build(doc, source)
    if builder.problems:
        raise ConfigError(builder.problems, source=source)
    return cfg


def fingerprint(doc: Any) -> str:
    """Short, stable hash of the config content (for state and run reports)."""
    blob = json.dumps(doc, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Semantic builder
# ---------------------------------------------------------------------------

def _num(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _node(value: Any) -> Optional[str]:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text or None


def _strip_comments(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in raw.items() if not str(k).startswith("_")}


class _Builder:
    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir
        self.problems: list[str] = []

    # -- helpers ---------------------------------------------------------------

    def fail(self, where: str, message: str) -> None:
        self.problems.append(f"{where}: {message}")

    def regex(self, pattern: Any, where: str) -> Optional[re.Pattern]:
        if not isinstance(pattern, str) or not pattern.strip():
            self.fail(where, "expected a non-empty regex string")
            return None
        try:
            return re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            self.fail(where, f"invalid regex ({exc})")
            return None

    def rule(self, raw: Any, where: str) -> Optional[PatternRule]:
        if isinstance(raw, Mapping):
            pattern = raw.get("pattern")
            rid = raw.get("id")
        else:
            pattern, rid = raw, None
        rx = self.regex(pattern, where)
        if rx is None:
            return None
        return PatternRule(id=str(rid) if rid else _rule_id(str(pattern)), regex=rx)

    def rules(self, raws: Any, where: str) -> tuple[PatternRule, ...]:
        if raws is None:
            return ()
        if isinstance(raws, (str, Mapping)):
            raws = [raws]
        out = []
        seen: set[str] = set()
        for i, raw in enumerate(raws):
            rule = self.rule(raw, f"{where}[{i}]")
            if rule is None:
                continue
            if rule.id in seen:
                self.fail(f"{where}[{i}]", f"duplicate rule id {rule.id!r}")
                continue
            seen.add(rule.id)
            out.append(rule)
        return tuple(out)

    def path(self, raw: str) -> Path:
        p = Path(os.path.expanduser(raw))
        return p if p.is_absolute() else (self.base_dir / p)

    def partitions(self, raws: Any, where: str) -> tuple[PricePartition, ...]:
        out: list[PricePartition] = []
        for i, raw in enumerate(raws or []):
            w = f"{where}[{i}]"
            if not isinstance(raw, (list, tuple)) or len(raw) != 2:
                self.fail(w, "expected [low, high]")
                continue
            lo, hi = _num(raw[0]), _num(raw[1])
            if raw[0] is not None and lo is None or raw[1] is not None and hi is None:
                self.fail(w, "bounds must be numbers or null")
                continue
            if lo is not None and hi is not None and not lo < hi:
                self.fail(w, f"low ({lo:g}) must be below high ({hi:g})")
                continue
            if lo is None and hi is None:
                self.fail(w, "a band needs at least one bound (omit price_partitions instead)")
                continue
            band = PricePartition(lo if lo else None, hi)
            if band in out:
                self.fail(w, f"duplicate band {band.label}")
                continue
            out.append(band)
        return tuple(out)

    # -- sections ----------------------------------------------------------------

    def measures(self, raw: Any) -> dict[str, MeasureSpec]:
        out: dict[str, MeasureSpec] = {}
        for name, spec in (raw or {}).items():
            w = f"measures.{name}"
            if not isinstance(spec, Mapping):
                self.fail(w, "expected a mapping")
                continue
            # The measure-level flag is only a default for UNSCALED patterns:
            # '1.024' is 1024 Wh, but '3.072' kWh is 3.072 kWh.
            unscaled_default = bool(spec.get("dot_thousands", False))
            patterns: list[MeasurePattern] = []
            for i, p in enumerate(spec.get("patterns") or []):
                pw = f"{w}.patterns[{i}]"
                if isinstance(p, Mapping):
                    regex_text, scale = p.get("regex"), _num(p.get("scale", 1.0))
                    dot = p.get("dot_thousands")
                else:
                    regex_text, scale, dot = p, 1.0, None
                rx = self.regex(regex_text, pw)
                if rx is None:
                    continue
                if rx.groups < 1:
                    self.fail(pw, "needs a capture group for the number")
                    continue
                if scale is None or scale <= 0:
                    self.fail(pw, "scale must be a positive number")
                    continue
                if dot is None:
                    dot = unscaled_default and scale == 1.0
                elif dot and scale != 1.0:
                    self.fail(pw, "dot_thousands on a scaled pattern would read '3.072' kWh "
                                  "as 3072 kWh; set it on the unscaled pattern only")
                    continue
                patterns.append(MeasurePattern(regex=rx, scale=float(scale), dot_thousands=bool(dot)))
            strip = tuple(rx for i, s in enumerate(spec.get("strip") or [])
                          if (rx := self.regex(s, f"{w}.strip[{i}]")) is not None)
            if not patterns:
                self.fail(w, "no usable patterns")
                continue
            out[str(name)] = MeasureSpec(name=str(name), patterns=tuple(patterns), strip=strip,
                                         pick=str(spec.get("pick", "first")))
        return out

    def lane(self, raw: Mapping[str, Any], i: int, *, measures: Mapping[str, MeasureSpec],
             active_phases: Optional[frozenset], creators: CreatorsSettings,
             limits: Limits) -> Optional[Lane]:
        w = f"lanes[{i}]"
        lane_id = str(raw.get("id") or "")
        if not lane_id:
            self.fail(w, "missing id")
            return None
        w = f"lanes[{i}]({lane_id})"
        include_all = self.rules(raw.get("include_all"), f"{w}.include_all")
        include_any = self.rules(raw.get("include_any"), f"{w}.include_any")
        exclude = self.rules(raw.get("exclude"), f"{w}.exclude")
        context = self.rules(raw.get("context_required"), f"{w}.context_required")
        nodes = frozenset(n for n in (_node(x) for x in raw.get("browse_nodes") or []) if n)
        has_title = bool(include_all or include_any)
        match = raw.get("match") or ("title" if has_title else "node")
        if match not in MATCH_MODES:
            self.fail(w, f"match must be one of {', '.join(MATCH_MODES)}")
        if "title" in match and not has_title:
            self.fail(w, f"match {match!r} needs include_all or include_any rules")
        if "node" in match and not nodes:
            self.fail(w, f"match {match!r} needs browse_nodes")
        if not has_title and not nodes:
            self.fail(w, "no positive rule: give include_all / include_any or browse_nodes "
                         "(a lane without one would claim every listing)")
        rules: list[MeasureRule] = []
        for name, spec in (raw.get("measures") or {}).items():
            if name not in measures:
                self.fail(f"{w}.measures.{name}", "unknown measure (define it under top-level measures)")
                continue
            spec = spec or {}
            lo, hi = _num(spec.get("min")), _num(spec.get("max"))
            if lo is not None and hi is not None and lo > hi:
                self.fail(f"{w}.measures.{name}", "min is above max")
            rules.append(MeasureRule(measure=str(name), min=lo, max=hi,
                                     missing_min_price=_num(spec.get("missing_min_price")),
                                     required=bool(spec.get("required", False))))
        phase = str(raw["phase"]) if raw.get("phase") is not None else None
        enabled = bool(raw.get("enabled", True))
        active = enabled and (active_phases is None or phase is None or phase in active_phases)
        search_node = _node(raw.get("search_browse_node"))
        search_index = str(raw.get("search_index") or creators.search_index)
        lane_pages = int(raw.get("max_pages") or limits.max_pages_per_query)
        if lane_pages > creators.max_item_page:
            self.fail(f"{w}.max_pages", f"{lane_pages} is above the API's itemPage ceiling "
                                        f"({creators.max_item_page})")
        partitions = self.partitions(raw.get("price_partitions"), f"{w}.price_partitions")
        seeds = self.seeds(raw.get("seeds") or [], w, lane_id=lane_id, search_node=search_node,
                           search_index=search_index, partitions=partitions,
                           max_pages=lane_pages, creators=creators)
        return Lane(
            id=lane_id, kind=str(raw.get("kind") or lane_id), category=str(raw.get("category") or lane_id),
            phase=phase, enabled=enabled, active=active,
            browse_nodes=nodes, search_browse_node=search_node, match=str(match),
            include_all=include_all, include_any=include_any, exclude=exclude,
            context_required=context, min_price=_num(raw.get("min_price")),
            measure_rules=tuple(rules), price_partitions=partitions, search_index=search_index,
            max_pages=lane_pages, seeds=seeds)

    def seeds(self, raws: Sequence[Any], w: str, *, lane_id: str, search_node: Optional[str],
              search_index: str, partitions: tuple[PricePartition, ...], max_pages: int,
              creators: CreatorsSettings) -> tuple[Seed, ...]:
        out: list[Seed] = []
        ids: set[str] = set()
        for j, raw in enumerate(raws):
            sw = f"{w}.seeds[{j}]"
            raw = {"query": raw} if isinstance(raw, str) else _strip_comments(raw or {})
            if raw.get("enabled", True) is False:
                continue
            query = " ".join(str(raw.get("query") or "").split())
            if raw.get("brand") and raw.get("brands"):
                self.fail(sw, "use brand or brands, not both")
            brands = tuple(" ".join(str(b).split()) for b in
                           (raw.get("brands") or ([raw["brand"]] if raw.get("brand") else [])))
            node = _node(raw["browse_node"]) if "browse_node" in raw else search_node
            if not query and not brands and not node:
                self.fail(sw, "needs a query, a brand or a browse node (else it searches everything)")
                continue
            seed_id = str(raw.get("id") or _slug(query) or _slug(
                "node-" + (node or "") + ("-" + brands[0] if brands else "")) or f"seed-{j}")
            if seed_id in ids:
                self.fail(sw, f"duplicate seed id {seed_id!r} in lane {lane_id!r}: give each seed an explicit id")
                continue
            ids.add(seed_id)
            if raw.get("partitions") is False:
                bands: tuple[PricePartition, ...] = ()
            elif "partitions" in raw:
                bands = self.partitions(raw.get("partitions"), f"{sw}.partitions")
            else:
                bands = partitions
            pages = int(raw.get("max_pages") or max_pages)
            if pages > creators.max_item_page:
                self.fail(sw, f"max_pages {pages} is above the API's itemPage ceiling "
                              f"({creators.max_item_page})")
            out.append(Seed(id=seed_id, query=query, brands=brands, browse_node=node,
                            sort_by=str(raw["sort_by"]) if raw.get("sort_by") else None,
                            search_index=str(raw.get("search_index") or search_index),
                            min_price=_num(raw.get("min_price")), partitions=bands,
                            max_pages=pages))
        return tuple(out)

    def catalog(self, raw: Mapping[str, Any]) -> CatalogSettings:
        w = "catalog"
        known_sql = str(raw.get("known_sql") or "")
        insert_sql = str(raw.get("insert_sql") or "")
        known_params = set(_PARAM_RE.findall(known_sql))
        if "asins" not in known_params:
            self.fail(f"{w}.known_sql", "must use %(asins)s (the text[] of ASINs to check)")
        for extra in sorted(known_params - set(KNOWN_PARAMS)):
            self.fail(f"{w}.known_sql", f"unknown parameter %({extra})s (only %(asins)s is bound)")
        insert_params = set(_PARAM_RE.findall(insert_sql))
        if "asin" not in insert_params:
            self.fail(f"{w}.insert_sql", "must use %(asin)s")
        for extra in sorted(insert_params - set(INSERT_PARAMS)):
            self.fail(f"{w}.insert_sql", f"unknown parameter %({extra})s: only "
                      + ", ".join(INSERT_PARAMS) + " are bound (Amazon content never is)")
        for name, sql in (("known_sql", known_sql), ("insert_sql", insert_sql)):
            # '%%' is the escaped literal percent; take the pairs out first so
            # the second '%' of a pair is not mistaken for a bare one.
            if _BAD_PERCENT_RE.search(sql.replace("%%", "")):
                self.fail(f"{w}.{name}", "positional %s or a bare % (write %% for a literal percent)")
        asin_regex = str(raw.get("asin_regex") or DEFAULT_ASIN_REGEX)
        try:
            re.compile(asin_regex)
        except re.error as exc:
            self.fail(f"{w}.asin_regex", f"invalid regex ({exc})")
        return CatalogSettings(
            known_sql=known_sql, insert_sql=insert_sql, dsn_env=raw.get("dsn_env") or None,
            statement_timeout_ms=int(raw.get("statement_timeout_ms") or 30000),
            connect_timeout_s=int(raw.get("connect_timeout_s") or 15),
            asin_regex=asin_regex, site_id=raw.get("site_id"))

    def seed_sources(self, raw: Mapping[str, Any], lane_ids: set[str]
                     ) -> tuple[tuple[SeedFileSource, ...], tuple[SeedQuerySource, ...]]:
        names: set[str] = set()
        files: list[SeedFileSource] = []
        queries: list[SeedQuerySource] = []

        def common(src: Mapping[str, Any], w: str) -> dict[str, Any]:
            name = str(src.get("name") or "")
            if name in names:
                self.fail(w, f"duplicate seed source name {name!r}")
            names.add(name)
            lanes = src.get("lanes")
            if lanes is not None:
                for lane in lanes:
                    if lane not in lane_ids:
                        self.fail(f"{w}.lanes", f"unknown lane {lane!r}")
            default_lane = src.get("default_lane")
            if default_lane is not None and default_lane not in lane_ids:
                self.fail(f"{w}.default_lane", f"unknown lane {default_lane!r}")
            lane_map = src.get("lane_map") or {}
            if not isinstance(lane_map, Mapping):
                self.fail(f"{w}.lane_map", "expected a mapping of source label -> lane id")
                lane_map = {}
            for label, lane in lane_map.items():
                if lane not in lane_ids:
                    self.fail(f"{w}.lane_map.{label}", f"unknown lane {lane!r}")
            confidence = _num(src.get("confidence"))
            if src.get("confidence") is not None and (confidence is None or not 0 <= confidence <= 1):
                self.fail(f"{w}.confidence", "must be a number in [0, 1]")
                confidence = None
            return {"lanes": frozenset(lanes) if lanes is not None else None,
                    "default_lane": default_lane,
                    "lane_map": {str(k): str(v) for k, v in lane_map.items()},
                    "trust_labels": bool(src.get("trust_labels", False)),
                    "confidence": confidence}

        def filters(src: Mapping[str, Any], key: str, w: str) -> dict[str, Any]:
            value = src.get(key) or {}
            if not isinstance(value, Mapping):
                self.fail(f"{w}.{key}", "expected a mapping")
                return {}
            return dict(value)

        for i, src in enumerate(raw.get("files") or []):
            w = f"seed_asins.files[{i}]"
            shared = common(src, w)
            files.append(SeedFileSource(
                name=str(src["name"]), path=self.path(str(src["path"])),
                json_path=src.get("json_path") or None,
                asin_field=str(src.get("asin_field") or "asin"),
                lane_field=str(src.get("lane_field") or "lane"),
                phase_field=str(src.get("phase_field") or "phase"),
                title_field=str(src["title_field"]) if src.get("title_field") else None,
                where=filters(src, "where", w), where_not=filters(src, "where_not", w),
                enabled=bool(src.get("enabled", True)), **shared))
        for i, src in enumerate(raw.get("queries") or []):
            w = f"seed_asins.queries[{i}]"
            shared = common(src, w)
            sql = str(src.get("sql") or "").strip()
            if sql.endswith(";"):
                sql = sql[:-1].rstrip()
            if not _SELECT_RE.match(sql):
                self.fail(f"{w}.sql", "must be a read-only SELECT (or WITH ... SELECT)")
            if ";" in sql:
                # One statement only: a 'COMMIT; DELETE ...' tail would end the
                # READ ONLY transaction and run in a new one.
                self.fail(f"{w}.sql", "must be a single statement (no ';' except one at the end)")
            # The engine binds its own keyset parameters around the SELECT, so
            # a placeholder of the source's own could never be bound.
            if _PARAM_RE.search(sql):
                self.fail(f"{w}.sql", "takes no parameters (it runs as a fixed SELECT)")
            if not src.get("site") and not src.get("dsn_env"):
                self.fail(w, "needs `site` (registry name) or `dsn_env`")
            queries.append(SeedQuerySource(
                name=str(src["name"]), sql=sql, site=src.get("site") or None,
                dsn_env=src.get("dsn_env") or None,
                classify_titles=bool(src.get("classify_titles", False)),
                refresh_hours=float(src.get("refresh_hours", 168.0)),
                statement_timeout_ms=int(src.get("statement_timeout_ms") or 30000),
                max_rows=int(src.get("max_rows") or 5000),
                enabled=bool(src.get("enabled", True)), **shared))
        return tuple(files), tuple(queries)

    def qa(self, raw: Mapping[str, Any], lane_ids: set[str], asin_re: re.Pattern) -> QASettings:
        golden: dict[str, str] = {}
        if raw.get("golden_file"):
            p = self.path(str(raw["golden_file"]))
            try:
                doc = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                self.fail("qa.golden_file", f"cannot read {p.name}: {exc}")
                doc = {}
            if not isinstance(doc, Mapping):
                self.fail("qa.golden_file", "expected a JSON object of ASIN -> label")
                doc = {}
            golden.update({str(k).strip().upper(): str(v) for k, v in doc.items()
                           if not str(k).startswith("_")})
        golden.update({str(k).strip().upper(): str(v) for k, v in (raw.get("golden") or {}).items()})
        for asin, label in sorted(golden.items()):
            if not asin_re.match(asin):
                self.fail("qa.golden", f"{asin!r} is not an ASIN")
            if label != REJECT_LABEL and label not in lane_ids:
                self.fail("qa.golden", f"{asin}: label {label!r} is neither a lane id nor 'reject'")
        return QASettings(golden=golden, window=int(raw.get("window") or 500),
                          min_observations=int(raw.get("min_observations") or 10))

    # -- whole document ------------------------------------------------------------

    def build(self, doc: Mapping[str, Any], source: str) -> DiscoveryConfig:
        doc = _strip_comments(doc)
        if str(doc.get("schema_version", SCHEMA_VERSION)) != SCHEMA_VERSION:
            self.fail("schema_version", f"unsupported (expected {SCHEMA_VERSION!r})")
        cr = doc.get("creators") or {}
        max_item_page = int(cr.get("max_item_page") or CREATORS_MAX_ITEM_PAGE)
        if max_item_page > CREATORS_MAX_ITEM_PAGE:
            self.fail("creators.max_item_page", f"{max_item_page} is above the API's itemPage "
                                                f"ceiling ({CREATORS_MAX_ITEM_PAGE}); a config may "
                                                "only lower it")
            max_item_page = CREATORS_MAX_ITEM_PAGE
        creators = CreatorsSettings(
            partner_tag=cr.get("partner_tag") or None,
            partner_tag_env=str(cr.get("partner_tag_env") or DEFAULT_PARTNER_TAG_ENV),
            search_index=str(cr.get("search_index") or "All"),
            max_item_page=max(1, max_item_page),
            result_count_cap=int(cr.get("result_count_cap") or CREATORS_RESULT_COUNT_CAP),
            price_filter_unit=str(cr.get("price_filter_unit") or "cents"))
        lm = doc.get("limits") or {}
        limits = Limits(
            max_calls_per_run=int(lm.get("max_calls_per_run", 40)),
            max_new_per_run=int(lm.get("max_new_per_run", 300)),
            max_pages_per_query=int(lm.get("max_pages_per_query", CREATORS_MAX_ITEM_PAGE)),
            max_seed_asins_per_run=int(lm.get("max_seed_asins_per_run", 5000)),
            stop_if_new_in_lane_below=int(lm.get("stop_if_new_in_lane_below", 0) or 0),
            low_yield_from_page=max(1, int(lm.get("low_yield_from_page", 2) or 1)))
        if limits.max_pages_per_query > creators.max_item_page:
            self.fail("limits.max_pages_per_query",
                      f"{limits.max_pages_per_query} is above the API's itemPage ceiling "
                      f"({creators.max_item_page})")
        bd = doc.get("budget") or {}
        budget = BudgetSettings(
            protect_consumers=tuple(str(c) for c in bd.get("protect_consumers") or []),
            keep_unused_fraction_of_day=float(bd.get("keep_unused_fraction_of_day", 0.0)))
        pr = doc.get("pricing") or {}
        pricing = PricingSettings(
            min_price=float(pr.get("min_price", MIN_PRICE_FLOOR)),
            currencies=tuple(str(c).upper() for c in (pr.get("currencies") or ["USD"])),
            require_new_condition=bool(pr.get("require_new_condition", True)))
        if not pricing.min_price >= MIN_PRICE_FLOOR:
            self.fail("pricing.min_price", f"{pricing.min_price:g} is below the ${MIN_PRICE_FLOOR:g} "
                                           "pricing-integrity floor (no $0 or sub-$1 offers)")
        catalog = self.catalog(doc.get("catalog") or {})
        phases = doc.get("active_phases")
        active_phases = frozenset(str(p) for p in phases) if phases is not None else None
        measures = self.measures(doc.get("measures"))
        reject_rules = self.rules(doc.get("reject_patterns"), "reject_patterns")
        lanes: list[Lane] = []
        for i, raw in enumerate(doc.get("lanes") or []):
            lane = self.lane(_strip_comments(raw), i, measures=measures, active_phases=active_phases,
                             creators=creators, limits=limits)
            if lane is None:
                continue
            if any(existing.id == lane.id for existing in lanes):
                self.fail(f"lanes[{i}]", f"duplicate lane id {lane.id!r}")
                continue
            lanes.append(lane)
        lane_ids = {lane.id for lane in lanes}
        if active_phases is not None:
            known_phases = {lane.phase for lane in lanes if lane.phase}
            for p in sorted(active_phases - known_phases):
                self.fail("active_phases", f"no lane has phase {p!r}")
        if lanes and not any(lane.active for lane in lanes):
            self.fail("lanes", "no lane is active (check enabled and active_phases)")
        seed_files, seed_queries = self.seed_sources(doc.get("seed_asins") or {}, lane_ids)
        try:
            asin_re = re.compile(catalog.asin_regex)
        except re.error:  # already reported by catalog(); keep validating the rest
            asin_re = re.compile(DEFAULT_ASIN_REGEX)
        qa = self.qa(doc.get("qa") or {}, lane_ids, asin_re)
        cf = doc.get("confidence") or {}
        confidence = ConfidenceSettings(
            node_and_title=float(cf.get("node_and_title", 0.9)),
            title_only=float(cf.get("title_only", 0.7)),
            node_only=float(cf.get("node_only", 0.6)),
            seed=float(cf.get("seed", 0.8)))
        rot = doc.get("rotation") or {}
        rotation = RotationSettings(cycle_cooldown_hours=float(rot.get("cycle_cooldown_hours", 0.0)))
        cfg = DiscoveryConfig(
            consumer=str(doc.get("consumer") or ""), catalog=catalog, lanes=tuple(lanes),
            site=doc.get("site") or None, enabled=bool(doc.get("enabled", True)),
            creators=creators, limits=limits, budget=budget, pricing=pricing,
            active_phases=active_phases, reject_rules=reject_rules, measures=measures,
            rotation=rotation, seed_files=seed_files, seed_queries=seed_queries, qa=qa,
            confidence=confidence, source=source, fingerprint=fingerprint(doc))
        if not cfg.consumer:
            self.fail("consumer", "required")
        if not self.problems:
            counts = Counter(q.key for q in cfg.queries())
            for key in sorted(k for k, n in counts.items() if n > 1):
                self.fail("lanes", f"two searches share the key {key!r}")
        return cfg
