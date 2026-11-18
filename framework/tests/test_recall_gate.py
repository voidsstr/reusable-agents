"""framework.core.recall_gate: parsing, merging, tiers, matching and config,
against real CPSC data (fixtures/recall_gate, fetched 2026-11-17). No
database and no network: see test_recall_gate_db.py / _agent.py for those."""
from __future__ import annotations

import ast
import datetime as dt
import json
from dataclasses import replace
from pathlib import Path

import pytest

from framework.core import recall_gate as rg
from framework.tests.test_recall_gate_support import (
    RECALL_CSV, WARNING_CSV, FakeHttp, catalog_rows, config_with_site,
    fixture_bytes, fixture_records, list_naming_records, match_table, offline_guard,
    repo_config_doc, review_case_records, settings_for,
)

REPO = Path(__file__).resolve().parents[2]
ECOFLOW = "cpsc-recall-26010"
AEIUSNY = "cpsc-warning-25454"
SEAGULL = "cpsc-recall-25187"


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    offline_guard(monkeypatch)


@pytest.fixture(scope="module")
def records():
    return fixture_records()


# ---------------------------------------------------------------------------
# Parsing real CPSC data
# ---------------------------------------------------------------------------

def test_api_record_ecoflow_delta_max_2000():
    [rec] = rg.parse_api_records(fixture_bytes("saferproducts_api_delta_max.json"))
    assert rec.id == ECOFLOW and rec.kind == rg.KIND_RECALL and rec.number == "26-010"
    assert rec.title.startswith("EcoFlow Technology Recalls Delta Max 2000 Power Stations")
    assert rec.published_at == "2025-10-09"
    assert rec.url == ("https://www.cpsc.gov/Recalls/2026/EcoFlow-Technology-Recalls-Delta-Max-"
                       "2000-Power-Stations-Due-to-Risk-of-Serious-Burn-Injury-and-Fire-Hazard")
    assert rec.url_derived is False
    assert rec.units_count == 25030
    assert rec.product_models == ()  # CPSC leaves Products[].Model empty ...
    assert any("EFD310" in c for c in rec.image_captions)  # ... the model is in the captions
    assert rec.remedy_options_raw == ("R",)  # an unrecognised option code
    assert "firmware update" in rec.remedy
    assert rec.importers == ("EcoFlow Technology, Inc., of San Francisco, California",)


def test_recall_csv_rows_parse_and_derive_urls():
    recs = {r.id: r for r in rg.parse_recall_csv(fixture_bytes(RECALL_CSV))}
    assert len(recs) == 15
    eco = recs[ECOFLOW]
    assert eco.number == "26-010" and eco.published_at == "2025-10-09"
    assert eco.remedy_options_raw == ("Repair",)
    # The CSV carries no URL; the derived one is the real page for this record.
    assert eco.url_derived and eco.url == rg.parse_api_records(
        fixture_bytes("saferproducts_api_delta_max.json"))[0].url
    newdery = recs["cpsc-recall-26792"]
    assert newdery.related_ids == ("cpsc-warning-25327",)  # the warning it superseded
    aiper = recs[SEAGULL]
    assert aiper.manufacturers and aiper.distributors
    assert " " not in aiper.description and "&nbsp;" not in aiper.description


def test_warning_csv_aeiusny():
    recs = {r.id: r for r in rg.parse_warning_csv(fixture_bytes(WARNING_CSV))}
    assert len(recs) == 6
    a = recs[AEIUSNY]
    assert a.kind == rg.KIND_WARNING and a.number == "25-454" and a.published_at == "2025-09-04"
    assert a.product_names == ("Aeiusny 400W and 500W portable power stations",)
    assert a.manufacturers == ("Shenzhen Yuanzhao E-Commerce Co., Ltd., of China",)
    # Derived URL equals the real CPSC warning page (warnings file under FY 2025).
    assert a.url == ("https://www.cpsc.gov/Warnings/2025/CPSC-Warns-Consumers-to-Immediately-"
                     "Stop-Using-Aeiusny-Power-Stations-Due-to-Risk-of-Serious-Injury-or-Death-"
                     "from-Fire-Sold-on-Amazon")
    assert rg.with_remedy_options(a, rg.DEFAULTS["remedy_options"]).remedy_options == ("dispose",)


def test_csv_header_drift_is_an_error_not_empty_data():
    bad = b"Number,Heading\n26-010,Something\n"
    with pytest.raises(rg.SourceError, match="unexpected CSV header"):
        rg.parse_recall_csv(bad)
    with pytest.raises(rg.SourceError):
        rg.parse_warning_csv(bad)


def test_api_error_payload_is_an_error():
    with pytest.raises(rg.SourceError, match="expected a JSON array"):
        rg.parse_api_records(b'{"Message": "No HTTP resource was found"}')
    with pytest.raises(rg.SourceError, match="not JSON"):
        rg.parse_api_records(b"<html>Access Denied</html>")


def test_merge_api_and_csv_into_one_record(records):
    eco = records[ECOFLOW]
    assert set(eco.sources) == {rg.SOURCE_API, rg.SOURCE_RECALL_CSV}
    assert eco.url_derived is False  # the API's real URL beats the derived one
    assert eco.number == "26-010"
    assert eco.remedy_options == ("repair",)  # CSV "Repair" normalises the API's "R"
    # The warning learns which recall superseded it.
    assert "cpsc-recall-26792" in records["cpsc-warning-25327"].related_ids
    # Merging is idempotent and order-stable.
    assert rg.merge_recall(eco, eco) == eco


def test_merge_keeps_known_values_on_a_csv_only_day(records):
    eco = records[ECOFLOW]
    csv_only = {r.id: r for r in rg.parse_recall_csv(fixture_bytes(RECALL_CSV))}[ECOFLOW]
    merged = rg.merge_recall(eco, csv_only)
    assert merged.url == eco.url and merged.url_derived is False
    assert merged.image_captions == eco.image_captions
    assert merged.content_sha() == rg.with_remedy_options(
        merged, rg.DEFAULTS["remedy_options"]).content_sha()


def test_recall_round_trips_through_its_stored_json(records):
    for rec in records.values():
        again = rg.Recall.from_dict(json.loads(json.dumps(rec.to_dict())))
        assert again == rec and again.content_sha() == rec.content_sha()


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def test_clean_text_handles_cpsc_csv_debris():
    raw = ('violate the https://www.cpsc.gov/Business--Manufacturing/Busines… '
           'lang="EN-US">lighters&amp;nbsp;because <b>they</b>&nbsp;do')
    assert rg.clean_text(raw) == ("violate the https://www.cpsc.gov/Business--Manufacturing/"
                                  "Busines… lighters because they do")


def test_norm_text_joins_units_and_drops_possessives_and_accents():
    assert rg.tokenize("Predator 2000-Watt Power Stations") == ("predator", "2000w", "power",
                                                                "stations")
    assert rg.tokenize("Aeiusny 400W and 500 W") == ("aeiusny", "400w", "and", "500w")
    assert rg.tokenize("Goal Zero’s YETI 3000X") == ("goal", "zero", "yeti", "3000x")
    assert rg.tokenize("The word “insé”") == ("the", "word", "inse")
    assert rg.compact("EFD-310") == "efd310"
    assert rg.tokenize("About 25,030 units") == ("about", "25030", "units")


@pytest.mark.parametrize("text,expected", [
    ("About 25,030", 25030),
    ("About 32,660 (In addition, about 2,530 were sold in Canada)", 32660),
    ("About 2.5 million", 2_500_000),
    ("Nearly 600,000", 600_000),
    ("54", 54),
    ("", None),
    ("None reported", None),
])
def test_parse_units(text, expected):
    assert rg.parse_units(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("October 09, 2025", "2025-10-09"),
    ("November 5, 2026", "2026-11-05"),
    ("2025-10-09T00:00:00", "2025-10-09"),
    ("10/09/2025", "2025-10-09"),
    ("not a date", None),
    ("", None),
])
def test_parse_date(text, expected):
    assert rg.parse_date(text) == expected


# ---------------------------------------------------------------------------
# Tiers
# ---------------------------------------------------------------------------

RULES = rg.DEFAULTS["tiers"]["rules"]


def test_tiers_for_the_golden_records(records):
    eco = rg.decide_tier(records[ECOFLOW], RULES)
    assert (eco.tier, eco.rule_id, eco.hold_days) == (rg.TIER_REPAIR_HOLD, "repair-remedy", 90)
    assert rg.hold_until_for(eco, records[ECOFLOW]) == "2026-01-07"  # recall date + 90 days
    assert rg.decide_tier(records[AEIUSNY], RULES).tier == rg.TIER_STOP_USE  # a warning
    assert rg.decide_tier(records[SEAGULL], RULES).tier == rg.TIER_STOP_USE  # replace
    yeti = records["cpsc-recall-26712"]  # firmware repair
    assert rg.hold_until_for(rg.decide_tier(yeti, RULES), yeti) == "2026-11-18"


def test_repair_hold_anchors(records):
    rec = records[ECOFLOW]
    matched = dt.datetime(2026, 11, 17, 6, 15, tzinfo=dt.timezone.utc)
    dec = rg.TierDecision(rg.TIER_REPAIR_HOLD, "r", hold_days=90, hold_anchor="first_match")
    assert rg.hold_until_for(dec, rec, first_matched=matched) == "2027-02-15"
    dec = rg.TierDecision(rg.TIER_REPAIR_HOLD, "r", hold_days=90, hold_anchor="later")
    assert rg.hold_until_for(dec, rec, first_matched=matched) == "2027-02-15"
    assert rg.hold_until_for(dec, rec) == "2026-01-07"
    assert rg.hold_until_for(rg.TierDecision(rg.TIER_STOP_USE, "r"), rec) is None


def test_mixed_remedies_are_stop_use_and_rule_order_matters(records):
    rec = rg.replace(records[ECOFLOW], remedy_options=("refund", "repair"))
    assert rg.decide_tier(rec, RULES).tier == rg.TIER_STOP_USE
    custom = [{"id": "everything-holds", "when": {}, "tier": rg.TIER_REPAIR_HOLD,
               "hold_days": 30}]
    assert rg.decide_tier(rec, custom).tier == rg.TIER_REPAIR_HOLD
    assert rg.decide_tier(rec, []).rule_id == "fallback"


def test_remedy_text_decides_when_no_code_is_known(records):
    api_only = rg.parse_api_records(fixture_bytes("saferproducts_api_delta_max.json"))[0]
    assert rg.normalize_remedy_options(api_only, rg.DEFAULTS["remedy_options"]) == ("repair",)


@pytest.mark.parametrize("tier,status,hold,today,blocked", [
    (rg.TIER_STOP_USE, rg.STATUS_ACTIVE, None, "2030-01-01", True),
    (rg.TIER_REPAIR_HOLD, rg.STATUS_ACTIVE, "2026-01-07", "2026-01-06", True),
    (rg.TIER_REPAIR_HOLD, rg.STATUS_ACTIVE, "2026-01-07", "2026-01-07", False),
    (rg.TIER_REPAIR_HOLD, rg.STATUS_ACTIVE, None, "2030-01-01", True),
    (rg.TIER_REVIEW, rg.STATUS_REVIEW, None, "2026-01-01", False),
    (rg.TIER_STOP_USE, rg.STATUS_CLEARED, None, "2026-01-01", False),
])
def test_buy_button_rule(tier, status, hold, today, blocked):
    assert rg.is_blocking(tier, status, hold, dt.date.fromisoformat(today)) is blocked


