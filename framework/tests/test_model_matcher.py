"""framework/core/model_matcher.py: ASIN-to-model matching ("search and confirm").

The model records are real makers and model names from the home-power research
dataset (fixtures/model_matcher/power-models.json: maker pages only, no Amazon
data), converted to the shape of a site's ``model_matching.models_sql`` rows.
Listing titles are written for these tests in the style of the marketplace;
none is copied from Amazon. Pure functions: no I/O, no network.
"""
from __future__ import annotations

import decimal
import json
import os
import re
from pathlib import Path

import pytest

from framework.core import model_matcher as M

FIXTURE = Path(__file__).parent / "fixtures" / "model_matcher" / "power-models.json"
ROWS = json.loads(FIXTURE.read_text())["models"]
# Three real models the dataset does not carry, so the nesting families are complete.
EXTRA = [
    {"id": "jackery-explorer-1000", "brand": "Jackery", "model_name": "Explorer 1000", "kind": "station",
     "capacity_wh": 1002, "power_w": 1000},
    {"id": "anker-solix-c1000", "brand": "Anker SOLIX", "model_name": "C1000", "kind": "station",
     "capacity_wh": 1056, "power_w": 1800},
    {"id": "anker-solix-f2000", "brand": "Anker SOLIX", "model_name": "F2000", "aliases": ["PowerHouse 767"],
     "kind": "station", "capacity_wh": 2048, "power_w": 2400},
]
ALIASES = {
    "EcoFlow": ["EF ECOFLOW"], "Anker SOLIX": ["Anker"], "BLUETTI": ["MAXOAK", "MAXOAK BLUETTI"],
    "Geneverse": ["GENERARK"], "Goal Zero": ["GOALZERO"],
}


@pytest.fixture(scope="module")
def idx():
    return M.ModelIndex([M.ModelRecord.from_row(r) for r in ROWS + EXTRA], brand_aliases=ALIASES)


def _ids(prefix):
    return sorted(r["id"] for r in ROWS + EXTRA if r["id"].startswith(prefix))


KIT_2X100 = "ecoflow-delta-3-max-plus-solar-generator-2-x-100w-rigid-solar-panel"
KIT_J1000P_AIR2 = "jackery-solar-generator-bundle-explorer-1000-plus-solarsaga-100-air-x-2"
KIT_C1000G2_PS200 = "anker-solix-c1000-gen-2-ps200-solar-panel"
KIT_C1000G2_PS100 = "anker-solix-c1000-gen-2-ps100-solar-panel"

