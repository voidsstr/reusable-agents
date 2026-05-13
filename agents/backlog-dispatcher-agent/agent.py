"""backlog-dispatcher-agent — decouples the implementer from producer agents.

Problem this solves
-------------------
The implementer is event-driven: it only ships work when a producer
agent (PI, SEO, comp-research, catalog-audit, …) dispatches a batch
through the responder's auto-queue. When producer agents are paused
(e.g. during quota crunches, retros, ops freezes) the 700+ open recs
in the accumulators sit untouched — the implementer is healthy but has
no source of work.

This agent unsticks that loop by acting as an INDEPENDENT producer
itself: every cron tick it walks each producer's accumulator + recent
runs and queues any open rec that hasn't already been dispatched.

What it does
------------
1. List every producer agent that has an accumulator.json.
2. For each producer, load the accumulator's open proposals.
3. Find the most recent runs/<run_ts>/recommendations.json that
   contains those rec_ids (the implementer needs the FULL rec body,
   which the accumulator doesn't store).
4. Per (producer, run_ts) group, call queue_recs() with the rec_ids.
5. The responder consumes the auto-queue on its next tick and
   dispatches batches to the implementer.

State
-----
Persists `last_queued_ids` per producer in self.state so the same rec
isn't queued twice. The accumulator's `state` field (open / implemented
/ skipped) is the source of truth — once the implementer ships a rec,
the producer agent's next run updates accumulator state to implemented
and we skip it.

When to disable
---------------
- Producers are running normally — they queue their own work, this
  agent re-queues the same recs and the responder dedupes. Wasteful
  but not harmful (deduplication is by request_id, which is stable
  per run_ts + subject_tag + site).
- Implementer is paused — no point queueing if nothing ships.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from framework.core.agent_base import AgentBase, RunResult  # noqa: E402
from framework.core.implementation_queue import queue_recs  # noqa: E402


# Per-tick safety caps. The implementer can only ship so much per hour,
# and the responder dispatches one batch per producer per tick. Queueing
# 700 recs at once just clogs the responder's auto-queue.
# 2026-05-11: bumped from 5/4 → 10/10 after observing the original
# throttle was too conservative for a 700-rec backlog. The implementer
# site-lock serializes per-site anyway so we can't actually run 10
# dispatches in parallel — but having 10 in the queue means responder
# doesn't idle waiting for the next tick when one finishes early.
MAX_RECS_PER_PRODUCER_PER_TICK = int(os.environ.get("BACKLOG_DISPATCHER_MAX_PER_PRODUCER", "10"))
MAX_PRODUCERS_PER_TICK = int(os.environ.get("BACKLOG_DISPATCHER_MAX_PRODUCERS", "10"))
# Hard cap on the auto-queue depth — once we've queued this many batches
# that the responder hasn't drained yet, STOP queuing more. Without this,
# the dispatcher would balloon the queue to 150+ batches while the
# implementer ships at ~5-10min per scope (queue counter doesn't block
# work, but it confuses the operator). 2026-05-12: prior to this cap,
# the queue grew from 6 → 156 in 30 min while the implementer drained
# at 1-2/min.
#
# Set MAX_INFLIGHT_SCOPES separately — the *true* throttle. We don't
# want more than N implementer scopes running concurrently anyway, so
# we stop queuing once the existing queue is enough to keep them busy.
MAX_QUEUE_DEPTH = int(os.environ.get("BACKLOG_DISPATCHER_MAX_QUEUE_DEPTH", "30"))
MAX_INFLIGHT_SCOPES = int(os.environ.get("BACKLOG_DISPATCHER_MAX_INFLIGHT", "2"))


# Producer agents we walk. Anything not in this list won't be touched
# even if it has an accumulator. Keeps blast radius small.
PRODUCER_AGENT_IDS = (
    "aisleprompt-progressive-improvement-agent",
    "specpicks-progressive-improvement-agent",
    "aisleprompt-seo-opportunity-agent",
    "specpicks-seo-opportunity-agent",
    "aisleprompt-competitor-research-agent",
    "specpicks-competitor-research-agent",
    "aisleprompt-catalog-audit-agent",
    "specpicks-catalog-audit-agent",
    "aisleprompt-article-author-agent",
    "specpicks-article-author-agent",
)


def _site_from_agent_id(aid: str) -> str:
    """Per-site agents are prefixed (aisleprompt-…, specpicks-…)."""
    for prefix in ("aisleprompt", "specpicks"):
        if aid.startswith(prefix + "-"):
            return prefix
    return ""


def _list_inflight_scopes() -> list[str]:
    """List the currently-running `agent-dispatch-implementer-*.scope` unit
    names. Source of truth: systemd --user.

    Returns [] if systemctl is unreachable (e.g. running in a container
    that doesn't share the host's systemd socket — the throttle becomes
    a no-op rather than a hard-fail).
    """
    import subprocess
    try:
        out = subprocess.run(
            ["systemctl", "--user", "list-units",
             "--no-pager", "--no-legend",
             "--state=running", "--type=scope"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if out.returncode != 0:
            return []
        scopes = []
        for line in out.stdout.splitlines():
            if "agent-dispatch-implementer-" in line:
                # Unit name is the first whitespace-delimited token. systemctl
                # sometimes prefixes a status-indicator glyph — strip it.
                token = line.lstrip("● ").split()[0]
                scopes.append(token)
        return scopes
    except Exception:
        return []


def _count_inflight_scopes() -> int:
    """Count currently-running implementer scopes. Wrapper for the rest of
    the dispatcher logic; the list form is used to write the heartbeat
    blob."""
    return len(_list_inflight_scopes())


def _write_live_scopes_heartbeat(storage, scopes: list[str]) -> None:
    """Publish the live in-flight scope list to blob so the Azure-hosted
    dashboard can show it. The dispatch-batches.json files in blob can't
    serve this purpose — the implementer only updates a LOCAL copy in its
    tempdir, never round-tripping the status back to blob. This heartbeat
    closes that gap.

    Written every tick (~60s). Stale heartbeats self-cure: the next tick
    overwrites with the current truth.
    """
    from datetime import datetime, timezone
    try:
        storage.write_json(
            "agents/backlog-dispatcher-agent/state/live-scopes.json",
            {
                "schema_version": "1",
                "count": len(scopes),
                "scopes": scopes,
                "updated_at": datetime.now(timezone.utc).isoformat(
                    timespec="seconds"),
            },
        )
    except Exception:
        pass


def _producer_allows_auto_implement(storage, aid: str) -> bool:
    """Read the producer's site.yaml `auto_implement` flag.

    Two sources, in order of preference:
      1. Storage at `agents/<aid>/config/site.yaml` (when present)
      2. On-disk site.yaml in the producer's home repo

    Returns True if the flag is true OR missing (default). Returns
    False ONLY when the flag is explicitly false. Errors → True
    (fail-open: don't block work just because we couldn't read config).
    """
    import os, yaml as _yaml  # type: ignore
    # 1) Storage location used by AgentBase loaders
    try:
        cfg = storage.read_text(f"agents/{aid}/config/site.yaml")
        if cfg:
            data = _yaml.safe_load(cfg) or {}
            if isinstance(data, dict) and "auto_implement" in data:
                return bool(data.get("auto_implement"))
    except Exception:
        pass
    # 2) On-disk fallback — per-site agents live under
    #    <site-repo>/agents/<id-without-site-prefix>/site.yaml
    for site in ("aisleprompt", "specpicks"):
        if aid.startswith(site + "-"):
            local_id = aid[len(site) + 1:]
            for cand in (
                f"/home/voidsstr/development/{site}/agents/{local_id}/site.yaml",
                f"/home/voidsstr/development/{site}/agents/{aid}/site.yaml",
            ):
                if os.path.isfile(cand):
                    try:
                        data = _yaml.safe_load(open(cand)) or {}
                        if isinstance(data, dict) and "auto_implement" in data:
                            return bool(data.get("auto_implement"))
                    except Exception:
                        return True  # fail-open
                    return True
    return True  # default: auto-implement permitted


def _subject_tag_from_agent_id(aid: str) -> str:
    """Best-effort subject_tag (controls which implementer runbook fires).
    The implementation_queue helper has its own heuristic but we set this
    explicitly so it's auditable."""
    if "seo-opportunity" in aid: return "seo"
    if "progressive-improvement" in aid: return "pi"
    if "article-author" in aid: return "article"
    if "competitor-research" in aid: return "cr"
    if "catalog-audit" in aid: return "catalog-audit"
    if "head-to-head" in aid: return "h2h"
    return "work"