# ---------------------------------------------------------------------------
# Golden matching
# ---------------------------------------------------------------------------

POWER_GOLDEN = {
    # EcoFlow Delta Max 2000 (recall 26-010): repair-hold until 2026-01-07.
    (ECOFLOW, 1, "exact", "model-number", "repair-hold", "active", "2026-01-07"),
    (ECOFLOW, 6, "exact", "component-model-number", "repair-hold", "active", "2026-01-07"),
    (ECOFLOW, 2, "sibling", "family", "review", "manual-review", None),
    # Goal Zero YETI 3000X (firmware repair, 2026-08-20 + 90 days).
    ("cpsc-recall-26712", 21, "exact", "model-name", "repair-hold", "active", "2026-11-18"),
    ("cpsc-recall-26712", 22, "sibling", "family", "review", "manual-review", None),
    # CPSC warning on Aeiusny 400W and 500W stations: stop-use. The 300W
    # (named by its number alone) is the same brand and category: review.
    (AEIUSNY, 11, "exact", "model-name", "stop-use", "active", None),
    (AEIUSNY, 12, "exact", "model-name", "stop-use", "active", None),
    (AEIUSNY, 13, "sibling", "numbered-variant", "review", "manual-review", None),
    (AEIUSNY, 14, "ambiguous", "no-model-detail", "review", "manual-review", None),
    ("cpsc-recall-24350", 61, "exact", "model-name", "stop-use", "active", None),
    # Predator 2000-Watt recalled; the 1000 Watt station is queued for review.
    ("cpsc-recall-25366", 71, "exact", "model-name", "stop-use", "active", None),
    ("cpsc-recall-25366", 72, "sibling", "numbered-variant", "review", "manual-review", None),
    ("cpsc-recall-24067", 81, "exact", "model-name", "stop-use", "active", None),
}

ROBOTS_GOLDEN = {
    (SEAGULL, 201, "exact", "model-number", "stop-use", "active", None),
    (SEAGULL, 202, "sibling", "family", "review", "manual-review", None),
    ("cpsc-recall-23784", 205, "exact", "model-name", "stop-use", "active", None),
    ("cpsc-recall-22727", 251, "sibling", "family", "review", "manual-review", None),
    ("cpsc-warning-24054", 212, "exact", "asin", "stop-use", "active", None),
    ("cpsc-warning-23160", 212, "exact", "asin", "stop-use", "active", None),
}


def test_golden_home_backup_power(records):
    got = match_table(settings_for("power-site", "home-backup-power"),
                      catalog_rows("home_backup_power"), records)
    assert got == POWER_GOLDEN


def test_golden_cleaning_robots(records):
    got = match_table(settings_for("robot-site", "cleaning-robots"),
                      catalog_rows("cleaning_robots"), records)
    assert got == ROBOTS_GOLDEN


def test_power_negatives_are_explained_by_design(records):
    """Not matched: DELTA 2 Max (family DELTA 2), RIVER 2, the DELTA Max extra
    battery (a battery, the recall is about stations), Goal Zero stations
    against Goal Zero CABLE recalls, the Boulder panel against a
    combiner-cable recall "for solar panels". The Aeiusny 300W and Predator
    1000 W (names that are a number alone, same brand as a recall naming
    other numbers) are review siblings, never blocked."""
    got = match_table(settings_for("power-site", "home-backup-power"),
                      catalog_rows("home_backup_power"), records)
    matched_products = {pid for _, pid, *_ in got}
    for pid in (3, 4, 5, 31, 41, 24):
        assert pid not in matched_products, pid
    for pid in (13, 72):
        assert {(r[2], r[3], r[5]) for r in got if r[1] == pid} == {
            ("sibling", "numbered-variant", "manual-review")}
    goal_zero_cable_recalls = {"cpsc-recall-21104", "cpsc-recall-24270", "cpsc-recall-25159"}
    assert not {r for r, *_ in got} & goal_zero_cable_recalls


def test_remedy_text_never_matches_the_replacement_product(records):
    """Recall 25-187's remedy offers 'a free replacement Aiper Scuba S1'."""
    assert "Scuba S1" in records[SEAGULL].remedy
    got = match_table(settings_for("robot-site", "cleaning-robots"),
                      catalog_rows("cleaning_robots"), records)
    assert not [row for row in got if row[1] == 203]


def test_compatibility_reference_brand_is_ignored_or_reviewed(records):
    """ZAUTNKN batteries 'for Dyson vacuums' (warning 24-054) must not block
    the Dyson V6 Animal itself. 'review' mode queues it instead."""
    rows = [r for r in catalog_rows("cleaning_robots") if r["brand"] == "Dyson"]
    one = {k: records[k] for k in ("cpsc-warning-24054",)}
    assert match_table(settings_for("s", "cleaning-robots"), rows, one) == set()
    review = settings_for("s", "cleaning-robots",
                          matching={"compat_reference_matches": "review"})
    got = match_table(review, rows, one)
    assert {(r[2], r[3]) for r in got} == {("ambiguous", "model-name-reference")}


def test_asin_hit_without_the_brand_is_exact_but_flagged(records):
    """CPSC printed ASIN B093Y1KK5Q under the ZAUTNKN battery warning AND a
    CO-detector warning: the listing sold a recalled product either way."""
    items, _ = rg.catalog_from_rows(catalog_rows("cleaning_robots"))
    m = rg.Matcher(settings_for("s", "cleaning-robots"), items)
    _, [co] = m.match(rg.RecallView(records["cpsc-warning-23160"]))
    assert co.rule == "asin" and co.match_kind == "exact"
    assert co.evidence["brand_named"] is False and "repurposed" in co.reason
    _, [bat] = m.match(rg.RecallView(records["cpsc-warning-24054"]))
    assert bat.evidence["brand_named"] is True


def test_niche_agnostic_profiles_ignore_each_other(records):
    """The power profile finds nothing in the Aiper recall, the robots profile
    nothing in the EcoFlow recall; in the 10-recall API week only EcoFlow is
    relevant to the power profile and nothing to the robots profile."""
    power, robots = (settings_for("p", "home-backup-power"),
                     settings_for("r", "cleaning-robots"))
    assert match_table(power, catalog_rows("home_backup_power"),
                       {SEAGULL: records[SEAGULL]}) == set()
    assert match_table(robots, catalog_rows("cleaning_robots"),
                       {ECOFLOW: records[ECOFLOW]}) == set()
    week = rg.parse_api_records(fixture_bytes(
        "saferproducts_api_recalldate_2025-10-08_2025-10-10.json"))
    assert len(week) == 10
    pm = rg.Matcher(power, rg.catalog_from_rows(catalog_rows("home_backup_power"))[0])
    rm = rg.Matcher(robots, rg.catalog_from_rows(catalog_rows("cleaning_robots"))[0])
    assert [r.id for r in week if pm.categories_for(rg.RecallView(r))] == [ECOFLOW]
    assert [r.id for r in week if rm.categories_for(rg.RecallView(r))] == []
    assert rm.categories_for(rg.RecallView(records[SEAGULL])) == ("robot",)


def test_category_phrase_after_a_cue_does_not_count(records):
    m = rg.Matcher(settings_for("p", "home-backup-power"), [])
    combiner = rg.RecallView(records["cpsc-recall-25159"])  # "Combiner Cables for Solar Panels"
    assert m.categories_for(combiner) == ("cable",)
    assert m.categories_for(rg.RecallView(records[AEIUSNY])) == ("station",)


@pytest.mark.parametrize("mode,expected", [
    ("review", {("ambiguous", "out-of-category")}),
    ("ignore", set()),
    ("exact", {("exact", "model-name")}),
])
def test_out_of_category_modes(records, mode, expected):
    """INSE stick vacuums (warning 25-213) are outside the robots profile."""
    rows = [{"product_id": 9, "brand": "INSE", "model_name": "S6P Pro",
             "category": "stick-vacuum"}]
    st = settings_for("r", "cleaning-robots", matching={"out_of_category": mode})
    got = match_table(st, rows, {"cpsc-warning-25213": records["cpsc-warning-25213"]})
    assert {(r[2], r[3]) for r in got} == expected


POWER_BANKS = {"power-bank": {"patterns": [r"\bpower banks?\b"],
                              "catalog_categories": ["power-bank"]}}


def test_warning_names_one_model_of_a_brand(records):
    """HAIARA warning 27-077 covers model W5 PRO only (the description says so
    near its end): that model is exact, another HAIARA power bank is not
    matched, and an out-of-category HAIARA lamp never is."""
    rows = [{"product_id": 1, "brand": "HAIARA", "model_name": "W5 PRO", "category": "power-bank"},
            {"product_id": 2, "brand": "HAIARA", "model_name": "Q10 Power Bank 20000mAh",
             "category": "power-bank"},
            {"product_id": 3, "brand": "HAIARA", "model_name": "Desk Lamp", "category": "lamp"}]
    st = settings_for("pb", "home-backup-power", categories=POWER_BANKS)
    got = match_table(st, rows, {"cpsc-warning-27077": records["cpsc-warning-27077"]})
    assert got == {("cpsc-warning-27077", 1, "exact", "model-name", "stop-use", "active", None)}


def test_brand_wide_recall_without_a_model_queues_in_category_rows():
    rec = _synthetic("Acme Recalls Power Banks Due to Fire Hazard",
                     "This recall involves all Acme power banks sold in 2025.")
    rows = [{"product_id": 1, "brand": "Acme", "model_name": "Q10 Power Bank",
             "category": "power-bank"},
            {"product_id": 2, "brand": "Acme", "model_name": "Desk Lamp", "category": "lamp"}]
    st = settings_for("pb", "home-backup-power", categories=POWER_BANKS)
    assert rg.RecallView(rec).identifiers == frozenset()
    got = match_table(st, rows, {rec.id: rec})
    assert got == {(rec.id, 1, "ambiguous", "brand-wide", "review", "manual-review", None)}


def test_identifier_tokens():
    assert rg._is_identifier("3000x") and rg._is_identifier("efd310") and rg._is_identifier("400w")
    assert not rg._is_identifier("2x") and not rg._is_identifier("24lbs")
    assert not rg._is_identifier("2025") and not rg._is_identifier("model")
    named = rg.RecallView(_synthetic("EcoFlow Recalls DELTA Max 2000 Power Stations"))
    assert named.identifiers == frozenset({"2000"})
    counted = rg.RecallView(_synthetic("Acme Recalls 46,000 Space Heaters",
                                       "About 46,000 units were sold."))
    assert counted.identifiers == frozenset()
    spec = rg.RecallView(_synthetic("Acme Recalls Power Stations", "Each has 1800W outlets."))
    assert spec.identifiers == frozenset({"1800w"}) and spec.model_identifiers == frozenset()
    assert named.model_identifiers == frozenset({"2000"})


