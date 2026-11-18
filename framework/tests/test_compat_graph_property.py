"""framework.core.compat_graph: property sweeps at -40 °C.

Seeded random sweeps (offline, deterministic) check the solar rules against
an independent oracle written here from the published NEC 690.7(A) rows and
the formula n x Voc x (1 + tc x (Tmin - 25)):

* every configuration the graph calls a fit keeps >= 5 % headroom at -40 °C,
  and every configuration the oracle calls a fit is one (no false negatives
  either: the rule is exact, not just safe);
* an unknown coefficient means x1.25 at -40 °C; an implausible one never
  makes a string look cooler than NEC;
* more Voc, a lower limit or a colder design temperature never adds fits;
* configuration enumeration respects the array limits and wiring;
* a missing required input never yields a fit;
* verdict labels agree with the options.

If the ``hypothesis`` package is installed, the same headroom property also
runs under hypothesis; otherwise that test is skipped and the sweeps stand."""
from __future__ import annotations

import random

import pytest

from framework.core import compat_graph as cg

CLOCK = "2026-11-17T00:00:00Z"
SEEDS = list(range(24))
CASES_PER_SEED = 40
TOL = 1e-6           # cases this close to a boundary are not judged by the oracle

# NEC Table 690.7(A), transcribed independently of the module.
NEC_ROWS = [(24, 20, 1.02), (19, 15, 1.04), (14, 10, 1.06), (9, 5, 1.08), (4, 0, 1.10),
            (-1, -5, 1.12), (-6, -10, 1.14), (-11, -15, 1.16), (-16, -20, 1.18),
            (-21, -25, 1.20), (-26, -30, 1.21), (-31, -35, 1.23), (-36, -40, 1.25)]


def oracle_nec(t_int):
    for warm, cold, f in NEC_ROWS:
        if cold <= t_int <= warm:
            return f
    return 1.0 if t_int >= 25 else None


def oracle_factor(tc, t=-40):
    nec = oracle_nec(t)
    if tc is None:
        return nec
    own = 1 + tc / 100 * (t - 25)
    if -0.6 <= tc <= -0.2:
        return max(1.0, own)
    return max([nec, 1.0] + ([own] if tc < 0 else []))


def graph(*, arrays=(4, 4, 4), rules=("cold",), headroom=None):
    rule_defs = {
        "cold": {"kind": "max_voltage_cold", "version": 1, "a_fields": {"max_v": "max_v"},
                 "b_fields": {"voc": "voc", "tc": "tc"}},
        "minv": {"kind": "min_voltage", "version": 1, "a_fields": {"min_v": "min_v"},
                 "b_fields": {"vmp": "vmp"}},
        "cur": {"kind": "max_current", "version": 1, "a_fields": {"max_a": "max_a"},
                "b_fields": {"isc": "isc"}},
    }
    if headroom is not None:
        rule_defs["cold"]["params"] = {"headroom": headroom}
    rs = {"version": 1, "relation": "charges_from", "a_kinds": ["station"], "b_kinds": ["panel"],
          "rules": list(rules)}
    if arrays:
        rs["arrays"] = {"max_series": arrays[0], "max_parallel": arrays[1], "max_units": arrays[2]}
    cfg = cg.resolve_config(file_cfg={}, site_cfg={"rules": rule_defs, "rulesets": {"pv": rs}})
    return cg.CompatGraph(cfg, clock=lambda: CLOCK)


def st(**v):
    return cg.ModelSpec.from_values(1, "station", v)


def pn(**v):
    return cg.ModelSpec.from_values(2, "panel", {k: x for k, x in v.items() if x is not None})


def ev(g, a, b, climate=None):
    out = g.evaluate_pair(a, b, ruleset="pv", climate=climate)
    assert isinstance(out, cg.Edge)
    return out


