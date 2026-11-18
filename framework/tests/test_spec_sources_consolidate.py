"""framework.core.spec_sources: validators and consolidation. Value ranges,
cross-field rules (a peak rating must be at least the continuous rating),
cross-source agreement within tolerance, conflict records, resolve
policies, manual entries, adjudication decisions, provenance rows and the
DB row shape. Synthetic candidates; no I/O."""
from __future__ import annotations

from framework.core import spec_sources as ss

FIELDS = ss.FieldRegistry({
    "cont_w": {"kind": "quantity", "unit": "W", "min": 50, "max": 30000, "required": True},
    "peak_w": {"kind": "quantity", "unit": "W", "min": 50, "max": 60000},
    "capacity_wh": {"kind": "quantity", "unit": "Wh", "min": 50, "max": 60000, "rel_tol": 0.01, "required": True},
    "switch_ms": {"kind": "quantity", "unit": "ms", "min": 0, "max": 1000},
    "v_max": {"kind": "quantity", "unit": "V", "min": 5, "max": 1000, "resolve": "min"},
    "model": {"kind": "text", "normalize": "alnum"},
    "kind_pref": {"kind": "quantity", "unit": "W", "resolve": "prefer_kind"},
    "chem": {"kind": "enum", "values": {"LiFePO4": ["lifepo4"], "NMC": ["nmc"]}},
    "size": {"kind": "dimensions", "unit": "mm", "rel_tol": 0.02},
    "flag": {"kind": "bool", "values": {"false": ["no"], "true": ["yes"]}},
})
RULES = [{"name": "peak-at-least-continuous", "kind": "gte", "a": "peak_w", "b": "cont_w"}]

PAGE = ss.SourceRef("https://maker.example/p/1", "manufacturer-page", "maker.example", "2026-11-17T10:00:00Z", "a" * 64)
FAQ = ss.SourceRef("https://maker.example/faq/1", "manufacturer-faq", "maker.example", "2026-11-17T10:00:00Z", "b" * 64)
MANUAL = ss.SourceRef("https://cdn.example/manual.pdf", "manual-pdf", "cdn.example", "2026-11-16T10:00:00Z", "c" * 64)
OPERATOR = ss.SourceRef("operator://notes/1", "manual-entry", "operator", "2026-11-17T11:00:00Z", "")


def cand(field, value=None, src=PAGE, text=None, rank=(0, 0), extractor="deterministic", conf=0.9, comps=()):
    return ss.Candidate(field=field, value_num=value, value_text=text,
                        unit=FIELDS[field].unit or None if field in FIELDS else None, source=src,
                        extractor=extractor, extractor_id=f"test:{field}", confidence=conf,
                        snippet=f"{field} {value if value is not None else text}", rank=rank,
                        components=tuple(comps))


def consolidate(cands, **kw):
    kw.setdefault("rules", RULES)
    return ss.consolidate("m1", cands, FIELDS, **kw)


def statuses(spec, field):
    return sorted((r.source_url, r.value_num if r.value_num is not None else r.value_text, r.status)
                  for r in spec.provenance() if r.field == field)


# ---------------------------------------------------------------------------
# value checks
# ---------------------------------------------------------------------------

def test_out_of_range_values_are_rejected_with_a_reason():
    spec = consolidate([cand("capacity_wh", 5.0), cand("capacity_wh", 1000.0, src=FAQ)])
    rows = {r.value_num: r for r in spec.provenance() if r.field == "capacity_wh"}
    assert rows[5.0].status == "rejected" and rows[5.0].reasons == ("out-of-range:<50",)
    assert rows[1000.0].status == "accepted"
    assert spec.fields["capacity_wh"].resolution == "single-source"


def test_enum_and_confidence_floor():
    spec = consolidate([cand("chem", text="Lead acid"), cand("cont_w", 1000.0, conf=0.3)], min_confidence=0.5)
    assert spec.fields["chem"].status == "missing"
    assert [r.reasons for r in spec.provenance() if r.field == "chem"] == [("not-an-allowed-value",)]
    assert [r.reasons for r in spec.provenance() if r.field == "cont_w"] == [("confidence-below-0.5",)]


# ---------------------------------------------------------------------------
# cross-field rules
# ---------------------------------------------------------------------------

