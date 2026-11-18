"""framework.core.compat_graph: edges, precedence, versioning and the writer.

Manual > manufacturer > computed precedence, disagreement flags and
policies, fit-record validation (never Amazon, manufacturer sources only),
record configurations, listed-only rulesets, exclusion policy, the edge row
shape and inputs hash, :meth:`CompatGraph.plan` (version bumps recompute,
manual rows are protected, unknown pairs are deleted), the EdgeWriter's SQL
against a fake DB-API connection, and model-keyed consumable fitment
(ModelIndex / split_model_list / fit_records_from_list). No I/O."""
from __future__ import annotations

import json

import pytest

from framework.core import compat_graph as cg

CLOCK = "2026-11-17T00:00:00Z"
MFR = {"source_url": "https://maker.example/compat", "source_kind": "manufacturer-page",
       "publisher": "maker.example", "source_id": 40}


def config(*, cold_version=1, solar_version=1, on_disagreement=None, headroom=None, extra=None):
    cold = {"kind": "max_voltage_cold", "version": cold_version, "a_fields": {"max_v": "max_v"},
            "b_fields": {"voc": "voc", "tc": "tc"}}
    if headroom is not None:
        cold["params"] = {"headroom": headroom}
    mfr = {"kind": "manufacturer_list", "version": 1}
    if on_disagreement:
        mfr["params"] = {"on_disagreement": on_disagreement}
    layer = {
        "rules": {"mfr": mfr, "cold": cold,
                  "minv": {"kind": "min_voltage", "version": 1, "a_fields": {"min_v": "min_v"},
                           "b_fields": {"vmp": "vmp"}}},
        "rulesets": {
            "solar": {"version": solar_version, "relation": "charges_from", "a_kinds": ["station"],
                      "b_kinds": ["panel"], "rules": ["mfr", "cold", "minv"],
                      "arrays": {"max_series": 3, "max_parallel": 2, "max_units": 4}},
            "expansion": {"version": 1, "relation": "expands", "a_kinds": ["station"],
                          "b_kinds": ["battery"], "rules": ["mfr"], "store_verdicts": ["fits", "no_fit"]},
        },
    }
    return cg.resolve_config(file_cfg={}, site_cfg=cg.merge_config(layer, extra or {}))


def graph(**kw):
    return cg.CompatGraph(config(**kw), clock=lambda: CLOCK)


def st(mid=1, **v):
    return cg.ModelSpec.from_values(mid, "station", v, name=f"Station {mid}", brand="Maker",
                                    source={"source_url": f"https://maker.example/s{mid}",
                                            "source_kind": "manufacturer-page", "source_id": 100 + mid})


def pn(mid=2, **v):
    return cg.ModelSpec.from_values(mid, "panel", v, name=f"Panel {mid}", brand="Maker",
                                    source={"source_url": f"https://maker.example/p{mid}",
                                            "source_kind": "manual-pdf", "source_id": 200 + mid})


def bat(mid=3):
    return cg.ModelSpec.from_values(mid, "battery", {}, name=f"Battery {mid}")


def rec(a, b, relation="charges_from", verdict="fits", **kw):
    base = dict(MFR)
    base.update(kw)
    return cg.FitRecord(a=a, b=b, relation=relation, verdict=verdict, **base)


def one(g, a, b, records=(), ruleset="solar", **kw):
    out = g.evaluate_pair(a, b, ruleset=ruleset, records=records, **kw)
    assert isinstance(out, cg.Edge), out
    return out


def codes(e):
    return {a.code for a in e.assumptions}


OVERVOLT = dict(st=dict(max_v=50, min_v=12), pn=dict(voc=49.5, vmp=41, tc=-0.27))   # computed over_limit
GOOD = dict(st=dict(max_v=60, min_v=12), pn=dict(voc=21.5, vmp=18.4))               # computed fits


# ---------------------------------------------------------------------------
# precedence
# ---------------------------------------------------------------------------

def test_computed_verdict_without_records():
    e = one(graph(), st(**GOOD["st"]), pn(**GOOD["pn"]))
    assert (e.verdict, e.confidence, e.decided_by) == (cg.FITS, cg.CONF_COMPUTED, cg.DECIDED_COMPUTED)


def test_a_manufacturer_fit_that_fails_the_cold_check_is_over_limit_flagged_and_shown():
    # Binding amendment 6: 'fits' needs >= 5 % headroom at -40 °C, with no
    # manufacturer exception. 58.19 V on a 50 V input is over the limit.
    e = one(graph(), st(**OVERVOLT["st"]), pn(**OVERVOLT["pn"]), records=[rec(1, 2, statement="Works with Station 1")])
    assert (e.verdict, e.confidence, e.decided_by) == (cg.OVER_LIMIT, cg.CONF_COMPUTED, cg.DECIDED_COMPUTED)
    assert e.store and "computed-check-disagrees" in e.flags
    assert {"manufacturer-listed", "computed-disagrees", "safety-veto"} <= codes(e)
    shown = next(a for a in e.assumptions if a.code == "computed-disagrees").text
    assert "exceeds the 50 V maximum" in shown
    assert any(ev.role == "record" and ev.source_id == 40 for ev in e.evidence)
    assert 40 in e.evidence_source_ids
    # Every policy: the statement never turns a cold-voltage failure into a fit.
    for policy in ("prefer_manufacturer", "most_conservative", "unknown"):
        e2 = one(graph(on_disagreement=policy), st(**OVERVOLT["st"]), pn(**OVERVOLT["pn"]), records=[rec(1, 2)])
        assert e2.verdict in (cg.OVER_LIMIT, cg.UNKNOWN) and "computed-check-disagrees" in e2.flags, policy


def test_a_manufacturer_fit_is_preferred_on_a_non_safety_disagreement():
    # One panel is below the input's minimum voltage in every configuration
    # (a computed no_fit), but nothing over-volts: the maker's word stands.
    s, p = st(max_v=200, min_v=100), pn(voc=21.5, vmp=18.4)
    assert one(graph(), s, p).verdict == cg.NO_FIT
    e = one(graph(), s, p, records=[rec(1, 2)])
    assert (e.verdict, e.confidence, e.decided_by) == (cg.FITS, cg.CONF_MANUFACTURER, cg.DECIDED_MANUFACTURER)
    assert "computed-check-disagrees" in e.flags and "computed-check-unknown" not in e.flags
    assert "below the 100 V minimum" in next(a.text for a in e.assumptions if a.code == "computed-disagrees")
    strict = one(graph(on_disagreement="most_conservative"), s, p, records=[rec(1, 2)])
    assert (strict.verdict, strict.decided_by) == (cg.NO_FIT, cg.DECIDED_COMPUTED)


def test_most_conservative_policy_lets_the_computed_failure_win():
    r = rec(1, 2, config={"series": 2}, via=77)
    e = one(graph(on_disagreement="most_conservative"), st(**OVERVOLT["st"]), pn(**OVERVOLT["pn"]),
            records=[r])
    assert (e.verdict, e.confidence, e.decided_by) == (cg.OVER_LIMIT, cg.CONF_COMPUTED, cg.DECIDED_COMPUTED)
    assert "computed-check-disagrees" in e.flags
    # The record's wiring and cable do not survive into a computed failure.
    assert (e.config["series"], e.via_model_id) == (1, None)


