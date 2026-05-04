"""Agent registry CRUD + discovery + trigger."""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from framework.core import registry, scheduler
from framework.core.storage import get_storage

from ..auth import require_token


router = APIRouter(prefix="/api/agents", tags=["agents"], dependencies=[Depends(require_token)])

# Ghost-reap TTL state: only run reap_all once per minute, in a background
# thread, so list_all stays fast even when storage round-trips are slow.
from concurrent.futures import ThreadPoolExecutor as _ReapExecutor
_LAST_REAP_AT: float = 0.0
_REAP_POOL = _ReapExecutor(max_workers=1)


# ---------------------------------------------------------------------------
# Module-level 60s TTL cache for the AI provider/defaults config blobs.
# Both `list_all` and `get_one` resolve `_summary` for one or more agents
# and previously read these two blobs on every request — that's 2 Azure
# round-trips for the hot detail path, and N+1 for the (already-fixed) list.
# Now: at most one fetch per 60s, lock-protected.
# ---------------------------------------------------------------------------

_CFG_CACHE: dict = {"providers": None, "defaults": None}
_CFG_CACHE_TS: float = 0.0
_CFG_CACHE_LOCK = threading.Lock()
_CFG_CACHE_TTL_S = 60.0


def _get_config_caches(force_refresh: bool = False) -> tuple[dict, dict]:
    """Return (providers, defaults) JSON dicts, cached for 60s.

    On read error, keeps the existing stale cache rather than blanking out —
    the dashboard's AI badges will lag rather than disappear if blob storage
    hiccups.
    """
    global _CFG_CACHE_TS, _CFG_CACHE
    now = time.time()
    if (not force_refresh
            and (now - _CFG_CACHE_TS) < _CFG_CACHE_TTL_S
            and _CFG_CACHE["providers"] is not None):
        return _CFG_CACHE["providers"], _CFG_CACHE["defaults"]
    with _CFG_CACHE_LOCK:
        # Double-check under lock
        now = time.time()
        if (not force_refresh
                and (now - _CFG_CACHE_TS) < _CFG_CACHE_TTL_S
                and _CFG_CACHE["providers"] is not None):
            return _CFG_CACHE["providers"], _CFG_CACHE["defaults"]
        try:
            s = get_storage()
            providers = s.read_json("config/ai-providers.json") or {}
            defaults = s.read_json("config/ai-defaults.json") or {}
            _CFG_CACHE = {"providers": providers, "defaults": defaults}
            _CFG_CACHE_TS = now
        except Exception:
            # On error: keep stale cache rather than blanking
            if _CFG_CACHE["providers"] is None:
                _CFG_CACHE = {"providers": {}, "defaults": {}}
                _CFG_CACHE_TS = now
        return _CFG_CACHE["providers"], _CFG_CACHE["defaults"]


def _age_seconds(iso_ts: str) -> float:
    """Age in seconds of an ISO-8601 UTC timestamp. Returns +inf on error."""
    if not iso_ts:
        return float("inf")
    try:
        # Tolerate both ...Z and ...+00:00
        s = iso_ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except Exception:
        return float("inf")


class AgentSummary(BaseModel):
    id: str
    name: str
    description: str = ""
    category: str = "misc"
    task_type: str = "desktop-task"
    cron_expr: str = ""
    timezone: str = "UTC"
    enabled: bool = True
    owner: str = ""
    last_run_status: str = ""
    last_run_at: Optional[str] = None
    next_run_at: Optional[str] = None
    runnable_modes: list[str] = Field(default_factory=lambda: ["cron", "manual"])
    confirmation_flow: dict = Field(default_factory=dict)
    application: str = "shared"  # derived: which app/site this agent serves
    # AI-provider summary — dashboard shows a "Uses Claude" / "Script-only" /
    # "Ollama" badge so it's at-a-glance which agents incur LLM cost vs which
    # are pure cron jobs.
    ai_provider: str = ""        # e.g. "claude-cli", "ollama-local", "" for none
    ai_kind: str = ""            # e.g. "claude-cli", "ollama", "anthropic"
    ai_model: str = ""           # default model
    ai_uses_claude: bool = False # convenience flag — uses Claude (any path)
    # The agent's MANIFEST-declared AI config, before any override is
    # applied. Populated from manifest.metadata.ai. Surfaced here so
    # the /llms page can show override-vs-manifest source without doing
    # an N+1 follow-up GET /api/agents/<id> per agent (was ~15s for 39
    # agents on production).
    ai_manifest_provider: str = ""
    ai_manifest_model: str = ""
    # Why the effective provider+model was picked: "override" |
    # "manifest" | "default" | "unset". Same resolution order as
    # framework.core.ai_providers.ai_client_for().
    ai_source: str = ""


