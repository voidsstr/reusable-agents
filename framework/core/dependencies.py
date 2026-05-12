"""Agent dependency graph.

Two layers stacked at query time:

1. **Default dependencies** (this file) — relationships every framework
   instance gets out of the box. Encodes "if you have these well-known
   agents registered, here's how they're connected." Examples:
   - The SEO sub-agent pipeline (collector → analyzer → reporter →
     implementer → deployer).
   - Any email-sending agent depends on responder-agent (the responder
     polls the inbox + routes replies back to the originating agent's
     response queue).
   - progressive-improvement-agent + competitor-research-agent
     dispatch tier=auto recs to implementer.
2. **Per-agent overrides** in `manifest.depends_on` — extra edges
   specific to a custom agent. Customer repos use this to wire their
   own agents into the graph without modifying framework code.

Graph shape: a list of edge objects with `{from, to, kind, description,
default}`. Nodes are derived from the registered-agents list.

Edge kinds and what they mean visually:

| kind | meaning | suggested edge style |
|---|---|---|
| triggers | A's success kicks off B (chained execution) | solid, animated |
| feeds-run-dir | A writes a run dir B will read | solid |
| polls-replies-for | A (the responder) polls an inbox and routes B's replies | dashed |
| routes-replies-to | inverse of polls-replies-for (drawn from the responder's POV) | dashed |
| dispatches-to | A drops a payload in B's response queue (no email gate) | dotted, animated |
| sends-email-via | A sends mail; the responder polls for replies | dashed |
| config-shared-with | A and B share the same config file (loose coupling) | dotted |
"""
from __future__ import annotations

from typing import Optional

from .registry import AgentManifest, list_agents
from .storage import StorageBackend


# ---------------------------------------------------------------------------
# Default edges (only included if BOTH endpoints are registered)
# ---------------------------------------------------------------------------

_DEFAULT_EDGES: list[dict] = [
    # --- SEO pipeline (collapsed into seo-opportunity-agent — collector,
    # analyzer, finalizer are now internal phases under
    # agents/seo-opportunity-agent/lib/{collector,analyzer,reporter}/) ---
    {"from": "seo-opportunity-agent", "to": "responder-agent",
     "kind": "queues-recs-to",
     "description": "Auto-queues each rec to responder-agent's auto-queue/ dir (via framework.core.implementation_queue)."},
    {"from": "responder-agent", "to": "implementer",
     "kind": "routes-replies-to",
     "description": "Responder parses replies + drains auto-queue, dispatching to the implementer."},
    {"from": "implementer", "to": "deployer",
     "kind": "triggers",
     "description": "Implementer commits + tests; deployer ships if site config has a deployer block."},

    # --- Per-site agent edge TEMPLATES ---
    # 2026-05-12 decoupling: replaced hardcoded "aisleprompt-*" and
    # "specpicks-*" entries with `*-<suffix>` templates. build_dependency_graph
    # expands `*` against every registered agent whose id matches the
    # suffix, so the framework no longer knows about specific sites by
    # name. Add new site simply by registering its `<site>-<suffix>`
    # agent — the dashboard picks up the edges automatically. Templates
    # are marked `_template: True` so the expander knows to walk them.
    {"from": "*-progressive-improvement-agent", "to": "responder-agent",
     "kind": "sends-email-via", "_template": True,
     "description": "Sends ranked recs email; responder routes 'implement rec-NNN' replies back."},
    {"from": "responder-agent", "to": "*-progressive-improvement-agent",
     "kind": "routes-replies-to", "_template": True,
     "description": "Drops parsed replies in the agent's response queue."},
    {"from": "*-progressive-improvement-agent", "to": "implementer",
     "kind": "dispatches-to", "_template": True,
     "description": "Auto-tier recs (only when site config opts into auto_implement) dispatch to the implementer."},

    {"from": "*-competitor-research-agent", "to": "responder-agent",
     "kind": "sends-email-via", "_template": True,
     "description": "Sends ranked recs email; responder routes replies back."},
    {"from": "responder-agent", "to": "*-competitor-research-agent",
     "kind": "routes-replies-to", "_template": True,
     "description": "Drops parsed replies in the agent's response queue."},
    {"from": "*-competitor-research-agent", "to": "implementer",
     "kind": "dispatches-to", "_template": True,
     "description": "Auto-tier recs dispatch to the implementer."},

    {"from": "*-catalog-audit-agent", "to": "implementer",
     "kind": "dispatches-to", "_template": True,
     "description": "Catalog-audit migrations dispatch to the implementer for ship."},
    {"from": "*-seo-opportunity-agent", "to": "implementer",
     "kind": "dispatches-to", "_template": True,
     "description": "SEO opportunity recs dispatch to the implementer."},
    {"from": "*-article-author-agent", "to": "implementer",
     "kind": "dispatches-to", "_template": True,
     "description": "Article-author proposals dispatch to the implementer for body write + DB insert."},

    # --- Daily-briefing chain ---
    {"from": "daily-briefing-calendar-agent", "to": "email-monitor",
     "kind": "config-shared-with",
     "description": "Both agents read the same calendar/email auth config."},
]