def test_a_negative_statement_against_a_computed_fit_explains_itself():
    e = one(graph(), st(**GOOD["st"]), pn(**GOOD["pn"]), records=[rec(1, 2, verdict="no_fit")])
    text = next(a.text for a in e.assumptions if a.code == "computed-disagrees")
    assert "limits allow it, but the manufacturer's statement is used" in text


def test_unknown_policy_withholds_a_verdict_on_disagreement():
    e = one(graph(on_disagreement="unknown"), st(**OVERVOLT["st"]), pn(**OVERVOLT["pn"]), records=[rec(1, 2)])
    assert e.verdict == cg.UNKNOWN and not e.store


def test_a_manufacturer_negative_statement_beats_a_computed_fit():
    e = one(graph(), st(**GOOD["st"]), pn(**GOOD["pn"]), records=[rec(1, 2, verdict="no_fit")])
    assert e.verdict == cg.NO_FIT and e.confidence == cg.CONF_MANUFACTURER
    assert "manufacturer-not-compatible" in codes(e) and "computed-check-disagrees" in e.flags


def test_agreeing_statement_has_no_disagreement_flag():
    e = one(graph(), st(**GOOD["st"]), pn(**GOOD["pn"]), records=[rec(1, 2)])
    assert e.verdict == cg.FITS and e.flags == () and "manufacturer-listed" in codes(e)


def test_manual_record_beats_manufacturer_records():
    manual = cg.FitRecord(a=1, b=2, relation="charges_from", verdict="no_fit", origin="manual",
                          statement="recalled cable harness; hold", fetched_at="2026-11-17T09:00:00Z")
    e = one(graph(), st(**GOOD["st"]), pn(**GOOD["pn"]), records=[rec(1, 2), manual])
    assert (e.verdict, e.confidence, e.decided_by) == (cg.NO_FIT, cg.CONF_MANUAL, cg.DECIDED_MANUAL)
    assert "manual" in codes(e) and "recalled cable harness" in " ".join(a.text for a in e.assumptions)


def test_the_latest_manual_record_wins():
    older = cg.FitRecord(a=1, b=2, relation="charges_from", verdict="no_fit", origin="manual",
                         fetched_at="2026-11-01T00:00:00Z")
    newer = cg.FitRecord(a=1, b=2, relation="charges_from", verdict="fits", origin="manual",
                         fetched_at="2026-11-10T00:00:00Z")
    assert one(graph(), st(**GOOD["st"]), pn(**GOOD["pn"]), records=[newer, older]).verdict == cg.FITS


def test_disagreeing_manufacturer_sources_resolve_to_the_most_conservative():
    recs = [rec(1, 2, verdict="fits"), rec(1, 2, verdict="no_fit", source_url="https://maker.example/faq",
                                           source_id=41)]
    e = one(graph(), st(**GOOD["st"]), pn(**GOOD["pn"]), records=recs)
    assert e.verdict == cg.NO_FIT and "records-disagree" in e.flags and "records-disagree" in codes(e)
    assert {40, 41} <= set(e.evidence_source_ids)


@pytest.mark.parametrize("bad,problem", [
    (dict(source_url="https://www.amazon.com/dp/B0ABCDEFGH"), "Amazon"),
    (dict(source_url="", source_id=None), "without a source"),
    (dict(source_kind="measured-test"), "not manufacturer fit data"),
    (dict(verdict="maybe"), "unknown verdict"),
    (dict(origin="forum"), "unknown origin"),
])
def test_invalid_records_are_ignored_and_reported(bad, problem):
    g = graph()
    kw = {"verdict": bad.pop("verdict", "fits")}
    r = cg.FitRecord(a=1, b=2, relation="charges_from", **{**MFR, **bad, **kw})
    res = g.build([st(**OVERVOLT["st"]), pn(**OVERVOLT["pn"])], [r])
    (e,) = res.edges
    assert e.verdict == cg.OVER_LIMIT and e.confidence == cg.CONF_COMPUTED
    assert any(problem in p for p in res.problems), res.problems


def test_records_on_a_ruleset_without_a_manufacturer_rule_are_reported():
    g = graph(extra={"rulesets": {"solar": {"rules": ["cold", "minv"]}}})
    res = g.build([st(**GOOD["st"]), pn(**GOOD["pn"])], [rec(1, 2, verdict="no_fit")])
    assert res.edges[0].verdict == cg.FITS
    assert any("no manufacturer_list rule" in p for p in res.problems)


def test_record_configuration_and_via_are_kept_with_matching_numbers():
    r = rec(1, 2, verdict="fits_in_series", config={"series": 2, "max_units": 2}, via=77)
    e = one(graph(), st(max_v=95, min_v=12), pn(voc=21.5, vmp=18.4), records=[r])
    assert e.verdict == cg.FITS_IN_SERIES and e.via_model_id == 77
    assert (e.config["series"], e.config["parallel"], e.config["max_units"]) == (2, 1, 2)
    assert e.config["cold_voc_v"] == pytest.approx(53.75)
    assert all("2 x" in c.text for c in e.checks if c.rule_id == "cold")


def test_a_record_configuration_outside_the_enumeration_is_evaluated():
    r = rec(1, 2, config={"series": 9})
    e = one(graph(), st(max_v=600, min_v=12), pn(voc=21.5, vmp=18.4), records=[r])
    assert (e.verdict, e.config["series"]) == (cg.FITS, 9) and e.flags == ()
    assert e.config["cold_voc_v"] == pytest.approx(241.875)                # 9 x 21.5 V x 1.25
    assert all("9 x" in c.text for c in e.checks if c.rule_id == "cold")
    assert (9, 1) in {(o.series, o.parallel) for o in e.options}


PECRON = dict(st=dict(max_v=95, min_v=32), pn=dict(voc=21.5, vmp=18.4))     # an 18 V panel, no tc


def test_a_stated_four_in_series_that_over_volts_is_never_stored_as_stated():
    # "four 18V panels in series": 107.5 V at -40 °C on a 95 V input.
    r = rec(1, 2, verdict="fits_in_series", config={"series": 4})
    e = one(graph(), st(**PECRON["st"]), pn(**PECRON["pn"]), records=[r])
    assert (e.verdict, e.decided_by) == (cg.FITS_IN_SERIES, cg.DECIDED_MANUFACTURER)
    assert (e.config["series"], e.config["parallel"]) == (2, 1)            # the best smaller string
    assert e.config["cold_voc_v"] == pytest.approx(53.75) and e.config["max_series"] == 3
    assert {"computed-check-disagrees", "record-config-limited"} <= set(e.flags)
    assert not any(c.outcome == cg.FAIL for c in e.checks)
    text = next(a.text for a in e.assumptions if a.code == "record-config-limited")
    assert "4 in series" in text and "107.5 V" in text and "exceeds the 95 V maximum" in text
    four = next(o for o in e.options if (o.series, o.parallel) == (4, 1))
    assert four.outcome == cg.OVER_LIMIT                                   # evaluated and kept on record
    # most_conservative: the computed result stands (its own fitting string).
    mc = one(graph(on_disagreement="most_conservative"), st(**PECRON["st"]), pn(**PECRON["pn"]), records=[r])
    assert (mc.verdict, mc.decided_by, mc.config["series"]) == (cg.FITS_IN_SERIES, cg.DECIDED_COMPUTED, 2)
    assert "computed-check-disagrees" in mc.flags
    assert one(graph(on_disagreement="unknown"), st(**PECRON["st"]), pn(**PECRON["pn"]),
               records=[r]).verdict == cg.UNKNOWN


