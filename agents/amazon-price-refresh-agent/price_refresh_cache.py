"""The site's Amazon display cache: what the price-refresh engine writes, and the purge.

WHY THIS EXISTS
---------------
A cache-model site keeps no Amazon content in its catalog. ``products`` holds
the ASIN and our own data; everything Amazon returned (title, image, price,
availability, ...) lives in one cache table keyed by ASIN, and the Amazon
license lets non-image content be cached for at most 24 hours (IP License
2(h)). Only the ASIN and our own derived values (the outcome, the lifecycle)
may be kept past that. This module is the one writer of that table:

* ``row_values`` turns one getItems answer into a value for EVERY API column:
  the answer's value, or NULL when the answer has none. A row's API columns
  therefore always come from the single answer stamped in ``fetched_at``,
  which is what makes the age of the row the age of every value in it.
  (Keeping an older title while stamping a newer ``fetched_at`` would extend
  that title's life past 24h without anyone noticing.)
* ``CacheWriter.upsert`` writes one batch in one statement and one commit,
  re-running it after a deadlock or serialization failure (the batch is
  idempotent: absolute values and a NOW() stamp). A batch that fails on a
  VALUE (an encoding, numeric or constraint error that only some answers
  carry) is re-written one row at a time: rows that still fail are skipped
  and listed in ``CacheWriter.poison`` (they keep their previous values), so
  one bad answer can never stop the sweep or the purge, run after run. A
  batch whose every row fails raises: that is the table, not an answer.
* ``CacheWriter.purge_report`` NULLs every API column of every row whose
  answer is older than the purge horizon (``CacheSpec.max_age_hours``) and
  says which rows it NULLed, and how old each was. The engine calls it only
  after a successful run; this module does not decide that.

THE PURGE HORIZON
-----------------
The purge runs once per run, at its end, so a row a hair younger than the
horizon at one purge is removed only at the next: one cron period later, and
possibly a whole run later in that run than the previous purge sat in its own
(a budget-spent run purges seconds after its tick, a full sweep minutes
later). The worst stored age is therefore horizon + period + run, and the
horizon that keeps every stored value under 24h is 24h minus the cron period
minus the longest run (``purge_horizon_hours``): 19.5h on a 4-hourly cron
whose runs finish within 30 minutes. ``build_cache_spec`` derives it
(``max_age_hours: auto``) or rejects a larger value.

Answers that are not answers (an ASIN Amazon silently left out of a response,
a failed batch, an answer whose values cannot be stored, an ASIN the run
never reached) are never written: the row keeps its previous values until a
later answer replaces them or the purge removes them.

THE SPARE
---------
A run that asked Amazon about a row and could not refresh it (its batch
failed, the answer left it out, or its values could not be stored) is a
failed run for that row. Like a failed run, it must not blank the row while
a visitor can still see it. So the purge takes the run's unrefreshed keys and
a spare window (the engine passes the quotable window, 24h minus the HTML
cache age) and leaves alone every such row whose answer is still inside that
window. The next run re-asks it: a fresh answer replaces it, and otherwise
that run's purge removes it, because by then it is normally past the window;
a row that run again cannot refresh while it is still inside the window is
spared once more. Rows the run never asked about (the budget ran out) are
not spared: the purge removes them once past the horizon.

The spare's price is the kind amendment 10 accepts for a failed run: a
spared row can stay stored past 24h, though never displayed past the
quotable window, until a later purge removes it. On a 4-hourly cron with
30-minute runs (horizon 19.5h; window ~23.8h at the kit's defaults) it is
stored up to:
* ~24.5h when it was answered on its cadence (within a run of a tick) and
  spared once;
* ~28.0h when it was answered off its cadence (a manual run, a catch-up run
  after downtime, a run longer than max_run_minutes). For example, a row
  answered 3.4h before a tick, whose batch then fails at ~19.4h and ~23.4h,
  is spared at ~23.4h and stored until ~27.9h; the failure at ~23.4h alone
  does it when the ~19.4h run never asked it and purged within 6 minutes;
* the spare window plus one period and one run at worst, ~28.3h
  (``worst_case_spared_age_hours``), reached by a row on its cadence whose
  batch fails in two runs in a row (spared twice). That is slightly (~19
  min) above amendment 10's one failed run: horizon + 2 periods + run, 28.0h.
Budget spills alone never pass 24h. Like the horizon's 24h, these bounds
assume every later tick's run purges within max_run_minutes. After a spare,
a run that skips its purge (failed or partial), or a tick skipped because a
manual run still holds the lock, adds one period (~32.3h).

COLUMN CONTRACT
---------------
``API_FIELDS`` are the logical names. ``CacheSpec.columns`` maps each to the
table's column (a site whose table lacks one maps it to null: it is then
neither requested from Amazon, written nor purged). ``OPT_IN_FIELDS``
(browse node ids, product group) are off unless a site maps them: the
license review keeps them out of the cache (derive a category once into your
own table instead), and getItems' acceptance of the browse-node resource is
unverified. Our own columns: ``key_col`` (the ASIN, unique),
``fetched_at_col`` (when the answer arrived), ``outcome_col`` (priced |
unavailable | no_offer | not_found | non_usd) and ``consumer_col`` (the
Creators budget line that paid for it). Every API column must be NULLable;
``browse_node_ids`` is an array (cast set in ``casts``, default ``text[]``).

Nothing here names a site, and nothing here calls Amazon.
"""
from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from framework.core.amazon_creators import parse_item
from framework.core.amazon_price_refresh import PRICE_RESOURCES, PriceOutcome

