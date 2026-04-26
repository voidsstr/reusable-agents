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
     dispatch tier=auto recs to seo-implementer.
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
    # --- SEO pipeline ---
    {"from": "seo-data-collector", "to": "seo-analyzer",
     "kind": "feeds-run-dir",
     "description": "Collector writes raw GSC + GA4 + DB data; analyzer reads it."},
    {"from": "seo-analyzer", "to": "seo-reporter",
     "kind": "feeds-run-dir",
     "description": "Analyzer writes recommendations.json; reporter reads + emails."},
    {"from": "seo-reporter", "to": "responder-agent",
     "kind": "sends-email-via",
     "description": "Reporter emails the recipient; responder polls the inbox for replies."},
    {"from": "responder-agent", "to": "seo-implementer",
     "kind": "routes-replies-to",
     "description": "Responder parses 'implement rec-NNN' replies and dispatches to the implementer."},
    {"from": "seo-implementer", "to": "seo-deployer",
     "kind": "triggers",
     "description": "Implementer commits + tests; deployer ships if site config has a deployer block."},

    # --- SEO orchestrator ---
    {"from": "seo-opportunity-agent", "to": "seo-data-collector",
     "kind": "triggers",
     "description": "Daily orchestrator chains the full SEO pipeline."},
    {"from": "seo-opportunity-agent", "to": "seo-analyzer",
     "kind": "triggers", "description": "Pipeline stage."},
    {"from": "seo-opportunity-agent", "to": "seo-reporter",
     "kind": "triggers", "description": "Pipeline stage."},

    # --- progressive-improvement-agent (per-site instances) ---
    {"from": "aisleprompt-progressive-improvement-agent", "to": "responder-agent",
     "kind": "sends-email-via",
     "description": "Sends ranked recs email; responder routes 'implement rec-NNN' replies back."},
    {"from": "responder-agent", "to": "aisleprompt-progressive-improvement-agent",
     "kind": "routes-replies-to",
     "description": "Drops parsed replies in the agent's response queue."},
    {"from": "aisleprompt-progressive-improvement-agent", "to": "seo-implementer",
     "kind": "dispatches-to",
     "description": "Auto-tier recs (only when site config opts into auto_implement) dispatch to the implementer."},

    {"from": "specpicks-progressive-improvement-agent", "to": "responder-agent",
     "kind": "sends-email-via",
     "description": "Sends ranked recs email; responder routes 'implement rec-NNN' replies back."},
    {"from": "responder-agent", "to": "specpicks-progressive-improvement-agent",
     "kind": "routes-replies-to",
     "description": "Drops parsed replies in the agent's response queue."},
    {"from": "specpicks-progressive-improvement-agent", "to": "seo-implementer",
     "kind": "dispatches-to",
     "description": "Auto-tier recs dispatch to the implementer."},

    # --- competitor-research-agent (per-site instances) ---
    {"from": "aisleprompt-competitor-research-agent", "to": "responder-agent",
     "kind": "sends-email-via",
     "description": "Sends ranked recs email; responder routes replies back."},
    {"from": "responder-agent", "to": "aisleprompt-competitor-research-agent",
     "kind": "routes-replies-to",
     "description": "Drops parsed replies in the agent's response queue."},
    {"from": "aisleprompt-competitor-research-agent", "to": "seo-implementer",
     "kind": "dispatches-to",
     "description": "Auto-tier recs dispatch to the implementer (rare for competitor recs by design)."},

    {"from": "specpicks-competitor-research-agent", "to": "responder-agent",
     "kind": "sends-email-via",
     "description": "Sends ranked recs email; responder routes replies back."},
    {"from": "responder-agent", "to": "specpicks-competitor-research-agent",
     "kind": "routes-replies-to",
     "description": "Drops parsed replies in the agent's response queue."},
    {"from": "specpicks-competitor-research-agent", "to": "seo-implementer",
     "kind": "dispatches-to",
     "description": "Auto-tier recs dispatch to the implementer."},

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

    # 1. Defaults — only include if both endpoints exist
    for e in _DEFAULT_EDGES:
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