def test_a_stated_two_in_series_on_a_50_v_input_falls_back_to_one_unit():
    r = rec(1, 2, config={"series": 2})
    e = one(graph(), st(max_v=50, min_v=12), pn(voc=21.5, vmp=18.4), records=[r])
    assert (e.verdict, e.config["series"], e.config["cold_voc_v"]) == (cg.FITS, 1, pytest.approx(26.875))
    assert {"computed-check-disagrees", "record-config-limited"} <= set(e.flags)


def test_a_stated_max_series_is_cut_to_what_passes_and_the_label_follows_the_string():
    r = rec(1, 2, config={"max_series": 4})
    e = one(graph(), st(**PECRON["st"]), pn(**PECRON["pn"]), records=[r])
    # A plain "fits" whose single unit is below the minimum input voltage.
    assert e.verdict == cg.FITS_IN_SERIES and e.config["series"] == 2
    assert e.config["max_series"] == 3
    assert {"computed-check-disagrees", "record-config-limited"} <= set(e.flags)
    clamp = next(a.text for a in e.assumptions if a.code == "record-limit-clamped")
    assert "up to 4 in series" in clamp and "at most 3 in series" in clamp and "107.5 V" in clamp
    fine = one(graph(), st(**PECRON["st"]), pn(**PECRON["pn"]), records=[rec(1, 2, config={"max_series": 3})])
    assert fine.config["max_series"] == 3 and "record-config-limited" not in fine.flags


def test_a_stated_max_units_is_cut_only_when_no_arrangement_passes():
    r = rec(1, 2, config={"max_units": 4})
    ok = one(graph(), st(**PECRON["st"]), pn(**PECRON["pn"]), records=[r])
    assert ok.config["max_units"] == 4 and "record-config-limited" not in ok.flags   # e.g. 2 x 2
    tight = one(graph(), st(max_v=50, min_v=12), pn(voc=21.5, vmp=18.4),
                records=[rec(1, 2, config={"max_units": 4, "max_parallel": 1})])
    assert tight.config["max_units"] == 1 and "record-config-limited" in tight.flags


def test_a_manual_fit_cannot_override_the_cold_check_and_keeps_owning_its_row():
    manual = cg.FitRecord(a=1, b=2, relation="charges_from", verdict="fits", origin="manual",
                          statement="operator says fine")
    e = one(graph(), st(**OVERVOLT["st"]), pn(**OVERVOLT["pn"]), records=[manual])
    assert (e.verdict, e.confidence, e.decided_by) == (cg.OVER_LIMIT, cg.CONF_MANUAL, cg.DECIDED_COMPUTED)
    assert "computed-check-disagrees" in e.flags and "safety-veto" in codes(e)
    # It replaces an older manual fit row (the SQL lets manual overwrite manual).
    old = dict(e.to_row(), verdict="fits", config={"inputs_hash": "old"})
    plan = graph().plan(graph().build([st(**OVERVOLT["st"]), pn(**OVERVOLT["pn"])], [manual]), [old])
    assert [u.verdict for u in plan.upserts] == [cg.OVER_LIMIT] and plan.protected == []


def test_a_fit_whose_cold_check_cannot_run_says_so_and_is_flagged():
    cfg_extra = {"rules": {"cold": {"b_fields": {"technology": "cell"}}}}
    thin = pn(voc=21.5, vmp=18.4, cell="CIGS thin film")
    e = one(graph(extra=cfg_extra), st(max_v=60, min_v=12), thin, records=[rec(1, 2)])
    assert (e.verdict, e.decided_by) == (cg.FITS, cg.DECIDED_MANUFACTURER)
    assert "computed-check-unknown" in e.flags and "computed-check-disagrees" not in e.flags
    assert "cold-not-checked" in codes(e) and "design-temp-default" not in codes(e)
    shown = next(a.text for a in e.assumptions if a.code == "cold-not-checked")
    assert "crystalline silicon only" in shown
    assert (e.config["series"], e.config["parallel"]) == (1, 1)             # nothing extrapolated
    assert e.to_row()["config"]["flags"] == list(e.flags)


# ---------------------------------------------------------------------------
# a statement is never extrapolated past a check that could not run
# ---------------------------------------------------------------------------

CUR = {"kind": "max_current", "version": 1, "a_fields": {"max_a": "max_a"}, "b_fields": {"isc": "isc"}}
WATTS = {"kind": "watts_clip", "version": 1, "a_fields": {"max_w": "max_w"},
         "b_fields": {"rated_w": "rated_w"}}


def current_graph(*, arrays=(3, 2, 4), on_disagreement=None):
    return graph(on_disagreement=on_disagreement, extra={
        "rules": {"cur": CUR, "watts": WATTS},
        "rulesets": {"solar": {"rules": ["mfr", "cold", "minv", "cur", "watts"],
                               "arrays": {"max_series": arrays[0], "max_parallel": arrays[1],
                                          "max_units": arrays[2]}}}})


# One unit passes the cold check on a 30 V input (21.5 V x 1.195 = 25.69 V of
# 28.5 V allowed), two in series do not; two strings would double the 12 A.
SMALL_INPUT = dict(max_v=30, min_v=12, max_w=400)
PANEL_12A = dict(voc=21.5, vmp=18, isc=12, rated_w=200, tc=-0.3)


@pytest.mark.parametrize("max_a,shown,max_parallel,flags", [
    (None, (1, 1), 1, {"computed-check-incomplete"}),     # not published: one string, said so
    (13, (1, 1), 1, set()),                               # 24 A in two strings would exceed 13 A
    (30, (1, 2), 2, set()),                               # published and roomy: two strings
    (10, (1, 1), None, {"computed-check-disagrees"}),     # fails even alone: never two strings
])
def test_a_fit_statement_never_recommends_more_strings_than_the_published_current_allows(
        max_a, shown, max_parallel, flags):
    s = st(**SMALL_INPUT, **({} if max_a is None else {"max_a": max_a}))
    e = one(current_graph(), s, pn(**PANEL_12A), records=[rec(1, 2)])
    assert (e.verdict, e.decided_by, e.store) == (cg.FITS, cg.DECIDED_MANUFACTURER, True)
    assert (e.config["series"], e.config["parallel"]) == shown
    assert e.config.get("max_parallel") == max_parallel
    assert e.config["design_current_a"] == 12 * shown[1]
    assert set(e.flags) == flags
    not_checked = [a.text for a in e.assumptions if a.code == "not-checked"]
    if max_a is None:
        assert not_checked == ["Input current not checked: the input's maximum current is not published."]
        assert "computed-check-unknown" not in e.flags                   # not a safety check
        assert e.config["max_units"] == 1 and "parallel-connector" not in codes(e)
    else:
        assert not_checked == []


