"""Master agent registry — replaces nsc_dashboard.db's agents table with
a single JSON document in storage at `registry/agents.json`.

Append-only event log at `registry/events.jsonl` records every register /
update / delete (separate from the activity events in status.py — those
are runtime events, these are configuration events).

Concurrency: registry reads are unsynchronized; writes acquire the
storage backend's `lock("registry/agents.json")` to avoid lost updates.
For Azure Blob without a true lock, we rely on read-modify-write being
infrequent enough that conflicts are rare; an If-Match etag-based
upgrade is on the todo list.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Any, Optional

from .storage import StorageBackend, get_storage


logger = logging.getLogger("framework.registry")

REGISTRY_KEY = "registry/agents.json"


@dataclass
class AgentManifest:
    """Static + slowly-changing facts about an agent. Lives at
    registry/agents.json (denormalized) AND agents/<id>/manifest.json
    (canonical). Source of truth: agents/<id>/manifest.json."""

    id: str
    name: str
    description: str = ""
    category: str = "misc"            # seo | research | fleet | personal | ops | misc
    task_type: str = "desktop-task"   # desktop-task | cloud-routine | manual | service
    cron_expr: str = ""               # 5-field cron; empty = no schedule
    timezone: str = "UTC"
    enabled: bool = True
    repo_dir: str = ""                # absolute path to agent's working directory
    runbook_path: str = ""            # AGENT.md path
    skill_path: str = ""              # SKILL.md path
    entry_command: str = ""           # shell command to run the agent on demand
    routine_id: str = ""              # claude.ai/code routine, if applicable
    trigger_url: str = ""
    trigger_token_env: str = ""
    owner: str = ""                   # email of human owner (for confirmations)
    capabilities: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    depends_on: list[dict[str, Any]] = field(default_factory=list)
    # depends_on entries: {agent_id, kind, description?}
    # kind ∈ triggers, feeds-run-dir, polls-replies-for, routes-replies-to,
    # dispatches-to, sends-email-via, config-shared-with

    runnable_modes: list[str] = field(
        default_factory=lambda: ["cron", "manual"]
    )
    """Allowed trigger sources. Subset of:
        cron     — scheduled timer (only if cron_expr is set)
        manual   — Run-now button in dashboard / API trigger
        chained  — dispatched by another agent (response queue, run-dir feed)

    Pure pipeline-stage agents (e.g. seo-implementer, seo-deployer) are
    typically ['chained'] only — manually running them with no upstream
    payload is a footgun. The dashboard greys out the Run-now button
    when 'manual' is not present."""

    confirmation_flow: dict[str, Any] = field(default_factory=dict)
    """How this agent gates work behind a human confirmation:

        {
          "enabled": true,
          "kind":    "email-recommendations" | "per-action" | "preview-mode" | "none",
          "description": "Recs are emailed daily; user replies to choose which to ship.",
          "owner_email": "..."
        }

    Surfaced in the dashboard so it's clear at a glance whether an agent
    operates autonomously or behind an email gate. `kind=email-
    recommendations` means: agent emails ranked recs and waits for the
    user's reply (via responder-agent) before shipping. Defaults to
    empty (no confirmation gate)."""

    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "AgentManifest":
        # Tolerate extra unknown fields by filtering to known field names
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})

    def to_dict(self) -> dict:
        return asdict(self)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_registry(storage: StorageBackend) -> dict[str, dict]:
    raw = storage.read_json(REGISTRY_KEY) or {}
    if isinstance(raw, list):
        # Legacy shape — convert
        return {a["id"]: a for a in raw if "id" in a}
    return raw


def _write_registry(storage: StorageBackend, registry: dict[str, dict]) -> None:
    storage.write_json(REGISTRY_KEY, registry)


def _log_registry_event(storage: StorageBackend, action: str, agent_id: str, **kw) -> None:
    try:
        storage.append_jsonl("registry/events.jsonl", {
            "ts": _now(),
            "kind": "registry",
            "action": action,
            "agent_id": agent_id,
            **kw,
        })
    except Exception as e:
        logger.warning(f"registry event log append failed: {e}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def register_agent(manifest: AgentManifest, storage: Optional[StorageBackend] = None) -> AgentManifest:
    """Register a new agent (or upsert if id exists).

    Writes manifest to both:
      - registry/agents.json (denormalized list for fast listing)
      - agents/<id>/manifest.json (canonical per-agent location)
    """
    s = storage or get_storage()
    if not manifest.id:
        raise ValueError("manifest.id is required")
    if not manifest.name:
        manifest.name = manifest.id
    now = _now()
    if not manifest.created_at:
        manifest.created_at = now
    manifest.updated_at = now

    with s.lock(REGISTRY_KEY):
        registry = _read_registry(s)
        is_new = manifest.id not in registry
        registry[manifest.id] = manifest.to_dict()
        _write_registry(s, registry)
    s.write_json(f"agents/{manifest.id}/manifest.json", manifest.to_dict())
    _log_registry_event(s, "registered" if is_new else "updated", manifest.id)
    logger.info(f"{'registered' if is_new else 'updated'} agent: {manifest.id}")
    return manifest


def list_agents(storage: Optional[StorageBackend] = None) -> list[AgentManifest]:
    s = storage or get_storage()
    return [AgentManifest.from_dict(d) for d in _read_registry(s).values()]


def get_agent(agent_id: str, storage: Optional[StorageBackend] = None) -> Optional[AgentManifest]:
    s = storage or get_storage()
    # Prefer canonical per-agent manifest.json (might have fields the registry rollup
    # doesn't reflect yet); fall back to registry copy.
    canonical = s.read_json(f"agents/{agent_id}/manifest.json")
    if canonical:
        return AgentManifest.from_dict(canonical)
    rollup = _read_registry(s).get(agent_id)
    return AgentManifest.from_dict(rollup) if rollup else None


def update_agent(
    agent_id: str,
    patch: dict[str, Any],
    storage: Optional[StorageBackend] = None,
) -> Optional[AgentManifest]:
    """Apply a partial update. Touches both registry and canonical manifest."""
    s = storage or get_storage()
    current = get_agent(agent_id, s)
    if current is None:
        return None
    d = current.to_dict()
    d.update({k: v for k, v in patch.items() if k != "id"})
    d["updated_at"] = _now()
    new = AgentManifest.from_dict(d)
    with s.lock(REGISTRY_KEY):
        registry = _read_registry(s)
        registry[agent_id] = new.to_dict()
        _write_registry(s, registry)
    s.write_json(f"agents/{agent_id}/manifest.json", new.to_dict())
    _log_registry_event(s, "updated", agent_id, changed_fields=list(patch))
    return new


def deregister_agent(
    agent_id: str,
    storage: Optional[StorageBackend] = None,
    delete_storage: bool = False,
) -> bool:
    """Remove an agent from the registry. Does NOT delete its storage by
    default — pass delete_storage=True to wipe agents/<id>/* completely."""
    s = storage or get_storage()
    with s.lock(REGISTRY_KEY):
        registry = _read_registry(s)
        if agent_id not in registry:
            return False
        del registry[agent_id]
        _write_registry(s, registry)
    if delete_storage:
        for key in s.list_prefix(f"agents/{agent_id}/", limit=10000):
            s.delete(key)
    _log_registry_event(s, "deregistered", agent_id, deleted_storage=delete_storage)
    return True


def discover_agents_from_dir(
    agents_dir: str,
    storage: Optional[StorageBackend] = None,
) -> dict[str, int]:
    """Scan a directory of agent subdirs (each with `manifest.json`) and
    upsert all of them into the registry. Mirrors the dashboard's
    `POST /api/agents/discover` behavior, but writes to blob storage.

    Returns: {discovered: N, updated: M}
    """
    import json
    from pathlib import Path

    s = storage or get_storage()
    base = Path(agents_dir).resolve()
    if not base.is_dir():
        raise FileNotFoundError(f"agents dir not found: {base}")
    discovered = 0
    updated = 0
    for child in sorted(base.iterdir()):
        if not child.is_dir() or child.name.startswith((".", "_")):
            continue
        manifest_path = child / "manifest.json"
        if not manifest_path.is_file():
            continue
        try:
            data = json.loads(manifest_path.read_text())
        except Exception as e:
            logger.warning(f"discover: bad manifest at {manifest_path}: {e}")
            continue
        # Honor any extra fields the manifest provides
        data.setdefault("id", child.name)
        data["repo_dir"] = str(child)
        if not data.get("runbook_path") and (child / "AGENT.md").is_file():
            data["runbook_path"] = str(child / "AGENT.md")
        if not data.get("skill_path") and (child / "SKILL.md").is_file():
            data["skill_path"] = str(child / "SKILL.md")
        existing = get_agent(data["id"], s)
        register_agent(AgentManifest.from_dict(data), storage=s)
        if existing is None:
            discovered += 1
        else:
            updated += 1
    return {"discovered": discovered, "updated": updated, "scanned_dir": str(base)}