def _synthetic(title: str, description: str = "") -> rg.Recall:
    return rg.Recall(id="cpsc-recall-99001", kind=rg.KIND_RECALL, number="99-001",
                     title=title, description=description, published_at="2026-11-01",
                     remedy_options=("refund",))


def test_maximal_munch_prefers_the_longer_model_name():
    rows = [{"product_id": 1, "brand": "Jackery", "model_name": "Explorer 1000"},
            {"product_id": 2, "brand": "Jackery", "model_name": "Explorer 1000 Plus"}]
    rec = _synthetic("Jackery Recalls Explorer 1000 Plus Power Stations Due to Fire Hazard")
    got = match_table(settings_for("p", "home-backup-power"), rows, {rec.id: rec})
    assert {(r[1], r[2], r[3]) for r in got} == {(2, "exact", "model-name"),
                                                 (1, "sibling", "variant")}


def test_variant_guard_short_numbers_and_words():
    st = settings_for("p", "home-backup-power")
    rows = [{"product_id": 1, "brand": "EcoFlow", "model_name": "DELTA Max"},
            {"product_id": 2, "brand": "EcoFlow", "model_name": "DELTA Pro"},
            {"product_id": 3, "brand": "EcoFlow", "model_name": "DELTA Pro 3", "family": "DELTA Pro"}]
    rec = _synthetic("EcoFlow Recalls DELTA Max 2000 Power Stations")
    got = {(r[1], r[2], r[3]) for r in match_table(st, rows, {rec.id: rec})}
    # "2000" sits next to "DELTA Max": a different variant, so review, not exact.
    # Without a family column "DELTA Pro" infers the line "DELTA" (renamed-model
    # safety net, review); an explicit family "DELTA Pro" does not match.
    assert got == {(1, "sibling", "variant"), (2, "sibling", "family")}


def test_model_number_needs_the_brand_as_subject():
    rows = [{"product_id": 1, "brand": "EcoFlow", "model_name": "DELTA Max 2000",
             "model_number": "EFD310"}]
    rec = _synthetic("Acme Recalls Chargers for EcoFlow Power Stations",
                     "Acme chargers sold for use with EcoFlow model EFD310 power stations.")
    assert match_table(settings_for("p", "home-backup-power"), rows, {rec.id: rec}) == set()


def test_short_numeric_model_numbers_need_a_model_cue():
    rows = [{"product_id": 1, "brand": "Bissell", "model_name": "Multi Reach",
             "model_number": "2551", "category": "robot-vacuum"}]
    st = settings_for("r", "cleaning-robots", matching={"out_of_category": "exact"})
    with_cue = _synthetic("Bissell Recalls Robot Vacuum Cleaners",
                          "The recall involves models 2551, 2551W and 25519.")
    without = _synthetic("Bissell Recalls Robot Vacuum Cleaners",
                         "About 2551 units were sold in 2023.")
    assert {r[3] for r in match_table(st, rows, {with_cue.id: with_cue})} == {"model-number"}
    assert {r[3] for r in match_table(st, rows, {without.id: without})} != {"model-number"}


# ---------------------------------------------------------------------------
# Review findings, on real CPSC rows (fixtures/recall_gate/*review_cases*) and
# on synthetic recalls where no real one exists
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def review_records():
    return review_case_records()


def _kinds(st, rows, recs):
    return {(r[1], r[2], r[3]) for r in match_table(st, rows, recs)}


def _one(records, rid):
    return {rid: records[rid]}


EBIKE = {"ebike": {"patterns": [r"\be-?bikes?\b"], "catalog_categories": ["ebike"]}}
RIDSTAR = [{"product_id": 1, "brand": "Ridstar", "model_name": "Q20", "category": "ebike"},
           {"product_id": 2, "brand": "Ridstar", "model_name": "Q20 Pro", "category": "ebike"},
           {"product_id": 3, "brand": "Ridstar", "model_name": "Q20 Lite", "category": "ebike"},
           {"product_id": 4, "brand": "Ridstar", "model_name": "MN26", "category": "ebike"}]


def test_a_model_and_its_longer_variant_named_together_are_both_exact(review_records):
    """CPSC warnings 26-337 ('Ridstar Q20 and Q20 Pro e-bikes') and 26-584
    ('Q20 and Q20 Lite'): every named model is blocked (the shorter name was
    downgraded to review before the fix), and only those."""
    st = settings_for("e", "home-backup-power", categories=EBIKE)
    assert _kinds(st, RIDSTAR, _one(review_records, "cpsc-warning-26337")) == {
        (1, "exact", "model-name"), (2, "exact", "model-name")}
    assert _kinds(st, RIDSTAR, _one(review_records, "cpsc-warning-26584")) == {
        (1, "exact", "model-name"), (3, "exact", "model-name")}
    chairs = settings_for("c", "home-backup-power", categories={
        "highchair": {"patterns": [r"\bhigh ?chairs?\b"], "catalog_categories": ["highchair"]}})
    boon = [{"product_id": 1, "brand": "Boon", "model_name": "Flair", "category": "highchair"},
            {"product_id": 2, "brand": "Boon", "model_name": "Flair Elite",
             "category": "highchair"},
            {"product_id": 3, "brand": "Boon", "model_name": "Grub", "category": "highchair"}]
    assert _kinds(chairs, boon, _one(review_records, "cpsc-recall-23272")) == {
        (1, "exact", "model-name"), (2, "exact", "model-name")}


def test_list_naming_names_every_model(review_records):
    """Recall 26-444 'Thermos Stainless King 3000 and 3020 Food Jars and
    Thermos Sportsman 3010': all three are exact; another King is a sibling."""
    jars = settings_for("j", "home-backup-power", categories={
        "jar": {"patterns": [r"\bfood jars?\b", r"\bbottles?\b"], "catalog_categories": ["jar"]}})
    rows = [{"product_id": 1, "brand": "Thermos", "model_name": "Stainless King 3000",
             "category": "jar"},
            {"product_id": 2, "brand": "Thermos", "model_name": "Stainless King 3020",
             "category": "jar"},
            {"product_id": 3, "brand": "Thermos", "model_name": "Sportsman 3010",
             "category": "jar"},
            {"product_id": 4, "brand": "Thermos", "model_name": "Stainless King 3050",
             "category": "jar"}]
    assert _kinds(jars, rows, _one(review_records, "cpsc-recall-26444")) == {
        (1, "exact", "model-name"), (2, "exact", "model-name"), (3, "exact", "model-name"),
        (4, "sibling", "family")}
    jackery = [{"product_id": n, "brand": "Jackery", "model_name": f"Explorer {n}",
                "category": "station"} for n in (1000, 1500, 2000, 3000)]
    rec = _synthetic("Jackery Recalls Explorer 1000, 1500 and 2000 Portable Power Stations",
                     "This recall involves Jackery Explorer 1000, 1500 and 2000 stations.")
    assert _kinds(settings_for("p", "home-backup-power"), jackery, {rec.id: rec}) == {
        (1000, "exact", "model-name"), (1500, "exact", "model-name"),
        (2000, "exact", "model-name"), (3000, "sibling", "family")}


@pytest.fixture(scope="module")
def list_records():
    return list_naming_records()


def _cat(key, pattern):
    return {key: {"patterns": [pattern], "catalog_categories": [key]}}


def _rows(brand, category, *names, start=1, family=""):
    return [{"product_id": start + i, "brand": brand, "model_name": n, "category": category,
             "family": family} for i, n in enumerate(names)]


def test_golden_elided_lists_name_every_listed_model(list_records):
    """Real recalls whose list states the shared leading words once: every
    named model is exact (they used to be siblings with a live buy button,
    'different model'); an unlisted model of the same line is a sibling."""
    rov = settings_for("v", "home-backup-power",
                       categories=_cat("rov", r"\boff highway vehicles?\b"))
    rows = (_rows("Arctic Cat", "rov", "Prowler Pro", "Prowler Pro Crew", "Prowler 500")
            + _rows("Tracker", "rov", "Off Road 800SX", "Off Road 800SX Crew", start=11))
    assert _kinds(rov, rows, _one(list_records, "cpsc-recall-25701")) == {
        (1, "exact", "model-name"), (2, "exact", "model-name"), (3, "sibling", "family"),
        (11, "exact", "model-name"), (12, "exact", "model-name")}
    strollers = settings_for("s", "home-backup-power",
                             categories=_cat("stroller", r"\bstrollers?\b"))
    rows = _rows("Tike Tech", "stroller", "Single City X3", "City X3 Sport", "Double City X3",
                 family="City X3")
    assert _kinds(strollers, rows, _one(list_records, "cpsc-recall-11002")) == {
        (1, "exact", "model-name"), (2, "exact", "model-name"), (3, "sibling", "family")}
    utv = settings_for("u", "home-backup-power",
                       categories=_cat("utv", r"\b(off-?road|utility) vehicles?\b"))
    rows = _rows("Polaris", "utv", "RANGER 1000", "RANGER 1000 Crew", "RANGER XP 1000",
                 "RANGER XP 1000 Crew", "RANGER 570")
    got = {r[0]: r[1:] for r in _kinds(utv, rows, _one(list_records, "cpsc-recall-23258"))}
    assert {k: got[k] for k in (1, 2, 3, 5)} == {
        1: ("exact", "model-name"), 2: ("exact", "model-name"), 3: ("exact", "model-name"),
        5: ("sibling", "family")}
    # "RANGER [1000, 1000 Crew, XP 1000,] XP 1000 Crew": the shared "RANGER"
    # stands 9 tokens back, beyond matching.max_span (8). It is never dropped:
    # it waits in the manual queue as a sibling.
    assert got[4][0] == "sibling"


def test_elided_list_naming_synthetic():
    st = settings_for("p", "home-backup-power")
    jackery = _rows("Jackery", "station", "Explorer 1000", "Explorer 1000 Plus", "Explorer 2000")
    for sep in (" and ", ", ", "/"):
        rec = _synthetic(f"Jackery Recalls Explorer 1000{sep}1000 Plus Portable Power Stations "
                         "Due to Fire Hazard")
        assert _kinds(st, jackery, {rec.id: rec}) == {
            (1, "exact", "model-name"), (2, "exact", "model-name"), (3, "sibling", "family")}, sep
    pro = _rows("Jackery", "station", "Explorer 1000", "Explorer 1000 Pro", "Explorer 2000 Pro")
    rec = _synthetic("Jackery Recalls Explorer 1000 Pro and 2000 Pro Portable Power Stations")
    # Explorer 1000 itself is not named: every mention of it reads "1000 Pro".
    assert _kinds(st, pro, {rec.id: rec}) == {
        (1, "sibling", "variant"), (2, "exact", "model-name"), (3, "exact", "model-name")}
    # Three items, the first two skipped.
    rec = _synthetic("Jackery Recalls Explorer 1000, 1000 Pro and 1000 Plus Power Stations")
    rows = _rows("Jackery", "station", "Explorer 1000", "Explorer 1000 Pro", "Explorer 1000 Plus")
    assert _kinds(st, rows, {rec.id: rec}) == {
        (1, "exact", "model-name"), (2, "exact", "model-name"), (3, "exact", "model-name")}


