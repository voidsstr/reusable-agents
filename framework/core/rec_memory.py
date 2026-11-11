"""Rec memory — shared dedup primitive for any producer agent.

Producer agents (competitor-research, progressive-improvement,
seo-opportunity, article-author, catalog-audit, head-to-head, ...)
were each re-emitting the same recs every cron tick because the
LLM prompt that generates recs had no awareness of what was already
proposed. This module gives all agents a single, uniform way to:

  1. Load the open + recently-implemented proposals from prior runs
  2. Build a `prior_block` string to inject into the LLM prompt so
     the model knows what NOT to re-emit
  3. After the run, persist the new recs back to the accumulator

Stored at `agents/<id>/state/accumulator.json` (one file per agent).

Compatible with the existing `competitor-research-agent/_accumulator.py`
schema so a future PR can collapse the two into one — for now this
file is the canonical version for all OTHER producers to import.

USAGE in a producer agent's run():

    from framework.core.rec_memory import prior_recs_prompt_block, persist_run

    prior_block = prior_recs_prompt_block(
        storage=self.storage, agent_id=self.agent_id,
        max_entries=120,
    )
    user_msg = f"... {COMPARE_INPUTS} ...{prior_block}\\n\\n... Produce up to N recs ..."
    raw = self.ai_chat([...{"role":"user","content":user_msg}...])
    ...
    persist_run(
        storage=self.storage, agent_id=self.agent_id, run_ts=self.run_ts,
        new_recs=recs,  # list of {id, title, type, ...}
    )

The LLM is explicitly instructed to skip recs equivalent to anything
in the prior block — see the system-prompt fragment `SKIP_PRIOR_RULE`
below for the canonical wording.
"""
from __future__ import annotations

import functools
import hashlib
import re
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional


SKIP_PRIOR_RULE = (
    "NEVER propose a feature substantively equivalent to anything "
    "listed under PREVIOUSLY-PROPOSED RECOMMENDATIONS. If you would "
    "propose one of those, SKIP it and emit a DIFFERENT gap instead. "
    "The operator has already seen those — re-emitting them wastes "
    "review cycles + LLM tokens."
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _accumulator_key(agent_id: str) -> str:
    return f"agents/{agent_id}/state/accumulator.json"


def _normalize_title(title: str) -> str:
    """Strip whitespace, lowercase, drop non-alphanumeric — used as the
    dedup key. Two recs are "the same" if their normalized title matches.
    """
    t = (title or "").strip().lower()
    return re.sub(r"[^a-z0-9]+", "", t)[:120]


def _proposal_id(title: str) -> str:
    """Stable opaque id for a proposal, derived from its normalized title.
    Lets us match across runs even when the rec's `id` field rotates
    (e.g. rec-001 on Monday + rec-001 on Tuesday usually point at
    different actual recs)."""
    h = hashlib.sha1(_normalize_title(title).encode("utf-8")).hexdigest()
    return f"p-{h[:10]}"


def load_active(storage, agent_id: str) -> dict:
    """Read the accumulator doc. Returns an empty skeleton if missing."""
    try:
        d = storage.read_json(_accumulator_key(agent_id))
    except Exception:
        d = None
    if not isinstance(d, dict):
        d = {}
    d.setdefault("agent_id", agent_id)
    d.setdefault("proposals", [])
    d.setdefault("last_run_ts", "")
    return d


def save_active(storage, agent_id: str, doc: dict) -> None:
    storage.write_json(_accumulator_key(agent_id), doc)


def prior_recs_prompt_block(
    *,
    storage,
    agent_id: str,
    max_entries: int = 120,
    include_states: tuple[str, ...] = ("open", "implemented", "deferred"),
) -> str:
    """Build the LLM-prompt fragment that lists previously-proposed recs.

    Returns an EMPTY string when no prior recs exist (so callers can
    interpolate it unconditionally without extra branching).
    """
    doc = load_active(storage, agent_id)
    lines: list[str] = []
    for p in doc.get("proposals", []):
        state = p.get("state", "open")
        if state not in include_states:
            continue
        title = (p.get("title") or "").strip()
        if not title:
            continue
        lines.append(f"  [{state}] {title[:140]}")
        if len(lines) >= max_entries:
            break
    if not lines:
        return ""
    return (
        "\n\nPREVIOUSLY-PROPOSED RECOMMENDATIONS (already in backlog "
        "or shipped — DO NOT re-emit anything substantively equivalent; "
        "pick a DIFFERENT gap instead):\n" + "\n".join(lines)
    )


def merge_run(doc: dict, *, new_recs: list[dict], run_ts: str) -> dict:
    """Merge this run's emitted recs into the accumulator. Existing
    proposals (matched by normalized title) get `last_seen_at` bumped
    and `seen_count` incremented. Genuinely new titles get appended in
    state=open."""
    proposals = list(doc.get("proposals", []))
    by_norm = {_normalize_title(p.get("title", "")): p for p in proposals}
    now = _now_iso()
    added = 0
    bumped = 0
    for r in new_recs or []:
        if not isinstance(r, dict):
            continue
        title = (r.get("title") or "").strip()
        if not title:
            continue
        norm = _normalize_title(title)
        if norm in by_norm:
            existing = by_norm[norm]
            existing["last_seen_at"] = now
            existing["last_seen_run_ts"] = run_ts
            existing["seen_count"] = int(existing.get("seen_count", 1)) + 1
            bumped += 1
        else:
            proposals.append({
                "id": _proposal_id(title),
                "title": title,
                "rec_type": r.get("type") or r.get("category") or "",
                "severity": r.get("severity", "medium"),
                "state": "open",
                "first_seen_at": now,
                "last_seen_at": now,
                "first_seen_run_ts": run_ts,
                "last_seen_run_ts": run_ts,
                "seen_count": 1,
                # Carry the rec body so we can re-dispatch the full content later
                "body": {
                    k: r.get(k) for k in (
                        "description", "rationale", "files", "evidence",
                        "expected_impact", "confidence", "tier_recommendation",
                    ) if r.get(k) is not None
                },
            })
            added += 1
    doc["proposals"] = proposals
    doc["last_run_ts"] = run_ts
    doc["last_merge"] = {"added": added, "bumped": bumped, "at": now}
    return doc


def transition(
    doc: dict, proposal_id: str, *, to_state: str, run_ts: str,
    implementation_sha: Optional[str] = None,
) -> bool:
    """Move a proposal to a new state (implemented / deferred / skipped).
    Returns True if a proposal was found + updated."""
    for p in doc.get("proposals", []):
        if p.get("id") == proposal_id:
            p["state"] = to_state
            p[f"{to_state}_at"] = _now_iso()
            p[f"{to_state}_run_ts"] = run_ts
            if implementation_sha:
                p["implementation_sha"] = implementation_sha
            return True
    return False


def open_proposals(doc: dict) -> list[dict]:
    return [p for p in doc.get("proposals", []) if p.get("state") == "open"]


def stats(doc: dict) -> dict:
    by_state: dict[str, int] = {}
    for p in doc.get("proposals", []):
        st = p.get("state", "open")
        by_state[st] = by_state.get(st, 0) + 1
    return by_state


def persist_run(
    *,
    storage,
    agent_id: str,
    run_ts: str,
    new_recs: list[dict],
) -> dict:
    """Convenience: load → merge → save in one call. Returns updated stats."""
    doc = load_active(storage, agent_id)
    merge_run(doc, new_recs=new_recs, run_ts=run_ts)
    save_active(storage, agent_id, doc)
    return stats(doc)


# ─────────────────────────────────────────────────────────────────────────────
# Shipped-lever ledger — what a dispatching producer already shipped
# ─────────────────────────────────────────────────────────────────────────────
#
# The accumulator above remembers what an agent PROPOSED. A producer that
# dispatches its top recs straight to the implementer (the growth
# strategists: up to 3 "levers" every 5 hours) also needs to know what
# SHIPPED, and whether it worked. Measured 2026-11-11 on one site: 19 of 48
# dispatched levers in a week came back from the implementer as
# implemented_via=pre-existing — the strategist had re-proposed something
# already live — and a build-page lever was in 16 of 16 consecutive memos
# while that surface made 4 clicks a week.
#
# The ledger is DERIVED, not stored: the implementer writes lifecycle
# markers (implemented, implemented_via, implemented_at, shipped,
# shipped_at, shipped_tag, commit / implemented_commit, deferred) back into
# the producer's own agents/<id>/runs/<run_ts>/recommendations.json, so the
# last N days of those files ARE the ledger. Nothing new to keep in sync.
#
#   shipped_levers()                 the ledger (state + live_since per lever)
#   human_clicks.lever_outcomes()    attaches clicks before/after live_since
#   lever_verdict()                  too-early / new-surface / lift / no-lift
#   shipped_levers_prompt_block()    "SHIPPED LEVERS (14d): do not re-propose"
#   shipped_levers_section()         that block plus the deploy-gate and
#                                    held-pages lines, for a growth brief
#   match_shipped() / select_levers()
#                                    drop re-proposals, held-page levers and
#                                    operator-only items in code, refill the
#                                    slot with the next dispatchable
#                                    primary-metric rec, keep secondary-metric
#                                    levers to the last slot, prefer DB-only
#                                    levers while the site's deploy gate is red
#   selection_summary()              what select_levers kept and dropped, as
#                                    the dispatch-selection.json artifact
#   ledger_metrics()                 RunResult metrics from the ledger
#   deploy_gate_status()             that gate, read from deploy_repair's state

LEDGER_DAYS = 14
LEVER_OUTCOME_DAYS = 7
# A re-proposal of a lever the implementer DEFERRED is dropped in code only
# this long after the deferral. Deferrals are often conditional ("fix the
# click filter first", "implementer no-op: HEAD unchanged"), so after that
# the prompt line (with the reason) decides, not a 14-day block.
DEFERRED_HOLD_HOURS = 48

SHIPPED_LEVER_RULE = (
    "NEVER propose a lever substantively equivalent to anything under SHIPPED "
    "LEVERS: same change, same pages or placements, new wording included. "
    "`pre-existing` means the implementer found it already live, so proposing "
    "it again was a wasted dispatch. `deferred` means the implementer did not "
    "do it, for the reason shown: re-propose it only once that reason no "
    "longer holds, and say what changed. A lever whose verdict is `no-lift` "
    "after 7 days live did not move verified-human clicks: do not re-propose "
    "it or a variation of it; pick a different surface. `new-surface` means "
    "it added a buy block on pages with nothing to compare against; judge it "
    "by its own clicks. Proposals that match a shipped lever (or one deferred "
    f"in the last {DEFERRED_HOLD_HOURS} hours) are dropped in code and their "
    "slot goes to your next dispatchable Amazon-click recommendation."
)

_RUN_TS_RE = re.compile(r"^\d{8}T\d{6}Z$")

# Lever titles reuse growth vocabulary that says nothing about WHICH change
# it is ("make every ... clicks ... page"). title_dedup's stopwords cover
# SEO filler; these cover lever filler.
LEVER_STOPWORDS = frozenset({
    "click", "clicks", "guide", "guides", "page", "pages", "amazon", "human",
    "verified", "lever", "levers", "site", "every", "make", "get", "turn",
    "into", "before", "after", "plus", "first", "via", "per", "its", "their",
    "them", "they", "now", "more", "less", "our", "real", "live", "fresh",
})

# Occasion vocabulary is not a change word either. During an event most
# levers name it ("... before Cyber Week", "Thanksgiving ..."), so two
# different changes on overlapping pages shared {cyber, week} and the second
# was dropped as a repeat of the first (measured 2026-11-11 on a real ledger,
# in the year's biggest deal window). framework CLAUDE.md: title-similarity
# dedup must exempt holiday-bearing proposals. Occasion NAMES come from the
# seasonal calendar (ids, aliases, labels; see occasion_phrases()) and are
# stripped as whole phrases, so "super bowl" goes while "mixing bowl" stays.
# These generic retail-event words are stripped wherever they appear.
OCCASION_WORDS = frozenset({
    "cyber", "monday", "week", "weekend", "holiday", "holidays", "season",
    "seasonal", "deal", "deals", "sale", "sales", "doorbuster", "doorbusters",
    "gift", "gifts", "gifting", "xmas", "bfcm",
})

# ships_via values: the implementer can apply "db" (a data change, no
# deploy) and "code" (a code change that deploys). Anything a person has to
# do (a credential, a decision, a manual check) is "operator": it stays in
# the memo and is never dispatched. A refill (a rec the memo did not rank as
# a lever) must say db or code.
DISPATCHABLE_VIA = ("db", "code")
NON_DISPATCHABLE_VIA = frozenset({"operator", "manual", "person", "none"})


def run_ts_datetime(run_ts: str) -> Optional[datetime]:
    """'20261111T154200Z' -> aware UTC datetime (None when not a run_ts)."""
    s = str(run_ts or "")
    if not _RUN_TS_RE.match(s):
        return None
    return datetime.strptime(s, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)


def _parse_iso(v: Any) -> Optional[datetime]:
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)