# (brand field, title, expected model id or None, expected reason code, expected rule or None)
CASES = [
    # -- Jackery Explorer 1000 / 1000 Plus / 1000 v2 / 1000 Pro -----------------------------------
    ("Jackery", "Jackery Explorer 1000 Portable Power Station, 1002Wh Solar Generator for Camping",
     "jackery-explorer-1000", "matched", "designation"),
    ("Jackery", "Jackery Explorer 1000 Plus Portable Power Station, 1264Wh LiFePO4, 2000W AC Output",
     "jackery-explorer-1000-plus", "matched", "designation"),
    ("Jackery", "Jackery Explorer 1000 v2 Portable Power Station, 1070Wh LiFePO4 Battery, 1500W AC",
     "jackery-explorer-1000-v2", "matched", "designation"),
    ("Jackery", "Jackery Explorer 1000V2 Portable Power Station 1070Wh",
     "jackery-explorer-1000-v2", "matched", "designation"),
    ("Jackery", "Jackery Explorer 1000 Pro Portable Power Station 1002Wh",
     "jackery-explorer-1000-pro", "matched", "designation"),
    ("Jackery", "Jackery Explorer 1000 v3 Portable Power Station", None, "unknown-variant", None),
    ("Jackery", "Jackery Explorer 1000 Plus Portable Power Station, 1070Wh", None, "capacity-mismatch", None),
    ("Jackery", "Jackery Explorer 1000 Plus Power Station JE-1000C", "jackery-explorer-1000-plus",
     "matched", "designation"),
    ("Jackery", "Jackery Explorer 1000 Plus and Explorer 2000 Plus Power Stations", None, "ambiguous", None),
    ("Jackery", "Jackery Explorer 300 Portable Power Station", None, "ambiguous", None),  # two records share it
    ("Jackery", "Jackery Battery Pack 2000 Plus Expansion Battery for Explorer 2000 Plus",
     "jackery-battery-pack-2000-plus", "matched", "designation"),
    ("Jackery", "Jackery Explorer 1000 Plus Solar Generator with SolarSaga 100 Air x 2",
     KIT_J1000P_AIR2, "matched", "bundle-kit"),
    ("Jackery", "Jackery Explorer 1000 Plus Solar Generator with SolarSaga 100 Air Solar Panel",
     None, "bundle-no-kit", None),      # one panel: the only kit we know has two
    ("Jackery", "Jackery Explorer 1000 Plus with 2 SolarSaga 100 Air Solar Panels", KIT_J1000P_AIR2,
     "matched", "bundle-kit"),
    ("VTOMAN", "VTOMAN FlashSpeed 1100 Portable Power Station with 110W Solar Panel", None,
     "bundle-ambiguous", None),         # two kits of ours: same station, same 110 W panel
    # -- EcoFlow DELTA 2 / 2 Max / 3 / 3 Plus / 3 Max / 3 Max Plus ---------------------------------
    ("EcoFlow", "EcoFlow DELTA 2 Portable Power Station, 1024Wh LFP Battery, 1800W AC Outlets",
     "ecoflow-delta-2", "matched", "designation"),
    ("EcoFlow", "EcoFlow DELTA 2 Max Portable Power Station, 2048Wh, 2400W AC Output",
     "ecoflow-delta-2-max", "matched", "designation"),
    ("EF ECOFLOW", "EF ECOFLOW Portable Power Station DELTA 3, 1024Wh LiFePO4, 1800W AC",
     "ecoflow-delta-3", "matched", "designation"),
    ("EF ECOFLOW", "EF ECOFLOW DELTA 3 Plus Portable Power Station, 1024Wh, 1800W, Solar Generator for Home Backup",
     "ecoflow-delta-3-plus", "matched", "designation"),
    ("EcoFlow", "EcoFlow DELTA 3 Max Portable Power Station 2048Wh, 2400W", "ecoflow-delta-3-max",
     "matched", "designation"),
    ("EcoFlow", "EcoFlow DELTA 3 Max Plus Portable Power Station 2048Wh, 3000W AC Output",
     "ecoflow-delta-3-max-plus", "matched", "designation"),
    ("EcoFlow", "EcoFlow DELTA3 Plus Portable Power Station 1024Wh", "ecoflow-delta-3-plus",
     "matched", "designation"),
    ("EcoFlow", "EcoFlow Delta-3-Plus Power Station, 1024 Wh", "ecoflow-delta-3-plus", "matched", "designation"),
    ("EcoFlow", "ＥｃｏＦｌｏｗ ＤＥＬＴＡ ３ Ｐｌｕｓ Power Station", "ecoflow-delta-3-plus", "matched",
     "designation"),  # full-width letters fold (NFKC)
    ("EcoFlow", "EcoFlow DELTA 3 1500 Portable Power Station, 1536Wh", "ecoflow-delta-3-1500",
     "matched", "designation"),
    ("EcoFlow", "EcoFlow DELTA 3 Portable Power Station, 2048Wh", None, "capacity-mismatch", None),
    ("EcoFlow", "EcoFlow DELTA 3 Plus 1024Wh Power Station, Expandable to 5kWh", "ecoflow-delta-3-plus",
     "matched", "designation"),
    ("EcoFlow", "EcoFlow EFD330 Portable Power Station", "ecoflow-delta-2", "matched", "model-number"),
    ("EcoFlow", "EcoFlow DELTA 2 Max Smart Extra Battery, 2048Wh LFP", "ecoflow-delta-2-max-smart-extra-battery",
     "matched", "designation"),
    ("EcoFlow", "EcoFlow DELTA 3 Max Plus Solar Generator with 2x100W Rigid Solar Panels, 2048Wh",
     KIT_2X100, "matched", "bundle-kit"),
    ("EcoFlow", "EcoFlow DELTA 3 Max Plus Solar Generator with 2 x 100W Rigid Solar Panel", KIT_2X100,
     "matched", "bundle-kit"),
    ("EcoFlow", "EcoFlow DELTA 3 Max Plus Solar Generator with 3 x 100W Rigid Solar Panel", None,
     "bundle-no-kit", None),
    ("EcoFlow", "EcoFlow DELTA 3 Max Plus with Solar Panel", None, "bundle-unverified", None),
    ("EcoFlow", "EcoFlow DELTA 3 Max Plus with 220W Bifacial Solar Panel", None, "bundle-no-kit", None),
    ("EcoFlow", "EcoFlow DELTA 3 Plus with Smart Extra Battery, Home Backup", None, "bundle-no-kit", None),
    ("EcoFlow", "EcoFlow 100W Rigid Solar Panel, Monocrystalline", "ecoflow-100w-rigid-solar-panel",
     "matched", "designation"),
    ("EcoFlow", "EcoFlow DELTA 3 Plus Portable Power Station (Solar Panel Optional), 1024Wh",
     "ecoflow-delta-3-plus", "matched", "designation"),
    ("EcoFlow", "EcoFlow XT60 Solar Adapter Compatible with DELTA 3 Plus", None, "no-designation", None),
    ("BougeRV", "BougeRV MC4 Solar Charging Cable for EcoFlow DELTA 3 Plus", None, "no-designation", None),
    ("EcoFlow", "Carrying Case for EcoFlow DELTA 2 Max", None, "no-designation", None),
    # -- BLUETTI AC180 / AC180P / AC200L ----------------------------------------------------------------
    ("BLUETTI", "BLUETTI AC180 Portable Power Station, 1152Wh LiFePO4 Battery, 1800W AC",
     "bluetti-ac180", "matched", "designation"),
    ("BLUETTI", "BLUETTI AC180P Portable Power Station, 1440Wh LiFePO4, 1800W", "bluetti-ac180p",
     "matched", "designation"),
    ("BLUETTI", "BLUETTI AC200L Portable Power Station, 2048Wh, 2400W AC", "bluetti-ac200l",
     "matched", "designation"),
    ("BLUETTI", "BLUETTI AC180 Portable Power Station, 1440Wh", None, "capacity-mismatch", None),
    ("BLUETTI", "BLUETTI AC180 vs AC200L Power Station", None, "ambiguous", None),
    ("MAXOAK", "MAXOAK BLUETTI AC200P Portable Power Station 2000Wh", "bluetti-ac200p", "matched",
     "designation"),
    ("BLUETTI", "BLUETTI AC200P L Portable Power Station 2304Wh", "bluetti-ac200p-l", "matched", "designation"),
    # -- Anker SOLIX C1000 / C1000X / C1000 Gen 2 / F2000 ---------------------------------------------
    ("Anker SOLIX", "Anker SOLIX C1000 Portable Power Station, 1056Wh, 1800W (Peak 2400W)",
     "anker-solix-c1000", "matched", "designation"),
    ("Anker", "Anker SOLIX C1000X Portable Power Station, 1056Wh, 1800W", "anker-solix-c1000x",
     "matched", "designation"),
    ("Anker", "Anker SOLIX C1000X Portable Power Station, 2400W AC Output", None, "wattage-mismatch", None),
    ("Anker", "Anker A1761111 Power Station", "anker-solix-c1000x", "matched", "model-number"),
    ("Anker SOLIX", "Anker SOLIX C1000 Gen 2 Portable Power Station, 1024Wh, 2000W",
     "anker-solix-c1000-gen-2", "matched", "designation"),
    ("Anker SOLIX", "Anker SOLIX F2000 Portable Power Station (PowerHouse 767), 2048Wh",
     "anker-solix-f2000", "matched", "designation"),
    ("Anker SOLIX", "Anker SOLIX C1000 Gen 2 Power Station with 200W Solar Panel (PS200)",
     KIT_C1000G2_PS200, "matched", "bundle-kit"),
    ("Anker SOLIX", "Anker SOLIX C1000 Gen 2 Solar Generator + PS100 Bifacial Portable Solar Panel",
     KIT_C1000G2_PS100, "matched", "bundle-kit"),
    ("Anker SOLIX", "Anker SOLIX C1000 Gen 2 with 2 x PS100 Bifacial Portable Solar Panel", None,
     "bundle-no-kit", None),
    # -- other makers -------------------------------------------------------------------------------------
    ("GENERARK", "GENERARK HomePower ONE PRO Portable Power Station", "geneverse-homepower-one-pro",
     "matched", "designation"),
    ("Geneverse", "Geneverse HomePower ONE Backup Battery Power Station", "geneverse-homepower-one",
     "matched", "designation"),
    ("DJI", "DJI Power 1000 Mini Portable Power Station", "dji-power-1000-mini", "matched", "designation"),
    ("DJI", "DJI Power 1000 Portable Power Station", "dji-power-1000", "matched", "designation"),
    ("Goal Zero", "Goal Zero Yeti 1500 Portable Power Station", "goal-zero-yeti-1500-6th-gen-2026",
     "matched", "designation"),
    # -- brand and designation gates ----------------------------------------------------------------------
    ("Acme Power", "Acme Power DELTA 3 Plus Portable Power Station", None, "brand-unknown", None),
    ("", "EcoFlow DELTA 3 Plus Portable Power Station", None, "brand-missing", None),
    (None, "Jackery Explorer 1000 Plus", None, "brand-missing", None),
    ("EcoFlow", "EcoFlow Portable Power Station 1024Wh LiFePO4", None, "no-designation", None),
    ("Jackery", "", None, "no-title", None),
    ("Jackery", "EcoFlow DELTA 2 Portable Power Station", None, "no-designation", None),  # brand is the field
]


