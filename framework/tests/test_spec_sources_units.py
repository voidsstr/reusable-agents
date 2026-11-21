"""framework.core.spec_sources: unit normalisers, quantity/range/dimension
parsing and field-value parsing. Strings are taken from the real fixtures
(manufacturer pages and manuals) wherever possible. Offline, no I/O."""
from __future__ import annotations

import pytest

from framework.core import spec_sources as ss


def _q(text, **kw):
    return [(q.value, q.unit, q.role, q.qualifier) for q in ss.find_quantities(text, **kw)]


# ---------------------------------------------------------------------------
# Units and conversion
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,value,unit", [
    ("1070 Wh", 1070.0, "Wh"),
    ("2,048Wh", 2048.0, "Wh"),             # thousands separator
    ("3.6 kWh", 3.6, "kWh"),
    ("3000w", 3000.0, "W"),                 # lower-case attached w
    ("Rated 2000W", 2000.0, "W"),
    ("12V⎓10A", 12.0, "V"),
    ("35.2V DC", 35.2, "V"),
    ("75Ah", 75.0, "Ah"),
    ("5200mAh", 5200.0, "mAh"),
    ("22.0Kg", 22.0, "kg"),
    ("48.5 lbs", 48.5, "lb"),
    ("4300Pa", 4300.0, "Pa"),
    ("≤ 22dB", 22.0, "dB"),
    ("65 dBA", 65.0, "dB"),
    ("10ms", 10.0, "ms"),
    ("within about 8 milliseconds", 8.0, "ms"),
    ("1.58 Hours", 1.58, "h"),
    ("3.5~4H", 3.5, "h"),
    ("300 minutes", 300.0, "min"),
    ("60HZ", 60.0, "Hz"),
    ("420 ml", 420.0, "mL"),
    ("45 ℃", 45.0, "degC"),                 # NFKC: ℃ -> °C
    ("113℉", 113.0, "degF"),
    ("386mm", 386.0, "mm"),
    ("12.64 inch", 12.64, "in"),
    ("-0.27 %/°C", -0.27, "%/degC"),
    ("1,5 kg", 1.5, "kg"),                  # decimal comma
])
def test_first_quantity(text, value, unit):
    qs = ss.find_quantities(text)
    assert qs, text
    assert qs[0].value == pytest.approx(value)
    assert qs[0].unit == unit


@pytest.mark.parametrize("value,src,dst,expected", [
    (23.8, "lb", "kg", 10.7955),
    (48.5, "lb", "kg", 21.9992),
    (32, "degF", "degC", 0.0),
    (14, "degF", "degC", -10.0),
    (-4, "degF", "degC", -20.0),
    (113, "degF", "degC", 45.0),
    (0, "degC", "degF", 32.0),
    (12.87, "in", "mm", 326.898),
    (32.7, "cm", "mm", 327.0),
    (5200, "mAh", "Ah", 5.2),
    (3.6, "kWh", "Wh", 3600.0),
    (20, "ms", "s", 0.02),
    (90, "min", "h", 1.5),
    (1, "L", "mL", 1000.0),
    (-0.15, "%/degF", "%/degC", -0.27),
    (16, "oz", "lb", 1.0),
])
def test_convert(value, src, dst, expected):
    assert ss._round_sig(ss.convert(value, src, dst), 6) == pytest.approx(expected, abs=1e-4)


def test_convert_rejects_cross_family_and_unknown_units():
    with pytest.raises(ValueError):
        ss.convert(1, "W", "Wh")
    with pytest.raises(ValueError):
        ss.convert(1, "furlong", "mm")


@pytest.mark.parametrize("spelling,canonical", [
    ("°C", "degC"), ("℃", "degC"), ("°F", "degF"), ("mah", "mAh"), ("ml", "mL"),
    ("lbs", "lb"), ("dBA", "dB"), ("hours", "h"), ("Wh", "Wh"), ("nonsense", ""),
])
def test_canonical_unit(spelling, canonical):
    assert ss.canonical_unit(spelling) == canonical


def test_normalize_text_folds_compatibility_forms():
    assert ss.normalize_text("LiFePO₄") == "LiFePO4"
    assert ss.normalize_text("0℃～45℃（Charging）") == "0°C~45°C(Charging)"
    assert ss.normalize_text("−20 °C") == "-20 °C"


