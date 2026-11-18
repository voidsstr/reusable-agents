"""framework.core.compat_graph on the fixture catalog, through spec_sources.

Every fixture model is a list of spec_sources candidates (with the verbatim
snippet each number came from); the tests consolidate them with
framework.core.spec_sources and build the graph with the repo config's
home-power profile, exactly as a spec-extractor agent would:

* a fitting pair (published tc, direct DC8020 connection);
* a cold-voltage failure (58.19 V at -40 °C: under 60 V but inside the 5 %
  headroom band; only 49.5 V at 25 °C);
* an unknown-tc pair (NEC x1.25), and the real EcoFlow 220 W panel that
  publishes no coefficient;
* an adapter path (one manufacturer-documented MC4-to-DC8020 adapter);
* the real Pecron manual allowing four 18 V panels in series, which the
  conservative rule rejects at 107.5 V;
* a manufacturer expansion statement and an excluded L14-30 cord.

Plus an end-to-end run from the real Jackery FAQ document (spec_sources
extraction -> SpecSet -> ModelSpec -> edge) and the cross-source conflict
case that must block the verdict. Offline."""
from __future__ import annotations

import html
import json
import re
from pathlib import Path

import pytest

from framework.core import compat_graph as cg
from framework.core import spec_sources as ss

ROOT = Path(__file__).resolve().parents[2]
FIX = Path(__file__).resolve().parent / "fixtures" / "compat_graph"
SS_FIX = Path(__file__).resolve().parent / "fixtures" / "spec_sources"
CLOCK = "2026-11-17T00:00:00Z"


@pytest.fixture(scope="module")
def catalog():
    return json.loads((FIX / "catalog.json").read_text())


@pytest.fixture(scope="module")
def repo_cfg():
    return json.loads((ROOT / "config" / "compat-graph-config.json").read_text())


def home_power(repo_cfg):
    cfg = cg.resolve_config(site_cfg={"profile": "home-power"}, file_cfg=repo_cfg)
    g = cg.CompatGraph(cfg, clock=lambda: CLOCK)
    assert g.problems == []
    return g


def source_ref(cat, sid):
    s = cat["sources"][sid]
    return ss.SourceRef(s["url"], s["kind"], s["publisher"], s["fetched_at"], s["content_sha256"])


def candidate(cat, c):
    ref = source_ref(cat, c["source"])
    if "bool" in c:
        return ss.Candidate(field=c["field"], value_num=1.0 if c["bool"] else 0.0,
                            value_text="true" if c["bool"] else "false", unit=None, source=ref,
                            extractor_id="fixture", snippet=c["snippet"])
    if "text" in c:
        return ss.Candidate(field=c["field"], value_num=None, value_text=c["text"], unit=None,
                            source=ref, extractor_id="fixture", snippet=c["snippet"])
    return ss.Candidate(field=c["field"], value_num=float(c["value"]), value_text=None,
                        unit=c["unit"], source=ref, extractor_id="fixture", snippet=c["snippet"])


def nodes_and_records(cat):
    fields = ss.FieldRegistry(cat["fields"])
    assert fields.problems == []
    source_ids = {s["url"]: s["source_id"] for s in cat["sources"].values()}
    nodes = []
    for m in cat["models"]:
        spec = ss.consolidate(str(m["model_id"]), [candidate(cat, c) for c in m["candidates"]], fields)
        assert spec.conflicts == [], (m["model_id"], spec.conflicts)
        nodes.append(cg.ModelSpec.from_specset(m["model_id"], m["kind"], spec, name=m["name"],
                                               brand=m["brand"], aliases=m.get("aliases", ()),
                                               source_ids=source_ids))
    records = []
    for r in cat["fit_records"]:
        s = cat["sources"][r["source"]]
        records.append(cg.FitRecord(a=r["a"], b=r["b"], relation=r["relation"], verdict=r["verdict"],
                                    source_url=s["url"], source_kind=s["kind"], publisher=s["publisher"],
                                    fetched_at=s["fetched_at"], content_sha256=s["content_sha256"],
                                    source_id=s["source_id"], statement=r["statement"]))
    return nodes, records


@pytest.fixture(scope="module")
def built(catalog, repo_cfg):
    nodes, records = nodes_and_records(catalog)
    g = home_power(repo_cfg)
    res = g.build(nodes, records)
    edges = {(e.a_model_id, e.b_model_id, e.rule_id): e for e in res.edges}
    skips = {(s.a_model_id, s.b_model_id, s.rule_id): s for s in res.skipped}
    return g, res, edges, skips


