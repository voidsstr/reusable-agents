"""site-goals-tracker: per-site profiles live in config, not in agent code
(SHOP-22). Repo config, then storage, then SITE_GOALS_PROFILE_FILE."""
from __future__ import annotations

import importlib.util as iu
import json
import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_AGENT = _ROOT / "agents" / "site-goals-tracker" / "agent.py"
_CONFIG = _ROOT / "config" / "site-goals-tracker-config.json"


@pytest.fixture()
def mod(monkeypatch, tmp_path):
    monkeypatch.delenv("SITE_GOALS_PROFILE_FILE", raising=False)
    monkeypatch.setenv("SITE_GOALS_SITE_YAML", str(tmp_path / "none.yaml"))
    spec = iu.spec_from_file_location("site_goals_tracker_profiles", _AGENT)
    m = iu.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_no_site_names_in_tracker_code():
    """String literals (docstrings aside) never name a site: a new site is a
    by_site entry in config/site-goals-tracker-config.json."""
    import ast
    tree = ast.parse(_AGENT.read_text())
    docs = {id(n.body[0].value) for n in ast.walk(tree)
            if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef)) and n.body
            and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
    strings = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)
               and isinstance(n.value, str) and id(n) not in docs]
    sites = list(json.loads(_CONFIG.read_text())["by_site"])
    assert sites
    leaks = [s for s in strings for site in sites if re.search(site, s, re.I)]
    assert leaks == []


def test_profiles_come_from_repo_config(mod):
    cfg = json.loads(_CONFIG.read_text())["by_site"]
    assert set(mod.SITE_PROFILES) == set(cfg)
    for site, prof in mod.SITE_PROFILES.items():
        assert prof["site"] == site and mod._short_site(prof) == site
        assert prof["agent_id"] == cfg[site]["agent_id"]


def test_storage_then_profile_file_override(mod, tmp_path, monkeypatch):
    site = sorted(mod.SITE_PROFILES)[0]

    class S:
        def read_json(self, key):
            assert key == mod.PROFILES_CONFIG_KEY
            return {"by_site": {site: {"goal_defaults": {"goal-organic-clicks-30d": {"target": 7}},
                                       "label": "From Storage"},
                                "newsite": {"host": "new.test", "agent_id": "newsite-sgt"}}}

    f = tmp_path / "p.json"
    f.write_text(json.dumps({"by_site": {site: {"label": "From File"}}}))
    monkeypatch.setenv("SITE_GOALS_PROFILE_FILE", str(f))
    profs = mod.load_profiles(storage=S(), read_storage=True)
    assert profs[site]["label"] == "From File"
    assert profs[site]["goal_defaults"]["goal-organic-clicks-30d"]["target"] == 7
    # untouched keys survive the deep merge
    assert profs[site]["goal_defaults"]["goal-active-pages-count"] == \
        mod.SITE_PROFILES[site]["goal_defaults"]["goal-active-pages-count"]
    assert profs[site]["agent_id"] == mod.SITE_PROFILES[site]["agent_id"]
    assert profs["newsite"]["site"] == "newsite" and profs["newsite"]["label"] == "newsite"


def test_new_site_from_config_gets_generic_goals(mod, monkeypatch):
    seen = {}
    monkeypatch.setattr(mod.goals_mod, "init_goals",
                        lambda agent_id, goals, storage=None: seen.setdefault("g", goals))
    prof = {"site": "newsite", "label": "New Site", "host": "new.test", "agent_id": "x",
            "goal_defaults": {"goal-organic-clicks-30d": {"target": 12}},
            "event_goals": [{"id": "goal-new-clicks-30d", "title": "t", "description": "d",
                             "metric": {"name": "n", "current": 0, "target": 5,
                                        "direction": "increase", "unit": "u"},
                             "status": "active"}]}
    mod.write_goal_definitions(prof, "x", site_yaml={})
    ids = [g["id"] for g in seen["g"]]
    assert ids[:2] == ["goal-total-conversions-30d", "goal-new-clicks-30d"]
    by = {g["id"]: g for g in seen["g"]}
    assert by["goal-organic-clicks-30d"]["metric"]["target"] == 12
    assert by["goal-organic-clicks-30d"]["title"].endswith("(New Site)")


def test_config_fleet_note_matches_tracker_constant(mod):
    notes = [a for prof in mod.SITE_PROFILES.values()
             for ov in (prof.get("goal_overrides") or {}).values()
             for a in ov.get("annotations") or []]
    assert notes and all(a["note"] == mod.SAFARI_FLEET_NOTE and a["ts"] == "2026-11-11"
                         for a in notes)
