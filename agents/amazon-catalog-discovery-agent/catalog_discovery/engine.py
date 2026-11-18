"""One discovery run: seed import, then the search loop, then metrics.

THE SEARCH LOOP
---------------
For each slot the rotation hands out (a query and an itemPage), the engine
calls ``framework.core.amazon_discovery.discover()`` for exactly that page:
``discover`` keeps doing what it already does well (the pricing-integrity
gate, the new/known split against the catalog, the budget check before the
call, 429 and request-error handling), and a :class:`PinnedPageClient`
adapter sends the request for the slot's page with the query's filters
(browse node, brand, sort, price band), which ``discover`` cannot pass
itself. The engine then:

1. classifies every new listing into the site's lanes (in memory, title
   never stored) and keeps the in-lane ones, up to ``max_new_per_run``;
2. inserts them through the site's ``insert_sql`` (ASIN + our labels) and
   commits, page by page, so an interrupted run keeps what it found;
3. tells the rotation what the page means for the query's paging.

It stops at the run's call allowance, the new-ASIN cap, the end of the cycle,
the consumer's daily budget, a 429 (the slot is retried next run), a streak
of server errors (5xx: the slots are retried next run), or when the API
becomes unavailable (auth / network: the run fails, work so far is kept).

Listings the catalog already carries are not classified for insertion, but
their fresh titles are screened against the global reject patterns: a hit is
reported (``known_now_rejected``, ASIN + rule id) so a row that entered by an
older rule set or an untitled seed label can be found and removed.

BUDGET
------
``allowed calls = min(max_calls_per_run, the consumer's remaining daily
share, the day total left after reserving every protected consumer's unspent
share and the keep-unused floor)``. The reservation is this engine's local
stand-in for the reserved floor W3 adds to ``amazon_creators``: today
``remaining_budget`` lets a small consumer spend day-total headroom that the
price refresh still needs later in the UTC day.
"""
from __future__ import annotations

import datetime as dt
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from framework.core import amazon_creators, amazon_discovery

from .catalog import Catalog, CatalogRow, InsertOutcome
from .config import ITEMS_PER_PAGE, REJECT_LABEL, DiscoveryConfig, Query, SeedQuerySource
from .lanes import GLOBAL_REJECT, HELD_BACK, OFF_LANE, LaneClassifier, LaneDecision
from .rotation import Rotation
from .seeds import (UNTRUSTED_LABEL, SeedSourceError, plan_source, read_seed_file,
                    read_seed_query, records_signature)

STOP_MAX_CALLS = "max-calls"
STOP_MAX_NEW = "max-new"
STOP_BUDGET = "budget-exhausted"
STOP_THROTTLED = "throttled"
STOP_UNAVAILABLE = "creators-unavailable"
STOP_CYCLE_COMPLETE = "cycle-complete"
STOP_NO_ALLOWANCE = "no-allowance"
STOP_SERVER_ERRORS = "server-errors"
#: Nothing left for this run, but the cycle is not complete: the remaining
#: slots failed with a server error and wait for the next run.
STOP_DEFERRED = "slots-deferred"
INVALID_ASIN = "invalid-asin"

#: HTTP statuses that are the server's problem, not the query's: the slot is
#: retried by a later run instead of ending the query for the cycle.
TRANSIENT_STATUSES = frozenset({408}) | frozenset(range(500, 600))
#: Consecutive server errors that end the run (Amazon is having a bad time:
#: further calls would only burn the allowance).
SERVER_ERROR_STREAK_STOP = 3
KNOWN_REJECT_SAMPLE_CAP = 50

#: A partitioned query's listing counts as inside its band within this
#: tolerance (a band edge and a price that moved a little are not unit bugs).
PARTITION_TOLERANCE = 0.05
#: Warn about the price-filter unit when this share of listings falls outside
#: their band, once at least PARTITION_MIN_CHECKED listings were checked.
PARTITION_SUSPECT_SHARE = 0.8
PARTITION_MIN_CHECKED = 20
REJECT_SAMPLE_CAP = 300

