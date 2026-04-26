"""Tests for shared/site_quality.py — config loader, tier scoring, reply
parsing (precise + bulk filters), email rendering, response application,
auto-dispatch.
"""
import json
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Config loading + validation
# ---------------------------------------------------------------------------

def test_load_quality_config_minimal(tmp_path):
    from shared.site_quality import load_quality_config
    cfg_path = tmp_path / "site.yaml"
    cfg_path.write_text("""
site:
  id: smoke-site
  domain: smoke.example.com
""")
    cfg = load_quality_config(cfg_path)
    assert cfg.site_id == "smoke-site"
    assert cfg.domain == "smoke.example.com"
    assert cfg.base_url == "https://smoke.example.com"
    assert cfg.label == "smoke-site"
    assert cfg.what_we_do == ""


def test_load_quality_config_full(tmp_path):
    from shared.site_quality import load_quality_config
    cfg_path = tmp_path / "site.yaml"
    cfg_path.write_text("""
site:
  id: full-site
  domain: full.example.com
  base_url: https://full.example.com
  label: Full Site
  what_we_do: |
    Test description.
crawler:
  seed_urls: ["/a", "/b"]
  max_depth: 1
  max_pages: 5
analyzer:
  max_recs_per_run: 10
  auto_implement_threshold: 0.9
auto_implement: false
reporter:
  email:
    to: ["x@example.com"]
""")
    cfg = load_quality_config(cfg_path)
    assert cfg.label == "Full Site"
    assert "Test description" in cfg.what_we_do
    assert cfg["crawler"]["max_depth"] == 1
    assert cfg["analyzer"]["auto_implement_threshold"] == 0.9


def test_load_quality_config_invalid_raises(tmp_path):
    from shared.site_quality import load_quality_config
    cfg_path = tmp_path / "bad.yaml"
    cfg_path.write_text("""
site:
  domain: missing-id.example.com
""")
    with pytest.raises(ValueError):
        load_quality_config(cfg_path)


# ---------------------------------------------------------------------------
# Tier scoring
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("conf,sev,thr,expected", [
    (0.97, "high",     0.95, "auto"),
    (0.95, "critical", 0.95, "auto"),
    (0.95, "low",      0.95, "review"),    # severity gate: low never auto
    (0.80, "high",     0.95, "review"),    # below threshold
    (0.45, "high",     0.95, "experimental"),
    (0.30, "low",      0.95, "experimental"),
    (0.99, "medium",   0.99, "auto"),
])
def test_score_tier(conf, sev, thr, expected):
    from shared.site_quality import score_tier
    assert score_tier(confidence=conf, severity=sev, threshold=thr) == expected


# ---------------------------------------------------------------------------
# Rec id assignment + schema validation
# ---------------------------------------------------------------------------

def test_assign_rec_ids_stable():
    from shared.site_quality import assign_rec_ids
    recs = [{"title": f"r{i}"} for i in range(5)]
    out = assign_rec_ids(recs)
    assert [r["id"] for r in out] == ["rec-001", "rec-002", "rec-003", "rec-004", "rec-005"]


def test_validate_recs_doc_passes():
    from shared.site_quality import validate_recs_doc
    doc = {
        "schema_version": "1",
        "site": "x",
        "agent": "progressive-improvement-agent",
        "run_ts": "20260426T000000Z",
        "recommendations": [
            {
                "id": "rec-001", "category": "broken-page", "severity": "high",
                "confidence": 0.9, "tier": "review", "title": "t", "rationale": "r",
            },
        ],
    }
    validate_recs_doc(doc)  # no raise


def test_validate_recs_doc_rejects_bad_tier():
    from shared.site_quality import validate_recs_doc
    import jsonschema
    doc = {
        "schema_version": "1", "site": "x", "agent": "competitor-research-agent",
        "run_ts": "t", "recommendations": [{
            "id": "rec-001", "category": "other", "severity": "low",
            "confidence": 0.5, "tier": "BAD", "title": "t", "rationale": "r",
        }],
    }
    with pytest.raises(jsonschema.ValidationError):
        validate_recs_doc(doc)


# ---------------------------------------------------------------------------
# Reply parsing — precise + bulk filters
# ---------------------------------------------------------------------------

def test_parse_user_action_explicit_recs():
    from shared.site_quality import parse_user_action
    payload = {"body": "implement rec-001 rec-007"}
    verb, rec_ids, filters, _ = parse_user_action(payload)
    assert verb == "implement"
    assert rec_ids == ["rec-001", "rec-007"]
    assert filters == []


def test_parse_user_action_bulk_all():
    from shared.site_quality import parse_user_action
    payload = {"body": "implement all"}
    verb, rec_ids, filters, _ = parse_user_action(payload)
    assert verb == "implement"
    assert rec_ids == []
    assert filters == ["all"]


