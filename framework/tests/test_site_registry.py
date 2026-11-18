"""framework.core.site_registry - the site registry primitive (F1a).

Three groups of tests:

* EQUIVALENCE. The built-in defaults must reproduce what today's consumers
  enumerate in code. Expected values are read from the consumers' own source
  (AST / the shell text / calling the consumer), never typed here, so this
  file names no site. A check fails while the literal exists and disagrees.
  When its extractor finds nothing, it skips only on a positive migration
  signal (the consumer's source mentions site_registry: W1 switched it, and
  W1 brings its own byte-identical tests); otherwise it fails, so a rewrite
  of a consumer cannot quietly switch its check off.
* STORAGE OVERRIDE, against LocalFilesystemStorage in a tmp dir: a third site
  described purely by config, patches, disabling, replace_sites, invalid
  overrides (including shared resources and an empty result) falling back to
  the defaults, never writing storage.
* API, schema agreement, secrets never echoed, copying/pickling, the cache
  and the CLI.

No network, no database, no LLM.
"""
from __future__ import annotations

import ast
import copy
import dataclasses
import inspect
import json
import logging
import os
import re
from pathlib import Path, PurePosixPath
from typing import Any, Optional

import pytest

from framework.core import site_registry
from framework.core.site_registry import SiteEntry, SiteRegistry, SiteRegistryError, UnknownSiteError
from framework.core.storage import LocalFilesystemStorage, reset_default_backend

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DOC = json.loads(site_registry.DEFAULTS_PATH.read_text(encoding="utf-8"))
SCHEMA = json.loads(site_registry.SCHEMA_PATH.read_text(encoding="utf-8"))

# A fictional third site (hyphenated on purpose: exercises the env-var and
# prefix rules). Only these four fields are needed; everything else derives.
THIRD = {
    "name": "gadget-den",
    "domain": "gadget-den.example",
    "repo_path": "/srv/sites/gadget-den",
    "branch": "main",
}
SECRET = "postgresql://svc:S3NTINEL-PASSWORD@db.internal:5432/app"


@pytest.fixture(autouse=True)
def _fresh_cache():
    site_registry.clear_cache()
    yield
    site_registry.clear_cache()


@pytest.fixture
def store(storage):
    """conftest's LocalFilesystemStorage (STORAGE_BACKEND=local pointed at the
    same tmp dir, so get_storage() sees it too); forget the process-wide
    backend afterwards so later tests do not inherit a dead tmp dir."""
    yield storage
    reset_default_backend()


@pytest.fixture(scope="module")
def base() -> SiteRegistry:
    """The built-in defaults alone."""
    return site_registry.load_registry(use_storage=False, max_age_s=0)


def _write_override(store, doc: Any) -> None:
    if isinstance(doc, bytes):
        store.write_bytes(site_registry.CONFIG_KEY, doc)
    else:
        store.write_json(site_registry.CONFIG_KEY, doc)


def _load(store, **kw) -> SiteRegistry:
    kw.setdefault("max_age_s", 0)
    return site_registry.load_registry(store, **kw)


# -- source-reading helpers -----------------------------------------------------

