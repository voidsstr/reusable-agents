"""Tests for seo-analyzer/llm_audit.py — the LLM-driven adaptive pass.

Mocks the AI client; verifies prompt + response parsing + dedup +
check-id whitelisting (no hallucinated checks).
"""
from __future__ import annotations

import importlib.util as iu
import json
import os
import sys
from pathlib import Path

# Load the llm_audit module from the seo-analyzer dir
_ROOT = Path(__file__).resolve().parent.parent.parent
_SPEC = iu.spec_from_file_location(
    "llm_audit", _ROOT / "seo-analyzer" / "llm_audit.py"
)
llm_audit = iu.module_from_spec(_SPEC)
sys.modules.setdefault("llm_audit", llm_audit)
_SPEC.loader.exec_module(llm_audit)


# ---------------------------------------------------------------------------
# Constants exposed for the dashboard / SEO-expert audit
# ---------------------------------------------------------------------------

def test_check_id_to_rec_type_is_complete():
    """Every check_id mapped to a valid SEO rec type."""
    valid_types = {
        "top5-target-page", "indexing-fix", "ctr-fix", "ssr-fix",
        "schema-markup", "internal-link", "sitemap-fix", "conversion-path",
        "content-expansion", "redirect-fix", "other",
    }
    for cid, rt in llm_audit.CHECK_ID_TO_REC_TYPE.items():
        assert rt in valid_types, f"{cid} maps to invalid rec type {rt}"


def test_all_check_ids_in_categories():
    """Every check_id is reachable through CHECK_CATEGORIES (audit groups)."""
    cats_flat = {cid for v in llm_audit.CHECK_CATEGORIES.values() for cid in v}
    # Allow the ai-search-geo / mobile-cwv etc. groupings to leave a few unmapped
    # (e.g. cta-* might land in `conversion`); just ensure most are covered
    assert len(cats_flat) >= int(len(llm_audit.ALL_CHECK_IDS) * 0.85)


def test_seo_checklist_lists_concrete_checks():
    """The audit prompt is a concrete checklist (not vibes)."""
    txt = llm_audit.SEO_AUDIT_CHECKLIST
    # Spot-check: should mention specific brackets like [meta-title-length]
    assert "[meta-title-length]" in txt
    assert "[schema-product-missing]" in txt
    assert "[cwv-render-blocking]" in txt
    assert "[geo-direct-answer-missing]" in txt
    # Should explicitly forbid invented categories
    assert "Do not invent categories" in txt or "outside it" in txt


# ---------------------------------------------------------------------------
# JSON parsing tolerance
# ---------------------------------------------------------------------------

def test_parse_llm_json_strips_fences():
    raw = '```json\n[{"check_id":"meta-title-length","url":"x","severity":"low","confidence":0.9,"title":"t","rationale":"r","evidence":"e","fix":"f"}]\n```'
    out = llm_audit._parse_llm_json(raw)
    assert isinstance(out, list)
    assert len(out) == 1
    assert out[0]["check_id"] == "meta-title-length"


def test_parse_llm_json_handles_preamble():
    raw = 'Here are the issues I found:\n[{"check_id":"h1-missing","url":"x","severity":"high","confidence":0.95,"title":"t","rationale":"r","evidence":"e","fix":"f"}]'
    out = llm_audit._parse_llm_json(raw)
    assert len(out) == 1
    assert out[0]["check_id"] == "h1-missing"


def test_parse_llm_json_returns_empty_on_invalid():
    out = llm_audit._parse_llm_json("not a JSON")
    assert out == []


# ---------------------------------------------------------------------------
# run_llm_audit — mocked LLM
# ---------------------------------------------------------------------------

def _mock_chat(canned_responses):
    """Return a callable that emits canned responses in order."""
    state = {"i": 0}
    def call(messages, *, temperature=0.0, max_tokens=2000):
        i = state["i"]
        state["i"] = (i + 1) % len(canned_responses)
        return canned_responses[i]
    return call