log = logging.getLogger("amazon_price_refresh_agent.cache")

#: Every Amazon-sourced value the cache can hold, in write order.
API_FIELDS: tuple[str, ...] = (
    "title", "image_url", "image_w", "image_h", "detail_page_url",
    "price", "list_price", "currency", "availability", "availability_type",
    "condition", "is_buybox_winner", "map_hidden", "browse_node_ids",
    "product_group",
)

#: Fields a site must map to a column to get: not in the cache by default.
OPT_IN_FIELDS: tuple[str, ...] = ("browse_node_ids", "product_group")

#: The Creators resource a field needs on top of PRICE_RESOURCES (which the
#: classifier always needs). detail_page_url needs none: getItems returns
#: detailPageURL for every item. Names are the ones amazon_creators.py lists
#: as accepted by the live API (the last two only for searchItems so far).
FIELD_RESOURCES: dict[str, str] = {
    "title": "itemInfo.title",
    "image_url": "images.primary.large",
    "image_w": "images.primary.large",
    "image_h": "images.primary.large",
    "browse_node_ids": "browseNodeInfo.browseNodes",
    "product_group": "itemInfo.classifications",
}

#: Outcomes Amazon answered: each replaces the row's API columns.
ANSWERED: frozenset[str] = frozenset({"priced", "unavailable", "no_offer", "non_usd", "not_found"})
#: Answered outcomes that carry the item itself (title, image, ...). not_found
#: carries nothing, so writing it NULLs the row; the engine holds those back
#: until the sweep has finished (see the agent's not_found guard).
CARRIES_ITEM: frozenset[str] = ANSWERED - {"not_found"}

#: field -> column; None = off (OPT_IN_FIELDS).
DEFAULT_COLUMNS: dict[str, Optional[str]] = {
    f: (None if f in OPT_IN_FIELDS else f) for f in API_FIELDS}
DEFAULT_CASTS: dict[str, str] = {"browse_node_ids": "text[]"}

IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
TABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")
#: A SQL type for a cast: words, an optional (n[,m]) and an optional [].
CAST_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_ ]{0,40}(\(\d{1,4}(,\s?\d{1,4})?\))?(\[\])?$")
#: IP License 2(h): non-image content may be cached for at most 24 hours.
MAX_LICENSED_AGE_HOURS = 24.0
#: The longest a run may take from its tick to its purge (see THE PURGE HORIZON).
DEFAULT_MAX_RUN_MINUTES = 30.0
MAX_URL_CHARS = 2048
_SHORT_TEXT = 64          # availability_type / condition / currency
_GROUP_TEXT = 200         # product_group
#: What Postgres cannot store in text: NUL, and lone UTF-16 surrogates (which
#: JSON's \\ud83d escape produces and which cannot even be encoded as UTF-8).
_UNSTORABLE_RE = re.compile("[\x00\ud800-\udfff]")
#: SQLSTATE classes of an error caused by the values being written: 22 data
#: exception (encoding, numeric overflow, value too long, invalid input), 23
#: integrity constraint (a CHECK or NOT NULL the site's table puts on a column).
VALUE_ERROR_CLASSES: tuple[str, ...] = ("22", "23")


class CacheConfigError(ValueError):
    """The cache block of a site config is unusable; ``problems`` says why."""

    def __init__(self, problems: Sequence[str]):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


@dataclass(frozen=True)
class CacheSpec:
    """Where and how one site's cache table is written (validated)."""

    table: str = "amazon_item_cache"
    key_col: str = "asin"
    fetched_at_col: str = "fetched_at"
    outcome_col: Optional[str] = "outcome"
    consumer_col: Optional[str] = "consumer"
    #: (field, column) for every ENABLED API field, in API_FIELDS order.
    columns: tuple[tuple[str, str], ...] = tuple((f, c) for f, c in DEFAULT_COLUMNS.items() if c)
    casts: tuple[tuple[str, str], ...] = tuple(DEFAULT_CASTS.items())
    #: Columns another writer fills from Amazon data: NULLed by the purge too.
    extra_purge_columns: tuple[str, ...] = ()
    max_text_chars: int = 1000
    #: The purge horizon. build_cache_spec() derives it from the cron period;
    #: this bare default is only right for a purge that runs continuously.
    max_age_hours: float = MAX_LICENSED_AGE_HOURS

    def column(self, field: str) -> Optional[str]:
        return dict(self.columns).get(field)

    def cast_for(self, field: str) -> Optional[str]:
        return dict(self.casts).get(field)

    @property
    def fields(self) -> tuple[str, ...]:
        return tuple(f for f, _ in self.columns)

    @property
    def api_columns(self) -> tuple[str, ...]:
        """Every column the purge NULLs: the enabled API columns plus extras."""
        out = [c for _, c in self.columns]
        out += [c for c in self.extra_purge_columns if c not in out]
        return tuple(out)

    @property
    def own_columns(self) -> tuple[str, ...]:
        """Our columns, which the purge never touches."""
        return tuple(c for c in (self.key_col, self.outcome_col, self.fetched_at_col,
                                 self.consumer_col) if c)


def purge_horizon_hours(period_hours: float,
                        max_run_minutes: float = DEFAULT_MAX_RUN_MINUTES) -> float:
    """The largest purge age that keeps every stored Amazon value under 24h
    when the purge runs once per run of a ``period_hours`` cron whose runs
    reach their purge within ``max_run_minutes`` (see THE PURGE HORIZON).

    Raises ValueError when the period and the run leave nothing."""
    p = float(period_hours)
    d = float(max_run_minutes) / 60.0
    if p <= 0 or d < 0:
        raise ValueError("period_hours must be > 0 and max_run_minutes >= 0")
    horizon = MAX_LICENSED_AGE_HOURS - p - d
    if horizon <= 0:
        raise ValueError(f"a {p:g}h cron with runs of up to {max_run_minutes:g} min leaves "
                         "no purge horizon under 24h")
    return round(horizon, 4)


