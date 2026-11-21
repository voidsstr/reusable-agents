"""framework.core.compat_graph: the rule kinds one at a time.

NEC 690.7(A) lookups, the cold open-circuit-voltage rule with its 5 %
headroom (at the boundaries), unknown / implausible temperature
coefficients, minimum voltage, current and current limiting, watts
clipping, connector paths (direct, mating pairs, one manufacturer adapter,
never two), connector ratings, wiring constraints, verdict labels, climate
lookups, safety floors and config validation. Synthetic models; no I/O."""
from __future__ import annotations

import copy

import pytest

from framework.core import compat_graph as cg

SRC = {"source_url": "https://maker.example/spec", "source_kind": "manufacturer-page",
       "source_id": 1}
CLOCK = "2026-11-17T00:00:00Z"

CONNECTORS = {
    "mc4": {"aliases": [r"\bmc-?4\b", r"photovoltaic connectors?"], "series": True,
            "parallel": True, "max_a": 30, "max_v": 600},
    "dc8020": {"aliases": [r"\bdc ?8020\b", r"\bdc ?8 ?mm\b"], "parallel": True},
    "xt60": {"aliases": [r"\bxt-?60\b(?!i)"], "mates_with": ["xt60i"]},
    "xt60i": {"aliases": [r"\bxt-?60i\b"]},
    "anderson": {"aliases": [r"\banderson\b", r"\bpowerpole\b"], "parallel": True},
    "l14-30": {"aliases": [r"\bl14-?30"]},
}

RULES = {
    "cold": {"kind": "max_voltage_cold", "version": 1, "a_fields": {"max_v": "pv_voc_max"},
             "b_fields": {"voc": "voc", "tc": "tc", "technology": "cell"}},
    "minv": {"kind": "min_voltage", "version": 1, "a_fields": {"min_v": "pv_v_min"},
             "b_fields": {"vmp": "vmp"}},
    "cur": {"kind": "max_current", "version": 1,
            "a_fields": {"max_a": "pv_a_max", "current_limited": "limited"},
            "b_fields": {"isc": "isc"}},
    "conn": {"kind": "connector_path", "version": 1, "a_fields": {"connectors": "pv_conn"},
             "b_fields": {"connectors": "conn"}},
    "watts": {"kind": "watts_clip", "version": 1, "a_fields": {"max_w": "pv_w_max"},
              "b_fields": {"rated_w": "rated_w"}},
    "mfr": {"kind": "manufacturer_list", "version": 1},
}


def make_graph(rule_ids=("cold",), *, arrays=None, params=None, ruleset=None, cfg=None):
    rules = copy.deepcopy(RULES)
    for rid, p in (params or {}).items():
        rules[rid].setdefault("params", {}).update(p)
    rs = {"version": 1, "relation": "charges_from", "a_kinds": ["station"],
          "b_kinds": ["panel"], "rules": list(rule_ids)}
    if arrays:
        rs["arrays"] = arrays
    rs.update(ruleset or {})
    layer = cg.merge_config({"connectors": CONNECTORS, "rules": rules, "rulesets": {"solar": rs}},
                            cfg or {})
    return cg.CompatGraph(cg.resolve_config(site_cfg=layer, file_cfg={}), clock=lambda: CLOCK)


def station(mid=1, **vals):
    return cg.ModelSpec.from_values(mid, "station", vals, name=f"Station {mid}", source=SRC)


def panel(mid=2, **vals):
    return cg.ModelSpec.from_values(mid, "panel", vals, name=f"Panel {mid}", source=SRC)


def adapter(mid=9, a="MC4", b="DC8020", source=None, kind="adapter", **vals):
    src = source if source is not None else SRC
    values = {"connector_a": {"value": a, **_fv(src)}, "connector_b": {"value": b, **_fv(src)}}
    values.update(vals)
    return cg.ModelSpec.from_values(mid, kind, values, name=f"Adapter {mid}", source=src)


def _fv(src):
    return {"source_url": src.get("source_url", ""), "source_kind": src.get("source_kind", ""),
            "source_id": src.get("source_id")}


def edge(g, a, b, **kw):
    out = g.evaluate_pair(a, b, ruleset="solar", **kw)
    assert isinstance(out, cg.Edge), out
    return out


def check(e, rule_id):
    (c,) = [c for c in e.checks if c.rule_id == rule_id]
    return c


def codes(e):
    return {a.code for a in e.assumptions}


# ---------------------------------------------------------------------------
# NEC Table 690.7(A) and the cold factor
# ---------------------------------------------------------------------------

NEC_ROWS = [  # (warmest, coldest) °C of each published row, factor
    (24, 20, 1.02), (19, 15, 1.04), (14, 10, 1.06), (9, 5, 1.08), (4, 0, 1.10),
    (-1, -5, 1.12), (-6, -10, 1.14), (-11, -15, 1.16), (-16, -20, 1.18), (-21, -25, 1.20),
    (-26, -30, 1.21), (-31, -35, 1.23), (-36, -40, 1.25),
]


@pytest.mark.parametrize("warm,cold,factor", NEC_ROWS)
def test_nec_factor_reproduces_every_integer_row_of_the_table(warm, cold, factor):
    for t in range(cold, warm + 1):
        assert cg.nec_factor(t) == factor, t


@pytest.mark.parametrize("t,expected", [
    (25, 1.0), (40, 1.0), (24.9, 1.02), (19.5, 1.04), (-0.5, 1.12), (-35.5, 1.25),
    (-40, 1.25), (-40.0001, None), (-60, None),
])
def test_nec_factor_between_rows_takes_the_colder_row_and_none_below_the_table(t, expected):
    assert cg.nec_factor(t) == expected


def test_config_rows_can_extend_or_raise_the_nec_table_but_never_lower_it():
    table = cg.merged_nec_table([[-40, 1.20], [-45, 1.27], [20, 1.05], ["x", 2], [0, 0.9]])
    assert cg.nec_factor(-40, table) == 1.25          # cannot lower the code's row
    assert cg.nec_factor(-43, table) == 1.27          # extends below -40
    assert cg.nec_factor(-45, table) == 1.27
    assert cg.nec_factor(-46, table) is None
    assert cg.nec_factor(22, table) == 1.05           # raised
    assert cg.nec_factor(17, table) == 1.05           # ... and a colder band is never lower
    assert cg.nec_factor(0, table) == 1.10            # a factor below 1 is ignored


@pytest.mark.parametrize("rows,t,code_factor", [
    ([[-37, 1.24]], -36, 1.25),     # a row inside the code's -36..-40 band
    ([[-37, 1.0]], -38, 1.25),
    ([[19, 1.0]], 19, 1.04),
    ([[-1, 1.02]], -1, 1.12),
    ([[-45, 1.0]], -43, 1.25),      # an extension row below the table is at least x1.25
    ([[-45, 1.0]], -40, 1.25),
])
def test_rows_inside_a_band_or_below_the_table_never_lower_a_factor(rows, t, code_factor):
    assert cg.nec_factor(t, cg.merged_nec_table(rows)) == code_factor
    assert cg.nec_factor(t, rows) >= code_factor                      # any table, not only merged ones
    assert cg.nec_table_problems(rows), rows                          # and the row is reported