TitleSink = Callable[[str, Optional[str], LaneDecision, str], None]


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BudgetDecision:
    allowed_calls: int
    max_calls_per_run: int
    own_remaining: Optional[int] = None
    day_total: Optional[int] = None
    day_left: Optional[int] = None
    protected_reserve: int = 0
    keep_floor: int = 0
    protected: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {k: (list(v) if isinstance(v, tuple) else v) for k, v in self.__dict__.items()}


def budget_allowance(cfg: DiscoveryConfig, client: Any, *, storage: Any = None,
                     max_calls: Optional[int] = None) -> BudgetDecision:
    """How many searchItems calls this run may make (see module docstring).
    ``max_calls`` (CLI override) can only lower the configured cap."""
    cap = cfg.limits.max_calls_per_run
    if max_calls is not None:
        cap = max(0, min(cap, int(max_calls)))
    own: Optional[int] = None
    if hasattr(client, "remaining_budget"):
        own = max(0, int(client.remaining_budget()))
    allowed = cap if own is None else min(cap, own)
    protect = [amazon_creators.normalize_consumer(c) for c in cfg.budget.protect_consumers]
    self_name = amazon_creators.normalize_consumer(cfg.consumer)
    protect = [c for c in dict.fromkeys(protect) if c != self_name]
    keep_fraction = cfg.budget.keep_unused_fraction_of_day
    if not protect and keep_fraction <= 0:
        return BudgetDecision(allowed_calls=allowed, max_calls_per_run=cap, own_remaining=own)
    bc = amazon_creators.load_budget_config(storage)
    usage = amazon_creators.usage_today(storage)
    try:
        total = max(0, int(bc.get("total_calls_per_day") or 0))
        default_share = float(bc.get("default_share", 0) or 0)
    except (TypeError, ValueError):
        total, default_share = 0, 0.0
    shares = bc.get("shares") or {}
    consumers = usage.get("consumers") or {}
    day_left = max(0, total - int(usage.get("total") or 0))
    reserve = 0
    for name in protect:
        try:
            share = float(shares.get(name, default_share) or 0)
        except (TypeError, ValueError):
            share = default_share
        used = int((consumers.get(name) or {}).get("calls") or 0)
        reserve += max(0, int(total * min(max(share, 0.0), 1.0)) - used)
    keep_floor = int(total * keep_fraction)
    headroom = max(0, day_left - reserve - keep_floor)
    return BudgetDecision(allowed_calls=min(allowed, headroom), max_calls_per_run=cap,
                          own_remaining=own, day_total=total, day_left=day_left,
                          protected_reserve=reserve, keep_floor=keep_floor,
                          protected=tuple(protect))


# ---------------------------------------------------------------------------
# Client adapter
# ---------------------------------------------------------------------------

class PinnedPageClient:
    """What ``discover()`` sees for one slot.

    ``discover()`` always asks for page 1 and passes no search filters. This
    adapter sends every ``search_items`` call for the slot's itemPage with the
    query's filters, and records the raw page length and totalResultCount the
    rotation needs. Budget checks and the throttle flag pass through. A
    request error's HTTP status is recorded (``error_status``) before it is
    re-raised to ``discover()``, so the engine can tell a refused query (4xx)
    from a server hiccup (5xx).
    """

    def __init__(self, client: Any, *, page: int, filters: Mapping[str, Any]) -> None:
        self._client = client
        self._page = int(page)
        self._filters = dict(filters)
        self.page_len: Optional[int] = None
        self.total_result_count: Optional[int] = None
        self.error_status: Optional[int] = None

    @property
    def last_search_meta(self) -> dict:
        return getattr(self._client, "last_search_meta", None) or {}

    @property
    def transient_error(self) -> bool:
        return self.error_status in TRANSIENT_STATUSES

    def budget_ok(self, consumer: str = "", calls: int = 1) -> bool:
        fn = getattr(self._client, "budget_ok", None)
        return True if fn is None else bool(fn(consumer, calls))

    def search_items(self, keywords: str, *, search_index: str = "All",
                     item_count: int = ITEMS_PER_PAGE, item_page: int = 1,
                     **filters: Any) -> list:
        merged = {**filters, **self._filters}
        try:
            items = self._client.search_items(keywords, search_index=search_index,
                                              item_count=item_count, item_page=self._page,
                                              **merged)
        except amazon_creators.CreatorsRequestError as exc:
            try:
                self.error_status = int(getattr(exc, "status", 0) or 0)
            except (TypeError, ValueError):
                self.error_status = 0
            raise
        items = list(items or [])
        self.page_len = len(items)
        try:
            self.total_result_count = int(self.last_search_meta.get("total_result_count") or 0) or None
        except (TypeError, ValueError):
            self.total_result_count = None
        return items