def worst_case_stored_age_hours(horizon_hours: float, period_hours: float,
                                max_run_minutes: float = DEFAULT_MAX_RUN_MINUTES) -> float:
    """How old a value can get in storage when every run purges at
    ``horizon_hours``: just under the horizon at one purge, purged at the next
    (one period later, up to one run later within its run). A row the spare
    keeps can get older: ``worst_case_spared_age_hours``."""
    return round(float(horizon_hours) + float(period_hours) + float(max_run_minutes) / 60.0, 4)


def worst_case_spared_age_hours(spare_window_hours: float, period_hours: float,
                                max_run_minutes: float = DEFAULT_MAX_RUN_MINUTES, *,
                                horizon_hours: Optional[float] = None) -> float:
    """How old a value can get in storage with the spare (see THE SPARE): a
    row spared at up to ``spare_window_hours`` old is purged by the next run,
    one period later and up to one run later within its run. The spare only
    keeps rows past the horizon, so a window at or under it spares nothing
    and the bound is the horizon's own (``worst_case_stored_age_hours``).
    ``horizon_hours`` defaults to the 'auto' horizon of the period and run,
    the largest a config may set. Like the horizon's bound, it assumes every
    later tick's run purges within ``max_run_minutes``; a run after a spare
    that skips its purge adds one period."""
    horizon = (purge_horizon_hours(period_hours, max_run_minutes) if horizon_hours is None
               else float(horizon_hours))
    return worst_case_stored_age_hours(max(float(spare_window_hours), horizon),
                                       period_hours, max_run_minutes)


def build_cache_spec(cfg: Mapping[str, Any], *, period_hours: Optional[float] = None,
                     max_run_minutes: Optional[float] = None) -> CacheSpec:
    """Validate a merged ``cache`` config block into a CacheSpec.

    ``period_hours`` (the cron period of the runs that purge) and
    ``max_run_minutes`` set the purge horizon: ``purge.max_age_hours`` 'auto'
    (or unset) becomes ``purge_horizon_hours(period, run)`` and an explicit
    value above it is a problem, because the 24h license limit applies to
    the oldest value storage can hold, not to the threshold. Pass the period
    whenever the purge runs on a schedule (the engine always does); without
    it 'auto' means 24h, which is only right for a continuous purge.

    Raises CacheConfigError listing every problem (never echoing a value that
    is not an identifier the site chose)."""
    problems: list[str] = []
    mode = cfg.get("mode", "cache")
    if mode != "cache":
        problems.append(f"cache.mode {mode!r} is not supported (only 'cache')")

    def ident(path: str, value: Any, *, table: bool = False, optional: bool = False) -> Optional[str]:
        if value in (None, "") and optional:
            return None
        rx = TABLE_RE if table else IDENT_RE
        if not isinstance(value, str) or not rx.match(value):
            problems.append(f"{path} must be a SQL identifier{' (schema.table allowed)' if table else ''}")
            return None
        return value

    table = ident("cache.table", cfg.get("table", "amazon_item_cache"), table=True)
    key_col = ident("cache.key_col", cfg.get("key_col", "asin"))
    fetched = ident("cache.fetched_at_col", cfg.get("fetched_at_col", "fetched_at"))
    outcome = ident("cache.outcome_col", cfg.get("outcome_col", "outcome"), optional=True)
    consumer = ident("cache.consumer_col", cfg.get("consumer_col", "consumer"), optional=True)

    raw_cols = dict(DEFAULT_COLUMNS)
    over = cfg.get("columns") or {}
    if not isinstance(over, Mapping):
        problems.append("cache.columns must be a mapping of field -> column (null disables)")
        over = {}
    for f, c in over.items():
        if f not in DEFAULT_COLUMNS:
            problems.append(f"cache.columns.{f}: unknown field (known: {', '.join(API_FIELDS)})")
            continue
        raw_cols[f] = c
    columns: list[tuple[str, str]] = []
    for f in API_FIELDS:
        c = ident(f"cache.columns.{f}", raw_cols.get(f), optional=True)
        if c:
            columns.append((f, c))

    raw_casts = dict(DEFAULT_CASTS)
    cast_over = cfg.get("casts") or {}
    if not isinstance(cast_over, Mapping):
        problems.append("cache.casts must be a mapping of field -> SQL type")
        cast_over = {}
    for f, t in cast_over.items():
        if f not in DEFAULT_COLUMNS:
            problems.append(f"cache.casts.{f}: unknown field")
            continue
        raw_casts[f] = t
    casts: list[tuple[str, str]] = []
    for f, t in raw_casts.items():
        if t in (None, ""):
            continue
        if not isinstance(t, str) or not CAST_RE.match(t.strip()):
            problems.append(f"cache.casts.{f} must be a plain SQL type such as text[] or bigint[]")
            continue
        casts.append((f, t.strip()))

    extras: list[str] = []
    for i, c in enumerate(cfg.get("extra_purge_columns") or []):
        c = ident(f"cache.extra_purge_columns[{i}]", c)
        if c:
            extras.append(c)

    own = [c for c in (key_col, fetched, outcome, consumer) if c]
    phys = [c for _, c in columns]
    for c in sorted({c for c in phys + own if (phys + own).count(c) > 1}):
        problems.append(f"cache: column {c!r} is mapped more than once")
    for c in extras:
        if c in own:
            problems.append(f"cache.extra_purge_columns: {c!r} is one of our own columns and must never be purged")

    try:
        max_chars = int(cfg.get("max_text_chars", 1000))
        if max_chars < 16:
            raise ValueError
    except (TypeError, ValueError):
        problems.append("cache.max_text_chars must be an integer >= 16")
        max_chars = 1000
    max_age = _purge_age(cfg.get("purge"), period_hours, max_run_minutes, problems)
    if not any(f == "price" for f, _ in columns):
        problems.append("cache.columns.price cannot be disabled: it is what this engine refreshes")

    if problems:
        raise CacheConfigError(problems)
    return CacheSpec(table=table, key_col=key_col, fetched_at_col=fetched, outcome_col=outcome,
                     consumer_col=consumer, columns=tuple(columns), casts=tuple(casts),
                     extra_purge_columns=tuple(extras), max_text_chars=max_chars,
                     max_age_hours=max_age)