def test_a_lowering_nec_row_cannot_turn_an_over_limit_string_into_a_fit():
    g = make_graph(cfg={"tables": {"nec_690_7_a": [[-37, 1.0]]},
                        "climate": {"zip_min_c": {"55101": -36.0}}})
    assert any("tables.nec_690_7_a row [-37, 1]" in p for p in g.problems)
    e = edge(g, station(pv_voc_max=60), panel(voc=50.0), climate=g.climate(zip_code="55101"))
    assert e.verdict == cg.OVER_LIMIT and e.config["voltage_factor"] == 1.25   # 62.5 V > 57 V
    assert cg.nec_table_problems([[-50, 1.30], [20, 1.05]]) == []
    assert cg.nec_table_problems([["x", 2]]) and cg.nec_table_problems([[0, 0.9]])


def test_coefficient_factor_matches_the_formula_and_never_drops_below_one():
    assert cg.coefficient_factor(-0.28, -40) == pytest.approx(1.182)
    # The pre-review draft: -0.28 %/°C at -20 °C was only x1.126.
    assert cg.coefficient_factor(-0.28, -20) == pytest.approx(1.126)
    assert cg.coefficient_factor(-0.30, 40) == 1.0
    assert cg.coefficient_factor(0.05, -40) == 1.0


@pytest.mark.parametrize("value,unit,voc,expected", [
    (-0.28, "%/°C", None, -0.28), (-0.28, "%/K", None, -0.28), (-0.28, "", None, -0.28),
    (-0.28, "%/degC", None, -0.28), (-0.16, "%/°F", None, -0.288), (-0.0028, "1/K", None, -0.28),
    (-60.2, "mV/°C", 21.5, -0.28), (-0.0602, "V/K", 21.5, -0.28), ("-0.3", "%/℃", None, -0.3),
])
def test_temperature_coefficient_units(value, unit, voc, expected):
    assert cg.tc_to_pct_per_c(value, unit, voc) == pytest.approx(expected)


@pytest.mark.parametrize("value,unit,voc", [
    (-60, "mV/°C", None), (-0.3, "furlongs", None), ("n/a", "%/°C", None), (None, "%/°C", None),
])
def test_unreadable_temperature_coefficients_are_none(value, unit, voc):
    assert cg.tc_to_pct_per_c(value, unit, voc) is None


# ---------------------------------------------------------------------------
# max_voltage_cold
# ---------------------------------------------------------------------------

def test_published_tc_is_applied_at_minus_40_with_five_percent_headroom():
    g = make_graph()
    e = edge(g, station(pv_voc_max=60), panel(voc=24.0, tc=-0.30))
    assert e.verdict == cg.FITS
    c = check(e, "cold")
    assert c.code == "cold-voc-ok"
    assert c.values["factor"] == pytest.approx(1.195)
    assert c.values["cold_voc_v"] == pytest.approx(28.68)
    assert c.values["allowed_v"] == pytest.approx(57.0)
    assert e.config["design_temp_c"] == -40.0
    assert e.config["factor_method"] == "temperature-coefficient"
    assert {"design-temp-default", "voc-tc", "headroom"} <= codes(e)


def test_within_the_headroom_band_is_over_limit_not_a_fit():
    g = make_graph()
    # 49.5 V x (1 + 0.0027 x 65) = 58.19 V: under the 60 V maximum, over 57 V.
    e = edge(g, station(pv_voc_max=60), panel(voc=49.5, tc=-0.27))
    assert e.verdict == cg.OVER_LIMIT
    assert check(e, "cold").code == "cold-voc-insufficient-headroom"
    assert "57 V fit limit" in check(e, "cold").text


def test_exceeding_the_maximum_is_over_limit():
    g = make_graph()
    e = edge(g, station(pv_voc_max=50), panel(voc=49.5, tc=-0.27))
    assert e.verdict == cg.OVER_LIMIT
    assert check(e, "cold").code == "cold-voc-exceeds-max"


@pytest.mark.parametrize("voc,verdict,code", [
    (45.6, cg.FITS, "cold-voc-ok"),                                  # 57.000 V == allowed
    (45.6001, cg.OVER_LIMIT, "cold-voc-insufficient-headroom"),     # just over the fit limit
    (48.0, cg.OVER_LIMIT, "cold-voc-insufficient-headroom"),        # 60.000 V == maximum
    (48.0001, cg.OVER_LIMIT, "cold-voc-exceeds-max"),
])
def test_headroom_boundaries_are_exact(voc, verdict, code):
    g = make_graph()
    e = edge(g, station(pv_voc_max=60), panel(voc=voc))    # unknown tc: x1.25 at -40 °C
    assert (e.verdict, check(e, "cold").code) == (verdict, code)


def test_unknown_tc_uses_the_nec_factor_and_labels_the_assumptions():
    g = make_graph()
    e = edge(g, station(pv_voc_max=60), panel(voc=21.5))
    c = check(e, "cold")
    assert c.values["factor"] == 1.25 and c.values["method"] == "nec-690.7a"
    assert {"voc-nec", "cell-type-assumed"} <= codes(e)
    text = " ".join(a.text for a in e.assumptions)
    assert "x1.25" in text and "-40 °C" in text


def test_unknown_tc_with_a_known_crystalline_cell_type_does_not_claim_an_assumed_type():
    g = make_graph()
    e = edge(g, station(pv_voc_max=60), panel(voc=21.5, cell="N Type TOPCon monocrystalline silicon"))
    assert "voc-nec" in codes(e) and "cell-type-assumed" not in codes(e)


@pytest.mark.parametrize("cell", ["CIGS thin film", "Amorphous silicon (a-Si)", "CdTe"])
def test_unknown_tc_on_thin_film_is_unknown_because_the_nec_table_is_for_crystalline(cell):
    g = make_graph()
    e = edge(g, station(pv_voc_max=60), panel(voc=21.5, cell=cell))
    assert e.verdict == cg.UNKNOWN
    assert check(e, "cold").code == "tc-unknown-non-crystalline"


def test_thin_film_with_a_published_tc_is_evaluated():
    g = make_graph()
    e = edge(g, station(pv_voc_max=60), panel(voc=21.5, tc=-0.29, cell="CIGS thin film"))
    assert e.verdict == cg.FITS


@pytest.mark.parametrize("tc,factor", [
    (0.05, 1.25),       # positive: an Isc coefficient or a sign slip -> NEC
    (-0.1, 1.25),       # implausibly flat: NEC (1.065 would be optimistic)
    (-0.75, 1.4875),    # implausibly steep: its own factor is larger, so it wins
])
def test_implausible_tc_is_distrusted_and_the_larger_factor_wins(tc, factor):
    g = make_graph()
    e = edge(g, station(pv_voc_max=600), panel(voc=21.5, tc=tc))
    c = check(e, "cold")
    assert c.values["method"] == "nec-690.7a(tc-rejected)"
    assert c.values["factor"] == pytest.approx(factor)
    assert "voc-tc-rejected" in codes(e)