# ---------------------------------------------------------------------------
# Ranges, qualifiers, things that are not quantities
# ---------------------------------------------------------------------------

def test_ranges_from_real_spec_strings():
    assert _q("Supports 16–60V Single Port Usage") == [(16.0, "V", "lo", ""), (60.0, "V", "hi", "")]
    assert _q("12–16V (Voltage Range)")[:2] == [(12.0, "V", "lo", ""), (16.0, "V", "hi", "")]
    assert _q("0 ℃ ~ 45 ℃") == [(0.0, "degC", "lo", ""), (45.0, "degC", "hi", "")]
    assert _q("-15-40℃ (-4-104℉)") == [(-15.0, "degC", "lo", ""), (40.0, "degC", "hi", ""),
                                       (-4.0, "degF", "lo", ""), (104.0, "degF", "hi", "")]
    assert _q("100–120 V AC ~ 60 Hz")[:3] == [(100.0, "V", "lo", ""), (120.0, "V", "hi", ""),
                                             (60.0, "Hz", "", "")]
    assert _q("11.5-50V/20A/500W max") == [(11.5, "V", "lo", ""), (50.0, "V", "hi", ""),
                                           (20.0, "A", "", ""), (500.0, "W", "", "max")]


def test_the_jackery_dc_input_row_holds_two_bands():
    qs = ss.find_quantities("(2x) DC 8mm Ports: 12-16V(Working Voltage)⎓8A Max, Double to 8A Max; "
                            "16-60V(Working Voltage)⎓10.5A Max, Double to 21A/400W Max")
    volts = [(q.value, q.role) for q in qs if q.unit == "V"]
    assert volts == [(12.0, "lo"), (16.0, "hi"), (16.0, "lo"), (60.0, "hi")]
    assert [q.value for q in qs if q.unit == "A"] == [8.0, 8.0, 10.5, 21.0]
    assert [q.value for q in qs if q.unit == "W"] == [400.0]


def test_mixed_family_dash_is_not_a_range():
    # Pecron writes USB outputs as "5V-2A": a voltage and a current, not a range.
    assert _q("5V-2A") == [(5.0, "V", "", ""), (2.0, "A", "", "")]


def test_bare_numbers_need_a_unit_from_the_label():
    assert _q("200-300") == []
    assert _q("200-300", default_unit="min") == [(200.0, "min", "lo", ""), (300.0, "min", "hi", "")]
    assert [q.value for q in ss.find_quantities("65/66/71/74", default_unit="dB")] == [65, 66, 71, 74]


@pytest.mark.parametrize("text", ["XT-60 Output", "E2000LFP", "GX16MF *2", "DC8020", "IP68",
                                  "2 x QC3.0", "Model JE-1000D", "5-Year warranty"])
def test_model_codes_and_counts_are_not_quantities(text):
    assert ss.find_quantities(text) == []


@pytest.mark.parametrize("text,qualifier", [
    ("≤ 22dB", "lte"),
    ("< 500W", "lte"),
    ("within about 8 milliseconds", "approx+lte"),
    ("Around 48.5lb", "approx"),
    ("≈2 Hours", "approx"),
    ("~8ms", "approx"),
    ("Max 1200W", "max"),
    ("1100W Max.", "max"),
    ("up to 300 minutes", "max"),
    ("at least 16V", "gte"),
    ("1500W Rated", ""),
])
def test_qualifiers(text, qualifier):
    assert ss.find_quantities(text)[0].qualifier == qualifier


def test_tilde_used_for_ac_is_not_approximation():
    hz = [q for q in ss.find_quantities("(3x) 120V ~ 60Hz") if q.unit == "Hz"][0]
    assert hz.qualifier == ""


