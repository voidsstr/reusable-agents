"""framework.core.brand_identity: brand/manufacturer pairs that are normal
marketplace patterns are explained; only a real disagreement is `differs`."""
from __future__ import annotations

import pytest

from framework.core import brand_identity as bi


@pytest.fixture(scope="module")
def cfg():
    return bi.resolve({"extra_brand_groups": [["nexigo", "nexight"]]})


@pytest.mark.parametrize("brand, manufacturer, title, want", [
    # parent company / spin-off
    ("Western Digital", "Sandisk Technologies, Inc.", "WD 1TB My Passport SSD", "alias"),
    ("Sandisk", "Western Digital Technologies, Inc.", "WD_Black SN770M 2TB SSD", "alias"),
    # legal entity, invisible U+200E, line suffix, city prefix, store suffix
    ("Western Digital", "‎Western Digital Technologies, Inc.", "WD 4TB Elements", "same"),
    ("Sandisk", "Sandisk Technologies, Inc.", "WD_BLACK SN850X 2TB", "same"),
    ("Logitech G", "Logitech", "Logitech Creators Blue Yeti", "same"),
    ("ORICO", "SHENZHEN ORICO TECHNOLOGIES CO., LTD", "ORICO MiniMate", "same"),
    ("TESSAN", "Zhuhai Tessan Power Technology Co., Ltd", "TESSAN travel adapter", "same"),
    ("Seagate", "Seagate Bare Drives", "Seagate BarraCuda 4TB", "same"),
    ("Cooler Master", "CoolerMaster", "Cooler Master fan", "same"),
    # renewed / reseller / no-brand markers
    ("Amazon Renewed", "Koncept", "WD19 Dell Docking Station", "reseller"),
    ("Samsung", "Oemgenuine", "OEM Samsung 256GB", "reseller"),
    ("Generic", "Pukami", "Gaming Chair", "reseller"),
    # platform brand on a partner-built or resold part
    ("Intel", "SweetWay", "Intel Pentium 4 3.4GHZ", "partner"),
    ("NVIDIA", "Asus", "NVIDIA GeForce RTX 3070", "partner"),
    # missing manufacturer
    ("Corsair", "", "Corsair Vengeance DDR5", "unknown"),
    ("", "Unknown", "Intel Core i9-13900K", "unknown"),
    # no brand: the title stands in
    ("", "msi", "MSI Gaming Radeon RX 6700 XT", "same"),
    ("", "GIGABYTE", "MSI Gaming GeForce RTX 2060 Super", "differs"),
    ("", "Foxiod", "3 Pack Cable Management Sleeve", "unknown"),
    # configured extra group
    ("NexiGo", "Nexight INC", "NexiGo N60 Pro", "alias"),
    # real disagreements
    ("Cylewet", "Qianxin", "Cylewet 12Pcs push button", "differs"),
    ("Lexar", "OHEDMEH", "Lexar 64GB CF", "differs"),
    ("Western Digital", "Digital Ally", "x", "differs"),
])
def test_relation(cfg, brand, manufacturer, title, want):
    assert bi.relation(brand, manufacturer, title=title, cfg=cfg) == want
    assert (want in bi.EXPLAINED) == (want != "differs")


def test_resolve_list_keys_replace_and_extra_keys_append():
    c = bi.resolve({"platform_brands": ["acme"], "extra_reseller_patterns": [r"\bopen box\b"]})
    assert c["platform_brands"] == ["acme"]
    assert r"\bopen box\b" in c["reseller_patterns"] and r"\brenewed\b" in c["reseller_patterns"]
    assert bi.is_reseller("Open Box Deals", c) and not bi.is_platform("Intel", c)
    # the default config is not mutated by a caller's overrides
    assert "intel" in bi.DEFAULT_CONFIG["platform_brands"]
    assert bi.relation("NexiGo", "Nexight INC") == "differs"


def test_normalize_and_groups():
    assert bi.normalize("SHENZHEN ORICO TECHNOLOGIES CO., LTD") == "orico"
    assert bi.normalize("‎Western Digital Technologies, Inc.") == "western digital"
    assert bi.normalize("Inc.") == "inc"  # nothing but stop tokens: kept as folded
    assert bi.normalize("Western\nDigital\tInc") == "western digital"  # whitespace splits words
    wd = bi.groups_for("Western Digital")
    assert wd and wd == bi.groups_for("SanDisk Technologies") == bi.groups_for("WD_BLACK")
    assert not bi.groups_for("Qianxin")
    # a name can sit in two groups (HyperX: Kingston memory, HP peripherals)
    assert len(bi.groups_for("HyperX")) == 2


def test_makers_in_text_matches_whole_tokens_longest_first():
    found = bi.makers_in_text(
        "Product name claims 'WD_BLACK SN850X' (a Western Digital product), but the "
        "description recommends 'SANDISK Optimus GX PRO 850X'")
    assert len(found) == 1
    (names,) = found.values()
    assert names == {"wd black", "western digital", "sandisk optimus"}
    # "wd" inside "wd black" and "sandisk" inside "sandisk optimus" are not
    # counted twice; a substring of a word never matches
    assert bi.makers_in_text("the swd drive and a Sandiskette") == {}
    # bold-math letters some listings use fold to plain ASCII
    assert bi.makers_in_text("\U0001d7ef\U0001d5e0 Tape | Car Joystick")