# ---------------------------------------------------------------------------
# provenance of the real fixture numbers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("source,doc", [
    ("jackery-faq", "docs/jackery-explorer-1000-v2-faq.html"),
    ("pecron-manual", "docs/pecron-e2000lfp-manual-eu.layout.txt"),
    ("fossibot-page", "docs/fossibot-f2400-product-page.html"),
])
def test_real_snippets_appear_verbatim_in_the_spec_sources_documents(catalog, source, doc):
    raw = (SS_FIX / doc).read_text(encoding="utf-8")
    if doc.endswith(".html"):
        raw = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    text = " ".join(ss.normalize_text(raw).split())
    snippets = [c["snippet"] for m in catalog["models"] for c in m["candidates"] if c["source"] == source]
    assert snippets
    for snip in snippets:
        assert " ".join(ss.normalize_text(snip).split()) in text, snip


def test_sources_file_lists_every_real_and_synthetic_source(catalog):
    meta = json.loads((FIX / "SOURCES.json").read_text())
    real = {r["source"] for r in meta["real"]}
    assert real == {k for k, v in catalog["sources"].items() if ".example/" not in v["url"]}
    assert all(".example/" in v["url"] for k, v in catalog["sources"].items() if k not in real)


# ---------------------------------------------------------------------------
# the four required fixtures, and the real-data extras
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("case", [
    "fitting-pair", "cold-voltage-failure", "unknown-tc-pair", "adapter-path", "unknown-tc-real-panel",
])
def test_solar_fixture_cases(catalog, built, case):
    _g, _res, edges, _ = built
    exp = catalog["cases"][case]
    e = edges[(exp["a"], exp["b"], exp["ruleset"])]
    assert e.verdict == exp["verdict"]
    assert e.via_model_id == exp["via"]
    assert e.config["factor_method"] == exp["factor_method"]
    assert e.config["voltage_factor"] == pytest.approx(exp["voltage_factor"], abs=1e-4)
    assert e.config["cold_voc_v"] == pytest.approx(exp["cold_voc_v"], abs=1e-3)
    assert (e.config["series"], e.config["parallel"]) == (exp["series"], exp["parallel"])
    if "max_series" in exp:
        assert e.config["max_series"] == exp["max_series"]
    if "code" in exp:
        assert exp["code"] in {c.code for c in e.checks}
    assert e.config["design_temp_c"] == -40.0
    assert e.rule_version == "solar-charging@1[mfr-list@1,pv-cold-voc@2,pv-min-voltage@1," \
                             "pv-max-current@1,pv-connector@1,pv-watts@1]e" + cg.ENGINE_VERSION
    texts = [a.text for a in e.assumptions]
    assert any("-40 °C" in t for t in texts)
    assert any("5% headroom" in t and "60 V maximum solar input voltage" in t for t in texts)
    assert e.store is True


def test_fitting_pair_details(built):
    e = built[2][(1, 12, "solar-charging")]
    assert {a.code for a in e.assumptions} >= {"voc-tc", "current-limited", "parallel-connector"}
    assert e.config["wiring"] == {"series": False, "parallel": True}      # DC8020 panels: parallel only
    assert e.evidence_source_ids == [101, 212]
    assert e.confidence == cg.CONF_COMPUTED


def test_cold_voltage_failure_would_pass_at_25c(built):
    e = built[2][(1, 13, "solar-charging")]
    (cold,) = [c for c in e.checks if c.rule_id == "pv-cold-voc"]
    assert cold.values["voc_v"] == 49.5 < cold.values["max_v"] == 60.0
    assert cold.values["cold_voc_v"] > cold.values["allowed_v"] == pytest.approx(57.0)
    assert e.via_model_id is None and "adapter-endorsed" not in e.flags


def test_unknown_tc_assumption_is_spelled_out(built):
    e = built[2][(1, 14, "solar-charging")]
    texts = " ".join(a.text for a in e.assumptions)
    assert "publishes no temperature coefficient" in texts and "x1.25" in texts
    assert "crystalline silicon assumed" in texts