class AgentDetail(AgentSummary):
    repo_dir: str = ""
    runbook_path: str = ""
    skill_path: str = ""
    entry_command: str = ""
    capabilities: list[str] = Field(default_factory=list)
    capabilities_detail: list[dict] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)
    depends_on: list[dict] = Field(default_factory=list)
    runbook_body: Optional[str] = None
    skill_body: Optional[str] = None
    readme_body: Optional[str] = None
    current_status: Optional[dict] = None
    recent_runs: list[dict] = Field(default_factory=list)
    # Live state of the confirmation gate (if confirmation_flow.enabled).
    # For schema-mapping-approval kind, populated from the agent's
    # mapping doc — surfaces approved_at + approved_by so the UI
    # banner can show "✓ approved" instead of the static "first run
    # emails proposal" text once approval has actually landed.
    confirmation_status: dict = Field(default_factory=dict)


class RegisterRequest(BaseModel):
    id: str
    name: str = ""
    description: str = ""
    category: str = "misc"
    task_type: str = "desktop-task"
    cron_expr: str = ""
    timezone: str = "UTC"
    enabled: bool = True
    repo_dir: str = ""
    runbook_path: str = ""
    skill_path: str = ""
    entry_command: str = ""
    owner: str = ""
    autowire_cron: bool = True
    metadata: dict = Field(default_factory=dict)
    depends_on: list[dict] = Field(default_factory=list)
    runnable_modes: list[str] = Field(default_factory=lambda: ["cron", "manual"])
    confirmation_flow: dict = Field(default_factory=dict)
    # Optional: file contents embedded by register-agent.sh (so the API,
    # which lives in a Docker container, can render them without bind-
    # mounting the host's filesystem).
    runbook_body: Optional[str] = None
    skill_body: Optional[str] = None
    readme_body: Optional[str] = None


class PatchRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    cron_expr: Optional[str] = None
    timezone: Optional[str] = None
    enabled: Optional[bool] = None
    entry_command: Optional[str] = None
    owner: Optional[str] = None
    runbook_body: Optional[str] = None
    skill_body: Optional[str] = None


class TriggerResponse(BaseModel):
    ok: bool
    run_id: str = ""
    detail: str = ""


class DiscoverRequest(BaseModel):
    agents_dir: str


def _derive_application(m: registry.AgentManifest) -> str:
    """Group an agent under an 'application' for the dashboard filter.

    Resolution order:
      1. manifest.metadata.site         (explicit, the new PI/CR agents set this)
      2. manifest.metadata.application  (explicit override)
      3. id prefix mapping              (legacy agents)
      4. category-based fallback
      5. 'shared'                       (framework-level / cross-cutting)
    """
    md = m.metadata or {}
    if md.get("site"):
        return str(md["site"])
    if md.get("application"):
        return str(md["application"])
    # id-prefix mapping for the well-known apps
    aid = m.id.lower()
    for prefix, app in [
        ("aisleprompt-", "aisleprompt"),
        ("specpicks-",   "specpicks"),
        ("reusable-agents-", "reusable-agents"),
    ]:
        if aid.startswith(prefix):
            return app
    # Whole-id matches for orchestrators that span multiple sites
    if aid == "seo-opportunity-agent":
        return "seo-pipeline"
    if aid in ("seo-data-collector", "seo-analyzer", "seo-reporter",
                "implementer", "seo-deployer"):
        return "seo-pipeline"
    if aid == "responder-agent":
        return "shared"
    # Category-based
    if m.category == "fleet":
        return "retro-fleet"
    if aid.startswith("retro-"):
        return "retro-fleet"
    if aid.startswith("travel-"):
        return "personal"
    return m.category or "shared"