def _num(value: Any) -> str:
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return str(value)


def _purge_age(purge: Any, period_hours: Optional[float], max_run_minutes: Optional[float],
               problems: list[str]) -> float:
    """The purge horizon for build_cache_spec (problems appended, never raised)."""
    run_min = DEFAULT_MAX_RUN_MINUTES if max_run_minutes is None else max_run_minutes
    if purge is not None and not isinstance(purge, Mapping):
        problems.append("cache.purge must be a mapping")
        purge = {}
    raw = (purge or {}).get("max_age_hours", "auto")
    bound: Optional[float] = MAX_LICENSED_AGE_HOURS
    if period_hours is not None:
        try:
            bound = purge_horizon_hours(period_hours, run_min)
        except (TypeError, ValueError):
            problems.append(f"schedule: a {_num(period_hours)}h cron with runs of up to "
                            f"{_num(run_min)} min leaves no purge horizon under 24h (Amazon "
                            "data may be cached for at most 24 hours): shorten the cron period")
            bound = None
    if raw is None or raw == "auto":
        return bound if bound is not None else MAX_LICENSED_AGE_HOURS
    try:
        if isinstance(raw, bool):
            raise TypeError
        max_age = float(raw)
        if not 0 < max_age <= MAX_LICENSED_AGE_HOURS:
            raise ValueError
    except (TypeError, ValueError):
        problems.append(f"cache.purge.max_age_hours must be 'auto' or > 0 and <= "
                        f"{MAX_LICENSED_AGE_HOURS:g} (Amazon data may be cached for at most "
                        "24 hours)")
        return bound if bound is not None else MAX_LICENSED_AGE_HOURS
    if bound is not None and period_hours is not None and max_age > bound:
        worst = worst_case_stored_age_hours(max_age, period_hours, run_min)
        problems.append(f"cache.purge.max_age_hours {max_age:g} on a {float(period_hours):g}h cron "
                        f"keeps Amazon data up to ~{worst:g}h (the purge runs once per run); use "
                        f"'auto' or at most {bound:g}h (24h minus the cron period and a "
                        f"{float(run_min):g}-minute run)")
    return max_age


def resources_for(spec: CacheSpec) -> tuple[str, ...]:
    """PRICE_RESOURCES plus what the enabled fields need, in a stable order."""
    out = list(PRICE_RESOURCES)
    for f in spec.fields:
        r = FIELD_RESOURCES.get(f)
        if r and r not in out:
            out.append(r)
    return tuple(out)


# ---------------------------------------------------------------------------
# One answer -> one row
# ---------------------------------------------------------------------------

def _text(value: Any, limit: int) -> Optional[str]:
    """A clean, bounded string or None. NUL bytes and lone UTF-16 surrogates
    are dropped: Postgres cannot store either, and one would fail its whole
    batch (and, unwritten, stay at the head of the queue every run)."""
    if value is None:
        return None
    s = _UNSTORABLE_RE.sub("", str(value)).strip()
    return s[:limit] if s else None


def _chosen_listing(listings: Sequence[Any]) -> Mapping[str, Any]:
    """The listing parse_item() takes the price from: New buy-box winner, any
    New, buy-box winner, first. The same order as amazon_creators' private
    picker, kept here so this module imports only public names; a test pins
    the two together."""
    rows = [l for l in listings if isinstance(l, Mapping)]
    if not rows:
        return {}

    def is_new(l: Mapping[str, Any]) -> bool:
        cond = l.get("condition")
        value = cond.get("value") if isinstance(cond, Mapping) else None
        return str(value or "").lower() == "new"

    return (next((l for l in rows if l.get("isBuyBoxWinner") and is_new(l)), None)
            or next((l for l in rows if is_new(l)), None)
            or next((l for l in rows if l.get("isBuyBoxWinner")), None)
            or rows[0])


def _url(value: Any) -> Optional[str]:
    s = _text(value, MAX_URL_CHARS + 1)
    if not s or len(s) > MAX_URL_CHARS or not s.lower().startswith(("https://", "http://")):
        return None
    return s


