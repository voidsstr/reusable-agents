"""Editorial calendar: the topic of an authoring run, chosen in code.

Why: when an LLM ranks its own topics it drifts toward evergreen "best X"
pages, duplicates pages the site already renders from data, and pulls
untrusted text (trends, forums, Amazon) into the prompt to justify a pick.
A data site knows in advance which questions its pages cannot answer on
their own; the calendar lists them, in the order the data to answer them
will exist, and the engine writes the next one whose data is ready. No LLM
call chooses a topic and no outside text reaches the prompt.

File (a site's ``content/editorial-calendar.yaml``; the site owns it)::

    version: 3                      # bump on edits (also hashed: any edit changes signals)
    entries:
      - id: fridge-24h-outage        # stable, kebab-case, never reused
        order: 20
        slug: best-power-station-refrigerator-24-hour-outage   # the article slug (fixed in code)
        working_title: "..."
        lane: station                # product lane -> tag lane:<lane>
        occasion: winter-storm-season          # optional; tagged holiday:<id> while active
        occasion_required: false     # true = only while the occasion is NOW/IMMINENT
        window: {not_before: "2026-12-01", not_after: "2026-12-10"}   # optional publish window
        status: active               # active | held
        questions: ["...", "..."]    # what the guide must answer that no single hub answers
        link_instead_of_repeating: ["/runtime/refrigerator"]          # hubs it links, never restates
        placeholders: ["{{table:runtime:refrigerator}}", "{{picks:power-station-for-refrigerator}}"]
        scope: {presets: [refrigerator], lists: [power-station-for-refrigerator]}
        scenario: {h: [24]}          # the question's own parameters, allowed in prose
        requires:
          - {check: runtime_stations, min: 12, params: {preset: refrigerator}}
          - {check: list_picks, min: 3, params: {list: power-station-for-refrigerator}}
          - {flag: commerce}         # a site flag that must be on

``check`` names a readiness query in the site's engine config (a SELECT that
returns one count); the engine runs it with ``scope`` + ``params``.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import hashlib
import re
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

_ID = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class CalendarError(ValueError):
    """The calendar file cannot be used."""


@dataclasses.dataclass(frozen=True)
class Requirement:
    check: str = ""
    flag: str = ""
    min: int = 1
    params: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    note: str = ""

    def label(self) -> str:
        if self.flag:
            return f"flag {self.flag}"
        p = ", ".join(f"{k}={v}" for k, v in sorted(self.params.items()))
        return f"{self.check}({p}) >= {self.min}"


@dataclasses.dataclass(frozen=True)
class Entry:
    id: str
    order: int
    slug: str
    working_title: str
    lane: str = ""
    format: str = ""
    occasion: str = ""
    occasion_required: bool = False
    not_before: Optional[_dt.date] = None
    not_after: Optional[_dt.date] = None
    status: str = "active"
    questions: tuple[str, ...] = ()
    link_instead_of_repeating: tuple[str, ...] = ()
    placeholders: tuple[str, ...] = ()
    scope: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    scenario: Mapping[str, tuple[float, ...]] = dataclasses.field(default_factory=dict)
    requires: tuple[Requirement, ...] = ()
    brief: str = ""

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        for k in ("not_before", "not_after"):
            d[k] = d[k].isoformat() if d[k] else None
        return d


@dataclasses.dataclass(frozen=True)
class Calendar:
    version: str
    digest: str
    entries: tuple[Entry, ...]
    path: str = ""

    def by_id(self, entry_id: str) -> Optional[Entry]:
        return next((e for e in self.entries if e.id == entry_id), None)


def _date(raw: Any, where: str) -> Optional[_dt.date]:
    if raw in (None, ""):
        return None
    if isinstance(raw, _dt.date):
        return raw
    try:
        return _dt.date.fromisoformat(str(raw)[:10])
    except ValueError:
        raise CalendarError(f"{where}: not a date: {raw!r}") from None


def _strs(v: Any, where: str) -> tuple[str, ...]:
    if v is None:
        return ()
    if isinstance(v, str):
        return (v,)
    if not isinstance(v, (list, tuple)):
        raise CalendarError(f"{where}: expected a list")
    return tuple(str(x) for x in v)


def parse_calendar(doc: Mapping[str, Any], *, digest: str = "", path: str = "") -> Calendar:
    if not isinstance(doc, Mapping):
        raise CalendarError("calendar: expected a mapping with 'entries'")
    entries, ids, slugs = [], set(), set()
    for i, raw in enumerate(doc.get("entries") or []):
        w = f"entries[{i}]"
        if not isinstance(raw, Mapping):
            raise CalendarError(f"{w}: expected an object")
        eid = str(raw.get("id") or "")
        if not _ID.match(eid) or eid in ids:
            raise CalendarError(f"{w}.id: {eid!r} is not a unique kebab-case id")
        slug = str(raw.get("slug") or "")
        if not _SLUG.match(slug) or len(slug) > 120 or slug in slugs:
            raise CalendarError(f"{w}.slug: {slug!r} is not a unique slug of at most 120 characters")
        ids.add(eid)
        slugs.add(slug)
        reqs = []
        for j, r in enumerate(raw.get("requires") or []):
            if not isinstance(r, Mapping) or bool(r.get("check")) == bool(r.get("flag")):
                raise CalendarError(f"{w}.requires[{j}]: exactly one of check / flag")
            try:
                mn = int(r.get("min", 1))
            except (TypeError, ValueError):
                raise CalendarError(f"{w}.requires[{j}].min: not an integer") from None
            reqs.append(Requirement(check=str(r.get("check") or ""), flag=str(r.get("flag") or ""), min=mn,
                                    params=dict(r.get("params") or {}), note=str(r.get("note") or "")))
        window = raw.get("window") or {}
        scenario = {}
        for u, vals in (raw.get("scenario") or {}).items():
            scenario[str(u)] = tuple(float(x) for x in (vals or ()))
        status = str(raw.get("status") or "active")
        if status not in ("active", "held"):
            raise CalendarError(f"{w}.status: active or held")
        try:
            order = int(raw.get("order", i + 1))
        except (TypeError, ValueError):
            raise CalendarError(f"{w}.order: not an integer") from None
        entries.append(Entry(
            id=eid, order=order, slug=slug, working_title=str(raw.get("working_title") or ""),
            lane=str(raw.get("lane") or ""), format=str(raw.get("format") or ""),
            occasion=str(raw.get("occasion") or ""), occasion_required=bool(raw.get("occasion_required")),
            not_before=_date(window.get("not_before"), f"{w}.window.not_before"),
            not_after=_date(window.get("not_after"), f"{w}.window.not_after"), status=status,
            questions=_strs(raw.get("questions"), f"{w}.questions"),
            link_instead_of_repeating=_strs(raw.get("link_instead_of_repeating"), f"{w}.link_instead_of_repeating"),
            placeholders=_strs(raw.get("placeholders"), f"{w}.placeholders"),
            scope=dict(raw.get("scope") or {}), scenario=scenario, requires=tuple(reqs),
            brief=str(raw.get("brief") or "")))
    if not entries:
        raise CalendarError("calendar: no entries")
    entries.sort(key=lambda e: (e.order, e.id))
    return Calendar(version=str(doc.get("version") or ""), digest=digest, entries=tuple(entries), path=path)


def load_calendar(path: str | Path) -> Calendar:
    import yaml
    p = Path(path)
    try:
        data = p.read_bytes()
    except OSError as e:
        raise CalendarError(f"calendar {p}: {e}") from None
    try:
        doc = yaml.safe_load(data.decode("utf-8")) or {}
    except yaml.YAMLError as e:
        raise CalendarError(f"calendar {p}: invalid YAML: {e}") from None
    return parse_calendar(doc, digest=hashlib.sha256(data).hexdigest()[:16], path=str(p))


@dataclasses.dataclass
class Choice:
    entry: Optional[Entry]
    occasion: str = ""                 # holiday id to tag ('' = none active for this entry)
    skipped: list = dataclasses.field(default_factory=list)   # [{id, reason}]
    readiness: dict = dataclasses.field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"entry": self.entry.id if self.entry else None, "slug": self.entry.slug if self.entry else None,
                "occasion": self.occasion, "skipped": self.skipped, "readiness": self.readiness}


def evaluate_requirements(entry: Entry, *, count_fn: Callable[[str, Mapping[str, Any]], int],
                          flags: Mapping[str, Any]) -> tuple[bool, list[dict]]:
    """(all met, per-requirement report). `count_fn(check, params)` returns the
    count for a named check; an exception counts as not met."""
    report, ok = [], True
    for r in entry.requires:
        if r.flag:
            met = bool(flags.get(r.flag))
            report.append({"requirement": r.label(), "met": met, "value": bool(flags.get(r.flag))})
        else:
            params = {**dict(entry.scope), **dict(r.params)}
            try:
                n = int(count_fn(r.check, params))
                met = n >= r.min
                report.append({"requirement": r.label(), "met": met, "value": n})
            except Exception as e:  # noqa: BLE001 - a failing check is "not ready", reported
                met = False
                report.append({"requirement": r.label(), "met": False, "error": f"{type(e).__name__}: {e}"[:200]})
        ok = ok and met
    return ok, report


def choose(calendar: Calendar, *, done_slugs: Iterable[str], today: _dt.date,
           active_occasions: Iterable[str], count_fn: Callable[[str, Mapping[str, Any]], int],
           flags: Mapping[str, Any] | None = None, only: str = "",
           exclude: Mapping[str, str] | Iterable[str] = ()) -> Choice:
    """The first entry (calendar order) that is active, not yet written, inside
    its window, has its occasion when it requires one, and whose data
    requirements are all met. `only` forces one entry id (dry runs); it still
    reports readiness. `exclude` (ids, or {id: reason}) are entries the caller
    already tried this run and could not use (facts pack refused, too few live
    links, gate lane): they are skipped so one bad entry never wedges the
    calendar."""
    excl = dict(exclude) if isinstance(exclude, Mapping) else {str(x): "tried this run" for x in exclude}
    done = {str(s).lower() for s in done_slugs}
    active = set(active_occasions)
    flags = dict(flags or {})
    skipped: list[dict] = []
    for e in calendar.entries:
        if only and e.id != only:
            continue
        why = ""
        if e.id in excl and not only:
            why = f"tried this run: {excl[e.id]}"[:200]
        elif e.status != "active":
            why = "held in the calendar"
        elif e.slug.lower() in done:
            why = "already written (a row with this slug exists)"
        elif e.not_before and today < e.not_before:
            why = f"window opens {e.not_before.isoformat()}"
        elif e.not_after and today > e.not_after:
            why = f"window closed {e.not_after.isoformat()}"
        elif e.occasion_required and e.occasion not in active:
            why = f"occasion {e.occasion} not active"
        if why and not only:
            skipped.append({"id": e.id, "reason": why})
            continue
        ok, report = evaluate_requirements(e, count_fn=count_fn, flags=flags)
        if only:
            return Choice(e if (ok and not why) else None, e.occasion if e.occasion in active else "",
                          skipped + ([{"id": e.id, "reason": why}] if why else
                                     [] if ok else [{"id": e.id, "reason": "data not ready"}]),
                          {e.id: report})
        if not ok:
            skipped.append({"id": e.id, "reason": "data not ready",
                            "unmet": [r["requirement"] for r in report if not r["met"]]})
            continue
        return Choice(e, e.occasion if e.occasion in active else "", skipped, {e.id: report})
    return Choice(None, "", skipped, {})


__all__ = ["Calendar", "Entry", "Requirement", "Choice", "CalendarError", "parse_calendar", "load_calendar",
           "choose", "evaluate_requirements"]