def test_elided_list_naming_negatives(list_records):
    st = settings_for("p", "home-backup-power")
    # Kept from round 1: a longer name alone still supersedes the shorter one ...
    rows = _rows("Jackery", "station", "Explorer 1000", "Explorer 1000 Plus")
    rec = _synthetic("Jackery Recalls Explorer 1000 Plus Power Stations Due to Fire Hazard")
    assert _kinds(st, rows, {rec.id: rec}) == {(1, "sibling", "variant"),
                                               (2, "exact", "model-name")}
    # ... and "1000 Pro and 2000" names Explorer 2000, not Explorer 2000 Pro.
    rows = _rows("Jackery", "station", "Explorer 1000 Pro", "Explorer 2000", "Explorer 2000 Pro")
    rec = _synthetic("Jackery Recalls Explorer 1000 Pro and 2000 Portable Power Stations")
    assert _kinds(st, rows, {rec.id: rec}) == {
        (1, "exact", "model-name"), (2, "exact", "model-name"), (3, "sibling", "variant")}
    # The skipped stretch must be whole list items: "Solar Generator 1000 Plus"
    # is another product line, not "[Explorer] 1000 Plus" ...
    rows = _rows("Jackery", "station", "Explorer 1000", "Explorer 1000 Plus")
    rec = _synthetic("Jackery Recalls Explorer 1000 and Solar Generator 1000 Plus Power Stations")
    assert (2, "exact", "model-name") not in _kinds(st, rows, {rec.id: rec})
    # ... and parallel to the named item ("Solar Panels" is not a "1000 ..." item).
    rec = _synthetic("Jackery Recalls Explorer 1000 and Solar Panels, 1000 Plus Power Stations")
    assert (2, "exact", "model-name") not in _kinds(st, rows, {rec.id: rec})
    utv = settings_for("u", "home-backup-power",
                       categories=_cat("utv", r"\b(off-?road|utility) vehicles?\b"))
    # Real 20-735 lost its punctuation: "... 570 and 1000 RANGER CREW 570 and
    # CREW XP 1000". The trailing "1000" of one item is not the leading word
    # of "RANGER 1000 Crew" (the name's words must read in order).
    rows = _rows("Polaris", "utv", "RANGER 1000", "RANGER 1000 Crew", "RANGER CREW XP 1000")
    got = {r[0]: r[1:] for r in _kinds(utv, rows, _one(list_records, "cpsc-recall-20735"))}
    assert got[1] == ("exact", "model-name") and got[3] == ("exact", "model-name")
    assert got[2][0] != "exact"
    # Real 19-088 "Honda EU2200i, EU2200i Companion and EB2200i": every named
    # model stays exact, and a complete first item is never read as the shared
    # prefix of a later one: no window skips "EU2200i Companion and" from the
    # first "EU2200i," (only the pre-existing "EU2200i [Companion and]
    # EB2200i" reading, whose gap holds no other EU2200i, remains).
    gens = settings_for("g", "home-backup-power", categories=_cat("gen", r"\bgenerators?\b"))
    rows = _rows("Honda", "gen", "EU2200i", "EU2200i Companion", "EB2200i", "EU2200i Camo")
    honda = _one(list_records, "cpsc-recall-19088")
    assert {(r[0], r[1]) for r in _kinds(gens, rows, honda)} == {
        (1, "exact"), (2, "exact"), (3, "exact"), (4, "sibling")}
    view = rg.RecallView(honda["cpsc-recall-19088"])
    pn = next(i for i, f in enumerate(view.fields) if f.name == "product_name")
    assert view.fields[pn].tokens[1:6] == ("eu2200i", "eu2200i", "companion", "and", "eb2200i")
    matcher = rg.Matcher(gens, [])
    assert [sorted(w[2]) for w in matcher._windows(view, pn, ("eu2200i", "eb2200i"))] == [[2, 5]]


def test_a_shorter_name_is_superseded_only_inside_a_longer_mention():
    st = settings_for("p", "home-backup-power")
    rows = [{"product_id": 1, "brand": "HALO", "model_name": "HALO 1000", "category": "station"},
            {"product_id": 2, "brand": "HALO", "model_name": "HALO 1000 Bolt",
             "category": "station"}]
    only_long = _synthetic("HALO Recalls HALO 1000 Bolt Power Stations Due to Fire Hazard")
    assert _kinds(st, rows, {only_long.id: only_long}) == {
        (1, "sibling", "superseded-by-longer-name"), (2, "exact", "model-name")}
    both = _synthetic("HALO Recalls HALO 1000 and HALO 1000 Bolt Power Stations")
    assert _kinds(st, rows, {both.id: both}) == {(1, "exact", "model-name"),
                                                 (2, "exact", "model-name")}


def test_a_model_the_recall_scopes_out_is_never_blocked(review_records):
    """Recall 25-395 prints 'if model number is EFMIS129-B or EFMIS129-C, it is
    not included in this recall': EFMIS129-B goes to review, never blocks; the
    recalled EFMIS129 and EFMIS175 are exact."""
    fridge = settings_for("f", "home-backup-power", categories={
        "fridge": {"patterns": [r"\bmini ?fridges?\b"], "catalog_categories": ["fridge"]}})
    rows = [{"product_id": 1, "brand": "Frigidaire", "model_name": "6-Can Mini Fridge",
             "model_number": "EFMIS129", "category": "fridge"},
            {"product_id": 2, "brand": "Frigidaire", "model_name": "6-Can Mini Fridge B",
             "model_number": "EFMIS129-B", "category": "fridge"},
            {"product_id": 3, "brand": "Frigidaire", "model_name": "9-Can Mini Fridge",
             "model_number": "EFMIS175", "category": "fridge"}]
    assert _kinds(fridge, rows, _one(review_records, "cpsc-recall-25395")) == {
        (1, "exact", "model-number"), (2, "ambiguous", "model-number-excluded"),
        (3, "exact", "model-number")}
    # Its own model number is scoped out: a shared, cleanly named model name
    # does not overrule that.
    shared = [dict(rows[1], model_name="Minifridges")]
    assert _kinds(fridge, shared, _one(review_records, "cpsc-recall-25395")) == {
        (2, "ambiguous", "model-number-excluded")}
    # The inversion the reviewer found: the exclusion sentence used to make
    # the EXCLUDED variant exact and the recalled model a sibling.
    st = settings_for("p", "home-backup-power")
    jackery = [{"product_id": 1, "brand": "Jackery", "model_name": "Explorer 1000",
                "category": "station"},
               {"product_id": 2, "brand": "Jackery", "model_name": "Explorer 1000 Plus",
                "category": "station"}]
    rec = _synthetic("Jackery Recalls Explorer 1000 Portable Power Stations Due to Fire Hazard",
                     "This recall involves the Explorer 1000 portable power station. "
                     "The Explorer 1000 Plus is not included in this recall.")
    assert _kinds(st, jackery, {rec.id: rec}) == {(1, "exact", "model-name"),
                                                  (2, "ambiguous", "model-name-excluded")}
    excl = _synthetic("Jackery Recalls Explorer Portable Power Stations",
                      "This recall involves Explorer stations, excluding the Explorer 1000 "
                      "Plus. The recalled Explorer 1000 units have serial numbers ...")
    assert _kinds(st, jackery, {excl.id: excl}) == {(1, "exact", "model-name"),
                                                   (2, "ambiguous", "model-name-excluded")}
    # A 'which ...' clause about something else is not an exclusion.
    other = _synthetic("Jackery Recalls Explorer 1000 Plus Portable Power Stations",
                       "Explorer 1000 Plus stations were sold with chargers, which are not "
                       "included in this recall.")
    assert (2, "exact", "model-name") in _kinds(st, jackery, {other.id: other})


def test_numbers_match_with_or_without_their_unit():
    st = settings_for("p", "home-backup-power")
    aeiusny = [{"product_id": 1, "brand": "Aeiusny", "model_name": "Portable Power Station 2000",
                "category": "station"}]
    title = _synthetic("CPSC Warns Consumers to Immediately Stop Using Aeiusny 2000W Power "
                       "Stations")
    assert _kinds(st, aeiusny, {title.id: title}) == {(1, "exact", "model-name")}
    delta = [{"product_id": 1, "brand": "EcoFlow", "model_name": "DELTA Max 2000",
              "family": "DELTA Max", "category": "station"}]
    rec = _synthetic("EcoFlow Recalls DELTA Max 2000W Portable Power Stations")
    assert _kinds(st, delta, {rec.id: rec}) == {(1, "exact", "model-name")}
    unit_catalog = [{"product_id": 1, "brand": "Aeiusny", "model_name": "500W Portable Power "
                     "Station", "category": "station"}]
    bare = _synthetic("Aeiusny 500 Portable Power Stations Recalled Due to Fire Hazard")
    assert _kinds(st, unit_catalog, {bare.id: bare}) == {(1, "exact", "model-name")}
    # A rating in running text is weaker than a name: review, never a block.
    rating = _synthetic("Aeiusny Recalls Portable Power Stations",
                        "The recalled stations have a 2000W pure sine wave inverter.")
    assert _kinds(st, aeiusny, {rating.id: rating}) == {(1, "ambiguous", "model-name-rating")}
    # Watts are not watt-hours.
    wh = _synthetic("Aeiusny Recalls 1000Wh Portable Power Stations")
    watt_row = [{"product_id": 1, "brand": "Aeiusny", "model_name": "1000W Portable Power "
                 "Station", "category": "station"},
                {"product_id": 2, "brand": "Aeiusny", "model_name": "Explorer 7",
                 "category": "station"}]
    assert (1, "exact", "model-name") not in _kinds(st, watt_row, {wh.id: wh})


ACME = [{"product_id": 1, "brand": "Acme", "model_name": "PowerMax 1000", "category": "station"},
        {"product_id": 2, "brand": "Acme", "model_name": "PowerMax 2000", "category": "station"},
        {"product_id": 3, "brand": "Acme", "model_name": "Desk Lamp", "category": "lamp"}]


@pytest.mark.parametrize("title,description", [
    ("Acme Recalls Portable Power Stations", "This recall involves all Acme power stations."),
    ("Acme Recalls LiFePO4 Portable Power Stations", "All Acme power stations."),
    ("Acme Recalls Portable Power Stations", "All Acme stations with 1800W AC outlets."),
])
def test_brand_wide_net_ignores_generic_words_and_description_specs(title, description):
    rec = _synthetic(title, description)
    assert _kinds(settings_for("p", "home-backup-power"), ACME, {rec.id: rec}) == {
        (1, "ambiguous", "brand-wide"), (2, "ambiguous", "brand-wide")}


def test_a_recall_naming_models_we_cannot_map_queues_the_brand():
    rec = _synthetic("Acme Recalls 1800W Portable Power Stations Due to Fire Hazard")
    st = settings_for("p", "home-backup-power")
    assert _kinds(st, ACME, {rec.id: rec}) == {(1, "ambiguous", "unmapped-model"),
                                               (2, "ambiguous", "unmapped-model")}
    quiet = settings_for("p", "home-backup-power", matching={"unmapped_models": "ignore"})
    assert _kinds(quiet, ACME, {rec.id: rec}) == set()
    # Mapped: the recall names PowerMax 2000, so PowerMax 1000 is not queued
    # (it has a name of its own, not just a number).
    named = _synthetic("Acme Recalls PowerMax 2000 Portable Power Stations")
    assert _kinds(st, ACME, {named.id: named}) == {(2, "exact", "model-name"),
                                                   (1, "sibling", "family")}