@pytest.mark.parametrize("tc", [-0.1, 0.05, -0.9])
def test_a_distrusted_tc_is_never_used_alone_below_the_nec_table(tc):
    # Regression (found by the property sweep): at -50 °C there is no NEC row,
    # and an implausibly flat -0.1 %/°C alone gave x1.075, LESS than x1.25 at
    # -40 °C, so colder weather made more strings "fit".
    g = make_graph()
    e = edge(g, station(pv_voc_max=600), panel(voc=21.5, tc=tc), climate=g.climate(design_temp_c=-50))
    assert e.verdict == cg.UNKNOWN
    assert check(e, "cold").code == "design-temp-below-nec-table"


def test_a_tc_in_an_unreadable_unit_falls_back_to_nec():
    g = make_graph()
    p = cg.ModelSpec.from_values(2, "panel", {"voc": 21.5, "tc": {"value": -0.3, "unit": "furlongs"}})
    c = check(edge(g, station(pv_voc_max=60), p), "cold")
    assert c.values["factor"] == 1.25 and c.values["method"] == "nec-690.7a(tc-rejected)"


def test_tc_in_millivolts_is_converted_with_voc():
    rules = copy.deepcopy(RULES)
    rules["cold"]["b_fields"] = {"voc": "voc", "tc_mv": "tc_mv"}
    g = cg.CompatGraph(cg.resolve_config(file_cfg={}, site_cfg={
        "rules": {"cold": rules["cold"]},
        "rulesets": {"solar": {"version": 1, "relation": "r", "a_kinds": ["station"],
                               "b_kinds": ["panel"], "rules": ["cold"]}}}))
    p = cg.ModelSpec.from_values(2, "panel", {"voc": 21.5, "tc_mv": {"value": -64.5, "unit": "mV/°C"}})
    c = check(edge(g, station(pv_voc_max=60), p), "cold")
    assert c.values["factor"] == pytest.approx(1 + 0.003 * 65)


def test_below_the_nec_table_unknown_tc_is_unknown_but_a_published_tc_still_works():
    g = make_graph()
    cold = g.climate(design_temp_c=-45)
    assert edge(g, station(pv_voc_max=60), panel(voc=21.5), climate=cold).verdict == cg.UNKNOWN
    e = edge(g, station(pv_voc_max=60), panel(voc=21.5, tc=-0.3), climate=cold)
    assert check(e, "cold").values["factor"] == pytest.approx(1 + 0.003 * 70)
    assert e.config["design_temp_c"] == -45 and "design-temp-caller" in codes(e)


@pytest.mark.parametrize("st,pn,missing", [
    ({}, {"voc": 21.5}, "missing:pv_voc_max"),
    ({"pv_voc_max": 60}, {}, "missing:voc"),
    ({"pv_voc_max": "sixty"}, {"voc": 21.5}, "not-a-number:pv_voc_max"),
])
def test_missing_or_unreadable_voltages_make_the_verdict_unknown(st, pn, missing):
    g = make_graph()
    e = edge(g, station(**st), panel(**pn))
    assert e.verdict == cg.UNKNOWN and check(e, "cold").code == missing


def test_values_in_other_units_are_converted():
    g = make_graph()
    p = cg.ModelSpec.from_values(2, "panel", {"voc": {"value": 21500, "unit": "mV"}})
    assert check(edge(g, station(pv_voc_max=60), p), "cold").values["voc_v"] == pytest.approx(21.5)
    bad = cg.ModelSpec.from_values(2, "panel", {"voc": {"value": 21.5, "unit": "A"}})
    assert check(edge(g, station(pv_voc_max=60), bad), "cold").code == "unit:voc:A"


# ---------------------------------------------------------------------------
# min_voltage, max_current, watts_clip
# ---------------------------------------------------------------------------

def test_min_voltage_pass_and_fail():
    g = make_graph(("cold", "minv"))
    assert edge(g, station(pv_voc_max=60, pv_v_min=12), panel(voc=21.5, vmp=18)).verdict == cg.FITS
    e = edge(g, station(pv_voc_max=95, pv_v_min=32), panel(voc=21.5, vmp=18.4))
    assert e.verdict == cg.NO_FIT and check(e, "minv").code == "vmp-below-min"
    assert "below the 32 V minimum" in check(e, "minv").text


def test_a_single_panel_below_the_minimum_fits_in_series():
    g = make_graph(("cold", "minv"), arrays={"max_series": 4, "max_parallel": 1, "max_units": 4})
    e = edge(g, station(pv_voc_max=95, pv_v_min=32), panel(voc=21.5, vmp=18.4))
    assert e.verdict == cg.FITS_IN_SERIES
    assert (e.config["series"], e.config["min_series"], e.config["max_series"]) == (2, 2, 3)
    by_s = {o.series: o for o in e.options}
    assert by_s[1].outcome == cg.NO_FIT and by_s[4].outcome == cg.OVER_LIMIT


def test_too_low_alone_and_too_high_in_series_is_no_fit():
    g = make_graph(("cold", "minv"), arrays={"max_series": 4, "max_parallel": 1, "max_units": 4})
    # 1 x 18.4 V < 40 V minimum; 2 x 21.5 x 1.25 = 53.75 V > 47.5 V allowed.
    e = edge(g, station(pv_voc_max=50, pv_v_min=40), panel(voc=21.5, vmp=18.4))
    assert e.verdict == cg.NO_FIT


def test_current_limit_documented_passes_even_above_the_rating():
    g = make_graph(("cold", "cur"))
    e = edge(g, station(pv_voc_max=60, pv_a_max=8, limited=True), panel(voc=21.5, isc=12.4))
    assert e.verdict == cg.FITS and check(e, "cur").code == "current-limited"
    assert "current-limited" in codes(e)


@pytest.mark.parametrize("limited", [False, "no", None])
def test_current_above_the_maximum_is_over_limit_without_documented_limiting(limited):
    g = make_graph(("cold", "cur"))
    st = station(pv_voc_max=60, pv_a_max=10, **({} if limited is None else {"limited": limited}))
    e = edge(g, st, panel(voc=21.5, isc=12.4))
    assert e.verdict == cg.OVER_LIMIT and check(e, "cur").code == "current-exceeds-max"


def test_missing_current_data_is_unknown_unless_configured_to_skip():
    g = make_graph(("cold", "cur"))
    e = edge(g, station(pv_voc_max=60), panel(voc=21.5, isc=12.4))
    assert e.verdict == cg.UNKNOWN and check(e, "cur").code == "missing:pv_a_max"
    g2 = make_graph(("cold", "cur"), params={"cur": {"on_missing": "skip"}})
    e2 = edge(g2, station(pv_voc_max=60), panel(voc=21.5, isc=12.4))
    assert e2.verdict == cg.FITS and check(e2, "cur").outcome == cg.SKIPPED
    assert "not-checked" in codes(e2)


def test_current_limited_only_rule_without_max_a_mapping():
    g = make_graph(("cold", "cur"), cfg={"rules": {"cur": {"a_fields": {"max_a": None}}}})
    e = edge(g, station(pv_voc_max=60), panel(voc=21.5, isc=12.4))
    assert e.verdict == cg.UNKNOWN and check(e, "cur").code == "missing:limited"


