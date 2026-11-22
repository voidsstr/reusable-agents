"""Settings for the affiliate article engine: one validated object built from
the site's YAML block (``article_engine:``). Nothing here names a site; every
table, SQL string, rule and threshold is the site's config.

Load order: ``$AFFILIATE_ARTICLE_CONFIG`` (or ``--site-yaml``) = ``PATH[#dotted.key]``
(default key ``article_engine``). A config that does not validate stops the
run before any DB, storage or model access (fail closed).
"""
from __future__ import annotations

import dataclasses
import os
import re
from pathlib import Path
from typing import Any, Mapping, Optional

from framework.core import authoring_capacity as cap
from framework.core import facts_pack as fp
from framework.core import guide_claims_guard as cg
from framework.core.article_link_classes import LinkClassConfigError, parse_link_classes

CONFIG_ENV = "AFFILIATE_ARTICLE_CONFIG"
DEFAULT_BLOCK = "article_engine"
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,80}$")
_SITE = re.compile(r"^[a-z][a-z0-9-]{0,40}$")
_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")
_STATUS = re.compile(r"^[a-z][a-z0-9_-]{0,19}$")
_SELECT_HEAD = re.compile(r"(?is)^\s*(with|select)\b")


class _OneSelect:
    """One SELECT (or WITH ... SELECT) statement: a ';' inside would let a
    second statement (e.g. SET SESSION CHARACTERISTICS ... READ WRITE) undo
    the read-only session, so it is refused."""

    @staticmethod
    def match(sql: str) -> bool:
        body = str(sql or "").strip().rstrip(";")
        return bool(_SELECT_HEAD.match(body)) and ";" not in body


_SELECT = _OneSelect()


class SettingsError(ValueError):
    def __init__(self, problems: list[str]):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems[:12]))


@dataclasses.dataclass(frozen=True)
class Settings:
    agent_id: str
    site: str
    audience: str
    host: str
    own_hosts: tuple[str, ...]
    dsn_env: str
    statement_timeout_ms: int
    calendar_path: str
    max_entries_per_run: int
    flags: Mapping[str, Any]
    schedule: cap.Schedule
    pool: Mapping[str, Any]
    model: Mapping[str, Any]
    occasions: Mapping[str, Any]
    output_gate: Mapping[str, Any]
    gate_lanes: tuple[str, ...]
    readiness_checks: Mapping[str, str]
    fact_queries: tuple[fp.FactQuery, ...]
    forbidden_relations: tuple[str, ...]
    fact_params: Mapping[str, Any]
    link_classes_raw: Mapping[str, Any]
    link_families: Mapping[str, str]
    static_paths: tuple[str, ...]
    indexable_sql: str
    placeholder_kinds: Mapping[str, Mapping[str, str]]
    buystrip: Mapping[str, Any]
    rule_sets: tuple[str, ...]
    claims_block: Mapping[str, Any]
    honesty_guard: bool
    prompt: Mapping[str, Any]
    articles: Mapping[str, Any]
    publish: Mapping[str, Any]
    signals: Mapping[str, str]
    post_publish: Mapping[str, Any]
    source: str = ""
    warnings: tuple[str, ...] = ()

    @property
    def link_classes(self):
        return parse_link_classes(self.link_classes_raw)

    def claims_policy(self) -> cg.ClaimsPolicy:
        return cg.parse_policy(self.claims_block, own_hosts=self.own_hosts)

    def describe(self) -> dict:
        d = dataclasses.asdict(self)
        d["schedule"] = {"not_before": self.schedule.not_before.isoformat() if self.schedule.not_before else None,
                         "dry_run_not_before": (self.schedule.dry_run_not_before.isoformat()
                                                if self.schedule.dry_run_not_before else None),
                         "blackouts": [{"start": b.start.isoformat(), "end": b.end.isoformat(), "reason": b.reason}
                                       for b in self.schedule.blackouts]}
        d["fact_queries"] = [q.name for q in self.fact_queries]
        return d


def _host_of(url: str) -> str:
    m = re.match(r"^https?://([^/:]+)", url or "", re.I)
    return m.group(1).lower() if m else ""