# Adversarial review 2026-11-22 (false-match hunt): every case that linked the
# wrong model, plus the review's probes and titles that must still match.
# (brand field, title, expected model id or None, expected reason code)
ADVERSARIAL = [
    ('Anker', 'Anker SOLIX C300X DC Power Bank Station 288Wh', None, 'unknown-variant'),
    ('EcoFlow', 'EF ECOFLOW DELTA 3 Max+ Portable Power Station 2048Wh', 'ecoflow-delta-3-max-plus', 'matched'),
    ('EcoFlow', 'EF ECOFLOW DELTA 2 Max+ Portable Power Station 2048Wh', None, 'unknown-variant'),
    ('EcoFlow', 'EF ECOFLOW RIVER 3 Plus Wireless Portable Power Station 286Wh', None, 'unknown-variant'),
    ('EcoFlow', 'EF ECOFLOW RIVER 3 UPS Portable Power Station 245Wh', None, 'unknown-variant'),
    ('Goal Zero', 'Goal Zero Nomad 100 Solar Panel 100W Foldable', None, 'no-designation'),
    ('Goal Zero', 'Goal Zero Nomad 100+ Solar Panel 100W Foldable', 'goal-zero-nomad-100', 'matched'),
    ('Jackery', 'Jackery Explorer 1000+ SolarSaga 100W Solar Panel', None, 'ambiguous'),
    ('EcoFlow', 'EcoFlow DELTA 3 Max Bag, Waterproof Dustproof Storage Cover', None, 'accessory'),
    ('EcoFlow', 'EcoFlow DELTA 2 Max Waterproof Bag', None, 'accessory'),
    ('EcoFlow', 'EcoFlow DELTA 2 Max Waterproof and Dustproof Cover', None, 'accessory'),
    ('EcoFlow', 'EcoFlow RIVER 3 Plus Replacement Handle Compatible with EcoFlow', None, 'accessory'),
    ('EcoFlow', 'EcoFlow DELTA Pro 3 Remote Control Accessory', None, 'accessory'),
    ('EcoFlow', 'EcoFlow RIVER 2 Pro Car Charging Cable 1.5m', None, 'accessory'),
    ('BLUETTI', 'BLUETTI AC180 Replacement AC Charging Cable', None, 'accessory'),
    ('BLUETTI', 'BLUETTI AC200MAX Car Charging Cable', None, 'accessory'),
    ('BLUETTI', 'BLUETTI AC70 Carrying Bag with Handle', None, 'accessory'),
    ('BLUETTI', 'BLUETTI AC180 Carrying Case', None, 'accessory'),
    ('BLUETTI', 'BLUETTI 200W Solar Panel, AC180 Compatible', None, 'accessory'),
    ('Jackery', 'Jackery Explorer 1000 Plus Carrying Case, Official Protective Bag', None, 'accessory'),
    ('Jackery', 'Jackery Explorer 2000 Plus Solar Charging Cable Extension 5m', None, 'accessory'),
    ('Anker', 'Anker SOLIX F3800 Plus Home Backup Kit', None, 'accessory'),
    ('Anker', 'Anker SOLIX C1000X Wheels Kit', None, 'accessory'),
    ('PECRON', 'PECRON E3800LFP Wheeled Cart', None, 'accessory'),
    ('DJI', 'DJI Power 1000 Car Charging Cable', None, 'accessory'),
    ('Goal Zero', 'Goal Zero Yeti 1500X Roll Cart', None, 'accessory'),
    ('EcoFlow', 'EcoFlow DELTA 3 Max 220W Bifacial Solar Panel', None, 'item-kind-mismatch'),
    ('EcoFlow', 'EcoFlow DELTA 3 Max Smart Extra Battery 2048Wh', None, 'item-kind-mismatch'),
    ('EcoFlow', 'Waterproof Cover Bag, EcoFlow DELTA 2 Max', None, 'accessory'),
    ('Jackery', 'Carrying Case for Power Station, Jackery Explorer 1000 Plus', None, 'accessory'),
    ('Jackery', 'Jackery Solar Generator 1000 Plus with 2 x SolarSaga 100 Air', None, 'bundle-component'),
    ('Jackery', 'Jackery Explorer 1000 Plus + Battery Pack 1000 Plus Expansion', None, 'bundle-no-kit'),
    ('Jackery', 'Jackery Explorer 1000 Plus Portable Power Station with Battery Pack 1000 Plus, Solar Generator for Home', None, 'bundle-no-kit'),
    ('PECRON', 'PECRON E3600LFP with PV300 Solar Panel x 2', None, 'bundle-no-kit'),
    ('PECRON', 'PECRON E3600LFP Portable Power Station with PV300 Solar Panel x 2, 3072Wh Solar Generator', None, 'bundle-no-kit'),
    ('Anker', 'Anker SOLIX C1000 Gen 2 Portable Power Station with PS200 Solar Panel x2, 1024Wh', None, 'bundle-no-kit'),
    ('Anker', 'Anker SOLIX C1000 Gen 2 with PS200 Solar Panel x2', None, 'bundle-no-kit'),
    ('PECRON', 'PECRON E3600LFP with 2pcs PV300 Solar Panel', None, 'bundle-no-kit'),
    ('EcoFlow', 'EF ECOFLOW DELTA 2 Portable Power Station 1024Wh (Renewed)', None, 'condition'),
    ('Jackery', 'Jackery Explorer 1000 Plus Portable Power Station (Refurbished)', None, 'condition'),
    ('BLUETTI', 'BLUETTI AC180 Portable Power Station, Certified Refurbished', None, 'condition'),
    ('Anker', 'Anker SOLIX C1000X Portable Power Station - Renewed Premium', None, 'condition'),
    ('Goal Zero', 'Goal Zero Yeti 1500X Portable Power Station (Used - Like New)', None, 'condition'),
    ('EcoFlow', 'EF ECOFLOW DELTA 3 Plus Portable Power Station 1024Wh, 1800W, Solar Generator, Renewed', None, 'condition'),
    ('EcoFlow', 'EcoFlow 100W Rigid Solar Panel (2 Pack)', None, 'multi-pack'),
    ('EcoFlow', 'EcoFlow 175W Rigid Solar Panel, 2 Pieces', None, 'multi-pack'),
    ('EcoFlow', 'EcoFlow 400W Rigid Solar Panel 2pcs', None, 'multi-pack'),
    ('Jackery', 'Jackery SolarSaga 100 Prime Solar Panel, 2 Pack', None, 'multi-pack'),
    ('Anker', 'Anker SOLIX PS100 Bifacial Portable Solar Panel 2-Pack', None, 'multi-pack'),
    ('BLUETTI', 'BLUETTI B80 Expansion Battery x2', None, 'multi-pack'),
    ('Jackery', 'Jackery Explorer 1000 Plus Portable Power Station 2-Pack', None, 'multi-pack'),
    ('Jackery', 'Jackery SolarSaga 100 Air Solar Panel, Twin Pack', None, 'multi-pack'),
    ('EcoFlow', 'EcoFlow 100W Rigid Solar Panel x 4', None, 'multi-pack'),
    ('Anker', 'Set of 2 Anker SOLIX PS200 Bifacial Portable Solar Panel', None, 'multi-pack'),
    ('EcoFlow', 'EF ECOFLOW DELTA 2 Max 1200W Portable Power Station', None, 'wattage-mismatch'),
    ('EcoFlow', 'EF ECOFLOW DELTA 2 1200W Portable Power Station', None, 'wattage-mismatch'),
    ('BLUETTI', 'BLUETTI AC200MAX 3000W Portable Power Station', None, 'wattage-mismatch'),
    ('EcoFlow', 'EF ECOFLOW DELTA 3 Max 800W Portable Power Station', None, 'wattage-mismatch'),
    ('EcoFlow', 'EF ECOFLOW RIVER 3 Plus with Extra Battery 858Wh', 'ecoflow-river-3-max-plus', 'matched'),
    ('BLUETTI', 'BLUETTI Solar Generator for Home Use, AC180 Portable Power Station 1152Wh', 'bluetti-ac180', 'matched'),
    ('EcoFlow', 'EF ECOFLOW Portable Power Station for Camping, DELTA 3 Plus 1024Wh', 'ecoflow-delta-3-plus', 'matched'),
    ('EcoFlow', 'EF ECOFLOW DELTA 2 Max Portable Power Station, 2048Wh, 2400W AC Output, with Car Charging Cable', 'ecoflow-delta-2-max', 'matched'),
    ('BLUETTI', 'BLUETTI Portable Power Station AC180, 1152Wh LiFePO4 Battery Backup, 1800W (2700W Power Lifting)', 'bluetti-ac180', 'matched'),
    ('BLUETTI', 'BLUETTI B300 Expansion Battery Compatible with AC200P', 'bluetti-b300', 'matched'),
    ('EcoFlow', 'EcoFlow 100W Rigid Solar Panel Compatible with DELTA 3 Plus', 'ecoflow-100w-rigid-solar-panel', 'matched'),
    ('Anker', 'Anker SOLIX PS100 Portable Solar Panel, Foldable Solar Charger', 'anker-solix-ps100-bifacial-portable-solar-panel', 'matched'),
    ('Jackery', 'Jackery Explorer 1000 v2 Portable Power Station, 1070Wh LiFePO4 Battery, 1500W AC, 3000W Peak', 'jackery-explorer-1000-v2', 'matched'),
    ('EcoFlow', 'EF ECOFLOW DELTA 2 Max Portable Power Station 2048Wh 2400W', 'ecoflow-delta-2-max', 'matched'),
    ('BLUETTI', 'BLUETTI AC200MAX Portable Power Station 2048Wh 2200W', 'bluetti-ac200max', 'matched'),
    ('EcoFlow', 'EcoFlow DELTA 3 Plus Portable Power Station 1024Wh, Expandable up to 2x Extra Batteries', 'ecoflow-delta-3-plus', 'matched'),
    ('Jackery', 'Jackery Explorer 1000 Plus Portable Power Station, 1264Wh, 4 x AC Outlets', 'jackery-explorer-1000-plus', 'matched'),
]


