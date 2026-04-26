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
    # Register only collector + analyzer — no reporter
    register_agent(AgentManifest(id="seo-data-collector", name="Collector", category="seo"), storage=storage)
    register_agent(AgentManifest(id="seo-analyzer", name="Analyzer", category="seo"), storage=storage)
    g = build_dependency_graph(storage=storage)
    sources = {(e["from"], e["to"]) for e in g["edges"]}
    assert ("seo-data-collector", "seo-analyzer") in sources
    # The reporter edges shouldn't be in the graph
    assert ("seo-analyzer", "seo-reporter") not in sources


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
    register_agent(AgentManifest(id="seo-data-collector", name="Collector", category="seo"), storage=storage)
    register_agent(AgentManifest(
        id="seo-analyzer", name="Analyzer", category="seo",
        depends_on=[{"agent_id": "seo-data-collector", "kind": "feeds-run-dir", "description": "dup"}],
    ), storage=storage)
    g = build_dependency_graph(storage=storage)
    matches = [e for e in g["edges"]
               if e["from"] == "seo-data-collector" and e["to"] == "seo-analyzer"
               and e["kind"] == "feeds-run-dir"]
    assert len(matches) == 1


# ---------------------------------------------------------------------------
# Default edge sanity
# ---------------------------------------------------------------------------

def test_seo_pipeline_default_chain():
    sources = {(e["from"], e["to"]) for e in _DEFAULT_EDGES}
    assert ("seo-data-collector", "seo-analyzer") in sources
    assert ("seo-analyzer", "seo-reporter") in sources
    assert ("seo-reporter", "responder-agent") in sources
    assert ("responder-agent", "seo-implementer") in sources
    assert ("seo-implementer", "seo-deployer") in sources


def test_progressive_improvement_default_edges():
    sources = {(e["from"], e["to"]) for e in _DEFAULT_EDGES}
    # PI agents send email + receive responses + dispatch auto recs
    assert ("aisleprompt-progressive-improvement-agent", "responder-agent") in sources
    assert ("responder-agent", "aisleprompt-progressive-improvement-agent") in sources
    assert ("aisleprompt-progressive-improvement-agent", "seo-implementer") in sources