def test_isc_factor_makes_the_current_check_stricter():
    st, pn = station(pv_voc_max=60, pv_a_max=15), panel(voc=21.5, isc=12.4)
    assert edge(make_graph(("cold", "cur")), st, pn).verdict == cg.FITS
    strict = make_graph(("cold", "cur"), params={"cur": {"isc_factor": 1.25}})
    e = edge(strict, st, pn)
    assert e.verdict == cg.OVER_LIMIT
    assert check(e, "cur").values["design_a"] == pytest.approx(15.5)


def test_watts_clipping_is_informational_and_drives_the_recommendation():
    g = make_graph(("cold", "cur", "watts"),
                   arrays={"max_series": 1, "max_parallel": 4, "max_units": 4})
    st = station(pv_voc_max=60, pv_a_max=10.5, limited=True, pv_w_max=200)
    e = edge(g, st, panel(voc=24, isc=5.6, rated_w=100, tc=-0.3))
    assert e.verdict == cg.FITS
    # 2 x 100 W reaches the 200 W maximum; a third panel adds nothing.
    assert (e.config["series"], e.config["parallel"], e.config["usable_w"]) == (1, 2, 200.0)
    assert e.config["max_units"] == 4 and e.config["limits_capped"] is True
    assert "parallel-connector" in codes(e)
    clipped = edge(g, st, panel(voc=24, isc=5.6, rated_w=220, tc=-0.3))
    assert check(clipped, "watts").code == "watts-clipped" and "watts-clipped" in codes(clipped)


def test_clipping_note_is_dropped_from_a_non_fit():
    g = make_graph(("cold", "watts"))
    e = edge(g, station(pv_voc_max=30, pv_w_max=100), panel(voc=49.5, rated_w=400))
    assert e.verdict == cg.OVER_LIMIT and "watts-clipped" not in codes(e)


# ---------------------------------------------------------------------------
# connector_path
# ---------------------------------------------------------------------------

def solar_conn(**kw):
    return make_graph(("cold", "conn"), **kw)


def test_direct_connector_match_from_free_text():
    g = solar_conn()
    e = edge(g, station(pv_voc_max=60, pv_conn="DC 8mm Ports"), panel(voc=21.5, conn="DC8020"))
    assert e.verdict == cg.FITS and e.via_model_id is None
    assert e.config["connector_path"]["code"] == "direct"


def test_declared_mating_connectors_connect_directly():
    g = solar_conn()
    e = edge(g, station(pv_voc_max=60, pv_conn="XT60i"), panel(voc=21.5, conn="XT60"))
    assert e.verdict == cg.FITS and e.config["connector_path"]["joint"] == ["xt60i", "xt60"]


def test_one_manufacturer_adapter_bridges_the_connectors():
    g = solar_conn()
    st, pn = station(pv_voc_max=60, pv_conn="DC8020"), panel(voc=21.5, conn="Photovoltaic connectors")
    e = edge(g, st, pn, adapters=[adapter(9, "MC4", "DC8020", max_a=15, max_v=60)])
    assert e.verdict == cg.FITS_WITH_ADAPTER and e.via_model_id == 9
    assert "adapter" in codes(e)
    assert any(ev.role == "via" for ev in e.evidence)


def test_no_documented_path_is_unknown_or_no_fit_by_config():
    st, pn = station(pv_voc_max=60, pv_conn="DC8020"), panel(voc=21.5, conn="MC4")
    e = edge(solar_conn(), st, pn)
    assert e.verdict == cg.UNKNOWN and check(e, "conn").code == "no-documented-path"
    strict = solar_conn(params={"conn": {"no_path": "fail"}})
    e2 = edge(strict, st, pn)
    assert e2.verdict == cg.NO_FIT and check(e2, "conn").code == "no-path"


def test_two_adapters_in_a_row_are_never_used():
    g = solar_conn()
    st, pn = station(pv_voc_max=60, pv_conn="DC8020"), panel(voc=21.5, conn="Anderson")
    chain = [adapter(8, "Anderson", "MC4"), adapter(9, "MC4", "DC8020")]
    e = edge(g, st, pn, adapters=chain)
    assert e.verdict == cg.UNKNOWN and e.via_model_id is None


@pytest.mark.parametrize("src", [
    {"source_url": "https://lab.example/test", "source_kind": "measured-test", "source_id": 3},
    {"source_url": "https://www.amazon.com/dp/B0ABCDEFGH", "source_kind": "manufacturer-page"},
    {},
])
def test_adapters_without_manufacturer_fit_data_are_ignored(src):
    g = solar_conn()
    st, pn = station(pv_voc_max=60, pv_conn="DC8020"), panel(voc=21.5, conn="MC4")
    e = edge(g, st, pn, adapters=[adapter(9, "MC4", "DC8020", source=src)])
    assert e.verdict == cg.UNKNOWN and e.via_model_id is None


def test_max_adapters_zero_requires_a_direct_match():
    g = solar_conn(params={"conn": {"max_adapters": 0}})
    st, pn = station(pv_voc_max=60, pv_conn="DC8020"), panel(voc=21.5, conn="MC4")
    assert edge(g, st, pn, adapters=[adapter()]).verdict == cg.UNKNOWN


def test_adapter_ratings_limit_the_configurations():
    g = make_graph(("cold", "cur", "conn"), arrays={"max_series": 3, "max_parallel": 2, "max_units": 4})
    st = station(pv_voc_max=150, pv_a_max=20, limited=True, pv_conn="DC8020")
    e = edge(g, st, panel(voc=21.5, isc=12.4, conn="MC4"),
             adapters=[adapter(9, "MC4", "DC8020", max_a=15, max_v=60)])
    by = {(o.series, o.parallel): o for o in e.options}
    assert by[(3, 1)].outcome == cg.OVER_LIMIT                 # 80.6 V > 60 V adapter
    assert "rating-voltage-exceeded" in by[(3, 1)].to_dict()["codes"]
    assert by[(2, 1)].outcome == cg.FITS
    # A branch connector would be a second extra part next to the adapter.
    assert {o.parallel for o in e.options} == {1}
    assert e.config["wiring"] == {"series": True, "parallel": False, "parallel_blocked_by": "adapter"}
    weak = edge(g, st, panel(voc=21.5, isc=16.0, conn="MC4"),
                adapters=[adapter(9, "MC4", "DC8020", max_a=15, max_v=60)])
    assert weak.verdict == cg.OVER_LIMIT                        # 16 A through a 15 A adapter
    assert "rating-current-exceeded" in {c.code for c in weak.checks}


def test_connector_ratings_limit_parallel_strings_on_a_direct_path():
    g = make_graph(("cold", "cur", "conn"), arrays={"max_series": 1, "max_parallel": 3, "max_units": 3})
    e = edge(g, station(pv_voc_max=60, pv_a_max=100, pv_conn="MC4"), panel(voc=21.5, isc=12.4, conn="MC4"))
    by = {(o.series, o.parallel): o.outcome for o in e.options}
    assert by == {(1, 1): cg.FITS, (1, 2): cg.FITS, (1, 3): cg.OVER_LIMIT}   # 37.2 A > 30 A MC4
    assert e.config["max_parallel"] == 2