def _resolve_ai_summary(agent_id: str,
                         providers_cache: dict | None = None,
                         defaults_cache: dict | None = None) -> tuple[str, str, str, bool]:
    """Best-effort resolve the agent's configured AI provider for the
    dashboard badge. Returns (provider_name, kind, model, uses_claude).

    The list endpoint resolves N agents at once; pass providers_cache +
    defaults_cache (read once at the top of the call) so each agent
    doesn't trigger its own Azure-blob round-trip. Without this, the
    list endpoint took ~30s on Azure storage (~600ms per blob read × 2
    config blobs × 25 agents = 30s)."""
    try:
        from framework.core import ai_providers as _ap
        if providers_cache is not None and defaults_cache is not None:
            # In-process resolve using the pre-fetched config blobs
            override = (defaults_cache.get("agent_overrides", {}) or {}).get(agent_id, {})
            provider_name = override.get("provider") or defaults_cache.get("default_provider", "")
            model = override.get("model") or defaults_cache.get("default_model", "")
            if not provider_name:
                return "", "", "", False
            pdict = (providers_cache or {}).get(provider_name)
            if not pdict:
                return "", "", "", False
            kind = pdict.get("kind", "")
            if not model:
                model = pdict.get("default_model", "")
            uses_claude = (
                kind in ("claude-cli", "anthropic")
                or "claude" in (model or "").lower()
                or "claude" in provider_name.lower()
            )
            return provider_name, kind, model or "", bool(uses_claude)
        # Slow path — single agent, no cache
        provider, model = _ap.resolve_for_agent(agent_id)
    except Exception:
        return "", "", "", False
    if provider is None:
        return "", "", "", False
    name = getattr(provider, "name", "")
    kind = getattr(provider, "kind", "")
    uses_claude = (
        kind in ("claude-cli", "anthropic")
        or "claude" in (model or "").lower()
        or "claude" in name.lower()
    )
    return name, kind, model or "", bool(uses_claude)