class BacklogDispatcher(AgentBase):
    agent_id = "backlog-dispatcher-agent"
    category = "ops"

    # No signals() override. The whole point of this agent is to KEEP
    # firing while the accumulator backlog has open recs — even if the
    # producer count hasn't changed since last tick (because the
    # implementer hasn't finished shipping the prior batch yet, so the
    # producer's state transitions to `implemented` haven't happened
    # yet either). Original signals() did a `(producer, total_open)`
    # hash which froze the dispatcher in place: when we queued 20 recs
    # but the implementer hadn't shipped them yet AND the producer
    # hadn't re-run to mark them implemented, total_open stayed
    # constant → hash matched → run() skipped → backlog stagnant.
    #
    # Run() itself dedupes via state['queued_ids'] so we never re-queue
    # the same rec twice. Skipping the short-circuit just lets every
    # 20-min tick top up the responder's auto-queue with NEW recs.

    def run(self) -> RunResult:
        """Walk producer run-dirs directly for unshipped recs.

        Why not the accumulator? Producers like PI / article-author don't
        call rec_memory.merge_run(), so the accumulator is frozen at the
        last seed/import. Title-matching against recent run-dirs fails
        because producers generate FRESH titles each run that don't
        overlap with stale accumulator entries — left 650 SEED-suffixed
        opens orphaned on 2026-05-12 even though 160+ real run-dirs full
        of real recs were sitting on disk waiting to ship.

        The new strategy: the run-dir IS the source of truth. Walk back
        through recommendations.json, skip recs marked `implemented` or
        `shipped`, skip recs whose rec-key is in our `queued_ids` cache,
        queue the rest. The accumulator is no longer load-bearing.

        Dedup key: `<run_ts>:<rec_id>` (was `accumulator.id` before).
        This means restarting the dispatcher with a stale `queued_ids`
        list won't re-queue work that's already in flight — see the
        post-success cleanup in the success branch below.
        """
        s = self.storage

        # ── Concurrency throttle (Phase 2 — 2026-05-12) ──────────────
        # Single check: in-flight implementer scope count. Without a
        # queue middleman, this IS the system's concurrency control.
        # If MAX_INFLIGHT_SCOPES dispatches are running, skip the tick.
        # The next tick (60s later) will re-check.
        inflight_scopes = _list_inflight_scopes()
        inflight = len(inflight_scopes)
        # Publish a heartbeat blob so the Azure-hosted dashboard can show
        # the live in-flight count. Must run every tick (even on throttle)
        # so a freshly-completed scope clears within ~60s.
        _write_live_scopes_heartbeat(s, inflight_scopes)
        if inflight >= MAX_INFLIGHT_SCOPES:
            self.decide(
                "observation",
                f"backpressure: {inflight} implementer scope(s) running "
                f"(cap={MAX_INFLIGHT_SCOPES}) — skipping tick",
            )
            return RunResult(
                status="success",
                summary=f"throttled: {inflight} scope(s) in flight ≥ cap {MAX_INFLIGHT_SCOPES}",
                metrics={"producers_dispatched": 0, "recs_dispatched": 0,
                          "inflight": inflight, "throttled": True},
            )

        already = set((self.state or {}).get("queued_ids", []))
        queued_now: list[str] = []
        per_producer_actions: list[dict] = []
        producers_touched = 0

        for aid in PRODUCER_AGENT_IDS:
            if producers_touched >= MAX_PRODUCERS_PER_TICK:
                break

            # Re-check inflight INSIDE the loop. dispatch_now() spawns
            # a scope immediately, so each iteration can push us over
            # the cap. Without this re-check, 8 producers all dispatch
            # in one tick and we end up with 5+ scopes against a cap
            # of 3.
            inflight_now = _count_inflight_scopes()
            if inflight_now >= MAX_INFLIGHT_SCOPES:
                self.decide(
                    "observation",
                    f"mid-loop throttle: {inflight_now} scope(s) running "
                    f"≥ cap {MAX_INFLIGHT_SCOPES} — stopping at this point",
                )
                break

            # ── Honor the producer's `auto_implement` gate ────────────
            # If the producer's site.yaml has `auto_implement: false`,
            # its recs are routed to email-approval — the operator
            # replies with `implement rec-NNN`, the responder picks it
            # up and dispatches. The backlog-dispatcher must NOT bypass
            # this: queueing the rec here would skip the email gate.
            # Default to True if no config / unable to load (preserves
            # pre-2026-05-12 behavior for producers that don't carry
            # this flag).
            if not _producer_allows_auto_implement(s, aid):
                continue

            # List ALL run-dirs (not just last 10). With site-PI having
            # 160 run-dirs each at 15 recs/run = 2,400 potential recs,
            # we still cap dispatch volume below.
            try:
                run_keys = sorted(
                    [k for k in (s.list_prefix(f"agents/{aid}/runs/") or [])
                     if k.endswith("/recommendations.json")],
                    reverse=True,
                )
            except Exception as e:
                self.decide("error", f"list runs failed for {aid}: {e}")
                continue
            if not run_keys:
                continue

            # Pick from a SINGLE run-dir per tick (responder needs one
            # source run_ts per queue entry — see implementer's run.sh
            # which materializes a single rundir from blob storage).
            # Walk newest-first; first run-dir with unshipped/unqueued
            # recs wins.
            picked_run: str | None = None
            picked_rec_ids: list[str] = []
            picked_dedup_keys: list[str] = []

            for run_key in run_keys[:50]:  # scan budget — was 10
                doc = s.read_json(run_key) or {}
                recs = doc if isinstance(doc, list) else (doc.get("recommendations") or [])
                if not isinstance(recs, list) or not recs:
                    continue
                run_ts = run_key.rsplit("/runs/", 1)[-1].split("/", 1)[0]
                cands: list[tuple[str, str, str]] = []  # (rec_id, dedup_key, title_key)
                for r in recs:
                    if not isinstance(r, dict):
                        continue
                    # Skip already-shipped/implemented (producer writes
                    # this flag after the implementer commits).
                    if r.get("shipped") or r.get("implemented"):
                        continue
                    rid = r.get("id") or r.get("rec_id") or r.get("rec_uid")
                    if not rid:
                        continue
                    rid = str(rid)
                    dedup_key = f"{run_ts}:{rid}"
                    # TITLE-DEDUP (2026-05-12): same logical rec gets
                    # re-emitted across many run_ts when the underlying
                    # state hasn't changed (e.g. "publish 50 comparison
                    # pages — coverage gap" was emitted 67× across PI
                    # run-dirs). Dispatching each emission wastes
                    # implementer cycles on identical work. Track the
                    # normalized title in queued_ids so we only ship
                    # one of them, ever.
                    title = (r.get("title") or "").strip().lower()
                    # Normalize whitespace — common producer emits a
                    # variable hex/count prefix on each tick that would
                    # otherwise look like a different title.
                    title_norm = " ".join(title.split())
                    title_key = f"title:{aid}:{title_norm}" if title_norm else None
                    # Already queued by us on a prior tick (and not yet
                    # marked shipped by the producer/post-ship hook).
                    if dedup_key in already:
                        continue
                    if title_key and title_key in already:
                        # Same logical rec already dispatched (different run_ts)
                        continue
                    cands.append((rid, dedup_key, title_key or ""))
                    if len(cands) >= MAX_RECS_PER_PRODUCER_PER_TICK:
                        break
                if cands:
                    picked_run = run_ts
                    picked_rec_ids = [rid for (rid, _, _) in cands]
                    # Persist BOTH the per-run-ts dedup AND the title
                    # dedup so future ticks honor "we already shipped
                    # this logical rec".
                    picked_dedup_keys = []
                    for (_, dk, tk) in cands:
                        picked_dedup_keys.append(dk)
                        if tk:
                            picked_dedup_keys.append(tk)
                    break

            if not (picked_run and picked_rec_ids):
                continue

            # ── Queue-less dispatch (2026-05-12 — Phase 2 final) ────────
            # Materialize the source run-dir from blob to /tmp + call
            # dispatch_now() directly. No auto-queue blob, no responder
            # middleman, no drainer agent — the implementer scope is
            # the consumer, the run-dir IS the data, dispatch_now() is
            # the link. The implementer marks recs shipped on success
            # so the next tick's scan correctly skips them.
            try:
                from framework.core.run_dir import RunDir
                from framework.core import dispatch as _dispatch
                import tempfile as _tempfile
                site = _site_from_agent_id(aid) or ""
                rd = RunDir(aid, picked_run, site=site)
                td = Path(_tempfile.mkdtemp(prefix=f"rundir-{aid}-{picked_run}-"))
                try:
                    rd.materialize(td)
                except Exception as e:
                    self.decide("error",
                                f"run-dir materialize failed for {aid}/{picked_run}: {e}")
                    # Clean the tempdir on failure (success leaves it for
                    # the implementer scope to read).
                    import shutil
                    shutil.rmtree(td, ignore_errors=True)
                    continue
                # The implementer scope reads from this tempdir; it lives
                # past this run() because dispatch_now() spawns systemd-
                # run --scope --no-block (async).
                req_id = f"r-{picked_run}-{_subject_tag_from_agent_id(aid)}-{site or 'unknown'}"
                try:
                    handle = _dispatch.dispatch_now(
                        agent_id=aid,
                        run_dir=str(td),
                        rec_ids=picked_rec_ids,
                        action="implement",
                        site=site,
                        subject_tag=_subject_tag_from_agent_id(aid),
                        request_id=req_id,
                        fallback_to_queue=False,  # no queue — fail fast on site-lock contention
                        notify_on_failure=False,
                    )
                except Exception as e:
                    self.decide("error", f"dispatch_now failed for {aid}: {e}")
                    import shutil
                    shutil.rmtree(td, ignore_errors=True)
                    continue
                req_id = handle.request_id if handle else req_id
            except Exception as e:
                self.decide("error", f"dispatch path failed for {aid}: {e}")
                continue

            queued_now.extend(picked_dedup_keys)
            producers_touched += 1
            per_producer_actions.append({
                "agent": aid,
                "run_ts": picked_run,
                "rec_ids": picked_rec_ids,
                "request_id": req_id,
            })
            self.decide(
                "dispatched",
                f"{aid} run_ts={picked_run} recs={picked_rec_ids} req={req_id}",
                evidence={"agent": aid, "rec_ids": picked_rec_ids},
            )

        # Persist queued ids so we don't double-queue. Bounded so state
        # doesn't grow unbounded — keep the most recent 2000 we've seen.
        new_state_queued = list(already | set(queued_now))[-2000:]
        next_state = dict(self.state or {})
        next_state["queued_ids"] = new_state_queued
        # Also stash the hash so the auto short-circuit logic in
        # AgentBase._check_short_circuit notices when accumulator changes.

        if not per_producer_actions:
            return RunResult(
                status="success",
                summary="no producer had unshipped recs in run-dirs",
                next_state=next_state,
                metrics={"producers_dispatched": 0, "recs_dispatched": 0},
            )

        total_recs = sum(len(a["rec_ids"]) for a in per_producer_actions)
        return RunResult(
            status="success",
            summary=(
                f"dispatched {total_recs} rec(s) across {len(per_producer_actions)} "
                f"producer(s) directly to implementer (no queue)"
            ),
            next_state=next_state,
            metrics={
                "producers_dispatched": len(per_producer_actions),
                "recs_dispatched": total_recs,
                "inflight_after": inflight + len(per_producer_actions),
            },
        )


def main() -> int:
    return 0 if BacklogDispatcher().run_once().status == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