def test_the_record_path_used_to_recommend_unchecked_parallel_strings_on_a_tall_input():
    # Regression for the round-2 review: a 150 V input without a published
    # current got 4 in series AND max_parallel 2 from a plain "fits".
    s = st(max_v=150, min_v=12, max_w=800)
    e = one(current_graph(arrays=(4, 4, 4)), s, pn(**PANEL_12A), records=[rec(1, 2)])
    assert (e.config["series"], e.config["parallel"]) == (4, 1)          # series adds no current
    assert (e.config["max_series"], e.config["max_parallel"], e.config["max_units"]) == (4, 1, 4)
    assert e.config["design_current_a"] == 12
    assert "computed-check-incomplete" in e.flags


def test_an_unknown_short_circuit_current_also_keeps_one_string():
    s = st(**SMALL_INPUT, max_a=40)
    e = one(current_graph(), s, pn(voc=21.5, vmp=18, rated_w=200, tc=-0.3), records=[rec(1, 2)])
    assert (e.config["series"], e.config["parallel"], e.config["max_parallel"]) == (1, 1, 1)
    assert "computed-check-incomplete" in e.flags
    assert "Input current not checked: the short-circuit current is not published." in [
        a.text for a in e.assumptions]


def test_a_stated_parallel_configuration_stands_with_the_current_unchecked_and_says_so():
    e = one(current_graph(), st(**SMALL_INPUT), pn(**PANEL_12A), records=[rec(1, 2, config={"parallel": 2})])
    assert (e.verdict, e.config["series"], e.config["parallel"]) == (cg.FITS, 1, 2)
    assert e.config["max_parallel"] == 2                                 # what the statement names
    assert "computed-check-incomplete" in e.flags and "computed-check-disagrees" not in e.flags
    assert "not-checked" in codes(e) and "parallel-connector" in codes(e)


def test_an_unknown_minimum_voltage_never_lowers_the_stated_series():
    r = rec(1, 2, verdict="fits_in_series", config={"series": 2})
    e = one(graph(), st(max_v=150), pn(voc=21.5, vmp=18.4), records=[r])
    assert (e.verdict, e.config["series"], e.config["parallel"]) == (cg.FITS_IN_SERIES, 2, 1)
    # One panel alone may be below the unknown minimum: limits start at two.
    assert (e.config["min_series"], e.config["max_series"]) == (2, 3)
    assert "max_parallel" not in e.config                               # no single-panel strings
    assert "computed-check-incomplete" in e.flags
    assert "Minimum input voltage not checked: the input's minimum voltage is not published." in [
        a.text for a in e.assumptions]
    known = one(graph(), st(max_v=150, min_v=12), pn(voc=21.5, vmp=18.4), records=[r])
    assert known.config["min_series"] == 1 and "computed-check-incomplete" not in known.flags


@pytest.mark.parametrize("min_v,verdict,series", [
    (12, cg.FITS_IN_SERIES, 2),        # two in series pass every check: shown instead of three
    (50, cg.OVER_LIMIT, None),         # 36.8 V is below a 50 V minimum: nothing the statement covers
    (None, cg.OVER_LIMIT, None),       # unknown minimum: never more than the worst published case
])
def test_a_vetoed_string_never_falls_back_below_an_unknown_minimum_voltage(min_v, verdict, series):
    # "three in series": 80.6 V at -40 °C on a 60 V input.
    r = rec(1, 2, verdict="fits_in_series", config={"series": 3})
    s = st(max_v=60, **({} if min_v is None else {"min_v": min_v}))
    e = one(graph(), s, pn(voc=21.5, vmp=18.4), records=[r])
    assert e.verdict == verdict
    if series is not None:
        assert e.config["series"] == series and "record-config-limited" in e.flags
    else:
        assert "safety-veto" in codes(e)


def test_a_failing_current_is_never_extrapolated_to_more_strings_either():
    # Every configuration exceeds the 10 A input; the maker's word stands
    # (prefer_manufacturer), on one string, not the 2-string watts maximum.
    e = one(current_graph(), st(**SMALL_INPUT, max_a=10), pn(**PANEL_12A), records=[rec(1, 2)])
    assert (e.config["series"], e.config["parallel"]) == (1, 1)
    assert "12 A exceeds the 10 A maximum" in next(a.text for a in e.assumptions
                                                    if a.code == "computed-disagrees")


def test_metrics_count_edges_whose_checks_are_incomplete():
    res = current_graph().build([st(1, **SMALL_INPUT), st(4, **SMALL_INPUT, max_a=30), pn(2, **PANEL_12A)],
                                [rec(1, 2), rec(4, 2)])
    m = res.metrics()
    assert m["compat_edges_incomplete"] == 1 and m["compat_edges_unchecked"] == 0
    (inc,) = [e for e in res.edges if "computed-check-incomplete" in e.flags]
    assert inc.a_model_id == 1 and inc.to_row()["config"]["flags"] == ["computed-check-incomplete"]


def test_records_given_the_other_way_round_match():
    e = one(graph(), st(**GOOD["st"]), pn(**GOOD["pn"]), records=[rec(2, 1)])
    assert e.decided_by == cg.DECIDED_MANUFACTURER and e.verdict == cg.FITS


def test_fits_in_parallel_only_comes_from_a_statement():
    e = one(graph(), st(**GOOD["st"]), pn(**GOOD["pn"]), records=[rec(1, 2, verdict="fits_in_parallel")])
    assert e.verdict == cg.FITS_IN_PARALLEL


# ---------------------------------------------------------------------------
# listed rulesets, exclusions, build options
# ---------------------------------------------------------------------------

def test_listed_rulesets_evaluate_only_pairs_with_records_and_report_missing_models():
    g = graph()
    models = [st(1), st(4), bat(3), bat(5)]
    recs = [rec(1, 3, relation="expands"), rec(4, 99, relation="expands")]
    res = g.build(models, recs, rulesets=["expansion"])
    assert [(e.a_model_id, e.b_model_id, e.verdict) for e in res.edges] == [(1, 3, cg.FITS)]
    assert res.unresolved == [{"ruleset": "expansion", "record_models": ["4", "99"], "missing": ["99"]}]
    assert res.edges[0].confidence == cg.CONF_MANUFACTURER and res.edges[0].store


def test_records_naming_unknown_models_are_reported_for_computed_rulesets_too():
    res = graph().build([st(1, **GOOD["st"]), pn(2, **GOOD["pn"])], [rec(1, 42)], rulesets=["solar"])
    assert res.unresolved == [{"ruleset": "solar", "record_models": ["1", "42"], "missing": ["42"]}]
    assert len(res.edges) == 1 and res.edges[0].decided_by == cg.DECIDED_COMPUTED


def test_a_listed_negative_is_stored_but_an_unlisted_pair_is_not_evaluated():
    g = graph()
    res = g.build([st(1), bat(3), bat(5)], [rec(1, 5, relation="expands", verdict="no_fit")],
                  rulesets=["expansion"])
    assert [(e.b_model_id, e.verdict, e.store) for e in res.edges] == [(5, cg.NO_FIT, True)]


