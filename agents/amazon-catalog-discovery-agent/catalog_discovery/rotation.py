"""Which (query, itemPage) slots a run searches, resumed across runs.

WHY THE ENGINE PAGES ITSELF
---------------------------
``amazon_discovery.discover()`` pages a keyword only while the previous page
came back full, so a short page (6-9 items) in the middle of a list ends it.
The sizing run saw that on 142 of 400 pages. Its fix (W3) waits for the
shared-module window, so this engine hands ``discover()`` ONE page at a time
(``pages_per_keyword=1``, the page pinned by a client adapter) and decides
here whether a query has more:

* an empty page ends the query;
* the API's itemPage ceiling (10) ends it; a config can lower the ceiling,
  never raise it;
* a totalResultCount BELOW the API's clip (306) is exact, so the query ends
  once ``page * 10`` reaches it; at the clip the length is unknown;
* a client error (4xx) ends it for this cycle (on page 1 the query itself is
  bad, e.g. a filter Creators refuses);
* a server error (5xx, 408) does NOT end it: the slot is left for the next
  run, and only after :data:`TRANSIENT_RETRY_CAP` failures of the same page
  does the query end (``server-error``), so one bad page never blocks a cycle;
* optionally, a page at or past ``low_yield_from_page`` that adds fewer new
  in-lane ASINs than ``low_yield_below`` ends it (``low-yield``: the sizing
  run's ``stop_if_new_inlane_lt``), so a cycle over a mostly known catalog
  does not walk every query to page 10;
* a short page does NOT end it.

ORDER
-----
Breadth first: every query's page 1, then every query's page 2, and so on,
config order within a level. A run therefore spends its few calls where new
listings are likeliest, and successive runs walk deeper. Progress lives in
agent state per query key (``next_page``), so a run that stops early (budget,
429, call cap, new-ASIN cap) resumes exactly where it stopped.

A *cycle* ends when every query has reached its end; the next run then starts
again at page 1 (new listings appear anywhere), unless ``cycle_cooldown_hours``
have not passed since the cycle ended. A cycle never restarts in the middle of
a run.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from .config import CREATORS_MAX_ITEM_PAGE, ITEMS_PER_PAGE, Query

STATE_VERSION = 1

END_EMPTY = "empty-page"
END_MAX_PAGE = "max-page"
END_TOTAL = "total-reached"
END_QUERY_ERROR = "query-error"
END_PAGE_ERROR = "page-error"
END_SERVER_ERROR = "server-error"
END_LOW_YIELD = "low-yield"
#: A slot outcome that is not an end: the page failed transiently and is
#: fetched again by a later run.
RETRY_LATER = "retry-later"
#: Server errors on the same page (across runs) before its query ends.
TRANSIENT_RETRY_CAP = 3
ERROR_ENDS = (END_QUERY_ERROR, END_PAGE_ERROR, END_SERVER_ERROR)


def _iso(ts: dt.datetime) -> str:
    return ts.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(text: Any) -> Optional[dt.datetime]:
    if not isinstance(text, str) or not text:
        return None
    try:
        return dt.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


@dataclass
class QueryProgress:
    """Per-query progress inside the current cycle."""

    next_page: int = 1
    ended: bool = False
    end_reason: str = ""
    total: Optional[int] = None
    saturated: bool = False
    pages: int = 0
    errors: int = 0
    #: Server errors on ``next_page`` so far (reset when a page succeeds).
    retries: int = 0

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "QueryProgress":
        total = raw.get("total")
        return cls(next_page=max(1, int(raw.get("next_page") or 1)),
                   ended=bool(raw.get("ended", False)),
                   end_reason=str(raw.get("end_reason") or ""),
                   total=int(total) if isinstance(total, (int, float)) else None,
                   saturated=bool(raw.get("saturated", False)),
                   pages=int(raw.get("pages") or 0), errors=int(raw.get("errors") or 0),
                   retries=int(raw.get("retries") or 0))

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"next_page": self.next_page}
        if self.ended:
            out["ended"] = True
            out["end_reason"] = self.end_reason
        if self.total is not None:
            out["total"] = self.total
        if self.saturated:
            out["saturated"] = True
        if self.pages:
            out["pages"] = self.pages
        if self.errors:
            out["errors"] = self.errors
        if self.retries:
            out["retries"] = self.retries
        return out


@dataclass(frozen=True)
class SlotOutcome:
    """What one fetched page meant for its query."""

    ended: bool
    end_reason: str = ""
    short_page_continued: bool = False
    saturated: bool = False


@dataclass
class Rotation:
    """Slot planner over the config's queries and the persisted progress."""

    queries: Sequence[Query]
    max_item_page: int
    result_count_cap: int
    cooldown_hours: float = 0.0
    now: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))
    #: Low-yield stop (0 = off): see the module docstring.
    low_yield_below: int = 0
    low_yield_from_page: int = 2
    cycle: int = 1
    cycle_started_at: Optional[str] = None
    # Set when the current cycle's last slot resolved (None while in progress).
    cycle_completed_at: Optional[str] = None
    last_cycle_completed_at: Optional[str] = None
    progress: dict[str, QueryProgress] = field(default_factory=dict)
    cycles_started_this_run: int = 0
    #: Queries whose slot failed transiently THIS run: skipped until the next
    #: run (not persisted; the slot stays open in the cycle).
    deferred: set[str] = field(default_factory=set)

    # -- state ---------------------------------------------------------------------

    @classmethod
    def from_state(cls, queries: Sequence[Query], state: Optional[Mapping[str, Any]], *,
                   max_item_page: int, result_count_cap: int, cooldown_hours: float = 0.0,
                   now: Optional[dt.datetime] = None, low_yield_below: int = 0,
                   low_yield_from_page: int = 2) -> "Rotation":
        state = state if isinstance(state, Mapping) else {}
        rot = cls(queries=list(queries), max_item_page=max_item_page,
                  result_count_cap=result_count_cap, cooldown_hours=cooldown_hours,
                  now=now or dt.datetime.now(dt.timezone.utc),
                  low_yield_below=max(0, int(low_yield_below or 0)),
                  low_yield_from_page=max(1, int(low_yield_from_page or 1)))
        if int(state.get("version") or STATE_VERSION) == STATE_VERSION:
            rot.cycle = max(1, int(state.get("cycle") or 1))
            rot.cycle_started_at = state.get("cycle_started_at")
            rot.cycle_completed_at = state.get("cycle_completed_at")
            rot.last_cycle_completed_at = state.get("last_cycle_completed_at")
            raw = state.get("queries") or {}
            known = {q.key for q in rot.queries}
            # Keys that left the config are pruned; new keys start at page 1.
            rot.progress = {k: QueryProgress.from_dict(v) for k, v in raw.items()
                            if k in known and isinstance(v, Mapping)}
        if not rot.cycle_started_at:
            rot.cycle_started_at = _iso(rot.now)
        return rot

    def to_state(self) -> dict[str, Any]:
        return {
            "version": STATE_VERSION,
            "cycle": self.cycle,
            "cycle_started_at": self.cycle_started_at,
            "cycle_completed_at": self.cycle_completed_at,
            "last_cycle_completed_at": self.last_cycle_completed_at,
            "queries": {k: p.to_dict() for k, p in sorted(self.progress.items())
                        if p.to_dict() != {"next_page": 1}},
        }

    # -- planning ------------------------------------------------------------------

    def _limit(self, query: Query) -> int:
        # The API refuses itemPage 11+: no config value can lift that.
        return max(1, min(query.max_pages, self.max_item_page, CREATORS_MAX_ITEM_PAGE))

    def _state(self, query: Query) -> QueryProgress:
        return self.progress.setdefault(query.key, QueryProgress())

    def fetchable(self, query: Query) -> bool:
        p = self.progress.get(query.key) or QueryProgress()
        return not p.ended and p.next_page <= self._limit(query)

    def begin(self) -> Optional[str]:
        """Call once at the start of a run. Returns None when there is work,
        'cooldown' when the cycle is complete and the cooldown still runs,
        'no-queries' when the config has none."""
        if not self.queries:
            return "no-queries"
        if any(self.fetchable(q) for q in self.queries):
            return None
        completed = _parse_iso(self.cycle_completed_at) or _parse_iso(self.cycle_started_at)
        if completed is not None and self.cooldown_hours > 0:
            if self.now < completed + dt.timedelta(hours=self.cooldown_hours):
                return "cooldown"
        self.last_cycle_completed_at = self.cycle_completed_at or _iso(self.now)
        self.cycle_completed_at = None
        self.cycle += 1
        self.cycle_started_at = _iso(self.now)
        self.progress = {}
        self.cycles_started_this_run += 1
        return None

    def cooldown_until(self) -> Optional[str]:
        """When the next cycle may start, while the current one is complete
        and cooling down; None otherwise."""
        if self.cooldown_hours <= 0 or any(self.fetchable(q) for q in self.queries):
            return None
        completed = _parse_iso(self.cycle_completed_at) or _parse_iso(self.cycle_started_at)
        if completed is None:
            return None
        return _iso(completed + dt.timedelta(hours=self.cooldown_hours))

    def next_slot(self) -> Optional[tuple[Query, int]]:
        """The lowest page level any query still needs, first query in config
        order at that level. None when nothing is left for THIS run: the
        cycle is complete, or only slots deferred by a server error remain."""
        best: Optional[tuple[int, int, Query]] = None
        for order, query in enumerate(self.queries):
            if query.key in self.deferred or not self.fetchable(query):
                continue
            page = (self.progress.get(query.key) or QueryProgress()).next_page
            if best is None or (page, order) < (best[0], best[1]):
                best = (page, order, query)
        return (best[2], best[0]) if best else None

    # -- recording -----------------------------------------------------------------

    def record(self, query: Query, page: int, *, items: int, total: Optional[int] = None,
               error: bool = False, new_in_lane: Optional[int] = None) -> SlotOutcome:
        """Record one fetched page of ``query``. ``items`` is the raw page
        length, ``total`` the response's totalResultCount (None/0 = absent),
        ``error`` a client error (4xx) for the page, ``new_in_lane`` the new
        in-lane ASINs the page added (for the low-yield stop; None = unknown)."""
        p = self._state(query)
        p.pages += 1
        if error:
            p.errors += 1
            p.retries = 0
            p.ended = True
            p.end_reason = END_QUERY_ERROR if page <= 1 else END_PAGE_ERROR
            self._note_completion()
            return SlotOutcome(True, p.end_reason)
        p.retries = 0
        if total:
            p.total = int(total)
        reachable = self._limit(query) * ITEMS_PER_PAGE
        saturated = bool(p.total) and (p.total >= self.result_count_cap or p.total > reachable)
        p.saturated = p.saturated or saturated
        if items <= 0:
            p.ended, p.end_reason = True, END_EMPTY
            p.next_page = page
            self._note_completion()
            return SlotOutcome(True, END_EMPTY, saturated=p.saturated)
        p.next_page = page + 1
        if page >= self._limit(query):
            p.ended, p.end_reason = True, END_MAX_PAGE
        elif p.total and p.total < self.result_count_cap and page * ITEMS_PER_PAGE >= p.total:
            p.ended, p.end_reason = True, END_TOTAL
        elif (self.low_yield_below and new_in_lane is not None
              and page >= self.low_yield_from_page and new_in_lane < self.low_yield_below):
            p.ended, p.end_reason = True, END_LOW_YIELD
        short = items < ITEMS_PER_PAGE and not p.ended
        if p.ended:
            self._note_completion()
        return SlotOutcome(p.ended, p.end_reason, short_page_continued=short,
                           saturated=p.saturated)

    def record_transient(self, query: Query, page: int, *,
                         retry_cap: int = TRANSIENT_RETRY_CAP) -> SlotOutcome:
        """A server error (5xx / 408) on ``page``: the slot stays open and is
        skipped for the rest of this run; after ``retry_cap`` such failures of
        the same page the query ends for the cycle (``server-error``)."""
        p = self._state(query)
        p.pages += 1
        p.errors += 1
        p.retries += 1
        p.next_page = max(p.next_page, page)
        if p.retries >= max(1, int(retry_cap)):
            p.ended, p.end_reason = True, END_SERVER_ERROR
            p.retries = 0
            self._note_completion()
            return SlotOutcome(True, END_SERVER_ERROR)
        self.deferred.add(query.key)
        return SlotOutcome(False, RETRY_LATER)

    def _note_completion(self) -> None:
        if self.cycle_completed_at is None and not any(self.fetchable(q) for q in self.queries):
            self.cycle_completed_at = _iso(self.now)

    # -- reporting -----------------------------------------------------------------

    def cycle_progress(self) -> float:
        """Share of this cycle's slots resolved, in [0, 1]. An ended query
        counts as fully resolved; others by pages fetched over their limit."""
        if not self.queries:
            return 1.0
        done = total = 0
        for query in self.queries:
            limit = self._limit(query)
            p = self.progress.get(query.key)
            total += limit
            if p is None:
                continue
            done += limit if p.ended else min(limit, p.next_page - 1)
        return round(done / total, 4) if total else 1.0

    def saturated_keys(self) -> list[str]:
        return sorted(k for k, p in self.progress.items() if p.saturated)

    def ended_with_errors(self) -> list[str]:
        return sorted(k for k, p in self.progress.items()
                      if p.ended and p.end_reason in ERROR_ENDS)

    def ended_low_yield(self) -> list[str]:
        return sorted(k for k, p in self.progress.items()
                      if p.ended and p.end_reason == END_LOW_YIELD)
