"""Seasonal + holiday signal for content-authoring agents.

Every editorial / culinary / shopping site has the same problem: the LLM
needs to know what date it is *and* what holiday or season is happening
now (or imminent), so it can propose timely articles instead of evergreen
ones the catalog already covers.

Hard-coding holidays per agent is the anti-pattern we keep finding. This
module is the framework-level primitive: a single calendar of US-anchored
holidays + seasonal moments with a vocabulary the LLM understands. Sites
override / extend via `config/seasonal-calendar.json` in storage. The
default list is sensible for both food (aisleprompt) and tech-review
(specpicks) audiences and is what ships out of the box.

Three windows on every occasion:

- **NOW**      — date is today ± 3 days. The article should publish RIGHT
                  NOW (we already missed the 4-week-out runway; ship the
                  late entry).
- **IMMINENT** — date is in 4–14 days. Drop everything else and propose
                  this; competition has not closed yet, an LLM-grade
                  article shipped today still has a chance.
- **UPCOMING** — date is in 15–60 days. The classical "publish 4 weeks
                  before" window the original aisleprompt code targeted.

Each occasion carries:

- `id` — kebab-case stable id (memorial-day, easter, …) — also the
  intended `holidays` tag on the published article, so the rotation
  logic in the homepage rail can match.
- `label` — human display name.
- `recipe_keywords` — list of strings the LLM should bias `target_query`
  and `primary_keyword` toward (e.g. for memorial-day: grilling,
  cookout, BBQ, ribs, burgers, hot dogs, sides, slaw).
- `bucket_hints` — list of article-bucket ids that fit (e.g.
  `seasonal-occasion`, `meal-planning`).
- `link_categories` — kitchen-shop / category slugs the article should
  cross-link to (e.g. grills, outdoor-cookware, summer-tableware).
- `audience` — `general` | `food` | `tech` | `gaming` — when an
  occasion only makes sense for some audiences (e.g. CES is tech-only).

Sites consume two functions:

- `active_signal()` → ranked list of NOW/IMMINENT occasions to put in
  the LLM prompt. The first NOW occasion (if any) is the "feature this
  weekend" signal the user asked for on 2026-05-23.
- `cycle_featured(articles_by_holiday)` → given a map of holiday-id →
  candidate article slugs, returns the slug to feature this hour. Tracks
  a rotation cursor so multiple articles per holiday cycle.

Refer to test_seasonal_calendar.py for the calibration cases.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field, asdict
from typing import Iterable, Sequence


# ---------------------------------------------------------------------------
# Occasion data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Occasion:
    """One holiday or seasonal moment.

    `month_day` is a string `MM-DD` for fixed-date holidays, or one of
    the relative-date sentinels:
      - `nth-weekday:<n>:<weekday>:<month>` (e.g. `nth-weekday:4:thursday:11`
        = 4th Thursday of November = Thanksgiving)
      - `last-weekday:<weekday>:<month>`     (e.g. `last-weekday:monday:5`
        = last Monday of May = Memorial Day)
      - `season:<spring|summer|fall|winter>` (for season-long windows;
        the `length_days` field defines the active window)
    """
    id: str
    label: str
    month_day: str
    audience: str = "general"
    recipe_keywords: tuple[str, ...] = ()
    bucket_hints: tuple[str, ...] = ()
    link_categories: tuple[str, ...] = ()
    length_days: int = 1


# ---------------------------------------------------------------------------
# Default occasions list — US-centric, food + tech friendly
# ---------------------------------------------------------------------------

DEFAULT_OCCASIONS: tuple[Occasion, ...] = (
    Occasion(
        id="new-years",
        label="New Year's Healthy Eating",
        month_day="01-01",
        recipe_keywords=("clean eating", "detox", "whole30", "high-protein",
                         "low-carb", "vegan reset"),
        bucket_hints=("meal-planning", "diet-guide", "seasonal-occasion"),
        link_categories=("meal-prep-containers", "kitchen-scales", "blenders"),
    ),
    Occasion(
        id="super-bowl",
        label="Super Bowl Sunday",
        month_day="nth-weekday:2:sunday:2",
        recipe_keywords=("wings", "dip", "nachos", "chili", "sliders",
                         "appetizers", "game day", "party food"),
        bucket_hints=("seasonal-occasion", "meal-planning"),
        link_categories=("slow-cookers", "serving-platters", "snack-bowls"),
    ),
    Occasion(
        id="valentines-day",
        label="Valentine's Day",
        month_day="02-14",
        recipe_keywords=("date-night", "romantic dinner", "chocolate",
                         "steak", "pasta carbonara", "dessert"),
        bucket_hints=("seasonal-occasion",),
        link_categories=("cookware", "wine-glasses", "dessert-tools"),
    ),
    Occasion(
        id="st-patricks-day",
        label="St. Patrick's Day",
        month_day="03-17",
        recipe_keywords=("corned beef", "cabbage", "irish stew",
                         "soda bread", "shepherd's pie"),
        bucket_hints=("seasonal-occasion",),
        link_categories=("dutch-ovens", "slow-cookers"),
    ),
    Occasion(
        id="easter",
        label="Easter",
        month_day="04-09",  # Approximation — Easter is computed; this is the
        # 5-year-average. Sites can override in seasonal-calendar.json.
        recipe_keywords=("ham", "lamb", "deviled eggs", "carrot cake",
                         "brunch", "asparagus", "easter dinner"),
        bucket_hints=("seasonal-occasion", "meal-planning"),
        link_categories=("roasting-pans", "cake-pans", "egg-decorating"),
    ),
    Occasion(
        id="cinco-de-mayo",
        label="Cinco de Mayo",
        month_day="05-05",
        recipe_keywords=("tacos", "guacamole", "margarita", "fajitas",
                         "enchiladas", "salsa", "carnitas", "tamales"),
        bucket_hints=("seasonal-occasion",),
        link_categories=("tortilla-press", "cocktail-shakers", "molcajetes"),
    ),
    Occasion(
        id="mothers-day",
        label="Mother's Day",
        month_day="nth-weekday:2:sunday:5",
        recipe_keywords=("brunch", "mimosa", "scones", "quiche",
                         "french toast", "pancakes", "afternoon tea"),
        bucket_hints=("seasonal-occasion",),
        link_categories=("griddles", "tea-kettles", "brunch-serveware"),
    ),
    Occasion(
        id="memorial-day",
        label="Memorial Day Weekend / Grilling Season Kickoff",
        month_day="last-weekday:monday:5",
        recipe_keywords=("grilling", "BBQ", "cookout", "ribs",
                         "burgers", "hot dogs", "potato salad", "coleslaw",
                         "corn on the cob", "pasta salad", "watermelon"),
        bucket_hints=("seasonal-occasion", "meal-planning"),
        link_categories=("grills", "grill-tools", "outdoor-serveware",
                         "coolers"),
        length_days=4,
    ),
    Occasion(
        id="fathers-day",
        label="Father's Day",
        month_day="nth-weekday:3:sunday:6",
        recipe_keywords=("steak", "ribs", "burgers", "smoker",
                         "brisket", "bourbon", "manly mains"),
        bucket_hints=("seasonal-occasion",),
        link_categories=("smokers", "cast-iron", "carving-knives"),
    ),
    Occasion(
        id="july-4th",
        label="4th of July / Independence Day",
        month_day="07-04",
        recipe_keywords=("grilling", "BBQ", "hot dogs", "burgers",
                         "watermelon", "pie", "fireworks party",
                         "patriotic dessert"),
        bucket_hints=("seasonal-occasion", "meal-planning"),
        link_categories=("grills", "outdoor-serveware", "coolers"),
        length_days=3,
    ),
    Occasion(
        id="labor-day",
        label="Labor Day Weekend / End of Summer Grilling",
        month_day="nth-weekday:1:monday:9",
        recipe_keywords=("grilling", "BBQ", "end-of-summer", "corn",
                         "tomato salad", "peach", "ice cream"),
        bucket_hints=("seasonal-occasion",),
        link_categories=("grills", "ice-cream-makers"),
        length_days=4,
    ),
    Occasion(
        id="back-to-school",
        label="Back to School Meal Prep",
        month_day="08-15",
        recipe_keywords=("lunchbox", "meal prep", "after-school",
                         "freezer meals", "weeknight dinners"),
        bucket_hints=("meal-planning", "diet-guide"),
        link_categories=("lunch-containers", "meal-prep-containers"),
        length_days=21,
    ),
    Occasion(
        id="halloween",
        label="Halloween",
        month_day="10-31",
        recipe_keywords=("pumpkin", "spooky desserts", "caramel apple",
                         "chili", "trick-or-treat", "spider cupcakes"),
        bucket_hints=("seasonal-occasion",),
        link_categories=("cookie-cutters", "cake-decorating"),
    ),
    Occasion(
        id="thanksgiving",
        label="Thanksgiving",
        month_day="nth-weekday:4:thursday:11",
        recipe_keywords=("turkey", "stuffing", "cranberry sauce",
                         "pumpkin pie", "mashed potatoes", "gravy",
                         "green bean casserole"),
        bucket_hints=("seasonal-occasion", "meal-planning"),
        link_categories=("roasting-pans", "meat-thermometers",
                         "pie-dishes", "carving-sets"),
        length_days=5,
    ),
    Occasion(
        id="black-friday",
        label="Black Friday / Cyber Monday",
        month_day="nth-weekday:4:friday:11",  # day after Thanksgiving
        audience="tech",
        recipe_keywords=(),
        bucket_hints=("buying-guide", "kitchen-buying-guide"),
        link_categories=("deals",),
        length_days=4,
    ),
    Occasion(
        id="christmas",
        label="Christmas / Holiday Baking",
        month_day="12-25",
        recipe_keywords=("holiday baking", "cookies", "gingerbread",
                         "ham", "prime rib", "eggnog", "christmas dinner"),
        bucket_hints=("seasonal-occasion", "meal-planning"),
        link_categories=("cookie-sheets", "stand-mixers",
                         "holiday-serveware"),
        length_days=10,
    ),
    Occasion(
        id="new-years-eve",
        label="New Year's Eve",
        month_day="12-31",
        recipe_keywords=("appetizers", "champagne cocktails",
                         "midnight snacks", "party dips"),
        bucket_hints=("seasonal-occasion",),
        link_categories=("champagne-flutes", "appetizer-platters"),
    ),
    # ---- Tech / gaming / specpicks audience ----
    Occasion(
        id="ces",
        label="CES (Consumer Electronics Show)",
        month_day="01-07",
        audience="tech",
        bucket_hints=("trending-ai", "buying-guide"),
        length_days=4,
    ),
    Occasion(
        id="prime-day",
        label="Amazon Prime Day",
        month_day="07-16",  # approximation; Amazon announces date
        audience="tech",
        bucket_hints=("buying-guide", "kitchen-buying-guide"),
        length_days=2,
    ),
    Occasion(
        id="back-to-school-tech",
        label="Back to School (laptops / desks / monitors)",
        month_day="08-01",
        audience="tech",
        bucket_hints=("buying-guide", "maker"),
        length_days=30,
    ),
)


# ---------------------------------------------------------------------------
# Date resolution
# ---------------------------------------------------------------------------

_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


def resolve_date(occasion: Occasion, year: int) -> datetime.date | None:
    """Resolve an occasion to a concrete date in the given year.

    Returns None if the format can't be parsed.
    """
    md = occasion.month_day
    if md.startswith("season:"):
        return None
    if md.startswith("nth-weekday:"):
        # nth-weekday:N:weekday:month
        try:
            _, n_s, weekday_s, month_s = md.split(":")
            n = int(n_s); month = int(month_s)
            wd = _WEEKDAYS[weekday_s.lower()]
        except (KeyError, ValueError):
            return None
        # Walk forward from the 1st of the month, count nth occurrence.
        d = datetime.date(year, month, 1)
        offset = (wd - d.weekday()) % 7
        d = d + datetime.timedelta(days=offset + 7 * (n - 1))
        if d.month != month:
            return None  # nth doesn't exist that month
        return d
    if md.startswith("last-weekday:"):
        try:
            _, weekday_s, month_s = md.split(":")
            month = int(month_s)
            wd = _WEEKDAYS[weekday_s.lower()]
        except (KeyError, ValueError):
            return None
        # Walk back from the last day of the month.
        if month == 12:
            next_first = datetime.date(year + 1, 1, 1)
        else:
            next_first = datetime.date(year, month + 1, 1)
        last_day = next_first - datetime.timedelta(days=1)
        offset = (last_day.weekday() - wd) % 7
        return last_day - datetime.timedelta(days=offset)
    # Fixed MM-DD
    try:
        m, d = md.split("-")
        return datetime.date(year, int(m), int(d))
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Window classification + signal builder
# ---------------------------------------------------------------------------

NOW_WINDOW_DAYS = 3       # +/- 3 days = NOW
IMMINENT_END_DAYS = 14    # next 4-14 days = IMMINENT
UPCOMING_END_DAYS = 60    # next 15-60 days = UPCOMING


@dataclass
class ActiveOccasion:
    occasion: Occasion
    when: datetime.date
    delta_days: int          # negative = passed, 0 = today
    window: str              # "now" | "imminent" | "upcoming"

    def as_prompt_line(self) -> str:
        when_word = "TODAY" if self.delta_days == 0 else (
            f"in {self.delta_days}d" if self.delta_days > 0
            else f"{-self.delta_days}d ago"
        )
        kw = ", ".join(self.occasion.recipe_keywords[:8])
        return (
            f"  - [{self.window.upper()}] {self.occasion.label} ({when_word}, "
            f"{self.when.isoformat()}): biased keywords → {kw or '(none)'}"
        )

    def to_dict(self) -> dict:
        d = asdict(self.occasion)
        d.update(when=self.when.isoformat(), delta_days=self.delta_days,
                 window=self.window)
        return d


def active_signal(today: datetime.date | None = None,
                  *,
                  occasions: Sequence[Occasion] = DEFAULT_OCCASIONS,
                  audience: str = "general") -> list[ActiveOccasion]:
    """Return all occasions matching today's NOW / IMMINENT / UPCOMING
    windows, newest-first.

    `audience` filters occasions whose `audience` is not "general" and
    not the requested audience. Pass `"food"` for aisleprompt, `"tech"`
    for specpicks.
    """
    today = today or datetime.date.today()
    out: list[ActiveOccasion] = []
    for occ in occasions:
        if occ.audience != "general" and occ.audience != audience:
            continue
        # Try this year + next year so occasions in January don't get
        # missed in December.
        for year in (today.year, today.year + 1):
            when = resolve_date(occ, year)
            if when is None:
                continue
            # `length_days` lets a multi-day window register as NOW from
            # the start through the end (e.g. Memorial Day Weekend covers
            # Saturday–Monday).
            start = when
            end = when + datetime.timedelta(days=max(0, occ.length_days - 1))
            if start <= today <= end:
                out.append(ActiveOccasion(
                    occasion=occ, when=start, delta_days=0, window="now"))
                break
            delta_to_start = (start - today).days
            if -NOW_WINDOW_DAYS <= delta_to_start <= NOW_WINDOW_DAYS:
                out.append(ActiveOccasion(
                    occasion=occ, when=start, delta_days=delta_to_start,
                    window="now"))
                break
            if NOW_WINDOW_DAYS < delta_to_start <= IMMINENT_END_DAYS:
                out.append(ActiveOccasion(
                    occasion=occ, when=start, delta_days=delta_to_start,
                    window="imminent"))
                break
            if IMMINENT_END_DAYS < delta_to_start <= UPCOMING_END_DAYS:
                out.append(ActiveOccasion(
                    occasion=occ, when=start, delta_days=delta_to_start,
                    window="upcoming"))
                break
    # Sort: NOW first, then IMMINENT, then UPCOMING; within each
    # window, soonest first.
    order = {"now": 0, "imminent": 1, "upcoming": 2}
    out.sort(key=lambda a: (order[a.window], abs(a.delta_days)))
    return out


def build_prompt_block(active: Sequence[ActiveOccasion],
                       *,
                       today: datetime.date | None = None) -> str:
    """Render the active occasions as a block we paste into the LLM
    user-message. Empty when nothing is in window.
    """
    if not active:
        return ""
    today = today or datetime.date.today()
    lines: list[str] = [
        f"SEASONAL + HOLIDAY SIGNAL — today is {today.isoformat()} "
        f"({today.strftime('%A')})."
    ]
    now_list = [a for a in active if a.window == "now"]
    if now_list:
        lines.append("")
        lines.append(
            "🔥 SHIP NOW (this weekend / today). At least ONE proposal "
            "MUST target the highest-priority NOW occasion below — bias the "
            "title, primary_keyword, target_query, and recipe links to it. "
            "If a relevant article already exists in RECENTLY PUBLISHED, "
            "skip the occasion (don't re-propose). Otherwise propose."
        )
        for a in now_list:
            lines.append(a.as_prompt_line())
            if a.occasion.link_categories:
                lines.append(
                    f"      link to /k/ categories: "
                    f"{', '.join(a.occasion.link_categories[:6])}"
                )
            if a.occasion.bucket_hints:
                lines.append(
                    f"      best buckets: {', '.join(a.occasion.bucket_hints)}"
                )
    imminent = [a for a in active if a.window == "imminent"]
    if imminent:
        lines.append("")
        lines.append(
            "📅 IMMINENT (4–14 days). Strong candidates for this run "
            "if you have proposal slots after the NOW pick:"
        )
        for a in imminent:
            lines.append(a.as_prompt_line())
    upcoming = [a for a in active if a.window == "upcoming"]
    if upcoming:
        lines.append("")
        lines.append(
            "🗓 UPCOMING (15–60 days). The classic publish-4-weeks-before "
            "window. Use only if NOW + IMMINENT are already covered:"
        )
        for a in upcoming:
            lines.append(a.as_prompt_line())
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Featured-articles rotation
# ---------------------------------------------------------------------------

def cycle_featured(articles_by_holiday: dict[str, list[str]],
                   *,
                   active: Sequence[ActiveOccasion] | None = None,
                   today: datetime.date | None = None) -> list[str]:
    """Return a list of slugs to feature on the homepage rail right now,
    ordered by relevance.

    The caller passes a map of `holiday_id → list of candidate slugs
    tagged with that holiday`. The output is:

      1. All NOW slugs (rotated by date-hour so multiple articles cycle).
      2. All IMMINENT slugs.
      3. All UPCOMING slugs (only if no NOW or IMMINENT slugs exist).

    Within each holiday, the rotation cursor is `floor(epoch_hour /
    rotate_every_hours)` mod len. Rotation cadence is fixed at 6h so
    a feature swaps four times a day — frequent enough to keep the
    homepage feeling alive, not so frequent that crawlers see flapping
    URLs in Last-Modified headers.
    """
    today = today or datetime.date.today()
    active = list(active or active_signal(today))
    rotate_every_hours = 6
    hour_cursor = (
        datetime.datetime.utcnow().toordinal() * 24
        + datetime.datetime.utcnow().hour
    ) // rotate_every_hours
    out: list[str] = []
    seen: set[str] = set()
    for window in ("now", "imminent", "upcoming"):
        for ao in active:
            if ao.window != window:
                continue
            slugs = articles_by_holiday.get(ao.occasion.id) or []
            if not slugs:
                continue
            # Rotation within this holiday's pool.
            rotated = slugs[hour_cursor % len(slugs):] + slugs[:hour_cursor % len(slugs)]
            for s in rotated:
                if s not in seen:
                    out.append(s)
                    seen.add(s)
        if out:
            # Don't move to UPCOMING if NOW or IMMINENT produced any.
            if window in ("now", "imminent"):
                continue
            break
    return out
