"""framework.core.rec_contract: recs a producer wrote under an older contract
are held, by the backlog dispatcher, by the implementer's gate, and by the
operator's one-off sweep.

2026-11-12: 200 open catalog-audit image/name recs written before the
image/name rule were still being replayed by the backlog dispatcher. Their
outline said "deactivate the product (UPDATE products SET is_active=false
WHERE slug=...)", and they had never been through the rule's brand/identity
triage or the protect set. 15 of them named active products, 7 of those
(6 products) article-linked, featured or clicked."""
from __future__ import annotations

import importlib.util as iu
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from framework.core import rec_contract as rc

_REPO = Path(__file__).resolve().parents[2]
CHECK = "product-image-matches-name"
PRODUCER = "shop-catalog-audit-agent"


def _old_rec(rid, slug="some-slug"):
    """The shape the catalog audit wrote before the image/name rule."""
    return {"id": rid, "check_id": CHECK, "category": "image-name-mismatch",
            "severity": "high", "tier": "review", "title": "Image mismatch: " + slug,
            "evidence": [{"ref_id": slug}],
            "implementation_outline": {"approach": "deactivate the product (UPDATE products "
                                                   "SET is_active=false WHERE slug=...)"}}


def _new_rec(rid, remedy="deactivate"):
    r = _old_rec(rid)
    r["identity_rule"] = {"remedy": remedy, "basis": "identity", "relation": "same", "why": "x"}
    r["ref_ids"] = ["B0NEW" + rid[-3:]]
    if remedy == "report":
        r["review_required"] = True
    return r


def test_old_image_name_recs_are_held_and_new_ones_pass():
    cfg = rc.load_config(file_cfg={})
    hold = rc.hold_reason(_old_rec("rec-001"), producer=PRODUCER, cfg=cfg)
    assert hold and hold["rule"] == "image-name-identity-rule" and "slug" in hold["why"]
    # a rec the rule triaged carries identity_rule and passes
    assert rc.hold_reason(_new_rec("rec-002"), producer=PRODUCER, cfg=cfg) is None
    # other checks and other producers' recs are untouched
    other = {**_old_rec("rec-003"), "check_id": "recipe-image-present"}
    assert rc.hold_reason(other, producer=PRODUCER, cfg=cfg) is None
    assert rc.hold_reason({"id": "rec-1", "title": "seo"}, producer="x-seo-agent", cfg=cfg) is None
    # held whatever the producer is called (or when it is not known)
    assert rc.hold_reason(_old_rec("rec-004"), producer="", cfg=cfg)
    # a person may release one
    assert rc.hold_reason({**_old_rec("rec-005"), "contract_override": True}, cfg=cfg) is None


def test_config_layers_and_fail_safe_defaults():
    extra = {"id": "x", "producers": ["*-seo-agent"], "match": {"type": "ctr-fix"},
             "require_any": ["target_files"]}
    cfg = rc.load_config(file_cfg={"extra_rules": [extra]})
    assert [r["id"] for r in cfg["rules"]] == ["image-name-identity-rule", "x"]
    assert rc.hold_reason({"type": "ctr-fix"}, producer="s-seo-agent", cfg=cfg)
    assert rc.hold_reason({"type": "ctr-fix"}, producer="s-pi-agent", cfg=cfg) is None
    assert rc.hold_reason({"type": "ctr-fix", "target_files": ["a"]},
                          producer="s-seo-agent", cfg=cfg) is None
    off = rc.load_config(file_cfg={"disabled_rules": ["image-name-identity-rule"]})
    assert rc.hold_reason(_old_rec("rec-001"), cfg=off) is None
    assert rc.load_config(file_cfg={"rules": []})["rules"] == []
    # a rule with no match would hold everything: it is ignored
    assert rc.hold_reason({"a": 1}, cfg={"rules": [{"id": "all", "require_any": ["z"]}]}) is None

    class Broken:
        def read_json(self, key):
            raise RuntimeError("storage down")

    # an unreadable config keeps the defaults, so the gate still holds
    assert rc.hold_reason(_old_rec("rec-001"), cfg=rc.load_config(storage=Broken()))


def test_filter_run_dir_defers_and_marks_held_recs(tmp_path):
    doc = {"agent_id": PRODUCER, "recommendations": [
        _old_rec("rec-001"), _new_rec("rec-002"), _old_rec("rec-003")]}
    (tmp_path / "recommendations.json").write_text(json.dumps(doc))
    keep, held = rc.filter_run_dir(str(tmp_path), ["rec-001", "rec-002", "rec-003", "rec-404"])
    assert keep == ["rec-002", "rec-404"] and [h[0] for h in held] == ["rec-001", "rec-003"]
    summary = (tmp_path / "changes" / "rec-001.summary.md").read_text()
    assert summary.startswith("DEFERRED:") and "image-name-identity-rule" in summary
    after = {r["id"]: r for r in json.loads((tmp_path / "recommendations.json").read_text())[
        "recommendations"]}
    assert after["rec-001"]["review_required"] and after["rec-001"]["contract_hold"]["by"] == "implementer"
    assert "contract_hold" not in after["rec-002"] and not after["rec-002"].get("review_required")
    # the dispatcher would no longer offer them
    assert not rc.is_dispatchable(after["rec-001"]) and rc.is_dispatchable(after["rec-002"])