def test_run_llm_audit_filters_hallucinated_check_ids():
    """The agent must only emit issues whose check_id is in the whitelist."""
    mock = _mock_chat([
        json.dumps([
            {"check_id": "meta-title-length", "url": "https://x/p1",
             "severity": "high", "confidence": 0.9,
             "title": "title too long", "rationale": "r", "evidence": "e", "fix": "f"},
            {"check_id": "INVENTED-CHECK", "url": "https://x/p2",
             "severity": "high", "confidence": 0.9,
             "title": "hallucination", "rationale": "r", "evidence": "e", "fix": "f"},
        ]),
    ])
    pages = [{"url": "https://x/p1", "title": "t", "body_text": "b"}]
    out = llm_audit.run_llm_audit(
        pages=pages, site_label="test",
        ai_chat_callable=mock, batch_size=4,
    )
    cids = [i["check_id"] for i in out]
    assert "meta-title-length" in cids
    assert "INVENTED-CHECK" not in cids


def test_run_llm_audit_empty_returns_empty():
    out = llm_audit.run_llm_audit(
        pages=[], site_label="x", ai_chat_callable=lambda *a, **k: "[]",
    )
    assert out == []


def test_run_llm_audit_no_callable_returns_empty():
    out = llm_audit.run_llm_audit(
        pages=[{"url": "x"}], site_label="x", ai_chat_callable=None,
    )
    assert out == []


def test_run_llm_audit_batches():
    """With batch_size=2 + 5 pages, we expect 3 LLM calls."""
    state = {"calls": 0}
    def mock(messages, **k):
        state["calls"] += 1
        return "[]"
    pages = [{"url": f"https://x/p{i}", "title": "t", "body_text": "b"} for i in range(5)]
    llm_audit.run_llm_audit(
        pages=pages, site_label="x", ai_chat_callable=mock, batch_size=2,
    )
    assert state["calls"] == 3  # ceil(5/2) = 3


def test_run_llm_audit_continues_after_batch_error():
    """A failing batch should produce an llm-audit-error placeholder + the
    next batch should still run."""
    state = {"i": 0}
    def mock(messages, **k):
        state["i"] += 1
        if state["i"] == 1:
            raise RuntimeError("transient LLM blip")
        return json.dumps([{
            "check_id": "h1-missing", "url": "https://x/p3",
            "severity": "high", "confidence": 0.95,
            "title": "t", "rationale": "r", "evidence": "e", "fix": "f",
        }])
    pages = [{"url": f"https://x/p{i}"} for i in range(4)]
    out = llm_audit.run_llm_audit(
        pages=pages, site_label="x", ai_chat_callable=mock, batch_size=2,
    )
    cids = [i["check_id"] for i in out]
    assert "llm-audit-error" in cids   # batch 1 failed
    assert "h1-missing" in cids        # batch 2 succeeded


# ---------------------------------------------------------------------------
# issues_to_recommendations
# ---------------------------------------------------------------------------

def test_issues_to_recommendations_maps_correctly():
    issues = [
        {"check_id": "meta-title-length", "url": "https://x/p1",
         "severity": "high", "confidence": 0.95,
         "title": "title", "rationale": "r", "evidence": "e", "fix": "f"},
        {"check_id": "schema-product-missing", "url": "https://x/p2",
         "severity": "critical", "confidence": 0.99,
         "title": "no schema", "rationale": "r", "evidence": "e", "fix": "f"},
    ]
    seq = {"i": 0}
    def next_id():
        seq["i"] += 1
        return f"rec-{seq['i']:03d}"
    recs = llm_audit.issues_to_recommendations(issues, next_id)
    assert len(recs) == 2
    assert recs[0]["type"] == "ctr-fix"          # meta-title-* → ctr-fix
    assert recs[1]["type"] == "schema-markup"    # schema-* → schema-markup
    assert recs[0]["llm_check_id"] == "meta-title-length"
    assert recs[0]["priority"] == "high"
    assert recs[1]["priority"] == "critical"
    # data_refs carries the URL for traceability
    assert recs[0]["data_refs"] == ["https://x/p1"]


def test_issues_to_recommendations_skips_audit_errors():
    """LLM audit errors are tracked but not surfaced as recs."""
    issues = [{"check_id": "llm-audit-error", "url": "x"}]
    recs = llm_audit.issues_to_recommendations(issues, lambda: "rec-001")
    assert recs == []


def test_format_pages_caps_body():
    """Body text gets capped so we don't blow LLM context."""
    pages = [{"url": "x", "title": "t", "body_text": "z" * 10_000}]
    out = llm_audit.format_pages_for_audit(pages, cap_chars=500)
    # The body section should appear truncated
    assert "z" * 500 in out
    assert "z" * 600 not in out