def _summary(m: registry.AgentManifest,
              providers_cache: dict | None = None,
              defaults_cache: dict | None = None,
              status: dict | None = None) -> AgentSummary:
    """Build one AgentSummary. `status` (dict) can be passed pre-fetched
    so the caller can parallelize blob reads — list_all() does this to
    avoid 29 serial Azure roundtrips."""
    if status is None:
        s = get_storage()
        status = s.read_json(f"agents/{m.id}/status.json") or {}
    ai_name, ai_kind, ai_model, uses_claude = _resolve_ai_summary(
        m.id, providers_cache=providers_cache, defaults_cache=defaults_cache,
    )
    # Manifest-declared AI config (from manifest.metadata.ai). Useful
    # for the /llms page to show override-vs-manifest source without
    # an N+1 GET /api/agents/<id> per agent.
    metadata = getattr(m, "metadata", None) or {}
    if not isinstance(metadata, dict):
        metadata = {}
    manifest_ai = metadata.get("ai") or {}
    if not isinstance(manifest_ai, dict):
        manifest_ai = {}
    manifest_provider = str(manifest_ai.get("provider") or "")
    manifest_model = str(manifest_ai.get("model") or "")
    # Source resolution mirrors framework.core.ai_providers.ai_client_for():
    #   override > manifest > default > unset
    overrides = (defaults_cache or {}).get("agent_overrides") or {}
    ovr = overrides.get(m.id) or {}
    if isinstance(ovr, dict) and (ovr.get("provider") or ovr.get("model")):
        ai_source = "override"
    elif manifest_provider or manifest_model:
        ai_source = "manifest"
    elif (defaults_cache or {}).get("default_provider"):
        ai_source = "default"
    else:
        ai_source = "unset"
    return AgentSummary(
        id=m.id, name=m.name, description=m.description, category=m.category,
        task_type=m.task_type, cron_expr=m.cron_expr, timezone=m.timezone,
        enabled=m.enabled, owner=m.owner,
        last_run_status=status.get("state", ""),
        last_run_at=status.get("updated_at"),
        runnable_modes=list(m.runnable_modes or ["cron", "manual"]),
        confirmation_flow=dict(m.confirmation_flow or {}),
        application=_derive_application(m),
        ai_provider=ai_name,
        ai_kind=ai_kind,
        ai_model=ai_model,
        ai_uses_claude=uses_claude,
        ai_manifest_provider=manifest_provider,
        ai_manifest_model=manifest_model,
        ai_source=ai_source,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("", response_model=list[AgentSummary])
def list_all():
    s = get_storage()
    # Module-level 60s cache — eliminates the 2 per-request blob reads.
    providers_cache, defaults_cache = _get_config_caches()
    manifests = list(registry.list_agents())

    # Lazy ghost-run reap — TTL-rate-limited because reading 30 status.json
    # blobs sequentially costs ~3s, which used to dominate the list_all
    # endpoint's 3.5s response time. Reap is a best-effort cleanup; running
    # it once a minute is plenty (a phantom "● tailing" badge that takes
    # 60s to clear is fine).
    import time as _t
    global _LAST_REAP_AT
    now_mono = _t.monotonic()
    if now_mono - _LAST_REAP_AT > 60.0:
        _LAST_REAP_AT = now_mono
        try:
            from framework.core.ghost_reaper import reap_all
            from concurrent.futures import ThreadPoolExecutor
            # Fire-and-forget: don't block the response on reap completion.
            _REAP_POOL.submit(reap_all, storage=s)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Status fetch:
    #   1. Try the single-blob snapshot (registry/agent-snapshot.json),
    #      written every 5s by snapshot_updater. One blob read replaces
    #      N parallel reads.
    #   2. If the snapshot is missing or stale (>30s old), fall back to
    #      the existing parallel-read path — never break if the updater
    #      thread is down.
    # ------------------------------------------------------------------
    statuses: dict[str, dict] = {}
    snap = s.read_json("registry/agent-snapshot.json") or {}
    snap_age_s = _age_seconds(snap.get("updated_at", ""))
    use_snapshot = snap_age_s < 30.0 and bool(snap.get("agents"))
    if use_snapshot:
        statuses = snap.get("agents") or {}
    else:
        # Fallback: parallel-read each agent's status.json.
        from concurrent.futures import ThreadPoolExecutor
        def _fetch_status(agent_id: str) -> tuple[str, dict]:
            try:
                return agent_id, (s.read_json(f"agents/{agent_id}/status.json") or {})
            except Exception:
                return agent_id, {}
        with ThreadPoolExecutor(max_workers=16) as ex:
            for aid, st in ex.map(_fetch_status, [m.id for m in manifests]):
                statuses[aid] = st

    return [
        _summary(m, providers_cache=providers_cache,
                 defaults_cache=defaults_cache, status=statuses.get(m.id, {}))
        for m in manifests
    ]


@router.post("/register", response_model=AgentSummary)
def register(req: RegisterRequest):
    s = get_storage()
    manifest = registry.AgentManifest(
        id=req.id, name=req.name or req.id, description=req.description,
        category=req.category, task_type=req.task_type, cron_expr=req.cron_expr,
        timezone=req.timezone, enabled=req.enabled, repo_dir=req.repo_dir,
        runbook_path=req.runbook_path, skill_path=req.skill_path,
        entry_command=req.entry_command, owner=req.owner,
        metadata=req.metadata,
        depends_on=list(req.depends_on or []),
        runnable_modes=list(req.runnable_modes or ["cron", "manual"]),
        confirmation_flow=dict(req.confirmation_flow or {}),
    )
    registry.register_agent(manifest, storage=s)
    # Persist embedded markdown content so the dashboard can render it
    # without needing to bind-mount the host's filesystem.
    if req.runbook_body:
        s.write_text(f"agents/{req.id}/runbook.md", req.runbook_body)
    if req.skill_body:
        s.write_text(f"agents/{req.id}/skill.md", req.skill_body)
    if req.readme_body:
        s.write_text(f"agents/{req.id}/readme.md", req.readme_body)
    if req.autowire_cron and req.cron_expr and req.entry_command:
        try:
            # Forward storage env vars so agent service units inherit them —
            # otherwise dispatched sub-processes (e.g. implementer) default to
            # local storage even when Azure is configured.
            _storage_env = {
                k: os.environ[k]
                for k in ("AZURE_STORAGE_CONNECTION_STRING", "AZURE_STORAGE_CONTAINER",
                           "STORAGE_BACKEND")
                if os.environ.get(k)
            }
            scheduler.write_systemd_units(
                agent_id=req.id, cron_expr=req.cron_expr,
                entry_command=req.entry_command,
                working_directory=req.repo_dir or os.path.expanduser("~"),
                timezone=req.timezone,
                extra_env=_storage_env or None,
            )
            scheduler.reload_and_enable(req.id)
        except Exception as e:
            # Don't fail the register on systemd issues; surface in detail
            return AgentSummary(**{**_summary(manifest).dict(), "owner": f"(autowire: {e})"})
    return _summary(manifest)


@router.post("/discover")
def discover(req: DiscoverRequest):
    """Walk a directory of agent subdirs, register each."""
    try:
        result = registry.discover_agents_from_dir(req.agents_dir)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"ok": True, **result}