def test_identifier_evidence_does_not_depend_on_the_category():
    """A model number named by the recalled brand is exact even when the
    title misses the profile's category patterns; a NAME on such a recall
    still goes to review (out_of_category)."""
    st = settings_for("p", "home-backup-power")
    rows = [{"product_id": 1, "brand": "EcoFlow", "model_name": "DELTA Pro Ultra",
             "model_number": "EFD500", "category": "station"},
            {"product_id": 2, "brand": "EcoFlow", "model_name": "Smart Panel 2",
             "category": "station"}]
    rec = _synthetic("EcoFlow Recalls Smart Home Panel 2 Units Due to Fire Hazard",
                     "This recall involves systems sold with model EFD500 inverters.")
    assert match_table(st, rows, {rec.id: rec}) >= {
        (rec.id, 1, "exact", "model-number", "stop-use", "active", None)}
    assert (2, "ambiguous", "out-of-category") in _kinds(st, rows, {rec.id: rec})


def test_home_energy_storage_recalls_are_in_the_power_niche(review_records):
    st = settings_for("p", "home-backup-power")
    m = rg.Matcher(st, [])
    for rid in ("cpsc-recall-23705", "cpsc-recall-22058"):
        assert m.categories_for(rg.RecallView(review_records[rid])) == ("station",), rid
    lg = [{"product_id": 1, "brand": "LG Energy Solution", "model_name": "RESU 10H",
           "model_number": "RESU10H", "category": "station"},
          {"product_id": 2, "brand": "LG Energy Solution", "model_name": "RESU 16H Prime",
           "category": "station"}]
    assert _kinds(st, lg, _one(review_records, "cpsc-recall-23705")) == {
        (1, "exact", "model-number"), (2, "sibling", "family")}


def test_a_strong_model_number_of_an_unnamed_brand_goes_to_review():
    """Catalog brand 'Anker SOLIX' vs a recall that says 'Anker': the model
    number still surfaces the row (review), it is never dropped."""
    st = settings_for("p", "home-backup-power")
    rows = [{"product_id": 1, "brand": "Anker SOLIX", "model_name": "C1000",
             "model_number": "A1761", "category": "station"},
            {"product_id": 2, "brand": "Rockpals", "model_name": "250W Station",
             "model_number": "RP250", "category": "station"}]
    rec = _synthetic("Anker Recalls Portable Power Stations Due to Fire Hazard",
                     "This recall involves Anker portable power stations with model number "
                     "A1761. Chargers for model RP250 are not affected.")
    assert _kinds(st, rows, {rec.id: rec}) == {(1, "ambiguous", "model-number-unnamed-brand")}


def test_a_kit_component_is_checked_against_the_components_own_brand():
    st = settings_for("p", "home-backup-power")
    rows = [{"product_id": 1, "brand": "EcoFlow", "model_name": "DELTA Max 2000",
             "model_number": "EFD310", "category": "station"},
            {"product_id": 2, "brand": "Renogy", "model_name": "Solar Generator Kit",
             "components": ["EFD310"], "category": "kit"},
            {"product_id": 3, "brand": "Renogy", "model_name": "Off-Grid Kit",
             "components": ["XQ12345"], "category": "kit"}]
    rec = _synthetic("EcoFlow Recalls DELTA Max Power Stations Due to Fire Hazard",
                     "This recall involves EcoFlow model EFD310.")
    got = match_table(st, rows, {rec.id: rec})
    assert {(r[1], r[2], r[3]) for r in got} == {(1, "exact", "model-number"),
                                                 (2, "exact", "component-model-number")}
    unnamed = _synthetic("Acme Recalls Portable Power Stations",
                         "Kits containing station XQ12345 are affected.")
    assert _kinds(st, rows, {unnamed.id: unnamed}) == {
        (3, "ambiguous", "model-number-unnamed-brand")}


def test_a_unit_count_is_never_a_model():
    st = settings_for("p", "home-backup-power")
    rows = [{"product_id": 1, "brand": "Acme", "model_name": "Volt 7", "model_number": "25030",
             "category": "station"},
            {"product_id": 2, "brand": "Acme", "model_name": "Acme 1000", "category": "station"}]
    rec = _synthetic("Acme Recalls PowerMax 3000 Portable Power Stations",
                     "About 25,030 units were sold. About 1,000 units were sold in Canada. "
                     "This recall involves Acme PowerMax 3000 power stations.")
    got = _kinds(st, rows, {rec.id: rec})
    assert not {k for k in got if k[1] == "exact"}, got


def test_matching_is_deterministic(records):
    st = settings_for("power-site", "home-backup-power")
    a = match_table(st, catalog_rows("home_backup_power"), records)
    b = match_table(st, list(reversed(catalog_rows("home_backup_power"))), dict(
        reversed(list(records.items()))))
    assert a == b
    items, _ = rg.catalog_from_rows(catalog_rows("home_backup_power"))
    m1 = rg.Matcher(st, items).match(rg.RecallView(records[ECOFLOW]))[1]
    m2 = rg.Matcher(st, items).match(rg.RecallView(records[ECOFLOW]))[1]
    assert [rg._stable_json(dict(x.evidence)) for x in m1] == \
           [rg._stable_json(dict(x.evidence)) for x in m2]


def test_catalog_rows_are_validated():
    items, problems = rg.catalog_from_rows([
        {"product_id": 1, "brand": "EcoFlow", "aliases": '["EFD-310", "Delta Max (2000)"]',
         "upcs": "{0123456789012}", "asin": "b0rgtest01"},
        {"brand": "EcoFlow"},
        {"product_id": 3, "brand": ""},
    ])
    assert len(items) == 1 and len(problems) == 2
    it = items[0]
    assert it.aliases == ("EFD-310", "Delta Max (2000)") and it.upcs == ("0123456789012",)
    assert it.asin == "B0RGTEST01" and it.key == "m:|p:1"


def test_catalog_label_column_is_ignored():
    """Nothing but our own brand + model_name is stored (Amazon licence): a
    `label` column a site maps to an offer title never reaches the gate."""
    items, _ = rg.catalog_from_rows([{"product_id": 1, "brand": "EcoFlow",
                                      "model_name": "DELTA Max 2000",
                                      "label": "Amazon offer title 2000W ..."}])
    assert items[0].display == "EcoFlow DELTA Max 2000"
    assert "label" not in {f.name for f in rg.dc_fields(rg.CatalogItem)}


# ---------------------------------------------------------------------------
# Config, DSN, email addresses
# ---------------------------------------------------------------------------

def test_repo_config_ships_profiles_and_no_site():
    doc = repo_config_doc()
    cfg = rg.resolve_config(doc)
    assert set(cfg["profiles"]) == {"home-backup-power", "cleaning-robots"}
    assert cfg["by_site"] == {} and rg.subscribed_sites(cfg) == []
    assert rg.validate_config(cfg) == []
    text = rg.REPO_CONFIG_PATH.read_text()
    assert "@" not in text  # recipients come from env var names, never literals


def test_site_settings_problems():
    cfg = config_with_site("s", "nope", catalog_sql="")
    _, problems = rg.site_settings(cfg, "s")
    joined = " | ".join(problems)
    assert "unknown profile" in joined and "catalog_sql is required" in joined
    assert "no categories" in joined
    cfg = config_with_site("s", "home-backup-power", tables={"recalls": "x; drop table y"},
                           categories={"c": {"patterns": ["(unclosed"]}},
                           tiers={"rules": [{"id": "bad", "when": {}, "tier": "review"}]})
    _, problems = rg.site_settings(cfg, "s")
    joined = " | ".join(problems)
    assert "unsafe identifier" in joined and "bad regex" in joined and "tier must be" in joined
    assert rg.validate_config(cfg)
    with pytest.raises(rg.RecallGateConfigError):
        rg.site_settings(cfg, "missing")


def test_storage_config_replaces_repo_default(storage):
    assert rg.load_file_config(storage)["profiles"]  # repo default
    storage.write_json(rg.CONFIG_KEY, {"enabled": False, "by_site": {}})
    cfg = rg.resolve_config(storage=storage)
    assert cfg["enabled"] is False and cfg["profiles"] == {}


def test_dsn_name_from_entry_or_site_registry(storage, monkeypatch):
    from framework.core import site_registry
    site_registry.clear_cache()
    storage.write_json(site_registry.CONFIG_KEY, {
        "schema_version": "1", "sites": [{
            "name": "powersite", "domain": "powersite.example", "repo_path": "/srv/powersite",
            "branch": "main", "dsn_env": "POWERSITE_DB_URL",
            "dsn_env_aliases": ["POWERSITE_DSN"]}]})
    try:
        st = settings_for("powersite", "home-backup-power", dsn_env="")
        assert rg.resolve_dsn_env_names(st, storage=storage) == ["POWERSITE_DB_URL"]
        st2 = settings_for("powersite", "home-backup-power", dsn_env="",
                           dsn_include_aliases=True)
        assert rg.resolve_dsn_env_names(st2, storage=storage) == ["POWERSITE_DB_URL",
                                                                   "POWERSITE_DSN"]
        dsn, names = rg.resolve_dsn(st2, storage=storage,
                                    environ={"POWERSITE_DSN": "postgresql://x"})
        assert dsn == "postgresql://x" and names[0] == "POWERSITE_DB_URL"
        explicit = settings_for("powersite", "home-backup-power", dsn_env="OTHER_DSN")
        assert rg.resolve_dsn_env_names(explicit, storage=storage) == ["OTHER_DSN"]
        unknown = settings_for("not-registered", "home-backup-power", dsn_env="")
        assert rg.resolve_dsn_env_names(unknown, storage=storage) == []
    finally:
        site_registry.clear_cache()


def test_email_addresses_from_env_names_or_literals():
    cfg = rg.DEFAULTS["email"]
    env = {"OPERATOR_EMAIL": "ops@example.com", "OPERATOR_FROM_EMAIL": "bot@example.com"}
    assert rg.email_addresses(cfg, env) == (["ops@example.com"], "bot@example.com")
    env["FRAMEWORK_OPERATOR_EMAIL"] = "first@example.com"
    assert rg.email_addresses(cfg, env)[0] == ["first@example.com"]
    assert rg.email_addresses(cfg, {}) == ([], "")
    literal = {**cfg, "to": ["lit@example.com"], "from": "litfrom@example.com"}
    assert rg.email_addresses(literal, env) == (["lit@example.com"], "litfrom@example.com")
    env["IMPLEMENTER_FROM"] = "implementer@example.com"
    assert rg.email_addresses(cfg, env)[1] == "implementer@example.com"


def test_sender_precedence_matches_the_fleet_operator_alerts():
    """resilience.notify_operator (and dispatch) prefer IMPLEMENTER_FROM over
    OPERATOR_FROM_EMAIL; the gate and its repo config must agree."""
    import inspect
    from framework.core import resilience
    src = inspect.getsource(resilience.notify_operator)
    assert src.index('"IMPLEMENTER_FROM"') < src.index('"OPERATOR_FROM_EMAIL"')
    assert rg.DEFAULTS["email"]["from_env"] == ["IMPLEMENTER_FROM", "OPERATOR_FROM_EMAIL"]
    assert repo_config_doc()["email"]["from_env"] == rg.DEFAULTS["email"]["from_env"]