def _source(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _gone(rel: str, what: str) -> None:
    """An extractor found nothing in ``rel``. Skip when the consumer now reads
    the registry (W1 owns its equivalence from then on); otherwise fail: the
    literal may still be there in a shape this test no longer recognises."""
    if "site_registry" in _source(rel):
        pytest.skip(f"consumer migrated: {rel} ({what} replaced by site_registry)")
    pytest.fail(f"{rel}: {what} not found, and the file does not use site_registry: the "
                "consumer changed shape, so update this test's extractor")


def _tree(rel: str) -> ast.Module:
    return ast.parse(_source(rel), filename=rel)


def _assigned_values(tree: ast.AST, name: str) -> list[ast.AST]:
    """Every value assigned to ``name`` anywhere in the tree, in source order."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
                out.append(node.value)
        elif (isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
              and node.target.id == name and node.value is not None):
            out.append(node.value)
    return sorted(out, key=lambda n: (n.lineno, n.col_offset))


def _str_seq(node: ast.AST) -> Optional[list[str]]:
    if (isinstance(node, (ast.Tuple, ast.List)) and node.elts
            and all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in node.elts)):
        return [e.value for e in node.elts]
    return None


def _str_keyed_dict(node: ast.AST) -> Optional[dict[str, ast.AST]]:
    if not isinstance(node, ast.Dict) or not node.keys:
        return None
    out = {}
    for k, v in zip(node.keys, node.values):
        if not (isinstance(k, ast.Constant) and isinstance(k.value, str)):
            return None
        out[k.value] = v
    return out


def _env_get_names(node: ast.AST) -> list[str]:
    """String arguments of every ``<x>.get("NAME")`` call under ``node``, in
    source order: the env var names a DSN expression tries."""
    calls = [n for n in ast.walk(node)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "get" and n.args
             and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str)]
    calls.sort(key=lambda c: (c.lineno, c.col_offset))
    return [c.args[0].value for c in calls]


def _load_function(rel: str, func_name: str):
    """Compile one self-contained module-level function out of a consumer,
    without importing (and so without running) the consumer module."""
    fn = next((n for n in _tree(rel).body
               if isinstance(n, ast.FunctionDef) and n.name == func_name), None)
    if fn is None:
        return None
    fn = copy.deepcopy(fn)
    fn.returns = None
    for a in fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs:
        a.annotation = None
    mod = ast.Module(body=[fn], type_ignores=[])
    ast.fix_missing_locations(mod)
    namespace: dict[str, Any] = {"__builtins__": {}}
    exec(compile(mod, rel, "exec"), namespace)
    return namespace[func_name]


# =============================================================================
# EQUIVALENCE: the defaults reproduce what the consumers enumerate today
# =============================================================================

_DISPATCHER = "agents/backlog-dispatcher-agent/agent.py"


def test_producer_ids_equal_the_backlog_dispatcher_tuple(base):
    values = _assigned_values(_tree(_DISPATCHER), "PRODUCER_AGENT_IDS")
    literal = _str_seq(values[0]) if values else None
    if literal is None:
        _gone(_DISPATCHER, "a literal PRODUCER_AGENT_IDS tuple")
    # Same ids AND the same walk order (the dispatcher stops at a per-tick cap,
    # so order decides which producer is served first).
    assert base.producer_agent_ids() == literal
    assert len(set(literal)) == len(literal)
    for aid in literal:
        site = base.site_for_agent(aid)
        assert site is not None and aid in base.producers_for(site)


def test_site_for_agent_equals_the_dispatcher_function(base):
    fn = _load_function(_DISPATCHER, "_site_from_agent_id")
    if fn is None:
        _gone(_DISPATCHER, "the module-level function _site_from_agent_id")
    corpus = list(base.producer_agent_ids())
    for entry in base.list_sites():
        corpus += [entry.agent_id("*-x-agent"), entry.name, entry.name + "-",
                   entry.name + "x-agent", "x-" + entry.name + "-agent",
                   entry.agent_id("*-site-goals-tracker")]
    corpus += ["agent-doctor", "implementer", "backlog-dispatcher-agent",
               "reusable-agents-digest", "seo-implementer", ""]
    try:
        expected = {aid: fn(aid) for aid in corpus}
    except NameError:
        _gone(_DISPATCHER, "a self-contained _site_from_agent_id")
    for aid, want in expected.items():
        assert (base.site_for_agent(aid) or "") == want, aid


def test_local_id_equals_the_dispatcher_on_disk_fallback(base):
    """_producer_allows_auto_implement looks for
    <root>/<site>/agents/<aid minus 'site-'>/site.yaml."""
    src = _source(_DISPATCHER)
    m = re.search(r'f"(?P<root>/[^"{]+)/\{site\}/agents/\{local_id\}/site\.yaml"', src)
    if not m or "local_id = aid[len(site) + 1:]" not in src:
        _gone(_DISPATCHER, "the on-disk <root>/<site>/agents/<local_id>/site.yaml fallback")
    for entry in base.list_sites():
        assert entry.repo_path == f"{m['root']}/{entry.name}"
        for aid in entry.producers:
            assert base.split_agent_id(aid) == (entry.name, aid[len(entry.name) + 1:])


_PRIORITY = "framework/core/priority.py"


def test_starvation_sites_equal_priority():
    from framework.core import priority
    literal = getattr(priority, "_STARVATION_SITES", None)
    if literal is None:
        _gone(_PRIORITY, "_STARVATION_SITES")
    reg = site_registry.load_registry(use_storage=False, max_age_s=0)
    assert tuple(reg.starvation_sites()) == tuple(literal)
    default = inspect.signature(priority.site_starvation_boost).parameters["sites"].default
    assert tuple(reg.starvation_sites()) == tuple(default)


def test_dsn_env_is_the_env_var_priority_reads(base, monkeypatch):
    """Call priority's reader with a recording environment: it asks for one
    DSN env var name and, finding none, returns before touching any database.
    (Only DSN-like names count, so the check still holds once the reader
    goes through the registry, which also reads its own settings.)"""
    from framework.core import priority
    if not hasattr(priority, "_read_articles_7d"):
        _gone(_PRIORITY, "_read_articles_7d")

    class _Recorder(dict):
        def __init__(self):
            super().__init__()
            self.asked: list[str] = []

        def get(self, key, default=None):
            self.asked.append(key)
            return default

    def dsn_like(key: str) -> bool:
        return key.startswith("DATABASE_URL") or key.endswith(("_DSN", "_DATABASE_URL"))

    for name in base.starvation_sites():
        rec = _Recorder()
        with monkeypatch.context() as m:
            m.setattr(os, "environ", rec)
            assert priority._read_articles_7d(name) is None
        assert [k for k in rec.asked if dsn_like(k)] == [base.dsn_env_for(name)]


def test_content_tiers_equal_the_priority_defaults(base):
    from framework.core import priority
    tiers = priority.DEFAULT_CONFIG["tiers"]
    cmap = base.content_tier_map()
    assert cmap, "the built-in defaults assign content tiers"
    for tier, agents in cmap.items():
        assert list(tiers[str(tier)]["agents"]) == agents
    for entry in base.list_sites():
        assert entry.content_tier is not None
        assert tiers[str(entry.content_tier)]["label"] == f"{entry.label} content production"
        for aid in entry.content_agents:
            assert priority.tier_for_agent(aid, manifest={},
                                           config=priority.DEFAULT_CONFIG) == entry.content_tier


def test_implementation_queue_treats_registry_sites_as_site_segments(base, store):
    """queue_recs' subject-tag heuristic skips site-name segments: every
    registry site must be skipped, so the tag is the producer's own word."""
    from framework.core import implementation_queue as iq
    run_ts = "20260101T000000Z"
    for entry in base.list_sites():
        for aid in entry.producers:
            rid = iq.queue_recs(source_agent=aid, site=entry.name, run_ts=run_ts,
                                rec_ids=["rec-001"], storage=store)
            tag = rid[len(f"r-{run_ts}-"):].split("-", 1)[0]
            _, local = base.split_agent_id(aid)
            assert tag == local.split("-", 1)[0] and tag != entry.name, aid
    # ...and it skips no OTHER site names: the literal holds exactly the registry's.
    rel = "framework/core/implementation_queue.py"
    found = []
    for node in ast.walk(_tree(rel)):
        if isinstance(node, ast.Compare) and any(isinstance(op, ast.NotIn) for op in node.ops):
            for comp in node.comparators:
                seq = _str_seq(comp)
                if seq and "agent" in seq:
                    found.append(seq)
    if not found:
        _gone(rel, "the literal segment list in the subject-tag heuristic")
    assert set(found[0]) - {"agent", "opportunity"} == set(base.site_names())


_RUN_SH = "agents/implementer/run.sh"


def test_seo_site_yaml_is_the_implementer_fallback(base):
    src = _source(_RUN_SH)
    m = re.search(r'CANDIDATE="\$REPO_ROOT/(?P<head>[^"$]*)\$RESPONDER_SITE(?P<tail>[^"]*)"', src)
    if not m:
        _gone(_RUN_SH, 'the CANDIDATE="$REPO_ROOT/...$RESPONDER_SITE..." site.yaml fallback')
    for entry in base.list_sites():
        expected = ROOT / f"{m['head']}{entry.name}{m['tail']}"
        assert entry.seo_site_yaml_path == expected
        assert site_registry.default_seo_site_yaml(entry.name) == entry.seo_site_yaml
        # lexists: a symlink into a site repo may dangle on a host without that repo
        assert os.path.lexists(expected), expected


@pytest.mark.parametrize("rel", ["agents/implementer/agent.py",
                                 "agents/responder-agent/responder.py"])
def test_seo_site_yaml_is_the_python_side_fallback(base, rel):
    """The implementer's Python side and the responder use the same fallback."""
    if not re.search(r'/\s*"examples"\s*/\s*"sites"\s*/\s*f"\{site\}\.yaml"', _source(rel)):
        _gone(rel, 'the / "examples" / "sites" / f"{site}.yaml" fallback')
    for entry in base.list_sites():
        assert entry.seo_site_yaml_path == ROOT / "examples" / "sites" / f"{entry.name}.yaml"


def test_dsn_env_is_the_implementer_rule(base):
    src = _source(_RUN_SH)
    upper = re.search(r"""SITE_UPPER="\$\(echo "\$RESPONDER_SITE" \| tr '\[:lower:\]-' '\[:upper:\]_'\)\"""", src)
    var = re.search(r'PER_SITE_VAR="(?P<prefix>[A-Z_]+)\$\{SITE_UPPER\}"', src)
    if not (upper and var):
        _gone(_RUN_SH, "the DATABASE_URL_<SITE> derivation (SITE_UPPER / PER_SITE_VAR)")
    for entry in base.list_sites():
        assert entry.dsn_env == var["prefix"] + entry.name.upper().replace("-", "_")
        assert entry.dsn_env == site_registry.default_dsn_env(entry.name)


def test_display_fields_equal_the_goals_tracker(base):
    tree = _tree("agents/goals-tracker/agent.py")
    trackers = None
    for value in _assigned_values(tree, "SITE_TRACKERS"):
        try:
            lit = ast.literal_eval(value)
        except ValueError:
            continue
        if isinstance(lit, dict) and lit and all(isinstance(v, dict) and "accent" in v
                                                 for v in lit.values()):
            trackers = lit
            break
    if trackers is None:
        _gone("agents/goals-tracker/agent.py", "a literal SITE_TRACKERS map")
    expected = {
        e.agent_id("*-site-goals-tracker"): {"label": e.label, "accent": e.accent,
                                             "kpi_goal_id": e.kpi_goal_id, "kpi_label": e.kpi_label}
        for e in base.list_sites()
    }
    assert trackers == expected
    # Every {site: "Label"} literal in the goals-tracker agrees on labels.
    names = set(base.site_names())
    for node in ast.walk(tree):
        d = _str_keyed_dict(node)
        if d and names <= set(d) and all(isinstance(v, ast.Constant) and isinstance(v.value, str)
                                         for v in d.values()):
            for name in names:
                assert d[name].value == base.get_site(name).label


_DSN_MAPS = (
    ("agents/implementer/reconcile-shipped.py", "DSN_BY_SITE"),
    ("agents/implementer/azure-shipped-backfill.py", "DSN_BY_SITE"),
    ("agents/catalog-audit-shipped-backfill/agent.py", "DSN_BY_SITE"),
    ("agents/agent-metrics-collector/agent.py", "DB_DSN_MAP"),
)


@pytest.mark.parametrize("rel,var", _DSN_MAPS, ids=[rel for rel, _ in _DSN_MAPS])
def test_dsn_env_names_equal_each_dsn_map(base, rel, var):
    """Each DSN map outside priority/run.sh reads the registry's dsn_env or
    its aliases, and dsn_for(names=...) reproduces the exact chain it tries,
    in its order, with nothing added (what W1 switches it to)."""
    checked = 0
    for value in _assigned_values(_tree(rel), var):
        mapping = _str_keyed_dict(value)
        if not mapping:
            continue
        for site, node in mapping.items():
            entry = base.get_site(site)
            assert entry is not None, (rel, site)
            names = list(entry.dsn_env_names(include_aliases=True))
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                chain = [node.value]  # the map holds the env var name itself
                assert chain[0] in names, (rel, site)
            else:
                chain = _env_get_names(node)  # os.environ.get(A) or os.environ.get(B) ...
                assert chain and names[:len(chain)] == chain, (rel, site, chain)
            assert base.describe_dsn(site, names=chain, environ={})["candidates"] == chain
            for i, name in enumerate(chain):
                env = {n: f"dsn-{n}" for n in names if n not in chain[:i]}
                assert base.dsn_for(site, names=chain, environ=env) == f"dsn-{name}", (rel, name)
            assert base.dsn_for(site, names=chain,
                                environ={n: "x" for n in names if n not in chain}) is None
            checked += 1
    if not checked:
        _gone(rel, f"a literal {var} map")


def test_digest_site_queries_use_dsn_env_and_domain(base):
    rel = "agents/digest-rollup-agent/agent.py"
    checked = 0
    for value in _assigned_values(_tree(rel), "SITE_QUERIES"):
        try:
            queries = ast.literal_eval(value)
        except (ValueError, TypeError, SyntaxError):
            continue
        if not isinstance(queries, dict):
            continue
        for site, cfg in queries.items():
            entry = base.get_site(site)
            assert entry is not None, site
            assert cfg["env"] == entry.dsn_env and cfg["label"] == entry.domain
            checked += 1
    if not checked:
        _gone(rel, "a literal SITE_QUERIES map")


@pytest.mark.parametrize("rel", ["agents/indexnow-submitter/sites.json",
                                 "agents/gsc-coverage-auditor/sites.json"])
def test_sites_json_hosts_and_dsn_env(base, rel):
    sites = json.loads(_source(rel)).get("sites") or []
    assert sites, f"{rel} lists no sites any more: update this test"
    for site in sites:
        entry = base.get_site(site["name"])
        assert entry is not None, (rel, site["name"])
        assert site["host"] == entry.domain
        assert site["databaseUrlEnv"] in entry.dsn_env_names(include_aliases=True)
        env = {site["databaseUrlEnv"]: "dsn"}
        assert base.dsn_for(entry.name, names=[site["databaseUrlEnv"]], environ=env) == "dsn"


def test_identity_fields_equal_each_example_site_yaml(base):
    yaml = pytest.importorskip("yaml")
    checked = 0
    for entry in base.list_sites():
        try:
            cfg = yaml.safe_load(entry.seo_site_yaml_path.read_text(encoding="utf-8")) or {}
        except OSError:
            continue  # dangling symlink: that site's repo is not on this host
        site, impl = cfg.get("site") or {}, cfg.get("implementer") or {}
        assert site.get("id") == entry.name
        assert site.get("domain") == entry.domain
        assert site.get("label") == entry.label
        assert impl.get("repo_path") == entry.repo_path
        assert impl.get("branch") == entry.branch
        checked += 1
    if not checked:
        pytest.skip("no example site.yaml is readable on this host")


def test_module_schema_and_tests_name_no_site():
    """Site values live in the registry data file only (CLAUDE.md: no
    site-name literals in framework code)."""
    names = [s["name"] for s in DEFAULT_DOC["sites"]]
    assert names
    for rel in ("framework/core/site_registry.py", "framework/tests/test_site_registry.py",
                "shared/schemas/sites-registry.schema.json"):
        text = _source(rel).lower()
        for name in names:
            assert name not in text, f"{rel} names a site; keep site values in the registry data"


# =============================================================================
# Defaults document and schema
# =============================================================================

def test_defaults_document_is_valid(base):
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.Draft7Validator.check_schema(SCHEMA)
    jsonschema.Draft7Validator(SCHEMA).validate(DEFAULT_DOC)
    assert site_registry.validate_document(DEFAULT_DOC) == []
    assert base.site_names() == [s["name"] for s in DEFAULT_DOC["sites"]]
    assert base.sources == (f"defaults:{site_registry.DEFAULTS_PATH}",)
    assert base.warnings == ()


def test_the_api_image_ships_the_defaults():
    """The dashboard API imports framework.core modules, and load_registry()
    raises when the defaults are missing, so the API image must contain the
    defaults file: it lives under a directory framework/api/Dockerfile
    copies (the image's WORKDIR /app stands for the repo root)."""
    top = site_registry.DEFAULTS_PATH.relative_to(ROOT).parts[0]
    dockerfile = _source("framework/api/Dockerfile")
    assert re.search(rf"^COPY\s+{re.escape(top)}/?\s+/app/{re.escape(top)}/?\s*$", dockerfile,
                     re.MULTILINE), f"framework/api/Dockerfile does not copy {top}/"
    assert re.search(r"^WORKDIR\s+/app\s*$", dockerfile, re.MULTILINE)


def test_resolved_entries_match_the_schema_site_definition(base):
    jsonschema = pytest.importorskip("jsonschema")
    site_schema = {"$ref": "#/definitions/site", "definitions": SCHEMA["definitions"]}
    validator = jsonschema.Draft7Validator(site_schema)
    for entry in base.list_sites(include_disabled=True):
        validator.validate(entry.to_dict())
    jsonschema.Draft7Validator(SCHEMA).validate(base.to_dict())


def _deref(node: dict) -> dict:
    while "$ref" in node:
        node = SCHEMA["definitions"][node["$ref"].rsplit("/", 1)[-1]]
    return node


def _leaf(prop: dict) -> tuple[dict, bool]:
    """(the non-null alternative, dereferenced; whether null is allowed)."""
    if "anyOf" in prop:
        alts = [a for a in prop["anyOf"] if a.get("type") != "null"]
        assert len(alts) == 1
        return _deref(alts[0]), len(alts) < len(prop["anyOf"])
    node = _deref(prop)
    types = node.get("type")
    if isinstance(types, list):
        return dict(node, type=[t for t in types if t != "null"][0]), "null" in types
    return node, False


def test_schema_and_runtime_field_table_agree():
    fields = site_registry.SITE_FIELDS
    patch = SCHEMA["definitions"]["site_patch"]
    resolved = SCHEMA["definitions"]["site"]
    assert list(patch["properties"]) == list(fields)
    assert list(resolved["properties"]) == list(fields)
    assert resolved["required"] == list(fields)
    assert patch["required"] == ["name"]
    assert set(SCHEMA["properties"]) == set(site_registry.DOCUMENT_KEYS)
    kinds = {"str": "string", "bool": "boolean", "int": "integer", "list": "array",
             "object": "object"}
    for key, spec in fields.items():
        node, nullable = _leaf(resolved["properties"][key])
        assert node["type"] == kinds[spec.kind], key
        assert nullable == spec.nullable, key
        if spec.kind == "str":
            assert node.get("pattern") == spec.pattern, key
            assert node.get("maxLength") == spec.max_len, key
        if spec.kind == "int":
            assert (node.get("minimum"), node.get("maximum")) == (spec.minimum, spec.maximum), key
        if spec.kind == "list":
            items = _deref(node["items"])
            assert node.get("uniqueItems") is True, key
            assert items.get("pattern") == spec.item_pattern, key
            assert items.get("maxLength") == spec.item_max_len, key
        if key != "name":  # every patch field may be null (= derived default)
            assert _leaf(patch["properties"][key])[1] is True, key


_OK = dict(THIRD)
_DOC_CASES = [
    ("minimal new site", {"sites": [dict(_OK)]}, True),
    ("full new site", {"schema_version": "1", "producer_templates": ["*-seo-opportunity-agent"],
                       "content_agent_templates": [], "sites": [dict(
                           _OK, label="Gadget Den", seo_site_yaml="/srv/sites/gadget-den/site.yml",
                           dsn_env="GADGET_DSN", dsn_env_aliases=["GADGET_DATABASE_URL"],
                           agent_prefix="gden", accent="#0af", kpi_goal_id="goal-amazon-clicks-30d",
                           kpi_label="Clicks", starvation=False, content_tier=4,
                           producer_templates=[], producer_extras=["*-deal-watch", "gizmo-feed-agent"],
                           content_agent_templates=["*-article-proposal-agent"], dispatch_group=1,
                           enabled=False, extra={"any": ["thing", 1]})]}, True),
    ("null patch fields", {"sites": [{"name": "gadget-den", "accent": None, "starvation": None,
                                      "extra": None, "producer_templates": None}]}, True),
    ("comment keys", {"_doc": ["x"], "sites": [dict(_OK, _why="y")]}, True),
    ("integer-valued float tier", {"sites": [dict(_OK, content_tier=4.0)]}, True),
    ("replace_sites", {"replace_sites": True, "sites": [dict(_OK)]}, True),
    ("unknown top-level key", {"sitez": []}, False),
    ("unknown site key", {"sites": [dict(_OK, starvaton=True)]}, False),
    ("missing name", {"sites": [{"domain": "gadget-den.example"}]}, False),
    ("upper-case name", {"sites": [dict(_OK, name="GadgetDen")]}, False),
    ("name with double hyphen", {"sites": [dict(_OK, name="gadget--den")]}, False),
    ("domain with scheme", {"sites": [dict(_OK, domain="https://gadget-den.example")]}, False),
    ("domain with path", {"sites": [dict(_OK, domain="gadget-den.example/x")]}, False),
    ("relative repo_path", {"sites": [dict(_OK, repo_path="srv/sites/gadget-den")]}, False),
    ("repo_path trailing slash", {"sites": [dict(_OK, repo_path="/srv/sites/gadget-den/")]}, False),
    ("branch with ..", {"sites": [dict(_OK, branch="feature..x")]}, False),
    ("branch ending in /", {"sites": [dict(_OK, branch="feature/")]}, False),
    ("seo_site_yaml not yaml", {"sites": [dict(_OK, seo_site_yaml="site.json")]}, False),
    ("connection string in dsn_env", {"sites": [dict(_OK, dsn_env=SECRET)]}, False),
    ("lower-case dsn_env", {"sites": [dict(_OK, dsn_env="database_url_x")]}, False),
    ("duplicate alias", {"sites": [dict(_OK, dsn_env_aliases=["A_DSN", "A_DSN"])]}, False),
    ("accent not hex", {"sites": [dict(_OK, accent="orange")]}, False),
    ("kpi_goal_id without goal-", {"sites": [dict(_OK, kpi_goal_id="amazon-clicks")]}, False),
    ("tier 0", {"sites": [dict(_OK, content_tier=0)]}, False),
    ("tier true", {"sites": [dict(_OK, content_tier=True)]}, False),
    ("tier 2.5", {"sites": [dict(_OK, content_tier=2.5)]}, False),
    ("negative group", {"sites": [dict(_OK, dispatch_group=-1)]}, False),
    ("starvation string", {"sites": [dict(_OK, starvation="yes")]}, False),
    ("template without star", {"producer_templates": ["seo-opportunity-agent"]}, False),
    ("duplicate template", {"producer_templates": ["*-a-agent", "*-a-agent"]}, False),
    ("null top-level templates", {"producer_templates": None}, False),
    ("extra not an object", {"sites": [dict(_OK, extra=[])]}, False),
    ("bad extra ref", {"sites": [dict(_OK, producer_extras=["Gizmo Feed"])]}, False),
    ("schema_version 2", {"schema_version": "2"}, False),
    ("replace_sites not bool", {"replace_sites": "yes"}, False),
    ("sites not a list", {"sites": {"gadget-den": {}}}, False),
    ("document not an object", [], False),
]


@pytest.mark.parametrize("label,doc,valid", _DOC_CASES, ids=[c[0] for c in _DOC_CASES])
def test_schema_and_runtime_validator_agree(label, doc, valid):
    jsonschema = pytest.importorskip("jsonschema")
    schema_ok = jsonschema.Draft7Validator(SCHEMA).is_valid(doc)
    runtime_problems = site_registry.validate_document(doc)
    assert schema_ok is valid, label
    assert (runtime_problems == []) is valid, (label, runtime_problems)
    assert all("S3NTINEL" not in p for p in runtime_problems)


def test_duplicate_site_names_in_one_document_are_rejected():
    """(JSON Schema cannot express unique-by-key; the runtime check does.)"""
    problems = site_registry.validate_document({"sites": [dict(THIRD), dict(THIRD)]})
    assert any("duplicate site name" in p for p in problems)


# =============================================================================
# Storage override (LocalFilesystemStorage)
# =============================================================================

def test_no_override_means_the_defaults(base, store):
    reg = _load(store)
    assert reg.to_dict() == base.to_dict()
    assert reg.sources == base.sources and reg.warnings == ()


def test_a_third_site_is_described_purely_by_config(base, store, monkeypatch):
    _write_override(store, {"schema_version": "1", "sites": [dict(THIRD)]})
    reg = _load(store)
    assert reg.sources == base.sources + (f"storage:{site_registry.CONFIG_KEY}",)
    assert reg.warnings == ()
    assert reg.site_names() == base.site_names() + ["gadget-den"]
    e = reg.get_site("gadget-den")
    # derived defaults
    assert e.agent_prefix == "gadget-den"
    assert e.dsn_env == "DATABASE_URL_GADGET_DEN" and e.dsn_env_aliases == ()
    assert e.seo_site_yaml == "examples/sites/gadget-den.yaml"
    assert e.seo_site_yaml_path == ROOT / "examples" / "sites" / "gadget-den.yaml"
    assert e.label == "Gadget-Den"
    # not boosted, no content tier, and walked after every built-in producer
    assert (e.starvation, e.content_tier, e.enabled) == (False, None, True)
    assert e.dispatch_group == 1 + max(s.dispatch_group for s in base.list_sites(True))
    assert e.accent is None and e.kpi_goal_id is None and dict(e.extra) == {}
    # producers come from the registry-wide templates
    assert reg.producers_for("gadget-den") == [
        "gadget-den" + t[1:] for t in base.producer_templates]
    n = len(base.producer_agent_ids())
    assert reg.producer_agent_ids()[:n] == base.producer_agent_ids()
    assert reg.producer_agent_ids()[n:] == reg.producers_for("gadget-den")
    assert reg.site_for_agent("gadget-den-seo-opportunity-agent") == "gadget-den"
    assert reg.split_agent_id("gadget-den-seo-opportunity-agent") == (
        "gadget-den", "seo-opportunity-agent")
    assert reg.agent_id_for("gadget-den", "*-site-goals-tracker") == "gadget-den-site-goals-tracker"
    # the incumbents are unchanged: same entries, same relative producer order,
    # same starvation set, same content tiers
    for name in base.site_names():
        assert reg.get_site(name) == base.get_site(name)
    incumbent = set(base.producer_agent_ids())
    assert [a for a in reg.producer_agent_ids() if a in incumbent] == base.producer_agent_ids()
    assert set(reg.producer_agent_ids()) == incumbent | set(reg.producers_for("gadget-den"))
    assert reg.starvation_sites() == base.starvation_sites()
    assert reg.content_tier_map() == base.content_tier_map()
    # DSN through the derived env var name
    monkeypatch.setenv("DATABASE_URL_GADGET_DEN", "postgresql://example/gadget")
    assert reg.dsn_for("gadget-den") == "postgresql://example/gadget"
    # the module-level API reads the same storage through get_storage()
    assert site_registry.site_names() == reg.site_names()
    assert site_registry.producers_for("gadget-den") == reg.producers_for("gadget-den")
    assert site_registry.site_for_agent("gadget-den-catalog-audit-agent") == "gadget-den"
    assert site_registry.dsn_env_for("gadget-den") == "DATABASE_URL_GADGET_DEN"
    assert site_registry.dsn_for("gadget-den") == "postgresql://example/gadget"
    assert site_registry.get_site("gadget-den") == e
    assert [s.name for s in site_registry.list_sites()] == reg.site_names()
    assert site_registry.producer_agent_ids() == reg.producer_agent_ids()
    assert site_registry.starvation_sites() == reg.starvation_sites()
    assert site_registry.content_tier_map() == reg.content_tier_map()
    assert site_registry.seo_site_yaml_path("gadget-den") == e.seo_site_yaml_path
    assert site_registry.split_agent_id("gadget-den-x-agent") == ("gadget-den", "x-agent")
    assert site_registry.agent_id_for("gadget-den", "*-x-agent") == "gadget-den-x-agent"


def test_dispatch_group_walks_a_new_site_after_every_incumbent(base, store):
    _write_override(store, {"sites": [dict(THIRD, dispatch_group=1, content_tier=4)]})
    reg = _load(store)
    n = len(base.producer_agent_ids())
    assert reg.producer_agent_ids()[:n] == base.producer_agent_ids()
    assert reg.producer_agent_ids()[n:] == reg.producers_for("gadget-den")
    assert reg.content_tier_map()[4] == reg.content_agents_for("gadget-den")
    assert reg.content_agents_for("gadget-den") == [
        "gadget-den" + t[1:] for t in base.content_agent_templates]


def test_a_new_site_defaults_to_the_group_after_every_built_in_site(base, store):
    first = base.site_names()[0]
    # a built-in site moved to a later group pushes the new site's default further
    _write_override(store, {"sites": [{"name": first, "dispatch_group": 3}, dict(THIRD)]})
    reg = _load(store, strict=True)
    assert reg.get_site(first).dispatch_group == 3
    assert reg.get_site("gadget-den").dispatch_group == 4
    assert reg.producer_agent_ids()[-len(reg.producers_for("gadget-den")):] == \
        reg.producers_for("gadget-den")
    # null resets to that derived default; an explicit value wins
    _write_override(store, {"sites": [dict(THIRD, dispatch_group=None)]})
    assert _load(store).get_site("gadget-den").dispatch_group == 1
    _write_override(store, {"sites": [dict(THIRD, dispatch_group=0)]})
    assert _load(store).get_site("gadget-den").dispatch_group == 0
    # built-in sites keep 0; with replace_sites there is no built-in site to protect
    assert {e.dispatch_group for e in base.list_sites(True)} == {0}
    _write_override(store, {"replace_sites": True, "sites": [dict(THIRD)]})
    assert _load(store).get_site("gadget-den").dispatch_group == 0


def test_site_templates_extras_and_walk_order(base, store):
    # dispatch_group=0 opts in to being interleaved with the built-in sites
    _write_override(store, {"sites": [dict(
        THIRD, dispatch_group=0,
        producer_templates=["*-seo-opportunity-agent", "*-gizmo-scout-agent"],
        producer_extras=["*-deal-watch-agent", "gizmo-feed-agent"])]})
    reg = _load(store)
    assert reg.producers_for("gadget-den") == [
        "gadget-den-seo-opportunity-agent", "gadget-den-gizmo-scout-agent",
        "gadget-den-deal-watch-agent", "gizmo-feed-agent"]
    # an explicit extra without the prefix is still owned by its site
    assert reg.site_for_agent("gizmo-feed-agent") == "gadget-den"
    assert reg.split_agent_id("gizmo-feed-agent") == ("gadget-den", "gizmo-feed-agent")
    # Walk order: template by template (registry-wide first, then the
    # site-only template), site by site; extras last, site by site.
    expected: list[str] = []
    for template in base.producer_templates + ("*-gizmo-scout-agent",):
        expected += [e.agent_id(template) for e in base.list_sites()
                     if template in e.producer_templates]
        if template in ("*-seo-opportunity-agent", "*-gizmo-scout-agent"):
            expected.append("gadget-den" + template[1:])
    for e in base.list_sites():
        expected += [e.agent_id(x) for x in e.producer_extras]
    expected += ["gadget-den-deal-watch-agent", "gizmo-feed-agent"]
    assert reg.producer_agent_ids() == expected


def test_a_patch_changes_only_the_fields_it_names(base, store):
    first = base.site_names()[0]
    _write_override(store, {"sites": [{"name": first, "starvation": False,
                                       "extra": {"note": "x"}, "_why": "comment"}]})
    reg = _load(store)
    before, after = base.get_site(first).to_dict(), reg.get_site(first).to_dict()
    assert {k for k in before if before[k] != after[k]} == {"starvation", "extra"}
    assert after["extra"] == {"note": "x"}
    assert first not in reg.starvation_sites()
    for name in base.site_names()[1:]:
        assert reg.get_site(name) == base.get_site(name)
    assert reg.site_names() == base.site_names()


def test_extra_deep_merges_and_null_resets_to_the_derived_default(base, store):
    first = base.site_names()[0]
    _write_override(store, {"sites": [{"name": first, "extra": {"a": {"b": 1, "c": 2}}}]})
    reg1 = _load(store)
    assert dict(reg1.get_site(first).extra["a"]) == {"b": 1, "c": 2}
    # null resets: accent -> None, label/dsn_env -> derived defaults
    _write_override(store, {"sites": [{"name": first, "accent": None, "label": None,
                                       "dsn_env": None, "dsn_env_aliases": None}]})
    e = _load(store).get_site(first)
    assert e.accent is None
    assert e.label == first.title()
    assert e.dsn_env == site_registry.default_dsn_env(first)
    assert e.dsn_env_aliases == ()
    # nested null removes one key of extra; '_' keys are data inside extra
    # (comments only at the site level)
    doc = {"sites": [dict(THIRD, _why="comment", extra={"a": {"b": 1, "c": 2}, "_note": "kept"})]}
    reg2 = SiteRegistry.from_documents(
        doc, {"sites": [{"name": "gadget-den", "extra": {"a": {"c": None}, "d": {"e": None}}}]},
        strict=True)
    assert site_registry._thaw(reg2.get_site("gadget-den").extra) == {
        "a": {"b": 1}, "_note": "kept", "d": {}}
    assert "_why" not in reg2.to_dict()["sites"][0]


def test_disabled_site_is_known_but_not_enumerated(base, store):
    last = base.site_names()[-1]
    _write_override(store, {"sites": [{"name": last, "enabled": False}]})
    reg = _load(store)
    assert last not in reg.site_names()
    assert reg.site_names(include_disabled=True) == base.site_names()
    assert reg.get_site(last).enabled is False
    assert not set(reg.producer_agent_ids()) & set(base.producers_for(last))
    assert reg.producer_agent_ids(include_disabled=True) == base.producer_agent_ids()
    assert last not in reg.starvation_sites()
    tiered = [a for agents in reg.content_tier_map().values() for a in agents]
    assert not set(tiered) & set(base.content_agents_for(last))
    # identity facts still hold
    assert reg.producers_for(last) == base.producers_for(last)
    assert reg.site_for_agent(base.producers_for(last)[0]) == last


def test_replace_sites_drops_the_defaults(base, store):
    _write_override(store, {"replace_sites": True, "sites": [dict(THIRD)]})
    reg = _load(store)
    assert reg.site_names() == ["gadget-den"]
    assert reg.producer_templates == base.producer_templates
    assert reg.content_agent_templates == base.content_agent_templates
    for aid in base.producer_agent_ids():
        assert reg.site_for_agent(aid) is None
    assert reg.starvation_sites() == []


def test_top_level_template_lists_replace_the_defaults(base, store):
    _write_override(store, {"producer_templates": ["*-seo-opportunity-agent"],
                            "content_agent_templates": []})
    reg = _load(store)
    for entry in reg.list_sites():
        assert reg.producers_for(entry.name) == (
            [entry.agent_id("*-seo-opportunity-agent")]
            + [entry.agent_id(x) for x in entry.producer_extras])
    assert reg.content_tier_map() == {}


def test_longest_agent_prefix_wins(store):
    _write_override(store, {"replace_sites": True, "sites": [
        dict(THIRD, name="gadget", domain="gadget.example", repo_path="/srv/sites/gadget"),
        dict(THIRD)]})
    reg = _load(store, strict=True)
    assert reg.site_for_agent("gadget-den-seo-opportunity-agent") == "gadget-den"
    assert reg.site_for_agent("gadget-seo-opportunity-agent") == "gadget"
    assert reg.split_agent_id("gadget-den-x-agent") == ("gadget-den", "x-agent")


def _clone_of(entry: SiteEntry) -> dict[str, Any]:
    """A copy-pasted entry left half-edited: new name, everything else the
    built-in site's."""
    return {"name": "gadget-den",
            **{k: getattr(entry, k) for k in ("domain", "repo_path", "branch", "dsn_env",
                                               "seo_site_yaml")}}


def _deeply_nested(depth: int) -> bytes:
    site = json.dumps(dict(THIRD)).encode()[:-1]  # drop the closing brace
    return (b'{"sites": [' + site + b', "extra": ' + b'{"a": ' * depth + b"1"
            + b"}" * depth + b"}]}")


def _bad_overrides(base: SiteRegistry) -> dict[str, Any]:
    first = base.site_names()[0]
    inc, last = base.get_site(first), base.get_site(base.site_names()[-1])
    repo = PurePosixPath(inc.repo_path)
    return {
        "not JSON": b"{nope",
        "not UTF-8": b"\xff\xfe\x00",
        "JSON null": b"null",
        "JSON array": b"[]",
        "JSON string": b'"sites"',
        "absurdly nested extra": _deeply_nested(5_000),
        "absurdly nested JSON": _deeply_nested(200_000),
        "no site left (replace_sites alone)": {"replace_sites": True},
        "every site disabled": {"sites": [{"name": n, "enabled": False}
                                          for n in base.site_names()]},
        "copy-paste of a built-in entry": {"sites": [_clone_of(inc)]},
        "built-in alias as dsn_env": {"sites": [dict(THIRD, dsn_env=inc.dsn_env_aliases[-1])]},
        "built-in dsn_env as an alias": {"sites": [dict(THIRD, dsn_env_aliases=[inc.dsn_env])]},
        "built-in domain": {"sites": [dict(THIRD, domain=inc.domain)]},
        "built-in checkout spelled differently": {"sites": [dict(
            THIRD, repo_path=f"{repo.parent}/./{repo.name}")]},
        "built-in site.yaml by absolute path": {"sites": [dict(
            THIRD, seo_site_yaml=str(inc.seo_site_yaml_path))]},
        "a disabled site keeps its resources": {"sites": [
            {"name": last.name, "enabled": False}, dict(THIRD, domain=last.domain)]},
        "unknown key": {"sitez": []},
        "new site without domain": {"sites": [{"name": "gadget-den", "repo_path": "/srv/x",
                                               "branch": "main"}]},
        "agent_prefix taken": {"sites": [dict(THIRD, agent_prefix=first)]},
        "producer of another site": {"sites": [dict(
            THIRD, producer_extras=[base.producers_for(first)[0]])]},
        "explicit id under another prefix": {"sites": [
            dict(THIRD, name="gadget", domain="gadget.example", repo_path="/srv/sites/gadget",
                 producer_extras=["gadget-den-x-agent"]),
            dict(THIRD)]},
        "expanded id too long": {"sites": [dict(THIRD, producer_extras=["*-" + "a" * 58])]},
        "alias repeats dsn_env": {"sites": [dict(THIRD, dsn_env_aliases=["DATABASE_URL_GADGET_DEN"])]},
        "producer listed twice": {"sites": [dict(THIRD, producer_extras=["*-seo-opportunity-agent"])]},
        "secret in dsn_env": {"sites": [dict(THIRD, dsn_env=SECRET)]},
        "secret in aliases": {"sites": [dict(THIRD, dsn_env_aliases=[SECRET])]},
        "secret as a key": {"sites": [dict(THIRD, **{SECRET: 1})]},
    }


@pytest.mark.parametrize("case", list(_bad_overrides(
    site_registry.load_registry(use_storage=False, max_age_s=0))))
def test_an_invalid_override_is_ignored_as_a_whole(base, store, caplog, case):
    _write_override(store, _bad_overrides(base)[case])
    with caplog.at_level(logging.DEBUG, logger="framework.site_registry"):
        reg = _load(store)
    # the reference sites keep working, exactly as the defaults define them
    assert reg.to_dict() == base.to_dict()
    assert reg.sources == base.sources
    assert reg.warnings and site_registry.CONFIG_KEY in reg.warnings[-1]
    assert site_registry.CONFIG_KEY in caplog.text
    with pytest.raises(SiteRegistryError) as err:
        _load(store, strict=True)
    # values are never echoed, so a pasted connection string cannot leak
    for text in (str(err.value), caplog.text, *reg.warnings):
        assert "S3NTINEL" not in text


def test_invalid_dsn_env_says_what_is_wrong_without_the_value(store):
    _write_override(store, {"sites": [dict(THIRD, dsn_env=SECRET)]})
    with pytest.raises(SiteRegistryError) as err:
        _load(store, strict=True)
    msg = str(err.value)
    assert "'dsn_env'" in msg and "connection string" in msg and "NAME" in msg
    assert "S3NTINEL" not in msg and "postgresql" not in msg


def test_sites_never_share_a_domain_checkout_site_yaml_or_database(base, store, tmp_path,
                                                                   capsys):
    inc = base.list_sites()[0]
    clone = _clone_of(inc)
    _write_override(store, {"sites": [clone]})
    with pytest.raises(SiteRegistryError) as err:
        _load(store, strict=True)
    msg = str(err.value)
    for shown in ("'domain'", "'repo_path'", "'seo_site_yaml' (the same file)",
                  f"DSN env var '{inc.dsn_env}'"):
        assert f"site 'gadget-den': {shown} is already used by site '{inc.name}'" in msg, shown
    assert "must not share" in msg
    # it names fields and the owning site, never the values themselves
    assert inc.domain not in msg and inc.repo_path not in msg
    # lenient: the built-in sites keep running exactly as before
    reg = _load(store)
    assert reg.to_dict() == base.to_dict() and "already used by site" in reg.warnings[-1]
    # the CLI gate an operator runs before writing the storage copy
    candidate = tmp_path / "clone.json"
    candidate.write_text(json.dumps({"sites": [clone]}), encoding="utf-8")
    rc, out, err_text = _cli(capsys, "validate", "--override", str(candidate))
    assert rc == 2 and out == "" and "already used by site" in err_text
    # an alias counts as much as dsn_env, in both directions, and the DSN of
    # the built-in site is then never reachable through the new site
    for patch in ({"dsn_env": inc.dsn_env_aliases[-1]}, {"dsn_env_aliases": [inc.dsn_env]}):
        with pytest.raises(SiteRegistryError, match="DSN env var"):
            SiteRegistry.from_documents(DEFAULT_DOC, {"sites": [dict(THIRD, **patch)]},
                                        strict=True)
    # a site of its own passes
    assert "gadget-den" in SiteRegistry.from_documents(
        DEFAULT_DOC, {"sites": [dict(THIRD)]}, strict=True).site_names()


def test_a_registry_keeps_at_least_one_enabled_site(base, tmp_path):
    for doc in ({}, {"sites": []}, {"sites": [dict(THIRD, enabled=False)]}):
        with pytest.raises(SiteRegistryError, match="no enabled site"):
            SiteRegistry.from_documents(doc)
        p = tmp_path / "defaults.json"
        p.write_text(json.dumps(doc), encoding="utf-8")
        with pytest.raises(SiteRegistryError, match="no enabled site"):
            site_registry.load_registry(use_storage=False, defaults_path=p, max_age_s=0)
    for override in ({"replace_sites": True},
                     {"replace_sites": True, "sites": [dict(THIRD, enabled=False)]},
                     {"sites": [{"name": n, "enabled": False} for n in base.site_names()]}):
        lenient = SiteRegistry.from_documents(DEFAULT_DOC, override)
        assert lenient.to_dict() == base.to_dict() and "no enabled site" in lenient.warnings[-1]
        with pytest.raises(SiteRegistryError, match="no enabled site"):
            SiteRegistry.from_documents(DEFAULT_DOC, override, strict=True)
    with pytest.raises(SiteRegistryError, match="no enabled site"):
        SiteRegistry([])


def test_the_loader_never_writes_storage(base, store, capsys):
    _write_override(store, {"sites": [dict(THIRD)]})
    root = Path(store.root)

    def snapshot():
        return {p.relative_to(root).as_posix(): p.read_bytes()
                for p in root.rglob("*") if p.is_file()}

    before = snapshot()
    _load(store)
    _load(store, strict=True)
    site_registry.load_registry(store)  # cached path
    site_registry.site_names()          # get_storage() path
    for argv in (["dump"], ["validate"], ["list", "--all"], ["show", "gadget-den"]):
        assert site_registry.main(argv) == 0
    capsys.readouterr()
    assert snapshot() == before


def test_unavailable_storage_falls_back_to_the_defaults(base, monkeypatch):
    def no_storage(*a, **k):
        raise SystemExit("AZURE_STORAGE_CONNECTION_STRING is not set")

    monkeypatch.setattr(site_registry, "get_storage", no_storage)
    reg = site_registry.load_registry(max_age_s=0)
    assert reg.to_dict() == base.to_dict()
    assert any("storage unavailable" in w for w in reg.warnings)
    with pytest.raises(SiteRegistryError):
        site_registry.load_registry(strict=True, max_age_s=0)


def test_a_broken_backend_warns_once_per_cache_window(monkeypatch, caplog):
    def no_storage(*a, **k):
        raise SystemExit("AZURE_STORAGE_CONNECTION_STRING is not set")

    monkeypatch.setattr(site_registry, "get_storage", no_storage)
    with caplog.at_level(logging.WARNING, logger="framework.site_registry"):
        first = site_registry.load_registry()
        for _ in range(5):
            assert site_registry.site_for_agent("agent-doctor") is None
            assert site_registry.load_registry() is first
    assert caplog.text.count("storage unavailable") == 1


def test_a_backend_read_error_falls_back_to_the_defaults(base, tmp_path):
    class _Down(LocalFilesystemStorage):
        def read_bytes(self, key):
            raise RuntimeError("backend down")

    down = _Down(str(tmp_path / "down"))
    reg = site_registry.load_registry(down, max_age_s=0)
    assert reg.to_dict() == base.to_dict()
    assert any("storage read failed" in w for w in reg.warnings)
    with pytest.raises(SiteRegistryError):
        site_registry.load_registry(down, strict=True, max_age_s=0)


def test_an_unreadable_but_present_override_is_reported(base, tmp_path):
    """An archived Azure blob reads as None but still exists; that must not
    pass silently as 'no override'."""
    class _Archived(LocalFilesystemStorage):
        def read_bytes(self, key):
            return None

    archived = _Archived(str(tmp_path / "archived"))
    archived.write_json(site_registry.CONFIG_KEY, {"sites": [dict(THIRD)]})
    reg = site_registry.load_registry(archived, max_age_s=0)
    assert reg.to_dict() == base.to_dict()
    assert any("exists but could not be read" in w for w in reg.warnings)
    with pytest.raises(SiteRegistryError, match="exists but could not be read"):
        site_registry.load_registry(archived, strict=True, max_age_s=0)
    # absent stays quiet
    empty = _Archived(str(tmp_path / "empty"))
    assert site_registry.load_registry(empty, max_age_s=0).warnings == ()


def test_broken_defaults_raise(tmp_path):
    with pytest.raises(SiteRegistryError, match="unreadable"):
        site_registry.load_registry(use_storage=False, defaults_path=tmp_path / "missing.json",
                                    max_age_s=0)
    bad_json = tmp_path / "bad.json"
    bad_json.write_text("{", encoding="utf-8")
    with pytest.raises(SiteRegistryError, match="not valid JSON"):
        site_registry.load_registry(use_storage=False, defaults_path=bad_json, max_age_s=0)
    not_utf8 = tmp_path / "latin1.json"
    not_utf8.write_bytes(b'{"_doc": "caf\xe9"}')
    with pytest.raises(SiteRegistryError, match="not UTF-8"):
        site_registry.load_registry(use_storage=False, defaults_path=not_utf8, max_age_s=0)
    deep = tmp_path / "deep.json"
    deep.write_bytes(_deeply_nested(200_000))
    with pytest.raises(SiteRegistryError, match="not valid JSON"):
        site_registry.load_registry(use_storage=False, defaults_path=deep, max_age_s=0)
    for doc, needle in (({"sites": [{"name": "gadget-den"}]}, "'domain' is required"),
                        ({"sites": [dict(THIRD), dict(THIRD, name="gadget",
                                                      agent_prefix="gadget-den")]},
                         "agent_prefix is already used"),
                        ({"sitez": []}, "unknown key")):
        p = tmp_path / "defaults.json"
        p.write_text(json.dumps(doc), encoding="utf-8")
        with pytest.raises(SiteRegistryError, match=needle):
            site_registry.load_registry(use_storage=False, defaults_path=p, max_age_s=0)


def test_alternative_defaults_document(tmp_path):
    p = tmp_path / "defaults.json"
    p.write_text(json.dumps({"schema_version": "1", "producer_templates": ["*-a-agent"],
                             "sites": [dict(THIRD)]}), encoding="utf-8")
    reg = site_registry.load_registry(use_storage=False, defaults_path=p, max_age_s=0)
    assert reg.site_names() == ["gadget-den"]
    assert reg.producer_agent_ids() == ["gadget-den-a-agent"]
    assert reg.content_tier_map() == {}


# =============================================================================
# API semantics
# =============================================================================

def test_dsn_for_reads_names_from_the_registry_and_never_logs_values(base, caplog, monkeypatch):
    entry = base.list_sites()[0]
    alias = entry.dsn_env_aliases[0]
    env = {alias: SECRET}
    with caplog.at_level(logging.DEBUG, logger="framework.site_registry"):
        # aliases are tried only when asked for
        assert base.dsn_for(entry.name, environ=env) is None
        assert base.dsn_for(entry.name, include_aliases=True, environ=env) == SECRET
        both = {entry.dsn_env: "primary-S3NTINEL", alias: SECRET}
        assert base.dsn_for(entry.name, include_aliases=True, environ=both) == "primary-S3NTINEL"
    assert "S3NTINEL" not in caplog.text
    assert alias in caplog.text  # names are fine to log
    info = base.describe_dsn(entry.name, include_aliases=True, environ=env)
    assert info == {"site": entry.name, "candidates": list(entry.dsn_env_names(True)),
                    "resolved_env": alias}
    assert base.dsn_for(entry.name, environ={entry.dsn_env: ""}) is None  # empty = unset
    monkeypatch.setenv(entry.dsn_env, "from-os-environ")
    assert base.dsn_for(entry.name) == "from-os-environ"
    assert base.dsn_env_for(entry.name) == entry.dsn_env
    assert "S3NTINEL" not in repr(entry) and "S3NTINEL" not in json.dumps(base.describe())


def test_dsn_for_names_tries_exactly_the_names_given(base, store, caplog):
    entry, other = base.list_sites()[0], base.list_sites()[-1]
    first_alias, last_alias = entry.dsn_env_aliases[0], entry.dsn_env_aliases[-1]
    env = {entry.dsn_env: "primary", first_alias: "first", last_alias: "last"}
    # one convention only, whatever else is set
    assert base.dsn_for(entry.name, names=[last_alias], environ=env) == "last"
    assert base.dsn_for(entry.name, names=last_alias, environ=env) == "last"  # a bare name
    assert base.dsn_for(entry.name, names=[last_alias], environ={entry.dsn_env: "p"}) is None
    # in the order given
    assert base.dsn_for(entry.name, names=[first_alias, entry.dsn_env], environ=env) == "first"
    assert base.describe_dsn(entry.name, names=[last_alias, entry.dsn_env], environ={}) == {
        "site": entry.name, "candidates": [last_alias, entry.dsn_env], "resolved_env": None}
    # only the site's own names: not another site's, not an arbitrary variable
    with caplog.at_level(logging.DEBUG, logger="framework.site_registry"):
        for bad in ([other.dsn_env], ["HOME"], [SECRET], []):
            with pytest.raises(ValueError) as err:
                base.dsn_for(entry.name, names=bad, environ=env)
            assert entry.dsn_env in str(err.value) and "S3NTINEL" not in str(err.value)
    assert "S3NTINEL" not in caplog.text
    with pytest.raises(TypeError):
        base.dsn_for(entry.name, names=[last_alias], include_aliases=True, environ=env)
    # the module-level call passes it through
    assert site_registry.dsn_for(entry.name, names=[last_alias], environ=env) == "last"


def test_entries_and_registries_copy_pickle_and_asdict(base):
    import pickle

    entry = SiteEntry(name="gadget-den", label="Gadget Den", domain="gadget-den.example",
                      repo_path="/srv/sites/gadget-den", branch="main",
                      seo_site_yaml="examples/sites/gadget-den.yaml",
                      dsn_env="DATABASE_URL_GADGET_DEN", extra={"a": {"b": [1, 2]}, "c": "d"})
    for clone in (copy.copy(entry), copy.deepcopy(entry), pickle.loads(pickle.dumps(entry))):
        assert clone == entry and hash(clone) == hash(entry)
        assert clone.to_dict() == entry.to_dict()
        with pytest.raises(TypeError):
            clone.extra["x"] = 1  # type: ignore[index]
        with pytest.raises(TypeError):
            clone.extra["a"]["x"] = 1
    # dataclasses.asdict is what FastAPI's jsonable_encoder uses for a dataclass
    assert json.loads(json.dumps(dataclasses.asdict(entry))) == entry.to_dict()
    mutable = entry.extra.copy()
    mutable["x"] = 1  # .copy() hands out an ordinary dict
    assert "x" not in entry.extra
    for mutate in (lambda e: e.update(x=1), lambda e: e.pop("c"), lambda e: e.setdefault("x", 1),
                   lambda e: e.popitem(), lambda e: e.clear(), lambda e: e.__delitem__("c"),
                   lambda e: e.__ior__({"x": 1})):
        with pytest.raises(TypeError):
            mutate(entry.extra)
    assert dict(entry.extra) == {"a": {"b": (1, 2)}, "c": "d"}
    for clone in (copy.deepcopy(base), pickle.loads(pickle.dumps(base))):
        assert clone.to_dict() == base.to_dict()
        assert clone.producer_agent_ids() == base.producer_agent_ids()
        assert clone.site_for_agent(base.producer_agent_ids()[-1]) == \
            base.site_for_agent(base.producer_agent_ids()[-1])


def test_fastapi_can_encode_an_entry(base):
    encoders = pytest.importorskip("fastapi.encoders")
    for entry in base.list_sites():
        assert encoders.jsonable_encoder(entry) == entry.to_dict()


def test_unknown_sites(base):
    assert base.get_site("no-such-site") is None
    assert base.get_site(None) is None  # type: ignore[arg-type]
    calls = (base.require_site, base.producers_for, base.dsn_env_for, base.seo_site_yaml_path,
             base.content_agents_for, base.dsn_for, base.describe_dsn, base.describe_site,
             lambda s: base.agent_id_for(s, "*-x-agent"))
    for call in calls:
        with pytest.raises(UnknownSiteError) as err:
            call("no-such-site")
        assert isinstance(err.value, KeyError)
        assert "no-such-site" in str(err.value) and "registry defines" in str(err.value)
    assert base.site_for_agent("no-such-site-seo-opportunity-agent") is None
    assert base.site_for_agent(None) is None  # type: ignore[arg-type]
    assert base.site_for_agent("") is None
    assert base.split_agent_id("agent-doctor") == (None, "agent-doctor")


def test_entries_and_registry_are_immutable(base):
    entry = base.list_sites()[0]
    with pytest.raises(dataclasses.FrozenInstanceError):
        entry.name = "x"  # type: ignore[misc]
    with pytest.raises(TypeError):
        entry.extra["k"] = 1  # type: ignore[index]
    assert isinstance(entry.producer_templates, tuple) and isinstance(entry.dsn_env_aliases, tuple)
    hash(entry)
    names = base.site_names()
    base.list_sites().clear()
    base.site_names().clear()
    assert base.site_names() == names


def test_site_entry_constructed_directly():
    e = SiteEntry(name="gadget-den", label="Gadget Den", domain="gadget-den.example",
                  repo_path="/srv/sites/gadget-den", branch="main",
                  seo_site_yaml="examples/sites/gadget-den.yaml", dsn_env="DATABASE_URL_GADGET_DEN",
                  producer_templates=["*-a-agent"], extra={"a": {"b": [1, 2]}})
    assert e.agent_prefix == "gadget-den"
    assert e.producer_templates == ("*-a-agent",) and e.producers == ("gadget-den-a-agent",)
    assert e.extra["a"]["b"] == (1, 2)
    with pytest.raises(TypeError):
        e.extra["a"]["c"] = 3  # nested values are frozen too
    assert e.to_dict()["extra"] == {"a": {"b": [1, 2]}}
    assert site_registry.expand_agent_ref("*-a-agent", "p") == "p-a-agent"
    assert site_registry.expand_agent_ref("full-agent", "p") == "full-agent"


def test_registry_validates_directly_constructed_entries():
    good = dict(name="gadget-den", label="Gadget Den", domain="gadget-den.example",
                repo_path="/srv/sites/gadget-den", branch="main",
                seo_site_yaml="examples/sites/gadget-den.yaml", dsn_env="DATABASE_URL_GADGET_DEN")
    assert SiteRegistry([SiteEntry(**good)]).site_names() == ["gadget-den"]
    for bad, needle in ((dict(good, domain="https://x"), "'domain'"),
                        (dict(good, dsn_env=SECRET), "connection string"),
                        (dict(good, dsn_env_aliases=("DATABASE_URL_GADGET_DEN",)), "repeats 'dsn_env'"),
                        (dict(good, content_tier=0), "'content_tier'")):
        with pytest.raises(SiteRegistryError, match=needle) as err:
            SiteRegistry([SiteEntry(**bad)])
        assert "S3NTINEL" not in str(err.value)
    with pytest.raises(SiteRegistryError, match="duplicate site name"):
        SiteRegistry([SiteEntry(**good), SiteEntry(**dict(good, agent_prefix="gden"))])
    with pytest.raises(SiteRegistryError, match="'domain' is already used by site 'gadget-den'"):
        SiteRegistry([SiteEntry(**good),
                      SiteEntry(**dict(good, name="gizmo", repo_path="/srv/sites/gizmo",
                                       seo_site_yaml="examples/sites/gizmo.yaml",
                                       dsn_env="DATABASE_URL_GIZMO"))])
    # an ill-typed field is reported, not a TypeError from the later checks
    with pytest.raises(SiteRegistryError, match="'repo_path' must be a string"):
        SiteRegistry([SiteEntry(**dict(good, repo_path=7))])  # type: ignore[arg-type]
    # a too-long expanded id is named in the message (it cannot be a secret)
    long_ref = "*-" + "a" * 58
    with pytest.raises(SiteRegistryError) as err:
        SiteRegistry([SiteEntry(**dict(good, producer_extras=(long_ref,)))])
    assert "gadget-den-" + "a" * 58 in str(err.value)


def test_to_dict_round_trips(base):
    doc = base.to_dict()
    again = SiteRegistry.from_documents(doc, strict=True)
    assert again.to_dict() == doc
    assert again.producer_agent_ids() == base.producer_agent_ids()
    assert again.content_tier_map() == base.content_tier_map()
    assert json.loads(json.dumps(doc)) == doc


def test_from_documents_strict_and_lenient():
    defaults = {"sites": [dict(THIRD)]}
    bad = {"sites": [dict(THIRD, branch="x..y")]}
    lenient = SiteRegistry.from_documents(defaults, bad, override_source="candidate")
    assert lenient.get_site("gadget-den").branch == "main"
    assert lenient.warnings and lenient.warnings[0].startswith("candidate ignored")
    with pytest.raises(SiteRegistryError, match="candidate is invalid"):
        SiteRegistry.from_documents(defaults, bad, override_source="candidate", strict=True)
    with pytest.raises(SiteRegistryError, match="defaults is invalid"):
        SiteRegistry.from_documents(bad | {"sitez": 1})


def test_cache_per_storage_with_ttl(store, tmp_path, monkeypatch):
    first = site_registry.load_registry(store)
    assert site_registry.load_registry(store) is first
    _write_override(store, {"sites": [dict(THIRD)]})
    assert site_registry.load_registry(store) is first  # cached until the TTL ends
    fresh = site_registry.load_registry(store, max_age_s=0)
    assert "gadget-den" in fresh.site_names()
    site_registry.clear_cache()
    again = site_registry.load_registry(store)
    assert again is not first and "gadget-den" in again.site_names()
    # another storage object is another cache entry
    other = LocalFilesystemStorage(str(tmp_path / "other"))
    assert "gadget-den" not in site_registry.load_registry(other).site_names()
    # strict and lenient loads are cached separately
    assert site_registry.load_registry(store, strict=True) is not again
    # an expired entry reloads
    real = site_registry.time.monotonic
    with monkeypatch.context() as m:
        m.setattr(site_registry.time, "monotonic", lambda: real() + 10_000)
        assert site_registry.load_registry(store) is not again
    # SITES_REGISTRY_CACHE_TTL_S=0 disables caching
    monkeypatch.setenv(site_registry.CACHE_TTL_ENV, "0")
    assert site_registry.load_registry(store) is not site_registry.load_registry(store)
    monkeypatch.setenv(site_registry.CACHE_TTL_ENV, "not-a-number")
    assert site_registry._cache_ttl() == site_registry.DEFAULT_CACHE_TTL_S


def test_max_age_is_the_callers_staleness_limit(store, monkeypatch):
    real = site_registry.time.monotonic
    first = site_registry.load_registry(store)  # cached with the default 60 s
    with monkeypatch.context() as m:
        m.setattr(site_registry.time, "monotonic", lambda: real() + 30)
        assert site_registry.load_registry(store) is first  # 30 s old: fine for 60 s
        fresh = site_registry.load_registry(store, max_age_s=5)  # too old for 5 s
        assert fresh is not first
        assert site_registry.load_registry(store, max_age_s=5) is fresh
    # an entry stored with a short TTL is not served past it, even to a
    # caller that would accept an older one
    with monkeypatch.context() as m:
        m.setattr(site_registry.time, "monotonic", lambda: real() + 40)
        assert site_registry.load_registry(store) is not fresh


# =============================================================================
# CLI (read-only, never prints a DSN value)
# =============================================================================

def _cli(capsys, *argv: str) -> tuple[int, str, str]:
    rc = site_registry.main(list(argv))
    out, err = capsys.readouterr()
    return rc, out, err


def test_cli_lookups(base, store, capsys):
    names = base.site_names()
    first = base.get_site(names[0])
    assert _cli(capsys, "list") == (0, "\n".join(names) + "\n", "")
    rc, out, _ = _cli(capsys, "list", "--json")
    assert rc == 0 and json.loads(out) == names
    rc, out, _ = _cli(capsys, "show", first.name)
    shown = json.loads(out)
    assert rc == 0 and shown["name"] == first.name
    assert shown["derived"]["producers"] == list(first.producers)
    assert shown["derived"]["seo_site_yaml_path"] == str(first.seo_site_yaml_path)
    assert _cli(capsys, "get", first.name, "dsn_env")[1] == first.dsn_env + "\n"
    assert _cli(capsys, "get", first.name, "repo_path")[1] == first.repo_path + "\n"
    assert _cli(capsys, "get", first.name, "seo_site_yaml_path")[1] == f"{first.seo_site_yaml_path}\n"
    assert _cli(capsys, "get", first.name, "starvation")[1] == "true\n"
    assert _cli(capsys, "get", first.name, "producers")[1] == "\n".join(first.producers) + "\n"
    assert _cli(capsys, "get", first.name, "extra")[1].strip() == "{}"
    assert _cli(capsys, "get", first.name, "no_such_field")[0] == 2
    assert _cli(capsys, "get", "no-such-site", "domain")[0] == 1
    assert _cli(capsys, "producers")[1] == "\n".join(base.producer_agent_ids()) + "\n"
    assert _cli(capsys, "producers", names[-1])[1] == "\n".join(base.producers_for(names[-1])) + "\n"
    assert _cli(capsys, "site-for-agent", base.producer_agent_ids()[0]) == (0, names[0] + "\n", "")
    assert _cli(capsys, "site-for-agent", "agent-doctor") == (1, "", "")
    assert _cli(capsys, "dsn-env", first.name)[1] == first.dsn_env + "\n"
    assert _cli(capsys, "dsn-env", first.name, "--aliases")[1] == \
        "\n".join(first.dsn_env_names(True)) + "\n"


def test_cli_reads_the_storage_override_unless_told_not_to(base, store, capsys):
    _write_override(store, {"sites": [dict(THIRD, enabled=False)]})
    assert _cli(capsys, "list")[1].split() == base.site_names()
    assert _cli(capsys, "list", "--all")[1].split() == base.site_names() + ["gadget-den"]
    assert _cli(capsys, "--no-storage", "list", "--all")[1].split() == base.site_names()
    assert _cli(capsys, "site-for-agent", "gadget-den-x-agent")[1] == "gadget-den\n"
    rc, out, _ = _cli(capsys, "dump")
    dumped = json.loads(out)
    assert rc == 0 and dumped["sources"][-1] == f"storage:{site_registry.CONFIG_KEY}"
    assert dumped["producer_agent_ids"] == base.producer_agent_ids()


def test_cli_never_prints_a_dsn_value(base, store, capsys, monkeypatch):
    first = base.list_sites()[0]
    monkeypatch.setenv(first.dsn_env, SECRET)
    rc, out, err = _cli(capsys, "dsn-env", first.name, "--resolved")
    assert (rc, out) == (0, first.dsn_env + "\n")
    monkeypatch.delenv(first.dsn_env)
    rc, out, err = _cli(capsys, "dsn-env", first.name, "--resolved")
    assert rc == 1 and out == "" and first.dsn_env in err
    monkeypatch.setenv(first.dsn_env, SECRET)
    outputs = [_cli(capsys, *argv) for argv in (["dump"], ["show", first.name],
                                                 ["get", first.name, "dsn_env_aliases"], ["validate"])]
    for _, out, err in outputs:
        assert "S3NTINEL" not in out + err


def test_cli_validate(base, store, tmp_path, capsys):
    rc, out, _ = _cli(capsys, "validate")
    assert rc == 0 and out.startswith(f"ok: {len(base.site_names())} site(s)")
    good = tmp_path / "override.json"
    good.write_text(json.dumps({"sites": [dict(THIRD)]}), encoding="utf-8")
    rc, out, _ = _cli(capsys, "validate", "--override", str(good))
    assert rc == 0 and "gadget-den" in out
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"sites": [dict(THIRD, dsn_env=SECRET)]}), encoding="utf-8")
    rc, out, err = _cli(capsys, "validate", "--override", str(bad))
    assert rc == 2 and "dsn_env" in err and "S3NTINEL" not in out + err
    not_json = tmp_path / "nope.json"
    not_json.write_text("{", encoding="utf-8")
    assert _cli(capsys, "validate", "--override", str(not_json))[0] == 2
    assert _cli(capsys, "validate", "--override", str(tmp_path / "missing.json"))[0] == 2
    # an invalid storage override fails validate but only warns for lookups
    _write_override(store, {"sites": [dict(THIRD, branch="x..y")]})
    rc, out, err = _cli(capsys, "validate")
    assert rc == 2 and "branch" in err
    rc, out, err = _cli(capsys, "list")
    assert rc == 0 and out.split() == base.site_names() and "warning:" in err