@pytest.mark.parametrize("brand,title,model_id,reason", ADVERSARIAL,
                         ids=[f"adv{i:02d}-{c[3]}" for i, c in enumerate(ADVERSARIAL)])
def test_adversarial_regressions(idx, brand, title, model_id, reason):
    res = M.match(brand, title, idx)
    assert (res.model_id, res.reason_code) == (model_id, reason)


def test_accessories_bundles_and_packs_never_take_a_bare_model(idx):
    """The operator's hard line: an accessory, a compatible-with listing or a
    bundle never links to the bare station (or panel / battery)."""
    bare = {"matched"}
    for brand, title, model_id, reason in ADVERSARIAL:
        if reason in ("accessory", "item-kind-mismatch", "bundle-component", "multi-pack", "condition"):
            assert M.match(brand, title, idx).reason_code not in bare


def test_glued_plus_reads_plus(idx):
    toks = M.tokenize("DELTA 3 Max+ Portable")
    assert [t.text for t in toks] == ["delta", "3", "max", "plus", "portable"]
    assert [t.synthetic for t in toks] == [False, False, False, True, False]
    assert toks[4].hard_before and not toks[3].hard_before
    assert [t.text for t in M.tokenize("AC180+B80")] == ["ac180", "b80"]
    assert M.designations_of(["Nomad 100+"])[0] == {"nomad100plus"}


def test_maker_variant_words_come_from_our_catalog(idx):
    anker = idx.brands[M.compact("Anker SOLIX")]
    assert "dc" in anker.variant_words          # C300 -> C300 DC
    assert "solar" not in anker.variant_words and "panel" not in anker.variant_words


def test_case_table_is_big_enough():
    assert len(CASES) >= 40


@pytest.mark.parametrize("brand,title,model_id,reason,rule", CASES,
                         ids=[f"{i:02d}-{c[2] or c[3]}"[:60] for i, c in enumerate(CASES)])
def test_match_cases(idx, brand, title, model_id, reason, rule):
    res = M.match(brand, title, idx)
    assert (res.model_id, res.reason_code) == (model_id, reason)
    if rule:
        assert res.rule == rule and res.confidence == M.RULE_CONFIDENCE[rule]
    else:
        assert res.rule == "" and res.confidence == 0.0


def test_family_members_exist_in_the_fixture():
    assert {"ecoflow-delta-2", "ecoflow-delta-2-max", "ecoflow-delta-3", "ecoflow-delta-3-plus",
            "ecoflow-delta-3-max-plus"} <= set(_ids("ecoflow-delta"))
    assert {"bluetti-ac180", "bluetti-ac180p", "bluetti-ac200l"} <= set(_ids("bluetti-ac"))
    assert {"anker-solix-c1000x", "anker-solix-c1000-gen-2"} <= set(_ids("anker-solix-c1000"))


def test_results_carry_only_our_outcome(idx):
    title = "EcoFlow DELTA 3 Plus Zorblaxian Portable Power Station"
    for brand in ("EcoFlow", "Nope"):
        res = M.match(brand, title, idx)
        assert set(res.to_dict()) == {"model_id", "confidence", "rule", "reason_code"}
        assert "zorblax" not in json.dumps(res.to_dict()).lower()
    assert M.match("EcoFlow", title, idx).ambiguous is False