def test_unreadable_storage_config_fails_closed(storage):
    """An archived or corrupt storage copy must not fall back to the repo
    default (whose by_site is empty: that would switch the gate off)."""
    storage.write_bytes(rg.CONFIG_KEY, b"{not json")
    with pytest.raises(rg.RecallGateConfigError, match="could not be read"):
        rg.resolve_config(storage=storage)
    storage.write_json(rg.CONFIG_KEY, ["not", "an", "object"])
    with pytest.raises(rg.RecallGateConfigError, match="not a JSON object"):
        rg.resolve_config(storage=storage)

    class Archived:
        def read_json(self, key):
            return None  # AzureBlobStorage reads an archived blob as None

        def exists(self, key):
            return True
    with pytest.raises(rg.RecallGateConfigError):
        rg.load_file_config(Archived())
    storage.delete(rg.CONFIG_KEY)
    assert rg.load_file_config(storage)["profiles"]  # no storage copy: repo default


def test_new_matching_options_are_validated():
    cfg = config_with_site("s", "home-backup-power",
                           matching={"unmapped_models": "maybe", "exclusion_after_regex": "("})
    _, problems = rg.site_settings(cfg, "s")
    joined = " | ".join(problems)
    assert "unmapped_models" in joined and "exclusion_after_regex" in joined