@pytest.mark.parametrize("limited", [True, False])
def test_a_known_rating_with_an_unknown_current_is_unknown_never_a_pass(limited):
    # Without the panel's Isc the 15 A adapter cannot be checked: unknown,
    # even when the station documents current limiting (which used to pass).
    g = make_graph(("cold", "cur", "conn"), arrays={"max_series": 2, "max_parallel": 2, "max_units": 4})
    st = station(pv_voc_max=60, pv_a_max=25, limited=limited, pv_conn="DC8020")
    ad = adapter(9, "MC4", "DC8020", max_a=15, max_v=60)
    blind = edge(g, st, panel(voc=21.5, conn="MC4"), adapters=[ad])
    assert blind.verdict == cg.UNKNOWN and not blind.store
    assert all(o.outcome == cg.UNKNOWN for o in blind.options)
    if limited:
        assert "rating-current-unknown" in {c.code for c in blind.checks}
    known = edge(g, st, panel(voc=21.5, isc=11.6, conn="MC4"), adapters=[ad])
    assert known.verdict == cg.FITS_WITH_ADAPTER and known.config["series"] in (1, 2)


def test_a_known_voltage_rating_without_a_cold_voltage_is_unknown():
    g = make_graph(("cold", "conn"))
    e = edge(g, station(pv_voc_max=60, pv_conn="MC4"), panel(voc=21.5, cell="CdTe thin film", conn="MC4"))
    assert e.verdict == cg.UNKNOWN
    assert {c.code for c in e.checks} >= {"tc-unknown-non-crystalline", "rating-voltage-unknown"}
    assert "cold-not-checked" in codes(e) and "design-temp-default" not in codes(e)


def test_rating_checks_can_be_labelled_not_checked_by_config():
    g = make_graph(("cold", "cur", "conn"), params={"conn": {"on_missing": "skip"}})
    e = edge(g, station(pv_voc_max=60, limited=True, pv_conn="DC8020"), panel(voc=21.5, conn="MC4"),
             adapters=[adapter(9, "MC4", "DC8020", max_a=15, max_v=60)])
    assert e.verdict == cg.FITS_WITH_ADAPTER
    assert any(a.code == "rating-not-checked" and "15 A" in a.text for a in e.assumptions)


def test_parallel_strings_need_a_branch_connector_within_the_adapter_budget():
    arrays = {"max_series": 2, "max_parallel": 2, "max_units": 4}
    st = station(pv_voc_max=150, pv_conn="MC4, DC8020")
    direct = edge(make_graph(("cold", "conn"), arrays=arrays), st, panel(voc=21.5, conn="MC4"))
    assert {o.parallel for o in direct.options} == {1, 2}       # the branch connector is the one part
    none_left = edge(make_graph(("cold", "conn"), arrays=arrays, params={"conn": {"max_adapters": 0}}),
                     st, panel(voc=21.5, conn="MC4"))
    assert {o.parallel for o in none_left.options} == {1}
    assert none_left.config["wiring"]["parallel_blocked_by"] == "max_adapters"


def test_the_endorsed_adapter_wins_over_another_bridge():
    g = solar_conn(params={"conn": {"endorse_relation": "adapter_for"}})
    st, pn = station(1, pv_voc_max=60, pv_conn="DC8020"), panel(2, voc=21.5, conn="MC4")
    ads = [adapter(7, "MC4", "DC8020", max_a=30, max_v=100), adapter(8, "MC4", "DC8020", max_a=10, max_v=60)]
    rec = cg.FitRecord(a=1, b=8, relation="adapter_for", source_url="https://maker.example/a8",
                       source_kind="manufacturer-page")
    assert edge(g, st, pn, adapters=ads).via_model_id == 7          # best rating
    e = edge(g, st, pn, adapters=ads, records=[rec])
    assert e.via_model_id == 8 and "adapter-endorsed" in e.flags


def test_connector_wiring_limits_series_and_parallel():
    g = make_graph(("cold", "conn"), arrays={"max_series": 3, "max_parallel": 3, "max_units": 4})
    st = station(pv_voc_max=150, pv_conn="DC8020, MC4")
    barrel = edge(g, st, panel(voc=21.5, conn="DC8020"))
    assert {o.series for o in barrel.options} == {1}           # a DC8020 output cannot be seriesed
    assert barrel.config["wiring"] == {"series": False, "parallel": True}
    mc4 = edge(g, st, panel(voc=21.5, conn="MC4"))
    assert {(o.series, o.parallel) for o in mc4.options} == {
        (1, 1), (1, 2), (1, 3), (2, 1), (2, 2), (3, 1)}


def test_missing_or_unresolvable_connectors_are_unknown():
    g = solar_conn()
    e = edge(g, station(pv_voc_max=60), panel(voc=21.5, conn="MC4"))
    assert e.verdict == cg.UNKNOWN and check(e, "conn").code == "missing:pv_conn"
    e2 = edge(g, station(pv_voc_max=60, pv_conn="USB-C"), panel(voc=21.5, conn="MC4"))
    assert check(e2, "conn").code == "unresolved-connector"


def test_a_missing_input_connector_keeps_the_panels_own_wiring():
    # B's own connector decides the wirings even when A's is not published:
    # barrel-plug panels are never enumerated in series.
    g = make_graph(("cold", "conn"), arrays={"max_series": 3, "max_parallel": 3, "max_units": 4})
    e = edge(g, station(pv_voc_max=150), panel(voc=21.5, conn="DC8020"))
    assert e.verdict == cg.UNKNOWN and check(e, "conn").code == "missing:pv_conn"
    assert {o.series for o in e.options} == {1}
    assert e.config["wiring"] == {"series": False, "parallel": True}
    assert e.config["connector_path"]["b"] == ["dc8020"]


def test_the_panels_own_rating_still_applies_when_the_path_is_unknown():
    g = make_graph(("cold", "cur", "conn"), arrays={"max_series": 1, "max_parallel": 3, "max_units": 3})
    e = edge(g, station(pv_voc_max=60, pv_a_max=100), panel(voc=21.5, isc=12.4, conn="MC4"))
    by = {(o.series, o.parallel): o for o in e.options}
    assert by[(1, 1)].outcome == by[(1, 2)].outcome == cg.UNKNOWN          # path unknown
    assert by[(1, 3)].outcome == cg.OVER_LIMIT                              # 37.2 A > 30 A MC4
    assert "rating-current-exceeded" in by[(1, 3)].codes
    assert e.verdict == cg.UNKNOWN and not e.store


