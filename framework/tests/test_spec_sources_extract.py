"""framework.core.spec_sources: deterministic extraction end to end on real
manufacturer documents, driven only by configuration.

The spec's three examples are here: the Pecron E2000LFP manual, the
Jackery Explorer 1000 v2 PV-input conflict (12-60 V read from the product
page's DC Input row vs 16-60 V on the FAQ), and the FOSSiBOT F2400 UPS
switchover (10 ms on the product page vs about 8 ms in the manual). A robot
vacuum page from another niche runs through the same code with a different
profile."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from framework.core import spec_sources as ss

FX = Path(__file__).resolve().parent / "fixtures" / "spec_sources"
SOURCES = {d["id"]: d for d in json.loads((FX / "SOURCES.json").read_text())["documents"]}
PROFILES = {p.stem: json.loads(p.read_text()) for p in (FX / "profiles").glob("*.json")}


def cfg(profile: str, **site_cfg) -> dict:
    return ss.resolve_config(file_cfg={"profiles": PROFILES}, site_cfg=dict({"profile": profile}, **site_cfg))


def load(doc_id: str) -> ss.SpecDocument:
    s = SOURCES[doc_id]
    raw = (FX / s["file"]).read_text()
    if s["format"] == "html":
        return ss.document_from_html(raw, url=s["url"], kind=s["kind"], fetched_at=s["fetched_on"])
    return ss.document_from_text(raw, url=s["url"], kind=s["kind"], content_type="application/pdf",
                                 fetched_at=s["fetched_on"])


def run(engine: ss.SpecSources, model_key: str, *doc_ids: str, decisions=()) -> ss.SpecSet:
    cands = []
    for d in doc_ids:
        res = engine.extract(load(d), brand=SOURCES[d]["brand"])
        assert res.errors == [], res.errors
        cands += res.candidates
    return engine.consolidate(model_key, cands, decisions=decisions)


@pytest.fixture(scope="module")
def power():
    engine = ss.SpecSources(cfg("home-power"))
    assert engine.problems == []
    return engine


# ---------------------------------------------------------------------------
# Pecron E2000LFP manual (PDF layout text, brand-specific extractors)
# ---------------------------------------------------------------------------

def test_pecron_e2000lfp_manual(power):
    spec = run(power, "pecron-e2000lfp", "pecron-manual")
    v = spec.values()
    assert v["capacity_wh"] == 1920.0
    assert v["chemistry"] == "LiFePO4"
    assert v["cycle_life"] == 3500
    assert v["ac_cont_w"] == 2000.0
    assert v["ups_supported"] is False                 # "The UPS function is not supported."
    assert "ups_ms" not in v
    assert (v["pv_v_min"], v["pv_voc_max"]) == (32.0, 95.0)   # main GX16MF input, not the 12-18 V DC5521 port
    assert v["pv_w_max"] == 1200.0
    assert v["weight_kg"] == pytest.approx(22.0, abs=0.01)
    assert (v["charge_temp_min_c"], v["discharge_temp_min_c"]) == (0.0, -20.0)
    assert v["dimensions_mm"] == pytest.approx([408.94, 254.0, 294.64])
    assert spec.conflicts == [] and spec.completeness == 1.0


def test_pecron_provenance_rows(power):
    spec = run(power, "pecron-e2000lfp", "pecron-manual")
    rows = spec.provenance()
    assert rows and {r.source_url for r in rows} == {SOURCES["pecron-manual"]["url"]}
    for r in rows:
        assert r.source_kind == "manual-pdf" and r.fetched_at == "2026-11-17"
        assert len(r.content_sha256) == 64 and r.extractor == "deterministic"
        assert r.extractor_id and 0 < r.confidence <= 1 and r.snippet
        assert r.status in ("accepted", "superseded", "rejected", "conflict")
    acc = {r.field: r for r in rows if r.status == "accepted"}
    assert set(acc) == set(spec.values())
    assert acc["pv_voc_max"].extractor_id == "brand:pecron/manual-main-solar-input"
    assert "less than 95V" in acc["pv_voc_max"].snippet
    assert acc["capacity_wh"].snippet.startswith("Capacity: 1920Wh")


# ---------------------------------------------------------------------------
# Jackery Explorer 1000 v2: the PV-input conflict
# ---------------------------------------------------------------------------

def test_jackery_pv_input_conflict(power):
    spec = run(power, "jackery-explorer-1000-v2", "jackery-product-page", "jackery-faq")
    pv = spec.fields["pv_v_min"]
    assert pv.status == "conflict" and pv.accepted is None
    c = pv.conflict
    assert c.kind == "cross-source" and c.field == "pv_v_min"
    assert sorted((x.value_num, x.source.kind) for x in c.candidates) == [
        (12.0, "manufacturer-page"), (16.0, "manufacturer-faq")]
    assert spec.conflicts == [c]
    # the upper bound agrees across both sources and is accepted
    voc = spec.fields["pv_voc_max"]
    assert voc.status == "accepted" and voc.accepted.value_num == 60.0 and voc.resolution == "agreement"
    assert voc.accepted.source.kind == "manufacturer-page"     # higher source-kind priority than the FAQ
    v = spec.values()
    assert v["capacity_wh"] == 1070.0 and v["model_number"] == "JE-1000D"
    assert (v["ac_cont_w"], v["ac_surge_w"]) == (1500.0, 3000.0)
    assert v["ups_ms"] == 20.0 and v["ups_supported"] is True
    assert v["weight_kg"] == pytest.approx(10.7955)            # 23.8 lb from the page, 10.8 kg on the FAQ agree
    assert (v["charge_temp_min_c"], v["discharge_temp_min_c"]) == (0.0, -10.0)
    assert v["fan_noise_db"] == 22.0
    # required fields all accepted, the conflict is on an optional field
    assert spec.completeness == 1.0
    statuses = {(r.source_url.rsplit("/", 1)[-1], r.status) for r in spec.provenance() if r.field == "pv_v_min"}
    assert statuses == {("jackery-explorer-1000-v2", "conflict"), ("explorer-1000-v2-basic-information", "conflict")}


def test_conflict_id_is_stable_across_runs(power):
    a = run(power, "jackery-explorer-1000-v2", "jackery-product-page", "jackery-faq")
    b = run(power, "jackery-explorer-1000-v2", "jackery-faq", "jackery-product-page")
    assert a.conflicts[0].conflict_id == b.conflicts[0].conflict_id


def test_wrong_variant_manual_surfaces_as_conflicts(power):
    # The v2 product page links an EU/UK manual for a different variant (JE-1000F).
    spec = run(power, "jackery-explorer-1000-v2", "jackery-product-page", "jackery-faq", "jackery-eu-manual")
    fields = {c.field for c in spec.conflicts}
    assert {"model_number", "capacity_wh", "ac_voltage", "ups_ms"} <= fields
    cap = spec.fields["capacity_wh"].conflict
    assert sorted({x.value_num for x in cap.candidates}) == [1024.0, 1070.0]
    assert "capacity_wh" in spec.missing_required


def test_scope_keeps_the_bundled_panel_out(power):
    # The product page lists the bundled panel's own "Weight" after the station.
    engine = ss.SpecSources(cfg("home-power", brands={"jackery": {"use": [
        {"set": "station-spec-table", "when": {"kinds": ["manufacturer-page"]}, "scope": {"end": "^SolarSaga\\b"},
         "occurrence": "all"}]}}))
    weights = [c.value_num for c in engine.extract(load("jackery-product-page"), brand="jackery").candidates
               if c.field == "weight_kg"]
    assert weights == [pytest.approx(10.7955)]
    unscoped = ss.SpecSources(cfg("home-power", brands={"jackery": {"use": [
        {"set": "station-spec-table", "when": {"kinds": ["manufacturer-page"]}, "occurrence": "all"}]}}))
    weights = [c.value_num for c in unscoped.extract(load("jackery-product-page"), brand="jackery").candidates
               if c.field == "weight_kg"]
    assert weights == [pytest.approx(10.7955), pytest.approx(6.5000, abs=0.01)]


# ---------------------------------------------------------------------------
# FOSSiBOT F2400: UPS switchover 10 ms vs about 8 ms
# ---------------------------------------------------------------------------

def test_fossibot_f2400_ups_switchover_conflict(power):
    spec = run(power, "fossibot-f2400", "fossibot-product-page", "fossibot-manual")
    ups = spec.fields["ups_ms"]
    assert ups.status == "conflict"
    by_kind = {x.source.kind: x for x in ups.conflict.candidates}
    assert by_kind["manufacturer-page"].value_num == 10.0
    assert by_kind["manual-pdf"].value_num == 8.0
    assert by_kind["manual-pdf"].qualifier == "approx+lte"          # "within about 8 milliseconds"
    assert "10ms uninterruptible home backup UPS" in by_kind["manufacturer-page"].snippet
    # the page and the manual also disagree on the case length (386 vs 396 mm)
    assert spec.fields["dimensions_mm"].status == "conflict"
    v = spec.values()
    assert (v["capacity_wh"], v["ac_cont_w"], v["ac_surge_w"]) == (2048.0, 2400.0, 4800.0)
    assert (v["pv_v_min"], v["pv_voc_max"], v["pv_w_max"]) == (11.5, 50.0, 500.0)
    assert v["ups_supported"] is True and v["weight_kg"] == 22.0


def test_fossibot_conservative_policy_takes_the_slower_switchover():
    engine = ss.SpecSources(cfg("home-power", fields={"ups_ms": {"resolve": "max"}}))
    spec = run(engine, "fossibot-f2400", "fossibot-product-page", "fossibot-manual")
    ups = spec.fields["ups_ms"]
    assert ups.status == "accepted" and ups.accepted.value_num == 10.0
    assert ups.resolution == "conservative-max"
    rows = [(r.value_num, r.status, r.reasons) for r in spec.provenance() if r.field == "ups_ms"]
    assert (8.0, "superseded", ("conservative-max",)) in rows
    assert (10.0, "accepted", ("conservative-max",)) in rows


def test_fossibot_page_contradicting_itself(power):
    # Bullet: "4,000+ Life Cycles"; spec table: "3,500+ Cycles". The table extractor outranks prose.
    spec = run(power, "fossibot-f2400", "fossibot-product-page")
    cyc = [(r.value_num, r.status, r.reasons) for r in spec.provenance() if r.field == "cycle_life"]
    assert (3500, "accepted", ("single-source",)) in cyc
    assert (4000, "superseded", ("disagrees-with-higher-priority-extractor-in-source",)) in cyc
    strict = ss.SpecSources(cfg("home-power", consolidate={"intra_source": "conflict"}))
    spec = run(strict, "fossibot-f2400", "fossibot-product-page")
    assert spec.fields["cycle_life"].status == "conflict"
    assert spec.fields["cycle_life"].conflict.kind == "intra-source"


# ---------------------------------------------------------------------------
# Another niche, same code
# ---------------------------------------------------------------------------

def test_robot_vacuum_spec_page():
    engine = ss.SpecSources(cfg("robot-vacuum"))
    assert engine.problems == []
    res = engine.extract(load("ecovacs-spec"))              # brand resolved from the host
    assert res.brand == "ecovacs"
    spec = engine.consolidate("ecovacs-deebot-n10", res.candidates)
    assert spec.values() == {
        "model_number": "110-2241-0104", "suction_pa": 4300.0, "battery_mah": 5200.0,
        "dustbin_ml": 420.0, "noise_db_max": 74.0, "runtime_min": 300.0, "charge_time_h": 6.5,
        "rated_power_w": 40.0, "dimensions_mm": [350.0, 350.0, 93.0]}
    assert spec.conflicts == [] and spec.completeness == 1.0
    # "up to 300 minutes" in prose agrees with the table's 200-300 range
    runtime = [(r.value_num, r.status) for r in spec.provenance() if r.field == "runtime_min"]
    assert sorted(runtime) == [(300.0, "accepted"), (300.0, "superseded")]


def test_profiles_do_not_leak_into_each_other(power):
    robot = ss.SpecSources(cfg("robot-vacuum"))
    assert {c.field for c in robot.extract(load("jackery-faq")).candidates} <= set(PROFILES["robot-vacuum"]["fields"])
    assert {c.field for c in power.extract(load("ecovacs-spec")).candidates} <= set(PROFILES["home-power"]["fields"])


# ---------------------------------------------------------------------------
# Registry mechanics
# ---------------------------------------------------------------------------

def test_brand_resolution(power):
    reg = power.extractors
    assert reg.resolve_brand("Jackery") == "jackery"
    assert reg.resolve_brand("Fossi Bot") == "fossibot"
    assert reg.resolve_brand("", "https://r.jackery.net/productGuide/x.pdf") == "jackery"
    assert reg.resolve_brand("", "https://eu.pecron.com/pages/x") == "pecron"
    assert reg.resolve_brand("unknown", "https://other.example/x") == ""
    # unknown brand: the profile's default extractor sets still run
    assert [e.ident for e in reg.extractors_for("")] == [
        "set:station-spec-table/spec-table", "set:station-text/ups-not-supported",
        "set:station-text/marketing-and-prose"]


def test_when_filters_by_source_kind():
    engine = ss.SpecSources(cfg("home-power", brands={"jackery": {"use": [
        {"set": "station-spec-table", "when": {"kinds": ["manufacturer-page"]}},
        {"set": "station-text", "when": {"url": "/blogs/"}}]}}))
    res = engine.extract(load("jackery-faq"), brand="jackery")
    assert res.skipped == ["set:station-spec-table/spec-table"]
    assert res.extractors == ["set:station-text/ups-not-supported", "set:station-text/marketing-and-prose"]
    res = engine.extract(load("jackery-product-page"), brand="jackery")
    assert res.extractors == ["set:station-spec-table/spec-table"]


def test_registered_extractor_kind_is_usable_from_config():
    def warranty_years(doc, ex, ctx):
        for p in doc.pairs:
            if p.label.lower() == "warranty":
                yield {"field": "warranty_years", "value": p.value.split("+")[0], "snippet": p.value}

    ss.register_extractor_kind("test-warranty", warranty_years)
    try:
        with pytest.raises(ValueError):
            ss.register_extractor_kind("test-warranty", warranty_years)
        engine = ss.SpecSources(cfg("home-power",
                                    fields={"warranty_years": {"kind": "count", "min": 1, "max": 20}},
                                    brands={"jackery": {"extractors": [{"name": "w", "kind": "test-warranty"}]}}))
        res = engine.extract(load("jackery-product-page"), brand="jackery")
        w = [c for c in res.candidates if c.field == "warranty_years"]
        assert [(c.value_num, c.extractor_id) for c in w] == [(3.0, "brand:jackery/w")]
    finally:
        ss._EXTRACTOR_KINDS.pop("test-warranty", None)


def plugin_extractor(doc, ex, ctx):
    """Used by the plugin tests below ('framework.tests.test_spec_sources_extract:plugin_extractor')."""
    if ex.options.get("explode"):
        raise RuntimeError("boom")
    yield {"field": "model_number", "value": "PLUGIN-1", "snippet": "from plugin", "confidence": 0.5}


def test_plugin_kind_needs_an_allowlisted_module():
    target = "framework.tests.test_spec_sources_extract:plugin_extractor"
    allowed = ss.SpecSources(cfg("home-power", plugins={"module_prefixes": ["framework.tests."]},
                                 brands={"pecron": {"extractors": [{"name": "p", "kind": "plugin", "callable": target}]}}))
    res = allowed.extract(load("pecron-manual"), brand="pecron")
    assert [(c.value_text, c.confidence) for c in res.candidates if c.field == "model_number"] == [("PLUGIN-1", 0.5)]
    denied = ss.SpecSources(cfg("home-power",
                                brands={"pecron": {"extractors": [{"name": "p", "kind": "plugin", "callable": target}]}}))
    res = denied.extract(load("pecron-manual"), brand="pecron")
    assert any("module_prefixes" in e for e in res.errors)
    assert any(c.field == "capacity_wh" for c in res.candidates)       # other extractors still ran


def test_a_failing_extractor_is_isolated():
    target = "framework.tests.test_spec_sources_extract:plugin_extractor"
    engine = ss.SpecSources(cfg("home-power", plugins={"module_prefixes": ["framework.tests."]},
                                brands={"pecron": {"extractors": [
                                    {"name": "bad", "kind": "plugin", "callable": target, "explode": True}]}}))
    res = engine.extract(load("pecron-manual"), brand="pecron")
    assert res.errors == ["brand:pecron/bad: RuntimeError: boom"]
    assert any(c.field == "capacity_wh" for c in res.candidates)


def test_config_problems_are_reported_not_raised():
    engine = ss.SpecSources(cfg("home-power",
                                default_extractors=["station-spec-table", "no-such-set"],
                                rules=[{"name": "r", "kind": "gte", "a": "ac_surge_w", "b": "nope"},
                                       {"name": "q", "kind": "between", "a": "ac_surge_w", "b": "ac_cont_w"}],
                                brands={"broken": {"use": ["missing-set"]},
                                        "badpick": {"extractors": [{"kind": "pairs", "fields": {
                                            "capacity_wh": {"labels": ["x"], "pick": "median"}}}]}}))
    text = "\n".join(engine.problems)
    for needle in ("no-such-set", "'nope'", "'between'", "missing-set", "median"):
        assert needle in text
    res = engine.extract(load("jackery-faq"), brand="broken")
    assert res.candidates == [] and res.errors and res.errors[0].startswith("config:")


def test_fingerprint_and_short_circuit(power):
    fp = power.fingerprint("jackery")
    assert fp == ss.SpecSources(cfg("home-power")).fingerprint("jackery")
    changed = ss.SpecSources(cfg("home-power", fields={"capacity_wh": {"rel_tol": 0.05}}))
    assert changed.fingerprint("jackery") != fp
    assert power.fingerprint("pecron") != fp
    doc = load("jackery-faq")
    prior = {"content_sha256": doc.content_sha256, "fingerprint": fp}
    assert not ss.needs_extraction(prior, content_sha256=doc.content_sha256, fingerprint=fp)
    assert ss.needs_extraction(prior, content_sha256="0" * 64, fingerprint=fp)
    assert ss.needs_extraction(prior, content_sha256=doc.content_sha256, fingerprint=changed.fingerprint("jackery"))
    assert ss.needs_extraction(None, content_sha256=doc.content_sha256, fingerprint=fp)


def test_extraction_is_deterministic(power):
    a = power.extract(load("fossibot-product-page"), brand="fossibot").candidates
    b = power.extract(load("fossibot-product-page"), brand="fossibot").candidates
    assert a == b


def test_extract_refuses_amazon_documents(power):
    doc = ss.SpecDocument(url="https://www.amazon.com/dp/B0D91KV51C", kind="manufacturer-page",
                          text="Capacity: 1000Wh")
    with pytest.raises(ss.AmazonContentRefused):
        power.extract(doc)


# ---------------------------------------------------------------------------
# Review round 1
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("site_cfg", [
    {"documents": {"main_only": False}}, {"documents": {"skip_tags": ["nav"]}},
    {"source_kind_priority": {"manufacturer-faq": 99}}, {"consolidate": {"intra_source": "conflict"}},
    {"consolidate": {"min_confidence": 0.5}},
])
def test_fingerprint_covers_rendering_and_consolidation_config(power, site_cfg):
    assert ss.SpecSources(cfg("home-power", **site_cfg)).fingerprint("jackery") != power.fingerprint("jackery")


def test_input_ceiling_conflicts_go_to_a_human(power):
    # Real page values plus a manual that states a lower ceiling: the profile
    # routes the ceiling to an operator; the model never sees it.
    cands = power.extract(load("jackery-product-page"), brand="jackery").candidates
    manual = ss.SourceRef("https://cdn.maker.example/manual.pdf", "manual-pdf", "cdn.maker.example")
    cands.append(ss.Candidate("pv_voc_max", 55.0, None, "V", manual, snippet="Solar input 12-55V"))
    spec = power.consolidate("jackery-explorer-1000-v2", cands)
    c = spec.fields["pv_voc_max"].conflict
    assert c is not None and c.route == "human"
    q = ss.AdjudicationQueue(ss.MemoryQueueStore(), cfg={})
    q.enqueue(spec.conflicts)
    assert c.conflict_id not in [x.conflict_id for x in q.plan(max_items=50)]
    model = ss.Decision(c.conflict_id, "jackery-explorer-1000-v2", "pv_voc_max", 0, "x", decided_by="opus")
    assert power.consolidate("jackery-explorer-1000-v2", cands, decisions=[model]).fields["pv_voc_max"].status == "conflict"
