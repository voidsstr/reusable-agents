"""Authoring capacity: may an Opus author spend a call right now?

Why: an article engine that runs on a cron must stand down, without an Opus
call, when (a) the operator has frozen authoring (a blackout window such as a
Black Friday freeze, or a hard "not before" launch date), (b) the shared
claude-pool cannot spare Opus (``claude_pool.ready_profiles`` counts distinct
accounts that could serve the model now; a background author that sets
``CLAUDE_POOL_RESERVE=N`` is refused by the pool shim unless MORE than N
accounts are ready), or (c) the fleet's production authors deferred their own
article-author work recently because Opus was unavailable: a new site's guide
must never take the last Opus window from a site that is already earning.

This module answers those three questions as data; it never calls a model and
never writes. Callers record the decision in their run metrics and stop.

    from framework.core import authoring_capacity as cap
    w = cap.window_decision(cfg.schedule, now=now, live=True)
    if not w.allowed: ...                       # blackout / not before
    p = cap.pool_decision(model="claude-opus-5-5", min_ready=2, storage=storage,
                          peer_patterns=["*-article-proposal-agent"],
                          peer_reasons=["required-model-unavailable"], peer_hours=6)
    if not p.allowed: ...                       # defer

Config shape (a site's own YAML; no storage config, no site names here):

    schedule:
      not_before: "2026-12-04T00:00:00Z"        # live runs: no Opus before this
      dry_run_not_before: "2026-12-02T00:00:00Z" # dry runs with the live model
      blackouts:
        - {start: "2026-11-26T02:00:00Z", end: "2026-12-02T00:00:00Z", reason: "BFCM freeze"}
    pool:
      min_ready_profiles: 2
      peer_defer_watch:
        agent_patterns: ["*-article-proposal-agent"]
        reasons: ["required-model-unavailable"]
        lookback_hours: 6

Peer defers are read (read-only) from two places, because the fleet writes
two signals: the per-rec defer-backoff records
(``framework/defer-backoff/<source_agent_id>.json``, written by
``defer_backoff.record_defer`` on the implementer's force-fallback path), and
the whole-batch ``deferred.json`` the implementer writes into the producer's
run dir (``agents/<source_agent_id>/runs/<run_ts>/deferred.json``, its
pool-exhausted rc=75/124 path, which records no backoff entry). Storage that
cannot be read DENIES (fail closed): an unreadable signal is not "no defers".

The day's Opus budget is a small local ledger (:class:`UsageLedger`, a JSON
file outside framework storage) that dry runs with the live model and live
runs share, so neither can spend the other's minutes and a run that raises
after a model call still counts what it spent.
"""
from __future__ import annotations

import contextlib
import dataclasses
import datetime as _dt
import fnmatch
import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

DEFER_PREFIX = "framework/defer-backoff/"
RUN_DEFER_NAME = "deferred.json"
#: deferred.json reasons / fields that mean "the required model was unavailable".
MODEL_DEFER_RE = re.compile(r"required[- _]model|required-model-unavailable|opus|pool|rate[- ]limit|rc=(?:75|124)",
                            re.I)
LEDGER_ENV = "AUTHORING_LEDGER_DIR"


class PeerStateUnreadable(RuntimeError):
    """The peer-defer signal could not be read; callers deny (fail closed)."""


class CapacityConfigError(ValueError):
    """A schedule or pool block that cannot be read. Callers fail closed."""


def _utc(d: _dt.datetime) -> _dt.datetime:
    return d if d.tzinfo else d.replace(tzinfo=_dt.timezone.utc)


def parse_instant(raw: Any, where: str) -> Optional[_dt.datetime]:
    """An ISO-8601 instant (a bare date is midnight UTC). None/'' is None."""
    if raw in (None, ""):
        return None
    if isinstance(raw, _dt.datetime):
        return _utc(raw)
    if isinstance(raw, _dt.date):
        return _dt.datetime(raw.year, raw.month, raw.day, tzinfo=_dt.timezone.utc)
    s = str(raw).strip()
    try:
        if len(s) == 10:
            d = _dt.date.fromisoformat(s)
            return _dt.datetime(d.year, d.month, d.day, tzinfo=_dt.timezone.utc)
        return _utc(_dt.datetime.fromisoformat(s.replace("Z", "+00:00")))
    except ValueError:
        raise CapacityConfigError(f"{where}: not an ISO-8601 instant: {raw!r}") from None