@pytest.mark.parametrize("st_vals,pn_vals,text", [
    (dict(pv_voc_max=60, pv_v_min=12, pv_conn="MC4"), dict(voc=21.5, vmp=18, isc=5, conn="MC4"),
     "Input current not checked: the input's maximum current is not published."),
    (dict(pv_voc_max=60, pv_v_min=12, pv_a_max=10, pv_conn="MC4"), dict(voc=21.5, isc=5, conn="MC4"),
     "Minimum input voltage not checked: the maximum-power voltage is not published."),
    (dict(pv_voc_max=60, pv_v_min="twelve", pv_a_max=10, pv_conn="MC4"), dict(voc=21.5, vmp=18, isc=5, conn="MC4"),
     "Minimum input voltage not checked: the input's minimum voltage is not a number."),
    (dict(pv_voc_max=60, pv_v_min=12, pv_a_max=10, pv_conn="USB-C"), dict(voc=21.5, vmp=18, isc=5, conn="MC4"),
     "Connector not checked: a connector is named in a way we cannot match to one connector."),
    (dict(pv_voc_max=60, pv_v_min=12, pv_a_max=10, pv_conn="DC8020"), dict(voc=21.5, vmp=18, isc=5, conn="MC4"),
     "Connector not checked: the connectors do not mate and no documented adapter joins them."),
    (dict(pv_voc_max=60, pv_v_min=12, pv_a_max=10, pv_conn="MC4"), dict(voc=21.5, vmp=18, isc=5),
     "Connector not checked: the attached product's connector is not published."),
])
def test_a_check_that_could_not_run_is_named_with_the_missing_value(st_vals, pn_vals, text):
    g = make_graph(("cold", "minv", "cur", "conn"))
    e = edge(g, station(**st_vals), panel(**pn_vals))
    assert e.verdict == cg.UNKNOWN
    assert [a.text for a in e.assumptions if a.code == "not-checked"] == [text]
    assert not any("missing:" in a.text for a in e.assumptions)          # never a raw problem code


def test_a_skipped_check_is_labelled_with_a_readable_reason_too():
    g = make_graph(("cold", "cur"), params={"cur": {"on_missing": "skip"}})
    e = edge(g, station(pv_voc_max=60), panel(voc=21.5, isc=12.4))
    assert e.verdict == cg.FITS
    assert [a.text for a in e.assumptions if a.code == "not-checked"] == [
        "Input current not checked: the input's maximum current is not published."]


TAX = {"mc4": {"aliases": [r"\bmc-?4\b"], "fallback_aliases": [r"^(?:pv|solar)\s+connectors?$"]},
       "xt60": {"aliases": [r"\bxt-?60\b"]}, "dc7909": {"aliases": [r"\bdc ?7909\b"]},
       "l14-30": {"aliases": [r"\bl14-?30"]}, "tt-30": {"aliases": [r"\btt-?30"]}}


@pytest.mark.parametrize("text,strict,fallback", [
    ("MC4", ("mc4",), ("mc4",)),
    ("PV connector", (), ("mc4",)),                         # generic wording: fallback only
    ("Solar connector: DC7909", ("dc7909",), ("dc7909",)),  # the specific name wins
    ("XT60 solar connector", ("xt60",), ("xt60",)),
    ("DC7909 (requires MC4 adapter)", (), ()),              # names a part it does not have
    ("MC4 to XT60 cable", (), ()),                          # two connectors in one piece
    ("MC4, XT60", ("mc4", "xt60"), ("mc4", "xt60")),        # a list names both
    ("Anderson", (), ()),
])
def test_connector_text_must_name_one_connector_specifically(text, strict, fallback):
    tax = cg.ConnectorTaxonomy(TAX)
    assert tax.resolve(text)[0] == strict
    assert tax.resolve(text, fallback=True)[0] == fallback


def test_policy_scans_every_connector_mention_even_in_ambiguous_text():
    tax = cg.ConnectorTaxonomy(TAX)
    assert tax.resolve("L14-30P to TT-30R")[0] == ()
    assert tax.mentions("L14-30P to TT-30R") == {"l14-30", "tt-30"}
    g = make_graph(("mfr",), ruleset={"a_kinds": ["station"], "b_kinds": ["cable"], "relation": "adapter_for"},
                   cfg={"policy": {"exclude_connectors": ["l14-30"]}})
    assert g.excluded_reason(adapter(22, "L14-30P to TT-30R", "TT-30R", kind="cable")) == \
        "excluded-connector:l14-30"


# U+2010 hyphen, U+2011 non-breaking hyphen (NFKC: U+2010), U+2012 figure dash,
# U+2013 en dash, U+2014 em dash, U+2015 horizontal bar.
UNICODE_DASHES = ["\u2010", "\u2011", "\u2012", "\u2013", "\u2014", "\u2015"]


@pytest.mark.parametrize("dash", UNICODE_DASHES)
def test_connector_text_reads_unicode_hyphens_and_dashes_as_a_hyphen(dash):
    tax = cg.ConnectorTaxonomy(TAX)
    assert tax.resolve(f"TT{dash}30R") == (("tt-30",), ())
    assert tax.mentions(f"L14{dash}30P to TT-30R") == {"l14-30", "tt-30"}
    # inside longer text the alias, not the exact-id match, has to read it
    assert tax.resolve(f"XT{dash}60 port, DC7909") == (("xt60", "dc7909"), ())


@pytest.mark.parametrize("dash", UNICODE_DASHES)
def test_an_excluded_connector_spelled_with_a_unicode_dash_is_still_excluded(dash):
    g = make_graph(("mfr",), ruleset={"a_kinds": ["station"], "b_kinds": ["cable"],
                                      "relation": "adapter_for"},
                   cfg={"policy": {"exclude_connectors": ["l14-30"]}})
    cord = adapter(22, f"NEMA L14{dash}30P", "TT-30R", kind="cable")
    assert g.excluded_reason(cord) == "excluded-connector:l14-30"
    rec = cg.FitRecord(a=1, b=22, relation="adapter_for", source_url="https://maker.example/c",
                       source_kind="manufacturer-page")
    out = g.evaluate_pair(station(), cord, ruleset="solar", records=[rec])
    assert isinstance(out, cg.Skip) and out.reason == "excluded-connector:l14-30:22"


@pytest.mark.parametrize("dash", UNICODE_DASHES)
def test_policy_matches_rules_read_unicode_dashes_as_a_hyphen(dash):
    g = make_graph(("mfr",), ruleset={"a_kinds": ["station"], "b_kinds": ["cable"],
                                      "relation": "adapter_for"},
                   cfg={"policy": {"exclude_models": [
                       {"field": "connector_a", "op": "matches", "value": r"\btwist-lock\b",
                        "kinds": ["cable"]}]}})
    assert g.excluded_reason(adapter(22, f"30A twist{dash}lock", "TT-30R", kind="cable")) == \
        r"excluded-model:connector_a:matches:\btwist-lock\b"
    assert g.excluded_reason(adapter(23, "30A twist lock", "TT-30R", kind="cable")) == ""


def test_fallback_aliases_apply_only_on_the_configured_side():
    g = solar_conn(params={"conn": {"fallback_alias_sides": ["b"]}},
                   cfg={"connectors": {"mc4": {"fallback_aliases": [r"^pv connectors?$"]}}})
    assert g.rules["conn"].params["fallback_alias_sides"] == ("b",)
    panel_side = edge(g, station(pv_voc_max=60, pv_conn="MC4"), panel(voc=21.5, conn="PV connector"))
    assert panel_side.verdict == cg.FITS
    station_side = edge(g, station(pv_voc_max=60, pv_conn="PV connector"), panel(voc=21.5, conn="MC4"))
    assert station_side.verdict == cg.UNKNOWN and check(station_side, "conn").code == "unresolved-connector"
    assert edge(solar_conn(cfg={"connectors": {"mc4": {"fallback_aliases": [r"^pv connectors?$"]}}}),
                station(pv_voc_max=60, pv_conn="MC4"), panel(voc=21.5, conn="PV connector")).verdict == cg.UNKNOWN
    bad = solar_conn(params={"conn": {"fallback_alias_sides": ["c"]}})
    assert "solar" in bad.rulesets and not bad.rulesets["solar"].enabled