@pytest.mark.parametrize("rule,value,excluded", [
    ({"field": "ac_v", "op": "gte", "value": 200}, 240, True),
    ({"field": "ac_v", "op": "gte", "value": 200}, 120, False),
    ({"field": "ac_v", "op": "lt", "value": 100}, 99.9, True),
    ({"field": "ac_v", "op": "eq", "value": 120}, 120.0, True),
    ({"field": "ac_v", "op": "ne", "value": 120}, 120, False),
    ({"field": "plug", "op": "in", "value": ["L14-30", "14-50"]}, "l14-30", True),
    ({"field": "plug", "op": "matches", "value": r"\b240 ?v\b"}, "240V dryer plug", True),
    ({"field": "grid", "op": "truthy"}, "yes", True),
    ({"field": "grid", "op": "truthy"}, False, False),
    ({"field": "grid", "op": "truthy", "kinds": ["panel"]}, True, False),
    # Numeric tests on text fail closed: any number in it counts.
    ({"field": "ac_v", "op": "gte", "value": 200}, "120/240V", True),
    ({"field": "ac_v", "op": "gte", "value": 200}, "240 VAC", True),
    ({"field": "ac_v", "op": "gte", "value": 200}, "120V AC", False),
    ({"field": "ac_v", "op": "gte", "value": 200}, "unknown", False),
])
def test_policy_excludes_models_by_spec_value(rule, value, excluded):
    g = graph(extra={"policy": {"exclude_models": [rule]}})
    s = st(max_v=60, min_v=12, **{rule["field"]: value})
    out = g.evaluate_pair(s, pn(**GOOD["pn"]), ruleset="solar")
    assert isinstance(out, cg.Skip) == excluded
    if excluded:
        assert out.reason.startswith(f"excluded-model:{rule['field']}:{rule['op']}") and out.reason.endswith(":1")


def test_values_sourced_from_amazon_never_reach_an_edge(caplog):
    amazon = {"source_url": "https://www.amazon.com/dp/B0ABCDEFGH", "source_kind": "manufacturer-page",
              "source_id": 7}
    s = cg.ModelSpec.from_values(1, "station", GOOD["st"], source=amazon)
    p = cg.ModelSpec.from_values(2, "panel", GOOD["pn"], source=amazon)
    assert s.values == {} and p.values == {}
    assert "sourced from Amazon dropped" in caplog.text
    e = one(graph(), s, p)
    assert e.verdict == cg.UNKNOWN and not e.store and e.evidence_source_ids == []
    mixed = cg.ModelSpec(1, "station", {"max_v": cg.FieldValue(60, "V", source_url=amazon["source_url"]),
                                        "min_v": cg.FieldValue(12, "V", source_url="https://maker.example/s")})
    assert set(mixed.values) == {"min_v"}                               # direct construction too
    rows = [{"field": "max_v", "value_num": 60, "unit": "V", "status": "accepted",
             "source_url": amazon["source_url"]},
            {"field": "min_v", "value_num": 12, "unit": "V", "status": "accepted", "source_id": 3}]
    assert set(cg.ModelSpec.from_db_rows(1, "station", rows).values) == {"min_v"}


def test_build_with_only_models_evaluates_pairs_touching_them():
    g = graph()
    models = [st(1, **GOOD["st"]), st(4, **GOOD["st"]), pn(2, **GOOD["pn"]), pn(5, **GOOD["pn"])]
    res = g.build(models, only_models=[5])
    assert sorted((e.a_model_id, e.b_model_id) for e in res.edges) == [(1, 5), (4, 5)]


def test_iter_build_batches_partition_the_full_build():
    g = graph()
    models = [st(i, max_v=40 + 10 * i, min_v=12) for i in (1, 4, 6, 7, 8)] + \
             [pn(j, voc=15 + j, vmp=12 + j) for j in (2, 5, 9)] + [bat(3), bat(10)]
    recs = [rec(1, 3, relation="expands"), rec(7, 10, relation="expands", verdict="no_fit")]
    full = {e.key: e.to_row() for e in g.build(models, recs).edges}
    seen: dict = {}
    batches = list(g.iter_build(models, recs, batch_size=2))
    assert [ids for ids, _ in batches] == [[1, 4], [6, 7], [8]]
    for ids, res in batches:
        for e in res.edges:
            assert e.a_model_id in ids and e.key not in seen
            seen[e.key] = e.to_row()
    assert seen == full
    with pytest.raises(ValueError):
        list(g.iter_build(models, batch_size=0))


def test_batched_plans_write_the_same_rows_as_one_plan():
    g = graph()
    models = catalog()
    existing = rows_of(g.build(models, RECS))
    g2 = graph(cold_version=2)
    one_shot = g2.plan(g2.build(models, RECS), existing)
    batched_keys = set()
    for ids, res in g2.iter_build(models, RECS, batch_size=1):
        rows = [r for r in existing if r["a_model_id"] in ids]
        plan = g2.plan(res, rows)
        assert plan.stale == [] and plan.deletes == []
        batched_keys |= {u.key for u in plan.upserts}
    assert batched_keys == {u.key for u in one_shot.upserts}


def test_duplicate_model_ids_and_unknown_rulesets_are_reported():
    g = graph()
    res = g.build([st(1, **GOOD["st"]), st(1, **GOOD["st"]), pn(2, **GOOD["pn"])], rulesets=["solar", "nope"])
    assert any("duplicate model ids" in p for p in res.problems)
    assert any("unknown ruleset 'nope'" in p for p in res.problems)
    assert len(res.edges) == 1


def test_metrics_and_summary():
    g = graph()
    res = g.build([st(1, **GOOD["st"]), st(4, **OVERVOLT["st"]), st(6), pn(2, **GOOD["pn"])],
                  [rec(4, 2)])
    m = res.metrics()
    assert m["compat_pairs_evaluated"] == 3 and m["compat_edges_fit"] == 2
    assert m["compat_edges_unknown"] == 1 and m["compat_edges_manufacturer"] == 1
    assert m["compat_edges_disagree"] == 0 and m["compat_edges_stored"] == 2
    s = res.summary()
    assert s["by_verdict"] == {"fits": 2, "unknown": 1} and s["design_temp_c"] == -40.0


# ---------------------------------------------------------------------------
# the edge row
# ---------------------------------------------------------------------------

def test_edge_row_has_exactly_the_compat_edges_columns_and_serialises():
    e = one(graph(), st(**GOOD["st"]), pn(**GOOD["pn"]))
    row = e.to_row()
    assert set(row) == {"a_model_id", "b_model_id", "relation", "verdict", "config", "via_model_id",
                        "rule_id", "rule_version", "evidence_source_ids", "confidence", "computed_at"}
    assert row["rule_id"] == "solar"
    assert row["rule_version"] == "solar@1[mfr@1,cold@1,minv@1]e" + cg.ENGINE_VERSION
    assert row["evidence_source_ids"] == [101, 202]
    assert row["computed_at"] == CLOCK
    c = row["config"]
    for k in ("series", "parallel", "cold_voc_v", "design_temp_c", "max_units", "assumptions",
              "checks", "options", "inputs_hash", "rules_sig", "decided_by", "evidence"):
        assert k in c, k
    assert c["evidence"] == [
        {"source_id": 101, "url": "https://maker.example/s1", "kind": "manufacturer-page",
         "fields": ["a:max_v", "a:min_v"]},
        {"source_id": 202, "url": "https://maker.example/p2", "kind": "manual-pdf",
         "fields": ["b:voc", "b:vmp"]}]
    json.dumps(row)
    full = e.to_dict()
    assert full["config"]["checks"][0]["values"]["factor"] == 1.25