def test_ambiguous_flag(idx):
    assert M.match("BLUETTI", "BLUETTI AC180 vs AC200L", idx).ambiguous
    assert M.match("EcoFlow", "EcoFlow DELTA 3 Max Plus with Solar Panel", idx).ambiguous
    assert not M.match("Jackery", "Jackery Explorer 1000 v3", idx).ambiguous


def test_match_accepts_plain_records_and_aliases():
    recs = [M.ModelRecord.from_row(r) for r in ROWS if r["brand"] == "EcoFlow"]
    res = M.match("EF ECOFLOW", "EF ECOFLOW DELTA 2 Max Power Station", recs,
                  brand_aliases={"EcoFlow": ["EF ECOFLOW"]})
    assert res.model_id == "ecoflow-delta-2-max"
    assert M.match("EF ECOFLOW", "EF ECOFLOW DELTA 2 Max Power Station", recs).reason_code == "brand-unknown"


def test_normalisation_and_tokens():
    assert M.compact("EF-DL-H02-3M") == "efdlh023m"
    assert M.compact("DELTA 3") == M.compact("delta-3") == M.compact("DELTA3") == "delta3"
    assert M.normalize_text("2 × 100W") == "2 x 100w"
    toks = M.tokenize("DELTA 3, Plus (EFD330)")
    assert [t.text for t in toks] == ["delta", "3", "plus", "efd330"]
    assert [t.hard_before for t in toks] == [False, False, True, True]


def test_designations_from_names():
    names, numbers = M.designations_of(["DELTA 3 Max (2048Wh) (EF-DL-H02-3M)"])
    assert names == {"delta3max"} and numbers == {"efdlh023m"}
    names, _ = M.designations_of(["Anker SOLIX C1000 Gen 2"], brand_prefixes=["Anker SOLIX"])
    assert names == {"c1000gen2"}
    assert M.designations_of(["BP3000 / BP3000 Plus"])[0] == {"bp3000", "bp3000plus"}
    assert M.designations_of(["Flash300 286Wh Fast Charging Power Station"])[0] == {"flash300"}
    assert M.designations_of(["SolarSaga 200W"])[0] == {"solarsaga200w"}
    assert "ps200" in M.designations_of(["PS200 Bifacial Portable Solar Panel"])[0]
    names, numbers = M.designations_of(["MJ32 Cascade Cable for EP3800-48V"])
    assert names == {"mj32cascadecable"} and not numbers
    assert M.designations_of(["120V Generator Input Adapter (for F3000)"])[1] == set()
    assert M.designations_of(["Explorer 300 (LiFePO4, current listing)"]) == ({"explorer300"}, set())
    assert M.designations_of(["New 200W Portable Solar Panel"])[0] == {"new200wportablesolarpanel"}
    assert M.designations_of(["Portable Power Station"])[0] == set()


def test_model_number_like():
    assert M.model_number_like("EFD330") and M.model_number_like("JE-1000C")
    assert not M.model_number_like("LiFePO4") and not M.model_number_like("100W")
    assert not M.model_number_like("XT60") and not M.model_number_like("2026")


def test_bare_number_after_a_designation_is_a_variant_unless_a_unit_follows(idx):
    assert M.match("EcoFlow", "EcoFlow DELTA 2 1024 Wh Power Station", idx).model_id == "ecoflow-delta-2"
    assert M.match("EcoFlow", "EcoFlow DELTA 2 2048 Power Station", idx).reason_code == "unknown-variant"
    # 'x 2' after a designation is a count, not a variant word: a two-pack, never the single unit
    assert M.match("Jackery", "Jackery Explorer 1000 Plus x 2 Bundle", idx).reason_code == "multi-pack"


def test_kit_info_reads_components_and_names(idx):
    info = idx.kit_info(KIT_2X100)
    assert info.station_id == "ecoflow-delta-3-max-plus" and info.panels == {100: 2} and not info.battery
    # components without ratings: the kit's own name, after the station ('Kit 600W (R600 + ...)')
    info = idx.kit_info("allpowers-solar-generator-kit-600w-r600-sf100-100w-flexible-solar-panel")
    assert info.station_id == "allpowers-r600" and info.panels == {100: 1}


def test_alias_naming_two_makers_resolves_to_none():
    recs = [M.ModelRecord(model_id=1, brand="Alpha", kind="station", name="AX100"),
            M.ModelRecord(model_id=2, brand="Beta", kind="station", name="BX100")]
    index = M.ModelIndex(recs, brand_aliases={"Alpha": ["Omni"], "Beta": ["Omni"]})
    assert index.resolve_brand("omni") is None and index.problems
    assert M.match("Omni", "Omni AX100", index).reason_code == "brand-unknown"


def test_from_row_handles_db_types():
    rec = M.ModelRecord.from_row({"id": 7, "brand": "Alpha", "model_name": "AX100", "kind": "kit",
                                  "aliases": None, "model_number": None, "capacity_wh": decimal.Decimal("1024.5"),
                                  "power_w": None, "components": [3, None, 4], "component_qty": [2, 1]})
    assert rec.capacity_wh == 1024.5 and rec.components == ((3, 2), (4, 1)) and rec.aliases == ()


def test_search_keywords(idx):
    assert M.search_keywords(idx, "ecoflow-delta-3-plus") == "EcoFlow DELTA 3 Plus"
    assert M.search_keywords(idx, KIT_2X100) == "EcoFlow DELTA 3 Max Plus Solar Generator 2 x 100W Rigid Solar Panel"
    assert M.search_keywords(idx, "anker-solix-c1000x") == "Anker SOLIX C1000X"
    assert M.search_keywords(idx, "missing") == ""


def test_matching_is_deterministic(idx):
    title = "EcoFlow DELTA 3 Max Plus Solar Generator with 2x100W Rigid Solar Panels"
    assert len({M.match("EcoFlow", title, idx) for _ in range(5)}) == 1


def test_every_dataset_model_matches_a_title_naming_it(idx):
    """Each non-kit model of the dataset, named in a plain title, matches itself,
    except where our own records are ambiguous (two records share a name) or
    the name is only generic words (accessory cables)."""
    hits = total = 0
    for r in ROWS:
        if r["kind"] in ("kit", "cable"):
            continue
        total += 1
        title = f"{r['brand']} {re.sub(r'[(][^)]*[)]', ' ', r['model_name'])} Portable"
        res = M.match(r["brand"], title, idx)
        assert res.model_id in (r["id"], None), (r["id"], res)   # never the wrong model
        hits += res.model_id == r["id"]
    assert hits / total >= 0.97, (hits, total)


SPECS_FILE = os.environ.get("MODEL_MATCHER_SPECS_FILE")


@pytest.mark.skipif(not (SPECS_FILE and Path(SPECS_FILE).is_file()),
                    reason="MODEL_MATCHER_SPECS_FILE (specs-v1.json) not set")
def test_fixture_is_current_with_the_dataset():
    data = json.loads(Path(SPECS_FILE).read_text())
    assert {m["id"] for m in data["models"]} == {r["id"] for r in ROWS}


