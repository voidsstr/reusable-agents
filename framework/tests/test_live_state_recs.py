"""Availability recs must survive run-to-run dedupe.

Regression cover for the bind found on 2026-09-10: a `broken-page` rec
from a crawl that ran during a two-hour Postgres saturation window was
resolved as transient. Marking it `implemented` (so the dispatch queue
stops re-issuing it) would ALSO have suppressed the identical rec for the
next 30 runs, so a genuine future outage on that URL would be swallowed.
Exempting live-state types from the dedupe filter decouples "this rec is
resolved" from "this URL is healthy".
"""
import json

from framework.core.work_types import (
    DEFAULT_LIVE_STATE_REC_TYPES,
    is_live_state,
    live_state_rec_types,
)


def test_availability_types_are_live_state():
    for rec in ({"category": "broken-page"}, {"type": "broken-link"},
                {"category": "fetch-error"}, {"type": "BROKEN-PAGE"}):
        assert is_live_state(rec), rec


def test_stored_content_defects_are_not_live_state():
    for rec in ({"category": "duplicate-content"}, {"type": "content-expansion"},
                {"category": "miscategorization"}, {}, {"type": None}):
        assert not is_live_state(rec), rec


def test_config_extends_defaults(storage):
    storage.write_json("config/live-state-rec-types.json",
                       {"schema_version": "1", "rec_types": ["custom-probe"]})
    live = live_state_rec_types(storage)
    assert "custom-probe" in live
    assert DEFAULT_LIVE_STATE_REC_TYPES <= live
    assert is_live_state({"type": "custom-probe"}, storage=storage)


def test_config_replace_swaps_defaults(storage):
    storage.write_json("config/live-state-rec-types.json",
                       {"schema_version": "1", "rec_types": ["only-this"],
                        "replace": True})
    live = live_state_rec_types(storage)
    assert live == frozenset({"only-this"})
    assert not is_live_state({"category": "broken-page"}, storage=storage)


def test_malformed_config_falls_back_to_defaults(storage):
    storage.write_json("config/live-state-rec-types.json", {"rec_types": "not-a-list"})
    assert live_state_rec_types(storage) == DEFAULT_LIVE_STATE_REC_TYPES
    assert live_state_rec_types(None) == DEFAULT_LIVE_STATE_REC_TYPES


def test_pi_dedupe_keeps_availability_recs_detectable(storage, monkeypatch):
    """The PI agent's handled-key set must exclude a resolved broken-page
    rec, so the next crawl can re-detect a fresh outage on the same URL."""
    import importlib.util
    import os
    import sys

    import pytest

    pi_dir = os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", "..",
        "agents", "progressive-improvement-agent"))
    sys.path.insert(0, pi_dir)
    spec = importlib.util.spec_from_file_location(
        "_pi_agent_under_test", os.path.join(pi_dir, "agent.py"))
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except ImportError as e:  # crawler deps (bs4, ...) not installed here
        pytest.skip(f"progressive-improvement-agent deps unavailable: {e}")

    agent_id = "test-pi-agent"
    prior = {
        "recommendations": [
            {"id": "rec-002", "category": "broken-page", "implemented": True,
             "title": "Broken page: https://example.com/recipes returned 503",
             "evidence": [{"url": "https://example.com/recipes"}]},
            {"id": "rec-009", "category": "duplicate-content", "implemented": True,
             "title": "Duplicate meta description on /recipes/pasta",
             "evidence": [{"url": "https://example.com/recipes/pasta"}]},
        ]
    }
    storage.write_json(
        f"agents/{agent_id}/runs/20260909T224500Z/recommendations.json", prior)

    handled = mod._load_handled_rec_keys(agent_id, "20260910T000000Z", storage)

    broken_key = mod._canonical_rec_key(prior["recommendations"][0])
    dupe_key = mod._canonical_rec_key(prior["recommendations"][1])
    assert broken_key not in handled, "availability rec must stay re-detectable"
    assert dupe_key in handled, "stored-content defect must still be suppressed"
