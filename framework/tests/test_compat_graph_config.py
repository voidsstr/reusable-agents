"""framework.core.compat_graph: configuration, the repo config file and the CLI.

The repo config encodes binding amendment 6 as versioned data (pv-cold-voc
v2: -40 °C, NEC 690.7(A) when tc is unknown, >= 5 % headroom) and the year-1
exclusions, selects no site (inert), and every profile compiles without a
problem. Layering (DEFAULTS <- defaults <- profile <- by_site <- site_cfg <-
by_agent_id), a storage copy replacing the repo file, a one-rule version
bump through a site layer, no site-name literals, and the offline CLI."""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from framework.core import compat_graph as cg

ROOT = Path(__file__).resolve().parents[2]
REPO_CFG = ROOT / "config" / "compat-graph-config.json"
MODULE = ROOT / "framework" / "core" / "compat_graph.py"
FIX = Path(__file__).resolve().parent / "fixtures" / "compat_graph"


@pytest.fixture(scope="module")
def doc():
    return json.loads(REPO_CFG.read_text())


def test_repo_config_is_inert_and_documented(doc):
    assert doc["schema_version"] == "1"
    assert doc["by_site"] == {} and doc["by_agent_id"] == {} and doc["defaults"] == {}
    assert "_comment" in doc and "_shape" in doc
    assert set(doc["profiles"]) == {"home-power", "robot-consumables"}


@pytest.mark.parametrize("profile", ["home-power", "robot-consumables"])
def test_every_profile_compiles_without_problems(doc, profile):
    g = cg.CompatGraph(cg.resolve_config(site_cfg={"profile": profile}, file_cfg=doc))
    assert g.problems == []
    assert g.rulesets and all(rs.enabled for rs in g.rulesets.values())


def test_without_a_profile_nothing_is_evaluated(doc):
    g = cg.CompatGraph(cg.resolve_config(file_cfg=doc))
    assert g.rulesets == {} and g.problems == []


def test_home_power_encodes_binding_amendment_6(doc):
    g = cg.CompatGraph(cg.resolve_config(site_cfg={"profile": "home-power"}, file_cfg=doc))
    cold = g.rules["pv-cold-voc"]
    assert (cold.kind, cold.version) == ("max_voltage_cold", "2")
    assert cold.params["headroom"] == 0.05
    assert dict(cold.a_fields) == {"max_v": "pv_voc_max"}
    assert dict(cold.b_fields) == {"voc": "voc", "tc": "temp_coeff_voc_pct_c", "technology": "cell_type"}
    assert g.default_climate.design_temp_c == -40.0
    assert cg.nec_factor(-40, g.nec_table) == 1.25
    solar = g.rulesets["solar-charging"]
    assert [r.id for r in solar.rules][0] == "mfr-list"                 # manufacturer list first
    assert {r.kind for r in solar.rules} == set(cg.RULE_KINDS)
    assert cg.UNKNOWN not in solar.store_verdicts
    assert g.rules["pv-connector"].params["max_adapters"] == 1
    assert g.rules["pv-connector"].params["fallback_alias_sides"] == ("b",)     # panels only
    assert "manual-entry" not in g.rules["pv-connector"].params["fit_source_kinds"]
    assert g.rules["mfr-list"].params["on_disagreement"] == "prefer_manufacturer"
    assert g.rules["pv-max-current"].a_fields["current_limited"] == "pv_current_limited"


def test_home_power_year1_exclusions(doc):
    g = cg.CompatGraph(cg.resolve_config(site_cfg={"profile": "home-power"}, file_cfg=doc))
    excluded = g._policy["exclude_connectors"]
    assert {"l14-30", "l5-30", "nema-14-50"} <= excluded                  # amendment 8 by name
    assert {"nema-14-30", "nema-6-30", "nema-6-50", "l6-30", "l14-20", "nema-10-30", "cs6365"} <= excluded
    assert all(g.taxonomy.entries[c]["family"].startswith("ac-240") or c == "l5-30" for c in excluded)
    assert not {"tt-30", "nema-5-15", "nema-5-20"} & excluded             # RV kits and 120 V stay in scope
    for text, cid in (("NEMA L14-30P", "l14-30"), ("L5-30R", "l5-30"), ("14-50R", "nema-14-50"),
                      ("NEMA 14-30P", "nema-14-30"), ("6-50R", "nema-6-50"), ("L6-30P", "l6-30"),
                      ("CS6365", "cs6365")):
        assert g.taxonomy.resolve(text)[0] == (cid,)
    cord = cg.ModelSpec.from_values(9, "cable", {"connector_a": "L14-30P", "connector_b": "L14-30R"})
    assert g.excluded_reason(cord) == "excluded-connector:l14-30"
    hot = cg.ModelSpec.from_values(9, "cable", {"ac_voltage": 240})
    assert g.excluded_reason(hot).startswith("excluded-model:ac_voltage:gte")
    grid = cg.ModelSpec.from_values(9, "station", {"grid_interactive": True})
    assert g.excluded_reason(grid) == "excluded-model:grid_interactive:truthy:?"