class _KnownOracle:
    """``known_asins`` for discover(): the catalog's ASINs plus every ASIN this
    run already classified (so a listing seen on two pages is judged once)."""

    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog
        self.db_known: set[str] = set()
        self.db_unknown: set[str] = set()
        self.run_seen: set[str] = set()

    def __call__(self, asins: Sequence[str]) -> set:
        fresh = sorted({a.upper() for a in asins if a}
                       - self.db_known - self.db_unknown - self.run_seen)
        if fresh:
            found = self.catalog.known(fresh)
            self.db_known |= found
            self.db_unknown |= set(fresh) - found
        return {a for a in asins if a and (a.upper() in self.db_known or a.upper() in self.run_seen)}


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

@dataclass
class SlotRecord:
    key: str
    lane: str
    page: int
    items: int
    total: Optional[int]
    new: int
    accepted: int
    rejected: int
    pricing_rejected: int
    known: int
    ended: bool
    end_reason: str
    error: bool

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class SearchReport:
    stopped_reason: str = ""
    api_calls: int = 0
    pages: int = 0
    throttled: int = 0
    request_errors: int = 0
    transient_errors: int = 0
    candidates: int = 0
    known_seen: int = 0
    accepted: list[CatalogRow] = field(default_factory=list)
    inserted: list[CatalogRow] = field(default_factory=list)
    insert_noop: int = 0
    insert_errors: int = 0
    insert_error_samples: list[str] = field(default_factory=list)
    deferred_max_new: int = 0
    rejected: Counter = field(default_factory=Counter)
    rejected_rules: Counter = field(default_factory=Counter)
    pricing_rejected: Counter = field(default_factory=Counter)
    cross_lane: int = 0
    node_confirmed: int = 0
    short_pages_continued: int = 0
    queries_ended: int = 0
    partition_checked: int = 0
    partition_misses: int = 0
    golden: list[dict[str, str]] = field(default_factory=list)
    slots: list[SlotRecord] = field(default_factory=list)
    reject_samples: list[dict[str, Any]] = field(default_factory=list)
    #: Catalog ASINs whose fresh search title hits a global reject pattern
    #: ({asin, rule}); the catalog is not changed, the operator acts on it.
    known_rejected: list[dict[str, str]] = field(default_factory=list)
    known_rejected_total: int = 0
    creators_error: str = ""

    @property
    def rejected_total(self) -> int:
        return sum(self.rejected.values())

    def lane_counts(self, rows: Optional[Iterable[CatalogRow]] = None) -> dict[str, int]:
        return dict(Counter(r.lane for r in (self.inserted if rows is None else rows)).most_common())

    @property
    def partition_unit_suspect(self) -> bool:
        return (self.partition_checked >= PARTITION_MIN_CHECKED
                and self.partition_misses >= PARTITION_SUSPECT_SHARE * self.partition_checked)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stopped_reason": self.stopped_reason,
            "api_calls": self.api_calls, "pages": self.pages, "throttled": self.throttled,
            "request_errors": self.request_errors, "transient_errors": self.transient_errors,
            "candidates": self.candidates,
            "known_seen": self.known_seen,
            "accepted": len(self.accepted), "inserted": len(self.inserted),
            "inserted_by_lane": self.lane_counts(),
            "insert_noop": self.insert_noop, "insert_errors": self.insert_errors,
            "insert_error_samples": list(self.insert_error_samples),
            "deferred_max_new": self.deferred_max_new,
            "rejected": dict(self.rejected), "rejected_rules": dict(self.rejected_rules.most_common(30)),
            "pricing_rejected": dict(self.pricing_rejected),
            "cross_lane": self.cross_lane, "node_confirmed": self.node_confirmed,
            "short_pages_continued": self.short_pages_continued,
            "queries_ended": self.queries_ended,
            "partition_checked": self.partition_checked, "partition_misses": self.partition_misses,
            "partition_unit_suspect": self.partition_unit_suspect,
            "rows": [{"asin": r.asin, "lane": r.lane, "confidence": r.confidence, "via": r.via,
                      "seed": r.seed} for r in self.inserted],
            "reject_samples": list(self.reject_samples),
            "known_rejected": list(self.known_rejected),
            "known_rejected_total": self.known_rejected_total,
            "slots": [s.to_dict() for s in self.slots],
            "creators_error": self.creators_error,
        }