# ---------------------------------------------------------------------------
# Matcher fixes of 2026-11-22: bundle families, named kits, panel specs with no
# connector word, common bundle phrasing, panel identity, digit / measure-only
# names, code-alone names, and Amazon's manufacturer model fields (rule 13).
# ---------------------------------------------------------------------------

J1000P = "jackery-explorer-1000-plus"
KIT_X2000V2_200 = "jackery-solar-generator-bundle-explorer-2000-v2-solarsaga-200w-x-2"
KIT_X2000V2_DUNE = "jackery-solar-generator-bundle-explorer-2000-v2-solarsaga-200w-dune-white-x-2"
KIT_R1500_SF200 = "allpowers-solar-generator-kit-1800w-r1500-sf200-200w-flexible-solar-panel"


def _index(patch=None):
    """The fixture index with some rows patched (our data only)."""
    rows = [dict(r, **(patch or {}).get(r["id"], {})) for r in ROWS + EXTRA]
    return M.ModelIndex([M.ModelRecord.from_row(r) for r in rows], brand_aliases=ALIASES)


@pytest.fixture(scope="module")
def fam():
    # Kit aliases are the maker's bundle-family names (rule 12).
    return _index({KIT_J1000P_AIR2: {"family_aliases": ["Solar Generator 1000 Plus"]}})


FAMILY_CASES = [
    ("Jackery Solar Generator 1000 Plus 1264Wh with 2x100W Solar Panels, LiFePO4 Portable Power Station",
     KIT_J1000P_AIR2, "matched"),
    ("Jackery Solar Generator 1000 Plus with 2 x SolarSaga 100 Air", KIT_J1000P_AIR2, "matched"),
    # the family name alone, or with panels no kit of ours has: never the bare station
    ("Jackery Solar Generator 1000 Plus, 1264Wh LiFePO4 Portable Power Station, 2000W AC Output", None,
     "bundle-unverified"),
    ("Jackery Solar Generator 1000 Plus with 200W Solar Panel, 1264Wh", None, "bundle-no-kit"),
    ("Jackery Solar Generator 1000 Plus Pro with 2x100W Solar Panels", None, "unknown-variant"),
    # the station's own name keeps its own rules
    ("Jackery Explorer 1000 Plus Portable Power Station, 1264Wh LiFePO4, 2000W AC Output", J1000P, "matched"),
]


@pytest.mark.parametrize("title,model_id,reason", FAMILY_CASES)
def test_kit_aliases_are_bundle_families_that_force_the_bundle_path(fam, title, model_id, reason):
    res = M.match("Jackery", title, fam)
    assert (res.model_id, res.reason_code) == (model_id, reason)
    if model_id:
        assert res.rule == ("designation" if model_id == J1000P else "bundle-kit")


def test_bundle_family_index_rules(fam, idx):
    assert fam.brands["jackery"].family == {"solargenerator1000plus": {J1000P}}
    assert not idx.brands["jackery"].family     # the fixture's kits carry no family alias
    # an alias holding one of our designations, a maker's name or no number is not a family name
    other = _index({KIT_J1000P_AIR2: {"family_aliases": ["Explorer 1000 Plus Solar Generator", "Jackery",
                                                  "Solar Generator Bundle Deluxe"]}})
    assert not other.brands["jackery"].family
    assert M.match("Jackery", "Jackery Explorer 1000 Plus Solar Generator, 1264Wh LiFePO4",
                   other).model_id == J1000P
    # one family name on kits of two stations: ambiguous
    two = _index({KIT_J1000P_AIR2: {"family_aliases": ["Solar Generator 9000"]},
                  KIT_X2000V2_200: {"family_aliases": ["Solar Generator 9000"]}})
    assert M.match("Jackery", "Jackery Solar Generator 9000 with 2 x SolarSaga 100 Air",
                   two).reason_code == "ambiguous"
    # without the alias the station after 'with' is still a bundled part
    assert M.match("Jackery", "Jackery Solar Generator 1000 Plus with 2 x SolarSaga 100 Air",
                   idx).reason_code == "bundle-component"


NAMED_KIT_CASES = [
    ("EF ECOFLOW RIVER 3 Max Plus Portable Power Station, 858Wh LiFePO4 Battery, 600W AC Output",
     "ecoflow-river-3-max-plus", "matched"),
    ("EF ECOFLOW RIVER 3 Max Portable Power Station, 572Wh LiFePO4", "ecoflow-river-3-max", "matched"),
    ("EF ECOFLOW RIVER 3 Max Portable Power Station, 858Wh LiFePO4", None, "capacity-mismatch"),
    ("EF ECOFLOW RIVER 3 Max Plus with 220W Solar Panel", None, "bundle-no-kit"),
    ("EF ECOFLOW RIVER 3 Max Plus Carrying Case", None, "accessory"),
    ("EF ECOFLOW RIVER 3 Max Portable Power Station 572Wh, 2-Pack", None, "multi-pack"),
    ("EF ECOFLOW RIVER 3 Max Plus Wireless Portable Power Station", None, "unknown-variant"),
    ("EF ECOFLOW 220W Solar Panel with RIVER 3 Max", None, "bundle-component"),
]


@pytest.mark.parametrize("title,model_id,reason", NAMED_KIT_CASES)
def test_a_kit_named_by_its_own_name(idx, title, model_id, reason):
    res = M.match("EF ECOFLOW", title, idx)
    assert (res.model_id, res.reason_code) == (model_id, reason)
    if model_id:
        assert res.rule == "designation"


def test_only_kits_whose_name_holds_no_component_designation_are_named(idx):
    ef = idx.brands["ecoflow"]
    assert ef.designations["river3max"] == {"ecoflow-river-3-max"}
    assert ef.designations["river3maxplus"] == {"ecoflow-river-3-max-plus"}
    kits = set(ef.kits)
    named = {m for ids in ef.designations.values() for m in ids if m in kits}
    # 'DELTA 3 Max Plus Solar Generator (...)' names its station: not a named kit
    assert named == {"ecoflow-river-3-max", "ecoflow-river-3-max-plus"}