@pytest.mark.parametrize("vals,reason", [
    ({"connector_a": "NEMA 14-30P", "connector_b": "NEMA 6-30R"}, "excluded-connector:nema-14-30"),
    ({"connector_a": "L6-30P", "connector_b": "L6-30R"}, "excluded-connector:l6-30"),
    ({"connector_a": "L14-30P to TT-30R adapter", "connector_b": "TT-30R"}, "excluded-connector:l14-30"),
    ({"connector_a": "TT-30P", "connector_b": "14-30R"}, "excluded-connector:nema-14-30"),
    ({"connector_a": "TT-30P", "connector_b": "TT-30R", "ac_voltage": "120/240V"},
     "excluded-model:ac_voltage:gte:200"),
])
def test_home_power_excludes_every_240_v_cord_it_can_recognise(doc, vals, reason):
    g = cg.CompatGraph(cg.resolve_config(site_cfg={"profile": "home-power"}, file_cfg=doc))
    for kind in ("adapter", "cable", "cord"):
        assert g.excluded_reason(cg.ModelSpec.from_values(9, kind, vals)) == reason
    rv = cg.ModelSpec.from_values(9, "cable", {"connector_a": "TT-30P", "connector_b": "TT-30R",
                                               "ac_voltage": "120V"})
    assert g.excluded_reason(rv) == ""


EN_DASH, NB_HYPHEN = "\u2013", "\u2011"     # NFKC turns the non-breaking hyphen into U+2010
# How makers write 240 V cords: Unicode dashes, a space between the groups,
# or 240 V wording that names no plug. text -> the reason prefix.
HOT_CORD_TEXTS = {
    f"NEMA L14{EN_DASH}30P": "excluded-connector:l14-30",
    f"L14{NB_HYPHEN}30R": "excluded-connector:l14-30",
    f"NEMA 14{EN_DASH}50R": "excluded-connector:nema-14-50",
    f"L5{EN_DASH}30P": "excluded-connector:l5-30",
    f"6{EN_DASH}50R": "excluded-connector:nema-6-50",
    "NEMA L14 30P": "excluded-connector:l14-30",
    "NEMA 14 50P": "excluded-connector:nema-14-50",
    "30A 240V twist-lock": "excluded-model:{field}:matches:",
    "4-prong 30 amp generator plug": "excluded-model:{field}:matches:",
    "30 Amp 240V twist-lock": "excluded-model:{field}:matches:",
    "4-prong 30A twist lock plug": "excluded-model:{field}:matches:",
    "Twist Lock, 30 Amp": "excluded-model:{field}:matches:",
    "120/240V": "excluded-model:{field}:matches:",
    "30A 125/250V": "excluded-model:{field}:matches:",
    "220-240V": "excluded-model:{field}:matches:",
}
# Unresolved or universal-input text is not 240 V wording, and numbers are not plugs.
COOL_CORD_TEXTS = (
    "USB-C", "Anderson SB50", "MC4", "MC4 extension, 1000V DC", "AC adapter 100-240V",
    "100 - 240 V", "100V to 240V", "GX16 aviation (twist-lock)", "GX16 5A twist lock",
    "MC4 locking connectors, 30A 1000V DC", "Anderson SB50 50A", "XT90 40A", "30Ah",
    "MC4 extension cable, 620 mm", "XT60 (max 650 W)", "Anderson PP45 1450 W",
    "DC8020 to MC4 (1030 mm)", "XT60 (620 mm lead)",
)