def test_adapter_path_uses_one_manufacturer_adapter(built):
    e = built[2][(1, 15, "solar-charging")]
    assert e.config["connector_path"]["code"] == "via-adapter"
    assert "adapter-endorsed" in e.flags
    via_ev = [x for x in e.evidence if x.role == "via"]
    assert {x.field for x in via_ev} == {"connector_a", "connector_b"}
    assert all(x.source_id == 221 for x in via_ev)
    assert e.evidence_source_ids == [101, 215, 221]
    assert "Example MC4 to DC8020 adapter" in " ".join(a.text for a in e.assumptions)


def test_real_panel_unknown_tc_and_no_parallel_strings_through_the_adapter(built):
    e = built[2][(1, 11, "solar-charging")]
    opts = {(o.series, o.parallel): o for o in e.options}
    assert opts[(2, 1)].outcome == cg.FITS and opts[(2, 1)].cold_voc_v == pytest.approx(53.75)
    assert opts[(3, 1)].outcome == cg.OVER_LIMIT
    # Two parallel strings would need a branch connector AND the adapter:
    # two extra parts, so none is offered (and 24.8 A would exceed its 15 A).
    assert all(p == 1 for _s, p in opts)
    assert e.config["wiring"]["parallel_blocked_by"] == "adapter"


def test_the_real_pecron_four_in_series_statement_is_stored_as_three(catalog, repo_cfg):
    # A record carrying the manual's limit ("four 18V panels in series")
    # for the real 18 V-class EcoFlow panel: 107.5 V at -40 °C on a 95 V
    # input is never stored; the best string that passes is (3 x 220 W, the
    # 600 W input full), with the reason next to it.
    nodes, _ = nodes_and_records(catalog)
    src = catalog["sources"]["pecron-manual"]
    rec = cg.FitRecord(a=2, b=11, relation="charges_from", verdict="fits_in_series", config={"series": 4},
                       source_url=src["url"], source_kind=src["kind"], publisher=src["publisher"],
                       source_id=src["source_id"], statement="up to four 18V solar panels in series")
    g = home_power(repo_cfg)
    pair = [n for n in nodes if n.model_id in (2, 11)]
    e = g.evaluate_pair(*pair, ruleset="solar-charging", records=[rec])
    assert (e.verdict, e.decided_by, e.store) == (cg.FITS_IN_SERIES, cg.DECIDED_MANUFACTURER, True)
    assert (e.config["series"], e.config["parallel"]) == (3, 1)
    assert e.config["cold_voc_v"] == pytest.approx(80.625) and e.config["usable_w"] == 600
    assert {"computed-check-disagrees", "record-config-limited"} <= set(e.flags)
    text = " ".join(a.text for a in e.assumptions)
    assert "4 in series" in text and "107.5 V" in text and "3 in series is shown instead" in text
    assert 102 in e.evidence_source_ids


def _maker_record(a, b, **kw):
    return cg.FitRecord(a=a, b=b, relation="charges_from", source_url="https://maker.example/compat",
                        source_kind="manufacturer-page", publisher="maker.example", **kw)


def test_a_plain_maker_fit_on_the_real_fossibot_never_recommends_unchecked_strings(catalog, repo_cfg):
    # The real FOSSiBOT F2400 page publishes no maximum solar input current
    # and no solar connector. With a plain "fits" for the real EcoFlow 220 W
    # panel the edge used to recommend 3 strings in parallel (37.2 A) with
    # max_parallel 4 and no caution; it is one panel, and says why.
    nodes, _ = nodes_and_records(catalog)
    by = {n.model_id: n for n in nodes}
    g = home_power(repo_cfg)
    assert g.evaluate_pair(by[3], by[11], ruleset="solar-charging").verdict == cg.UNKNOWN
    e = g.evaluate_pair(by[3], by[11], ruleset="solar-charging", records=[_maker_record(3, 11)])
    assert (e.verdict, e.confidence, e.store) == (cg.FITS, cg.CONF_MANUFACTURER, True)
    assert (e.config["series"], e.config["parallel"], e.config["design_current_a"]) == (1, 1, 12.4)
    assert (e.config["max_parallel"], e.config["max_units"]) == (1, 1)
    assert e.flags == ("computed-check-incomplete",)
    assert [a.text for a in e.assumptions if a.code == "not-checked"] == [
        "Input current not checked: the station's maximum solar input current is not published.",
        "Connector not checked: the station's solar input connector is not published."]
    assert "parallel-connector" not in {a.code for a in e.assumptions}
    assert e.to_row()["config"]["flags"] == ["computed-check-incomplete"]