# ---------------------------------------------------------------------------
# Dimensions and label units
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,default,expected,unit", [
    ("L*W*H=396X284X321mm", "", (396, 284, 321), "mm"),
    ("12.87 × 8.82 × 9.72 in / 32.7 × 22.4 × 24.7 cm", "", (12.87, 8.82, 9.72), "in"),
    ("L16.1*W10*H11.6 in L409*W256*H297 mm", "", (16.1, 10, 11.6), "in"),
    ("15.20x11.18x12.64 inch (386mm*284mm*321mm)", "", (15.2, 11.18, 12.64), "in"),
    ("325 * 325 * 99 mm", "", (325, 325, 99), "mm"),
    ("350x350x93", "mm", (350, 350, 93), "mm"),
])
def test_dimensions(text, default, expected, unit):
    d = ss.find_dimensions(text, default_unit=default)[0]
    assert (d.a, d.b, d.c) == pytest.approx(expected)
    assert d.unit == unit


def test_dimensions_without_any_unit_are_ignored():
    assert ss.find_dimensions("350x350x93") == []
    assert ss.find_dimensions("2 x QC3.0 18W") == []


@pytest.mark.parametrize("label,units", [
    ("Battery Specifications (Type, mAh)", ["mAh"]),
    ("Size of Robot (φxH,mm)", ["mm"]),
    ("Noise Value (dBA )", ["dB"]),
    ("Working Time (min)", ["min"]),
    ("Charging Time (H)", ["h"]),
    ("Dust Bin Capacity (ml)", ["mL"]),
    ("Product Dimensions (cm) (W)Length × (D)Width × (H)Height", ["cm", "W", "h"]),
    ("Capacity", []),
])
def test_units_in_label(label, units):
    assert ss.units_in_label(label) == units


# ---------------------------------------------------------------------------
# parse_field_value
# ---------------------------------------------------------------------------

def _f(name="f", **kw):
    return ss.FieldRegistry({name: kw})[name]


def test_quantity_converts_to_the_field_unit_and_keeps_the_qualifier():
    pv = ss.parse_field_value(_f(kind="quantity", unit="kg"), "Around 23.8 lbs (10.8 kg)")
    assert pv.value_num == pytest.approx(10.7955)
    assert pv.unit == "kg" and pv.qualifier == "approx"


def test_quantity_ignores_other_families():
    pv = ss.parse_field_value(_f(kind="quantity", unit="Wh"), "30.4Ah/35.2V DC (1070 Wh)")
    assert pv.value_num == 1070.0


def test_unit_from_label_parentheses():
    spec = _f(kind="quantity", unit="mL")
    assert ss.parse_field_value(spec, "420") is None
    assert ss.parse_field_value(spec, "420", label="Dust Bin Capacity (ml)").value_num == 420.0


@pytest.mark.parametrize("pick,expected", [
    ("first", 12.0), ("last", 60.0), ("min", 12.0), ("max", 60.0), ("range_lo", 12.0), ("range_hi", 16.0),
])
def test_picks(pick, expected):
    text = "12-16V car; 16-60V solar"
    assert ss.parse_field_value(_f(kind="quantity", unit="V"), text, pick=pick).value_num == expected


def test_range_picks_are_strict():
    # A single "60V Max" must not be read as the bottom of a window.
    assert ss.parse_field_value(_f(kind="quantity", unit="V"), "60V Max", pick="range_lo") is None


def test_count_enum_bool_text_and_dimensions():
    assert ss.parse_field_value(_f(kind="count"), "3,500+ Cycles to 80%").value_num == 3500
    assert ss.parse_field_value(_f(kind="count"), "> 3500 Times").qualifier == "gte"
    chem = _f(kind="enum", values={"LiFePO4": ["lifepo4", "lithium iron phosphate"], "NMC": ["\\bnmc\\b"]})
    assert ss.parse_field_value(chem, "LiFePO₄").value_text == "LiFePO4"
    assert ss.parse_field_value(chem, "Lithium Iron Phosphate").value_text == "LiFePO4"
    assert ss.parse_field_value(chem, "Lead acid") is None
    ups = _f(kind="bool", values={"false": ["not supported"], "true": ["\\bups\\b", "supported"]})
    # negation-aware: "not supported" also contains "supported" and "UPS"
    assert ss.parse_field_value(ups, "The UPS function is not supported.").value_text == "false"
    assert ss.parse_field_value(ups, "UPS supported").value_num == 1.0
    assert ss.parse_field_value(_f(kind="text"), "  JE-1000D ;").value_text == "JE-1000D"
    dims = ss.parse_field_value(_f(kind="dimensions", unit="mm"), "15.20x11.18x12.64 inch")
    assert dims.components == pytest.approx((386.08, 283.972, 321.056))
    assert dims.value_text == "386.08×283.972×321.056"