SPEC_AND_PHRASING_CASES = [
    # (b) a panel spec in parentheses or a comma clause, no connector word
    ("EcoFlow", "EcoFlow DELTA 3 Max Plus Solar Generator (2 x 100W Rigid Solar Panel), 2048Wh", KIT_2X100, "matched"),
    ("EcoFlow", "EcoFlow DELTA 3 Max Plus Portable Power Station, 2 x 100W Rigid Solar Panels Included, 2048Wh",
     KIT_2X100, "matched"),
    ("EcoFlow", "EcoFlow DELTA 3 Max Plus Solar Generator (220W Bifacial Solar Panel), 2048Wh", None, "bundle-no-kit"),
    ("EcoFlow", "EcoFlow DELTA 3 Max Plus Portable Power Station 2048Wh (200W Solar Panel Sold Separately)",
     "ecoflow-delta-3-max-plus", "matched"),
    # (c) 'Power Station Kit', a generic head before 'with', 'and' before a quantity, decimal / comma Wh
    ("EcoFlow", "EcoFlow DELTA 3 Max Plus Portable Power Station Kit with 2 x 100W Rigid Solar Panels, 2048Wh",
     KIT_2X100, "matched"),
    ("Anker", "Anker SOLIX F3800 Plus Home Backup Kit", None, "accessory"),   # no bundle evidence: an accessory kit
    ("EcoFlow", "EcoFlow Solar Generator Kit with DELTA 3 Max Plus Power Station and 2 x 100W Rigid Solar Panels",
     KIT_2X100, "matched"),
    ("EcoFlow", "EcoFlow 100W Rigid Solar Panel with DELTA 3 Max Plus Power Station", None, "bundle-component"),
    ("Jackery", "Jackery Explorer 1000 Plus and 2 x SolarSaga 100 Air Solar Panels Combo, 1264Wh",
     KIT_J1000P_AIR2, "matched"),
    ("BLUETTI", "BLUETTI AC180 1,152Wh Portable Power Station, 1800W AC Output", "bluetti-ac180", "matched"),
    ("BLUETTI", "BLUETTI AC180P 1440.0Wh Portable Power Station", "bluetti-ac180p", "matched"),
    ("BLUETTI", "BLUETTI AC180 1,440Wh Portable Power Station", None, "capacity-mismatch"),
    # (d) 'expandable with <battery>' is what the station takes, unless the battery is included
    ("BLUETTI", "BLUETTI AC180P Portable Power Station 1440Wh, 1800W AC Output, Expandable with B230 Battery",
     "bluetti-ac180p", "matched"),
    ("BLUETTI", "BLUETTI AC180P Portable Power Station 1440Wh, Expandable with B230 Battery (Included)", None,
     "bundle-no-kit"),
    ("EF ECOFLOW", "EF ECOFLOW RIVER 3 Plus Portable Power Station 286Wh, Expandable with Smart Extra Battery",
     "ecoflow-river-3-plus", "matched"),
    # (e) a generation / version qualifier in parentheses continues the name
    ("Anker SOLIX", "Anker SOLIX C1000 (Gen 2) Portable Power Station, 1024Wh", "anker-solix-c1000-gen-2", "matched"),
    ("Anker SOLIX", "Anker SOLIX C1000 (2nd Gen) Portable Power Station, 1024Wh", "anker-solix-c1000-gen-2",
     "matched"),
    ("EcoFlow", "EcoFlow DELTA 2 (Gen 3) Portable Power Station, 1024Wh", None, "unknown-variant"),
    # (h) a panel model named in the title must be the kit's; a stated panel build must be its build
    ("Jackery", "Jackery Explorer 1000 Plus Solar Generator with 2 x SolarSaga 100W Solar Panels, 1264Wh", None,
     "bundle-no-kit"),
    ("Jackery", "Jackery Explorer 2000 v2 with 2 x SolarSaga 200W Solar Panels, 2042Wh", KIT_X2000V2_200, "matched"),
    ("Jackery", "Jackery Explorer 2000 v2 with 2 x 200W Solar Panels, 2042Wh", None, "bundle-ambiguous"),
    ("ALLPOWERS", "ALLPOWERS R1500 Solar Generator with 200W Foldable Solar Panel, 1152Wh", None, "bundle-no-kit"),
    ("ALLPOWERS", "ALLPOWERS R1500 Solar Generator with SF200 200W Flexible Solar Panel, 1152Wh", KIT_R1500_SF200,
     "matched"),
    # a kit name's '200W(Dune White) x 2' is two panels: a one-panel bundle is not that kit
    ("Jackery", "Jackery SolarSaga 200W Solar Panel + Explorer 2000 v2 Portable Power Station Bundle, 2042Wh", None,
     "bundle-no-kit"),
]


@pytest.mark.parametrize("brand,title,model_id,reason", SPEC_AND_PHRASING_CASES,
                         ids=[f"s{i:02d}-{c[3]}" for i, c in enumerate(SPEC_AND_PHRASING_CASES)])
def test_panel_specs_phrasing_and_panel_identity(idx, brand, title, model_id, reason):
    res = M.match(brand, title, idx)
    assert (res.model_id, res.reason_code) == (model_id, reason)


def test_kit_names_read_panel_counts_identity_and_build(idx):
    assert idx.kit_info(KIT_X2000V2_DUNE).panels == {200: 2}           # '200W(Dune White) x 2'
    assert idx.kit_info(KIT_X2000V2_DUNE).named_panel_ids == {"jackery-solarsaga-200w"}
    assert idx.kit_info(KIT_X2000V2_200).panel_ids == {"jackery-solarsaga-200w"}
    assert idx.kit_info(
        "jackery-solar-generator-bundle-homepower-2000-plus-battery-pack-2000-plus-v2-200w-dune-white-x2").panels \
        == {200: 2}
    assert idx.kit_info(KIT_R1500_SF200).panel_forms == {"flexible"}
    assert idx.kit_info(KIT_2X100).panel_forms == {"rigid"}
    assert idx.kit_info("ecoflow-delta-3-max-plus-solar-generator-2-x-160w-solar-panel").panel_forms == frozenset()


def test_digit_and_measure_only_names_are_not_designations():
    assert M.designations_of(["DJI Power 1000"], brand_prefixes=["DJI Power"])[0] == set()
    assert M.designations_of(["200W 12V N-Type Portable Solar Panel Suitcase Kit"])[0] == set()
    assert M.designations_of(["Power 1000"], brand_prefixes=["DJI"])[0] == {"power1000"}
    recs = [M.ModelRecord(model_id="p1000", brand="DJI", kind="station", name="Power 1000",
                          aliases=("DJI Power 1000",), capacity_wh=1024, power_w=2200),
            M.ModelRecord(model_id="p2000", brand="DJI", kind="station", name="Power 2000",
                          aliases=("DJI Power 2000",), capacity_wh=2048, power_w=3000)]
    index = M.ModelIndex(recs, brand_aliases={"DJI": ["DJI Power"]})
    assert "1000" not in index.brands["dji"].designations
    title = "DJI Power 2000 Portable Power Station, 2048Wh LiFePO4, 3000W Output, Recharges at 1000 Watts from AC"
    assert M.match("DJI", title, index).model_id == "p2000"


