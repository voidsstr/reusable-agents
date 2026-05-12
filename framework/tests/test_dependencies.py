"""Tests for framework.core.dependencies — graph builder + per-agent
override merging."""
from __future__ import annotations

from framework.core.dependencies import _DEFAULT_EDGES, build_dependency_graph
from framework.core.registry import AgentManifest, register_agent


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

def test_empty_registry_returns_empty_graph(storage):
    g = build_dependency_graph(storage=storage)
    assert g["nodes"] == []
    assert g["edges"] == []
    assert any(k["id"] == "triggers" for k in g["kinds"])


def test_default_edges_only_appear_when_endpoints_registered(storage):
    # Register only seo-opportunity-agent + responder — no implementer
    register_agent(AgentManifest(id="seo-opportunity-agent", name="SEO", category="seo"), storage=storage)
    register_agent(AgentManifest(id="responder-agent", name="Responder", category="ops"), storage=storage)
    g = build_dependency_graph(storage=storage)
    sources = {(e["from"], e["to"]) for e in g["edges"]}
    assert ("seo-opportunity-agent", "responder-agent") in sources
    # The implementer edges shouldn't be in the graph
    assert ("responder-agent", "implementer") not in sources


def test_blueprints_excluded_by_default(storage):
    register_agent(AgentManifest(
        id="progressive-improvement-agent", name="PI base",
        category="seo", metadata={"is_blueprint": True},
    ), storage=storage)
    register_agent(AgentManifest(
        id="aisleprompt-progressive-improvement-agent",
        name="AislePrompt PI", category="seo",
    ), storage=storage)
    g = build_dependency_graph(storage=storage, include_blueprints=False)
    ids = [n["id"] for n in g["nodes"]]
    assert "aisleprompt-progressive-improvement-agent" in ids
    assert "progressive-improvement-agent" not in ids


def test_blueprints_included_when_asked(storage):
    register_agent(AgentManifest(
        id="progressive-improvement-agent", name="PI base",
        category="seo", metadata={"is_blueprint": True},
    ), storage=storage)
    g = build_dependency_graph(storage=storage, include_blueprints=True)
    ids = [n["id"] for n in g["nodes"]]
    assert "progressive-improvement-agent" in ids


# ---------------------------------------------------------------------------
# Per-agent overrides
# ---------------------------------------------------------------------------

def test_manifest_depends_on_adds_edge(storage):
    register_agent(AgentManifest(id="agent-a", name="A", category="ops"), storage=storage)
    register_agent(AgentManifest(
        id="agent-b", name="B", category="ops",
        depends_on=[{"agent_id": "agent-a", "kind": "depends-on", "description": "B needs A"}],
    ), storage=storage)
    g = build_dependency_graph(storage=storage)
    edges = [(e["from"], e["to"], e["kind"]) for e in g["edges"]]
    # Convention: "B depends on A" → edge drawn A → B
    assert ("agent-a", "agent-b", "depends-on") in edges
    custom_edge = next(e for e in g["edges"] if e["from"] == "agent-a" and e["to"] == "agent-b")
    assert custom_edge["default"] is False
    assert custom_edge["description"] == "B needs A"


def test_manifest_depends_on_skips_unknown_target(storage):
    register_agent(AgentManifest(
        id="solo", name="Solo", category="ops",
        depends_on=[{"agent_id": "does-not-exist", "kind": "depends-on"}],
    ), storage=storage)
    g = build_dependency_graph(storage=storage)
    assert g["edges"] == []


def test_default_and_override_dedupe(storage):
    """If a manifest declares the same edge as a default, we don't double-emit."""
    register_agent(AgentManifest(id="seo-opportunity-agent", name="SEO", category="seo"), storage=storage)
    register_agent(AgentManifest(
        id="responder-agent", name="Responder", category="ops",
        depends_on=[{"agent_id": "seo-opportunity-agent", "kind": "queues-recs-to", "description": "dup"}],
    ), storage=storage)
    g = build_dependency_graph(storage=storage)
    matches = [e for e in g["edges"]
               if e["from"] == "seo-opportunity-agent" and e["to"] == "responder-agent"
               and e["kind"] == "queues-recs-to"]
    assert len(matches) == 1


# ---------------------------------------------------------------------------
# Default edge sanity
# ---------------------------------------------------------------------------

def test_seo_pipeline_default_chain():
    sources = {(e["from"], e["to"]) for e in _DEFAULT_EDGES}
    assert ("seo-opportunity-agent", "responder-agent") in sources
    assert ("responder-agent", "implementer") in sources
    assert ("implementer", "deployer") in sources


def test_progressive_improvement_default_edges():
    # Post-decoupling (2026-05-12): per-site agents like
    # `aisleprompt-progressive-improvement-agent` are NOT hardcoded in
    # _DEFAULT_EDGES anymore. The framework ships `*-progressive-
    # improvement-agent` templates that expand against the registered
    # agent list at query time. Verify the template entries exist; the
    # end-to-end expansion is covered by
    # test_template_expansion_against_registered below.
    sources = {(e["from"], e["to"]) for e in _DEFAULT_EDGES}
    assert ("*-progressive-improvement-agent", "responder-agent") in sources
    assert ("responder-agent", "*-progressive-improvement-agent") in sources
    assert ("*-progressive-improvement-agent", "implementer") in sources
    # And confirm those entries are flagged as templates (otherwise the
    # expander silently treats them as literal agent ids).
    templates = [e for e in _DEFAULT_EDGES if e.get("_template")]
    template_pairs = {(e["from"], e["to"]) for e in templates}
    assert ("*-progressive-improvement-agent", "implementer") in template_pairs


def test_no_site_specific_literals_in_default_edges():
    """The framework must not hardcode any site name in its default
    graph. Per-site agents flow through wildcard templates so adding
    a new site doesn't require a framework patch."""
    for e in _DEFAULT_EDGES:
        for side in (e["from"], e["to"]):
            assert "aisleprompt" not in side, (
                f"site-specific literal {side!r} in framework defaults — "
                f"convert to '*-<suffix>' template"
            )
            assert "specpicks" not in side, (
                f"site-specific literal {side!r} in framework defaults — "
                f"convert to '*-<suffix>' template"
            )