def test_a_stated_parallel_count_on_the_real_fossibot_still_meets_the_panels_mc4_rating(catalog, repo_cfg):
    # The station's connector is unknown, but every string ends in the
    # panel's own MC4 (30 A): three strings (37.2 A) are cut to two.
    nodes, _ = nodes_and_records(catalog)
    by = {n.model_id: n for n in nodes}
    e = home_power(repo_cfg).evaluate_pair(by[3], by[11], ruleset="solar-charging",
                                           records=[_maker_record(3, 11, config={"parallel": 3})])
    assert (e.verdict, e.config["series"], e.config["parallel"]) == (cg.FITS, 1, 2)
    assert {"computed-check-disagrees", "record-config-limited", "computed-check-incomplete"} <= set(e.flags)
    text = next(a.text for a in e.assumptions if a.code == "record-config-limited")
    assert "3 in parallel" in text and "37.2 A exceeds the 30 A connector rating" in text


@pytest.mark.parametrize("pv_a_max,shown,max_parallel", [
    (None, (1, 1), 1), (13, (1, 1), 1), (30, (1, 2), 2)])
def test_home_power_fit_without_a_published_input_current_keeps_one_string(repo_cfg, pv_a_max, shown,
                                                                           max_parallel):
    # The round-2 review case: 30 V / 400 W MC4 input, 200 W 12 A panel.
    src = {"source_url": "https://maker.example/s", "source_kind": "manufacturer-page", "source_id": 1}
    st = cg.ModelSpec.from_values(1, "station", {
        "pv_voc_max": 30, "pv_v_min": 12, "pv_w_max": 400, "pv_input_connectors": "MC4",
        **({} if pv_a_max is None else {"pv_a_max": pv_a_max})}, source=src)
    pn = cg.ModelSpec.from_values(2, "panel", {"voc": 21.5, "vmp": 18, "isc": 12, "rated_w": 200,
                                               "temp_coeff_voc_pct_c": -0.3, "connector": "MC4"}, source=src)
    e = home_power(repo_cfg).evaluate_pair(st, pn, ruleset="solar-charging", records=[_maker_record(1, 2)])
    assert e.verdict == cg.FITS
    assert (e.config["series"], e.config["parallel"], e.config["max_parallel"]) == (*shown, max_parallel)
    unchecked = [a.text for a in e.assumptions if a.code == "not-checked"]
    if pv_a_max is None:
        assert unchecked == ["Input current not checked: the station's maximum solar input current "
                             "is not published."]
        assert "computed-check-incomplete" in e.flags
    else:
        assert unchecked == [] and "computed-check-incomplete" not in e.flags


@pytest.mark.parametrize("case", ["real-two-in-series-over-at-minus-40", "real-manual-allows-four-in-series"])
def test_real_station_cases_stay_unknown_but_record_the_cold_failure(catalog, built, case):
    exp = catalog["cases"][case]
    e = built[2][(exp["a"], exp["b"], exp["ruleset"])]
    assert e.verdict == exp["verdict"] and e.store is False
    opt = next(o for o in e.options if [o.series, o.parallel] == exp["option"])
    assert opt.outcome == cg.OVER_LIMIT
    assert exp["option_code"] in opt.to_dict()["codes"]
    assert opt.cold_voc_v == pytest.approx(exp["option_cold_voc_v"])


def test_pecron_manual_limit_versus_the_conservative_rule(built):
    # The manual: "Do not wire ... more than four 18V solar panels in series"
    # (Voc must stay under 95 V). At 25 °C four of these panels are 86 V; at
    # -40 °C with the NEC factor they are 107.5 V.
    e = built[2][(2, 11, "solar-charging")]
    four = next(o for o in e.options if (o.series, o.parallel) == (4, 1))
    assert 4 * 21.5 < 95 < four.cold_voc_v
    three = next(o for o in e.options if (o.series, o.parallel) == (3, 1))
    assert "cold-voc-exceeds-max" not in three.to_dict().get("codes", [])


def test_expansion_statement_and_excluded_cord(catalog, built):
    _g, _res, edges, skips = built
    exp = catalog["cases"]["expansion-listed"]
    e = edges[(exp["a"], exp["b"], exp["ruleset"])]
    assert (e.verdict, e.confidence) == (exp["verdict"], exp["confidence"])
    assert e.evidence_source_ids == [105]
    assert "Listed as compatible by the manufacturer (jackery.com)." in [a.text for a in e.assumptions]
    cord = catalog["cases"]["excluded-cord"]
    assert skips[(cord["a"], cord["b"], cord["ruleset"])].reason == cord["skipped"]
    assert (cord["a"], cord["b"], cord["ruleset"]) not in edges