def test_cli_filter_prints_keep_only_when_something_is_held(tmp_path):
    def run(ids):
        r = subprocess.run([sys.executable, "-m", "framework.cli.rec_contract", "filter",
                            "--run-dir", str(tmp_path), "--rec-ids", ids],
                           capture_output=True, text=True, cwd=str(_REPO),
                           env={**os.environ, "PYTHONPATH": str(_REPO),
                                "STORAGE_BACKEND": "local",
                                "AGENT_STORAGE_LOCAL_PATH": str(tmp_path / "storage")})
        assert r.returncode == 0, r.stderr
        return r.stdout.strip()

    doc = {"agent_id": PRODUCER, "recommendations": [_old_rec("rec-001"), _new_rec("rec-002")]}
    (tmp_path / "recommendations.json").write_text(json.dumps(doc))
    assert run("rec-002") == ""
    assert run("rec-001,rec-002") == "KEEP:rec-002"
    assert run("rec-001") == "KEEP:"
    # an unreadable run dir changes nothing and does not fail the caller
    (tmp_path / "recommendations.json").write_text("{not json")
    assert run("rec-001") == ""


def test_sweep_marks_open_held_recs_once(storage):
    key = f"agents/{PRODUCER}/runs/20261021T123000Z/recommendations.json"
    closed = {**_old_rec("rec-004"), "deferred": True}
    review = {**_old_rec("rec-005"), "review_required": True}
    storage.write_json(key, {"agent_id": PRODUCER, "recommendations": [
        _old_rec("rec-001"), _new_rec("rec-002"), closed, review]})
    storage.write_json(f"agents/{PRODUCER}/runs/20261112T133000Z/recommendations.json",
                       {"recommendations": [_new_rec("rec-001", remedy="report")]})
    dry = rc.sweep(storage, [PRODUCER], cfg=rc.load_config(file_cfg={}))
    assert dry["totals"]["held_dispatchable"] == 1 and dry["totals"]["held_review"] == 1
    assert dry["totals"]["marked"] == 0
    assert "contract_hold" not in storage.read_json(key)["recommendations"][0]
    done = rc.sweep(storage, [PRODUCER], cfg=rc.load_config(file_cfg={}), apply=True)
    assert done["totals"]["marked"] == 2
    recs = {r["id"]: r for r in storage.read_json(key)["recommendations"]}
    assert recs["rec-001"]["review_required"] and recs["rec-001"]["contract_hold"]["rule"]
    assert "contract_hold" not in recs["rec-004"]       # closed recs are left alone
    assert "contract_hold" not in recs["rec-002"]
    again = rc.sweep(storage, [PRODUCER], cfg=rc.load_config(file_cfg={}), apply=True)
    assert again["totals"]["marked"] == 0                 # idempotent


# ── where the gate is enforced ──────────────────────────────────────────────

def _load_dispatcher():
    spec = iu.spec_from_file_location(
        "backlog_dispatcher_rc", _REPO / "agents" / "backlog-dispatcher-agent" / "agent.py")
    m = iu.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_backlog_dispatcher_does_not_dispatch_held_recs(storage, monkeypatch, tmp_path):
    m = _load_dispatcher()
    aid = "specpicks-catalog-audit-agent"
    assert aid in m.PRODUCER_AGENT_IDS
    monkeypatch.setattr(m, "PRODUCER_AGENT_IDS", (aid,))
    run_ts = "20261021T123000Z"
    storage.write_json(f"agents/{aid}/run-index.json",
                       {"recent": [{"run_ts": run_ts, "status": "success"}]})
    storage.write_json(f"agents/{aid}/runs/{run_ts}/recommendations.json",
                       {"agent_id": aid, "recommendations": [
                           _old_rec("rec-001", "slug-a"), _old_rec("rec-002", "slug-b"),
                           {**_new_rec("rec-003"), "title": "Image mismatch: c"}]})
    for name, val in (("_kill_stuck_scopes", lambda **k: []), ("_list_inflight_scopes", lambda: []),
                      ("_cleanup_stale_pool_counters", lambda: 0),
                      ("_write_live_scopes_heartbeat", lambda *a: None),
                      ("_effective_cap", lambda cap, fam: (1, "")),
                      ("_count_inflight_scopes", lambda: 0),
                      ("_classify_rec_backend", lambda rec, kind="": "claude")):
        monkeypatch.setattr(m, name, val)
    from framework.core import dispatch, run_dir
    calls = []

    class H:
        request_id = "r"

    monkeypatch.setattr(dispatch, "dispatch_now", lambda **kw: calls.append(kw) or H())
    monkeypatch.setattr(dispatch, "recently_dispatched", lambda aid, storage=None: set())
    monkeypatch.setattr(run_dir.RunDir, "materialize", lambda self, td, *a, **k: None)
    d = m.BacklogDispatcher.__new__(m.BacklogDispatcher)
    d.storage, d.state, d.agent_id, d.run_ts = storage, {}, "backlog-dispatcher-agent", "T"
    d.decisions_log = []
    d.decide = lambda kind, msg, **k: d.decisions_log.append((kind, msg))
    res = d.run()
    assert [c["rec_ids"] for c in calls] == [["rec-003"]]
    assert res.metrics["recs_held_by_contract"] == 2
    assert any("rec contract: 2 open rec(s) held" in msg for _, msg in d.decisions_log)


def test_implementer_runs_the_contract_gate_before_any_llm_path():
    text = (_REPO / "agents" / "implementer" / "run.sh").read_text()
    gate = text.index("python3 -m framework.cli.rec_contract filter")
    assert gate < text.index('case "$IMPLEMENTER_LLM" in')
    assert gate < text.index("# ── Page holds")
    block = text[gate:text.index("# ── Page holds")]
    assert '"KEEP:")' in block and "exit 0" in block and "deferred.json" in block
    assert 'export RESPONDER_REC_IDS="${CONTRACT_OUT#KEEP:}"' in block