def test_a_code_read_alone_never_takes_another_models_code():
    station = M.ModelRecord(model_id="st", brand="BougeRV", kind="station", name="ROVER2000 Semi-Solid State",
                            capacity_wh=2008, power_w=2500)
    battery = M.ModelRecord(model_id="bat", brand="BougeRV", kind="battery",
                            name="ROVER2000 Semi-Solid Extra Battery", capacity_wh=2008)
    index = M.ModelIndex([station, battery])
    assert "rover2000" not in index.brands["bougerv"].designations
    assert M.match("BougeRV", "BougeRV Rover 2000 Portable Power Station, 2008Wh", index).model_id is None
    assert M.match("BougeRV", "BougeRV ROVER2000 Semi-Solid Extra Battery 2008Wh", index).model_id == "bat"
    index = M.ModelIndex([M.ModelRecord(**{**station.__dict__, "aliases": ("ROVER2000",)}), battery])
    assert index.brands["bougerv"].designations["rover2000"] == {"st"}
    assert M.match("BougeRV", "BougeRV ROVER2000 Portable Power Station, 2008Wh", index).model_id == "st"
    # a code no other model uses still names its model alone
    assert "ps200" in _index().brands["ankersolix"].designations


def test_bundle_no_kit_carries_our_signature_only(idx):
    res = M.match("EcoFlow", "EcoFlow DELTA 3 Max Plus with 220W Bifacial Solar Panel QZXSENTINEL", idx)
    assert res.reason_code == "bundle-no-kit"
    assert res.to_dict()["bundle"] == {"station_id": "ecoflow-delta-3-max-plus", "panels": {"220": 1},
                                       "battery": False, "panel_ids": [], "battery_ids": []}
    assert "qzx" not in json.dumps(res.to_dict()).lower()
    hash(res)
    res = M.match("Jackery", "Jackery Explorer 1000 Plus with 2 x SolarSaga 100 Prime Solar Panels", idx)
    assert res.bundle.to_dict() == {"station_id": J1000P, "panels": {"100": 2}, "battery": False,
                                    "panel_ids": ["jackery-solarsaga-100-prime"], "battery_ids": []}
    assert M.match("EcoFlow", "EcoFlow DELTA 3 Max Plus with Solar Panel", idx).bundle is None   # unverified
    assert M.match("EcoFlow", "EcoFlow DELTA 2 Portable Power Station 1024Wh", idx).to_dict().keys() == {
        "model_id", "confidence", "rule", "reason_code"}


FIELD_TITLE = "EcoFlow Portable Power Station 1024Wh LiFePO4, 1800W AC Output for Camping"
# (brand field, title, manufacturer field values, expected model id or None, expected reason)
MODEL_FIELD_CASES = [
    ("EcoFlow", FIELD_TITLE, ["EFD330"], "ecoflow-delta-2", "matched"),
    ("EcoFlow", FIELD_TITLE, ["EFD330", "EFD330"], "ecoflow-delta-2", "matched"),
    ("EcoFlow", FIELD_TITLE, ["DELTA 2"], "ecoflow-delta-2", "matched"),
    ("EcoFlow", FIELD_TITLE, ["EFD330-US"], "ecoflow-delta-2", "matched"),
    ("Jackery", "Jackery Portable Power Station 1264Wh LiFePO4, 2000W AC Output", ["JE-1000C"], J1000P, "matched"),
    ("GRECELL", "GRECELL Portable Power Station 1000W, 999Wh Solar Generator with 3 AC Outlets", ["T1000"],
     "grecell-t1000-1000w-999wh", "matched"),
    # values that never count, or that disagree
    ("EcoFlow", FIELD_TITLE, ["EFD330", "EFD350"], None, "ambiguous"),
    ("EcoFlow", FIELD_TITLE, ["DELTA 2 / DELTA 2 Max"], None, "ambiguous"),
    ("EcoFlow", FIELD_TITLE, ["1024"], None, "no-designation"),
    ("EcoFlow", FIELD_TITLE, ["1800W"], None, "no-designation"),
    ("EcoFlow", FIELD_TITLE, ["Portable Power Station"], None, "no-designation"),
    ("EcoFlow", FIELD_TITLE, ["DELTA 2 Ultra"], None, "unknown-variant"),
    # the title still decides what the listing is, and its stated specs veto or confirm
    ("EcoFlow", "EcoFlow Portable Power Station 2048Wh LiFePO4", ["EFD330"], None, "capacity-mismatch"),
    ("EcoFlow", "EcoFlow Portable Power Station for Camping and Home Backup", ["EFD330"], None,
     "model-field-unconfirmed"),
    ("EcoFlow", "EcoFlow Waterproof Bag for Portable Power Station, Dustproof Cover", ["EFD330"], None, "accessory"),
    ("EcoFlow", "EcoFlow Smart Extra Battery 2048Wh LiFePO4 for Portable Power Station", ["EFD350"], None,
     "item-kind-mismatch"),
    ("EcoFlow", "EcoFlow Portable Power Station 1024Wh (Renewed)", ["EFD330"], None, "condition"),
    ("EcoFlow", "EcoFlow Portable Power Station 1024Wh, 2 Pack", ["EFD330"], None, "multi-pack"),
    # a title naming a designation never consults the fields; an unknown brand never does either
    ("EcoFlow", "EcoFlow DELTA 2 Max Portable Power Station 2048Wh", ["EFD330"], "ecoflow-delta-2-max", "matched"),
    ("Acme", "Acme Portable Power Station 1024Wh LiFePO4, 1800W AC Output", ["EFD330"], None, "brand-unknown"),
    # kits: only through the bundle path
    ("EF ECOFLOW", "EF ECOFLOW Portable Power Station 572Wh LiFePO4, 600W AC Output", ["RIVER 3 Max"], None,
     "bundle-unverified"),
    ("EcoFlow", "EcoFlow Solar Generator with 2 x 100W Rigid Solar Panels, 2048Wh", ["DELTA 3 Max Plus"],
     KIT_2X100, "matched"),
    ("EcoFlow", "EcoFlow Solar Generator with 220W Bifacial Solar Panel, 2048Wh", ["DELTA 3 Max Plus"], None,
     "bundle-no-kit"),
]


@pytest.mark.parametrize("brand,title,fields,model_id,reason", MODEL_FIELD_CASES,
                         ids=[f"f{i:02d}-{c[4]}" for i, c in enumerate(MODEL_FIELD_CASES)])
def test_manufacturer_model_fields(idx, brand, title, fields, model_id, reason):
    res = M.match(brand, title, idx, model_fields=fields)
    assert (res.model_id, res.reason_code) == (model_id, reason)
    if model_id and res.rule == "model-field":
        assert res.confidence == M.RULE_CONFIDENCE["model-field"]


def test_model_field_links_are_their_own_rule_and_carry_no_field_text(idx):
    assert 0.9 <= M.RULE_CONFIDENCE["model-field"] < M.RULE_CONFIDENCE["designation"]
    res = M.match("EcoFlow", FIELD_TITLE, idx, model_fields=["EFD330 QZXSENTINEL"])
    assert (res.model_id, res.rule) == ("ecoflow-delta-2", "model-field")
    assert "qzx" not in json.dumps(res.to_dict()).lower()
    assert M.match("EcoFlow", FIELD_TITLE, idx).reason_code == "no-designation"     # no fields: as before