def test_cli_config_reads_the_site_registry_storage_override(storage, capsys):
    """A new site is registered only in storage and leaves dsn_env empty (the
    deferred-wiring recipe): the CLI must find its DSN env var name the way
    the agent does."""
    from framework.core import site_registry
    site_registry.clear_cache()
    storage.write_json(site_registry.CONFIG_KEY, {
        "schema_version": "1", "sites": [{
            "name": "powersite", "domain": "powersite.example", "repo_path": "/srv/powersite",
            "branch": "main", "dsn_env": "POWERSITE_REVIEW_DSN"}]})
    doc = repo_config_doc()
    doc["by_site"] = {"powersite": {"enabled": True, "profile": "home-backup-power",
                                    "catalog_sql": "SELECT 1", "dsn_env": ""}}
    storage.write_json(rg.CONFIG_KEY, doc)
    try:
        assert rg.main(["config", "--site", "powersite"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["dsn_env_names"] == ["POWERSITE_REVIEW_DSN"]
        st, _ = rg.site_settings(rg.resolve_config(storage=storage), "powersite")
        assert rg.site_hosts(st, storage=storage) == ["powersite.example",
                                                      "www.powersite.example"]
    finally:
        site_registry.clear_cache()


# ---------------------------------------------------------------------------
# Sources (fake HTTP; no network)
# ---------------------------------------------------------------------------

NOW = dt.datetime(2026, 11, 17, 12, 0, tzinfo=dt.timezone.utc)


def _fast_cfg(**over):
    cfg = rg.resolve_config(repo_config_doc())
    cfg["http"]["retries"] = 1
    cfg["http"]["retry_base_delay_s"] = 0.0
    for name in (rg.SOURCE_RECALL_CSV, rg.SOURCE_WARNING_CSV):
        cfg["sources"][name]["min_rows"] = 1
    return rg._merge(cfg, over)


def test_api_window_first_run_then_incremental():
    http = FakeHttp()
    f = rg.SourceFetcher(_fast_cfg(), http_get=http, now=NOW, cache_dir="")
    first = f.fetch_api({})
    assert first.ok and len(first.records) == 11
    assert first.state["window_start"] == "2024-11-17"  # 730 days back
    f.fetch_api({"last_success_at": "2026-11-16T12:00:00+00:00"})
    f.fetch_api({"last_success_at": "2026-09-01T12:00:00+00:00"})  # long outage: widen
    starts = [q["LastPublishDateStart"][0] for q in http.api_queries()]
    assert starts == ["2024-11-17", "2026-10-27", "2026-08-30"]
    assert all(q["LastPublishDateEnd"] == ["2026-11-17"] and q["format"] == ["json"]
               for q in http.api_queries())


def test_api_backfill_reads_the_long_window_again():
    http = FakeHttp()
    f = rg.SourceFetcher(_fast_cfg(), http_get=http, now=NOW, cache_dir="")
    prior = {"last_success_at": "2026-11-16T12:00:00+00:00"}
    r = f.fetch_api(prior, backfill=True)
    assert r.ok and r.state["backfill"] is True and r.state["window_start"] == "2024-11-17"
    assert len(r.records) == 11  # FakeHttp honours the window: every fixture record
    r2 = f.fetch_api(prior)
    assert r2.state["backfill"] is False and r2.records == []  # 21 days: nothing that old
    assert f.fetch_all({rg.SOURCE_API: prior}, backfill=True)[rg.SOURCE_API].state["backfill"]


def test_api_extra_filters_are_passed_through():
    http = FakeHttp()
    cfg = _fast_cfg(sources={rg.SOURCE_API: {"params": {"ProductType": "Batteries",
                                                        "format": "xml"}}})
    rg.SourceFetcher(cfg, http_get=http, now=NOW, cache_dir="").fetch_api({})
    [q] = http.api_queries()
    assert q["ProductType"] == ["Batteries"] and q["format"] == ["json"]  # format is fixed


def test_csv_conditional_get_uses_the_cache(tmp_path):
    http = FakeHttp()
    f = rg.SourceFetcher(_fast_cfg(), http_get=http, now=NOW, cache_dir=str(tmp_path))
    r1 = f.fetch_csv(rg.SOURCE_RECALL_CSV, {}, rg.parse_recall_csv)
    assert r1.ok and not r1.not_modified and len(r1.records) == 15
    assert (tmp_path / f"{rg.SOURCE_RECALL_CSV}.csv").is_file()
    state = rg.next_source_state({}, r1, NOW)
    r2 = f.fetch_csv(rg.SOURCE_RECALL_CSV, state, rg.parse_recall_csv)
    assert r2.ok and r2.not_modified and len(r2.records) == 15
    assert http.calls[-1][1]["If-Modified-Since"] == http.last_modified
    # No cache file: never send validators (a 304 would leave us with nothing).
    f2 = rg.SourceFetcher(_fast_cfg(), http_get=http, now=NOW, cache_dir=str(tmp_path / "x"))
    f2.fetch_csv(rg.SOURCE_RECALL_CSV, state, rg.parse_recall_csv)
    assert "If-Modified-Since" not in http.calls[-1][1]


def test_source_failures_keep_the_last_success(tmp_path):
    http = FakeHttp()
    http.fail[rg.SOURCE_RECALL_CSV] = 503
    f = rg.SourceFetcher(_fast_cfg(), http_get=http, now=NOW, cache_dir=str(tmp_path))
    res = f.fetch_all({})
    assert res[rg.SOURCE_API].ok and res[rg.SOURCE_WARNING_CSV].ok
    bad = res[rg.SOURCE_RECALL_CSV]
    assert not bad.ok and "503" in bad.error
    # 5xx is retried (1 retry configured) before giving up.
    assert sum(1 for u, _ in http.calls if http.source_of(u) == rg.SOURCE_RECALL_CSV) == 2
    prior = {"last_success_at": "2026-11-16T06:15:00+00:00", "sha256": "abc"}
    st = rg.next_source_state(prior, bad, NOW)
    assert st["last_success_at"] == prior["last_success_at"] and "503" in st["last_error"]
    states = {rg.SOURCE_API: rg.next_source_state({}, res[rg.SOURCE_API], NOW),
              rg.SOURCE_RECALL_CSV: st,
              rg.SOURCE_WARNING_CSV: rg.next_source_state({}, res[rg.SOURCE_WARNING_CSV], NOW)}
    assert rg.sync_age_hours(states, f.enabled(), NOW) == 29.75
    assert rg.sync_age_hours({}, f.enabled(), NOW) == 9999.0
    assert rg.sync_age_hours({}, [], NOW) is None


def test_a_non_https_source_fails_alone(tmp_path):
    http = FakeHttp()
    cfg = _fast_cfg()
    cfg["sources"][rg.SOURCE_WARNING_CSV]["url"] = "http://example.com/warnings.csv"
    res = rg.SourceFetcher(cfg, http_get=http, now=NOW, cache_dir=str(tmp_path)).fetch_all({})
    assert not res[rg.SOURCE_WARNING_CSV].ok and "https" in res[rg.SOURCE_WARNING_CSV].error
    assert res[rg.SOURCE_API].ok and res[rg.SOURCE_RECALL_CSV].ok
    assert not any(u.startswith("http://") for u, _ in http.calls)
    assert rg.validate_config(cfg) == [f"sources.{rg.SOURCE_WARNING_CSV}.url must be an https URL"]


def test_bad_buy_button_marker_is_a_config_problem():
    cfg = config_with_site("s", "home-backup-power",
                           buy_button={"http": {"enabled": True, "markers": ["(oops"]}})
    _, problems = rg.site_settings(cfg, "s")
    assert any("buy_button.http.markers" in p for p in problems)


def test_truncated_csv_is_rejected(tmp_path):
    http = FakeHttp()
    cfg = _fast_cfg()
    cfg["sources"][rg.SOURCE_RECALL_CSV]["min_rows"] = 1000
    r = rg.SourceFetcher(cfg, http_get=http, now=NOW, cache_dir=str(tmp_path)).fetch_csv(
        rg.SOURCE_RECALL_CSV, {}, rg.parse_recall_csv)
    assert not r.ok and "truncated" in r.error
    assert not (tmp_path / f"{rg.SOURCE_RECALL_CSV}.csv").exists()  # never cached


# ---------------------------------------------------------------------------
# Schema, email rendering
# ---------------------------------------------------------------------------

def test_schema_sql_renders_and_rejects_unsafe_names():
    sql = rg.schema_sql()
    for needle in ("CREATE TABLE IF NOT EXISTS recalls", "CREATE TABLE IF NOT EXISTS recall_matches",
                   "match_key         text NOT NULL UNIQUE", "VIEW v_recall_buy_blocks",
                   "VIEW v_recall_pick_exclusions", "VIEW v_recall_notices"):
        assert needle in sql
    custom = rg.schema_sql({"recalls": "safety.recalls", "matches": "safety.recall_matches",
                            "sync_log": "safety.recall_sync_log"})
    assert "VIEW safety.v_recall_buy_blocks" in custom and "REFERENCES safety.recalls" in custom
    with pytest.raises(rg.RecallGateConfigError):
        rg.schema_sql({"recalls": "recalls; DROP TABLE x"})


def test_match_email_escapes_and_explains(records):
    row = {"match_key": f"{ECOFLOW}|m:101|p:1", "recall_id": ECOFLOW, "model_id": 101,
           "product_id": 1, "item_label": "EcoFlow <DELTA> Max 2000", "match_kind": "exact",
           "tier": "repair-hold", "proposed_tier": "repair-hold", "status": "active",
           "hold_until": dt.date(2026, 1, 7), "reason": "model number EFD310 ...",
           "evidence": {"rule": "model-number"}, "title": records[ECOFLOW].title,
           "url": records[ECOFLOW].url, "number": "26-010", "kind": "recall",
           "published_at": dt.date(2025, 10, 9), "remedy": "firmware", "hazard": "fire",
           "units": "About 25,030"}
    subject, body = rg.render_match_email("power-site", row, today=dt.date(2025, 12, 1))
    assert subject.startswith("[recall-gate] power-site: REPAIR-HOLD - EcoFlow <DELTA> Max 2000")
    assert "26-010" in subject
    assert "&lt;DELTA&gt;" in body and "<DELTA>" not in body
    assert "Buy button blocked" in body and "--decision confirm|clear" in body
    _, later = rg.render_match_email("power-site", row, today=dt.date(2026, 11, 17))
    assert "Recall notice only" in later  # the hold has ended
    review = {**row, "match_kind": "sibling", "tier": "review", "status": "manual-review"}
    s2, b2 = rg.render_match_email("power-site", review, today=dt.date(2025, 12, 1))
    assert "REVIEW (sibling)" in s2 and "Manual review needed" in b2


def test_review_request_and_unverified_emails_explain_themselves(records):
    row = {"match_key": f"{ECOFLOW}|m:101|p:1", "recall_id": ECOFLOW, "model_id": 101,
           "product_id": 1, "item_label": "EcoFlow DELTA Max 2000", "match_kind": "exact",
           "tier": "stop-use", "proposed_tier": "stop-use", "status": "active",
           "hold_until": None, "reason": "model number EFD310", "evidence": {"rule": "x"},
           "title": "t", "url": "", "number": "26-010", "kind": "recall",
           "review_requested_at": dt.datetime(2026, 11, 17, tzinfo=dt.timezone.utc),
           "gate_proposal": {"why": "gate-would-lift", "match_kind": "sibling",
                             "rule": "family", "reason": "same line"}}
    subject, body = rg.render_match_email("s", row, today=dt.date(2026, 11, 17))
    assert "REVIEW REQUESTED" in subject and "will not do that itself" in body
    assert "Still blocked" in body
    cleared = {**row, "status": "manual-review", "tier": "review",
               "gate_proposal": json.dumps({"why": "stronger-evidence-after-clear",
                                            "cleared_by": "ops", "cleared_kind": "sibling",
                                            "match_kind": "exact", "rule": "model-number",
                                            "reason": "r"})}
    _, body2 = rg.render_match_email("s", cleared, today=dt.date(2026, 11, 17))
    assert "A match you cleared has stronger evidence" in body2 and "ops" in body2
    s3, b3 = rg.render_buy_button_alert("s", [], unverified=[{"key": "m:1|p:1", "label": "X",
                                                              "why": "HTTP 403"}])
    assert "could not be verified" in s3 and "HTTP 403" in b3


# ---------------------------------------------------------------------------
# The update planner (no database): the gate never lifts a block by itself
# ---------------------------------------------------------------------------

T0 = dt.datetime(2026, 11, 17, 6, 15, tzinfo=dt.timezone.utc)
ITEM = rg.CatalogItem(product_id=1, model_id=101, brand="EcoFlow", model_name="DELTA Max 2000")


def _result(kind, tier, status, hold=None, rule="model-number"):
    return rg.MatchResult(recall_id=ECOFLOW, item=ITEM, match_kind=kind, rule=rule, tier=tier,
                          proposed_tier="stop-use", status=status, hold_until=hold,
                          reason="r", evidence={"rule": rule})


def _row(kind, tier, status, hold=None, **extra):
    return {"match_key": f"{ECOFLOW}|{ITEM.key}", "item_label": ITEM.display,
            "match_kind": kind, "tier": tier, "proposed_tier": "stop-use", "status": status,
            "hold_until": hold, "reason": "r", "evidence": {"rule": "model-number"},
            "matcher_version": rg.MATCHER_VERSION, "reviewer": None, "reviewed_at": None,
            "stale_since": None, "review_requested_at": None, "gate_proposal": None, **extra}


def test_plan_applies_escalations_and_changes_nothing_when_equal():
    today = T0.date()
    old = _row("sibling", "review", "manual-review")
    new = _result("exact", "stop-use", "active")
    plan = rg.plan_match_update(old, new, today=today, now=T0)
    assert plan["match_kind"] == "exact" and plan["status"] == "active"
    same = _row("exact", "stop-use", "active")
    assert rg.plan_match_update(same, new, today=today, now=T0) == {}


@pytest.mark.parametrize("new", [
    _result("sibling", "review", "manual-review", rule="family"),        # demoted to review
    _result("exact", "repair-hold", "active", hold="2026-01-07"),         # hold already over
])
def test_plan_never_lifts_a_block_by_itself(new):
    old = _row("exact", "stop-use", "active")
    plan = rg.plan_match_update(old, new, today=T0.date(), now=T0)
    assert not {"match_kind", "tier", "status", "hold_until"} & set(plan)
    assert plan["review_requested_at"] == T0
    assert plan["gate_proposal"]["why"] == "gate-would-lift"
    assert plan["gate_proposal"]["status"] == new.status
    # Held: the next run keeps it, and withdraws the request once the gate
    # agrees with the stored decision again.
    held = {**old, **plan}
    again = rg.plan_match_update(held, new, today=T0.date(), now=T0)
    assert again == {}
    back = rg.plan_match_update(held, _result("exact", "stop-use", "active"),
                                today=T0.date(), now=T0)
    assert back["review_requested_at"] is None and back["gate_proposal"] is None
    assert not {"match_kind", "tier", "status", "hold_until"} & set(back)


def _blocking(row):
    return rg.is_blocking(row["tier"], row["status"], row["hold_until"], T0.date())


def test_plan_keeps_human_decisions_but_blocks_over_a_clear_with_stronger_evidence():
    confirmed = _row("sibling", "repair-hold", "active", reviewer="ops")
    plan = rg.plan_match_update(confirmed, _result("ambiguous", "review", "manual-review"),
                                today=T0.date(), now=T0)
    assert not {"match_kind", "tier", "status", "reviewer", "review_requested_at"} & set(plan)
    cleared = _row("sibling", "review", "cleared", reviewer="ops")
    same_kind = rg.plan_match_update(cleared, _result("sibling", "review", "manual-review"),
                                     today=T0.date(), now=T0)
    assert not {"status", "reviewer"} & set(same_kind)
    # Stronger evidence that blocks: applied over the clear (the gate always
    # applies a stricter block), the human asked again, the clear recorded.
    stronger = rg.plan_match_update(cleared, _result("exact", "stop-use", "active"),
                                    today=T0.date(), now=T0)
    after = {**cleared, **stronger}
    assert (after["status"], after["tier"], after["reviewer"], after["match_kind"],
            after["review_requested_at"]) == ("active", "stop-use", None, "exact", T0)
    assert _blocking(after)
    prop = stronger["gate_proposal"]
    assert prop["why"] == "gate-escalated-over-review" and prop["cleared_by"] == "ops"
    assert prop["human_decision"]["status"] == "cleared"
    # The request carries a human decision: it stays until a human decides,
    # even though the gate now agrees with the stored state.
    assert rg.plan_match_update(after, _result("exact", "stop-use", "active"),
                                today=T0.date(), now=T0) == {}
    # Stronger evidence that does not block (the hold is over): back to the
    # queue, buy button unchanged, as before.
    ended = _result("exact", "repair-hold", "active", hold="2026-01-07")
    requeue = rg.plan_match_update(cleared, ended, today=T0.date(), now=T0)
    queued = {**cleared, **requeue}
    assert (queued["status"], queued["tier"], queued["reviewer"], queued["match_kind"]) == (
        "manual-review", "review", None, "exact")
    assert requeue["gate_proposal"]["why"] == "stronger-evidence-after-clear"
    assert rg.plan_match_update(queued, ended, today=T0.date(), now=T0).keys() <= {"gate_proposal"}
    # ... and when the evidence later blocks, that is applied too.
    esc = rg.plan_match_update(queued, _result("exact", "stop-use", "active"),
                               today=T0.date(), now=T0)
    later = {**queued, **esc}
    assert _blocking(later) and later["review_requested_at"] == T0
    assert later["gate_proposal"]["why"] == "gate-escalated-over-review"
    assert later["gate_proposal"]["cleared_by"] == "ops"


def test_plan_applies_a_stricter_block_over_a_pending_gate_request():
    """An active repair hold that ended, waiting on a gate review request
    (the gate stopped finding it for a run); the recall is re-announced as a
    refund (stop-use): the block is applied and the moot request withdrawn."""
    for why in ("gate-no-longer-matches", "gate-would-lift"):
        old = _row("exact", "repair-hold", "active", hold="2026-01-07",
                   proposed_tier="repair-hold", review_requested_at=T0 - dt.timedelta(days=1),
                   gate_proposal={"why": why})
        assert not _blocking(old)
        plan = rg.plan_match_update(old, _result("exact", "stop-use", "active"),
                                    today=T0.date(), now=T0)
        after = {**old, **plan}
        assert (after["tier"], after["hold_until"]) == ("stop-use", None) and _blocking(after)
        assert after["review_requested_at"] is None and after["gate_proposal"] is None
        assert rg.needs_email({**after, "notified_key": rg.notification_key(old)}, T0.date())
    # A pending request whose result does not block harder keeps the decision.
    old = _row("exact", "stop-use", "active", review_requested_at=T0,
               gate_proposal={"why": "gate-would-lift"})
    plan = rg.plan_match_update(old, _result("sibling", "review", "manual-review"),
                                today=T0.date(), now=T0)
    assert not {"match_kind", "tier", "status", "hold_until", "review_requested_at"} & set(plan)


def test_plan_applies_a_recall_escalation_to_a_confirmed_match():
    """A human confirmed a sibling as the recalled product (repair hold,
    over); CPSC re-announces the recall as stop-use. The stricter tier
    reaches the confirmed row, the reviewer stays, a review is requested and
    emailed. It fires once: a deliberate lower tier afterwards stands."""
    confirmed = _row("sibling", "repair-hold", "active", hold="2026-01-07",
                     proposed_tier="repair-hold", reviewer="ops",
                     reviewed_at=T0 - dt.timedelta(days=30))
    gate_now = replace(_result("sibling", "review", "manual-review", rule="family"),
                       proposed_tier="stop-use")
    plan = rg.plan_match_update(confirmed, gate_now, today=T0.date(), now=T0)
    after = {**confirmed, **plan}
    assert (after["tier"], after["status"], after["hold_until"], after["reviewer"]) == (
        "stop-use", "active", None, "ops")
    assert _blocking(after) and after["review_requested_at"] == T0
    prop = after["gate_proposal"]
    assert prop["why"] == "gate-escalated-over-review"
    assert (prop["recall_tier_was"], prop["recall_tier"]) == ("repair-hold", "stop-use")
    assert prop["human_decision"]["tier"] == "repair-hold"
    assert rg.plan_match_update(after, gate_now, today=T0.date(), now=T0) == {}
    # The human re-confirms repair-hold on purpose (review_match): kept.
    reconfirmed = {**after, "tier": "repair-hold", "hold_until": dt.date(2026, 1, 7),
                   "review_requested_at": None, "gate_proposal": None}
    assert rg.plan_match_update(reconfirmed, gate_now, today=T0.date(), now=T0) == {}
    # The same recall escalation reaches a gate-owned row the gate would
    # otherwise demote: the block stays AND becomes stop-use.
    gate_row = _row("exact", "repair-hold", "active", hold="2026-01-07",
                    proposed_tier="repair-hold")
    plan = rg.plan_match_update(gate_row, gate_now, today=T0.date(), now=T0)
    after = {**gate_row, **plan}
    assert (after["tier"], after["status"]) == ("stop-use", "active") and _blocking(after)
    assert after["gate_proposal"]["why"] == "gate-would-lift"


def test_plan_stale_update():
    row = _row("exact", "repair-hold", "active", hold="2026-01-07", proposed_tier="repair-hold",
               model_id=101, product_id=1, recall_id=ECOFLOW)
    # Unchanged recall: a listed product asks a human, an unlisted one nobody.
    listed = rg.plan_stale_update(row, recall_tier="repair-hold", listed=True, now=T0)
    assert listed["gate_proposal"]["why"] == "gate-no-longer-matches"
    assert rg.plan_stale_update(row, recall_tier="repair-hold", listed=False, now=T0) == {}
    # The recall escalates while the gate no longer finds the match: the
    # active decision still takes the stricter tier.
    gone = rg.plan_stale_update(row, recall_tier="stop-use", listed=False, now=T0)
    assert gone == {"proposed_tier": "stop-use", "tier": "stop-use", "hold_until": None}
    human = {**row, "reviewer": "ops"}
    asked = rg.plan_stale_update(human, recall_tier="stop-use", listed=True, now=T0)
    assert asked["tier"] == "stop-use" and asked["review_requested_at"] == T0
    assert asked["gate_proposal"]["why"] == "gate-escalated-over-review"
    assert rg.plan_stale_update(human, recall_tier="repair-hold", listed=True, now=T0) == {}
    # Idempotent once written; a cleared or review row never gains a tier.
    assert rg.plan_stale_update({**row, **gone}, recall_tier="stop-use", listed=False,
                                now=T0) == {}
    review = _row("sibling", "review", "manual-review", proposed_tier="repair-hold")
    assert rg.plan_stale_update(review, recall_tier="stop-use", listed=True, now=T0) == {
        "proposed_tier": "stop-use"}


@pytest.mark.parametrize("prev,row,expected", [
    (None, {"tier": "review", "status": "manual-review"}, True),
    ("sibling/review/manual-review//", {"match_kind": "sibling", "tier": "review",
                                         "status": "manual-review", "hold_until": None}, False),
    # escalation into a confirmed match (hold already over): still an email
    ("sibling/review/manual-review//", {"match_kind": "exact", "tier": "repair-hold",
                                         "status": "active", "hold_until": "2026-01-07"}, True),
    # becomes blocking (a later hold anchor moved the hold out)
    ("exact/repair-hold/active/2026-01-07/", {"match_kind": "exact", "tier": "repair-hold",
                                               "status": "active",
                                               "hold_until": "2027-02-15"}, True),
    # still blocking, different tier: bookkeeping only
    ("exact/stop-use/active//", {"match_kind": "exact", "tier": "repair-hold",
                                 "status": "active", "hold_until": "2027-02-15"}, False),
    # the gate asks for a human
    ("exact/stop-use/active//", {"match_kind": "exact", "tier": "stop-use", "status": "active",
                                 "review_requested_at": T0}, True),
])
def test_needs_email(prev, row, expected):
    assert rg.needs_email({**row, "notified_key": prev}, T0.date()) is expected


# ---------------------------------------------------------------------------
# Live-page buy-button probe (fake HTTP)
# ---------------------------------------------------------------------------

def _probe_settings(**http):
    return settings_for("p", "home-backup-power", buy_button={"http": {
        "enabled": True, "markers": [r"data-cta-placement=", r"amazon\.com/dp/"], **http}})


def _probe_item(n, url):
    return rg.CatalogItem(product_id=n, brand="B", model_name=f"M{n}", url=url)


def test_probe_only_fetches_the_sites_own_host_and_never_follows_off_site():
    http = FakeHttp()
    site = "https://shop.example/product/"
    http.pages[site + "1"] = (200, '<a data-cta-placement="pdp">Buy</a>')
    http.pages[site + "2"] = (302, "", {"location": "https://www.amazon.com/dp/B0TEST0001"})
    http.pages[site + "3"] = (301, "", {"location": "/product/3b"})
    http.pages[site + "3b"] = (200, "<p>Recalled</p>")
    http.pages[site + "4"] = (302, "", {"location": "https://elsewhere.example/gone"})
    http.pages[site + "5"] = (403, "challenge")
    items = [_probe_item(1, site + "1"), _probe_item(2, site + "2"), _probe_item(3, site + "3"),
             _probe_item(4, site + "4"), _probe_item(5, site + "5"),
             _probe_item(6, "https://evil.example/product/6"), _probe_item(7, ""),
             _probe_item(8, site + "8")]
    out = rg.probe_buy_buttons(items, _probe_settings(max_probes=7), http_get=http,
                               allowed_hosts=["shop.example"])
    assert sorted(v["key"] for v in out["violations"]) == [items[0].key, items[1].key]
    assert sorted(out["unverified"]) == sorted([items[4].key, items[5].key, items[6].key,
                                                items[7].key])  # 403, host, no URL, cap
    assert out["checked"] == 4
    fetched = [u for u, _ in http.calls]
    assert not any("amazon" in u or "evil" in u or "elsewhere" in u for u in fetched)
    assert site + "3b" in fetched  # same-host redirect followed


def test_probe_without_an_allowed_host_fetches_nothing():
    http = FakeHttp()
    items = [_probe_item(1, "https://shop.example/product/1")]
    out = rg.probe_buy_buttons(items, _probe_settings(), http_get=http)
    assert out["unverified"] == [items[0].key] and http.calls == []
    with_cfg = rg.probe_buy_buttons(items, _probe_settings(allowed_hosts=["shop.example"]),
                                    http_get=http)
    assert with_cfg["checked"] == 1 and with_cfg["unverified"] == []  # FakeHttp: 404


def test_urllib_get_can_leave_a_redirect_to_the_caller(monkeypatch):
    """The probe's real getter never follows a redirect (loopback server only:
    the offline guard is lifted for this one test)."""
    import http.server
    import threading
    monkeypatch.undo()

    class Redirect(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(302)
            self.send_header("Location", "https://www.amazon.com/dp/B0TEST0001")
            self.end_headers()

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), Redirect)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        resp = rg.urllib_get(f"http://127.0.0.1:{srv.server_port}/p", headers={}, timeout=5,
                             follow_redirects=False)
    finally:
        srv.shutdown()
    assert resp.status == 302
    assert resp.headers["location"] == "https://www.amazon.com/dp/B0TEST0001"


# ---------------------------------------------------------------------------
# No LLM, ever
# ---------------------------------------------------------------------------

_LLM_MODULES = ("framework.core.ai_providers", "framework.core.llm_json",
                "framework.core.llm_stream", "framework.core.llm_usage",
                "framework.core.local_llm", "framework.core.code_editor",
                "framework.core.llm_flush_sidecar", "anthropic", "openai", "ollama")


def _imports(path: Path) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level and mod == "":
                mod = "framework.core"
            out.add(mod)
            out.update(f"{mod}.{a.name}" for a in node.names)
    return out


@pytest.mark.parametrize("rel", ["framework/core/recall_gate.py",
                                 "agents/recall-gate-agent/agent.py"])
def test_no_llm_module_is_imported(rel):
    found = _imports(REPO / rel)
    bad = sorted(m for m in found for llm in _LLM_MODULES
                 if m == llm or m.startswith(llm + "."))
    assert bad == []
    text = (REPO / rel).read_text()
    for call in (".ai_chat(", ".ai_chat_resolved(", "ai_client_for(", "chat_with_fallback("):
        assert call not in text


def test_matcher_runs_with_llm_entry_points_booby_trapped(monkeypatch, records):
    from framework.core import ai_providers
    calls = []

    def trap(*a, **k):
        calls.append(a)
        raise AssertionError("LLM called")
    monkeypatch.setattr(ai_providers, "chat_with_fallback", trap)
    monkeypatch.setattr(ai_providers, "ai_client_for", trap)
    match_table(settings_for("p", "home-backup-power"), catalog_rows("home_backup_power"),
                records)
    assert calls == []


# ---------------------------------------------------------------------------
# Agent packaging (inert)
# ---------------------------------------------------------------------------

AGENT_DIR = REPO / "agents" / "recall-gate-agent"


def test_manifest_is_inert():
    doc = json.loads((AGENT_DIR / "manifest.json").read_text())
    assert doc["id"] == "recall-gate-agent"
    assert doc["enabled"] is False and not doc.get("cron_expr")
    assert (AGENT_DIR / doc["runbook"]).is_file()


def test_goals_bind_to_metrics_the_agent_emits():
    doc = json.loads((AGENT_DIR / "goals.json").read_text())
    from framework.core import goals as goals_mod
    goals_mod._validate_goals_doc(doc)
    metrics = {g["target_metric"] for g in doc["goals"]}
    assert {"recall_sync_age_h", "recalled_live_buy_buttons", "manual_queue_oldest_h",
            "site_errors"} == metrics
    targets = {g["target_metric"]: g["metric"]["target"] for g in doc["goals"]}
    assert targets["recall_sync_age_h"] == 26 and targets["recalled_live_buy_buttons"] == 0
    assert all(3 <= len(doc["goals"]) <= 7 for _ in [0])
    source = (AGENT_DIR / "agent.py").read_text()
    for name in metrics:
        assert f'"{name}"' in source


def test_cli_schema_and_config_without_storage(capsys):
    assert rg.main(["--no-storage", "schema"]) == 0
    assert "CREATE TABLE IF NOT EXISTS recall_matches" in capsys.readouterr().out
    assert rg.main(["--no-storage", "config"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["problems"] == [] and "home-backup-power" in doc["profiles"]
    assert rg.main(["--no-storage", "config", "--site", "nope"]) == 1


def test_cli_match_dry_run(tmp_path, capsys, monkeypatch, records):
    doc = repo_config_doc()
    doc["by_site"] = {"power-site": {"enabled": True, "profile": "home-backup-power",
                                     "catalog_sql": "SELECT 1", "dsn_env": "X"}}
    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text(json.dumps(doc))
    monkeypatch.setattr(rg, "REPO_CONFIG_PATH", cfg_path)
    recs = tmp_path / "recalls.json"
    recs.write_text(json.dumps([records[ECOFLOW].to_dict()]))
    cat = tmp_path / "catalog.json"
    cat.write_text(json.dumps(catalog_rows("home_backup_power")))
    assert rg.main(["--no-storage", "match", "--site", "power-site", "--recalls", str(recs),
                    "--catalog", str(cat), "--today", "2025-12-01"]) == 0
    out = json.loads(capsys.readouterr().out)
    by_item = {o["item"]: o for o in out}
    assert by_item["EcoFlow DELTA Max 2000"]["blocking"] is True
    assert by_item["EcoFlow DELTA Max 1600"]["kind"] == "sibling"