def test_inputs_hash_tracks_inputs_but_not_the_clock():
    a, b = st(**GOOD["st"]), pn(**GOOD["pn"])
    h = one(graph(), a, b).inputs_hash
    later = cg.CompatGraph(config(), clock=lambda: "2027-01-01T00:00:00Z")
    assert one(later, a, b).inputs_hash == h
    assert one(graph(), a, pn(voc=21.6, vmp=18.4)).inputs_hash != h
    moved = cg.ModelSpec.from_values(2, "panel", GOOD["pn"], name="Panel 2", brand="Maker",
                                     source={"source_url": "https://maker.example/p2-v2",
                                             "source_kind": "manual-pdf", "source_id": 299})
    assert one(graph(), a, moved).inputs_hash != h                    # new source -> new evidence
    assert one(graph(headroom=0.08), a, b).inputs_hash != h          # effective params
    g = graph()
    assert one(g, a, b, climate=g.climate(design_temp_c=-45)).inputs_hash != h
    assert one(graph(), a, b, records=[rec(1, 2)]).inputs_hash != h


# ---------------------------------------------------------------------------
# plan(): version bumps, deletes, manual rows
# ---------------------------------------------------------------------------

def rows_of(result):
    return [e.to_row() for e in result.stored()]


def catalog():
    return [st(1, **GOOD["st"]), st(4, **OVERVOLT["st"]), pn(2, **GOOD["pn"]), pn(5, voc=30, vmp=25, tc=-0.3),
            bat(3)]


RECS = [rec(1, 3, relation="expands")]


def test_first_build_upserts_everything_stored_and_a_rebuild_changes_nothing():
    g = graph()
    res = g.build(catalog(), RECS)
    plan = g.plan(res, [])
    assert len(plan.upserts) == len(res.stored()) == 5
    again = g.plan(g.build(catalog(), RECS), rows_of(res))
    assert again.upserts == [] and again.deletes == [] and len(again.unchanged) == 5


def test_bumping_a_rule_version_recomputes_exactly_the_rulesets_that_use_it():
    g1 = graph()
    existing = rows_of(g1.build(catalog(), RECS))
    g2 = graph(cold_version=2)
    assert g2.ruleset_version("solar") != g1.ruleset_version("solar")
    assert g2.ruleset_version("expansion") == g1.ruleset_version("expansion")
    assert g2.pairs_to_recompute(existing) == {("1", "2", "solar"), ("1", "5", "solar"),
                                               ("4", "2", "solar"), ("4", "5", "solar")}
    plan = g2.plan(g2.build(catalog(), RECS), existing)
    assert {(u.rule_id, u.rule_version) for u in plan.upserts} == {("solar", g2.ruleset_version("solar"))}
    assert len(plan.upserts) == len(plan.recomputed) == 4
    assert [(d["rule_id"]) for d in plan.unchanged] == ["expansion"]


def test_bumping_a_ruleset_version_or_the_engine_recomputes(monkeypatch):
    g1 = graph()
    existing = rows_of(g1.build(catalog(), RECS))
    plan = graph(solar_version=2).plan(graph(solar_version=2).build(catalog(), RECS), existing)
    assert len(plan.recomputed) == 4
    monkeypatch.setattr(cg, "ENGINE_VERSION", "999")
    g3 = graph()
    assert g3.ruleset_version("expansion").endswith("e999")
    assert len(g3.plan(g3.build(catalog(), RECS), existing).recomputed) == 5


def test_changed_specs_rewrite_only_their_edges():
    g = graph()
    existing = rows_of(g.build(catalog(), RECS))
    cat = catalog()
    cat[3] = pn(5, voc=31, vmp=25, tc=-0.3)
    plan = g.plan(g.build(cat, RECS), existing)
    assert sorted((u.a_model_id, u.b_model_id) for u in plan.upserts) == [(1, 5), (4, 5)]
    assert plan.recomputed == []


def test_a_pair_that_becomes_unknown_or_excluded_is_deleted():
    g = graph()
    existing = rows_of(g.build(catalog(), RECS))
    cat = catalog()
    cat[2] = pn(2, vmp=18.4)                                    # Voc gone: unknown, not stored
    plan = g.plan(g.build(cat, RECS), existing)
    assert {(d["a_model_id"], d["b_model_id"]) for d in plan.deletes} == {(1, 2), (4, 2)}
    gx = graph(extra={"policy": {"exclude_models": [{"field": "grid", "op": "truthy"}]}})
    cat2 = catalog()
    cat2[0] = st(1, grid=True, **GOOD["st"])
    plan2 = gx.plan(gx.build(cat2, RECS), existing)
    assert {(d["a_model_id"], d["b_model_id"], d["rule_id"]) for d in plan2.deletes} == {
        (1, 2, "solar"), (1, 5, "solar"), (1, 3, "expansion")}


def test_manual_rows_are_never_overwritten_or_deleted():
    g = graph()
    existing = rows_of(g.build(catalog(), RECS))
    for r in existing:
        if (r["a_model_id"], r["b_model_id"]) in ((1, 2), (4, 5)):
            r["confidence"] = cg.CONF_MANUAL
            r["verdict"] = cg.NO_FIT
    cat = catalog()
    cat[3] = pn(5, vmp=25)                                       # (4, 5) would now be deleted
    plan = g.plan(g.build(cat, RECS), existing)
    protected = {(p["a_model_id"], p["b_model_id"]) for p in plan.protected}
    assert protected == {(1, 2), (4, 5)}
    touched = {(u.a_model_id, u.b_model_id) for u in plan.upserts} | \
              {(d["a_model_id"], d["b_model_id"]) for d in plan.deletes}
    assert not touched & protected


def test_scope_full_deletes_rows_for_models_that_left_and_evaluated_scope_reports_stale_rows():
    g = graph()
    existing = rows_of(g.build(catalog(), RECS))
    smaller = [m for m in catalog() if m.model_id != 5]
    partial = g.plan(g.build(smaller, RECS), existing)
    assert partial.deletes == [] and partial.stale == []          # untouched, still current
    full = g.plan(g.build(smaller, RECS), existing, scope="full")
    assert {(d["a_model_id"], d["b_model_id"]) for d in full.deletes} == {(1, 5), (4, 5)}
    g2 = graph(cold_version=3)
    stale = g2.plan(g2.build(smaller, RECS), existing)
    assert {(d["a_model_id"], d["b_model_id"]) for d in stale.stale} == {(1, 5), (4, 5)}
    with pytest.raises(ValueError):
        g.plan(g.build(smaller), existing, scope="some")