_GET_ONE_CACHE: dict[str, tuple[float, "AgentDetail"]] = {}
_GET_ONE_TTL_S = 5.0  # tight enough that toggle-state shows up quickly,
                      # loose enough to absorb tab clicks + back/forward
                      # nav without re-loading 2.6s of run-list + markdown


@router.get("/{agent_id}", response_model=AgentDetail)
def get_one(agent_id: str):
    import time as _t
    now = _t.monotonic()
    cached = _GET_ONE_CACHE.get(agent_id)
    if cached and (now - cached[0]) < _GET_ONE_TTL_S:
        return cached[1]
    m = registry.get_agent(agent_id)
    if m is None:
        raise HTTPException(status_code=404, detail=f"unknown agent {agent_id!r}")
    s = get_storage()
    # Configs come from the module-level 60s TTL cache.
    providers_cache, defaults_cache = _get_config_caches()

    status = s.read_json(f"agents/{agent_id}/status.json")
    runs = []
    runs_prefix = f"agents/{agent_id}/runs/"
    # Only fetch the 5 most recent runs in the detail load. The Runs tab
    # has its own listRuns endpoint for the full history. Cuts 15
    # sequential Azure reads — the page's biggest latency source.
    run_keys = sorted(
        (k for k in s.list_prefix(runs_prefix) if k.endswith("/progress.json")),
        reverse=True,
    )[:5]
    for key in run_keys:
        rd = s.read_json(key)
        if rd:
            runs.append(rd)

    # Markdown content: prefer storage (embedded at register-time) over
    # host filesystem (only works when API isn't containerized).
    def _load_md(storage_key: str, host_path: str) -> Optional[str]:
        v = s.read_text(storage_key)
        if v: return v
        if host_path and Path(host_path).is_file():
            try: return Path(host_path).read_text()
            except Exception: return None
        return None

    runbook_body = _load_md(f"agents/{m.id}/runbook.md", m.runbook_path)
    skill_body   = _load_md(f"agents/{m.id}/skill.md", m.skill_path)
    readme_body  = _load_md(f"agents/{m.id}/readme.md", "")

    # Resolve confirmation_flow current state — for schema-mapping-approval
    # kind, look up the agent's mapping doc to detect whether the schema
    # has actually been approved yet. Without this the banner stays
    # forever stuck on "first run emails proposal" even after approval.
    confirmation_status: dict = {}
    cf = m.confirmation_flow or {}
    if cf.get("enabled") and cf.get("kind") == "schema-mapping-approval":
        # Mapping docs live at agents/<id>/mappings/<site_id>.json. We
        # don't always know the site_id from the manifest, so list the
        # mappings/ prefix and pick the most recently-approved doc.
        try:
            best_at = ""
            best_by = ""
            for k in s.list_prefix(f"agents/{m.id}/mappings/"):
                if not k.endswith(".json"):
                    continue
                doc = s.read_json(k) or {}
                at = doc.get("approved_at") or ""
                if at and at > best_at:
                    best_at = at
                    best_by = doc.get("approved_by") or ""
            if best_at:
                confirmation_status = {
                    "approved": True,
                    "approved_at": best_at,
                    "approved_by": best_by,
                }
            else:
                confirmation_status = {"approved": False}
        except Exception:
            confirmation_status = {"approved": None}

    base = _summary(m, providers_cache=providers_cache, defaults_cache=defaults_cache).dict()
    detail = AgentDetail(
        **base,
        repo_dir=m.repo_dir,
        runbook_path=m.runbook_path,
        skill_path=m.skill_path,
        entry_command=m.entry_command,
        capabilities=m.capabilities,
        capabilities_detail=(m.metadata or {}).get("capabilities", []),
        metadata=m.metadata,
        depends_on=list(m.depends_on or []),
        runbook_body=runbook_body,
        skill_body=skill_body,
        readme_body=readme_body,
        current_status=status,
        recent_runs=runs,
        confirmation_status=confirmation_status,
    )
    _GET_ONE_CACHE[agent_id] = (now, detail)
    return detail


