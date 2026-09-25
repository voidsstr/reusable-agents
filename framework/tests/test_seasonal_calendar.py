"""Calibration tests for framework.core.seasonal_calendar."""
import datetime as dt

from framework.core import seasonal_calendar as sc


def _occ(oid):
    return next(o for o in sc.DEFAULT_OCCASIONS if o.id == oid)


def test_black_friday_is_day_after_thanksgiving():
    # 2024: Nov 1 was a Friday, so "4th Friday" (Nov 22) was a week early.
    for year in (2023, 2024, 2025, 2026, 2027, 2030):
        tg = sc.resolve_date(_occ("thanksgiving"), year)
        assert sc.resolve_date(_occ("black-friday"), year) == tg + dt.timedelta(days=1)
        assert sc.resolve_date(_occ("black-friday-kitchen"), year) == tg + dt.timedelta(days=1)


def test_prime_big_deal_days_year_dates_and_rule():
    pb = _occ("prime-big-deal-days")
    assert sc.resolve_date(pb, 2025) == dt.date(2025, 10, 7)
    assert sc.resolve_date(pb, 2026) == dt.date(2026, 10, 7)
    # Rule fallback: 2nd Tuesday of October
    assert sc.resolve_date(pb, 2024) == dt.date(2024, 10, 8)
    assert sc.resolve_date(pb, 2027) == dt.date(2027, 10, 12)


def test_deal_events_imminent_per_audience():
    today = dt.date(2026, 9, 25)
    tech = {a.occasion.id: a.window for a in sc.active_signal(today, audience="tech")}
    food = {a.occasion.id: a.window for a in sc.active_signal(today, audience="food")}
    assert tech.get("prime-big-deal-days") == "imminent"
    assert "prime-big-deal-days-kitchen" not in tech
    assert food.get("prime-big-deal-days-kitchen") == "imminent"
    assert "prime-big-deal-days" not in food
    bf_day = dt.date(2026, 11, 16)
    tech = {a.occasion.id: a.window for a in sc.active_signal(bf_day, audience="tech")}
    food = {a.occasion.id: a.window for a in sc.active_signal(bf_day, audience="food")}
    assert tech.get("black-friday") == "imminent"
    assert food.get("black-friday-kitchen") == "imminent"


def test_deal_occasions_carry_keywords_and_links():
    for oid in ("prime-big-deal-days", "black-friday"):
        o = _occ(oid)
        assert o.recipe_keywords and o.link_categories and o.guidance
        for cat in ("gpus", "prebuilt-gaming-pcs", "monitors", "gaming-desks"):
            assert cat in o.link_categories


def test_imminent_block_has_links_tag_and_guidance():
    today = dt.date(2026, 9, 25)
    block = sc.build_prompt_block(sc.active_signal(today, audience="tech"),
                                  today=today, category_path_prefix="/category/")
    assert "holiday:prime-big-deal-days" in block
    assert "/category/<slug>" in block and "gaming-desks" in block
    assert "Never invent a discount" in block


def test_storage_date_override_and_new_occasion(storage):
    storage.write_json(sc.CONFIG_KEY, {
        "date_overrides": {"prime-big-deal-days": {"2026": "10-14"}},
        "occasions": [{"id": "launch-x", "label": "Launch X",
                       "month_day": "10-01", "audience": "tech",
                       "link_categories": ["gpus"]}],
        "disabled": ["halloween"],
    })
    occs = {o.id: o for o in sc.load_occasions(storage)}
    assert sc.resolve_date(occs["prime-big-deal-days"], 2026) == dt.date(2026, 10, 14)
    assert occs["launch-x"].link_categories == ("gpus",)
    assert "halloween" not in occs
    act = {a.occasion.id for a in sc.active_signal(dt.date(2026, 9, 25),
                                                   audience="tech", storage=storage)}
    assert "launch-x" in act and "halloween" not in act


def test_bad_storage_config_falls_back(storage):
    storage.write_json(sc.CONFIG_KEY, {"occasions": [{"id": "broken"}]})
    assert len(sc.load_occasions(storage)) == len(sc.DEFAULT_OCCASIONS)