def load_run_recs(storage, agent_id: str, *, days: int = LEDGER_DAYS,
                  now: Optional[datetime] = None) -> list[dict]:
    """Every rec in the agent's own run dirs from the last `days` days
    (agents/<id>/runs/<run_ts>/recommendations.json — the copy the
    implementer writes lifecycle markers into), oldest first. Each returned
    dict is a copy carrying `run_ts`. Unreadable dirs are skipped."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    prefix = f"agents/{agent_id}/runs/"
    try:
        children = storage.list_child_prefixes(prefix) or []
    except Exception:
        return []
    out: list[dict] = []
    for run_ts in sorted(children):
        when = run_ts_datetime(run_ts)
        if when is None or when < cutoff or when > now + timedelta(hours=1):
            continue
        try:
            doc = storage.read_json(f"{prefix}{run_ts}/recommendations.json")
        except Exception:
            continue
        recs = doc.get("recommendations") if isinstance(doc, dict) else doc
        for r in recs if isinstance(recs, list) else []:
            if isinstance(r, dict) and str(r.get("title") or "").strip():
                out.append({**r, "run_ts": run_ts})
    return out


def _commit_of(rec: dict) -> str:
    for k in ("implemented_commit", "commit"):
        if rec.get(k):
            return str(rec[k])[:12]
    multi = rec.get("implemented_commits")
    if isinstance(multi, list) and multi:
        return ",".join(str(c)[:12] for c in multi[:3])
    via = str(rec.get("implemented_via") or "")
    m = re.match(r"^commit\s+([0-9a-f]{7,40})", via)
    return m.group(1)[:12] if m else ""


def lever_status(rec: dict) -> tuple[str, Optional[str]]:
    """(state, live_since ISO or None) for one dispatched rec.

    live          implemented and deployed (live since shipped_at), or a
                  DB-only lever (`ships_via: db`, live since implemented_at)
    pre-existing  the implementer found the change already live: nothing
                  shipped, the proposal itself was a repeat
    implemented   committed, not deployed yet (e.g. the deploy gate is red)
    deferred      the implementer declined it (deferred_reason says why)
    dispatched    no lifecycle marker yet (in flight, or the run failed)
    """
    via = str(rec.get("implemented_via") or "").strip().lower()
    if rec.get("implemented") is True:
        if via.startswith("pre-existing"):
            return "pre-existing", None
        if rec.get("shipped") is True and _parse_iso(rec.get("shipped_at")):
            return "live", _parse_iso(rec.get("shipped_at")).isoformat()
        if str(rec.get("ships_via") or "").strip().lower() == "db" and \
                _parse_iso(rec.get("implemented_at")):
            return "live", _parse_iso(rec.get("implemented_at")).isoformat()
        return "implemented", None
    if rec.get("deferred") is True:
        return "deferred", None
    return "dispatched", None


def _as_list(v: Any) -> list[str]:
    """A rec field as a list of non-empty strings. LLM-written JSON is not
    trusted: a bare string is one item, a scalar (5) becomes ["5"], and a
    mapping or anything else unusable becomes []."""
    if v is None or isinstance(v, (bool, Mapping)):
        return []
    if isinstance(v, (str, int, float)):
        v = [v]
    elif not isinstance(v, (list, tuple, set, frozenset)):
        return []
    return [str(x).strip() for x in v
            if not isinstance(x, (Mapping, list, tuple, set)) and str(x if x is not None else "").strip()]


def _str(v: Any) -> str:
    return "" if v is None or isinstance(v, (Mapping, list, tuple, set)) else str(v).strip()


def shipped_levers(storage, agent_id: str, *, days: int = LEDGER_DAYS,
                   now: Optional[datetime] = None,
                   include_states: tuple[str, ...] = ("live", "pre-existing",
                                                      "implemented", "deferred"),
                   ) -> list[dict]:
    """The do-not-re-propose ledger: this agent's dispatched recs from the
    last `days` days in `include_states`, newest first. Each entry:
    id, title, run_ts, expected_metric, target_urls, placements, ships_via,
    state, live_since, commit, shipped_tag, deferred_reason, deferred_at.
    A rec whose fields cannot be read is skipped, never fatal: one malformed
    rec must not switch the whole ledger (and the repeat filter) off."""
    ledger: list[dict] = []
    for r in load_run_recs(storage, agent_id, days=days, now=now):
        try:
            state, live_since = lever_status(r)
            if state not in include_states:
                continue
            deferred_at = _parse_iso(r.get("deferred_at")) if state == "deferred" else None
            ledger.append({
                "id": _str(r.get("id")),
                "title": _str(r.get("title")),
                "run_ts": _str(r.get("run_ts")),
                "tier": _str(r.get("tier")),
                "expected_metric": _str(r.get("expected_metric")),
                "target_urls": _as_list(r.get("target_urls")),
                "placements": _as_list(r.get("placements")),
                "ships_via": _str(r.get("ships_via")).lower(),
                "state": state,
                "live_since": live_since,
                "commit": _commit_of(r),
                "shipped_tag": _str(r.get("shipped_tag")),
                "deferred_reason": (_str(r.get("deferred_reason"))[:160]
                                    if state == "deferred" else ""),
                "deferred_at": deferred_at.isoformat() if deferred_at else "",
            })
        except Exception:
            continue
    ledger.sort(key=lambda e: (e["run_ts"], e["id"]), reverse=True)
    return ledger


def lever_verdict(outcome: Optional[dict], *, min_days: float = LEVER_OUTCOME_DAYS,
                  min_gain: int = 2) -> str:
    """n/a / unscoped / too-early / new-surface / lift / no-lift for a
    human_clicks.before_after() result.

    lift = at least `min_gain` more clicks on the lever's pages/placements in
    the window after it went live than in the window before, AND faster
    growth than the site's own over the same two windows (a lever does not
    get credit for a site-wide surge; with no clicks before, the comparison
    is add-one smoothed). unscoped = no pages or placements recorded.

    A placement-scoped lever with no clicks on its placements before (it
    ADDED the buy block, or the placement column did not exist yet) is
    judged on its pages' clicks across all placements (`pages_before` /
    `pages_after`): those clicks may only have moved off another buy block on
    the same pages. With no pages recorded either there is nothing to compare
    against: new-surface."""
    if not outcome:
        return "n/a"
    if not outcome.get("scoped"):
        return "unscoped"
    if float(outcome.get("after_days") or 0) < float(min_days):
        return "too-early"
    b, a = int(outcome.get("before") or 0), int(outcome.get("after") or 0)
    sb, sa = int(outcome.get("site_before") or 0), int(outcome.get("site_after") or 0)
    if a - b < min_gain:
        return "no-lift"
    if b == 0 and outcome.get("pages_before") is not None:
        if not outcome.get("pages_scoped"):
            return "new-surface"
        b, a = int(outcome.get("pages_before") or 0), int(outcome.get("pages_after") or 0)
        if a - b < min_gain:
            return "no-lift"
    if sb:
        if b and (a / b) <= (sa / sb):
            return "no-lift"
        if not b and (a + 1) / (b + 1) <= (sa + 1) / (sb + 1):
            return "no-lift"
    return "lift"


def _fmt_ts(iso: Optional[str]) -> str:
    d = _parse_iso(iso)
    return d.strftime("%Y-%m-%d %H:%MZ") if d else "?"


def shipped_levers_prompt_block(ledger: list[dict], *, days: int = LEDGER_DAYS,
                                max_entries: int = 40,
                                now: Optional[datetime] = None) -> str:
    """The "SHIPPED LEVERS (14d): do not re-propose" brief section.

    Each line: state, id, title, pages/placements, and — for live levers
    with an `outcome` (human_clicks.lever_outcomes) — clicks on its pages /
    placements in the 7 days before vs since it went live, the site's own
    counts for the same windows, and the verdict."""
    now = now or datetime.now(timezone.utc)
    head = (f"SHIPPED LEVERS (last {days} days, from this agent's own dispatched "
            f"recommendations.json; newest first): {SHIPPED_LEVER_RULE}")
    if not ledger:
        return head + "\n  (none in the window)"
    lines = [head]
    counts: dict[str, int] = {}
    no_lift = 0
    for e in ledger[:max(0, int(max_entries))]:
        state = e.get("state") or "?"
        counts[state] = counts.get(state, 0) + 1
        if state == "live":
            age = ""
            d = _parse_iso(e.get("live_since"))
            if d:
                age = f", {max(0.0, (now - d).total_seconds() / 86400):.1f}d live"
            label = f"live since {_fmt_ts(e.get('live_since'))}{age}"
        elif state == "deferred":
            label = f"deferred: {e.get('deferred_reason') or 'no reason given'}"
        elif state == "implemented":
            label = "implemented, not deployed yet"
        else:
            label = state
        bits = [f"  [{label}] {e.get('id', '')} \"{(e.get('title') or '')[:150]}\""]
        if e.get("expected_metric"):
            bits.append(f"metric={e['expected_metric']}")
        if e.get("target_urls"):
            pages = e["target_urls"]
            bits.append("pages=" + ",".join(pages[:4]) + (f" (+{len(pages) - 4})" if len(pages) > 4 else ""))
        if e.get("placements"):
            bits.append("placements=" + ",".join(e["placements"][:6]))
        o = e.get("outcome")
        if o:
            verdict = lever_verdict(o)
            if verdict == "no-lift":
                no_lift += 1
            if o.get("scoped"):
                pages = ""
                if not o.get("before") and o.get("pages_before") is not None and o.get("pages_scoped"):
                    pages = (f" (none on these placements before: judged on its pages, all "
                             f"placements: before={o.get('pages_before')} after={o.get('pages_after')})")
                bits.append(f"clicks {o.get('days', LEVER_OUTCOME_DAYS)}d before={o.get('before')}"
                            f" after({o.get('after_days')}d)={o.get('after')}{pages}"
                            f" | site before={o.get('site_before')}"
                            f" after={o.get('site_after')} | verdict={verdict}")
            else:
                bits.append("verdict=unscoped (no pages/placements recorded)")
        lines.append(" ".join(bits))
    summary = ", ".join(f"{n} {s}" for s, n in sorted(counts.items(), key=lambda kv: -kv[1]))
    lines.append(f"  Summary: {summary}" + (f"; {no_lift} no-lift after 7d live" if no_lift else ""))
    return "\n".join(lines)


# ── matching + dispatch selection ───────────────────────────────────────────

def _words(text: Any) -> str:
    """Lowercase words joined by single spaces, padded (" black friday ").
    Apostrophes are dropped first so "Mother's Day" reads "mothers day"."""
    s = str(text if text is not None else "").lower()
    s = s.replace("'", "").replace("’", "")
    return " " + " ".join(re.findall(r"[a-z0-9]+", s)) + " "


@functools.lru_cache(maxsize=1)
def occasion_phrases() -> tuple[str, ...]:
    """Occasion names from framework seasonal_calendar, as lowercase phrases,
    longest first: every occasion's id ("black-friday" -> "black friday"),
    aliases (when the calendar has them) and label parts ("Black Friday /
    Cyber Monday (tech deals)" -> "black friday", "cyber monday"). The
    built-in calendar; a deployment's storage additions are not read here."""
    try:
        from framework.core import seasonal_calendar
        occasions = seasonal_calendar.load_occasions()
    except Exception:
        occasions = ()
    phrases: set[str] = set()
    for o in occasions:
        names = [getattr(o, "id", "") or ""]
        names += list(getattr(o, "aliases", ()) or ())
        label = re.sub(r"\([^)]*\)", " ", str(getattr(o, "label", "") or ""))
        names += label.split("/")
        for n in names:
            p = _words(str(n).replace("-", " ")).strip()
            if p:
                phrases.add(p)
    return tuple(sorted(phrases, key=lambda p: (-len(p), p)))


def strip_occasions(text: Any) -> str:
    """`text` as padded lowercase words with every calendar occasion name
    (occasion_phrases) cut out; OCCASION_WORDS are left to the caller."""
    s = _words(text)
    for p in occasion_phrases():
        needle = f" {p} "
        while needle in s:
            s = s.replace(needle, " ")
    return s


@functools.lru_cache(maxsize=8192)
def _lever_tokens(title: str) -> frozenset:
    from framework.core.title_dedup import title_signature
    return frozenset(t for t in title_signature(strip_occasions(title))
                     if t not in LEVER_STOPWORDS and t not in OCCASION_WORDS)


def lever_tokens(title: Any) -> frozenset:
    """Meaningful title tokens: title_dedup's signature minus lever filler
    (LEVER_STOPWORDS) and occasion vocabulary (calendar occasion names as
    phrases, plus OCCASION_WORDS), so "Cyber Week" or "Thanksgiving" in two
    titles never makes them the same change."""
    return _lever_tokens(_str(title))


def _norm_targets(values: Any) -> list[str]:
    out = []
    for v in _as_list(values):
        s = re.sub(r"^[a-z][a-z0-9+.-]*://[^/]*", "", v, flags=re.I)
        s = s.split("?", 1)[0].split("#", 1)[0]
        if not s.endswith("*"):
            s = s.rstrip("/") or "/"
        if s:
            out.append(s.lower())
    return out


def _target_hit(a: str, b: str) -> bool:
    if a.endswith("*") and b.endswith("*"):
        return a[:-1].startswith(b[:-1]) or b[:-1].startswith(a[:-1])
    if a.endswith("*"):
        return b.startswith(a[:-1])
    if b.endswith("*"):
        return a.startswith(b[:-1])
    return a == b


def page_overlap(a: Any, b: Any) -> float:
    """Overlap coefficient of two target_urls lists (prefix* entries match
    anything under them): shared / size of the smaller list."""
    pa, pb = _norm_targets(a), _norm_targets(b)
    if not pa or not pb:
        return 0.0
    small, big = (pa, pb) if len(pa) <= len(pb) else (pb, pa)
    hits = sum(1 for x in small if any(_target_hit(x, y) for y in big))
    return hits / len(small)


def _slug_tokens(targets: Any) -> set[str]:
    """Words that name the target pages themselves (from their paths)."""
    toks: set[str] = set()
    for t in _norm_targets(targets):
        toks.update(w for w in re.findall(r"[a-z0-9]+", t) if len(w) >= 3 and not w.isdigit())
    return toks


def match_shipped(rec: dict, ledger: list[dict], *, title_threshold: float = 0.5,
                  min_shared: int = 3, page_threshold: float = 0.5,
                  page_min_shared: int = 2) -> Optional[dict]:
    """The ledger entry `rec` repeats, or None. Returns a copy of the entry
    with `match_reason` / `match_score`.

    A repeat is (a) a similar title — at least `min_shared` meaningful tokens
    covering `title_threshold` of the shorter title (title_dedup's
    containment rule, minus lever filler words), or (b) the same pages
    (target_urls overlap >= `page_threshold`) with at least
    `page_min_shared` title tokens in common that are not just the pages'
    own slug words ("black friday" on the Black Friday page says nothing
    about WHICH change). Two different changes to one busy surface share
    pages but not change words, so they do not match. Occasion vocabulary
    never counts as a change word (lever_tokens): two different Cyber Week
    levers on overlapping guides are two levers.

    Calibrated 2026-11-11 on two growth strategists' dispatched levers since
    11-04 (78 levers): of the 23 the implementer returned as pre-existing
    while an earlier lever was in the window, it flags 21 (19/19 on one
    site, 2/4 on the other); the other 25 matches are the same few themes
    re-worded run after run (one product surface, one build-page ladder,
    one homepage module). Dropping occasion words cost no pre-existing catch
    and removed two matches: a different Cyber Week lever on overlapping
    guides, and a build-ladder follow-up the implementer shipped as new work."""
    from framework.core.title_dedup import is_similar_title
    if not isinstance(rec, Mapping):
        return None
    ta = lever_tokens(rec.get("title"))
    if not ta:
        return None
    best: Optional[dict] = None
    for e in ledger or []:
        if not isinstance(e, Mapping):
            continue
        tb = lever_tokens(e.get("title"))
        if not tb:
            continue
        shared = len(ta & tb)
        cont = shared / min(len(ta), len(tb))
        reason, score = "", 0.0
        if is_similar_title(ta, tb, threshold=title_threshold, min_shared=min_shared):
            reason, score = "title", cont
        else:
            ov = page_overlap(rec.get("target_urls"), e.get("target_urls"))
            if ov >= page_threshold:
                slugs = _slug_tokens(rec.get("target_urls")) | _slug_tokens(e.get("target_urls"))
                if len((ta - slugs) & (tb - slugs)) >= page_min_shared:
                    reason, score = "pages", ov * 0.5 + cont * 0.5
        if reason and (best is None or score > best["match_score"]):
            best = {**e, "match_reason": reason, "match_score": round(score, 2)}
    return best


def _deferral_is_recent(entry: Mapping, now: datetime, hours: float) -> bool:
    when = _parse_iso(entry.get("deferred_at")) or run_ts_datetime(_str(entry.get("run_ts")))
    return when is None or (now - when) < timedelta(hours=float(hours))


def blocking_levers(ledger: Optional[list[dict]], *, now: Optional[datetime] = None,
                    deferred_hold_hours: float = DEFERRED_HOLD_HOURS) -> list[dict]:
    """The ledger entries a new rec may not repeat: every live, pre-existing
    or implemented lever, and a deferred one only within
    `deferred_hold_hours` of its deferral (deferred_at, else its run_ts)."""
    now = now or datetime.now(timezone.utc)
    return [e for e in ledger or [] if isinstance(e, Mapping)
            and (e.get("state") != "deferred"
                 or _deferral_is_recent(e, now, deferred_hold_hours))]


def ships_via(rec: Mapping) -> str:
    """The rec's normalized ships_via ("db", "code", "operator", ... or "")."""
    return _str(rec.get("ships_via")).lower() if isinstance(rec, Mapping) else ""


def select_levers(recs: list[dict], ledger: Optional[list[dict]] = None, *,
                  max_n: int = 3, tiers: Iterable[str] = ("lever",),
                  refill_tiers: Iterable[str] = ("smaller",),
                  primary_metrics: Iterable[str] = (),
                  max_secondary: int = 1,
                  prefer_db: bool = False, max_code: int = 1,
                  holds: Iterable[Any] = (),
                  deferred_hold_hours: float = DEFERRED_HOLD_HOURS,
                  now: Optional[datetime] = None,
                  ) -> tuple[list[dict], list[dict]]:
    """Choose which recs a dispatching producer hands to the implementer.

    1. Candidates are the recs whose tier is in `tiers`, in memo order.
    2. A candidate is dropped, and its slot goes to the next refill, when
       - its ships_via is operator-only (NON_DISPATCHABLE_VIA: a person has
         to do it, so the implementer can only no-op);
       - it touches a held page (`holds`: framework page_hold PageHolds;
         page_hold.rec_holds, prefix* targets included);
       - it repeats a shipped lever (match_shipped against
         blocking_levers(): deferred levers block for `deferred_hold_hours`).
       A refill is the next rec from `refill_tiers` whose expected_metric is
       primary, whose ships_via is db or code (DISPATCHABLE_VIA: a smaller
       move the memo did not mark implementable is never promoted), and
       which touches no held page and repeats nothing.
    3. Primary-metric candidates (e.g. verified-human Amazon clicks) come
       first; at most `max_secondary` others (e.g. AI-referral levers) are
       kept, always after them: "AI referrals in the third slot only".
    4. prefer_db (the site's deploy gate is red, so code reaches no user):
       DB-only recs (`ships_via: db`) come first, at most `max_code` code
       recs are kept, and each held code rec's slot goes to the next DB-only
       primary refill when there is one.
    5. At most `max_n` are selected.

    Returns (selected, dropped); every dropped item is {"rec", "reason"},
    plus "matched" = {id, title, state, match_reason, match_score} for a
    repeat and "held" = [held paths] for a page hold."""
    tiers = {str(t).lower() for t in tiers}
    refill_tiers = {str(t).lower() for t in refill_tiers}
    primary = {str(m) for m in primary_metrics}
    blocking = blocking_levers(ledger, now=now, deferred_hold_hours=deferred_hold_hours)
    holds = list(holds or [])
    dropped: list[dict] = []
    used: set[int] = set()

    def _tier(r: dict) -> str:
        return _str(r.get("tier")).lower()

    def _is_primary(r: dict) -> bool:
        return not primary or _str(r.get("expected_metric")) in primary

    def _is_db(r: dict) -> bool:
        return ships_via(r) == "db"

    def _held(r: dict) -> list:
        if not holds:
            return []
        try:
            from framework.core import page_hold
            # hold_override is a person releasing one rec; a rec the LLM just
            # wrote cannot release itself.
            return page_hold.rec_holds({k: v for k, v in r.items() if k != "hold_override"}, holds)
        except Exception:
            return []

    def _skip(r: dict) -> Optional[dict]:
        """Why a candidate cannot be dispatched, as a dropped item, or None."""
        via = ships_via(r)
        if via in NON_DISPATCHABLE_VIA:
            return {"rec": r, "reason": f"not dispatchable (ships_via={via}): a person has to do it"}
        hit = _held(r)
        if hit:
            from framework.core import page_hold
            return {"rec": r, "reason": page_hold.deferral_reason(hit),
                    "held": [h.path for h in hit]}
        m = match_shipped(r, blocking)
        if m:
            return {"rec": r, "reason": "repeats a shipped lever",
                    "matched": {k: m.get(k) for k in ("id", "title", "state",
                                                       "match_reason", "match_score")}}
        return None

    pool = [r for r in recs or [] if isinstance(r, dict)]

    def _refill(want_db: bool = False) -> Optional[dict]:
        for i, r in enumerate(pool):
            if i in used or _tier(r) not in refill_tiers or not _is_primary(r):
                continue
            if ships_via(r) not in DISPATCHABLE_VIA or (want_db and not _is_db(r)):
                continue
            if _skip(r):
                continue
            used.add(i)
            return r
        return None

    chosen: list[dict] = []
    n_refill = 0
    for i, r in enumerate(pool):
        if _tier(r) not in tiers or i in used:
            continue
        used.add(i)
        why = _skip(r)
        if why:
            dropped.append(why)
            n_refill += 1
            continue
        chosen.append(r)
    # Refills rank after the surviving candidates: the memo's own order is kept.
    for _ in range(n_refill):
        sub = _refill()
        if sub is None:
            break
        chosen.append(sub)

    firsts = [r for r in chosen if _is_primary(r)]
    others = [r for r in chosen if not _is_primary(r)]
    keep_others = others[:max(0, int(max_secondary))]
    for r in others[len(keep_others):]:
        dropped.append({"rec": r, "reason": f"secondary metric: at most {max_secondary} "
                                            f"per run, in the last slot"})
    ordered = firsts + keep_others

    if prefer_db:
        dbs = [r for r in ordered if _is_db(r)]
        codes = [r for r in ordered if not _is_db(r)]
        kept_codes = codes[:max(0, int(max_code))]
        for r in codes[len(kept_codes):]:
            sub = _refill(want_db=True)
            dropped.append({"rec": r, "reason": "deploy gate red: code lever held"
                                                + ("; slot went to a DB-only rec" if sub else "")})
            if sub is not None:
                dbs.append(sub)
        # keep the secondary-last rule inside each group
        ordered = ([r for r in dbs if _is_primary(r)] + [r for r in kept_codes if _is_primary(r)]
                   + [r for r in dbs + kept_codes if not _is_primary(r)])

    selected = ordered[:max(0, int(max_n))]
    for r in ordered[len(selected):]:
        dropped.append({"rec": r, "reason": f"over the per-run cap ({max_n})"})
    return selected, dropped


_SELECTION_KEYS = ("title", "tier", "expected_metric", "ships_via", "target_urls", "placements")


def selection_summary(selected: list[dict], dropped: list[dict],
                      gate: Optional[Mapping] = None) -> dict:
    """select_levers()' result as a JSON-able record (a producer's
    dispatch-selection.json): what went out, what was dropped and why."""
    def _rec(d: Mapping) -> Mapping:
        r = d.get("rec") if isinstance(d, Mapping) else None
        return r if isinstance(r, Mapping) else {}
    return {
        "selected": [{k: r.get(k) for k in _SELECTION_KEYS}
                     for r in selected or [] if isinstance(r, Mapping)],
        "dropped": [{"title": _rec(d).get("title"), "tier": _rec(d).get("tier"),
                     "reason": d.get("reason"), "matched": d.get("matched"),
                     "held": d.get("held")}
                    for d in dropped or [] if isinstance(d, Mapping)],
        "repeats_dropped": sum(1 for d in dropped or [] if isinstance(d, Mapping) and d.get("matched")),
        "held_dropped": sum(1 for d in dropped or [] if isinstance(d, Mapping) and d.get("held")),
        "deploy_gate": (gate or {}).get("status"),
    }


def ledger_metrics(ledger: Optional[list[dict]]) -> dict:
    """RunResult metrics from a shipped-lever ledger (after
    human_clicks.lever_outcomes attached outcomes):
      pre_existing_lever_share_14d  pre-existing / (live + implemented +
                                    pre-existing): how often a dispatched
                                    lever was already live
      shipped_levers_lift_14d / shipped_levers_no_lift_14d
                                    live levers judged lift / no-lift
                                    (lever_verdict) after 7 days
    Keys with no data are left out."""
    entries = [e for e in ledger or [] if isinstance(e, Mapping)]
    states = [_str(e.get("state")) for e in entries]
    done = [s for s in states if s in ("live", "implemented", "pre-existing")]
    out: dict = {}
    if done:
        out["pre_existing_lever_share_14d"] = round(states.count("pre-existing") / len(done), 3)
    judged = [lever_verdict(e.get("outcome")) for e in entries
              if e.get("state") == "live" and isinstance(e.get("outcome"), Mapping)]
    judged = [v for v in judged if v in ("lift", "no-lift")]
    if judged:
        out["shipped_levers_no_lift_14d"] = float(judged.count("no-lift"))
        out["shipped_levers_lift_14d"] = float(judged.count("lift"))
    return out


def deploy_gate_status(site: str) -> dict:
    """The site's deploy gate as the deployer last recorded it
    (framework.core.deploy_repair gate state): {site, status, red_since,
    hours_red, last_reason, last_deployed_tag}. status 'unknown' when there
    is no state. Read-only."""
    out = {"site": site, "status": "unknown", "red_since": "", "hours_red": None,
           "last_reason": "", "last_deployed_tag": ""}
    if not site:
        return out
    try:
        from framework.core.deploy_repair import GateState
        st = GateState.load(site)
    except Exception as e:
        out["error"] = str(e)[:200]
        return out
    out.update({"status": st.status or "unknown", "red_since": st.red_since or "",
                "last_reason": (st.last_reason or "")[:200],
                "last_deployed_tag": st.last_deployed_tag or ""})
    since = _parse_iso(st.red_since) if st.status == "red" else None
    if since:
        out["hours_red"] = round((datetime.now(timezone.utc) - since).total_seconds() / 3600, 1)
    return out


def deploy_gate_prompt_line(gate: dict, *, max_code: int = 1) -> str:
    """One brief line on the deploy gate; the red case tells the strategist
    to prefer DB-only levers (and why). Prices are left out of the DB-only
    examples on purpose: a price written by hand gets a fresh timestamp and
    would pass the freshness and display gates without being verified."""
    gate = gate or {}
    status = gate.get("status") or "unknown"
    site = gate.get("site") or "site"
    if status == "red":
        hours = gate.get("hours_red")
        n = max(0, int(max_code))
        cap = {0: "No code lever is", 1: "At most one code lever is"}.get(
            n, f"At most {n} code levers are")
        return (f"DEPLOY GATE: RED for {site} since {gate.get('red_since') or '?'} "
                f"({f'{hours}h; ' if hours is not None else ''}"
                f"{gate.get('last_reason') or 'tests failing'}; "
                f"production is still on {gate.get('last_deployed_tag') or 'an older tag'}). "
                "Code changes queue behind the gate and reach no visitor until it is "
                "green. Prefer DB-ONLY levers (content rows, article bodies, pick lists, "
                "featured flags: data the implementer applies straight to production) "
                "and mark them \"ships_via\": \"db\". Never write a price by hand: prices "
                "change only through the site's price-refresh pipeline (ask for a refresh). "
                f"{cap} dispatched while the gate is red.")
    if status == "green":
        return (f"DEPLOY GATE: green for {site} (last deployed "
                f"{gate.get('last_deployed_tag') or '?'}); code levers ship on the next deploy.")
    return f"DEPLOY GATE: status unknown for {site}."


def shipped_levers_section(ledger: Optional[list[dict]], gate: Optional[Mapping] = None,
                           holds: Iterable[Any] = (), *, max_chars: Optional[int] = None,
                           max_code: int = 1, days: int = LEDGER_DAYS,
                           now: Optional[datetime] = None) -> str:
    """The body of a growth brief's "SHIPPED LEVERS" section: the deploy-gate
    line, the held-pages line (framework page_hold.holds_prompt_line, when a
    page is held) and shipped_levers_prompt_block (cut at `max_chars`)."""
    parts = [deploy_gate_prompt_line(dict(gate or {}), max_code=max_code)]
    holds = list(holds or [])
    if holds:
        try:
            from framework.core import page_hold
            line = page_hold.holds_prompt_line(holds)
        except Exception:
            line = ""
        if line:
            parts.append(line)
    block = shipped_levers_prompt_block(list(ledger or []), days=days, now=now)
    parts.append(block[:max_chars] if max_chars else block)
    return "\n\n".join(parts)