@router.patch("/{agent_id}", response_model=AgentSummary)
def patch(agent_id: str, req: PatchRequest):
    s = get_storage()
    m = registry.get_agent(agent_id, storage=s)
    if m is None:
        raise HTTPException(status_code=404, detail=f"unknown agent {agent_id!r}")
    # Body content edits (runbook / skill) write to disk, not the manifest
    if req.runbook_body is not None and m.runbook_path:
        Path(m.runbook_path).parent.mkdir(parents=True, exist_ok=True)
        Path(m.runbook_path).write_text(req.runbook_body)
    if req.skill_body is not None and m.skill_path:
        Path(m.skill_path).parent.mkdir(parents=True, exist_ok=True)
        Path(m.skill_path).write_text(req.skill_body)

    patch_dict = {
        k: v for k, v in req.dict().items()
        if k in {"name", "description", "category", "cron_expr", "timezone",
                 "enabled", "entry_command", "owner"}
        and v is not None
    }
    if patch_dict:
        registry.update_agent(agent_id, patch_dict, storage=s)

    _bust_get_one_cache(agent_id)
    return _summary(registry.get_agent(agent_id, storage=s))


def _bust_get_one_cache(agent_id: str) -> None:
    """Invalidate the /api/agents/<id> TTL cache entry so the next
    GET reflects an enable/disable/patch/trigger we just performed."""
    _GET_ONE_CACHE.pop(agent_id, None)


@router.post("/{agent_id}/enable")
def enable(agent_id: str):
    m = registry.update_agent(agent_id, {"enabled": True})
    if m is None: raise HTTPException(status_code=404, detail="unknown agent")
    _bust_get_one_cache(agent_id)
    return {"ok": True, "enabled": True}


@router.post("/{agent_id}/disable")
def disable(agent_id: str):
    m = registry.update_agent(agent_id, {"enabled": False})
    if m is None: raise HTTPException(status_code=404, detail="unknown agent")
    _bust_get_one_cache(agent_id)
    return {"ok": True, "enabled": False}


@router.delete("/{agent_id}")
def deregister(agent_id: str, delete_storage: bool = False):
    ok = registry.deregister_agent(agent_id, delete_storage=delete_storage)
    if not ok: raise HTTPException(status_code=404, detail="unknown agent")
    _bust_get_one_cache(agent_id)
    # Best-effort: remove systemd timer if it was autowired
    try: scheduler.remove_systemd_units(agent_id)
    except Exception: pass
    return {"ok": True, "deleted_storage": delete_storage}


@router.post("/{agent_id}/trigger", response_model=TriggerResponse)
def trigger(agent_id: str):
    """Enqueue an agent run by writing a job file the host-worker picks up.

    The host-worker (`framework/api/host_worker.sh` or its predecessor in
    nsc-assistant/agents/lib/host-worker.sh) polls /tmp/agent-trigger-queue/
    every 2s and exec's the agent on the host (where it has access to
    docker, git, az, etc. — the API container can't.)
    """
    m = registry.get_agent(agent_id)
    if m is None: raise HTTPException(status_code=404, detail="unknown agent")
    if not m.entry_command:
        raise HTTPException(status_code=400, detail="agent has no entry_command")
    # Gate: agent must allow manual triggering.
    if m.runnable_modes and "manual" not in m.runnable_modes:
        raise HTTPException(
            status_code=409,
            detail=(
                f"agent {agent_id} is not manually runnable "
                f"(runnable_modes={m.runnable_modes}). It is queue-driven — "
                "an upstream agent dispatches work to it. See the dependency "
                "graph for incoming edges."
            ),
        )

    queue_dir = Path(os.getenv("AGENT_TRIGGER_QUEUE_DIR", "/tmp/agent-trigger-queue"))
    queue_dir.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    job = {
        "agent_id": agent_id,
        "run_id": run_id,
        "entry_command": m.entry_command,
        "triggered_by": "manual:api",
        "enqueued_at": datetime.now(timezone.utc).isoformat(),
    }
    job_path = queue_dir / f"{agent_id}-{run_id}.json"
    tmp = job_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(job, indent=2))
    tmp.rename(job_path)
    return TriggerResponse(ok=True, run_id=run_id,
                           detail=f"queued at {job_path}")