def test_a_station_port_that_requires_an_adapter_is_not_a_direct_match():
    g = solar_conn()
    e = edge(g, station(pv_voc_max=60, pv_conn="DC8020 (requires MC4 adapter)"), panel(voc=21.5, conn="MC4"))
    assert e.verdict == cg.UNKNOWN and check(e, "conn").code == "unresolved-connector"


def test_excluded_connectors_never_form_a_path_and_the_pair_is_skipped():
    g = solar_conn(cfg={"policy": {"exclude_connectors": ["l14-30"]}})
    out = g.evaluate_pair(station(pv_voc_max=60, pv_conn="L14-30R"), panel(voc=21.5, conn="L14-30P"),
                          ruleset="solar")
    assert isinstance(out, cg.Skip) and out.reason == "excluded-path"
    st, pn = station(pv_voc_max=60, pv_conn="DC8020"), panel(voc=21.5, conn="MC4")
    fine = g.evaluate_pair(st, pn, ruleset="solar", adapters=[
        adapter(9, "MC4", "DC8020", kind="cable"), adapter(10, "L14-30", "DC8020", kind="cable")])
    # The MC4->DC8020 cable is fine; the L14-30 one is excluded but irrelevant here.
    assert isinstance(fine, cg.Edge) and fine.via_model_id == 9


def test_a_pair_that_connects_only_through_an_excluded_adapter_is_skipped():
    g = solar_conn(cfg={"policy": {"exclude_models": [
        {"field": "ac_voltage", "op": "gte", "value": 200, "kinds": ["cable"]}]}})
    st, pn = station(pv_voc_max=60, pv_conn="DC8020"), panel(voc=21.5, conn="MC4")
    hot = adapter(9, "MC4", "DC8020", kind="cable", ac_voltage=240)
    out = g.evaluate_pair(st, pn, ruleset="solar", adapters=[hot])
    assert isinstance(out, cg.Skip) and out.reason == "excluded-path"
    assert g.excluded_reason(hot) == "excluded-model:ac_voltage:gte:200"


def test_a_model_carrying_an_excluded_connector_is_skipped_everywhere():
    g = make_graph(("mfr",), ruleset={"a_kinds": ["station"], "b_kinds": ["cable"],
                                      "relation": "adapter_for"},
                   cfg={"policy": {"exclude_connectors": ["l14-30"]}})
    cord = adapter(22, "L14-30P", "L14-30R", kind="cable")
    rec = cg.FitRecord(a=1, b=22, relation="adapter_for", source_url="https://maker.example/c",
                       source_kind="manufacturer-page")
    out = g.evaluate_pair(station(), cord, ruleset="solar", records=[rec])
    assert isinstance(out, cg.Skip) and out.reason == "excluded-connector:l14-30:22"


# ---------------------------------------------------------------------------
# verdict labels
# ---------------------------------------------------------------------------

def test_a_manufacturer_fit_on_a_computed_adapter_path_is_labelled_fits_with_adapter():
    g = make_graph(("mfr", "cold", "conn"))
    st, pn = station(1, pv_voc_max=60, pv_conn="DC8020"), panel(2, voc=21.5, conn="MC4")
    rec = cg.FitRecord(a=1, b=2, relation="charges_from", source_url="https://maker.example/m",
                       source_kind="manual-pdf")
    e = edge(g, st, pn, records=[rec], adapters=[adapter()])
    assert (e.verdict, e.via_model_id, e.decided_by) == (cg.FITS_WITH_ADAPTER, 9, cg.DECIDED_MANUFACTURER)
    no = edge(g, st, pn, records=[cg.FitRecord(a=1, b=2, relation="charges_from", verdict="no_fit",
                                               source_url="https://maker.example/m",
                                               source_kind="manual-pdf")], adapters=[adapter()])
    assert (no.verdict, no.via_model_id) == (cg.NO_FIT, None)        # no cable on a negative verdict
    named = edge(g, st, pn, adapters=[adapter()], records=[cg.FitRecord(
        a=1, b=2, relation="charges_from", verdict="no_fit", via=44, source_url="https://maker.example/m",
        source_kind="manual-pdf")])
    assert named.via_model_id == 44                                   # unless the record names it


def test_a_stated_series_string_the_connector_cannot_wire_is_still_checked():
    # XT60 panels are not enumerated in series, but a record stating two in
    # series is evaluated on demand: 53.75 V at -40 °C over a 50 V input.
    g = make_graph(("mfr", "cold", "conn"), arrays={"max_series": 3, "max_parallel": 1, "max_units": 3})
    rec = cg.FitRecord(a=1, b=2, relation="charges_from", config={"series": 2},
                       source_url="https://maker.example/m", source_kind="manual-pdf")
    e = edge(g, station(pv_voc_max=50, pv_conn="XT60"), panel(voc=21.5, conn="XT60"), records=[rec])
    assert e.verdict == cg.FITS and (e.config["series"], e.config["parallel"]) == (1, 1)
    assert {"computed-check-disagrees", "record-config-limited"} <= set(e.flags)
    stated = next(o for o in e.options if (o.series, o.parallel) == (2, 1))
    assert stated.outcome == cg.OVER_LIMIT and stated.cold_voc_v == pytest.approx(53.75)
    text = next(a.text for a in e.assumptions if a.code == "record-config-limited")
    assert "2 in series" in text and "53.75 V" in text and "1 unit is shown" in text


def test_adapter_label_wins_over_series_and_config_keeps_the_count():
    g = make_graph(("cold", "minv", "conn"),
                   arrays={"max_series": 3, "max_parallel": 1, "max_units": 3})
    e = edge(g, station(pv_voc_max=95, pv_v_min=32, pv_conn="DC8020"),
             panel(voc=21.5, vmp=18.4, conn="MC4"), adapters=[adapter()])
    assert e.verdict == cg.FITS_WITH_ADAPTER and e.config["min_series"] == 2


def test_parallel_strings_never_rescue_a_failing_single_unit():
    # Parallel strings only add current, so computed rules never label a pair
    # fits_in_parallel (only a manufacturer record can).
    g = make_graph(("cold", "minv", "cur"),
                   arrays={"max_series": 1, "max_parallel": 4, "max_units": 4})
    low = edge(g, station(pv_voc_max=60, pv_v_min=20, pv_a_max=40), panel(voc=21.5, vmp=18, isc=4))
    assert low.verdict == cg.NO_FIT
    assert all(o.outcome == cg.NO_FIT for o in low.options)
    ok = edge(g, station(pv_voc_max=60, pv_v_min=12, pv_a_max=10), panel(voc=21.5, vmp=18, isc=4))
    assert ok.verdict == cg.FITS and ok.config["max_parallel"] == 2


# ---------------------------------------------------------------------------
# climate
# ---------------------------------------------------------------------------