def _dim(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return None
    return n if 0 < n < 100_000 else None


def _node_ids(nodes: Any) -> Optional[list[str]]:
    out: list[str] = []
    for n in nodes or []:
        nid = n.get("id") if isinstance(n, Mapping) else None
        s = _text(nid, 32)
        if s and s not in out:
            out.append(s)
    return out or None


def row_values(outcome: PriceOutcome, raw: Optional[Mapping[str, Any]], *,
               max_text_chars: int = 1000) -> dict[str, Any]:
    """The value of every API field after ``outcome``'s answer (None = NULL).

    ``raw`` is the getItems item the outcome was classified from. Only
    answered outcomes (``ANSWERED``) can be written; ``missing`` and ``error``
    raise, because writing them would blank a row Amazon never answered for.
    So does an outcome that carries an item (``CARRIES_ITEM``) arriving
    without it: writing it would stamp a row fresh with every value NULL.

    The availability type and the buy-box flag describe the listing the
    answer was read from. When that listing is not shown (``no_offer`` other
    than MAP-hidden, ``non_usd``) they are NULL like the availability text
    the classifier clears, so nothing next to a NULL price reads as buyable.
    """
    if outcome.status not in ANSWERED:
        raise ValueError(f"{outcome.asin}: outcome {outcome.status!r} is not an answer; "
                         "the row must keep its previous values")
    vals: dict[str, Any] = dict.fromkeys(API_FIELDS)
    if outcome.status == "not_found":
        return vals
    if not raw:
        raise ValueError(f"{outcome.asin}: outcome {outcome.status!r} arrived without its item; "
                         "refusing to stamp the row fresh with every value NULL")
    p = parse_item(dict(raw))
    listings = (((raw.get("offersV2") or {}).get("listings")) or [])
    best = _chosen_listing(listings)
    large = (((raw.get("images") or {}).get("primary") or {}).get("large")) or {}
    image_url = _url(large.get("url") if isinstance(large, Mapping) else None)
    listing_shown = outcome.status in ("priced", "unavailable") or bool(outcome.map_hidden)
    vals.update(
        title=_text(p.get("title"), max_text_chars),
        image_url=image_url,
        image_w=_dim(large.get("width")) if image_url else None,
        image_h=_dim(large.get("height")) if image_url else None,
        detail_page_url=_url(raw.get("detailPageURL")),
        availability=_text(outcome.availability, max_text_chars),
        availability_type=(_text(outcome.availability_type, _SHORT_TEXT)
                           if listing_shown else None),
        condition=_text(outcome.condition, _SHORT_TEXT),
        is_buybox_winner=(bool(best.get("isBuyBoxWinner"))
                          if listing_shown and "isBuyBoxWinner" in best else None),
        map_hidden=bool(outcome.map_hidden),
        browse_node_ids=_node_ids(p.get("browse_nodes")),
        product_group=_text(p.get("product_group"), _GROUP_TEXT),
    )
    if outcome.status == "priced":
        vals.update(price=outcome.price, list_price=outcome.original_price,
                    currency=_text(outcome.currency, _SHORT_TEXT))
    elif outcome.status == "non_usd":
        # The foreign currency is the reason there is no price; keep it.
        vals["currency"] = _text(outcome.currency, _SHORT_TEXT)
    return vals


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

def build_upsert_sql(spec: CacheSpec, n_rows: int) -> str:
    """INSERT .. ON CONFLICT (key) DO UPDATE for ``n_rows`` rows; named params
    ``r<i>_key``, ``r<i>_<field>``, ``r<i>_outcome`` and ``consumer``."""
    if n_rows < 1:
        raise ValueError("build_upsert_sql needs at least one row")
    cols = [spec.key_col] + [c for _, c in spec.columns]
    if spec.outcome_col:
        cols.append(spec.outcome_col)
    cols.append(spec.fetched_at_col)
    if spec.consumer_col:
        cols.append(spec.consumer_col)
    rows = []
    for i in range(n_rows):
        vals = [f"%(r{i}_key)s"]
        for f, _ in spec.columns:
            cast = spec.cast_for(f)
            vals.append(f"%(r{i}_{f})s" + (f"::{cast}" if cast else ""))
        if spec.outcome_col:
            vals.append(f"%(r{i}_outcome)s")
        vals.append("NOW()")
        if spec.consumer_col:
            vals.append("%(consumer)s")
        rows.append("(" + ", ".join(vals) + ")")
    sets = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols[1:])
    return (f"INSERT INTO {spec.table} ({', '.join(cols)})\n"
            f"VALUES " + ",\n       ".join(rows) + "\n"
            f"ON CONFLICT ({spec.key_col}) DO UPDATE SET {sets}")


def upsert_params(spec: CacheSpec, rows: Sequence[tuple[str, str, Mapping[str, Any]]],
                  consumer: str) -> dict[str, Any]:
    params: dict[str, Any] = {"consumer": consumer}
    for i, (asin, status, vals) in enumerate(rows):
        params[f"r{i}_key"] = asin
        for f, _ in spec.columns:
            params[f"r{i}_{f}"] = vals.get(f)
        if spec.outcome_col:
            params[f"r{i}_outcome"] = status
    return params


def _stale_where(spec: CacheSpec, alias: str = "t") -> str:
    fa = f"{alias}.{spec.fetched_at_col}"
    any_api = " OR ".join(f"{alias}.{c} IS NOT NULL" for c in spec.api_columns)
    return (f"({fa} IS NULL OR {fa} < NOW() - make_interval(secs => %(prf_max_age_s)s))\n"
            f"   AND ({any_api})")


def _spared_where(spec: CacheSpec, alias: str = "t") -> str:
    """The rows a spare keeps: one of the run's unrefreshed keys whose answer
    is still inside the spare window. A row without ``fetched_at`` is never
    spared (it has no age, so it is never inside a window)."""
    fa = f"{alias}.{spec.fetched_at_col}"
    return (f"{alias}.{spec.key_col} = ANY(%(prf_spare)s::text[])\n"
            f"        AND {fa} IS NOT NULL\n"
            f"        AND {fa} >= NOW() - make_interval(secs => %(prf_spare_s)s)")