def test_peak_below_continuous_opens_a_rule_conflict():
    spec = consolidate([cand("cont_w", 1500.0), cand("peak_w", 1000.0)])
    assert spec.fields["cont_w"].status == "conflict" and spec.fields["peak_w"].status == "conflict"
    (c,) = spec.conflicts
    assert c.kind == "rule" and c.rule == "peak-at-least-continuous"
    assert [x.value_num for x in c.candidates] == [1000.0, 1500.0]
    assert "fails" in c.detail
    assert spec.values() == {}
    assert spec.completeness == 0.0 and "cont_w" in spec.missing_required


def test_rule_passes_and_skips_when_a_side_is_missing():
    assert consolidate([cand("cont_w", 1500.0), cand("peak_w", 3000.0)]).conflicts == []
    assert consolidate([cand("cont_w", 1500.0), cand("peak_w", 1500.0)]).conflicts == []   # equal is fine
    assert consolidate([cand("cont_w", 1500.0)]).conflicts == []


def test_rule_with_reject_policy_drops_the_named_field():
    rules = [dict(RULES[0], on_fail="reject", reject="peak_w")]
    spec = consolidate([cand("cont_w", 1500.0), cand("peak_w", 1000.0)], rules=rules)
    assert spec.fields["cont_w"].status == "accepted"
    assert spec.fields["peak_w"].status == "missing"
    assert [r.status for r in spec.provenance() if r.field == "peak_w"] == ["rejected"]


def test_operator_can_waive_a_rule_for_one_model():
    first = consolidate([cand("cont_w", 1500.0), cand("peak_w", 1000.0)])
    (c,) = first.conflicts
    waiver = ss.Decision(c.conflict_id, "m1", c.field, 0, "datasheet confirms", decided_by="operator")
    spec = consolidate([cand("cont_w", 1500.0), cand("peak_w", 1000.0)], decisions=[waiver])
    assert spec.conflicts == [] and spec.values() == {"cont_w": 1500.0, "peak_w": 1000.0}
    assert "rule-waived:peak-at-least-continuous" in spec.fields["peak_w"].resolution


# ---------------------------------------------------------------------------
# cross-source agreement and conflicts
# ---------------------------------------------------------------------------

def test_agreement_within_tolerance_accepts_the_best_source():
    spec = consolidate([cand("capacity_wh", 1070.0, src=PAGE), cand("capacity_wh", 1075.0, src=FAQ),
                        cand("capacity_wh", 1072.0, src=MANUAL)])
    fr = spec.fields["capacity_wh"]
    assert fr.status == "accepted" and fr.resolution == "agreement"
    assert fr.accepted.source == MANUAL                       # manual-pdf outranks page and FAQ
    assert statuses(spec, "capacity_wh") == [
        ("https://cdn.example/manual.pdf", 1072.0, "accepted"),
        ("https://maker.example/faq/1", 1075.0, "superseded"),
        ("https://maker.example/p/1", 1070.0, "superseded")]


def test_deterministic_outranks_llm_when_values_agree():
    spec = consolidate([cand("cont_w", 1500.0, src=MANUAL, extractor="haiku", conf=0.6),
                        cand("cont_w", 1500.0, src=PAGE)])
    assert spec.fields["cont_w"].accepted.extractor == "deterministic"


def test_disagreement_outside_tolerance_is_a_conflict():
    spec = consolidate([cand("capacity_wh", 1070.0, src=PAGE), cand("capacity_wh", 1024.0, src=MANUAL)])
    fr = spec.fields["capacity_wh"]
    assert fr.status == "conflict" and fr.accepted is None and fr.resolution == "conflict"
    c = fr.conflict
    assert c.kind == "cross-source" and len(c.conflict_id) == 20
    assert [x.source.url for x in c.candidates] == [MANUAL.url, PAGE.url]   # deterministic order
    assert all(r.conflict_id == c.conflict_id for r in spec.provenance() if r.field == "capacity_wh")
    # the id changes when a source's value changes
    other = consolidate([cand("capacity_wh", 1070.0, src=PAGE), cand("capacity_wh", 1000.0, src=MANUAL)])
    assert other.conflicts[0].conflict_id != c.conflict_id


