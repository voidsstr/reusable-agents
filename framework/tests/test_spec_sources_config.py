"""framework.core.spec_sources: config resolution, the repo default config,
guards that keep the module niche- and site-agnostic and inert, fixture
provenance, and the offline CLI."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from framework.core import spec_sources as ss

ROOT = Path(__file__).resolve().parents[2]
FX = Path(__file__).resolve().parent / "fixtures" / "spec_sources"
PROFILES = {p.stem: json.loads(p.read_text()) for p in (FX / "profiles").glob("*.json")}


# ---------------------------------------------------------------------------
# layering
# ---------------------------------------------------------------------------

FILE = {
    "defaults": {"fetch": {"min_interval_s": 20}, "consolidate": {"intra_source": "supersede"}},
    "profiles": {"niche-a": {"fields": {"w": {"kind": "quantity", "unit": "W", "max": 100}},
                             "fetch": {"timeout_s": 5}},
                 "niche-b": {"fields": {"v": {"kind": "quantity", "unit": "V"}}}},
    "by_site": {"site-x": {"profile": "niche-a", "fetch": {"user_agent": "UA-x"},
                           "fields": {"w": {"max": 200}}}},
    "by_agent_id": {"site-x-spec-extractor": {"llm": {"enabled": True}, "fields": {"w": {"required": True}}}},
}


def test_layers_merge_in_order():
    cfg = ss.resolve_config(site="site-x", agent_id="site-x-spec-extractor", file_cfg=FILE,
                            site_cfg={"fetch": {"min_interval_s": 30}})
    assert cfg["profile"] == "niche-a" and cfg["config_problems"] == []
    assert cfg["fetch"]["min_interval_s"] == 30            # site_cfg over defaults
    assert cfg["fetch"]["timeout_s"] == 5                  # profile
    assert cfg["fetch"]["user_agent"] == "UA-x"            # by_site
    assert cfg["fetch"]["max_redirects"] == 5              # module DEFAULTS
    assert cfg["fields"]["w"] == {"kind": "quantity", "unit": "W", "max": 200, "required": True}
    assert cfg["llm"]["enabled"] is True and cfg["adjudication"]["enabled"] is False
    assert cfg["site"] == "site-x" and cfg["agent_id"] == "site-x-spec-extractor"


def test_profile_choice_precedence_and_unknown_profile():
    assert ss.resolve_config(site="site-x", file_cfg=FILE, site_cfg={"profile": "niche-b"})["profile"] == "niche-b"
    cfg = ss.resolve_config(site="site-x", file_cfg=FILE, site_cfg={"profile": "niche-b"})
    assert set(cfg["fields"]) == {"v", "w"}                # by_site still layers its own fields on top
    bad = ss.resolve_config(file_cfg=FILE, site_cfg={"profile": "nope"})
    assert bad["config_problems"] == ["unknown profile 'nope'"]
    assert ss.resolve_config(file_cfg=FILE)["profile"] == ""


def test_lists_replace_dicts_merge():
    base = {"a": {"x": 1, "y": [1, 2]}, "b": 1}
    assert ss.merge_config(base, {"a": {"y": [3]}}) == {"a": {"x": 1, "y": [3]}, "b": 1}
    assert base == {"a": {"x": 1, "y": [1, 2]}, "b": 1}     # inputs untouched


def test_storage_copy_replaces_the_repo_file(storage):
    assert ss.load_file_config(storage)["profiles"] == {}   # repo default when storage has none
    storage.write_json(ss.CONFIG_KEY, {"profiles": {"p": {"fields": {}}}, "by_site": {"s": {"profile": "p"}}})
    doc = ss.load_file_config(storage)
    assert "_comment" not in doc and doc["by_site"] == {"s": {"profile": "p"}}
    assert ss.resolve_config(site="s", storage=storage)["profile"] == "p"


# ---------------------------------------------------------------------------
# the repo default config file
# ---------------------------------------------------------------------------

def test_repo_config_is_valid_generic_and_inert():
    doc = json.loads((ROOT / "config" / "spec-sources-config.json").read_text())
    assert doc["schema_version"] == "1"
    assert doc["defaults"] == {} and doc["profiles"] == {} and doc["by_site"] == {} and doc["by_agent_id"] == {}
    cfg = ss.resolve_config(file_cfg=doc)
    assert cfg["config_problems"] == [] and cfg["llm"]["enabled"] is False
    assert cfg["adjudication"]["enabled"] is False and cfg["adjudication"]["daily_budget_h"] == 0.1
    assert cfg["llm"]["batch_size"] == 10 and cfg["llm"]["pool_reserve"] == 2
    engine = ss.SpecSources(cfg)
    assert engine.problems == []


def test_example_profiles_resolve_cleanly():
    for name in PROFILES:
        engine = ss.SpecSources(ss.resolve_config(file_cfg={"profiles": PROFILES}, site_cfg={"profile": name}))
        assert engine.problems == [], (name, engine.problems)


SITE_AND_NICHE_WORDS = ["aisleprompt", "specpicks", "runtimepicks", "jackery", "pecron", "fossibot",
                        "ecovacs", "roborock", "deebot", "eufy", "power station", "powerstation",
                        "vacuum", "solar", "lifepo", "inverter", "cpap"]


@pytest.mark.parametrize("path", ["framework/core/spec_sources.py", "config/spec-sources-config.json"])
def test_no_site_or_niche_literals(path):
    text = (ROOT / path).read_text().lower()
    found = [w for w in SITE_AND_NICHE_WORDS if w in text]
    assert found == [], f"{path} names {found}; niche and site values belong in a profile/config"


# ---------------------------------------------------------------------------
# inertness
# ---------------------------------------------------------------------------

def test_building_an_engine_touches_nothing(tmp_path):
    cache = tmp_path / "cache"
    engine = ss.SpecSources(ss.resolve_config(file_cfg={}), cache_dir=cache,
                            transport=lambda *a: pytest.fail("no request expected"))
    assert engine.problems == []
    assert not cache.exists()                               # the fetcher is created lazily
    _ = engine.fetcher
    assert not cache.exists()                               # and writes nothing until a fetch


def test_module_has_no_import_time_side_effects():
    src = (ROOT / "framework/core/spec_sources.py").read_text()
    top_level_calls = [ln for ln in src.splitlines()
                       if re.match(r"^[a-z_]+\(", ln) and not ln.startswith(("def ", "class "))]
    assert top_level_calls == []
    assert "get_storage()" not in src.split("def load_file_config", 1)[0]


# ---------------------------------------------------------------------------
# fixture provenance
# ---------------------------------------------------------------------------

def test_fixture_sources_are_documented_and_present():
    meta = json.loads((FX / "SOURCES.json").read_text())
    for d in meta["documents"]:
        assert (FX / d["file"]).is_file(), d["file"]
        assert d["kind"] in ss.SOURCE_KINDS and d["url"].startswith("https://") and d["fetched_on"]
        assert not ss.is_amazon_url(d["url"])
    for s in meta["synthetic"] + meta["robots"]:
        assert (FX / s["file"]).is_file(), s["file"]
    listed = {d["file"] for d in meta["documents"]} | {s["file"] for s in meta["synthetic"]} \
        | {r["file"] for r in meta["robots"]}
    on_disk = {str(p.relative_to(FX)) for p in FX.rglob("*") if p.is_file()
               and p.parent.name in ("docs", "amazon", "robots")}
    assert on_disk == listed


# ---------------------------------------------------------------------------
# CLI (offline)
# ---------------------------------------------------------------------------

def test_cli_units(capsys):
    assert ss.main(["units", "Supports 16–60V, 23.8 lbs", "--to", "kg"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert [(r["value"], r["unit"], r["range"]) for r in out] == [(16.0, "V", "lo"), (60.0, "V", "hi"), (23.8, "lb", None)]
    assert out[2]["converted"] == pytest.approx(10.7955)


def test_cli_robots(capsys):
    f = str(FX / "robots" / "jackery.com.robots.txt")
    assert ss.main(["robots", f, "https://www.jackery.com/cart/"]) == 0
    assert json.loads(capsys.readouterr().out) == {"allowed": False, "crawl_delay": None}


def test_cli_extract(tmp_path, capsys):
    cfg_file = tmp_path / "cfg.json"
    cfg_file.write_text(json.dumps({"profiles": PROFILES}))
    doc = str(FX / "docs" / "jackery-explorer-1000-v2-faq.html")
    rc = ss.main(["--config-file", str(cfg_file), "--profile", "home-power", "extract", doc,
                  "--url", "https://www.jackery.com/blogs/jackery-faqs/explorer-1000-v2-basic-information",
                  "--kind", "manufacturer-faq", "--brand", "jackery", "--model-key", "je-1000d"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["brand"] == "jackery" and out["errors"] == [] and out["problems"] == []
    assert out["summary"]["values"]["pv_v_min"] == 16.0
    assert out["summary"]["values"]["model_number"] == "JE-1000D"
    assert all(r["source_kind"] == "manufacturer-faq" for r in out["provenance"])


def test_cli_config(tmp_path, capsys):
    cfg_file = tmp_path / "cfg.json"
    cfg_file.write_text(json.dumps({"profiles": PROFILES}))
    assert ss.main(["--config-file", str(cfg_file), "--profile", "robot-vacuum", "config"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["profile"] == "robot-vacuum" and "suction_pa" in out["fields"]


# ---------------------------------------------------------------------------
# Review round 1: an unreadable storage config is a problem, not a silent default
# ---------------------------------------------------------------------------

def test_unreadable_storage_config_is_reported(storage, monkeypatch):
    assert ss.resolve_config(site="s", storage=storage)["config_problems"] == []   # absent copy: fine

    def archived(key):
        raise OSError("blob is in the archive tier")
    monkeypatch.setattr(storage, "read_json", archived)
    problems: list[str] = []
    doc = ss.load_file_config(storage, problems=problems)
    assert doc["profiles"] == {} and len(problems) == 1 and "unreadable" in problems[0]
    cfg = ss.resolve_config(site="s", storage=storage)
    assert any("unreadable" in p for p in cfg["config_problems"])
    assert any("unreadable" in p for p in ss.SpecSources(cfg).problems)


def test_refuse_lists_add_to_the_builtin_rules():
    extra = "(^|\\.)listing-mirror\\.example$"
    cfg = ss.resolve_config(file_cfg={"defaults": {"refuse": {"host_patterns": [extra]}}})
    ref = ss.SpecSources(cfg).refusal
    assert ref.url_reason("https://listing-mirror.example/x").startswith("amazon-host")
    assert ref.url_reason("https://www.amazon.com/s?k=x").startswith("amazon-host")
    shape = json.loads((ROOT / "config" / "spec-sources-config.json").read_text())["_shape"]["defaults.refuse"]
    assert "no layer can remove" in shape and "extend, never empty" not in shape