def random_tc(rnd):
    r = rnd.random()
    if r < 0.35:
        return None                                   # not published
    if r < 0.85:
        return round(rnd.uniform(-0.5, -0.2), 4)      # plausible
    return round(rnd.choice([rnd.uniform(-1.0, -0.61), rnd.uniform(-0.19, 0.1)]), 4)


def fit_set(e):
    return {(o.series, o.parallel) for o in e.options if o.outcome == cg.FITS}


# ---------------------------------------------------------------------------

@pytest.mark.parametrize("seed", SEEDS)
def test_cold_verdicts_match_the_oracle_and_always_keep_headroom_at_minus_40(seed):
    rnd = random.Random(seed)
    g = graph()
    judged = 0
    for _ in range(CASES_PER_SEED):
        max_v = round(rnd.uniform(12, 200), 2)
        voc = round(rnd.uniform(5, 60), 3)
        tc = random_tc(rnd)
        e = ev(g, st(max_v=max_v), pn(voc=voc, tc=tc))
        factor = oracle_factor(tc)
        assert e.config["design_temp_c"] == -40.0
        for o in e.options:
            cold = o.series * voc * factor
            assert o.cold_voc_v == pytest.approx(cold, rel=1e-12)
            if o.outcome == cg.FITS:                            # safety: never a fit without headroom
                assert cold <= 0.95 * max_v + 1e-9
            if abs(cold - 0.95 * max_v) > TOL:                  # exactness away from the boundary
                judged += 1
                assert (o.outcome == cg.FITS) == (cold < 0.95 * max_v)
                if o.outcome != cg.FITS:
                    assert o.outcome == cg.OVER_LIMIT
    assert judged > CASES_PER_SEED


@pytest.mark.parametrize("seed", SEEDS[:8])
def test_unknown_tc_means_x125_and_distrusted_tc_is_never_cooler_than_nec(seed):
    rnd = random.Random(1000 + seed)
    g = graph(arrays=None)
    for _ in range(CASES_PER_SEED):
        voc = rnd.uniform(5, 60)
        unknown = ev(g, st(max_v=1000), pn(voc=voc))
        (c,) = unknown.checks
        assert c.values["factor"] == 1.25 and c.values["method"] == "nec-690.7a"
        bad_tc = rnd.choice([rnd.uniform(-0.19, 0.3), rnd.uniform(-2.0, -0.61)])
        distrusted = ev(g, st(max_v=1000), pn(voc=voc, tc=bad_tc))
        (d,) = distrusted.checks
        assert d.values["method"] == "nec-690.7a(tc-rejected)"
        assert d.values["factor"] >= 1.25
        good_tc = rnd.uniform(-0.6, -0.2)
        trusted = ev(g, st(max_v=1000), pn(voc=voc, tc=good_tc))
        (t,) = trusted.checks
        assert t.values["factor"] == pytest.approx(1 + good_tc / 100 * -65)