def test_only_decisive_solar_verdicts_are_stored(built):
    _g, res, _e, _s = built
    stored = res.stored()
    assert {e.verdict for e in stored if e.rule_id == "solar-charging"} <= {
        "fits", "fits_with_adapter", "fits_in_series", "fits_in_parallel", "over_limit"}
    assert all(e.verdict != cg.UNKNOWN for e in stored)
    for e in stored:
        if e.rule_id == "solar-charging":
            codes = {a.code for a in e.assumptions}
            assert {"design-temp-default", "headroom", "per-input"} <= codes, e.key


def test_rebuilding_the_catalog_plans_no_writes(catalog, built):
    g, res, _e, _s = built
    nodes, records = nodes_and_records(catalog)
    again = g.build(nodes, records)
    plan = g.plan(again, [e.to_row() for e in res.stored()])
    assert plan.upserts == [] and plan.deletes == [] and len(plan.unchanged) == len(res.stored())


def test_from_db_rows_matches_from_specset(catalog, repo_cfg):
    fields = ss.FieldRegistry(catalog["fields"])
    m = next(x for x in catalog["models"] if x["model_id"] == 1)
    spec = ss.consolidate("1", [candidate(catalog, c) for c in m["candidates"]], fields)
    ids = {s["url"]: s["source_id"] for s in catalog["sources"].values()}
    a = cg.ModelSpec.from_specset(1, "station", spec, source_ids=ids)
    rows = ss.to_db_rows(spec, model_id=1)["spec_values"]
    b = cg.ModelSpec.from_db_rows(1, "station", rows, source_ids=ids)
    assert {k: (v.value, v.source_id) for k, v in a.values.items()} == \
           {k: (v.value, v.source_id) for k, v in b.values.items()}
    assert b.values["pv_current_limited"].value is True


def test_from_db_rows_drops_conflicting_and_non_accepted_rows():
    rows = [{"field": "voc", "value_num": 21.5, "unit": "V", "status": "accepted", "source_id": 1},
            {"field": "voc", "value_num": 22.0, "unit": "V", "status": "accepted", "source_id": 2},
            {"field": "vmp", "value_num": 18.0, "unit": "V", "status": "conflict"},
            {"field": "isc", "value_num": 5.0, "unit": "A", "status": "accepted", "source_id": 3}]
    m = cg.ModelSpec.from_db_rows(2, "panel", rows, sources={3: {"url": "https://p.example/x",
                                                                 "kind": "manual-pdf"}})
    assert set(m.values) == {"isc"}
    assert (m.values["isc"].source_url, m.values["isc"].source_kind) == ("https://p.example/x", "manual-pdf")


def test_from_db_rows_ignores_rows_without_a_status_unless_told_they_are_accepted(caplog):
    # A caller query that forgot the status column must not let conflict or
    # rejected values decide edges.
    rows = [{"field": "pv_voc_max", "value_num": 150.0, "unit": "V", "source_id": 3},
            {"field": "pv_v_min", "value_num": 12.0, "unit": "V", "status": "", "source_id": 3},
            {"field": "pv_a_max", "value_num": 10.0, "unit": "A", "status": "accepted", "source_id": 3}]
    assert set(cg.ModelSpec.from_db_rows(1, "station", rows).values) == {"pv_a_max"}
    assert "2 spec_values rows without a status ignored" in caplog.text
    trusted = cg.ModelSpec.from_db_rows(1, "station", rows, assume_accepted=True)
    assert set(trusted.values) == {"pv_voc_max", "pv_v_min", "pv_a_max"}
    rejected = [dict(rows[0], status="rejected")]
    assert cg.ModelSpec.from_db_rows(1, "station", rejected, assume_accepted=True).values == {}


# ---------------------------------------------------------------------------
# end to end from a real manufacturer document (spec_sources extraction)
# ---------------------------------------------------------------------------

