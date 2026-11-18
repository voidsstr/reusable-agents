"""Site registry: the one list of the sites a framework deployment serves.

WHY THIS EXISTS
---------------
"Which sites exist" is answered today by two-site literals spread across the
consumers: the backlog dispatcher's producer tuple and its agent-id -> site
function, priority.py's starvation tuple and per-site content tiers, the
implementation queue's subject-tag heuristic, the implementer's
``examples/sites/<site>.yaml`` and ``DATABASE_URL_<SITE>`` lookups, the
goals-tracker and digest display maps, the DSN maps of the reconcilers and
backfills. A third site cannot be added without editing every one of them,
and a missed one fails silently: a producer that is never dispatched, or a
starvation boost that permanently favours a site with a gated content
pipeline.

This module is the primitive those consumers will read. It has NO consumers
yet: switching them over (W1) is a separate change with equivalence tests,
after the holiday freeze. Until then nothing imports it, so it changes no
behaviour, and it never writes framework storage.

MODEL
-----
A site entry (:class:`SiteEntry`) carries:

``name``               slug every consumer keys on (``RESPONDER_SITE``)
``label`` ``accent``   display name and colour for digests and goal cards
``kpi_goal_id`` ``kpi_label``
                       the site-goals-tracker goal shown as the headline KPI
``domain``             public hostname
``repo_path`` ``branch``
                       the site's checkout on the fleet host, and the branch
                       the implementer commits to
``seo_site_yaml``      the SEO site.yaml (implementer fallback config);
                       relative paths resolve against the framework repo root
``dsn_env``            NAME of the env var holding the site's Postgres DSN;
``dsn_env_aliases``    other names consumers read for it, in fallback order
``agent_prefix``       prefix of the site's per-site agent ids
``starvation``         takes part in priority's starvation rebalance
``content_tier``       priority tier of the site's content agents
``producer_templates`` ``'*-<suffix>'`` producer templates (``'*'`` becomes
                       ``agent_prefix``), inherited from the registry-wide
                       list unless the site sets its own
``producer_extras``    explicit extra producers (full ids or templates)
``content_agent_templates``
                       the agents that take ``content_tier``
``dispatch_group``     producer ordering group (see below)
``enabled``            false = known, but left out of every enumeration
``extra``              free-form consumer values not promoted to fields yet

Derived defaults (so a new site needs only name, domain, repo_path, branch):
``label`` = name title-cased; ``agent_prefix`` = name; ``dsn_env`` =
``DATABASE_URL_<NAME>`` (upper-cased, ``-`` -> ``_``, the implementer's rule);
``seo_site_yaml`` = ``examples/sites/<name>.yaml`` (the implementer's
fallback path); ``starvation`` false; ``content_tier`` none; ``enabled`` true;
``dispatch_group`` 0 for a site the built-in defaults define, and one more
than the highest built-in group for a site only the storage override defines.

Sites never share resources. No two sites, disabled ones included, may have
the same ``domain``, ``repo_path``, ``seo_site_yaml`` file or DSN env var
(``dsn_env`` and ``dsn_env_aliases`` together). A half-edited copy of an
existing entry is rejected, instead of pointing a new site's agents at a
live site's database or checkout. A registry must also keep at least one
enabled site: an empty one would silently stop every per-site agent.

PRODUCER ORDER
--------------
:meth:`SiteRegistry.producer_agent_ids` is the backlog dispatcher's walk
order. Sites are taken one ``dispatch_group`` at a time (ascending); inside a
group, ids are expanded template by template (registry-wide template order,
then templates only some sites declare), site by site in registry order, and
then each site's ``producer_extras``. With the built-in defaults this is the
dispatcher's tuple exactly. A site only the storage override defines lands in
a later group by default, so its producers are walked after every built-in
producer instead of taking a per-tick dispatch slot ahead of them; it is
interleaved with the built-in sites only if it sets ``dispatch_group``.

CONFIG (lowest to highest precedence)
-------------------------------------
    built-in defaults   shared/sites/registry.default.json (repo)
      <- storage override  config/sites-registry.json (framework storage)

Both are documents of the shape in shared/schemas/sites-registry.schema.json.
The override is merged over the defaults BY SITE NAME: a present field
replaces the default, ``null`` resets it to its derived default, objects
deep-merge, lists replace; a site the defaults do not define is appended in
override order. Top-level template lists replace the defaults' lists.
``"replace_sites": true`` drops the default sites altogether (a different
deployment of the framework). ``"enabled": false`` retires one site.

FAILURE SEMANTICS
-----------------
Invalid or missing built-in defaults raise :class:`SiteRegistryError` (a repo
bug the tests catch). They raise rather than degrade to an empty registry,
because "no sites" would be silently wrong for every consumer. The defaults
live under shared/, which the dashboard API image copies along with
framework/, so API-side consumers find them too. An invalid storage override
(wrong shape, a field rule, a resource another site uses, no enabled site
left, or content that is not a JSON object) is all-or-nothing: by default it
is ignored, the built-in defaults are used, and the reason lands in
``SiteRegistry.warnings`` and the log, so the reference sites keep running;
with ``strict=True`` it raises instead (use that before writing an override).
Validation messages never echo field values, only field paths and rules, so a
connection string pasted into ``dsn_env`` cannot leak into logs.

DSN VALUES
----------
The registry holds env var NAMES only. :func:`dsn_for` reads the value from
the environment and returns it to the caller; it is never logged, printed,
cached or stored here. By default it tries ``dsn_env`` only;
``include_aliases=True`` tries ``dsn_env_aliases`` after it, in order; and
``names=[...]`` tries exactly the names given (each must be one of the
site's), for a consumer that reads one particular naming convention.

CACHING
-------
:func:`load_registry` caches per storage backend for
``SITES_REGISTRY_CACHE_TTL_S`` seconds (default 60) because the consumers
poll (the dispatcher every minute, the drainer every 15 s). ``max_age_s`` is
the oldest cached registry the caller accepts (0 bypasses the cache);
:func:`clear_cache` empties it.

CLI (read-only; never prints a DSN value)
-----------------------------------------
    python3 -m framework.core.site_registry list [--all] [--json]
    python3 -m framework.core.site_registry show <site>
    python3 -m framework.core.site_registry get <site> <field>
    python3 -m framework.core.site_registry producers [<site>] [--all]
    python3 -m framework.core.site_registry site-for-agent <agent-id>
    python3 -m framework.core.site_registry dsn-env <site> [--aliases] [--resolved]
    python3 -m framework.core.site_registry validate [--override FILE] [--allow-drop]
    python3 -m framework.core.site_registry dump
Global flags: --no-storage (built-in defaults only), --defaults FILE.
``validate`` refuses an override that drops or disables a built-in site
unless ``--allow-drop`` is given. ``validate`` and ``dump`` note on stderr
when no storage override is applied, and warn about a similarly named key
the registry never reads.
Exit codes: 0 ok, 1 not found, 2 invalid input or registry (or a refused
drop).
"""
from __future__ import annotations

import argparse
import copy
import json
import logging
import os
import posixpath
import re
import sys
import threading
import time
import weakref
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence, Union

from .storage import StorageBackend, get_storage

log = logging.getLogger("framework.site_registry")

CONFIG_KEY = "config/sites-registry.json"
# Names an operator might write by mistake (the design's repo_layout spells
# configs '<feature>-config.json'). The registry never reads them; the CLI's
# validate and dump warn when one exists.
MISNAMED_CONFIG_KEYS: tuple[str, ...] = (
    "config/sites-registry-config.json",
    "config/site-registry.json",
    "config/sites_registry.json",
)
SCHEMA_VERSION = "1"
FRAMEWORK_ROOT = Path(__file__).resolve().parents[2]
# Under shared/ (not examples/) because the dashboard API image copies only
# framework/ and shared/ (framework/api/Dockerfile).
DEFAULTS_PATH = FRAMEWORK_ROOT / "shared" / "sites" / "registry.default.json"
SCHEMA_PATH = FRAMEWORK_ROOT / "shared" / "schemas" / "sites-registry.schema.json"
CACHE_TTL_ENV = "SITES_REGISTRY_CACHE_TTL_S"
DEFAULT_CACHE_TTL_S = 60.0
MAX_AGENT_ID_LEN = 63  # agent ids are kebab-case, <= 63 chars (create-agent.sh)