def test_rows_of_other_or_disabled_rulesets_are_left_alone():
    g = graph()
    existing = rows_of(g.build(catalog(), RECS))
    foreign = dict(existing[0], rule_id="someone-else", rule_version="x@1[]e1")
    off = graph(extra={"rulesets": {"solar": {"enabled": False}}})
    plan = off.plan(off.build(catalog(), RECS), existing + [foreign])
    assert plan.disabled == ["solar"]
    assert plan.deletes == [] and plan.upserts == [] and plan.stale == []
    assert off.stale([foreign]) == []
    # ... but they are listed, so an agent can surface them.
    assert sorted((o["rule_id"], o["reason"]) for o in plan.orphaned) == \
        [("solar", "ruleset-disabled")] * 4 + [("someone-else", "unknown-ruleset")]
    assert plan.summary()["orphaned"] == 5


def test_purge_orphaned_deletes_rows_of_disabled_and_removed_rulesets_but_not_manual_ones():
    g = graph()
    existing = rows_of(g.build(catalog(), RECS))
    foreign = dict(existing[0], rule_id="someone-else", rule_version="x@1[]e1")
    keep = dict(existing[1], confidence=cg.CONF_MANUAL)
    off = graph(extra={"rulesets": {"solar": {"enabled": False}}})
    assert (existing[0]["rule_id"], existing[1]["rule_id"]) == ("expansion", "solar")
    plan = off.plan(off.build(catalog(), RECS), existing[2:] + [foreign, keep], purge_orphaned=True)
    assert {(d["a_model_id"], d["b_model_id"], d["rule_id"]) for d in plan.deletes} == {
        (1, 5, "solar"), (4, 2, "solar"), (4, 5, "solar"), (1, 3, "someone-else")}
    assert {(p["a_model_id"], p["b_model_id"], p["rule_id"]) for p in plan.protected} == {(1, 2, "solar")}


def test_a_partial_build_never_deletes_rows_outside_the_part_it_covered():
    g = graph()
    models = catalog()
    existing = rows_of(g.build(models, RECS))
    one_model = g.build(models, RECS, only_models=[5])
    assert one_model.partial and one_model.covers(1, 5) and not one_model.covers(1, 2)
    assert g.plan(one_model, existing, scope="full").deletes == []
    for ids, res in g.iter_build(models, RECS, batch_size=1):
        assert res.partial
        assert g.plan(res, existing, scope="full").deletes == []          # unfiltered rows, still safe
    # Inside the covered part, scope="full" still removes pairs whose model left.
    gone = [m for m in models if m.model_id != 2]
    plan = g.plan(g.build(gone, RECS, only_models=[1]), existing, scope="full")
    assert {(d["a_model_id"], d["b_model_id"]) for d in plan.deletes} == {(1, 2)}


def test_plan_refuses_edges_computed_warmer_than_the_default_design_temperature():
    g = graph(extra={"climate": {"zip_min_c": {"33101": 7.0, "99701": -45.0}}})
    models = catalog()
    with pytest.raises(cg.CompatGraphError, match="warmer than the -40 °C default"):
        g.plan(g.build(models, RECS, climate=g.climate(zip_code="33101")), [])
    with pytest.raises(cg.CompatGraphError):
        g.plan(g.build(models, RECS, climate=cg.Climate(25.0)), [])
    allowed = g.plan(g.build(models, RECS, climate=g.climate(zip_code="33101")), [],
                     allow_location_climate=True)
    assert allowed.upserts and any("location climate" in w for w in allowed.warnings)
    colder = g.plan(g.build(models, RECS, climate=g.climate(zip_code="99701")), [])
    assert colder.upserts and any("colder zip climate" in w for w in colder.warnings)


def test_param_change_without_a_version_bump_is_rewritten_and_warned():
    g = graph()
    existing = rows_of(g.build(catalog(), RECS))
    g2 = graph(headroom=0.07)
    plan = g2.plan(g2.build(catalog(), RECS), existing)
    assert plan.upserts and plan.recomputed == []
    assert any("without a version bump" in w for w in plan.warnings)


def test_duplicate_existing_rows_are_warned():
    g = graph()
    existing = rows_of(g.build(catalog(), RECS))
    plan = g.plan(g.build(catalog(), RECS), existing + [existing[0]])
    assert any("duplicate existing row" in w for w in plan.warnings)


def test_existing_rows_with_json_text_config_are_understood():
    g = graph()
    existing = rows_of(g.build(catalog(), RECS))
    for r in existing:
        r["config"] = json.dumps(r["config"])
    assert len(g.plan(g.build(catalog(), RECS), existing).unchanged) == 5


# ---------------------------------------------------------------------------
# EdgeWriter (fake DB-API connection) and the reference DDL
# ---------------------------------------------------------------------------

class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.description = [("a_model_id",), ("b_model_id",), ("relation",), ("rule_id",)]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        if self.conn.fail_on and self.conn.fail_on in sql:
            raise RuntimeError("boom")
        self.conn.calls.append((sql, params))

    def fetchall(self):
        return [(1, 2, "charges_from", "solar")]


class FakeConn:
    def __init__(self, fail_on=""):
        self.calls, self.commits, self.rollbacks, self.fail_on = [], 0, 0, fail_on

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


@pytest.fixture
def plain_batches(monkeypatch):
    def run(cur, sql, rows, page_size):
        for r in rows:
            cur.execute(sql, r)
    monkeypatch.setattr(cg, "_run_batch", run)


def test_writer_sql_protects_manual_rows_and_uses_named_params():
    w = cg.EdgeWriter(FakeConn())
    up = w.upsert_sql()
    assert up.startswith("INSERT INTO compat_edges (a_model_id, b_model_id, relation, verdict, config,")
    assert "%(config)s::jsonb" in up and "%(computed_at)s::timestamptz" in up
    assert "ON CONFLICT (a_model_id, b_model_id, relation, rule_id) DO UPDATE SET verdict = EXCLUDED.verdict" in up
    assert "WHERE compat_edges.confidence IS DISTINCT FROM 'manual' OR EXCLUDED.confidence = 'manual'" in up
    assert w.delete_sql().endswith("AND confidence IS DISTINCT FROM 'manual'")
    assert "rule_id = ANY(%(rule_ids)s)" in w.select_sql()


def test_writer_column_map_and_identifier_validation():
    w = cg.EdgeWriter(FakeConn(), table="site.edges", columns={"rule_id": "ruleset", "config": "details"})
    assert "INSERT INTO site.edges" in w.upsert_sql() and "ruleset" in w.upsert_sql()
    assert "details AS config" in w.select_sql()
    with pytest.raises(cg.CompatGraphError):
        cg.EdgeWriter(FakeConn(), table="edges; DROP TABLE x")
    with pytest.raises(cg.CompatGraphError):
        cg.EdgeWriter(FakeConn(), columns={"verdict": "verdict--"})


def test_writer_applies_a_plan_in_one_transaction(plain_batches):
    g = graph()
    res = g.build(catalog(), RECS)
    conn = FakeConn()
    w = cg.EdgeWriter(conn, cfg=g.cfg)
    plan = g.plan(res, [{"a_model_id": 9, "b_model_id": 8, "relation": "charges_from", "rule_id": "solar",
                         "rule_version": "old", "confidence": "computed", "config": {}}], scope="full")
    out = w.apply(plan)
    assert out == {"upserted": 5, "deleted": 1} and conn.commits == 1
    params = [p for sql, p in conn.calls if sql.startswith("INSERT")]
    assert all(isinstance(p["config"], str) and json.loads(p["config"])["inputs_hash"] for p in params)
    (dsql, dparams), = [(s, p) for s, p in conn.calls if s.startswith("DELETE")]
    assert dparams == {"a_model_id": 9, "b_model_id": 8, "relation": "charges_from", "rule_id": "solar"}