@dataclass
class SeedReport:
    imported: list[CatalogRow] = field(default_factory=list)
    skipped: Counter = field(default_factory=Counter)
    already_known: int = 0
    noop: int = 0
    errors: dict[str, str] = field(default_factory=dict)
    insert_errors: int = 0
    unchanged: list[str] = field(default_factory=list)
    #: Sources cut by ``max_seed_asins_per_run``: the rest follows next run.
    truncated: list[str] = field(default_factory=list)
    #: Query sources that returned a full ``max_rows`` page: the next run
    #: continues after the page's last ASIN (keyset), nothing is dropped.
    continued: list[str] = field(default_factory=list)
    #: Sources whose label-only records were skipped as untrusted: name -> count.
    untrusted: dict[str, int] = field(default_factory=dict)
    per_source: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"imported": len(self.imported),
                "imported_by_lane": dict(Counter(r.lane for r in self.imported).most_common()),
                "skipped": dict(self.skipped), "already_known": self.already_known,
                "noop": self.noop, "insert_errors": self.insert_errors,
                "errors": dict(self.errors), "unchanged": list(self.unchanged),
                "truncated": list(self.truncated), "continued": list(self.continued),
                "untrusted": dict(self.untrusted), "per_source": dict(self.per_source)}


# ---------------------------------------------------------------------------
# Golden-label precision
# ---------------------------------------------------------------------------

def update_golden_window(window: Optional[Mapping[str, Any]], observations: Iterable[Mapping[str, str]],
                         *, run_ts: str, size: int) -> dict[str, dict[str, str]]:
    """Latest observation per golden ASIN, newest ``size`` kept."""
    prior = window if isinstance(window, Mapping) else {}
    out: dict[str, dict[str, str]] = {str(k): dict(v) for k, v in prior.items()
                                      if isinstance(v, Mapping)}
    for obs in observations:
        out[str(obs["asin"])] = {"expected": str(obs["expected"]),
                                 "predicted": str(obs["predicted"]), "run_ts": run_ts}
    if len(out) > size:
        keep = sorted(out.items(), key=lambda kv: (kv[1].get("run_ts", ""), kv[0]))[-size:]
        out = dict(keep)
    return out


def golden_metrics(window: Mapping[str, Mapping[str, str]], *, min_observations: int) -> dict[str, float]:
    """Precision of the reject and in-lane decisions against operator labels.

    reject_precision: of the labelled listings the rules REJECTED, the share
    the operator also labelled 'reject'. in_lane_precision: of the labelled
    listings the rules put in a lane, the share put in the RIGHT lane. A
    metric is reported only once ``min_observations`` decisions back it.
    """
    obs = [o for o in (window.values() if isinstance(window, Mapping) else []) if isinstance(o, Mapping)]
    out: dict[str, float] = {"golden_observations": float(len(obs))}
    rejected = [o for o in obs if o.get("predicted") == REJECT_LABEL]
    laned = [o for o in obs if o.get("predicted") != REJECT_LABEL]
    if len(rejected) >= min_observations:
        out["reject_precision"] = round(
            sum(1 for o in rejected if o.get("expected") == REJECT_LABEL) / len(rejected), 4)
    if len(laned) >= min_observations:
        out["in_lane_precision"] = round(
            sum(1 for o in laned if o.get("predicted") == o.get("expected")) / len(laned), 4)
    return out


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