# Kept byte-identical to the patterns in shared/schemas/sites-registry.schema.json
# (framework/tests/test_site_registry.py compares them). This module matches
# with re.fullmatch, the stricter reading of the schema's anchored patterns.
NAME_PATTERN = r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$"
DOMAIN_PATTERN = r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$"
ABS_PATH_PATTERN = r"^/[^\x00-\x1f]*[^/\x00-\x1f]$"
BRANCH_PATTERN = r"^(?!.*\.\.)(?!.*//)(?!.*[/.]$)[A-Za-z0-9][A-Za-z0-9._/-]*$"
ENV_PATTERN = r"^[A-Z_][A-Z0-9_]*$"
YAML_PATH_PATTERN = r"^[^\x00-\x1f]+\.ya?ml$"
LABEL_PATTERN = r"^[^\x00-\x1f]+$"
ACCENT_PATTERN = r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$"
GOAL_PATTERN = r"^goal-[a-z0-9-]+$"
TEMPLATE_PATTERN = r"^\*(?:-[a-z0-9]+)+$"
AGENT_REF_PATTERN = r"^(?:\*(?:-[a-z0-9]+)+|[a-z][a-z0-9]*(?:-[a-z0-9]+)*)$"


class SiteRegistryError(ValueError):
    """The registry is invalid: the built-in defaults, or (strict mode) the
    storage override. Messages name field paths and rules, never values."""


class UnknownSiteError(KeyError):
    """A lookup named a site the registry does not define."""

    def __str__(self) -> str:  # KeyError would repr-quote the message
        return str(self.args[0]) if self.args else "unknown site"


# ---------------------------------------------------------------------------
# Field table (the runtime validator; mirrors the JSON schema)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _FieldSpec:
    kind: str                          # "str" | "bool" | "int" | "list" | "object"
    rule: str = ""                     # human rule for messages; never includes a value
    pattern: Optional[str] = None      # str fields
    max_len: Optional[int] = None      # str fields
    minimum: Optional[int] = None      # int fields
    maximum: Optional[int] = None
    item_pattern: Optional[str] = None  # list fields (items are strings)
    item_max_len: Optional[int] = None
    nullable: bool = False             # may be null in a RESOLVED entry


_NAME_RULE = "must be a lowercase kebab-case slug (a-z, 0-9, single '-'), at most 40 characters"
_TEMPLATE_RULE = "must be a template '*-<suffix>' (lowercase kebab-case suffix), unique in its list"

# Canonical order: SiteEntry.to_dict() and the schema's resolved 'site' use it.
SITE_FIELDS: dict[str, _FieldSpec] = {
    "name": _FieldSpec("str", _NAME_RULE, NAME_PATTERN, 40),
    "label": _FieldSpec("str", "must be 1-80 printable characters", LABEL_PATTERN, 80),
    "domain": _FieldSpec("str", "must be a bare lowercase hostname (no scheme, no path)",
                         DOMAIN_PATTERN, 253),
    "repo_path": _FieldSpec("str", "must be an absolute path without a trailing '/'",
                            ABS_PATH_PATTERN, 1024),
    "branch": _FieldSpec("str", "must be a git branch name", BRANCH_PATTERN, 100),
    "seo_site_yaml": _FieldSpec("str", "must be a path ending in .yaml or .yml",
                                YAML_PATH_PATTERN, 1024),
    "dsn_env": _FieldSpec("str", "must be an environment variable NAME (A-Z, 0-9, _)",
                          ENV_PATTERN, 128),
    "dsn_env_aliases": _FieldSpec("list", "must be environment variable NAMES (A-Z, 0-9, _), "
                                  "unique", item_pattern=ENV_PATTERN, item_max_len=128),
    "agent_prefix": _FieldSpec("str", _NAME_RULE, NAME_PATTERN, 40),
    "accent": _FieldSpec("str", "must be a hex colour (#rgb or #rrggbb)", ACCENT_PATTERN,
                         nullable=True),
    "kpi_goal_id": _FieldSpec("str", "must be a goal id ('goal-' + lowercase kebab-case)",
                              GOAL_PATTERN, 100, nullable=True),
    "kpi_label": _FieldSpec("str", "must be 1-120 printable characters", LABEL_PATTERN, 120,
                            nullable=True),
    "starvation": _FieldSpec("bool", "must be true or false"),
    "content_tier": _FieldSpec("int", "must be an integer tier", minimum=1, maximum=99,
                               nullable=True),
    "producer_templates": _FieldSpec("list", _TEMPLATE_RULE, item_pattern=TEMPLATE_PATTERN,
                                     item_max_len=62),
    "producer_extras": _FieldSpec("list", "must be agent ids or '*-<suffix>' templates, unique",
                                  item_pattern=AGENT_REF_PATTERN, item_max_len=MAX_AGENT_ID_LEN),
    "content_agent_templates": _FieldSpec("list", _TEMPLATE_RULE, item_pattern=TEMPLATE_PATTERN,
                                          item_max_len=62),
    "dispatch_group": _FieldSpec("int", "must be an integer group", minimum=0, maximum=99),
    "enabled": _FieldSpec("bool", "must be true or false"),
    "extra": _FieldSpec("object", "must be an object"),
}

# Present (non-empty) once the override is merged; no derived default exists.
REQUIRED_FIELDS: tuple[str, ...] = ("name", "domain", "repo_path", "branch")

_TEMPLATE_LIST_SPEC = SITE_FIELDS["producer_templates"]
DOCUMENT_KEYS: tuple[str, ...] = (
    "schema_version", "replace_sites", "producer_templates", "content_agent_templates", "sites",
)

_SAFE_TOKEN = re.compile(r"[A-Za-z0-9_.-]{1,128}")
_SECRET_HINT = re.compile(r"://|@|;|=")


def _safe(token: Any) -> str:
    """Quote a key or site name for a message only when it cannot be a
    secret-bearing value (no ':', '/', '@', '=', spaces ...)."""
    if isinstance(token, str) and _SAFE_TOKEN.fullmatch(token):
        return repr(token)
    return "<redacted>"


def _where_site(pos: int, name: Any, prefix: str = "") -> str:
    """'sites[i] (name)' for messages; the name only when it is a valid slug."""
    shown = name if isinstance(name, str) and re.fullmatch(NAME_PATTERN, name) else "<invalid name>"
    return f"{prefix}sites[{pos}] ({shown})"


def default_label(name: str) -> str:
    """Derived ``label``: the name title-cased (the goals-tracker fallback)."""
    return name.title()


def default_dsn_env(name: str) -> str:
    """Derived ``dsn_env``: ``DATABASE_URL_<NAME>``, upper-cased with '-' -> '_'
    (the implementer's run.sh rule)."""
    return "DATABASE_URL_" + name.upper().replace("-", "_")


def default_seo_site_yaml(name: str) -> str:
    """Derived ``seo_site_yaml``: the implementer's fallback path."""
    return f"examples/sites/{name}.yaml"


def expand_agent_ref(ref: str, prefix: str) -> str:
    """Expand a ``'*-<suffix>'`` template with an agent prefix; a full agent
    id is returned unchanged."""
    if ref.startswith("*-"):
        return prefix + ref[1:]
    return ref


