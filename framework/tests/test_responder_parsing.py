"""Tests for responder-agent's parse_actions — both precise rec-id replies
and the new bulk-filter syntax."""
from __future__ import annotations

import importlib.util as iu
import os
import sys
from pathlib import Path

# Load responder-agent module directly (it's a script, not on sys.path)
_RESPONDER = Path(os.path.dirname(__file__)).resolve().parent.parent / "responder-agent" / "responder.py"
spec = iu.spec_from_file_location("responder_test_module", _RESPONDER)
mod = iu.module_from_spec(spec)
sys.modules.setdefault("responder_test_module", mod)
spec.loader.exec_module(mod)

parse_actions = mod.parse_actions


def test_parse_explicit_rec_ids():
    out = parse_actions("implement rec-001 rec-002")
    assert len(out) == 1
    assert out[0]["action"] == "implement"
    assert out[0]["rec_ids"] == ["rec-001", "rec-002"]
    assert out[0]["filters"] == []


def test_parse_skip():
    out = parse_actions("skip rec-003")
    assert out[0]["action"] == "skip"
    assert out[0]["rec_ids"] == ["rec-003"]


def test_parse_bulk_all():
    out = parse_actions("implement all")
    assert len(out) == 1
    assert out[0]["action"] == "implement"
    assert out[0]["rec_ids"] == []
    assert "all" in out[0]["filters"]


def test_parse_bulk_severity_combo():
    out = parse_actions("implement high and critical")
    assert out[0]["action"] == "implement"
    assert "high" in out[0]["filters"]
    assert "critical" in out[0]["filters"]


def test_parse_bulk_tier():
    out = parse_actions("skip experimental")
    assert out[0]["action"] == "skip"
    assert out[0]["filters"] == ["experimental"]


def test_parse_mixed_explicit_and_bulk():
    out = parse_actions("implement rec-001 and all high")
    assert out[0]["rec_ids"] == ["rec-001"]
    assert "high" in out[0]["filters"]


def test_parse_rejects_html_residue():
    out = parse_actions("<div>implement rec-001</div>")
    assert out == []


def test_parse_no_recs_no_filters_returns_empty():
    out = parse_actions("looks good, ship it!")
    assert out == []


def test_parse_multi_line():
    out = parse_actions("""implement rec-001
skip rec-002""")
    assert len(out) == 2
    assert out[0]["action"] == "implement" and out[0]["rec_ids"] == ["rec-001"]
    assert out[1]["action"] == "skip" and out[1]["rec_ids"] == ["rec-002"]


def test_parse_modify_keeps_explicit_only():
    """modify and merge require explicit rec ids — bulk filters don't apply."""
    out = parse_actions("modify rec-005: shorter title")
    assert out[0]["action"] == "modify"
    assert out[0]["rec_ids"] == ["rec-005"]
    assert out[0]["filters"] == []