@pytest.mark.parametrize("kind", ["adapter", "cable", "cord"])
def test_home_power_fails_closed_on_240_v_cord_spellings(doc, kind):
    """Amendment 8 holds however the maker spells the plug, in every connector
    field. Exclusion only removes edges, so it fails closed; text that is
    merely unresolved is never a reason by itself."""
    g = cg.CompatGraph(cg.resolve_config(site_cfg={"profile": "home-power"}, file_cfg=doc),
                       clock=lambda: "2026-11-17T00:00:00Z")
    fields = g._policy["connector_fields"]
    assert {"connector", "connector_a", "connector_b", "output_connectors"} <= set(fields)
    for text, reason in HOT_CORD_TEXTS.items():
        for f in fields:
            got = g.excluded_reason(cg.ModelSpec.from_values(9, kind, {f: text}))
            assert got.startswith(reason.format(field=f)), (f, text, got)
    for text in COOL_CORD_TEXTS:
        for f in fields:
            assert g.excluded_reason(cg.ModelSpec.from_values(9, kind, {f: text})) == "", (f, text)
    for pair in (("TT-30P", "TT-30R"), ("NEMA 5-15P", "TT-30R")):
        rv = cg.ModelSpec.from_values(9, kind, {"connector_a": pair[0], "connector_b": pair[1],
                                                "ac_voltage": "120V"})
        assert g.excluded_reason(rv) == "", pair

    # End to end: a manufacturer adapter_for statement never stores an edge for
    # such a cord (an ASCII-only exclusion stored a 'fits' edge for these).
    if kind not in g.rulesets["adapter-fit"].b_kinds:
        return
    src = {"source_url": "https://maker.example/c", "source_kind": "manufacturer-page", "source_id": 1}
    station = cg.ModelSpec.from_values(1, "station", {"pv_voc_max": 60}, source=src)
    rec = cg.FitRecord(a=1, b=22, relation="adapter_for", source_url="https://maker.example/c",
                       source_kind="manufacturer-page")

    def evaluate(connector_a):
        cord = cg.ModelSpec.from_values(22, kind, {"connector_a": connector_a, "connector_b": "TT-30R"},
                                        source=src)
        return g.evaluate_pair(station, cord, ruleset="adapter-fit", records=[rec])

    ok = evaluate("DC8020")                                   # the same statement for a plain cable
    assert isinstance(ok, cg.Edge) and (ok.verdict, ok.store) == (cg.FITS, True)
    for text in (f"NEMA L14{EN_DASH}30P", f"L14{NB_HYPHEN}30R", "NEMA L14 30P", "30A 240V twist-lock"):
        out = evaluate(text)
        assert isinstance(out, cg.Skip) and out.reason.startswith("excluded-"), (text, out)
    assert evaluate(f"NEMA L14{EN_DASH}30P").reason == "excluded-connector:l14-30:22"


def test_home_power_wording_rules_cover_cords_only(doc):
    """The 240 V wording rules carry kinds: a station or panel whose own
    connector text mentions 240 V keeps its edges (grid_interactive is the
    station rule)."""
    g = cg.CompatGraph(cg.resolve_config(site_cfg={"profile": "home-power"}, file_cfg=doc))
    rules = [m for m in g._policy["exclude_models"] if m["op"] == "matches"]
    assert sorted(m["field"] for m in rules) == sorted(g._policy["connector_fields"])
    assert all(sorted(m["kinds"]) == ["adapter", "cable", "cord"] for m in rules)
    station = cg.ModelSpec.from_values(1, "station",
                                       {"output_connectors": "120/240V, 4-prong 30A twist-lock"})
    panel = cg.ModelSpec.from_values(2, "panel", {"connector": "MC4 (250V DC)"})
    assert g.excluded_reason(station) == "" and g.excluded_reason(panel) == ""


@pytest.mark.parametrize("text,ids", [
    ("DC 8mm Ports", ("dc8020",)), ("8mm DC", ("dc8020",)), ("XT60i", ("xt60i",)),
    ("XT-60", ("xt60",)), ("Anderson Powerpole", ("anderson-pp45",)), ("Anderson PP45", ("anderson-pp45",)),
    ("GX16MF-5", ("gx16",)), ("MC4 and DC7909", ("mc4", "dc7909")), ("5.5 x 2.1 mm", ("dc5521",)),
    ("TT-30R", ("tt-30",)), ("MC4-compatible", ("mc4",)),
    # specific names win over generic wording; generic wording alone is not a port
    ("Solar connector: DC7909", ("dc7909",)), ("PV connector (DC7909)", ("dc7909",)),
    ("Solar Connectors: XT60i", ("xt60i",)), ("XT60 solar connector", ("xt60",)),
    ("8mm DC solar connector", ("dc8020",)), ("Photovoltaic connectors", ()),
    # loose aliases that used to invent ports
    ("Anderson", ()), ("Anderson SB50", ()), ("EcoFlow app control", ()), ("Model 8020 panel", ()),
    ("Aviation plug", ()), ("DC8020 (requires MC4 adapter)", ()), ("Anderson solar connector", ()),
])
def test_home_power_connector_aliases(doc, text, ids):
    g = cg.CompatGraph(cg.resolve_config(site_cfg={"profile": "home-power"}, file_cfg=doc))
    assert g.taxonomy.resolve(text)[0] == ids