def test_parse_user_action_bulk_severity():
    from shared.site_quality import parse_user_action
    payload = {"body": "implement high and critical"}
    verb, rec_ids, filters, _ = parse_user_action(payload)
    assert verb == "implement"
    assert "high" in filters
    assert "critical" in filters


def test_parse_user_action_bulk_tier():
    from shared.site_quality import parse_user_action
    payload = {"body": "skip experimental"}
    verb, rec_ids, filters, _ = parse_user_action(payload)
    assert verb == "skip"
    assert filters == ["experimental"]


def test_parse_user_action_mixed():
    from shared.site_quality import parse_user_action
    payload = {"body": "implement rec-002 and all high"}
    verb, rec_ids, filters, _ = parse_user_action(payload)
    assert verb == "implement"
    assert rec_ids == ["rec-002"]
    assert "high" in filters


def test_parse_user_action_skip():
    from shared.site_quality import parse_user_action
    verb, rec_ids, _, _ = parse_user_action({"body": "skip rec-003"})
    assert verb == "skip" and rec_ids == ["rec-003"]


def test_parse_user_action_unknown():
    from shared.site_quality import parse_user_action
    verb, rec_ids, filters, _ = parse_user_action({"body": "great work"})
    assert verb == "unknown" and rec_ids == [] and filters == []


def test_parse_user_action_ignores_quoted_history():
    """The user reply might quote the prior email; we shouldn't pick up
    'implement all' from a quote unless the user typed it themselves."""
    from shared.site_quality import parse_user_action
    payload = {"body": (
        "implement rec-001\n"
        "\n"
        "From: automation@nsc\n"
        "Subject: implement all of these\n"  # this would be inside quoted block
    )}
    verb, rec_ids, filters, _ = parse_user_action(payload)
    assert verb == "implement"
    assert rec_ids == ["rec-001"]
    # Boundary cuts off the quoted "From:" — the verb-scope window stops there
    # so 'implement all' in the quoted line isn't picked up as a filter.


# ---------------------------------------------------------------------------
# Filter expansion
# ---------------------------------------------------------------------------

def test_expand_filters_all():
    from shared.site_quality import expand_filters_to_rec_ids
    recs = [
        {"id": "rec-001", "tier": "auto", "severity": "high"},
        {"id": "rec-002", "tier": "review", "severity": "low"},
        {"id": "rec-003", "tier": "experimental", "severity": "medium"},
    ]
    assert expand_filters_to_rec_ids(recs, ["all"]) == ["rec-001", "rec-002", "rec-003"]


def test_expand_filters_severity_union():
    from shared.site_quality import expand_filters_to_rec_ids
    recs = [
        {"id": "rec-001", "tier": "auto", "severity": "high"},
        {"id": "rec-002", "tier": "review", "severity": "critical"},
        {"id": "rec-003", "tier": "review", "severity": "low"},
    ]
    assert expand_filters_to_rec_ids(recs, ["high", "critical"]) == ["rec-001", "rec-002"]


def test_expand_filters_tier():
    from shared.site_quality import expand_filters_to_rec_ids
    recs = [
        {"id": "rec-001", "tier": "auto", "severity": "high"},
        {"id": "rec-002", "tier": "review", "severity": "low"},
        {"id": "rec-003", "tier": "auto", "severity": "medium"},
    ]
    assert expand_filters_to_rec_ids(recs, ["auto"]) == ["rec-001", "rec-003"]


def test_expand_filters_empty():
    from shared.site_quality import expand_filters_to_rec_ids
    assert expand_filters_to_rec_ids([{"id": "rec-001"}], []) == []


# ---------------------------------------------------------------------------
# apply_user_responses — round-trip with bulk
# ---------------------------------------------------------------------------

@pytest.fixture
def recs_doc_path(tmp_path):
    p = tmp_path / "recommendations.json"
    p.write_text(json.dumps({
        "schema_version": "1", "site": "x",
        "agent": "progressive-improvement-agent",
        "run_ts": "t",
        "recommendations": [
            {"id": "rec-001", "tier": "auto", "severity": "high"},
            {"id": "rec-002", "tier": "review", "severity": "medium"},
            {"id": "rec-003", "tier": "experimental", "severity": "low"},
            {"id": "rec-004", "tier": "review", "severity": "critical"},
        ],
    }))
    return p


def test_apply_user_responses_explicit(recs_doc_path):
    from shared.site_quality import apply_user_responses
    applied = apply_user_responses(
        responses=[{"body": "implement rec-002 rec-004"}],
        prior_recs_path=recs_doc_path,
    )
    ids = sorted(a["id"] for a in applied)
    assert ids == ["rec-002", "rec-004"]
    doc = json.loads(recs_doc_path.read_text())
    by = {r["id"]: r for r in doc["recommendations"]}
    assert by["rec-002"]["user_response"]["action"] == "implement"
    assert by["rec-002"]["user_response"]["matched_via"] == "explicit-rec-id"
    assert by["rec-001"].get("user_response") is None  # untouched