def build_settings(block: Mapping[str, Any], *, agent_id: str, source: str = "") -> Settings:
    if not isinstance(block, Mapping):
        raise SettingsError(["config block is not a mapping"])
    b = dict(block)
    p: list[str] = []
    w: list[str] = []

    site = str(b.get("site") or "")
    if not _SITE.match(site):
        p.append(f"site: {site!r} is not a kebab-case site id")
    audience = str(b.get("audience") or "general")
    host = str(b.get("host") or "").rstrip("/")
    if not re.match(r"^https://[a-z0-9.-]+$", host, re.I):
        p.append("host: the site's canonical origin, https://<host>")
    own = [_host_of(host)] + [str(h).lower() for h in (b.get("extra_hosts") or [])]

    db = dict(b.get("database") or {})
    dsn_env = str(db.get("dsn_env") or "")
    if not _ENV_NAME.match(dsn_env):
        p.append("database.dsn_env: the NAME of an environment variable (never a DSN)")

    cal = dict(b.get("calendar") or {})
    cal_path = str(cal.get("path") or "")
    try:
        max_entries = int(cal.get("max_entries_per_run") or 8)
    except (TypeError, ValueError):
        max_entries = 0
    if not 1 <= max_entries <= 50:
        p.append("calendar.max_entries_per_run: 1-50 (entries tried per run when one cannot be used)")
    if not cal_path:
        p.append("calendar.path: the site's editorial calendar file")
    elif source and not os.path.isabs(cal_path):
        cal_path = str((Path(source).parent / cal_path).resolve())

    try:
        schedule = cap.parse_schedule(b.get("schedule"))
    except cap.CapacityConfigError as e:
        p.append(str(e))
        schedule = cap.Schedule()

    model = {"dispatch_kind": "article-author", "required_tier": "opus", "provider": "",
             "allowed_provider_kinds": ["claude-cli", "anthropic"], "max_tokens": 16000, "temperature": 0.3,
             "max_minutes_per_day": 20, "max_calls_per_run": 2, "max_dry_run_calls_per_day": 50,
             **dict(b.get("model") or {})}
    cli = {"verified": False, "wrapper": "", **dict(model.get("claude_cli") or {})}
    model["claude_cli"] = cli
    if str(model["required_tier"]) != "opus":
        p.append(f"model.required_tier={model['required_tier']!r}: editorial prose is Opus-only (CLAUDE.md); "
                 "this engine never runs another tier")
    kinds = [str(k) for k in (model.get("allowed_provider_kinds") or [])]
    if not kinds:
        p.append("model.allowed_provider_kinds: name the provider kinds that can serve Opus (an empty list "
                 "would allow any provider)")
    if int(model.get("max_calls_per_run") or 0) not in (1, 2):
        p.append("model.max_calls_per_run: 1 (no re-prompt) or 2 (one re-prompt)")
    try:
        if float(model.get("max_minutes_per_day") or 0) <= 0:
            p.append("model.max_minutes_per_day: a positive cap (the day's Opus minutes)")
        if int(model.get("max_dry_run_calls_per_day") or 0) < 0:
            p.append("model.max_dry_run_calls_per_day: 0 or more")
    except (TypeError, ValueError):
        p.append("model.max_minutes_per_day / max_dry_run_calls_per_day: numbers")

    pool = {"min_ready_profiles": 2, **dict(b.get("pool") or {})}
    pool.setdefault("peer_defer_watch", {})

    gate = dict((b.get("proposals") or {}).get("output_gate") or {})
    if not gate:
        p.append("proposals.output_gate: required (the article output gate's per-site block)")

    checks = {}
    for name, spec in ((b.get("readiness") or {}).get("checks") or {}).items():
        sql = str((spec or {}).get("sql") if isinstance(spec, Mapping) else spec or "")
        if not _SELECT.match(sql):
            p.append(f"readiness.checks.{name}: one SELECT returning a count")
        checks[str(name)] = sql

    try:
        queries, forbidden = fp.parse_queries(b.get("facts"))
        if not queries:
            p.append("facts.queries: at least one query")
    except (fp.FactsPackError, KeyError) as e:
        p.append(f"facts: {e}")
        queries, forbidden = (), ()

    links = dict(b.get("links") or {})
    try:
        classes = parse_link_classes(links.get("classes"))
        if not classes:
            p.append("links.classes: at least one link class")
    except LinkClassConfigError as e:
        p.append(f"links.classes: {e}")
    families = {str(k): str(v) for k, v in (links.get("families") or {}).items()}
    for root in families:
        if not re.match(r"^/[a-z0-9-]+/$", root):
            p.append(f"links.families: {root!r} is not a root like /product/")
    indexable_sql = str(links.get("indexable_sql") or "")
    if not _SELECT.match(indexable_sql):
        p.append("links.indexable_sql: a SELECT returning (family, key) of indexable pages")

    kinds = {}
    for k, spec in ((b.get("placeholders") or {}).get("kinds") or {}).items():
        spec = dict(spec or {})
        if not _SELECT.match(str(spec.get("resolve_sql") or "")):
            p.append(f"placeholders.kinds.{k}.resolve_sql: a SELECT that returns a row when the key renders")
        kinds[str(k)] = {"resolve_sql": str(spec.get("resolve_sql") or ""), "describe": str(spec.get("describe") or "")}

    buystrip = {"max": 6, **dict(b.get("buystrip") or {})}
    if buystrip.get("eligible_sql") and not _SELECT.match(str(buystrip["eligible_sql"])):
        p.append("buystrip.eligible_sql: a SELECT returning the eligible ASINs")

    rule_sets = tuple(str(x) for x in ((b.get("content_policy") or {}).get("rule_sets") or ()))
    if not rule_sets:
        w.append("content_policy.rule_sets: empty — the site has no content policy")

    claims_block = dict(b.get("claims") or {})
    try:
        cg.parse_policy(claims_block, own_hosts=own)
    except (cg.ClaimsConfigError, fp.FactsPackError, KeyError, ValueError) as e:
        p.append(f"claims: {e}")

    arts = {"table": "editorial_articles", "category": "guides", "insert_status": "pending", "byline": "",
            **dict(b.get("articles") or {})}
    if not _IDENT.match(str(arts["table"])):
        p.append("articles.table: a SQL identifier")
    if not _STATUS.match(str(arts["insert_status"])) or arts["insert_status"] == "published":
        p.append("articles.insert_status: the gate's queue status (never 'published')")
    if not str(arts.get("byline") or "").strip():
        p.append("articles.byline: the site's byline (site.json byline.name)")

    publish = {"enabled": True, "timeout_s": 300, "extra_args": [], **dict(b.get("publish") or {})}
    sig = {"data_sql": "", "published_sql": "", **{k: str(v) for k, v in (b.get("signals") or {}).items()}}
    for k in ("data_sql", "published_sql"):
        if not _SELECT.match(sig[k]):
            p.append(f"signals.{k}: a SELECT")

    occ = {"source": "site", "list": [], **dict(b.get("occasions") or {})}
    if occ["source"] not in ("site", "calendar"):
        p.append("occasions.source: site or calendar")

    if p:
        raise SettingsError(p)
    return Settings(
        agent_id=agent_id, site=site, audience=audience, host=host, own_hosts=tuple(h for h in own if h),
        dsn_env=dsn_env, statement_timeout_ms=int(db.get("statement_timeout_ms") or 30000),
        calendar_path=cal_path, max_entries_per_run=max_entries, flags=dict(b.get("flags") or {}), schedule=schedule, pool=pool, model=model,
        occasions=occ, output_gate=gate, gate_lanes=tuple(str(x) for x in (b.get("gate_lanes") or ())),
        readiness_checks=checks, fact_queries=queries, forbidden_relations=forbidden,
        fact_params=dict((b.get("facts") or {}).get("params") or {}),
        link_classes_raw=dict(links.get("classes") or {}), link_families=families,
        static_paths=tuple(str(x) for x in (links.get("static_paths") or ())), indexable_sql=indexable_sql,
        placeholder_kinds=kinds, buystrip=buystrip, rule_sets=rule_sets, claims_block=claims_block,
        honesty_guard=bool((b.get("honesty_guard") or {"enabled": True}).get("enabled", True)),
        prompt=dict(b.get("prompt") or {}), articles=arts, publish=publish, signals=sig,
        post_publish=dict(b.get("post_publish") or {}), source=source, warnings=tuple(w))


def load_settings(spec: Optional[str] = None, *, agent_id: str,
                  environ: Optional[Mapping[str, str]] = None) -> Settings:
    import yaml
    env = os.environ if environ is None else environ
    spec = spec or env.get(CONFIG_ENV, "")
    if not spec:
        raise SettingsError([f"no config: set ${CONFIG_ENV} (PATH[#key]) or pass --site-yaml"])
    path, _, key = spec.partition("#")
    try:
        doc = yaml.safe_load(Path(path).read_text()) or {}
    except (OSError, yaml.YAMLError) as e:
        raise SettingsError([f"config {path}: {e}"]) from None
    node: Any = doc
    for part in (key or DEFAULT_BLOCK).split("."):
        node = node.get(part) if isinstance(node, Mapping) else None
    if node is None:
        raise SettingsError([f"config {path}: no block {key or DEFAULT_BLOCK!r}"])
    return build_settings(node, agent_id=agent_id, source=str(Path(path).resolve()))