@pytest.mark.parametrize("text,ids", [
    ("Photovoltaic connectors", ("mc4",)), ("PV connector", ("mc4",)), ("solar  connectors", ("mc4",)),
    ("XT60 solar connector", ("xt60",)), ("Anderson solar connector", ()),
])
def test_home_power_generic_pv_wording_is_a_panel_side_fallback(doc, text, ids):
    g = cg.CompatGraph(cg.resolve_config(site_cfg={"profile": "home-power"}, file_cfg=doc))
    assert g.taxonomy.resolve(text, fallback=True)[0] == ids


def test_home_power_generic_station_wording_never_makes_a_direct_fit(doc):
    g = cg.CompatGraph(cg.resolve_config(site_cfg={"profile": "home-power"}, file_cfg=doc),
                       clock=lambda: "2026-11-17T00:00:00Z")
    src = {"source_url": "https://maker.example/s", "source_kind": "manufacturer-page", "source_id": 1}
    panel = cg.ModelSpec.from_values(2, "panel", {"voc": 21.5, "vmp": 18.4, "isc": 5.0, "connector": "MC4"},
                                     source=src)
    for text in ("Solar connector: DC7909", "PV connector", "DC8020 (requires MC4 adapter)",
                 "XT60 solar connector"):
        station = cg.ModelSpec.from_values(1, "station", {"pv_voc_max": 60, "pv_v_min": 12,
                                                          "pv_current_limited": True,
                                                          "pv_input_connectors": text}, source=src)
        e = g.evaluate_pair(station, panel, ruleset="solar-charging")
        assert e.verdict == cg.UNKNOWN and e.config["connector_path"]["code"] != "direct", text


def test_config_layers_merge_in_order(doc):
    file_cfg = json.loads(json.dumps(doc))
    file_cfg["defaults"] = {"profile": "home-power", "store_verdicts": ["fits"]}
    file_cfg["by_site"] = {"site-a": {"climate": {"zip_min_c": {"33101": 7.0}},
                                      "rules": {"pv-min-voltage": {"version": 5}}}}
    file_cfg["by_agent_id"] = {"agent-a": {"rulesets": {"adapter-fit": {"enabled": False}}}}
    cfg = cg.resolve_config(site="site-a", agent_id="agent-a", file_cfg=file_cfg,
                            site_cfg={"texts": {"per-input": "Per port."}})
    assert cfg["profile"] == "home-power" and cfg["site"] == "site-a" and cfg["agent_id"] == "agent-a"
    g = cg.CompatGraph(cfg)
    assert g.climate(zip_code="33101").design_temp_c == 7.0
    assert g.rules["pv-min-voltage"].version == "5"
    assert g.texts["per-input"] == "Per port."
    assert not g.rulesets["adapter-fit"].enabled
    assert g.rulesets["solar-charging"].store_verdicts != frozenset({"fits"})   # its own list wins
    assert g.rulesets["expansion-battery"].store_verdicts == frozenset({"fits", "no_fit"})


def test_unknown_profile_is_reported(doc):
    cfg = cg.resolve_config(site_cfg={"profile": "nope"}, file_cfg=doc)
    assert cfg["config_problems"] == ["unknown profile 'nope'"]
    assert cg.CompatGraph(cfg).problems == ["unknown profile 'nope'"]


def test_a_one_rule_version_bump_through_a_site_layer_touches_only_its_rulesets(doc):
    base = cg.CompatGraph(cg.resolve_config(site_cfg={"profile": "home-power"}, file_cfg=doc))
    bumped = cg.CompatGraph(cg.resolve_config(
        site_cfg={"profile": "home-power", "rules": {"mfr-list": {"version": 2}}}, file_cfg=doc))
    for rsid in ("solar-charging", "expansion-battery", "adapter-fit"):
        assert bumped.ruleset_version(rsid) != base.ruleset_version(rsid)
    cold_only = cg.CompatGraph(cg.resolve_config(
        site_cfg={"profile": "home-power", "rules": {"pv-cold-voc": {"version": 3}}}, file_cfg=doc))
    assert cold_only.ruleset_version("solar-charging") != base.ruleset_version("solar-charging")
    assert cold_only.ruleset_version("expansion-battery") == base.ruleset_version("expansion-battery")