@dataclasses.dataclass(frozen=True)
class Blackout:
    start: _dt.datetime
    end: _dt.datetime
    reason: str = ""

    def contains(self, now: _dt.datetime) -> bool:
        return self.start <= now < self.end


@dataclasses.dataclass(frozen=True)
class Schedule:
    not_before: Optional[_dt.datetime] = None
    dry_run_not_before: Optional[_dt.datetime] = None
    blackouts: tuple[Blackout, ...] = ()


def parse_schedule(block: Optional[Mapping[str, Any]]) -> Schedule:
    block = dict(block or {})
    out = []
    for i, b in enumerate(block.get("blackouts") or []):
        if not isinstance(b, Mapping):
            raise CapacityConfigError(f"schedule.blackouts[{i}]: expected an object")
        start = parse_instant(b.get("start"), f"schedule.blackouts[{i}].start")
        end = parse_instant(b.get("end"), f"schedule.blackouts[{i}].end")
        if start is None or end is None or end <= start:
            raise CapacityConfigError(f"schedule.blackouts[{i}]: needs start < end")
        out.append(Blackout(start, end, str(b.get("reason") or "")))
    return Schedule(not_before=parse_instant(block.get("not_before"), "schedule.not_before"),
                    dry_run_not_before=parse_instant(block.get("dry_run_not_before"),
                                                     "schedule.dry_run_not_before"),
                    blackouts=tuple(out))


@dataclasses.dataclass
class Decision:
    allowed: bool
    reason: str
    kind: str = ""                     # "" | blackout | not-before | pool | peer-defer
    details: dict = dataclasses.field(default_factory=dict)

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def window_decision(schedule: Schedule, *, now: Optional[_dt.datetime] = None,
                    live: bool = True) -> Decision:
    """May an Opus call happen at `now`? `live` = a publishing run (else a dry
    run that would still call the live model; it reads ``dry_run_not_before``,
    falling back to ``not_before``). Blackouts apply to both."""
    now = _utc(now or _dt.datetime.now(_dt.timezone.utc))
    for b in schedule.blackouts:
        if b.contains(now):
            return Decision(False, f"blackout until {b.end.isoformat()}" + (f" ({b.reason})" if b.reason else ""),
                            "blackout", {"start": b.start.isoformat(), "end": b.end.isoformat()})
    nb = schedule.not_before if live else (schedule.dry_run_not_before or schedule.not_before)
    if nb is not None and now < nb:
        return Decision(False, f"not before {nb.isoformat()}" + ("" if live else " (dry run)"), "not-before",
                        {"not_before": nb.isoformat()})
    return Decision(True, "outside every blackout and past not-before")


def reserve_from_env(environ: Optional[Mapping[str, str]] = None) -> int:
    env = os.environ if environ is None else environ
    try:
        return max(0, int(str(env.get("CLAUDE_POOL_RESERVE", "0") or 0)))
    except ValueError:
        return 0


def required_ready(min_ready: int, reserve: int) -> int:
    """Accounts that must be ready: the config floor, and more than the
    caller's CLAUDE_POOL_RESERVE (the shim refuses at <= reserve)."""
    return max(int(min_ready or 0), int(reserve or 0) + 1, 1)


def _peer_agents(storage, agent_patterns: Sequence[str], exclude_agent: str) -> list[str]:
    names = set()
    for child in storage.list_child_prefixes("agents/") or []:
        if child != exclude_agent and any(fnmatch.fnmatchcase(child, p) for p in agent_patterns):
            names.add(child)
    return sorted(names)


def _run_dir_defers(storage, agent: str, *, cutoff: _dt.datetime, now: _dt.datetime,
                    reasons: Sequence[str]) -> list[dict]:
    """deferred.json files in the agent's run dirs that say the required model
    was unavailable at or after `cutoff` (their deferred_at, else the run_ts).
    The producer's run may predate its dispatch, so run dirs from two days
    before the cutoff are read too."""
    out = []
    floor = (cutoff - _dt.timedelta(days=2)).strftime("%Y%m%dT%H%M%S")
    for run_ts in storage.list_child_prefixes(f"agents/{agent}/runs/") or []:
        if run_ts[:15] < floor[:15]:
            continue
        key = f"agents/{agent}/runs/{run_ts}/{RUN_DEFER_NAME}"
        if not storage.exists(key):
            continue
        try:
            at = _dt.datetime.strptime(run_ts[:15], "%Y%m%dT%H%M%S").replace(tzinfo=_dt.timezone.utc)
        except ValueError:
            continue
        doc = storage.read_json(key) or {}
        if not isinstance(doc, Mapping):
            continue
        try:
            stamped = parse_instant(doc.get("deferred_at"), "deferred_at") if doc.get("deferred_at") else None
        except CapacityConfigError:
            stamped = None
        at = stamped or at
        if at < cutoff:
            continue
        why = " ".join(str(doc.get(k) or "") for k in ("reason", "required_model", "exit_code"))
        model_defer = bool(doc.get("required_model")) or doc.get("exit_code") in (75, 124) or bool(
            MODEL_DEFER_RE.search(why))
        if reasons and not (model_defer or any(r in why for r in reasons)):
            continue
        out.append({"agent_id": agent, "rec_id": str(doc.get("rec_ids") or "")[:80], "at": at.isoformat(),
                    "reason": str(doc.get("reason") or "")[:120], "source": key})
    return out