def _f9_station(docs):
    """Extract the Jackery Explorer 1000 v2 with spec_sources' own home-power
    test profile, as the spec-extractor agent would."""
    prof = json.loads((SS_FIX / "profiles" / "home-power.json").read_text())
    s = ss.SpecSources(ss.resolve_config(site_cfg={"profile": "home-power"},
                                         file_cfg={"profiles": {"home-power": prof}}))
    sources = {d["id"]: d for d in json.loads((SS_FIX / "SOURCES.json").read_text())["documents"]}
    cands = []
    for doc_id in docs:
        d = sources[doc_id]
        html = (SS_FIX / d["file"]).read_text(encoding="utf-8")
        doc = ss.document_from_html(html, url=d["url"], kind=d["kind"], fetched_at=CLOCK)
        cands += s.extract(doc, brand=d["brand"]).candidates
    return s.consolidate("jackery-explorer-1000-v2", cands)


def test_end_to_end_from_the_real_jackery_faq(catalog, repo_cfg):
    spec = _f9_station(["jackery-faq"])
    vals = spec.values()
    assert vals["pv_voc_max"] == 60.0 and vals["pv_v_min"] == 16.0
    station = cg.ModelSpec.from_specset(
        1, "station", spec, name="Explorer 1000 v2", brand="Jackery",
        extra={"pv_input_connectors": {"value": "DC8020", "source_url": "https://www.jackery.com/blogs/"
                                       "jackery-faqs/explorer-1000-v2-basic-information",
                                       "source_kind": "manufacturer-faq"},
               "pv_current_limited": {"value": True, "source_url": "https://www.jackery.com/blogs/"
                                      "jackery-faqs/explorer-1000-v2-basic-information",
                                      "source_kind": "manufacturer-faq"}})
    nodes, _ = nodes_and_records(catalog)
    panel_14 = next(n for n in nodes if n.model_id == 14)
    g = home_power(repo_cfg)
    e = g.evaluate_pair(station, panel_14, ruleset="solar-charging")
    assert isinstance(e, cg.Edge) and e.verdict == cg.FITS
    urls = {x.source_url for x in e.evidence if x.role == "a"}
    assert urls == {"https://www.jackery.com/blogs/jackery-faqs/explorer-1000-v2-basic-information"}


def test_a_cross_source_spec_conflict_blocks_the_verdict(catalog, repo_cfg):
    # The product page reads the DC input as 12-60 V (car + solar bands), the
    # FAQ says 16-60 V for solar: a pv_v_min conflict until adjudicated.
    spec = _f9_station(["jackery-product-page", "jackery-faq"])
    assert "pv_v_min" in {c.field for c in spec.conflicts}
    station = cg.ModelSpec.from_specset(1, "station", spec,
                                        extra={"pv_input_connectors": "DC8020", "pv_current_limited": True})
    assert "pv_v_min" not in station.values
    nodes, _ = nodes_and_records(catalog)
    g = home_power(repo_cfg)
    e = g.evaluate_pair(station, next(n for n in nodes if n.model_id == 14), ruleset="solar-charging")
    assert e.verdict == cg.UNKNOWN and not e.store
    assert "missing:pv_v_min" in {c.code for c in e.checks}


# ---------------------------------------------------------------------------
# robots fallback: model-keyed consumable fitment with the repo profile
# ---------------------------------------------------------------------------

def test_robot_consumable_fitment_with_the_repo_profile(catalog, repo_cfg):
    rob = catalog["robots"]
    g = cg.CompatGraph(cg.resolve_config(site_cfg={"profile": "robot-consumables"}, file_cfg=repo_cfg),
                       clock=lambda: CLOCK)
    assert g.problems == []
    models = [cg.ModelSpec.from_values(m["model_id"], m["kind"], {}, name=m["name"], brand=m["brand"])
              for m in rob["models"]]
    src = catalog["sources"][rob["list"]["source"]]
    robots_only = [m for m in models if m.kind == "robot"]
    recs, bad = cg.fit_records_from_list(rob["list"]["subject"], rob["list"]["text"],
                                         index=cg.ModelIndex(robots_only), relation="consumable_for",
                                         source_url=src["url"], source_kind=src["kind"],
                                         publisher=src["publisher"], source_id=src["source_id"])
    assert [b.name for b in bad] == rob["expect_unresolved"]
    res = g.build(models, recs)
    assert sorted(e.a_model_id for e in res.stored()) == rob["expect_fits"]
    assert all(e.relation == "consumable_for" and e.confidence == cg.CONF_MANUFACTURER for e in res.edges)