def test_cli_validate_refuses_to_drop_or_disable_a_built_in_site(base, store, tmp_path, capsys):
    last = base.site_names()[-1]
    cases = {"drops": {"replace_sites": True, "sites": [dict(THIRD)]},
             "disables": {"sites": [{"name": last, "enabled": False}, dict(THIRD)]}}
    for verb, doc in cases.items():
        candidate = tmp_path / f"{verb}.json"
        candidate.write_text(json.dumps(doc), encoding="utf-8")
        rc, out, err = _cli(capsys, "validate", "--override", str(candidate))
        assert rc == 2 and out == "", verb
        assert f"refused: the override {verb} built-in site(s)" in err and "--allow-drop" in err
        rc, out, err = _cli(capsys, "validate", "--override", str(candidate), "--allow-drop")
        assert rc == 0 and out.startswith("ok:") and f"note: the override {verb}" in err
    assert last in err  # the disabled site is named
    # the same gate covers the storage copy
    _write_override(store, cases["disables"])
    assert _cli(capsys, "validate")[0] == 2
    assert _cli(capsys, "validate", "--allow-drop")[0] == 0
    # leaving no enabled site is invalid even with --allow-drop
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"replace_sites": True}), encoding="utf-8")
    rc, out, err = _cli(capsys, "validate", "--override", str(empty), "--allow-drop")
    assert rc == 2 and "no enabled site" in err
    # a file holding JSON null (say `jq .missing > f`) is not "no override"
    for content in ("null", "[]", '"sites"'):
        nothing = tmp_path / "nothing.json"
        nothing.write_text(content, encoding="utf-8")
        rc, out, err = _cli(capsys, "validate", "--override", str(nothing))
        assert rc == 2 and "not a JSON object" in err, content
    deep = tmp_path / "deep.json"
    deep.write_bytes(_deeply_nested(200_000))
    assert _cli(capsys, "validate", "--override", str(deep))[0] == 2