def test_climate_per_zip_then_zip3_then_default_and_callers_can_only_go_colder():
    g = make_graph(cfg={"climate": {"zip_min_c": {"33101": 7.0, "997": -45.0, "100": -15.0}}})
    assert g.climate().design_temp_c == -40.0
    z = g.climate(zip_code="33101")
    assert (z.design_temp_c, z.basis, z.zip) == (7.0, "zip", "33101")
    assert g.climate(zip_code="99701-1234").design_temp_c == -45.0       # ZIP3 fallback
    assert g.climate(zip_code="10001").design_temp_c == -15.0
    assert g.climate(zip_code="60601").design_temp_c == -40.0            # not listed
    assert g.climate(zip_code="33101", design_temp_c=-20).design_temp_c == -20
    assert g.climate(zip_code="33101", design_temp_c=20).design_temp_c == 7.0
    nofb = make_graph(cfg={"climate": {"zip_min_c": {"997": -45.0}, "zip3_fallback": False}})
    assert nofb.climate(zip_code="99701").design_temp_c == -40.0


def test_a_warm_zip_relaxes_the_factor_only_through_the_table():
    g = make_graph(cfg={"climate": {"zip_min_c": {"33101": 7.0}}})
    st, pn = station(pv_voc_max=50), panel(voc=21.5)
    two = make_graph(arrays={"max_series": 2, "max_parallel": 1, "max_units": 2},
                     cfg={"climate": {"zip_min_c": {"33101": 7.0}}})
    default = edge(two, st, pn)
    miami = edge(two, st, pn, climate=two.climate(zip_code="33101"))
    # 2 x 21.5 x 1.25 = 53.75 V > 47.5 V at -40 °C; 2 x 21.5 x 1.08 = 46.44 V at 7 °C.
    assert default.config["max_series"] == 1 and miami.config["max_series"] == 2
    assert "design-temp-zip" in codes(miami)
    assert edge(g, st, pn).config["design_temp_c"] == -40.0


def test_invalid_zip_entries_are_ignored_and_reported():
    g = make_graph(cfg={"climate": {"zip_min_c": {"ABCDE": -10, "1234": -10, "02134": 99,
                                                   "02135": "cold"}}})
    assert g.climate(zip_code="02134").design_temp_c == -40.0
    assert sum("zip_min_c" in p for p in g.problems) == 4


# ---------------------------------------------------------------------------
# safety floors and config validation
# ---------------------------------------------------------------------------

def test_default_design_temperature_cannot_be_warmer_than_minus_40():
    g = make_graph(cfg={"climate": {"default_min_c": -20}})
    assert g.default_climate.design_temp_c == -40.0
    assert any("default_min_c" in p for p in g.problems)
    colder = make_graph(cfg={"climate": {"default_min_c": -45}})
    assert colder.default_climate.design_temp_c == -45.0


@pytest.mark.parametrize("headroom", [0.0, 0.01, 0.049])
def test_headroom_below_five_percent_is_clamped(headroom):
    g = make_graph(params={"cold": {"headroom": headroom}})
    assert g.rules["cold"].params["headroom"] == 0.05
    assert any("headroom" in p for p in g.problems)
    assert edge(g, station(pv_voc_max=60), panel(voc=48.0)).verdict == cg.OVER_LIMIT


def test_more_headroom_is_allowed():
    g = make_graph(params={"cold": {"headroom": 0.10}})
    e = edge(g, station(pv_voc_max=60), panel(voc=45.6))      # 57 V > 54 V allowed
    assert e.verdict == cg.OVER_LIMIT and not g.problems


def test_the_cold_check_can_never_be_skipped():
    g = make_graph(params={"cold": {"on_missing": "skip"}})
    assert g.rules["cold"].params["on_missing"] == "unknown"
    assert edge(g, station(), panel(voc=21.5)).verdict == cg.UNKNOWN


def test_more_than_one_adapter_is_clamped_and_reported():
    g = solar_conn(params={"conn": {"max_adapters": 3}})
    assert g.rules["conn"].params["max_adapters"] == 1
    assert any("max_adapters" in p for p in g.problems)


def test_isc_factor_below_one_is_clamped():
    g = make_graph(("cold", "cur"), params={"cur": {"isc_factor": 0.8}})
    assert g.rules["cur"].params["isc_factor"] == 1.0


@pytest.mark.parametrize("rule_over,needle", [
    ({"cold": {"kind": "max_voltage_hot"}}, "unknown kind"),
    ({"cold": {"version": None}}, "missing version"),
    ({"cold": {"a_fields": {"max_v": None}}}, "a_fields.max_v is required"),
    ({"cold": {"b_fields": {"isc": "isc"}}}, "not a role"),
    ({"cold": {"params": {"tc_plausible_pct_per_c": [-0.1, -0.5]}}}, "tc_plausible"),
    ({"cold": {"params": {"non_crystalline_patterns": ["("]}}}, "bad regex"),
    ({"cold": {"params": {"headroom": 0.6}}}, "below 0.5"),
])
def test_invalid_rules_disable_the_ruleset_with_a_reason(rule_over, needle):
    g = make_graph(cfg={"rules": rule_over})
    rs = g.rulesets["solar"]
    assert not rs.enabled
    assert any(needle in p for p in rs.problems), rs.problems
    out = g.evaluate_pair(station(pv_voc_max=60), panel(voc=21.5), ruleset="solar")
    assert isinstance(out, cg.Skip) and out.reason == "ruleset-disabled"


@pytest.mark.parametrize("rs_over,needle", [
    ({"rules": ["nope"]}, "unknown rule"),
    ({"version": None}, "missing version"),
    ({"relation": ""}, "missing relation"),
    ({"a_kinds": []}, "a_kinds and b_kinds"),
    ({"arrays": {"max_series": 0, "max_parallel": 1, "max_units": 1}}, "arrays.max_series"),
    ({"arrays": {"max_series": 13, "max_parallel": 1, "max_units": 1}}, "arrays.max_series"),
    ({"store_verdicts": ["fits", "maybe"]}, "store_verdicts"),
    ({"pairs": "some"}, "pairs must be"),
    ({"rules": ["cold", "cold"]}, "more than one max_voltage_cold"),
])
def test_invalid_rulesets_are_disabled(rs_over, needle):
    g = make_graph(ruleset=rs_over)
    assert not g.rulesets["solar"].enabled
    assert any(needle in p for p in g.rulesets["solar"].problems), g.rulesets["solar"].problems
    res = g.build([station(pv_voc_max=60), panel(voc=21.5)])
    assert res.disabled == ("solar",) and res.edges == []


def test_other_config_problems_are_reported():
    g = make_graph(("cold", "conn"), cfg={
        "connectors": {"mc4": {"aliases": ["("], "mates_with": ["nope"]}},
        "policy": {"exclude_connectors": ["zz"], "exclude_models": [{"field": "x", "op": "about"},
                                                                    {"field": "y", "op": "matches", "value": "("}]},
        "rules": {"conn": {"params": {"no_path": "maybe"}}},
    })
    joined = " | ".join(g.problems)
    for needle in ("bad alias regex", "mates_with unknown connector", "exclude_connectors",
                   "exclude_models[0] malformed", "exclude_models[1] bad regex", "no_path"):
        assert needle in joined, needle


def test_unknown_ruleset_name_raises():
    with pytest.raises(cg.CompatGraphError):
        make_graph().evaluate_pair(station(), panel(), ruleset="nope")