class _FrozenDict(dict):
    """A read-only dict, used for ``SiteEntry.extra``.

    It subclasses dict rather than wrapping a MappingProxyType, so entries
    stay picklable and deep-copyable, and dataclasses.asdict() (which
    FastAPI's jsonable_encoder uses for dataclasses) and json.dumps handle
    them. ``.copy()`` returns an ordinary, mutable dict."""

    __slots__ = ()

    def _read_only(self, *args: Any, **kwargs: Any) -> Any:
        raise TypeError("site registry entries are read-only")

    __setitem__ = __delitem__ = __ior__ = _read_only  # type: ignore[assignment]
    clear = pop = popitem = setdefault = update = _read_only  # type: ignore[assignment]

    def __reduce__(self) -> tuple:
        # pickle would otherwise rebuild the dict through __setitem__
        return (_FrozenDict, (dict(self),))

    def __copy__(self) -> "_FrozenDict":
        return self

    def __deepcopy__(self, memo: dict) -> "_FrozenDict":
        return _FrozenDict({k: copy.deepcopy(v, memo) for k, v in self.items()})


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _FrozenDict({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {k: _thaw(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [_thaw(v) for v in value]
    return value


def _as_int(value: Any) -> Any:
    """JSON numbers like 2.0 are integers (JSON Schema draft-07 agrees)."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _check_value(key: str, value: Any, spec: _FieldSpec, where: str,
                 problems: list[str], *, allow_null: bool) -> None:
    """Append rule violations for one field to ``problems``. Never echoes the
    value itself."""
    label = f"{where}: '{key}'"
    if value is None:
        if not allow_null:
            problems.append(f"{label} must not be null")
        return
    if spec.kind == "str":
        if not isinstance(value, str):
            problems.append(f"{label} must be a string")
            return
        too_long = spec.max_len is not None and len(value) > spec.max_len
        if too_long or (spec.pattern and not re.fullmatch(spec.pattern, value)):
            hint = ""
            if key == "dsn_env" and _SECRET_HINT.search(value):
                hint = (" (it looks like a connection string: keep the DSN in the environment "
                        "and put the variable's NAME here)")
            problems.append(f"{label} {spec.rule}{hint}")
    elif spec.kind == "bool":
        if not isinstance(value, bool):
            problems.append(f"{label} {spec.rule}")
    elif spec.kind == "int":
        value = _as_int(value)
        if isinstance(value, bool) or not isinstance(value, int):
            problems.append(f"{label} {spec.rule}")
        elif ((spec.minimum is not None and value < spec.minimum)
              or (spec.maximum is not None and value > spec.maximum)):
            problems.append(f"{label} must be between {spec.minimum} and {spec.maximum}")
    elif spec.kind == "list":
        if not isinstance(value, (list, tuple)):
            problems.append(f"{label} must be a list")
            return
        seen: set[str] = set()
        for i, item in enumerate(value):
            bad = (not isinstance(item, str)
                   or (spec.item_max_len is not None and len(item) > spec.item_max_len)
                   or (spec.item_pattern is not None and not re.fullmatch(spec.item_pattern, item)))
            if bad:
                hint = ""
                if key == "dsn_env_aliases" and isinstance(item, str) and _SECRET_HINT.search(item):
                    hint = " (an item looks like a connection string, not a variable NAME)"
                problems.append(f"{label}[{i}] {spec.rule}{hint}")
            elif item in seen:
                problems.append(f"{label}[{i}] repeats an earlier item")
            else:
                seen.add(item)
    elif spec.kind == "object":
        if not isinstance(value, Mapping):
            problems.append(f"{label} {spec.rule}")
    else:  # pragma: no cover - table bug
        raise AssertionError(f"unknown field kind {spec.kind!r}")


def validate_document(doc: Any, *, where: str = "document") -> list[str]:
    """Shape-check one registry document (the defaults file or a storage
    override): known keys, field types and patterns, unique site names.
    Returns the problems; an empty list means the shape is valid. Whether a
    NEW site carries domain/repo_path/branch is checked after merging (see
    :meth:`SiteRegistry.from_documents`)."""
    if not isinstance(doc, Mapping):
        return [f"{where}: must be a JSON object"]
    problems: list[str] = []
    for key in doc:
        if isinstance(key, str) and key.startswith("_"):
            continue  # comment
        if key not in DOCUMENT_KEYS:
            problems.append(f"{where}: unknown key {_safe(key)}")
    if "schema_version" in doc and doc["schema_version"] != SCHEMA_VERSION:
        problems.append(f"{where}: 'schema_version' must be \"{SCHEMA_VERSION}\"")
    if "replace_sites" in doc and not isinstance(doc["replace_sites"], bool):
        problems.append(f"{where}: 'replace_sites' must be true or false")
    for key in ("producer_templates", "content_agent_templates"):
        if key in doc:
            _check_value(key, doc[key], _TEMPLATE_LIST_SPEC, where, problems, allow_null=False)
    if "sites" not in doc:
        return problems
    sites = doc["sites"]
    if not isinstance(sites, list):
        problems.append(f"{where}: 'sites' must be a list")
        return problems
    seen: set[str] = set()
    for i, site in enumerate(sites):
        if not isinstance(site, Mapping):
            problems.append(f"{where}: sites[{i}] must be an object")
            continue
        name = site.get("name")
        at = _where_site(i, name, prefix=f"{where}: ")
        name_ok = (isinstance(name, str) and len(name) <= 40
                   and re.fullmatch(NAME_PATTERN, name) is not None)
        if not name_ok:
            problems.append(f"{at}: 'name' is required and {_NAME_RULE}")
        else:
            if name in seen:
                problems.append(f"{at}: duplicate site name")
            seen.add(name)
        for key, value in site.items():
            if key == "name" or (isinstance(key, str) and key.startswith("_")):
                continue
            spec = SITE_FIELDS.get(key)
            if spec is None:
                problems.append(f"{at}: unknown key {_safe(key)}")
                continue
            _check_value(key, value, spec, at, problems, allow_null=True)
    return problems


# ---------------------------------------------------------------------------
# Entries and the registry
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SiteEntry:
    """One resolved site. Immutable; holds env var NAMES only, never values."""

    name: str
    label: str
    domain: str
    repo_path: str
    branch: str
    seo_site_yaml: str
    dsn_env: str
    dsn_env_aliases: tuple[str, ...] = ()
    agent_prefix: str = ""
    accent: Optional[str] = None
    kpi_goal_id: Optional[str] = None
    kpi_label: Optional[str] = None
    starvation: bool = False
    content_tier: Optional[int] = None
    producer_templates: tuple[str, ...] = ()
    producer_extras: tuple[str, ...] = ()
    content_agent_templates: tuple[str, ...] = ()
    dispatch_group: int = 0
    enabled: bool = True
    extra: Mapping[str, Any] = field(default_factory=_FrozenDict, hash=False)

    def __post_init__(self) -> None:
        if not self.agent_prefix:
            object.__setattr__(self, "agent_prefix", self.name)
        for key in ("dsn_env_aliases", "producer_templates", "producer_extras",
                    "content_agent_templates"):
            object.__setattr__(self, key, tuple(getattr(self, key)))
        object.__setattr__(self, "extra", _freeze(dict(self.extra)))

    def agent_id(self, ref: str) -> str:
        """This site's id for a ``'*-<suffix>'`` template (a full id passes
        through unchanged)."""
        return expand_agent_ref(ref, self.agent_prefix)

    @property
    def producers(self) -> tuple[str, ...]:
        """Templated producers, then explicit extras, expanded."""
        return (tuple(self.agent_id(t) for t in self.producer_templates)
                + tuple(self.agent_id(x) for x in self.producer_extras))

    @property
    def content_agents(self) -> tuple[str, ...]:
        """The agents that take ``content_tier``."""
        return tuple(self.agent_id(t) for t in self.content_agent_templates)

    @property
    def seo_site_yaml_path(self) -> Path:
        """``seo_site_yaml`` resolved against the framework repo root."""
        p = Path(self.seo_site_yaml)
        return p if p.is_absolute() else FRAMEWORK_ROOT / p

    def dsn_env_names(self, include_aliases: bool = False) -> tuple[str, ...]:
        """Env var NAMES to try for the DSN, in order."""
        return (self.dsn_env,) + (self.dsn_env_aliases if include_aliases else ())

    def to_dict(self) -> dict[str, Any]:
        """JSON-able resolved entry (schema ``#/definitions/site``). Holds no
        secret: the DSN is referenced by env var name only."""
        return {key: _thaw(getattr(self, key)) for key in SITE_FIELDS}


def _merge(base: Mapping[str, Any], patch: Mapping[str, Any], *,
           site_level: bool = True) -> dict[str, Any]:
    """Override semantics: present values replace, None removes (the derived
    default applies; inside ``extra`` the key is simply unset), mappings
    deep-merge, lists replace. '_' keys are comments at the site level only;
    inside ``extra`` they are ordinary data."""
    out = copy.deepcopy(dict(base))
    for key, value in patch.items():
        if site_level and isinstance(key, str) and key.startswith("_"):
            continue
        if value is None:
            out.pop(key, None)
        elif isinstance(value, Mapping):
            prior = out.get(key)
            out[key] = _merge(prior if isinstance(prior, Mapping) else {}, value,
                              site_level=False)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _resolve_entry(raw: Mapping[str, Any], pos: int, templates: Sequence[str],
                   content_templates: Sequence[str], default_group: int = 0) -> SiteEntry:
    """Fill derived defaults into one merged site dict and validate it.
    ``default_group`` is the derived ``dispatch_group`` (see :func:`_build`)."""
    name = raw.get("name")
    where = _where_site(pos, name)
    problems: list[str] = []
    for key in REQUIRED_FIELDS:
        if raw.get(key) in (None, ""):
            problems.append(f"{where}: '{key}' is required")
    if problems:
        raise SiteRegistryError("; ".join(problems))

    def pick(key: str, default: Any) -> Any:
        value = raw.get(key)
        return default if value is None else value

    resolved: dict[str, Any] = {
        "name": name,
        "label": pick("label", default_label(name)),
        "domain": raw["domain"],
        "repo_path": raw["repo_path"],
        "branch": raw["branch"],
        "seo_site_yaml": pick("seo_site_yaml", default_seo_site_yaml(name)),
        "dsn_env": pick("dsn_env", default_dsn_env(name)),
        "dsn_env_aliases": list(pick("dsn_env_aliases", [])),
        "agent_prefix": pick("agent_prefix", name),
        "accent": raw.get("accent"),
        "kpi_goal_id": raw.get("kpi_goal_id"),
        "kpi_label": raw.get("kpi_label"),
        "starvation": pick("starvation", False),
        "content_tier": _as_int(raw.get("content_tier")),
        "producer_templates": list(pick("producer_templates", templates)),
        "producer_extras": list(pick("producer_extras", [])),
        "content_agent_templates": list(pick("content_agent_templates", content_templates)),
        "dispatch_group": _as_int(pick("dispatch_group", default_group)),
        "enabled": pick("enabled", True),
        "extra": pick("extra", {}),
    }
    for key, spec in SITE_FIELDS.items():
        _check_value(key, resolved[key], spec, where, problems, allow_null=spec.nullable)
    if resolved["dsn_env"] in resolved["dsn_env_aliases"]:
        problems.append(f"{where}: 'dsn_env_aliases' repeats 'dsn_env'")
    if problems:
        raise SiteRegistryError("; ".join(problems))
    return SiteEntry(**resolved)


class SiteRegistry:
    """An immutable, validated set of :class:`SiteEntry` in registry order.

    Build one with :func:`load_registry` (defaults + storage override) or
    :meth:`from_documents` (in-memory documents). Construction validates the
    cross-site rules: unique names and agent prefixes, valid expanded agent
    ids, every producer and content agent owned by exactly one site and
    mapping back to it through :meth:`site_for_agent`.
    """

    def __init__(self, sites: Iterable[SiteEntry], *,
                 producer_templates: Sequence[str] = (),
                 content_agent_templates: Sequence[str] = (),
                 sources: Sequence[str] = (),
                 warnings: Sequence[str] = ()) -> None:
        self._sites: tuple[SiteEntry, ...] = tuple(sites)
        self.producer_templates: tuple[str, ...] = tuple(producer_templates)
        self.content_agent_templates: tuple[str, ...] = tuple(content_agent_templates)
        self.sources: tuple[str, ...] = tuple(sources)
        self.warnings: tuple[str, ...] = tuple(warnings)
        self._by_name: dict[str, SiteEntry] = {}
        for entry in self._sites:
            self._by_name.setdefault(entry.name, entry)
        # Longest prefix first, so 'a-b' wins over 'a' for 'a-b-x-agent'.
        self._prefixes: tuple[tuple[str, str], ...] = tuple(sorted(
            ((e.agent_prefix, e.name) for e in self._sites), key=lambda pn: -len(pn[0])))
        self._explicit_owner: dict[str, str] = {}
        for entry in self._sites:
            for ref in entry.producer_extras:
                aid = entry.agent_id(ref)
                if not aid.startswith(entry.agent_prefix + "-"):
                    self._explicit_owner.setdefault(aid, entry.name)
        problems = self._cross_check()
        if problems:
            raise SiteRegistryError("; ".join(problems))

    # -- construction ------------------------------------------------------

    @classmethod
    def from_documents(cls, defaults_doc: Mapping[str, Any],
                       override_doc: Optional[Mapping[str, Any]] = None, *,
                       defaults_source: str = "defaults",
                       override_source: str = "override",
                       strict: bool = False,
                       warnings: Sequence[str] = ()) -> "SiteRegistry":
        """Merge an override document over a defaults document.

        The defaults must be valid on their own (else SiteRegistryError). An
        invalid override raises with ``strict=True``; otherwise it is ignored
        as a whole and the reason is appended to ``warnings``. ``None`` means
        "no override"; callers that parse a document (``read_override``, the
        CLI) reject a JSON ``null`` themselves, so it never arrives here as
        "absent".
        """
        problems = validate_document(defaults_doc, where=defaults_source)
        if problems:
            raise SiteRegistryError(f"{defaults_source} is invalid: " + "; ".join(problems))
        try:
            base = _build(defaults_doc, None, sources=[defaults_source], warnings=list(warnings))
        except SiteRegistryError:
            raise
        except Exception as exc:  # e.g. RecursionError on absurdly nested data
            raise SiteRegistryError(f"{defaults_source} is invalid: {type(exc).__name__} "
                                    "while resolving it") from exc
        if override_doc is None:
            return base
        try:
            problems = validate_document(override_doc, where=override_source)
            if problems:
                raise SiteRegistryError("; ".join(problems))
            return _build(defaults_doc, override_doc,
                          sources=[defaults_source, override_source], warnings=list(warnings))
        except Exception as exc:
            # Whatever is wrong with the override, the built-in sites keep
            # running. Only SiteRegistryError text is shown, because it never
            # echoes a value; any other error (a RecursionError from absurd
            # nesting, say) is named by its type alone.
            reason = (str(exc) if isinstance(exc, SiteRegistryError)
                      else f"{type(exc).__name__} while merging it (malformed document)")
            if strict:
                raise SiteRegistryError(f"{override_source} is invalid: {reason}") from None
            note = f"{override_source} ignored, built-in defaults used instead: {reason}"
            log.warning("site registry: %s", note)
            return base._with_warnings([note])

    def _with_warnings(self, more: Sequence[str]) -> "SiteRegistry":
        return SiteRegistry(self._sites, producer_templates=self.producer_templates,
                            content_agent_templates=self.content_agent_templates,
                            sources=self.sources, warnings=self.warnings + tuple(more))

    @staticmethod
    def _resources(entry: SiteEntry) -> list[tuple[str, str, str]]:
        """``(kind, comparable value, how a message names it)`` for every
        resource a site must not share with another site. Paths are compared
        after normalisation, so '/a/./b' and '/a//b' are '/a/b'."""
        out = [
            ("domain", entry.domain, "'domain'"),
            ("repo_path", posixpath.normpath(entry.repo_path), "'repo_path'"),
            ("seo_site_yaml", os.path.normpath(str(entry.seo_site_yaml_path)),
             "'seo_site_yaml' (the same file)"),
        ]
        out += [("dsn_env", name, f"DSN env var {_safe(name)}")
                for name in entry.dsn_env_names(include_aliases=True)]
        return out

    def _cross_check(self) -> list[str]:
        problems: list[str] = []
        names: set[str] = set()
        prefixes: dict[str, str] = {}
        owner: dict[str, str] = {}
        used: dict[tuple[str, str], str] = {}
        shared_resource = False
        for entry in self._sites:
            where = f"site {_safe(entry.name)}"
            # Field rules again, for entries constructed directly rather than
            # through _resolve_entry (which has already checked its own).
            fields = entry.to_dict()
            before = len(problems)
            for key, spec in SITE_FIELDS.items():
                _check_value(key, fields[key], spec, where, problems, allow_null=spec.nullable)
            if len(problems) > before:
                continue  # the checks below assume well-typed fields
            if entry.dsn_env in entry.dsn_env_aliases:
                problems.append(f"{where}: 'dsn_env_aliases' repeats 'dsn_env'")
            # No two sites, disabled ones included, share a domain, checkout,
            # site.yaml or database: a half-edited copy of a live site's entry
            # would otherwise send the new site's agents to the live site's DB.
            for kind, value, shown in self._resources(entry):
                prior = used.setdefault((kind, value), entry.name)
                if prior != entry.name:
                    shared_resource = True
                    problems.append(f"{where}: {shown} is already used by site {_safe(prior)}")
            if entry.name in names:
                problems.append(f"{where}: duplicate site name")
            names.add(entry.name)
            if entry.agent_prefix in prefixes:
                problems.append(f"{where}: agent_prefix is already used by site "
                                f"{_safe(prefixes[entry.agent_prefix])}")
            prefixes.setdefault(entry.agent_prefix, entry.name)
            own: set[str] = set()
            for kind, ids in (("producer", entry.producers), ("content agent", entry.content_agents)):
                for aid in ids:
                    if len(aid) > MAX_AGENT_ID_LEN or not re.fullmatch(NAME_PATTERN, aid):
                        problems.append(f"{where}: {kind} id {_safe(aid)} is not a valid agent id "
                                        f"(lowercase kebab-case, at most {MAX_AGENT_ID_LEN} chars)")
                    if kind == "producer":
                        if aid in own:
                            problems.append(f"{where}: producer {_safe(aid)} is listed twice")
                        own.add(aid)
                        if aid in owner and owner[aid] != entry.name:
                            problems.append(f"{where}: producer {_safe(aid)} is already a producer "
                                            f"of site {_safe(owner[aid])}")
                        owner.setdefault(aid, entry.name)
        if shared_resource:
            problems.append("sites must not share a domain, repo_path, seo_site_yaml file or "
                            "DSN env var; give each site its own")
        if not any(e.enabled for e in self._sites):
            problems.append("the registry has no enabled site, so every per-site list would be "
                            "empty; at least one site must stay enabled")
        if problems:
            return problems
        # Round trip: every id a site owns must resolve back to that site.
        for entry in self._sites:
            for aid in entry.producers + entry.content_agents:
                back = self.site_for_agent(aid)
                if back != entry.name:
                    problems.append(f"site {_safe(entry.name)}: agent {_safe(aid)} resolves to "
                                    f"{_safe(back) if back else 'no site'} through the agent "
                                    f"prefixes; give it an id under prefix {_safe(entry.agent_prefix)}")
        return problems

    # -- lookups -------------------------------------------------------------

    def get_site(self, name: str) -> Optional[SiteEntry]:
        """The entry for ``name`` (enabled or not), or None."""
        if not isinstance(name, str):
            return None
        return self._by_name.get(name)

    def require_site(self, name: str) -> SiteEntry:
        """The entry for ``name``; UnknownSiteError when it is not defined."""
        entry = self.get_site(name)
        if entry is None:
            known = ", ".join(e.name for e in self._sites) or "none"
            raise UnknownSiteError(f"unknown site {_safe(name)} (registry defines: {known})")
        return entry

    def list_sites(self, include_disabled: bool = False) -> list[SiteEntry]:
        """Entries in registry order; disabled ones only on request."""
        return [e for e in self._sites if include_disabled or e.enabled]

    def site_names(self, include_disabled: bool = False) -> list[str]:
        return [e.name for e in self.list_sites(include_disabled)]

    def site_for_agent(self, agent_id: str) -> Optional[str]:
        """The site an agent id belongs to: the longest agent prefix the id
        starts with (followed by '-'), else an explicit producer extra; None
        for framework-wide agents. Disabled sites still own their agents."""
        if not isinstance(agent_id, str) or not agent_id:
            return None
        for prefix, name in self._prefixes:
            if agent_id.startswith(prefix + "-"):
                return name
        return self._explicit_owner.get(agent_id)

    def split_agent_id(self, agent_id: str) -> tuple[Optional[str], str]:
        """``(site, local_id)``: the id without its site prefix, the name the
        agent's directory has inside the site repo. ``(None, agent_id)`` when
        no site owns it; an explicit extra keeps its full id."""
        site = self.site_for_agent(agent_id)
        if site is None:
            return None, agent_id
        prefix = self._by_name[site].agent_prefix + "-"
        if agent_id.startswith(prefix):
            return site, agent_id[len(prefix):]
        return site, agent_id

    def agent_id_for(self, site: str, template: str) -> str:
        """``template`` ('*-<suffix>') expanded for ``site``."""
        return self.require_site(site).agent_id(template)

    # -- producers, tiers, starvation ----------------------------------------

    def producers_for(self, site: str) -> list[str]:
        """The site's producer agent ids (templates, then extras). Returned
        even when the site is disabled; the global walk list skips it."""
        return list(self.require_site(site).producers)

    def producer_agent_ids(self, include_disabled: bool = False) -> list[str]:
        """Every producer the backlog dispatcher walks, in walk order (see the
        module docstring, PRODUCER ORDER)."""
        sites = self.list_sites(include_disabled)
        out: list[str] = []
        seen: set[str] = set()

        def add(aid: str) -> None:
            if aid not in seen:
                seen.add(aid)
                out.append(aid)

        for group in sorted({e.dispatch_group for e in sites}):
            members = [e for e in sites if e.dispatch_group == group]
            # Registry-wide template order first, then templates only some
            # sites declare, in first-seen order.
            order: list[str] = list(self.producer_templates)
            for entry in members:
                for template in entry.producer_templates:
                    if template not in order:
                        order.append(template)
            for template in order:
                for entry in members:
                    if template in entry.producer_templates:
                        add(entry.agent_id(template))
            for entry in members:
                for ref in entry.producer_extras:
                    add(entry.agent_id(ref))
        return out

    def content_agents_for(self, site: str) -> list[str]:
        return list(self.require_site(site).content_agents)

    def content_tier_map(self, include_disabled: bool = False) -> dict[int, list[str]]:
        """``{tier: [content agent ids]}`` for sites that set ``content_tier``
        and have content agents; tiers ascending, sites in registry order.
        What priority.py's tier ladder needs for the per-site content tiers.
        A tier here may already exist in the ladder with other agents (tier 4
        is research / catalog hygiene): a consumer appends these ids to that
        tier's agents and keeps its label, and creates a block labelled
        '<label> content production' only for a tier the ladder lacks. The
        registry's ``content_tier`` is then the one place a site's content
        tier is set."""
        tiers: dict[int, list[str]] = {}
        for entry in self.list_sites(include_disabled):
            if entry.content_tier is not None and entry.content_agents:
                tiers.setdefault(entry.content_tier, []).extend(entry.content_agents)
        return dict(sorted(tiers.items()))

    def starvation_sites(self) -> list[str]:
        """Enabled sites that take part in the starvation rebalance."""
        return [e.name for e in self.list_sites() if e.starvation]

    # -- DSN ------------------------------------------------------------------

    def dsn_env_for(self, site: str) -> str:
        """NAME of the env var holding ``site``'s DSN."""
        return self.require_site(site).dsn_env

    @staticmethod
    def _dsn_candidates(entry: SiteEntry, include_aliases: bool,
                        names: Union[str, Sequence[str], None]) -> tuple[str, ...]:
        if names is None:
            return entry.dsn_env_names(include_aliases)
        if include_aliases:
            raise TypeError("pass names= or include_aliases=True, not both")
        chosen = (names,) if isinstance(names, str) else tuple(dict.fromkeys(names))
        known = entry.dsn_env_names(include_aliases=True)
        unknown = [n for n in chosen if n not in known]
        if not chosen or unknown:
            raise ValueError(
                f"site {_safe(entry.name)}: names= must list some of its DSN env vars "
                f"({', '.join(known)}); "
                + (f"not one of them: {', '.join(_safe(n) for n in unknown)}" if unknown
                   else "none given"))
        return chosen

    def describe_dsn(self, site: str, *, include_aliases: bool = False,
                     names: Union[str, Sequence[str], None] = None,
                     environ: Optional[Mapping[str, str]] = None) -> dict[str, Any]:
        """Which env var would supply the DSN, by NAME only:
        ``{"site", "candidates": [...], "resolved_env": name | None}``.
        ``names`` (see :meth:`dsn_for`) replaces the default candidates."""
        entry = self.require_site(site)
        env = os.environ if environ is None else environ
        candidates = self._dsn_candidates(entry, include_aliases, names)
        resolved = next((n for n in candidates if env.get(n)), None)
        return {"site": entry.name, "candidates": list(candidates), "resolved_env": resolved}

    def dsn_for(self, site: str, *, include_aliases: bool = False,
                names: Union[str, Sequence[str], None] = None,
                environ: Optional[Mapping[str, str]] = None) -> Optional[str]:
        """``site``'s DSN from the environment: ``dsn_env`` first, then (only
        with ``include_aliases``) ``dsn_env_aliases`` in order. ``names``
        instead tries exactly the env vars given, in that order, for a
        consumer that reads one convention (say only ``<SITE>_DATABASE_URL``);
        each must be the site's ``dsn_env`` or one of its aliases (else
        ValueError), and it cannot be combined with ``include_aliases``
        (TypeError). None when none is set. The value is returned, never
        logged."""
        info = self.describe_dsn(site, include_aliases=include_aliases, names=names,
                                 environ=environ)
        name = info["resolved_env"]
        if name is None:
            log.debug("site %s: no DSN, none of %s is set",
                      info["site"], ", ".join(info["candidates"]))
            return None
        log.debug("site %s: DSN from $%s", info["site"], name)
        env = os.environ if environ is None else environ
        return env.get(name)

    def seo_site_yaml_path(self, site: str) -> Path:
        return self.require_site(site).seo_site_yaml_path

    # -- serialisation -------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """The resolved registry as a valid registry document (it can be
        written back as defaults or a replace_sites override)."""
        return {
            "schema_version": SCHEMA_VERSION,
            "producer_templates": list(self.producer_templates),
            "content_agent_templates": list(self.content_agent_templates),
            "sites": [e.to_dict() for e in self._sites],
        }

    def describe_site(self, site: str) -> dict[str, Any]:
        entry = self.require_site(site)
        doc = entry.to_dict()
        doc["derived"] = {
            "producers": list(entry.producers),
            "content_agents": list(entry.content_agents),
            "seo_site_yaml_path": str(entry.seo_site_yaml_path),
            "dsn_env_names": list(entry.dsn_env_names(include_aliases=True)),
        }
        return doc

    def describe(self) -> dict[str, Any]:
        """Everything an operator needs to see, without any DSN value."""
        return {
            "schema_version": SCHEMA_VERSION,
            "sources": list(self.sources),
            "warnings": list(self.warnings),
            "producer_templates": list(self.producer_templates),
            "content_agent_templates": list(self.content_agent_templates),
            "producer_agent_ids": self.producer_agent_ids(),
            "starvation_sites": self.starvation_sites(),
            "content_tier_map": {str(k): v for k, v in self.content_tier_map().items()},
            "sites": [self.describe_site(e.name) for e in self._sites],
        }

    def __repr__(self) -> str:
        return (f"SiteRegistry(sites={[e.name for e in self._sites]!r}, "
                f"sources={list(self.sources)!r}, warnings={len(self.warnings)})")


def _build(defaults_doc: Mapping[str, Any], override_doc: Optional[Mapping[str, Any]], *,
           sources: Sequence[str], warnings: Sequence[str]) -> SiteRegistry:
    override: Mapping[str, Any] = override_doc or {}
    templates = override.get("producer_templates")
    if templates is None:
        templates = defaults_doc.get("producer_templates") or []
    content_templates = override.get("content_agent_templates")
    if content_templates is None:
        content_templates = defaults_doc.get("content_agent_templates") or []

    raw_sites: list[dict[str, Any]] = []
    if not override.get("replace_sites"):
        raw_sites = [_merge({}, s) for s in defaults_doc.get("sites") or []]
    built_in = {s["name"] for s in raw_sites}
    position = {s["name"]: i for i, s in enumerate(raw_sites)}
    for patch in override.get("sites") or []:
        name = patch["name"]
        if name in position:
            raw_sites[position[name]] = _merge(raw_sites[position[name]], patch)
        else:
            position[name] = len(raw_sites)
            raw_sites.append(_merge({}, patch))

    # Built-in sites first. A site only the override defines then defaults to
    # the group after the highest built-in group: the dispatcher stops at
    # per-tick caps, so a new site must not take a slot ahead of a built-in
    # producer unless it asks to by setting dispatch_group itself. (With
    # replace_sites there is no built-in site and the default stays 0.)
    entries: list[Optional[SiteEntry]] = [None] * len(raw_sites)
    for i, raw in enumerate(raw_sites):
        if raw["name"] in built_in:
            entries[i] = _resolve_entry(raw, i, templates, content_templates)
    later_group = 1 + max((e.dispatch_group for e in entries if e is not None), default=-1)
    later_group = min(later_group, SITE_FIELDS["dispatch_group"].maximum or later_group)
    for i, raw in enumerate(raw_sites):
        if entries[i] is None:
            entries[i] = _resolve_entry(raw, i, templates, content_templates,
                                        default_group=later_group)
    return SiteRegistry([e for e in entries if e is not None], producer_templates=templates,
                        content_agent_templates=content_templates,
                        sources=sources, warnings=warnings)


# ---------------------------------------------------------------------------
# Loading (defaults file + storage override) and the process cache
# ---------------------------------------------------------------------------

def read_defaults(path: Union[str, os.PathLike, None] = None) -> dict[str, Any]:
    """Read and shape-check the built-in defaults document."""
    p = Path(path) if path else DEFAULTS_PATH
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise SiteRegistryError(f"site registry defaults unreadable at {p}: "
                                f"{exc.strerror or type(exc).__name__}") from None
    except UnicodeDecodeError:
        raise SiteRegistryError(f"site registry defaults unreadable at {p}: "
                                "not UTF-8 text") from None
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SiteRegistryError(f"site registry defaults at {p} are not valid JSON "
                                f"(line {exc.lineno}, column {exc.colno}: {exc.msg})") from None
    except (ValueError, RecursionError) as exc:  # huge integers, absurd nesting
        raise SiteRegistryError(f"site registry defaults at {p} are not valid JSON "
                                f"({type(exc).__name__})") from None
    problems = validate_document(doc, where=str(p))
    if problems:
        raise SiteRegistryError(f"site registry defaults at {p} are invalid: " + "; ".join(problems))
    return doc


def read_override(storage: StorageBackend) -> tuple[Optional[dict[str, Any]], list[str]]:
    """``(document | None, notes)`` for the storage override. Read-only.
    Unreadable content, non-JSON content and JSON that is not an object
    (``null`` included) yield None plus a note; the content is never echoed.
    ``(None, [])`` means there is no override."""
    try:
        raw = storage.read_bytes(CONFIG_KEY)
    except Exception as exc:  # a backend that raises instead of returning None
        return None, [f"{CONFIG_KEY}: storage read failed ({type(exc).__name__}), not applied"]
    if raw is None:
        # The Azure backend returns None for a blob it cannot read, e.g. one
        # moved to the Archive tier by the lifecycle policy. Its properties
        # stay readable, so exists() tells "absent" from "present but
        # unreadable"; the latter must not pass silently as "no override".
        try:
            present = storage.exists(CONFIG_KEY)
        except Exception:
            present = False
        if present:
            return None, [f"{CONFIG_KEY} exists but could not be read (archived blob or "
                          "transient storage error), not applied"]
        return None, []
    try:
        doc = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError:
        return None, [f"{CONFIG_KEY} ignored: not UTF-8 text"]
    except json.JSONDecodeError as exc:
        return None, [f"{CONFIG_KEY} ignored: not valid JSON "
                      f"(line {exc.lineno}, column {exc.colno}: {exc.msg})"]
    except Exception as exc:  # RecursionError (absurd nesting), ValueError (huge integers)
        return None, [f"{CONFIG_KEY} ignored: not valid JSON ({type(exc).__name__})"]
    if not isinstance(doc, dict):
        # JSON null in particular must not pass as "no override": a pipeline
        # such as `jq .missing_key > override.json` writes exactly that.
        return None, [f"{CONFIG_KEY} ignored: not a JSON object"]
    return doc, []


_CACHE_LOCK = threading.Lock()
# key -> (loaded_at, expires, weakref|None, SiteRegistry); times are time.monotonic()
_CACHE: dict[tuple, tuple] = {}


def clear_cache() -> None:
    """Forget every cached registry (tests; after writing an override)."""
    with _CACHE_LOCK:
        _CACHE.clear()


def _cache_ttl() -> float:
    raw = os.environ.get(CACHE_TTL_ENV, "")
    if not raw:
        return DEFAULT_CACHE_TTL_S
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_CACHE_TTL_S


def _cache_get(key: tuple, store: Optional[StorageBackend],
               max_age: float) -> Optional[SiteRegistry]:
    """The cached registry, unless it has outlived the TTL it was stored with
    or is older than this caller's ``max_age``."""
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit is None:
            return None
        loaded_at, expires, ref, registry = hit
        alive = (store is None) if ref is None else (ref() is store)
        now = time.monotonic()
        if now >= expires or not alive:
            _CACHE.pop(key, None)
            return None
        if now - loaded_at >= max_age:
            return None  # too old for this caller; its reload replaces the entry
        return registry


def _cache_put(key: tuple, store: Optional[StorageBackend], registry: SiteRegistry,
               ttl: float) -> None:
    ref = None
    if store is not None:
        try:
            ref = weakref.ref(store)
        except TypeError:  # backend without weakref support: do not cache
            return
    now = time.monotonic()
    with _CACHE_LOCK:
        for k in [k for k, (_loaded, exp, r, _reg) in _CACHE.items()
                  if exp <= now or (r is not None and r() is None)]:
            _CACHE.pop(k, None)
        _CACHE[key] = (now, now + ttl, ref, registry)


def load_registry(storage: Optional[StorageBackend] = None, *,
                  use_storage: bool = True,
                  defaults_path: Union[str, os.PathLike, None] = None,
                  strict: bool = False,
                  max_age_s: Optional[float] = None) -> SiteRegistry:
    """The effective registry: built-in defaults merged with the storage
    override ``config/sites-registry.json`` when one exists.

    storage        backend to read the override from (default: get_storage())
    use_storage    False = built-in defaults only, storage is not touched
    defaults_path  alternative defaults document (default DEFAULTS_PATH)
    strict         raise on an invalid override / unreadable storage instead
                   of falling back to the defaults with a warning
    max_age_s      the oldest cached registry this caller accepts, and the
                   TTL a fresh load is cached with; None =
                   SITES_REGISTRY_CACHE_TTL_S (60 s), 0 = always reload

    Never writes storage. Raises SiteRegistryError when the defaults
    themselves are missing or invalid.
    """
    path = Path(defaults_path) if defaults_path else DEFAULTS_PATH
    notes: list[str] = []
    storage_note = ""
    store: Optional[StorageBackend] = None
    if use_storage:
        store = storage
        if store is None:
            try:
                store = get_storage()
            except (Exception, SystemExit) as exc:  # get_storage() exits on a misconfigured backend
                storage_note = (f"framework storage unavailable ({type(exc).__name__}); "
                                f"{CONFIG_KEY} not read, built-in defaults only")
                if strict:
                    raise SiteRegistryError(storage_note) from None
    ttl = _cache_ttl() if max_age_s is None else max(0.0, float(max_age_s))
    key = (id(store) if store is not None else 0, str(path), bool(strict), bool(use_storage))
    if ttl > 0:
        cached = _cache_get(key, store, ttl)
        if cached is not None:
            return cached  # carries its warnings; logged once, when it was loaded

    if storage_note:
        log.warning("site registry: %s", storage_note)
        notes.append(storage_note)
    defaults_doc = read_defaults(path)
    override_doc = None
    if store is not None:
        override_doc, read_notes = read_override(store)
        if read_notes:
            if strict:
                raise SiteRegistryError("; ".join(read_notes))
            for note in read_notes:
                log.warning("site registry: %s", note)
            notes.extend(read_notes)
    registry = SiteRegistry.from_documents(
        defaults_doc, override_doc,
        defaults_source=f"defaults:{path}",
        override_source=f"storage:{CONFIG_KEY}",
        strict=strict, warnings=notes)
    if ttl > 0:
        _cache_put(key, store, registry, ttl)
    return registry


# ---------------------------------------------------------------------------
# Module-level conveniences (the effective, cached registry)
# ---------------------------------------------------------------------------

def get_site(name: str, *, storage: Optional[StorageBackend] = None) -> Optional[SiteEntry]:
    """The entry for ``name`` (enabled or not), or None."""
    return load_registry(storage).get_site(name)


def list_sites(*, include_disabled: bool = False,
               storage: Optional[StorageBackend] = None) -> list[SiteEntry]:
    """Entries in registry order (enabled only unless asked)."""
    return load_registry(storage).list_sites(include_disabled)


def site_names(*, include_disabled: bool = False,
               storage: Optional[StorageBackend] = None) -> list[str]:
    return load_registry(storage).site_names(include_disabled)


def producers_for(site: str, *, storage: Optional[StorageBackend] = None) -> list[str]:
    """``site``'s producer agent ids. UnknownSiteError for an unknown site."""
    return load_registry(storage).producers_for(site)


def producer_agent_ids(*, include_disabled: bool = False,
                       storage: Optional[StorageBackend] = None) -> list[str]:
    """Every producer across the enabled sites, in dispatcher walk order."""
    return load_registry(storage).producer_agent_ids(include_disabled)


def site_for_agent(agent_id: str, *, storage: Optional[StorageBackend] = None) -> Optional[str]:
    """The site that owns ``agent_id``, or None for framework-wide agents."""
    return load_registry(storage).site_for_agent(agent_id)


def split_agent_id(agent_id: str, *,
                   storage: Optional[StorageBackend] = None) -> tuple[Optional[str], str]:
    return load_registry(storage).split_agent_id(agent_id)


def agent_id_for(site: str, template: str, *, storage: Optional[StorageBackend] = None) -> str:
    return load_registry(storage).agent_id_for(site, template)


def starvation_sites(*, storage: Optional[StorageBackend] = None) -> list[str]:
    return load_registry(storage).starvation_sites()


def content_tier_map(*, include_disabled: bool = False,
                     storage: Optional[StorageBackend] = None) -> dict[int, list[str]]:
    return load_registry(storage).content_tier_map(include_disabled)


def dsn_env_for(site: str, *, storage: Optional[StorageBackend] = None) -> str:
    """NAME of the env var holding ``site``'s DSN."""
    return load_registry(storage).dsn_env_for(site)


def dsn_for(site: str, *, include_aliases: bool = False,
            names: Union[str, Sequence[str], None] = None,
            environ: Optional[Mapping[str, str]] = None,
            storage: Optional[StorageBackend] = None) -> Optional[str]:
    """``site``'s DSN from the environment (never logged); None when unset.
    UnknownSiteError for an unknown site. See :meth:`SiteRegistry.dsn_for`
    for ``include_aliases`` and ``names``."""
    return load_registry(storage).dsn_for(site, include_aliases=include_aliases, names=names,
                                          environ=environ)


def seo_site_yaml_path(site: str, *, storage: Optional[StorageBackend] = None) -> Path:
    return load_registry(storage).seo_site_yaml_path(site)


# ---------------------------------------------------------------------------
# CLI (read-only)
# ---------------------------------------------------------------------------

_DERIVED_FIELDS = ("producers", "content_agents", "seo_site_yaml_path")


def _print_value(value: Any) -> None:
    if value is None:
        return
    if isinstance(value, bool):
        print("true" if value else "false")
    elif isinstance(value, (list, tuple)):
        for item in value:
            print(item)
    elif isinstance(value, Mapping):
        print(json.dumps(_thaw(value), indent=2, sort_keys=True))
    else:
        print(value)


def _cli_storage() -> Optional[StorageBackend]:
    """The framework storage backend, or None when it is unavailable
    (load_registry then reports that as a warning, or raises if strict)."""
    try:
        return get_storage()
    except (Exception, SystemExit):  # get_storage() exits on a misconfigured backend
        return None


def _note_override_status(reg: SiteRegistry, store: Optional[StorageBackend]) -> None:
    """stderr notes for an operator: that no storage override is applied,
    and that a similarly named key exists which the registry never reads (an
    override written under the wrong name would otherwise go unnoticed)."""
    if store is None:
        return
    if (f"storage:{CONFIG_KEY}" not in reg.sources
            and not any(CONFIG_KEY in note for note in reg.warnings)):
        print(f"note: no override at {CONFIG_KEY}; built-in defaults only", file=sys.stderr)
    for key in MISNAMED_CONFIG_KEYS:
        try:
            present = store.exists(key)
        except Exception:
            present = False
        if present:
            print(f"warning: {key} exists, but the site registry reads only {CONFIG_KEY}",
                  file=sys.stderr)


def _cli_validate(args: argparse.Namespace, defaults: Optional[str],
                  store: Optional[StorageBackend]) -> int:
    """``validate``: a strict load of the effective registry, or of the
    defaults plus a candidate override file. An override that drops or
    disables a built-in site is refused unless --allow-drop is given."""
    defaults_doc = read_defaults(defaults)
    defaults_source = f"defaults:{defaults or DEFAULTS_PATH}"
    base = SiteRegistry.from_documents(defaults_doc, defaults_source=defaults_source)
    if args.override:
        try:
            doc = json.loads(Path(args.override).read_text(encoding="utf-8"))
        except OSError as exc:
            print(f"invalid: cannot read {args.override}: "
                  f"{exc.strerror or type(exc).__name__}", file=sys.stderr)
            return 2
        except (ValueError, RecursionError) as exc:  # JSONDecodeError, UnicodeDecodeError, ...
            print(f"invalid: {args.override} is not valid JSON ({type(exc).__name__})",
                  file=sys.stderr)
            return 2
        if not isinstance(doc, dict):  # JSON null is not "no override"
            print(f"invalid: {args.override} is not a JSON object", file=sys.stderr)
            return 2
        reg = SiteRegistry.from_documents(defaults_doc, doc, defaults_source=defaults_source,
                                          override_source=f"file:{args.override}", strict=True)
    else:
        reg = load_registry(store, use_storage=not args.no_storage, defaults_path=defaults,
                            strict=True, max_age_s=0)
        _note_override_status(reg, store)
    dropped = [n for n in base.site_names(include_disabled=True) if reg.get_site(n) is None]
    disabled = [n for n in base.site_names()
                if (entry := reg.get_site(n)) is not None and not entry.enabled]
    if dropped or disabled:
        what = "; ".join(part for part in (
            f"drops built-in site(s) {', '.join(dropped)}" if dropped else "",
            f"disables built-in site(s) {', '.join(disabled)}" if disabled else "") if part)
        if not args.allow_drop:
            print(f"refused: the override {what}; pass --allow-drop if that is intended",
                  file=sys.stderr)
            return 2
        print(f"note: the override {what} (--allow-drop given)", file=sys.stderr)
    print(f"ok: {len(reg.list_sites(True))} site(s): "
          + ", ".join(reg.site_names(include_disabled=True)))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python3 -m framework.core.site_registry",
        description="Read-only view of the site registry: the built-in defaults merged with "
                    f"framework storage {CONFIG_KEY}. Never writes storage, never prints a "
                    "DSN value.")
    ap.add_argument("--no-storage", action="store_true",
                    help="built-in defaults only; do not read framework storage")
    ap.add_argument("--defaults", default="", metavar="FILE",
                    help="defaults document (default: shared/sites/registry.default.json)")
    sub = ap.add_subparsers(dest="cmd", metavar="COMMAND")
    sub.required = True
    p = sub.add_parser("list", help="site names in registry order")
    p.add_argument("--all", action="store_true", help="include disabled sites")
    p.add_argument("--json", action="store_true", help="print a JSON array")
    p = sub.add_parser("show", help="one resolved site entry, as JSON")
    p.add_argument("site")
    p = sub.add_parser("get", help="one field of a site (lists print one item per line)")
    p.add_argument("site")
    p.add_argument("field", help="a site field, or one of: " + ", ".join(_DERIVED_FIELDS))
    p = sub.add_parser("producers", help="producer agent ids (all sites, in dispatcher order)")
    p.add_argument("site", nargs="?", default="")
    p.add_argument("--all", action="store_true", help="include disabled sites")
    p = sub.add_parser("site-for-agent", help="the site that owns an agent id")
    p.add_argument("agent_id")
    p = sub.add_parser("dsn-env", help="env var NAME(s) for a site's DSN (never the value)")
    p.add_argument("site")
    p.add_argument("--aliases", action="store_true", help="also list the alias names, in order")
    p.add_argument("--resolved", action="store_true",
                   help="print only the first name that is set in this environment")
    p = sub.add_parser("validate", help="strict check of the effective registry, or of a "
                                        "candidate override file (nothing is written)")
    p.add_argument("--override", default="", metavar="FILE",
                   help="a local JSON file in the storage override's shape, checked instead "
                        "of the storage copy")
    p.add_argument("--allow-drop", action="store_true",
                   help="accept an override that drops or disables a built-in site "
                        "(refused by default)")
    sub.add_parser("dump", help="the resolved registry with sources and warnings, as JSON")
    args = ap.parse_args(argv)

    defaults = args.defaults or None
    try:
        # validate --override checks a local file against the defaults and
        # never needs storage, so it works where no backend is configured.
        offline = args.no_storage or (args.cmd == "validate" and bool(args.override))
        store = None if offline else _cli_storage()
        if args.cmd == "validate":
            return _cli_validate(args, defaults, store)

        reg = load_registry(store, use_storage=not args.no_storage, defaults_path=defaults,
                            max_age_s=0)
        for note in reg.warnings:
            print(f"warning: {note}", file=sys.stderr)
        if args.cmd == "dump":
            _note_override_status(reg, store)

        if args.cmd == "list":
            names = reg.site_names(include_disabled=args.all)
            if args.json:
                print(json.dumps(names))
            else:
                _print_value(names)
        elif args.cmd == "show":
            print(json.dumps(reg.describe_site(args.site), indent=2))
        elif args.cmd == "get":
            entry = reg.require_site(args.site)
            if args.field in SITE_FIELDS:
                _print_value(getattr(entry, args.field))
            elif args.field in _DERIVED_FIELDS:
                value = getattr(entry, args.field)
                _print_value(str(value) if isinstance(value, Path) else value)
            else:
                print(f"unknown field {_safe(args.field)}; fields: "
                      + ", ".join(list(SITE_FIELDS) + list(_DERIVED_FIELDS)), file=sys.stderr)
                return 2
        elif args.cmd == "producers":
            ids = reg.producers_for(args.site) if args.site else \
                reg.producer_agent_ids(include_disabled=args.all)
            _print_value(ids)
        elif args.cmd == "site-for-agent":
            site = reg.site_for_agent(args.agent_id)
            if site is None:
                return 1
            print(site)
        elif args.cmd == "dsn-env":
            if args.resolved:
                info = reg.describe_dsn(args.site, include_aliases=args.aliases)
                if info["resolved_env"] is None:
                    print(f"none of {', '.join(info['candidates'])} is set", file=sys.stderr)
                    return 1
                print(info["resolved_env"])
            else:
                _print_value(list(reg.require_site(args.site).dsn_env_names(args.aliases)))
        elif args.cmd == "dump":
            print(json.dumps(reg.describe(), indent=2))
        return 0
    except UnknownSiteError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except SiteRegistryError as exc:
        print(f"invalid: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