def recent_peer_defers(storage, *, agent_patterns: Sequence[str], reasons: Sequence[str] = (),
                       lookback_hours: float = 6.0, now: Optional[_dt.datetime] = None,
                       exclude_agent: str = "") -> list[dict]:
    """Defers recorded for agents matching `agent_patterns` (fnmatch on the
    source agent id) within `lookback_hours`: defer-backoff entries whose last
    reason starts with one of `reasons` (any reason when empty), and run-dir
    deferred.json files that say the required model was unavailable.
    Read-only. Raises PeerStateUnreadable when storage is missing or a listing
    or read fails (the caller denies); a malformed entry is skipped."""
    if not agent_patterns:
        return []
    if storage is None:
        raise PeerStateUnreadable("no storage to read peer defers from")
    now = _utc(now or _dt.datetime.now(_dt.timezone.utc))
    cutoff = now - _dt.timedelta(hours=float(lookback_hours))
    out = []
    try:
        keys = storage.list_prefix(DEFER_PREFIX) or []
        for key in sorted(keys):
            name = key[len(DEFER_PREFIX):]
            if "/" in name or not name.endswith(".json"):
                continue
            agent = name[:-5]
            if agent == exclude_agent or not any(fnmatch.fnmatchcase(agent, p) for p in agent_patterns):
                continue
            doc = storage.read_json(key) or {}
            for rec_id, e in ((doc.get("recs") or {}) if isinstance(doc, Mapping) else {}).items():
                if not isinstance(e, Mapping) or not e.get("last_deferred_at"):
                    continue
                try:
                    at = parse_instant(e.get("last_deferred_at"), "last_deferred_at")
                except CapacityConfigError:
                    continue                      # malformed entry: skipped, never a crash
                if at is None or at < cutoff:
                    continue
                why = str(e.get("last_reason") or "")
                if reasons and not any(why.startswith(r) for r in reasons):
                    continue
                out.append({"agent_id": agent, "rec_id": str(rec_id), "at": at.isoformat(), "reason": why[:120],
                            "source": key})
        for agent in _peer_agents(storage, agent_patterns, exclude_agent):
            out += _run_dir_defers(storage, agent, cutoff=cutoff, now=now, reasons=reasons)
    except PeerStateUnreadable:
        raise
    except Exception as e:  # noqa: BLE001 - unreadable is not "no defers"
        raise PeerStateUnreadable(f"peer defer state unreadable: {type(e).__name__}") from None
    return out


def pool_decision(*, model: str, min_ready: int = 2, storage=None,
                  peer_patterns: Sequence[str] = (), peer_reasons: Sequence[str] = (),
                  peer_hours: float = 6.0, self_agent: str = "",
                  ready_fn: Optional[Callable[[str], Iterable[str]]] = None,
                  environ: Optional[Mapping[str, str]] = None,
                  now: Optional[_dt.datetime] = None) -> Decision:
    """Proceed only when enough distinct pool accounts can serve `model` now
    and no production author deferred for lack of it recently."""
    if ready_fn is None:
        from framework.cli import claude_pool
        ready_fn = claude_pool.ready_profiles
    reserve = reserve_from_env(environ)
    need = required_ready(min_ready, reserve)
    try:
        ready = list(ready_fn(model) or [])
    except Exception as e:  # noqa: BLE001 - an unreadable pool is not capacity
        return Decision(False, f"claude-pool state unreadable: {type(e).__name__}", "pool",
                        {"ready": 0, "need": need, "reserve": reserve})
    details = {"ready": len(ready), "need": need, "reserve": reserve, "model": model}
    if len(ready) < need:
        return Decision(False, f"claude-pool has {len(ready)} account(s) ready for {model}; need {need}"
                        + (f" (CLAUDE_POOL_RESERVE={reserve})" if reserve else ""), "pool", details)
    try:
        defers = recent_peer_defers(storage, agent_patterns=peer_patterns, reasons=peer_reasons,
                                    lookback_hours=peer_hours, now=now, exclude_agent=self_agent)
    except PeerStateUnreadable as e:
        return Decision(False, f"{e}; deferring (an unreadable signal is not 'no defers')", "peer-defer", details)
    if defers:
        details["peer_defers"] = defers[:10]
        agents = sorted({d["agent_id"] for d in defers})
        return Decision(False, f"production authors deferred in the last {peer_hours:g}h: {', '.join(agents)}",
                        "peer-defer", details)
    return Decision(True, f"{len(ready)} account(s) ready for {model} (need {need}); no recent peer defers",
                    "", details)