@pytest.mark.parametrize("seed", SEEDS[:12])
def test_more_voc_a_lower_limit_or_colder_weather_never_adds_fits(seed):
    rnd = random.Random(2000 + seed)
    g = graph(arrays=(5, 2, 8))
    for _ in range(CASES_PER_SEED // 2):
        max_v = rnd.uniform(20, 160)
        voc = rnd.uniform(8, 50)
        tc = random_tc(rnd)
        base = fit_set(ev(g, st(max_v=max_v), pn(voc=voc, tc=tc)))
        hotter_panel = fit_set(ev(g, st(max_v=max_v), pn(voc=voc * rnd.uniform(1.0, 1.5), tc=tc)))
        lower_limit = fit_set(ev(g, st(max_v=max_v * rnd.uniform(0.6, 1.0)), pn(voc=voc, tc=tc)))
        colder = fit_set(ev(g, st(max_v=max_v), pn(voc=voc, tc=tc if tc is not None else -0.3),
                            climate=g.climate(design_temp_c=rnd.uniform(-60, -40))))
        same_tc = fit_set(ev(g, st(max_v=max_v), pn(voc=voc, tc=tc if tc is not None else -0.3)))
        assert hotter_panel <= base
        assert lower_limit <= base
        assert colder <= same_tc


@pytest.mark.parametrize("seed", SEEDS[:12])
def test_enumeration_respects_array_limits_and_wiring(seed):
    rnd = random.Random(3000 + seed)
    for _ in range(10):
        ms, mp = rnd.randint(1, 6), rnd.randint(1, 6)
        mu = rnd.randint(1, 12)
        g = graph(arrays=(ms, mp, mu), rules=("cold", "minv", "cur"))
        e = ev(g, st(max_v=rnd.uniform(20, 400), min_v=rnd.uniform(0, 60), max_a=rnd.uniform(5, 40)),
               pn(voc=rnd.uniform(10, 50), vmp=rnd.uniform(8, 40), isc=rnd.uniform(1, 12),
                  tc=random_tc(rnd)))
        combos = [(o.series, o.parallel) for o in e.options]
        assert combos[0] == (1, 1)
        assert len(combos) == len(set(combos))
        expected = {(s, p) for s in range(1, ms + 1) for p in range(1, mp + 1) if s * p <= mu}
        assert set(combos) == expected
        fits = [o for o in e.options if o.outcome == cg.FITS]
        if fits:
            assert (e.config["series"], e.config["parallel"]) in fit_set(e)
            assert e.config["max_units"] == max(o.units for o in fits)
            assert e.config["min_series"] == min(o.series for o in fits)


@pytest.mark.parametrize("seed", SEEDS[:12])
def test_verdict_labels_agree_with_the_options(seed):
    rnd = random.Random(4000 + seed)
    g = graph(arrays=(4, 3, 6), rules=("cold", "minv", "cur"))
    seen = set()
    for _ in range(CASES_PER_SEED):
        fields = {"max_v": rnd.uniform(15, 200), "min_v": rnd.uniform(0, 70), "max_a": rnd.uniform(3, 30)}
        pfields = {"voc": rnd.uniform(8, 50), "vmp": rnd.uniform(6, 42), "isc": rnd.uniform(1, 14),
                   "tc": random_tc(rnd)}
        if rnd.random() < 0.15:
            fields.pop(rnd.choice(sorted(fields)))
        e = ev(g, st(**fields), pn(**pfields))
        seen.add(e.verdict)
        fits = fit_set(e)
        single = next(o for o in e.options if (o.series, o.parallel) == (1, 1))
        assert e.verdict != cg.FITS_IN_PARALLEL           # parallel never rescues a single unit
        if e.verdict == cg.FITS:
            assert single.outcome == cg.FITS
        elif e.verdict == cg.FITS_IN_SERIES:
            assert single.outcome != cg.FITS and any(p == 1 for _, p in fits)
        else:
            assert not fits
            if e.verdict in (cg.OVER_LIMIT, cg.NO_FIT):
                assert e.verdict == single.outcome
                assert all(o.outcome in (cg.OVER_LIMIT, cg.NO_FIT) for o in e.options)
            else:
                assert e.verdict == cg.UNKNOWN
                assert any(o.outcome == cg.UNKNOWN for o in e.options)
    assert {cg.FITS, cg.OVER_LIMIT} <= seen


@pytest.mark.parametrize("seed", SEEDS[:8])
def test_a_missing_required_input_never_yields_a_fit(seed):
    rnd = random.Random(5000 + seed)
    g = graph(arrays=(3, 2, 6), rules=("cold", "minv", "cur"))
    required = {"station": ["max_v", "min_v", "max_a"], "panel": ["voc", "vmp", "isc"]}
    for _ in range(CASES_PER_SEED):
        s = {"max_v": 1000.0, "min_v": 1.0, "max_a": 1000.0}     # would fit with everything present
        p = {"voc": 20.0, "vmp": 17.0, "isc": 5.0, "tc": random_tc(rnd)}
        side = rnd.choice(["station", "panel"])
        drop = rnd.choice(required[side])
        (s if side == "station" else p).pop(drop)
        e = ev(g, st(**s), pn(**p))
        assert e.verdict == cg.UNKNOWN, (side, drop)
        assert not fit_set(e)


@pytest.mark.parametrize("headroom", [0.0, 0.02, 0.04999])
def test_the_headroom_floor_holds_whatever_the_config_says(headroom):
    rnd = random.Random(6000)
    loose, floor = graph(headroom=headroom), graph()
    for _ in range(200):
        a, b = st(max_v=rnd.uniform(20, 100)), pn(voc=rnd.uniform(10, 40), tc=random_tc(rnd))
        assert fit_set(ev(loose, a, b)) == fit_set(ev(floor, a, b))


def test_the_same_inputs_give_the_same_edge():
    g = graph(arrays=(3, 3, 6), rules=("cold", "minv", "cur"))
    a, b = st(max_v=60, min_v=12, max_a=15), pn(voc=21.5, vmp=18.4, isc=6, tc=-0.3)
    e1, e2 = ev(g, a, b), ev(g, a, b)
    assert e1.to_row() == e2.to_row() and e1.inputs_hash == e2.inputs_hash


def test_hypothesis_headroom_property():
    hyp = pytest.importorskip("hypothesis")
    st_ = pytest.importorskip("hypothesis.strategies")
    g = graph()

    @hyp.settings(max_examples=300, deadline=None)
    @hyp.given(max_v=st_.floats(12, 300), voc=st_.floats(5, 80),
               tc=st_.one_of(st_.none(), st_.floats(-1.0, 0.3)))
    def prop(max_v, voc, tc):
        e = ev(g, st(max_v=max_v), pn(voc=voc, tc=tc))
        for o in e.options:
            if o.outcome == cg.FITS:
                assert o.series * voc * oracle_factor(tc) <= 0.95 * max_v + 1e-9

    prop()


# ---------------------------------------------------------------------------
# records against the safety floor; ratings with missing inputs; NEC merging
# ---------------------------------------------------------------------------

CONN = {"mc4": {"aliases": [r"\bmc4\b"], "series": True, "parallel": True, "max_a": 30, "max_v": 600},
        "dc8020": {"aliases": [r"\bdc8020\b"], "parallel": True}}


def rated_graph(*, policy="prefer_manufacturer", arrays=(4, 3, 6)):
    rules = {
        "mfr": {"kind": "manufacturer_list", "version": 1, "params": {"on_disagreement": policy}},
        "cold": {"kind": "max_voltage_cold", "version": 1, "a_fields": {"max_v": "max_v"},
                 "b_fields": {"voc": "voc", "tc": "tc"}},
        "minv": {"kind": "min_voltage", "version": 1, "a_fields": {"min_v": "min_v"},
                 "b_fields": {"vmp": "vmp"}},
        "cur": {"kind": "max_current", "version": 1,
                "a_fields": {"max_a": "max_a", "current_limited": "limited"}, "b_fields": {"isc": "isc"}},
        "conn": {"kind": "connector_path", "version": 1, "a_fields": {"connectors": "conn"},
                 "b_fields": {"connectors": "conn"}},
    }
    rs = {"version": 1, "relation": "charges_from", "a_kinds": ["station"], "b_kinds": ["panel"],
          "rules": list(rules), "arrays": {"max_series": arrays[0], "max_parallel": arrays[1],
                                           "max_units": arrays[2]}}
    cfg = cg.resolve_config(file_cfg={}, site_cfg={"connectors": CONN, "rules": rules,
                                                   "rulesets": {"pv": rs}})
    return cg.CompatGraph(cfg, clock=lambda: CLOCK)


SRC = {"source_url": "https://maker.example/x", "source_kind": "manufacturer-page", "source_id": 1}


def adapter(max_a, max_v):
    vals = {"connector_a": {"value": "MC4", **SRC}, "connector_b": {"value": "DC8020", **SRC},
            "max_a": max_a, "max_v": max_v}
    return cg.ModelSpec.from_values(9, "adapter", vals, source=SRC)


def random_pair(rnd):
    s = {"max_v": rnd.uniform(20, 160), "min_v": rnd.uniform(0, 60), "max_a": rnd.uniform(5, 30),
         "limited": rnd.random() < 0.4, "conn": rnd.choice(["MC4", "DC8020"])}
    p = {"voc": rnd.uniform(10, 50), "vmp": rnd.uniform(8, 42), "isc": rnd.uniform(1, 14),
         "tc": random_tc(rnd), "conn": "MC4"}
    if rnd.random() < 0.2:
        p.pop(rnd.choice(["isc", "vmp"]))
    return cg.ModelSpec.from_values(1, "station", s), pn(**p), p


def oracle_safe(e, max_v, voc, tc, adapter_rating=None):
    """No configuration the edge stores as a fit may exceed the headroom
    limit at -40 °C (oracle arithmetic) or a known rating."""
    factor = oracle_factor(tc)
    s = e.config["series"]
    if factor is not None:
        assert s * voc * factor <= 0.95 * max_v + 1e-6, (e.verdict, s, e.flags)
        if "max_series" in e.config:
            assert e.config["max_series"] * voc * factor <= 0.95 * max_v + 1e-6
    for c in e.checks:
        if c.kind == "max_voltage_cold" or c.code.startswith("rating-"):
            assert c.outcome != cg.FAIL, (c.code, e.verdict)


@pytest.mark.parametrize("seed", SEEDS[:12])
def test_no_statement_ever_stores_a_fit_that_fails_a_safety_check(seed):
    rnd = random.Random(7000 + seed)
    for policy in ("prefer_manufacturer", "most_conservative", "unknown"):
        g = rated_graph(policy=policy)
        for _ in range(CASES_PER_SEED // 2):
            s, p, pv = random_pair(rnd)
            cfg = {}
            if rnd.random() < 0.5:
                cfg["series"] = rnd.randint(1, 6)
            if rnd.random() < 0.3:
                cfg["parallel"] = rnd.randint(1, 3)
            if rnd.random() < 0.3:
                cfg["max_series"] = rnd.randint(1, 8)
            rec = cg.FitRecord(a=1, b=2, relation="charges_from",
                               verdict=rnd.choice(["fits", "fits", "fits_in_series", "fits_in_parallel"]),
                               origin=rnd.choice(["manufacturer", "manufacturer", "manual"]), config=cfg,
                               source_url="https://maker.example/list", source_kind="manual-pdf")
            e = g.evaluate_pair(s, p, ruleset="pv", records=[rec], adapters=[adapter(15, 60)])
            assert isinstance(e, cg.Edge)
            if e.verdict in cg.FIT_VERDICTS:
                oracle_safe(e, s.values["max_v"].value, pv["voc"], pv["tc"])
            stated = cfg.get("series", 1), cfg.get("parallel", 1)
            if ("series" in cfg or "parallel" in cfg) and e.verdict in cg.FIT_VERDICTS \
                    and (e.config["series"], e.config["parallel"]) != stated:
                assert "record-config-limited" in e.flags or e.decided_by == cg.DECIDED_COMPUTED


@pytest.mark.parametrize("seed", SEEDS[:12])
def test_removing_isc_never_adds_fits_even_through_a_rated_adapter(seed):
    rnd = random.Random(8000 + seed)
    g = rated_graph()
    # Not vacuous: on an unrated path a documented current limit still fits.
    lim = cg.ModelSpec.from_values(1, "station", {"max_v": 60, "min_v": 12, "limited": True, "conn": "DC8020"})
    assert fit_set(g.evaluate_pair(lim, pn(voc=21.5, vmp=18, conn="DC8020"), ruleset="pv"))
    for _ in range(CASES_PER_SEED):
        s, p, pv = random_pair(rnd)
        pv.setdefault("isc", rnd.uniform(1, 14))
        pv.setdefault("vmp", rnd.uniform(8, 42))
        pv["conn"] = rnd.choice(["MC4", "DC8020"])                     # DC8020 carries no rating
        full = pn(**pv)
        blind = pn(**{k: v for k, v in pv.items() if k != "isc"})
        ad = [adapter(rnd.choice([10, 15, 30]), rnd.choice([60, 100]))]
        with_isc = fit_set(g.evaluate_pair(s, full, ruleset="pv", adapters=ad))
        without = fit_set(g.evaluate_pair(s, blind, ruleset="pv", adapters=ad))
        assert without <= with_isc
        if pv["conn"] == "MC4":                    # an MC4 joint or the adapter: a known rating
            assert not without                     # that nothing without Isc can satisfy


@pytest.mark.parametrize("seed", SEEDS[:8])
def test_config_nec_rows_never_lower_a_factor_and_colder_is_never_smaller(seed):
    rnd = random.Random(9000 + seed)
    for _ in range(CASES_PER_SEED):
        rows = [[rnd.randint(-60, 24), round(rnd.uniform(0.9, 1.4), 3)] for _ in range(rnd.randint(1, 4))]
        table = cg.merged_nec_table(rows)
        previous = 1.0
        for t in range(24, -61, -1):
            f = cg.nec_factor(t, table)
            code = oracle_nec(t)
            if code is not None:
                assert f is not None and f >= code - 1e-12, (rows, t)
            if f is None:
                break
            assert f >= previous - 1e-12, (rows, t)                     # monotone as it gets colder
            if t < -40:
                assert f >= 1.25
            previous = f


# ---------------------------------------------------------------------------
# missing data never enlarges what a statement is stored with
# ---------------------------------------------------------------------------
# With a watts rule the recommendation order prefers the most usable watts,
# which is exactly what used to pull a plain "fits" out to parallel strings
# that the current check never verified.

def watts_graph(arrays=(4, 3, 6)):
    g = rated_graph(arrays=arrays)
    rules = dict(g.cfg["rules"])
    rules["watts"] = {"kind": "watts_clip", "version": 1, "a_fields": {"max_w": "max_w"},
                      "b_fields": {"rated_w": "rated_w"}}
    rs = dict(g.cfg["rulesets"]["pv"], rules=list(g.cfg["rulesets"]["pv"]["rules"]) + ["watts"])
    cfg = cg.resolve_config(file_cfg={}, site_cfg={"connectors": CONN, "rules": rules,
                                                   "rulesets": {"pv": rs}})
    return cg.CompatGraph(cfg, clock=lambda: CLOCK)


def random_watts_pair(rnd):
    s = {"max_v": rnd.uniform(20, 160), "min_v": rnd.uniform(0, 30), "max_a": rnd.uniform(5, 40),
         "max_w": rnd.uniform(150, 1200), "conn": rnd.choice(["MC4", "MC4", "DC8020"])}
    p = {"voc": rnd.uniform(10, 50), "vmp": rnd.uniform(8, 42), "isc": rnd.uniform(1, 14),
         "rated_w": rnd.uniform(50, 400), "tc": random_tc(rnd), "conn": "MC4"}
    return s, p


def plain_record(verdict, **kw):
    return cg.FitRecord(a=1, b=2, relation="charges_from", verdict=verdict,
                        source_url="https://maker.example/list", source_kind="manual-pdf", **kw)


def base_parallel(verdict):
    return 2 if verdict == "fits_in_parallel" else 1


def evaluate(g, s, p, rec):
    e = g.evaluate_pair(cg.ModelSpec.from_values(1, "station", s, source=SRC),
                        cg.ModelSpec.from_values(2, "panel", {k: v for k, v in p.items() if v is not None},
                                                 source=SRC),
                        ruleset="pv", records=[rec], adapters=[adapter(15, 60)])
    assert isinstance(e, cg.Edge)
    return e


@pytest.mark.parametrize("drop", ["max_a", "isc"])
@pytest.mark.parametrize("seed", SEEDS[:12])
def test_removing_the_input_current_or_isc_never_enlarges_a_stated_fit(seed, drop):
    rnd = random.Random(10000 + seed + (0 if drop == "max_a" else 500))
    g = watts_graph()
    tempting = 0
    for _ in range(CASES_PER_SEED):
        s, p = random_watts_pair(rnd)
        rec = plain_record(rnd.choice(["fits", "fits", "fits_in_series", "fits_in_parallel"]))
        known = evaluate(g, s, p, rec)
        blind = evaluate(g, {k: v for k, v in s.items() if k != drop},
                         {k: v for k, v in p.items() if k != drop}, rec)
        base = base_parallel(rec.verdict)
        if blind.verdict not in cg.FIT_VERDICTS:
            continue
        # The statement names one string (two for fits_in_parallel); without
        # the current nothing verifies more, so nothing more is stored.
        assert blind.config["parallel"] == base, (s, p, rec.verdict, blind.config)
        assert blind.config.get("max_parallel", 0) <= base
        if known.verdict in cg.FIT_VERDICTS:
            assert blind.config["parallel"] <= known.config["parallel"]
            assert blind.config.get("max_parallel", 0) <= max(known.config.get("max_parallel", 0), base)
        # ... and it says so next to the verdict.
        assert "computed-check-incomplete" in blind.flags
        assert any(a.code == "not-checked" and a.text.startswith("Input current not checked")
                   for a in blind.assumptions)
        tempting += any(o.parallel > base and o.outcome == cg.UNKNOWN for o in blind.options)
    assert tempting >= 5            # not vacuous: unchecked bigger arrays were on offer


@pytest.mark.parametrize("seed", SEEDS[:8])
def test_removing_the_minimum_voltage_never_lowers_a_stated_series(seed):
    rnd = random.Random(11000 + seed)
    g = watts_graph()
    fits = 0
    for _ in range(CASES_PER_SEED):
        s, p = random_watts_pair(rnd)
        s["min_v"] = rnd.uniform(20, 90)
        series = rnd.randint(2, 4)
        rec = plain_record("fits_in_series", config={"series": series})
        blind = evaluate(g, {k: v for k, v in s.items() if k != "min_v"}, p, rec)
        if blind.verdict not in cg.FIT_VERDICTS:
            continue
        fits += 1
        assert blind.config["series"] == series, (s, p, blind.config)
        assert blind.config.get("min_series", series) >= series
        assert "computed-check-incomplete" in blind.flags
    assert fits >= 5


@pytest.mark.parametrize("seed", SEEDS[:8])
def test_without_the_station_connector_the_panels_rating_still_caps_every_stored_fit(seed):
    rnd = random.Random(12000 + seed)
    g = watts_graph()
    capped = 0
    for _ in range(CASES_PER_SEED):
        s, p = random_watts_pair(rnd)
        s.pop("conn")
        p["isc"] = rnd.uniform(8, 14)                    # two or three strings may pass 30 A
        cfg = {"parallel": rnd.choice([2, 3])} if rnd.random() < 0.5 else {}
        rec = plain_record(rnd.choice(["fits", "fits_in_parallel"]), config=cfg)
        e = evaluate(g, s, p, rec)
        if e.verdict not in cg.FIT_VERDICTS:
            continue
        assert e.config["design_current_a"] <= 30 + 1e-9, (s, p, cfg, e.config)   # the panel's MC4
        assert any(a.code == "not-checked" and a.text.startswith("Connector not checked")
                   for a in e.assumptions)
        assert "computed-check-incomplete" in e.flags
        if not cfg:
            assert e.config["parallel"] == base_parallel(rec.verdict)
        capped += "record-config-limited" in e.flags
    assert capped >= 1              # a stated count was cut to the panel's rating