def _spare_params(spare: Iterable[str], spare_max_age_hours: Optional[float]) -> dict[str, Any]:
    """{prf_spare, prf_spare_s} when there is anything to spare, else {}."""
    keys = list(dict.fromkeys(str(k) for k in (spare or ()) if k))
    try:
        window = float(spare_max_age_hours) if spare_max_age_hours is not None else 0.0
    except (TypeError, ValueError):
        window = 0.0
    if not keys or window <= 0:
        return {}
    return {"prf_spare": keys, "prf_spare_s": window * 3600.0}


def build_purge_sql(spec: CacheSpec, *, spare: Iterable[str] = (),
                    spare_max_age_hours: Optional[float] = None) -> tuple[str, dict[str, Any]]:
    """NULL every API column of every row whose answer is older than
    ``max_age_hours`` (or has no ``fetched_at`` at all), except the rows
    ``spare`` names whose answer is at most ``spare_max_age_hours`` old (see
    THE SPARE). Our own columns (the key, outcome, fetched_at, consumer) are
    kept. RETURNING gives each purged row's key and its age in hours at the
    purge (NULL without a ``fetched_at``)."""
    sets = ", ".join(f"{c} = NULL" for c in spec.api_columns)
    params: dict[str, Any] = {"prf_max_age_s": float(spec.max_age_hours) * 3600.0}
    where = _stale_where(spec)
    spare_params = _spare_params(spare, spare_max_age_hours)
    if spare_params:
        where += f"\n   AND NOT ({_spared_where(spec)})"
        params.update(spare_params)
    fa = f"t.{spec.fetched_at_col}"
    sql = (f"UPDATE {spec.table} AS t SET {sets}\n"
           f" WHERE {where}\n"
           f"RETURNING t.{spec.key_col}, EXTRACT(EPOCH FROM (NOW() - {fa})) / 3600.0 AS age_h")
    return sql, params


def build_spared_sql(spec: CacheSpec, spare: Iterable[str],
                     spare_max_age_hours: Optional[float]) -> Optional[tuple[str, dict[str, Any]]]:
    """The keys of the rows past the horizon that a purge with this spare
    leaves (column ``spared_key``); None when nothing would be spared."""
    spare_params = _spare_params(spare, spare_max_age_hours)
    if not spare_params:
        return None
    sql = (f"SELECT t.{spec.key_col} AS spared_key\n"
           f"  FROM {spec.table} t\n"
           f" WHERE {_stale_where(spec)}\n"
           f"   AND ({_spared_where(spec)})")
    return sql, {"prf_max_age_s": float(spec.max_age_hours) * 3600.0, **spare_params}


#: Column names of build_stale_counts_sql's one row, in order.
STALE_COUNT_NAMES: tuple[str, ...] = ("stale_api_rows", "stale_price_rows", "api_rows_past_24h")


def build_stale_counts_sql(spec: CacheSpec, *, spare: Iterable[str] = (),
                           spare_max_age_hours: Optional[float] = None
                           ) -> tuple[str, dict[str, Any]]:
    """Rows still holding Amazon data past the purge horizon: all of them,
    those holding a price, and those already past the 24h license limit.

    The first two are what the purge should have NULLed: 0 after every
    successful purge, and above 0 only while purges are skipped (each such
    row passes 24h within one more cron period). Pass the spare the run's
    purge used, and the rows it deliberately kept are left out of them too.
    The third is an actual breach of the 24h limit (spared rows included)."""
    price = spec.column("price")
    price_filter = f"t.{price} IS NOT NULL" if price else "FALSE"
    fa = f"t.{spec.fetched_at_col}"
    params: dict[str, Any] = {"prf_max_age_s": float(spec.max_age_hours) * 3600.0,
                              "prf_licence_s": MAX_LICENSED_AGE_HOURS * 3600.0}
    spare_params = _spare_params(spare, spare_max_age_hours)
    if spare_params:
        kept = f"NOT ({_spared_where(spec)})"
        head = (f"SELECT count(*) FILTER (WHERE {kept}) AS stale_api_rows,\n"
                f"       count(*) FILTER (WHERE ({kept}) AND {price_filter}) AS stale_price_rows,\n")
        params.update(spare_params)
    else:
        head = (f"SELECT count(*) AS stale_api_rows,\n"
                f"       count(*) FILTER (WHERE {price_filter}) AS stale_price_rows,\n")
    sql = (head +
           f"       count(*) FILTER (WHERE {fa} IS NULL\n"
           f"                          OR {fa} < NOW() - make_interval(secs => %(prf_licence_s)s))"
           f" AS api_rows_past_24h\n"
           f"  FROM {spec.table} t\n"
           f" WHERE {_stale_where(spec)}")
    return sql, params