# ---------------------------------------------------------------------------
# The day's model budget: a local ledger shared by dry and live runs
# ---------------------------------------------------------------------------

class LedgerError(RuntimeError):
    """The ledger cannot be read or written; callers deny (fail closed)."""


def ledger_path(agent_id: str, environ: Optional[Mapping[str, str]] = None) -> Path:
    """``$AUTHORING_LEDGER_DIR/<agent_id>.json`` (default
    ``~/.reusable-agents/authoring-ledger/``): a host-local file, never
    framework storage (dry runs may not write storage)."""
    env = os.environ if environ is None else environ
    root = env.get(LEDGER_ENV) or os.environ.get(LEDGER_ENV) or os.path.expanduser(
        "~/.reusable-agents/authoring-ledger")
    safe = re.sub(r"[^A-Za-z0-9_.-]", "-", agent_id or "agent")[:100]
    return Path(root) / f"{safe}.json"


class UsageLedger:
    """Per-UTC-day model usage for one agent: ``opus_minutes``, ``live_calls``,
    ``dry_run_calls``. Locked with fcntl; days older than 14 are dropped."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    @contextlib.contextmanager
    def _locked(self):
        import fcntl
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(str(self.path) + ".lock", "a+")
        except OSError as e:
            raise LedgerError(f"ledger {self.path}: {type(e).__name__}") from None
        try:
            fcntl.flock(fh, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
            fh.close()

    def _load(self) -> dict:
        if not self.path.exists():
            return {"days": {}}
        try:
            doc = json.loads(self.path.read_text() or "{}")
        except (OSError, ValueError) as e:
            raise LedgerError(f"ledger {self.path} unreadable: {type(e).__name__}") from None
        if not isinstance(doc, dict) or not isinstance(doc.get("days", {}), dict):
            raise LedgerError(f"ledger {self.path}: not a ledger document")
        doc.setdefault("days", {})
        return doc

    def day(self, day: str) -> dict:
        with self._locked():
            d = dict(self._load()["days"].get(day) or {})
        return {"opus_minutes": float(d.get("opus_minutes") or 0.0), "live_calls": int(d.get("live_calls") or 0),
                "dry_run_calls": int(d.get("dry_run_calls") or 0)}

    def add(self, day: str, *, minutes: float = 0.0, live_calls: int = 0, dry_run_calls: int = 0) -> dict:
        with self._locked():
            doc = self._load()
            d = doc["days"].setdefault(day, {})
            d["opus_minutes"] = round(float(d.get("opus_minutes") or 0.0) + float(minutes), 3)
            d["live_calls"] = int(d.get("live_calls") or 0) + int(live_calls)
            d["dry_run_calls"] = int(d.get("dry_run_calls") or 0) + int(dry_run_calls)
            for old in sorted(doc["days"])[:-14]:
                doc["days"].pop(old, None)
            tmp = self.path.with_suffix(".tmp")
            try:
                tmp.write_text(json.dumps(doc, indent=1, sort_keys=True))
                os.replace(tmp, self.path)
            except OSError as e:
                raise LedgerError(f"ledger {self.path} not written: {type(e).__name__}") from None
            return dict(d)


__all__ = ["UsageLedger", "LedgerError", "ledger_path", "PeerStateUnreadable", "Blackout", "Schedule", "Decision", "CapacityConfigError", "parse_schedule", "parse_instant",
           "window_decision", "pool_decision", "recent_peer_defers", "required_ready", "reserve_from_env"]