def test_writer_rolls_back_on_failure(plain_batches):
    g = graph()
    conn = FakeConn(fail_on="DELETE")
    plan = g.plan(g.build(catalog(), RECS), [{"a_model_id": 9, "b_model_id": 8, "relation": "charges_from",
                                              "rule_id": "solar", "confidence": "computed"}], scope="full")
    with pytest.raises(RuntimeError):
        cg.EdgeWriter(conn).apply(plan)
    assert conn.commits == 0 and conn.rollbacks == 1


def test_writer_loads_existing_rows():
    conn = FakeConn()
    rows = cg.EdgeWriter(conn).load_existing(rule_ids=["solar"])
    assert rows == [{"a_model_id": 1, "b_model_id": 2, "relation": "charges_from", "rule_id": "solar"}]
    assert conn.calls[0][1] == {"all": False, "rule_ids": ["solar"]}
    cg.EdgeWriter(conn).load_existing()
    assert conn.calls[1][1] == {"all": True, "rule_ids": []}


def test_reference_ddl():
    ddl = cg.reference_ddl()
    assert "UNIQUE (a_model_id, b_model_id, relation, rule_id)" in ddl
    for v in cg.VERDICTS + cg.CONFIDENCES:
        assert f"'{v}'" in ddl
    assert "evidence_source_ids bigint[]" in ddl and "config jsonb" in ddl
    assert "a_model_id uuid" in cg.reference_ddl(model_id_type="uuid")
    with pytest.raises(cg.CompatGraphError):
        cg.reference_ddl(model_id_type="bigint; DROP")


# ---------------------------------------------------------------------------
# manufacturer lists: model-keyed consumable fitment
# ---------------------------------------------------------------------------

def robots():
    return [cg.ModelSpec.from_values(51, "robot", {"model_number": "DN10"}, name="DEEBOT N10", brand="ECOVACS"),
            cg.ModelSpec.from_values(52, "robot", {}, name="DEEBOT N10 PLUS", brand="ECOVACS"),
            cg.ModelSpec.from_values(53, "robot", {}, name="DEEBOT N8", brand="ECOVACS"),
            cg.ModelSpec.from_values(54, "robot", {}, name="Roomba i7", brand="iRobot"),
            cg.ModelSpec.from_values(55, "robot", {}, name="Roomba i7+", brand="iRobot"),
            cg.ModelSpec.from_values(56, "robot", {}, name="Roomba j7", brand="iRobot", aliases=("j7",)),
            cg.ModelSpec.from_values(57, "robot", {}, name="Model X", brand="AlphaBots"),
            cg.ModelSpec.from_values(58, "robot", {}, name="Model X", brand="BetaBots")]


@pytest.mark.parametrize("text,expected", [
    ("Compatible with: DEEBOT N10, N10 PLUS and DEEBOT X1 OMNI",
     ["DEEBOT N10", "DEEBOT N10 PLUS", "DEEBOT X1 OMNI"]),
    ("Fits Roomba i3, i3+, i7 / i7+; j7", ["Roomba i3", "Roomba i3+", "Roomba i7", "Roomba i7+", "Roomba j7"]),
    ("Explorer 1000, 1500 & 2000 Plus", ["Explorer 1000", "Explorer 1500", "Explorer 2000 Plus"]),
    ("", []),
])
def test_split_model_list(text, expected):
    assert cg.split_model_list(text) == expected


def test_name_keys_keep_plus_variants_apart_and_drop_a_leading_brand():
    assert cg.name_key("Roomba i7+") == "roombai7plus" != cg.name_key("Roomba i7")
    assert cg.name_key("iRobot Roomba j7", brand="iRobot") == "roombaj7"
    assert cg.name_key("DEEBOT N10", brand="ECOVACS") == "deebotn10"


def test_model_index_resolves_exactly_and_never_guesses():
    idx = cg.ModelIndex(robots())
    assert idx.resolve("Deebot n10").model_id == 51
    assert idx.resolve("ECOVACS DEEBOT N10").model_id == 51
    assert idx.resolve("DN10").model_id == 51                              # model number
    assert idx.resolve("N10", brand="ECOVACS").status == "none"           # not a known name
    assert idx.resolve("Roomba i7+").model_id == 55
    assert idx.resolve("j7").model_id == 56                                # alias
    amb = idx.resolve("Model X")
    assert amb.status == "ambiguous" and set(amb.candidates) == {57, 58}
    assert idx.resolve("Model X", brand="AlphaBots").status == "ambiguous"
    assert idx.resolve("AlphaBots Model X").model_id == 57
    assert idx.resolve("DEEBOT N10", kinds=["consumable"]).status == "none"


def test_fit_records_from_a_published_list():
    idx = cg.ModelIndex(robots())
    recs, bad = cg.fit_records_from_list(
        61, "Compatible with: DEEBOT N10, N10 PLUS and DEEBOT X1 OMNI", index=idx,
        relation="consumable_for", source_url="https://robots.example/kit",
        source_kind="manufacturer-page", source_id=261)
    assert [(r.a, r.b, r.relation, r.statement) for r in recs] == [
        (51, 61, "consumable_for", "DEEBOT N10"), (52, 61, "consumable_for", "DEEBOT N10 PLUS")]
    assert [(b.name, b.status) for b in bad] == [("DEEBOT X1 OMNI", "none")]
    recs_a, _ = cg.fit_records_from_list(61, ["DEEBOT N10", "DEEBOT N10"], index=idx,
                                         relation="r", subject_side="a")
    assert [(r.a, r.b) for r in recs_a] == [(61, 51)]                      # de-duplicated
    with pytest.raises(ValueError):
        cg.fit_records_from_list(61, "x", index=idx, relation="r", subject_side="c")


def test_consumable_fitment_edges_through_the_manufacturer_list_rule():
    cfg = cg.resolve_config(file_cfg={}, site_cfg={
        "rules": {"mfr": {"kind": "manufacturer_list", "version": 1}},
        "rulesets": {"fit": {"version": 1, "relation": "consumable_for", "a_kinds": ["robot"],
                             "b_kinds": ["consumable"], "rules": ["mfr"],
                             "store_verdicts": ["fits", "no_fit"]}}})
    g = cg.CompatGraph(cfg, clock=lambda: CLOCK)
    kit = cg.ModelSpec.from_values(61, "consumable", {}, name="Kit")
    recs, _ = cg.fit_records_from_list(61, "DEEBOT N10, N10 PLUS", index=cg.ModelIndex(robots()),
                                       relation="consumable_for", source_url="https://robots.example/kit",
                                       source_kind="manufacturer-page", source_id=261)
    res = g.build(robots() + [kit], recs)
    assert g.rulesets["fit"].pairs == "listed"
    assert [(e.a_model_id, e.b_model_id, e.verdict, e.confidence) for e in res.edges] == [
        (51, 61, "fits", "manufacturer-listed"), (52, 61, "fits", "manufacturer-listed")]
    assert all(e.evidence_source_ids == [261] for e in res.edges)