def build_catalog_count_sql(catalog: Mapping[str, Any],
                            keys: Sequence[str]) -> tuple[str, dict[str, Any]]:
    """How many of ``keys`` are rows of the refresh catalog (its ``where``
    and ASIN regex applied), as ``catalog_rows``: e.g. the purged rows that
    are still pages the engine refreshes, as opposed to retired ones."""
    table = catalog.get("table") or ""
    asin_col = catalog.get("asin_col") or "asin"
    if not TABLE_RE.match(str(table)) or not IDENT_RE.match(str(asin_col)):
        raise ValueError("catalog.table / catalog.asin_col must be SQL identifiers")
    clash = [k for k in (catalog.get("params") or {}) if str(k).startswith(("prf_", "prp_"))]
    if clash:
        raise ValueError(f"catalog.params may not use the reserved prf_/prp_ prefixes: {clash}")
    params: dict[str, Any] = dict(catalog.get("params") or {})
    params.update({"prf_keys": list(dict.fromkeys(str(k) for k in keys)),
                   "prf_asin_re": str(catalog.get("asin_regex") or r"^[A-Z0-9]{10}$")})
    where = str(catalog.get("where") or "").strip()
    sql = (f"SELECT count(DISTINCT c.{asin_col}) AS catalog_rows\n"
           f"  FROM {table} c\n"
           f" WHERE ({where or 'TRUE'}) AND c.{asin_col} ~ %(prf_asin_re)s\n"
           f"   AND c.{asin_col} = ANY(%(prf_keys)s::text[])")
    return sql, params


def build_freshness_sql(spec: CacheSpec, catalog: Mapping[str, Any], *,
                        display_hours: float, min_price: float,
                        indexable_sql: str = "") -> tuple[str, dict[str, Any]]:
    """How many indexable pages have a fresh check / a fresh, quotable price.

    Indexable pages are ``indexable_sql`` (a trusted SELECT whose first column
    is the ASIN) or, by default, the refresh catalog itself. Fresh = answered
    within ``display_hours``; priced = outcome 'priced' with a price of at
    least ``min_price``."""
    params: dict[str, Any] = {"prf_display_s": float(display_hours) * 3600.0,
                              "prf_min_price": float(min_price)}
    if indexable_sql.strip():
        idx = f"SELECT DISTINCT q.asin FROM ({indexable_sql.strip()}) AS q(asin)"
    else:
        table = catalog.get("table") or ""
        asin_col = catalog.get("asin_col") or "asin"
        if not TABLE_RE.match(str(table)) or not IDENT_RE.match(str(asin_col)):
            raise ValueError("catalog.table / catalog.asin_col must be SQL identifiers")
        where = str(catalog.get("where") or "").strip()
        clash = [k for k in (catalog.get("params") or {}) if str(k).startswith(("prf_", "prp_"))]
        if clash:
            raise ValueError(f"catalog.params may not use the reserved prf_/prp_ prefixes: {clash}")
        params.update(dict(catalog.get("params") or {}))
        params["prf_asin_re"] = str(catalog.get("asin_regex") or r"^[A-Z0-9]{10}$")
        idx = (f"SELECT DISTINCT c.{asin_col} AS asin FROM {table} c\n"
               f"     WHERE ({where or 'TRUE'}) AND c.{asin_col} ~ %(prf_asin_re)s")
    fa = f"t.{spec.fetched_at_col}"
    fresh = f"{fa} >= NOW() - make_interval(secs => %(prf_display_s)s)"
    price = spec.column("price")
    priced = [fresh, f"t.{price} >= %(prf_min_price)s"]
    if spec.outcome_col:
        priced.append(f"t.{spec.outcome_col} = 'priced'")
    sql = (f"WITH idx AS MATERIALIZED (\n  {idx}\n)\n"
           f"SELECT count(*) AS indexable,\n"
           f"       count(t.{spec.key_col}) FILTER (WHERE {fresh}) AS fresh_checked,\n"
           f"       count(t.{spec.key_col}) FILTER (WHERE {' AND '.join(priced)}) AS fresh_priced\n"
           f"  FROM idx i LEFT JOIN {spec.table} t ON t.{spec.key_col} = i.asin")
    return sql, params


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------

def is_retryable_write_error(exc: BaseException) -> bool:
    """A deadlock or serialization failure: Postgres rolled the transaction
    back and the same statements may simply run again."""
    if getattr(exc, "pgcode", None) in ("40P01", "40001"):
        return True
    try:
        from psycopg2 import errors as pg_errors  # type: ignore
    except Exception:  # noqa: BLE001 - no driver, nothing is retryable
        return False
    return isinstance(exc, (pg_errors.DeadlockDetected, pg_errors.SerializationFailure))


def is_value_error(exc: BaseException) -> bool:
    """True when ``exc`` came from the values being written (a string the
    driver cannot encode, a number the column cannot hold, a CHECK on one
    answer), as opposed to the table, the connection or a lock, which fail
    every row the same way."""
    if isinstance(exc, (UnicodeError, ValueError)):  # client-side adaptation (encoding, NUL)
        return True
    return str(getattr(exc, "pgcode", "") or "")[:2] in VALUE_ERROR_CLASSES


def _cell(row: Any, i: int, name: str) -> Any:
    if isinstance(row, Mapping):
        return row.get(name)
    return row[i] if row is not None and len(row) > i else None