def test_storage_copy_replaces_the_repo_file(storage):
    storage.write_json(cg.CONFIG_KEY, {"profiles": {"p": {"rules": {"m": {"kind": "manufacturer_list",
                                                                        "version": 7}}}}})
    assert cg.load_file_config(storage)["profiles"]["p"]["rules"]["m"]["version"] == 7
    cfg = cg.resolve_config(site_cfg={"profile": "p"}, storage=storage)
    assert cg.CompatGraph(cfg).rules["m"].version == "7"


def test_unreadable_storage_falls_back_to_the_repo_file(doc):
    class Broken:
        def read_json(self, key):
            raise OSError("down")
    assert cg.load_file_config(Broken()) == doc


def test_no_site_name_literals_in_module_or_config():
    for path in (MODULE, REPO_CFG):
        text = path.read_text().lower()
        for name in ("specpicks", "aisleprompt", "runtimepicks"):
            assert name not in text, (path.name, name)


def test_module_is_inert_on_import():
    src = MODULE.read_text()
    imported = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    # No network-capable module, and the only writer takes a caller-owned
    # connection (psycopg2 appears only as execute_batch, imported lazily).
    assert not imported & {"urllib", "requests", "httpx", "socket", "http", "subprocess"}
    assert "psycopg2.connect" not in src and ".connect(" not in src


# ---------------------------------------------------------------------------
# CLI (offline, read-only)
# ---------------------------------------------------------------------------

def run_cli(capsys, *argv):
    assert cg.main(list(argv)) == 0
    return capsys.readouterr().out


def test_cli_factor_and_ddl(capsys):
    out = json.loads(run_cli(capsys, "factor", "--temp", "-40", "--tc", "-0.28"))
    assert out["nec_690_7_a"] == 1.25
    assert out["temperature_coefficient"] == pytest.approx(1.182)
    assert "CREATE TABLE IF NOT EXISTS edges" in run_cli(capsys, "ddl", "--table", "edges")


def test_cli_config_and_rules(capsys):
    out = json.loads(run_cli(capsys, "--config-file", str(REPO_CFG), "--profile", "home-power", "config"))
    assert out["problems"] == [] and out["config"]["profile"] == "home-power"
    rules = json.loads(run_cli(capsys, "--config-file", str(REPO_CFG), "--profile", "home-power", "rules"))
    assert rules["solar-charging"]["rule_version"].startswith("solar-charging@1[mfr-list@1,pv-cold-voc@2")


def test_cli_eval(capsys, tmp_path):
    models = [
        {"model_id": 1, "kind": "station", "name": "S", "values": {
            "pv_voc_max": {"value": 60, "unit": "V", "source_url": "https://m.example/s",
                           "source_kind": "manufacturer-page", "source_id": 1},
            "pv_v_min": 16, "pv_a_max": 10.5, "pv_w_max": 200, "pv_current_limited": True,
            "pv_input_connectors": "DC8020"}},
        {"model_id": 2, "kind": "panel", "name": "P", "values": {
            "voc": 23.4, "vmp": 19.5, "isc": 11.6, "rated_w": 200, "connector": "DC8020"}},
    ]
    f = tmp_path / "models.json"
    f.write_text(json.dumps({"models": models}))
    out = json.loads(run_cli(capsys, "--config-file", str(REPO_CFG), "--profile", "home-power",
                             "eval", str(f)))
    (e,) = out["edges"]
    assert (e["verdict"], e["config"]["voltage_factor"]) == ("fits", 1.25)
    assert out["summary"]["design_temp_c"] == -40.0
    warm = {"profiles": {"home-power": {"climate": {"zip_min_c": {"33101": 7.0}}}}}
    cfg_file = tmp_path / "cfg.json"
    cfg_file.write_text(json.dumps(cg.merge_config(json.loads(REPO_CFG.read_text()), warm)))
    out2 = json.loads(run_cli(capsys, "--config-file", str(cfg_file), "--profile", "home-power",
                              "eval", str(f), "--zip", "33101", "--all"))
    assert out2["summary"]["design_temp_c"] == 7.0 and out2["summary"]["climate_basis"] == "zip"


def test_fixture_catalog_names_only_profile_fields(doc):
    """Every field a home-power rule reads is defined by the fixture field
    registry (the spec_sources profile a site must ship, see deferred wiring)."""
    cat = json.loads((FIX / "catalog.json").read_text())
    g = cg.CompatGraph(cg.resolve_config(site_cfg={"profile": "home-power"}, file_cfg=doc))
    used = set()
    for r in g.rules.values():
        used |= set(r.a_fields.values()) | set(r.b_fields.values())
        used |= {v for k, v in (r.params.get("adapter_fields") or {}).items()}
    assert used <= set(cat["fields"]), used - set(cat["fields"])