def test_cli_validate_override_needs_no_storage(base, tmp_path, monkeypatch, capsys):
    """The pre-write gate checks a local file against the defaults; it must
    work (and stay quiet about storage) where no backend is configured."""
    def no_storage(*a, **k):
        raise AssertionError("validate --override touched framework storage")

    monkeypatch.setattr(site_registry, "get_storage", no_storage)
    candidate = tmp_path / "override.json"
    candidate.write_text(json.dumps({"sites": [dict(THIRD)]}), encoding="utf-8")
    rc, out, err = _cli(capsys, "validate", "--override", str(candidate))
    assert (rc, err) == (0, "") and out.startswith(f"ok: {len(base.site_names()) + 1} site(s)")


def test_cli_notes_a_missing_or_misnamed_override(base, store, capsys):
    rc, out, err = _cli(capsys, "validate")
    assert rc == 0 and f"note: no override at {site_registry.CONFIG_KEY}" in err
    rc, out, err = _cli(capsys, "dump")
    assert rc == 0 and f"note: no override at {site_registry.CONFIG_KEY}" in err
    # an override written under a sibling name is never read: say so
    misnamed = site_registry.MISNAMED_CONFIG_KEYS[0]
    store.write_json(misnamed, {"sites": [dict(THIRD)]})
    rc, out, err = _cli(capsys, "dump")
    assert rc == 0 and [s["name"] for s in json.loads(out)["sites"]] == base.site_names()
    assert f"warning: {misnamed} exists" in err and site_registry.CONFIG_KEY in err
    # once the right key holds it, there is nothing to note about it
    _write_override(store, {"sites": [dict(THIRD)]})
    rc, out, err = _cli(capsys, "validate")
    assert rc == 0 and "gadget-den" in out and "note: no override" not in err
    # lookups stay quiet; --no-storage never touches storage
    assert _cli(capsys, "list")[2] == ""
    assert "note:" not in _cli(capsys, "--no-storage", "dump")[2]