def test_text_and_dimension_agreement():
    spec = consolidate([cand("model", text="JE-1000D"), cand("model", text="je1000d", src=FAQ),
                        cand("size", comps=(386.0, 284.0, 321.0)), cand("size", comps=(388.0, 284.0, 321.0), src=FAQ)])
    assert spec.fields["model"].status == "accepted"
    assert spec.fields["size"].status == "accepted"
    spec = consolidate([cand("size", comps=(386.0, 284.0, 321.0)), cand("size", comps=(396.0, 284.0, 321.0), src=FAQ)])
    assert spec.fields["size"].status == "conflict"


def test_intra_source_disagreement_policies():
    cands = [cand("cont_w", 1500.0, rank=(0, 0)), cand("cont_w", 1800.0, rank=(2, 0))]
    spec = consolidate(cands)
    assert spec.fields["cont_w"].accepted.value_num == 1500.0
    assert [r.reasons for r in spec.provenance() if r.value_num == 1800.0] == [
        ("disagrees-with-higher-priority-extractor-in-source",)]
    spec = consolidate(cands, intra_source="conflict")
    assert spec.fields["cont_w"].status == "conflict" and spec.conflicts[0].kind == "intra-source"


def test_duplicate_values_in_one_source_are_superseded():
    spec = consolidate([cand("cont_w", 1500.0, rank=(0, 0)), cand("cont_w", 1500.0, rank=(1, 0))])
    assert [r.reasons for r in spec.provenance() if r.status == "superseded"] == [("duplicate-in-source",)]


# ---------------------------------------------------------------------------
# resolve policies, manual entries, decisions
# ---------------------------------------------------------------------------

def test_conservative_min_policy():
    spec = consolidate([cand("v_max", 60.0, src=PAGE), cand("v_max", 50.0, src=FAQ)])
    fr = spec.fields["v_max"]
    assert fr.accepted.value_num == 50.0 and fr.resolution == "conservative-min" and spec.conflicts == []


def test_prefer_kind_policy_needs_a_unique_top_kind():
    spec = consolidate([cand("kind_pref", 100.0, src=PAGE), cand("kind_pref", 200.0, src=MANUAL)])
    assert spec.fields["kind_pref"].accepted.value_num == 200.0
    page2 = ss.SourceRef("https://maker.example/p/2", "manufacturer-page", content_sha256="d" * 64)
    spec = consolidate([cand("kind_pref", 100.0, src=PAGE), cand("kind_pref", 200.0, src=page2)])
    assert spec.fields["kind_pref"].status == "conflict"


def test_manual_entry_overrides_everything():
    spec = consolidate([cand("switch_ms", 10.0, src=PAGE), cand("switch_ms", 8.0, src=MANUAL),
                        cand("switch_ms", 12.0, src=OPERATOR, extractor="manual", conf=1.0)])
    fr = spec.fields["switch_ms"]
    assert fr.accepted.value_num == 12.0 and fr.resolution == "manual" and spec.conflicts == []


def test_adjudication_decision_settles_a_conflict():
    cands = [cand("switch_ms", 10.0, src=PAGE), cand("switch_ms", 8.0, src=MANUAL)]
    (c,) = consolidate(cands).conflicts
    idx = [x.value_num for x in c.candidates].index(10.0)
    d = ss.Decision(c.conflict_id, "m1", "switch_ms", idx, "page is the current statement", confidence=0.95)
    spec = consolidate(cands, decisions=[d])
    fr = spec.fields["switch_ms"]
    assert fr.status == "accepted" and fr.resolution == "adjudicated"
    assert fr.accepted.value_num == 10.0 and fr.accepted.extractor == "opus-adjudicated"
    assert fr.accepted.source == PAGE and fr.accepted.confidence == 0.95
    acc = [r for r in spec.provenance() if r.field == "switch_ms" and r.status == "accepted"]
    assert acc[0].extractor == "opus-adjudicated" and acc[0].source_url == PAGE.url
    assert acc[0].conflict_id == c.conflict_id