def test_apply_user_responses_bulk_all(recs_doc_path):
    from shared.site_quality import apply_user_responses
    applied = apply_user_responses(
        responses=[{"body": "implement all"}],
        prior_recs_path=recs_doc_path,
    )
    assert len(applied) == 4
    doc = json.loads(recs_doc_path.read_text())
    for r in doc["recommendations"]:
        assert r["user_response"]["action"] == "implement"
        assert r["user_response"]["matched_via"] == "bulk-filter"


def test_apply_user_responses_bulk_severity(recs_doc_path):
    """'implement critical and high' should hit rec-001 (high) and rec-004 (critical)."""
    from shared.site_quality import apply_user_responses
    applied = apply_user_responses(
        responses=[{"body": "implement critical and high"}],
        prior_recs_path=recs_doc_path,
    )
    ids = sorted(a["id"] for a in applied)
    assert ids == ["rec-001", "rec-004"]


def test_apply_user_responses_bulk_skip_experimental(recs_doc_path):
    from shared.site_quality import apply_user_responses
    applied = apply_user_responses(
        responses=[{"body": "skip experimental"}],
        prior_recs_path=recs_doc_path,
    )
    ids = [a["id"] for a in applied]
    assert ids == ["rec-003"]
    doc = json.loads(recs_doc_path.read_text())
    by = {r["id"]: r for r in doc["recommendations"]}
    assert by["rec-003"]["user_response"]["action"] == "skip"


# ---------------------------------------------------------------------------
# Email rendering
# ---------------------------------------------------------------------------

def test_render_recs_email_subject_tag(tmp_path):
    from shared.site_quality import load_quality_config, render_recs_email
    cfg_path = tmp_path / "x.yaml"
    cfg_path.write_text("""
site:
  id: e2e-site
  domain: e2e.example.com
  label: E2E
""")
    cfg = load_quality_config(cfg_path)
    subject, body = render_recs_email(
        cfg=cfg, agent_id="progressive-improvement-agent",
        request_id="r-20260101-000000-0001",
        recs=[{
            "id": "rec-001", "category": "broken-page", "severity": "high",
            "confidence": 0.9, "tier": "review", "title": "t", "rationale": "r",
            "evidence": [{"url": "https://e2e.example.com/x"}],
        }],
        summary="One issue found.",
    )
    assert "[progressive-improvement-agent:r-20260101-000000-0001]" in subject
    assert "rec-001" in body
    # Bulk-reply syntax should be in the body
    assert "implement all" in body
    assert "implement high" in body


def test_render_recs_email_empty_state(tmp_path):
    from shared.site_quality import load_quality_config, render_recs_email
    cfg_path = tmp_path / "x.yaml"
    cfg_path.write_text("""
site:
  id: clean-site
  domain: clean.example.com
""")
    cfg = load_quality_config(cfg_path)
    _, body = render_recs_email(
        cfg=cfg, agent_id="progressive-improvement-agent",
        request_id="r-20260101-000000-0001",
        recs=[], summary="",
    )
    assert "No recommendations" in body or "site is clean" in body


# ---------------------------------------------------------------------------
# Auto-dispatch
# ---------------------------------------------------------------------------

def test_dispatch_auto_recs_disabled_when_off(tmp_path, storage):
    from shared.site_quality import load_quality_config, dispatch_auto_recs
    cfg_path = tmp_path / "x.yaml"
    cfg_path.write_text("""
site:
  id: off-site
  domain: off.example.com
auto_implement: false
""")
    cfg = load_quality_config(cfg_path)
    dispatched = dispatch_auto_recs(
        cfg=cfg, agent_id="progressive-improvement-agent",
        recs=[{"id": "rec-001", "tier": "auto"}], storage=storage,
    )
    assert dispatched == []


def test_dispatch_auto_recs_enabled(tmp_path, storage):
    from shared.site_quality import load_quality_config, dispatch_auto_recs
    cfg_path = tmp_path / "x.yaml"
    cfg_path.write_text("""
site:
  id: on-site
  domain: on.example.com
auto_implement: true
implementer:
  agent_id: seo-implementer
""")
    cfg = load_quality_config(cfg_path)
    dispatched = dispatch_auto_recs(
        cfg=cfg, agent_id="progressive-improvement-agent",
        recs=[
            {"id": "rec-001", "tier": "auto"},
            {"id": "rec-002", "tier": "review"},
            {"id": "rec-003", "tier": "auto"},
        ],
        storage=storage,
    )
    assert sorted(dispatched) == ["rec-001", "rec-003"]
    # Verify it landed in the implementer's response queue
    keys = storage.list_prefix("agents/seo-implementer/responses-queue/")
    assert len(keys) == 1
    payload = storage.read_json(keys[0])
    assert payload["action"] == "implement"
    assert sorted(payload["rec_ids"]) == ["rec-001", "rec-003"]
    assert payload["auto_dispatched"] is True