def _hours(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class PurgeResult:
    """What one purge did (``CacheWriter.purge_report``)."""

    #: (key, age in hours at the purge; None = the row had no fetched_at) of
    #: every row whose API columns were NULLed.
    purged: tuple[tuple[str, Optional[float]], ...] = ()
    #: Keys past the horizon left alone by the spare (see THE SPARE).
    spared: tuple[str, ...] = ()

    @property
    def count(self) -> int:
        return len(self.purged)

    def keys(self) -> list[str]:
        return [k for k, _ in self.purged]

    def within(self, hours: float) -> list[str]:
        """Keys of purged rows whose answer was at most ``hours`` old: values
        a visitor could still see when ``hours`` is the quotable window."""
        return [k for k, age in self.purged if age is not None and age <= float(hours)]


class CacheWriter:
    """Writes answers to one site's cache table over one DB connection.

    Each call is its own transaction (commit on success). A deadlock or
    serialization failure rolls back and re-runs the call up to ``retries``
    times, sleeping ``backoff_s * attempt`` plus up to 0.5 s of jitter (the
    reference site's verifier lost two runs in 12 days to exactly this before
    it retried). A value error (``is_value_error``) in an upsert re-writes the
    batch one row at a time and skips the rows that still fail, recording
    them in ``poison`` as (asin, error type); when every row of a batch of two
    or more fails, the first error propagates instead (the table is wrong,
    not an answer). Any other error rolls back and propagates."""

    def __init__(self, conn, spec: CacheSpec, *, consumer: str, retries: int = 3,
                 backoff_s: float = 1.0, sleep: Callable[[float], None] = time.sleep,
                 jitter: Optional[Callable[[], float]] = None):
        self.conn = conn
        self.spec = spec
        self.consumer = consumer
        self.retries = max(0, int(retries))
        self.backoff_s = max(0.0, float(backoff_s))
        self.sleep = sleep
        self.jitter = jitter or (lambda: random.uniform(0, 0.5))
        self.rows_written = 0
        self.rows_purged = 0
        self.retried = 0
        #: (asin, error type) of every answer skipped because its values
        #: could not be stored; the row keeps its previous values.
        self.poison: list[tuple[str, str]] = []

    def _transaction(self, fn: Callable[[Any], Any]) -> Any:
        attempt = 0
        while True:
            try:
                with self.conn.cursor() as cur:
                    out = fn(cur)
                self.conn.commit()
                return out
            except Exception as exc:
                try:
                    self.conn.rollback()
                except Exception:  # noqa: BLE001 - the original error matters
                    pass
                if not is_retryable_write_error(exc) or attempt >= self.retries:
                    raise
                attempt += 1
                self.retried += 1
                log.warning("cache write hit %s; retry %d/%d", type(exc).__name__,
                            attempt, self.retries)
                self.sleep(self.backoff_s * attempt + self.jitter())

    def upsert(self, rows: Sequence[tuple[str, str, Mapping[str, Any]]]) -> int:
        """Write ``(asin, outcome, values)`` rows: one statement, one commit.
        Returns the number of rows written (rows skipped on a value error are
        in ``poison``)."""
        rows = list(rows)
        if not rows:
            return 0
        bad = [a for a, s, _ in rows if s not in ANSWERED]
        if bad:
            raise ValueError(f"refusing to write rows without an answer: {bad[:5]}")
        try:
            self._write(rows)
        except Exception as exc:
            if not is_value_error(exc):
                raise
            return self._write_each(rows, exc)
        self.rows_written += len(rows)
        return len(rows)

    def _write(self, rows: Sequence[tuple[str, str, Mapping[str, Any]]]) -> None:
        sql = build_upsert_sql(self.spec, len(rows))
        params = upsert_params(self.spec, rows, self.consumer)
        self._transaction(lambda cur: cur.execute(sql, params))

    def _write_each(self, rows: Sequence[tuple[str, str, Mapping[str, Any]]],
                    first: BaseException) -> int:
        """A batch failed on a value: write its rows one at a time."""
        failed: list[tuple[str, str]] = []
        written = 0
        if len(rows) == 1:
            failed.append((rows[0][0], type(first).__name__))
        else:
            for row in rows:
                try:
                    self._write([row])
                except Exception as exc:
                    if not is_value_error(exc):
                        raise
                    failed.append((row[0], type(exc).__name__))
                    continue
                written += 1
            if not written:
                raise first
        for asin, kind in failed:
            log.warning("cache write skipped %s: its values cannot be stored (%s)", asin, kind)
        self.poison.extend(failed)
        self.rows_written += written
        return written

    def purge_report(self, *, spare: Iterable[str] = (),
                     spare_max_age_hours: Optional[float] = None) -> PurgeResult:
        """NULL every API column older than the spec's max age, except the
        spared rows (``build_purge_sql``), in one transaction that also lists
        the rows the spare kept. Returns what was purged and what was spared."""
        sql, params = build_purge_sql(self.spec, spare=spare,
                                      spare_max_age_hours=spare_max_age_hours)
        spared_q = build_spared_sql(self.spec, spare, spare_max_age_hours)
        key = self.spec.key_col

        def go(cur):
            cur.execute(sql, params)
            rows = list(cur.fetchall() or [])
            spared: list[str] = []
            if spared_q is not None:
                cur.execute(*spared_q)
                spared = [str(_cell(r, 0, "spared_key")) for r in (cur.fetchall() or [])]
            return rows, spared

        rows, spared = self._transaction(go)
        result = PurgeResult(
            purged=tuple((str(_cell(r, 0, key)), _hours(_cell(r, 1, "age_h"))) for r in rows),
            spared=tuple(spared))
        self.rows_purged += result.count
        return result

    def purge(self, *, spare: Iterable[str] = (),
              spare_max_age_hours: Optional[float] = None) -> int:
        """``purge_report`` returning only the number of rows purged."""
        return self.purge_report(spare=spare, spare_max_age_hours=spare_max_age_hours).count


def read_one(conn, sql: str, params: Mapping[str, Any], names: Iterable[str]) -> dict[str, int]:
    """Run a read-only aggregate and return its first row as {name: int};
    the transaction is rolled back afterwards either way."""
    names = list(names)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, dict(params))
            row = cur.fetchone()
    finally:
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
    return {n: int(_cell(row, i, n) or 0) for i, n in enumerate(names)}
