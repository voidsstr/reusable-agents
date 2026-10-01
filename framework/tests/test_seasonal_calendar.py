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
    # Announced Oct 6-7 on aboutamazon.com; the 10-07 estimate it replaced
    # put "October 7-8" into published articles.
    assert sc.resolve_date(pb, 2026) == dt.date(2026, 10, 6)
    assert sc.resolve_date(_occ("prime-big-deal-days-kitchen"), 2026) == dt.date(2026, 10, 6)
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


# ---- event_state / recently_ended / next_occasion (seasonal-no-post-event-state)

def test_event_state_strict_window_no_now_slack():
    pb = _occ("prime-big-deal-days")
    states = {d: sc.event_state(pb, dt.date(2026, 10, d)) for d in (5, 6, 7, 8, 9)}
    assert states == {5: "before", 6: "during", 7: "during", 8: "ended", 9: "ended"}
    # id and holiday:<id> tag resolve to the same occasion
    assert sc.event_state("prime-big-deal-days-kitchen", dt.date(2026, 10, 8)) == "ended"
    assert sc.event_state("holiday:prime-big-deal-days", dt.date(2026, 10, 7)) == "during"
    assert sc.event_state("holiday:none", dt.date(2026, 10, 7)) is None


def test_event_state_multi_day_and_year_spill():
    # Christmas window Dec 25 .. Jan 3: Jan 2 is still last year's window.
    assert sc.event_state("christmas", dt.date(2027, 1, 2)) == "during"
    assert sc.event_state("christmas", dt.date(2027, 1, 4)) == "before"
    # Black Friday 2026: Nov 27-30 (Thanksgiving Nov 26)
    assert sc.event_state("black-friday", dt.date(2026, 11, 30)) == "during"
    assert sc.event_state("black-friday", dt.date(2026, 12, 1)) == "ended"


def test_event_state_honors_storage_date_override(storage):
    storage.write_json(sc.CONFIG_KEY, {
        "date_overrides": {"prime-big-deal-days": {"2026": "10-13"}}})
    assert sc.event_state("prime-big-deal-days", dt.date(2026, 10, 8),
                          storage=storage) == "before"


def test_recently_ended_fires_day_after_and_only_for_deals():
    assert sc.recently_ended(dt.date(2026, 10, 7)) == []
    ended = sc.recently_ended(dt.date(2026, 10, 8))
    assert {w.occasion.id for w in ended} == {"prime-big-deal-days",
                                              "prime-big-deal-days-kitchen"}
    w = ended[0]
    assert (w.start, w.end) == (dt.date(2026, 10, 6), dt.date(2026, 10, 7))
    assert w.date_range_label() == "October 6–7, 2026"
    # lookback: still caught a week late, not after
    assert sc.recently_ended(dt.date(2026, 10, 14), lookback_days=7)
    assert sc.recently_ended(dt.date(2026, 10, 15), lookback_days=7) == []
    # holidays are not refreshed unless asked for
    labor = sc.recently_ended(dt.date(2026, 9, 11), kinds=None)
    assert "labor-day" in {w.occasion.id for w in labor}
    assert "labor-day" not in {w.occasion.id for w in sc.recently_ended(dt.date(2026, 9, 11))}
    assert {w.occasion.id for w in sc.recently_ended(dt.date(2026, 10, 8),
                                                     audience="food")} == {"prime-big-deal-days-kitchen"}


def test_next_occasion_reangles_to_black_friday():
    today = dt.date(2026, 10, 8)
    food = sc.next_occasion("food", today, kind=sc.RETAIL_DEAL_KIND)
    tech = sc.next_occasion("tech", today, kind=sc.RETAIL_DEAL_KIND)
    assert (food.occasion.id, food.start) == ("black-friday-kitchen", dt.date(2026, 11, 27))
    assert (tech.occasion.id, tech.start) == ("black-friday", dt.date(2026, 11, 27))
    # without a kind the next food-or-general occasion is Halloween
    assert sc.next_occasion("food", today).occasion.id == "halloween"
    assert sc.next_occasion("tech", today, kind=sc.RETAIL_DEAL_KIND,
                            horizon_days=30) is None


def test_ended_sale_drops_out_of_now_window():
    # The +/-3-day NOW slack used to keep "SHIP NOW: Prime Big Deal Days"
    # in the proposer prompt on Oct 8-9, after the sale had closed.
    on = {a.occasion.id for a in sc.active_signal(dt.date(2026, 10, 7), audience="tech")}
    after = {a.occasion.id for a in sc.active_signal(dt.date(2026, 10, 8), audience="tech")}
    assert "prime-big-deal-days" in on and "prime-big-deal-days" not in after
    # holidays keep the late-entry slack
    assert "halloween" in {a.occasion.id for a in sc.active_signal(dt.date(2026, 11, 2),
                                                                   audience="food")}


def test_deal_guidance_requires_absolute_dates_and_yearless_slugs():
    g = _occ("prime-big-deal-days").guidance
    assert "absolute date" in g and "never a countdown" in g
    assert "year-less slug" in g and "never rename an existing slug" in g
    assert _occ("prime-big-deal-days").kind == sc.RETAIL_DEAL_KIND
    assert _occ("halloween").kind == ""


def test_format_date_range():
    assert sc.format_date_range(dt.date(2026, 11, 27), dt.date(2026, 11, 27)) == "November 27, 2026"
    assert sc.format_date_range(dt.date(2026, 11, 27), dt.date(2026, 12, 1)) == "November 27 – December 1, 2026"
    assert sc.format_date_range(dt.date(2026, 12, 25), dt.date(2027, 1, 3)) == "December 25, 2026 – January 3, 2027"