def test_stale_or_unresolved_decisions_do_not_settle():
    cands = [cand("switch_ms", 10.0, src=PAGE), cand("switch_ms", 8.0, src=MANUAL)]
    (c,) = consolidate(cands).conflicts
    stale = ss.Decision("0" * 20, "m1", "switch_ms", 0)
    assert consolidate(cands, decisions=[stale]).fields["switch_ms"].status == "conflict"
    unresolved = ss.Decision(c.conflict_id, "m1", "switch_ms", None, "cannot tell")
    spec = consolidate(cands, decisions=[unresolved])
    assert spec.fields["switch_ms"].status == "conflict" and spec.conflicts[0].detail == "needs-human"
    other_model = ss.Decision(c.conflict_id, "m2", "switch_ms", 0)
    assert consolidate(cands, decisions=[other_model]).fields["switch_ms"].status == "conflict"


def test_operator_decision_is_recorded_as_manual():
    cands = [cand("switch_ms", 10.0, src=PAGE), cand("switch_ms", 8.0, src=MANUAL)]
    (c,) = consolidate(cands).conflicts
    d = ss.Decision(c.conflict_id, "m1", "switch_ms", 0, decided_by="operator", confidence=1.0)
    assert consolidate(cands, decisions=[d]).fields["switch_ms"].accepted.extractor == "manual"


# ---------------------------------------------------------------------------
# provenance, values, completeness, DB rows
# ---------------------------------------------------------------------------

def test_every_candidate_gets_a_provenance_row():
    cands = [cand("cont_w", 1500.0), cand("cont_w", 1500.0, src=FAQ), cand("capacity_wh", 5.0),
             cand("capacity_wh", 1000.0, src=MANUAL), cand("flag", 1.0, text="true"),
             cand("size", comps=(1.0, 2.0, 3.0)), cand("unknown_field", 1.0)]
    spec = consolidate(cands)
    rows = spec.provenance()
    assert len(rows) == 6                                     # unknown fields are dropped
    for r in rows:
        d = r.to_dict()
        assert set(d) >= {"model_key", "field", "value_num", "value_text", "unit", "status", "source_url",
                          "source_kind", "publisher", "fetched_at", "content_sha256", "extractor",
                          "confidence", "snippet", "reasons", "conflict_id"}
    assert spec.values() == {"cont_w": 1500.0, "capacity_wh": 1000.0, "flag": True, "size": [1.0, 2.0, 3.0]}
    assert [r.value_text for r in rows if r.field == "size"] == ["1×2×3"]
    assert spec.completeness == 1.0 and spec.missing_required == ()


def test_summary_and_db_rows():
    spec = consolidate([cand("cont_w", 1500.0), cand("capacity_wh", 1070.0), cand("capacity_wh", 1024.0, src=MANUAL)])
    s = spec.summary()
    assert s["completeness"] == 0.5 and s["missing_required"] == ["capacity_wh"]
    assert s["conflicts"] == ["capacity_wh"] and s["values"] == {"cont_w": 1500.0}
    db = ss.to_db_rows(spec, model_id=42)
    assert sorted(r["url"] for r in db["spec_sources"]) == [MANUAL.url, PAGE.url]
    assert all(set(r) == {"model_id", "kind", "url", "publisher", "fetched_at", "content_sha256"}
               for r in db["spec_sources"])
    assert len(db["spec_values"]) == 3
    assert {r["status"] for r in db["spec_values"]} == {"accepted", "conflict"}
    assert all(r["model_id"] == 42 and r["extractor"] in ss.EXTRACTOR_CLASSES for r in db["spec_values"])


def test_candidate_and_conflict_round_trip_through_json():
    spec = consolidate([cand("capacity_wh", 1070.0), cand("capacity_wh", 1024.0, src=MANUAL)])
    c = spec.conflicts[0]
    back = ss.Conflict.from_dict(c.to_dict())
    assert back.conflict_id == c.conflict_id and [x.value_num for x in back.candidates] == [1024.0, 1070.0]
    assert back.candidates[0].source == MANUAL
    d = ss.Decision(c.conflict_id, "m1", "capacity_wh", 1, "x")
    assert ss.Decision.from_dict(d.to_dict()) == d


# ---------------------------------------------------------------------------
# Review round 1: stable conflict ids, Amazon sources, human-only fields
# ---------------------------------------------------------------------------