def _node_ids(item: Mapping[str, Any]) -> list[str]:
    return [str(n.get("id")) for n in item.get("browse_nodes") or []
            if isinstance(n, Mapping) and n.get("id") is not None]


def _price(item: Mapping[str, Any]) -> Optional[float]:
    try:
        return None if item.get("price") is None else float(item["price"])
    except (TypeError, ValueError):
        return None


class DiscoveryEngine:
    """Runs the two phases over a config, a catalog and (for search) a client."""

    def __init__(self, cfg: DiscoveryConfig, *, classifier: Optional[LaneClassifier] = None,
                 now: Optional[dt.datetime] = None, log: Callable[[str], None] = print,
                 title_sink: Optional[TitleSink] = None) -> None:
        self.cfg = cfg
        self.classifier = classifier or LaneClassifier(cfg)
        self.now = now or dt.datetime.now(dt.timezone.utc)
        self.log = log
        self.title_sink = title_sink

    def rotation(self, state: Optional[Mapping[str, Any]]) -> Rotation:
        return Rotation.from_state(self.cfg.queries(), state,
                                   max_item_page=self.cfg.max_pages,
                                   result_count_cap=self.cfg.creators.result_count_cap,
                                   cooldown_hours=self.cfg.rotation.cycle_cooldown_hours,
                                   now=self.now,
                                   low_yield_below=self.cfg.limits.stop_if_new_in_lane_below,
                                   low_yield_from_page=self.cfg.limits.low_yield_from_page)

    # -- seeds -----------------------------------------------------------------------

    def import_seeds(self, catalog: Catalog, *, state: dict[str, Any],
                     connect_seed: Callable[[SeedQuerySource], Any]) -> SeedReport:
        """Import every due seed source (no Amazon calls). Mutates
        ``state['seed_sources']`` so an unchanged source is skipped next run."""
        report = SeedReport()
        if not isinstance(state.get("seed_sources"), dict):
            state["seed_sources"] = {}  # absent or malformed: re-check every source
        sources_state: dict[str, Any] = state["seed_sources"]
        budget_left = self.cfg.limits.max_seed_asins_per_run
        now_iso = self.now.strftime("%Y-%m-%dT%H:%M:%SZ")

        def ingest(src: Any, records: list, *, signature: str,
                   more: bool = False, cursor: str = "", next_cursor: str = "") -> None:
            nonlocal budget_left
            name = src.name
            plan = plan_source(records, src, cfg=self.cfg, classifier=self.classifier)
            known = catalog.known([r.asin for r in plan.rows]) if plan.rows else set()
            new_rows = [r for r in plan.rows if r.asin not in known]
            take = new_rows[:max(0, budget_left)]
            cut = len(new_rows) > len(take)
            outcome = catalog.insert(take)
            catalog.commit()
            budget_left -= len(take)
            report.imported.extend(outcome.inserted)
            report.skipped.update(plan.skipped)
            report.already_known += len(known)
            report.noop += outcome.noop
            report.insert_errors += outcome.errors
            if cut:
                report.truncated.append(name)
            elif more:
                report.continued.append(name)
            if plan.skipped.get(UNTRUSTED_LABEL):
                report.untrusted[name] = int(plan.skipped[UNTRUSTED_LABEL])
            # A cut page is read again next run (its inserted rows are known by
            # then); a full keyset page continues after its last ASIN; a source
            # read to its end with insert errors is re-read from the start.
            complete = not cut and not more and outcome.errors == 0
            entry: dict[str, Any] = {
                "signature": signature, "config": self.cfg.fingerprint, "complete": complete,
                "checked_at": now_iso, "records": len(records), "inserted": len(outcome.inserted),
                "already_known": len(known), "skipped": dict(plan.skipped),
            }
            if plan.notes:
                entry["notes"] = dict(plan.notes)
            if isinstance(src, SeedQuerySource):
                entry["cursor"] = cursor if cut else (next_cursor if more else "")
            sources_state[name] = entry
            report.per_source[name] = {k: v for k, v in entry.items()
                                       if k not in ("signature", "config", "cursor")}
            if more and not cut:
                report.per_source[name]["continues_next_run"] = True
            if outcome.error_samples:
                report.per_source[name]["insert_error_samples"] = outcome.error_samples

        for src in self.cfg.seed_files:
            if not src.enabled:
                continue
            try:
                records, sha = read_seed_file(src)
            except SeedSourceError as exc:
                report.errors[src.name] = str(exc)[:300]
                continue
            prev = sources_state.get(src.name) or {}
            if (prev.get("signature") == sha and prev.get("config") == self.cfg.fingerprint
                    and prev.get("complete")):
                report.unchanged.append(src.name)
                continue
            ingest(src, records, signature=sha)

        for qsrc in self.cfg.seed_queries:
            if not qsrc.enabled:
                continue
            prev = sources_state.get(qsrc.name) or {}
            same_config = prev.get("config") == self.cfg.fingerprint
            if prev.get("complete") and same_config:
                checked = _parse_ts(prev.get("checked_at"))
                if checked is not None and self.now < checked + dt.timedelta(hours=qsrc.refresh_hours):
                    report.unchanged.append(qsrc.name)
                    continue
            # An unfinished read continues where it stopped (same config only).
            cursor = str(prev.get("cursor") or "") if same_config and not prev.get("complete") else ""
            conn = None
            try:
                conn = connect_seed(qsrc)
                page = read_seed_query(conn, qsrc, after=cursor)
            except Exception as exc:  # noqa: BLE001 -- a seed source never fails the run
                report.errors[qsrc.name] = f"{type(exc).__name__}: {str(exc)[:240]}"
                continue
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:  # noqa: BLE001
                        pass
            ingest(qsrc, page.records, signature=records_signature(page.records),
                   more=page.more, cursor=cursor, next_cursor=page.last)
        return report

    # -- search ------------------------------------------------------------------------

    def run_search(self, client: Any, catalog: Catalog, *, rotation: Rotation,
                   allowed_calls: int) -> SearchReport:
        report = SearchReport()
        gate = rotation.begin()
        if gate is not None:
            report.stopped_reason = gate
            return report
        if allowed_calls <= 0:
            report.stopped_reason = STOP_NO_ALLOWANCE
            return report
        cfg = self.cfg
        max_new = cfg.limits.max_new_per_run
        oracle = _KnownOracle(catalog)
        golden = cfg.qa.golden
        golden_done: set[str] = set()
        known_counted: set[str] = set()
        asin_re = cfg.catalog.asin_re
        unit = cfg.creators.price_filter_unit
        server_error_streak = 0

        while True:
            if report.api_calls >= allowed_calls:
                report.stopped_reason = STOP_MAX_CALLS
                break
            if len(report.accepted) >= max_new:
                report.stopped_reason = STOP_MAX_NEW
                break
            slot = rotation.next_slot()
            if slot is None:
                report.stopped_reason = STOP_DEFERRED if rotation.deferred else STOP_CYCLE_COMPLETE
                break
            query, page = slot
            adapter = PinnedPageClient(client, page=page, filters=query.filters(unit))
            # A seed's own floor may only RAISE the pricing-integrity floor.
            floor = None if query.min_price is None else max(query.min_price, cfg.pricing.min_price)
            keyword = {"q": query.text, "category": query.lane,
                       "search_index": query.search_index, "min_price": floor}
            try:
                res = amazon_discovery.discover(
                    adapter, [keyword], known_asins=oracle, max_calls=1,
                    max_new=ITEMS_PER_PAGE, pages_per_keyword=1,
                    search_index=query.search_index, item_count=ITEMS_PER_PAGE,
                    min_price=cfg.pricing.min_price, currencies=cfg.pricing.currencies,
                    require_new=cfg.pricing.require_new_condition, respect_budget=True)
            except amazon_creators.CreatorsUnavailable as exc:
                report.stopped_reason = STOP_UNAVAILABLE
                report.creators_error = str(exc)[:300]
                break
            report.api_calls += res.api_calls
            report.throttled += res.throttled
            if res.stopped_reason in (STOP_BUDGET, STOP_THROTTLED) or res.api_calls == 0:
                # Not fetched (or fetched and throttled): the slot is retried next run.
                report.stopped_reason = res.stopped_reason or STOP_BUDGET
                break
            error = res.errors > 0
            report.request_errors += res.errors
            if error and adapter.transient_error:
                # Amazon's problem, not the query's: retried by a later run.
                report.transient_errors += 1
                server_error_streak += 1
                outcome_slot = rotation.record_transient(query, page)
                if outcome_slot.ended:
                    report.queries_ended += 1
                report.slots.append(SlotRecord(
                    key=query.key, lane=query.lane, page=page, items=0, total=None, new=0,
                    accepted=0, rejected=0, pricing_rejected=0, known=0,
                    ended=outcome_slot.ended, end_reason=outcome_slot.end_reason, error=True))
                if server_error_streak >= SERVER_ERROR_STREAK_STOP:
                    report.stopped_reason = STOP_SERVER_ERRORS
                    break
                continue
            server_error_streak = 0
            report.pricing_rejected.update(res.rejected)
            page_rows, overflow, slot_stats = self._classify_page(
                query, res, oracle=oracle, report=report, max_new=max_new,
                golden=golden, golden_done=golden_done, known_counted=known_counted,
                asin_re=asin_re)
            if page_rows:
                outcome = catalog.insert(page_rows)
                catalog.commit()
                self._merge_insert(report, outcome)
                report.accepted.extend(page_rows)
            if not error:
                report.pages += 1
            if overflow:
                # The cap cut this page short: leave the slot unrecorded so the
                # next run fetches it again (what was inserted is known by then).
                outcome_slot = None
            else:
                outcome_slot = rotation.record(query, page, items=adapter.page_len or 0,
                                               total=adapter.total_result_count, error=error,
                                               new_in_lane=len(page_rows))
                if outcome_slot.short_page_continued:
                    report.short_pages_continued += 1
                if outcome_slot.ended:
                    report.queries_ended += 1
            report.slots.append(SlotRecord(
                key=query.key, lane=query.lane, page=page, items=adapter.page_len or 0,
                total=adapter.total_result_count, new=slot_stats["new"],
                accepted=len(page_rows), rejected=slot_stats["rejected"],
                pricing_rejected=sum(res.rejected.values()), known=slot_stats["known"],
                ended=bool(outcome_slot and outcome_slot.ended),
                end_reason=outcome_slot.end_reason if outcome_slot else "deferred",
                error=error))
        return report

    # -- internals -------------------------------------------------------------------------

    def _classify_page(self, query: Query, res: Any, *, oracle: _KnownOracle, report: SearchReport,
                       max_new: int, golden: Mapping[str, str], golden_done: set[str],
                       known_counted: set[str], asin_re: Any
                       ) -> tuple[list[CatalogRow], bool, dict[str, int]]:
        rows: list[CatalogRow] = []
        overflow = False
        stats = {"new": 0, "rejected": 0, "known": 0}
        for item in res.candidates:
            asin = str(item.get("asin") or "").upper()
            if not asin:
                continue
            oracle.run_seen.add(asin)
            stats["new"] += 1
            report.candidates += 1
            price = _price(item)
            decision = self.classifier.classify(item.get("title"), price=price,
                                                browse_nodes=_node_ids(item))
            self._observe(asin, item, decision, query, golden=golden, golden_done=golden_done,
                          report=report, price=price)
            if not asin_re.match(asin):
                self._reject(report, asin, INVALID_ASIN, None, decision)
                stats["rejected"] += 1
                continue
            if decision.lane is None:
                rule = decision.rule if decision.reason == GLOBAL_REJECT else None
                self._reject(report, asin, decision.reason, rule, decision)
                stats["rejected"] += 1
                continue
            if len(report.accepted) + len(rows) >= max_new:
                report.deferred_max_new += 1
                overflow = True
                continue
            lane = self.cfg.lane(decision.lane)
            assert lane is not None
            if decision.lane != query.lane:
                report.cross_lane += 1
            if decision.node_confirmed:
                report.node_confirmed += 1
            rows.append(CatalogRow(asin=asin, lane=lane.id, lane_kind=lane.kind,
                                   lane_category=lane.category, confidence=decision.confidence,
                                   via=f"discovery:{query.lane}/{query.seed_id}", seed=query.key))
        for item in res.seen_known:
            asin = str(item.get("asin") or "").upper()
            if not asin:
                continue
            stats["known"] += 1
            price = _price(item)
            known_decision: Optional[LaneDecision] = None
            if asin in oracle.db_known and asin not in known_counted:
                known_counted.add(asin)
                report.known_seen += 1
                # Re-screen a catalog row with its fresh title: a global reject
                # now (an older rule set, an untitled seed label) is reported.
                known_decision = self.classifier.classify(item.get("title"), price=price,
                                                          browse_nodes=_node_ids(item))
                if known_decision.reason == GLOBAL_REJECT:
                    report.known_rejected_total += 1
                    if len(report.known_rejected) < KNOWN_REJECT_SAMPLE_CAP:
                        report.known_rejected.append({"asin": asin,
                                                      "rule": known_decision.rule or ""})
            if asin in golden and asin not in golden_done:
                decision = known_decision or self.classifier.classify(
                    item.get("title"), price=price, browse_nodes=_node_ids(item))
                self._observe(asin, item, decision, query, golden=golden, golden_done=golden_done,
                              report=report, price=price)
            elif query.partition is not None and price is not None:
                self._partition_check(query, price, report)
        return rows, overflow, stats

    def _observe(self, asin: str, item: Mapping[str, Any], decision: LaneDecision, query: Query, *,
                 golden: Mapping[str, str], golden_done: set[str], report: SearchReport,
                 price: Optional[float]) -> None:
        if asin in golden and asin not in golden_done:
            golden_done.add(asin)
            report.golden.append({"asin": asin, "expected": golden[asin],
                                  "predicted": decision.predicted_label})
        if query.partition is not None and price is not None:
            self._partition_check(query, price, report)
        if self.title_sink is not None:
            self.title_sink(asin, item.get("title"), decision, query.key)

    @staticmethod
    def _partition_check(query: Query, price: float, report: SearchReport) -> None:
        report.partition_checked += 1
        if not query.partition.contains(price, tolerance=PARTITION_TOLERANCE):
            report.partition_misses += 1

    @staticmethod
    def _reject(report: SearchReport, asin: str, reason: str, rule: Optional[str],
                decision: LaneDecision) -> None:
        report.rejected[reason] += 1
        if rule:
            report.rejected_rules[f"global:{rule}"] += 1
        elif reason == HELD_BACK and decision.matched_lane:
            report.rejected_rules[f"held-back:{decision.matched_lane}"] += 1
        elif reason == OFF_LANE:
            for step in decision.trace:
                report.rejected_rules[f"off-lane:{step}"] += 1
        if len(report.reject_samples) < REJECT_SAMPLE_CAP:
            sample: dict[str, Any] = {"asin": asin, "reason": reason}
            if rule:
                sample["rule"] = rule
            if decision.matched_lane:
                sample["matched_lane"] = decision.matched_lane
            if decision.trace:
                sample["trace"] = list(decision.trace)[:6]
            report.reject_samples.append(sample)

    @staticmethod
    def _merge_insert(report: SearchReport, outcome: InsertOutcome) -> None:
        report.inserted.extend(outcome.inserted)
        report.insert_noop += outcome.noop
        report.insert_errors += outcome.errors
        room = max(0, 5 - len(report.insert_error_samples))
        report.insert_error_samples.extend(outcome.error_samples[:room])


def _parse_ts(text: Any) -> Optional[dt.datetime]:
    if not isinstance(text, str):
        return None
    try:
        return dt.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None