# ---------------------------------------------------------------------------
# Graph build
# ---------------------------------------------------------------------------

def _agent_to_node(m: AgentManifest) -> dict:
    return {
        "id": m.id,
        "name": m.name,
        "category": m.category,
        "enabled": m.enabled,
        "is_blueprint": bool(m.metadata.get("is_blueprint")),
        "blueprint": m.metadata.get("blueprint", ""),
        "owner": m.owner,
        "cron": m.cron_expr,
    }


def build_dependency_graph(
    storage: Optional[StorageBackend] = None,
    *,
    include_blueprints: bool = False,
) -> dict:
    """Return {nodes, edges} where:
       - nodes are the registered agents (minus blueprints by default)
       - edges are the union of:
           1. _DEFAULT_EDGES whose endpoints are both registered
           2. each agent's `manifest.depends_on` entries
    """
    agents = list_agents(storage=storage)
    if not include_blueprints:
        agents = [a for a in agents if not a.metadata.get("is_blueprint")]
    by_id = {a.id: a for a in agents}

    nodes = [_agent_to_node(a) for a in agents]

    edges: list[dict] = []
    seen_keys: set[tuple[str, str, str]] = set()

    # 1. Defaults — only include if both endpoints exist. Templates
    # (entries with _template=True) expand "*-suffix" into every
    # registered agent whose id ends with "-suffix". This is how the
    # graph picks up per-site agents (aisleprompt-progressive-..., etc.)
    # without the framework knowing those sites by name.
    def _expand(side: str, agent_ids: list[str]) -> list[str]:
        if not side.startswith("*-"):
            return [side]
        suffix = side[2:]  # everything after "*-"
        return sorted(aid for aid in agent_ids if aid.endswith("-" + suffix))

    registered_ids = list(by_id.keys())
    for e in _DEFAULT_EDGES:
        if e.get("_template"):
            for f in _expand(e["from"], registered_ids):
                for t in _expand(e["to"], registered_ids):
                    if f not in by_id or t not in by_id or f == t:
                        continue
                    k = e["kind"]
                    key = (f, t, k)
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    edges.append({
                        "from": f, "to": t, "kind": k,
                        "description": e.get("description", ""),
                        "default": True,
                    })
            continue
        f, t, k = e["from"], e["to"], e["kind"]
        if f not in by_id or t not in by_id:
            continue
        key = (f, t, k)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        edges.append({**e, "default": True})

    # 2. Per-agent overrides — manifest.depends_on
    # An entry on agent A means: A depends on entry.agent_id.
    # Convention: edge direction is FROM dependency-target TO A
    # (i.e. "A depends on B" → drawn as B → A) so the graph reads
    # "data flows from B to A".
    for a in agents:
        for dep in (a.depends_on or []):
            target = dep.get("agent_id")
            kind = dep.get("kind", "depends-on")
            description = dep.get("description", "")
            if not target or target not in by_id:
                continue
            key = (target, a.id, kind)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            edges.append({
                "from": target, "to": a.id,
                "kind": kind, "description": description,
                "default": False,
            })

    return {
        "nodes": nodes,
        "edges": edges,
        "kinds": [
            {"id": "triggers", "label": "triggers (chained)", "style": "solid-animated"},
            {"id": "feeds-run-dir", "label": "feeds run dir", "style": "solid"},
            {"id": "sends-email-via", "label": "sends email via", "style": "dashed"},
            {"id": "polls-replies-for", "label": "polls replies for", "style": "dashed"},
            {"id": "routes-replies-to", "label": "routes replies to", "style": "dashed"},
            {"id": "dispatches-to", "label": "auto-dispatches to", "style": "dotted-animated"},
            {"id": "config-shared-with", "label": "shares config", "style": "dotted"},
            {"id": "depends-on", "label": "depends on", "style": "solid"},
        ],
    }