def test_field_registry_reports_bad_fields_instead_of_raising():
    reg = ss.FieldRegistry({
        "ok": {"kind": "quantity", "unit": "W"},
        "bad_kind": {"kind": "colour"},
        "bad_unit": {"kind": "quantity", "unit": "parsecs"},
        "dims_not_length": {"kind": "dimensions", "unit": "W"},
        "enum_without_values": {"kind": "enum"},
        "bad_policy": {"kind": "quantity", "unit": "W", "resolve": "coin-flip"},
    })
    assert list(reg) == ["ok"]
    assert len(reg.problems) == 5


# ---------------------------------------------------------------------------
# Review round 1: spelled-out length units and dot-grouped thousands
# ---------------------------------------------------------------------------

def test_dimensions_spelled_out_in_inches_or_feet():
    d = ss.find_dimensions("15.3 x 10.2 x 11.6 inches")[0]
    assert (d.a, d.b, d.c, d.unit) == (15.3, 10.2, 11.6, "in")
    assert ss.find_dimensions("15.3 x 10.2 x 11.6 inches")[0].to("mm")[0] == pytest.approx(388.62)
    assert (ss.canonical_unit("inches"), ss.canonical_unit("feet"), ss.canonical_unit("foot")) == ("in", "ft", "ft")
    pv = ss.parse_field_value(_f(kind="dimensions", unit="mm"), "Size: 15.3 x 10.2 x 11.6 inches")
    assert pv.components == pytest.approx((388.62, 259.08, 294.64))


@pytest.mark.parametrize("text,value,unit", [
    ("1.000.000 mAh", 1000000.0, "mAh"),     # two dot groups: thousands
    ("1.234,5 W", 1234.5, "W"),              # dot thousands, comma decimal
    ("2.048 kWh", 2.048, "kWh"),             # one dot group stays a decimal ...
    ("1.024kWh", 1.024, "kWh"),              # ... because this is how kWh are written
    ("2,048 Wh", 2048.0, "Wh"),
    ("1,5 kg", 1.5, "kg"),
])
def test_thousands_separators(text, value, unit):
    q = ss.find_quantities(text)[0]
    assert (q.value, q.unit) == (pytest.approx(value), unit)


# ---------------------------------------------------------------------------
# Review round 2: a lone dot group in a min / max pick
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,pick", [
    # '1.000W' reads as 1.0 W, so max would take the 500 W; dot-grouped it is 1000 W
    ("12-60V⎓15A, 500W Max (single port); 1.000W Max (dual ports)", "max"),
    ("1.200W Max (dual ports); 800W Max (single port)", "min"),
])
def test_min_max_pick_refuses_a_lone_dot_group_that_could_be_thousands(text, pick):
    assert ss.parse_field_value(_f(kind="quantity", unit="W"), text, pick=pick) is None


@pytest.mark.parametrize("text,unit,pick,expected", [
    ("12-60V⎓15A, 500W Max (single port); 1000W Max (dual ports)", "W", "max", 1000.0),
    ("12-60V⎓15A, 1.200W Max", "W", "max", 1.2),    # alone: read as written, the range check decides
    ("12-60V⎓15A, 1.200W Max", "V", "max", 60.0),   # other families do not matter
    ("Capacity 2.048 kWh, expandable to 6.144 kWh", "Wh", "max", 6144.0),   # every value alike
    ("2.048 kWh", "Wh", "min", 2048.0),
])
def test_min_max_pick_keeps_rows_the_thousands_reading_does_not_change(text, unit, pick, expected):
    pv = ss.parse_field_value(_f(kind="quantity", unit=unit), text, pick=pick)
    assert pv.value_num == pytest.approx(expected)


def test_a_thousands_reading_outside_the_field_range_is_not_an_ambiguity():
    text = "2.048 kWh, expandable to 3000 Wh"       # 2048 Wh, unless '2.048' meant 2 048 000 Wh
    assert ss.parse_field_value(_f(kind="quantity", unit="Wh", max=60000), text, pick="max").value_num == 3000.0
    assert ss.parse_field_value(_f(kind="quantity", unit="Wh"), text, pick="max") is None