def test_conflict_id_ignores_the_content_hash():
    # A store page's bytes (and so its content hash) can change on every fetch;
    # the same values from the same sources must keep the same conflict, so a
    # decision keeps applying and the queue does not refill daily.
    page_v2 = ss.SourceRef(PAGE.url, PAGE.kind, PAGE.publisher, "2026-11-18T10:00:00Z", "e" * 64)
    a = consolidate([cand("switch_ms", 10.0, src=PAGE), cand("switch_ms", 8.0, src=MANUAL)])
    b = consolidate([cand("switch_ms", 10.0, src=page_v2), cand("switch_ms", 8.0, src=MANUAL)])
    (ca,), (cb,) = a.conflicts, b.conflicts
    assert ca.conflict_id == cb.conflict_id
    idx = [x.value_num for x in ca.candidates].index(10.0)
    d = ss.Decision(ca.conflict_id, "m1", "switch_ms", idx, "page is current")
    again = consolidate([cand("switch_ms", 10.0, src=page_v2), cand("switch_ms", 8.0, src=MANUAL)], decisions=[d])
    assert again.fields["switch_ms"].status == "accepted" and again.values()["switch_ms"] == 10.0
    # a changed value is a different disagreement
    c = consolidate([cand("switch_ms", 12.0, src=page_v2), cand("switch_ms", 8.0, src=MANUAL)])
    assert c.conflicts[0].conflict_id != ca.conflict_id


def test_amazon_sourced_candidates_are_never_accepted_or_stored():
    amazon = ss.SourceRef("https://www.amazon.com/dp/B0D91KV51C", "manual-entry", "", "", "")
    spec = consolidate([cand("switch_ms", 10.0, src=amazon, extractor="manual", conf=1.0),
                        cand("cont_w", 1500.0, src=ss.SourceRef("https://notes.example/x", "manual-entry")),
                        cand("capacity_wh", 1000.0, src=PAGE, text=None)])
    assert spec.fields["switch_ms"].status == "missing"
    assert [r.reasons for r in spec.provenance() if r.field == "switch_ms"] == [("amazon-source",)]
    db = ss.to_db_rows(spec, model_id=7)
    assert all("amazon" not in r["url"] for r in db["spec_sources"])
    assert all("amazon" not in r["source_url"] for r in db["spec_values"])
    assert {r["field"] for r in db["spec_values"]} == {"cont_w", "capacity_wh"}
    # Amazon text in a snippet is refused too
    marked = ss.Candidate("cont_w", 1500.0, None, "W", PAGE, snippet="Best Sellers Rank #3 in Generators")
    assert consolidate([marked]).fields["cont_w"].status == "missing"


HUMAN = ss.FieldRegistry({"v_in_max": {"kind": "quantity", "unit": "V", "min": 5, "max": 1000,
                                       "adjudicate": "human"}})


def test_human_only_fields_ignore_model_decisions():
    cands = [cand("v_max", 60.0, src=PAGE), cand("v_max", 50.0, src=FAQ)]
    cands = [ss.Candidate("v_in_max", c.value_num, None, "V", c.source) for c in cands]
    spec = ss.consolidate("m1", cands, HUMAN)
    (c,) = spec.conflicts
    assert c.route == "human" and c.detail == "human-only-field"
    assert ss.Conflict.from_dict(c.to_dict()).route == "human"
    by_model = ss.Decision(c.conflict_id, "m1", "v_in_max", 0, "x", decided_by="opus")
    assert ss.consolidate("m1", cands, HUMAN, decisions=[by_model]).fields["v_in_max"].status == "conflict"
    by_operator = ss.Decision(c.conflict_id, "m1", "v_in_max", 0, "checked the label", decided_by="operator")
    fr = ss.consolidate("m1", cands, HUMAN, decisions=[by_operator]).fields["v_in_max"]
    assert fr.status == "accepted" and fr.accepted.extractor == "manual"
    assert ss.FieldRegistry({"x": {"kind": "quantity", "unit": "V", "adjudicate": "coin"}}).problems


def test_only_an_operator_can_waive_a_rule():
    first = consolidate([cand("cont_w", 1500.0), cand("peak_w", 1000.0)])
    (c,) = first.conflicts
    assert c.route == "human"
    model_waiver = ss.Decision(c.conflict_id, "m1", c.field, 0, "looks fine", decided_by="opus")
    assert consolidate([cand("cont_w", 1500.0), cand("peak_w", 1000.0)], decisions=[model_waiver]).conflicts
