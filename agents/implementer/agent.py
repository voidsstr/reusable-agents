"""Implementer — AgentBase wrapper.

The actual LLM-driven code-editor logic lives in `run.sh` (1700+ lines:
build-aider-invocation, claude-pool routing, repo checkout, commit +
tag, downstream deployer trigger). This module is a thin AgentBase
shell that subprocess-calls `run.sh` so dispatched runs show up in the
dashboard with proper progress.json + run-index + status.

Triggered by:
  - responder-agent dispatch  (`RESPONDER_REC_IDS` + `RESPONDER_RUN_DIR`
    env vars + `SEO_AGENT_CONFIG` pointing at the site that's shipping)
  - direct manual trigger via the dashboard

The implementer is a chained agent — `runnable_modes=['chained']` — so
it never runs on cron. Each invocation processes one batch of rec_ids
against one site's run-dir.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from framework.core.agent_base import AgentBase, RunResult  # noqa: E402


def _read_json(path: Path):
    """Best-effort JSON read. Returns None on any problem."""
    try:
        import json as _json
        return _json.loads(path.read_text())
    except Exception:
        return None


def _ship_evidence(
    run_dir: Path, rec_ids: str, rec_count: int,
) -> tuple[int, int, str, bool]:
    """Return (shipped, deferred, reason, verified) from run artifacts.

    FAIL CLOSED. A rec counts as shipped only when an artifact proves
    it: the `_ship_status.json` sidecar run.sh writes, or an explicit
    `applied-recs.json` entry, or an `implemented` flag stamped by THIS
    run. Anything else counts as unverified — never as shipped.

    This is deliberately asymmetric: over-reporting a ship makes the
    fleet unauditable (deferred work looks delivered and nobody
    re-queues it), while under-reporting only costs a re-dispatch.

    Precedence:
      1. `_ship_status.json`  — run.sh's explicit verdict
      2. `applied-recs.json`  — DB-write convention (article-author,
         catalog-audit, h2h): applied_rec_ids / deferred_rec_ids
      3. `recommendations.json` — recs stamped implemented in this run
      4. `deferred.json`      — whole-batch defer (required-model-unavailable)
    """
    requested = [r.strip() for r in (rec_ids or "").split(",") if r.strip()]

    # 1. Explicit sidecar wins — run.sh computed this on purpose.
    ship = _read_json(run_dir / "_ship_status.json")
    if isinstance(ship, dict) and "shipped" in ship:
        try:
            shipped = int(ship.get("shipped", 0))
            deferred = int(ship.get("deferred", 0))
        except (TypeError, ValueError):
            shipped, deferred = 0, 0
        reason = str(ship.get("reason", "ship_status"))
        return shipped, deferred, reason, True

    shipped_ids: set[str] = set()
    deferred_ids: set[str] = set()
    reasons: list[str] = []

    # 2. applied-recs.json — the DB-write convention.
    applied = _read_json(run_dir / "applied-recs.json")
    if isinstance(applied, dict):
        for key in ("applied_rec_ids", "rec_ids"):
            for rid in applied.get(key) or []:
                if rid:
                    shipped_ids.add(str(rid))
        for rid in applied.get("deferred_rec_ids") or []:
            if rid:
                deferred_ids.add(str(rid))
        if shipped_ids or deferred_ids:
            reasons.append(str(applied.get("method") or "applied-recs.json"))

    # 3. recommendations.json — recs this run stamped implemented.
    this_run = os.environ.get("RESPONDER_RUN_TS", "")
    recs_doc = _read_json(run_dir / "recommendations.json")
    if isinstance(recs_doc, dict):
        wanted = set(requested)
        for r in recs_doc.get("recommendations") or []:
            if not isinstance(r, dict):
                continue
            rid = str(r.get("id") or "")
            if rid not in wanted or r.get("implemented") is not True:
                continue
            # Only count a stamp from THIS run (or an explicit
            # pre-existing verification); an older run's flag is not
            # evidence that this batch did anything.
            if (
                not this_run
                or r.get("implemented_run_ts") == this_run
                or r.get("implemented_via") == "pre-existing"
            ):
                shipped_ids.add(rid)

    # 4. deferred.json — whole-batch defer (e.g. opus unavailable).
    dfr = _read_json(run_dir / "deferred.json")
    if isinstance(dfr, dict):
        ids = dfr.get("rec_ids") or dfr.get("deferred_rec_ids") or []
        if ids:
            for rid in ids:
                if rid:
                    deferred_ids.add(str(rid))
        elif requested:
            deferred_ids.update(r for r in requested if r not in shipped_ids)
        if dfr.get("reason"):
            reasons.append(str(dfr["reason"]))

    # A rec cannot be both; an explicit apply beats an inherited defer.
    deferred_ids -= shipped_ids

    if not shipped_ids and not deferred_ids:
        return 0, 0, "no-evidence", False

    reason = "; ".join(dict.fromkeys(reasons)) or "artifact-derived"
    return len(shipped_ids), len(deferred_ids), reason, True


class Implementer(AgentBase):
    agent_id = "implementer"
    name = "Implementer"
    description = (
        "LLM-driven code editor. Reads selected recommendations + site "
        "repo, applies edits via aider/claude/opencode chain, commits + "
        "tags release, triggers deployer if configured."
    )
    category = "seo"
    # Implementer dispatches its own per-rec emails on success/failure.
    send_run_summary_email = False

    def run(self) -> RunResult:
        rec_ids = os.environ.get("RESPONDER_REC_IDS", "")
        run_dir = os.environ.get("RESPONDER_RUN_DIR", "")
        cfg = os.environ.get("SEO_AGENT_CONFIG", "")
        # Derive SEO_AGENT_CONFIG from RESPONDER_SITE if the dispatcher
        # didn't set it explicitly. dispatch_now() callers from non-SEO
        # producers (catalog-audit, PI, competitor-research) typically
        # pass RESPONDER_SITE only; run.sh has the same fallback for the
        # bash side, but agent.py runs first and bails before run.sh
        # gets a chance to derive.
        if not cfg:
            site = os.environ.get("RESPONDER_SITE", "")
            if site:
                fallback = (
                    Path(_REPO_ROOT) / "examples" / "sites" / f"{site}.yaml"
                )
                if fallback.is_file():
                    cfg = str(fallback)
                    os.environ["SEO_AGENT_CONFIG"] = cfg
        if not (rec_ids and run_dir and cfg):
            missing = [n for n, v in [
                ("RESPONDER_REC_IDS", rec_ids),
                ("RESPONDER_RUN_DIR", run_dir),
                ("SEO_AGENT_CONFIG (or RESPONDER_SITE)", cfg),
            ] if not v]
            return RunResult(
                status="failure",
                summary=f"missing env: {', '.join(missing)}",
                error_text=f"implementer requires {missing} to be set by the dispatching agent",
            )

        rec_count = len([r for r in rec_ids.split(",") if r.strip()])
        self.decide("plan",
                    f"implementing {rec_count} rec(s) from "
                    f"run-dir={run_dir} site_cfg={cfg}")
        self.status(
            f"implementing {rec_count} rec(s)", progress=0.10,
            current_action="aider/claude chain",
        )

        run_sh = Path(__file__).resolve().parent / "run.sh"
        env = {**os.environ}
        # AgentBase already sets AGENT_ID / RUN_TS for us
        env["AGENT_ID"] = self.agent_id
        env["RUN_TS"] = self.run_ts
        # run.sh keys its own run-index off IMPLEMENTER_RUN_TS — pin it to
        # AgentBase's run_ts so the bash-side run-index entry, the
        # progress.json this AgentBase wrapper writes, and the dispatch
        # log filename all share one timestamp.
        env["IMPLEMENTER_RUN_TS"] = self.run_ts
        env["PYTHONPATH"] = str(_REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")

        proc = subprocess.run(["bash", str(run_sh)], env=env)
        rc = int(proc.returncode)

        if rc != 0:
            self.decide("warning", f"run.sh exited rc={rc}")
            return RunResult(
                status="failure",
                summary=f"implementer exited rc={rc} on {rec_count} rec(s)",
                error_text=f"run.sh returned non-zero: {rc}",
                metrics={"rec_count": rec_count, "exit_code": rc},
            )

        # Establish what actually shipped. See _ship_evidence(): this
        # FAILS CLOSED. rc=0 alone is not evidence of a ship — the old
        # code defaulted `shipped = rec_count` whenever the sidecar was
        # missing, so every path that doesn't write one (notably the
        # claude-pool article-author path) reported "implemented N/N"
        # even when every rec in the batch was DEFERRED.
        shipped, deferred, reason, verified = _ship_evidence(
            Path(run_dir), rec_ids, rec_count,
        )

        if not verified:
            # No artifact proves anything shipped. Say so plainly rather
            # than claiming success — an unauditable fleet is worse than
            # a visibly-uncertain one.
            self.decide(
                "warning",
                f"no ship evidence for {rec_count} rec(s) — reporting unverified",
            )
            return RunResult(
                status="success",
                summary=(
                    f"no verified ship for {rec_count} rec(s) from "
                    f"{Path(run_dir).name} (no commit, no applied-recs.json, "
                    f"no ship-status sidecar)"
                ),
                metrics={
                    "rec_count": rec_count,
                    "shipped": 0,
                    "deferred": deferred,
                    "unverified": rec_count - deferred,
                    "exit_code": 0,
                },
            )

        if shipped == 0 and deferred > 0:
            self.decide("result", f"deferred {deferred} rec(s) — reason={reason}")
            return RunResult(
                status="success",
                summary=f"deferred {deferred} rec(s) from {Path(run_dir).name} ({reason})",
                metrics={
                    "rec_count": rec_count,
                    "shipped": 0,
                    "deferred": deferred,
                    "exit_code": 0,
                },
            )

        self.decide(
            "result",
            f"shipped {shipped}/{rec_count} rec(s) (deferred {deferred})",
        )
        return RunResult(
            status="success",
            summary=(
                f"implemented {shipped}/{rec_count} rec(s) from "
                f"{Path(run_dir).name}"
                + (f" ({deferred} deferred)" if deferred else "")
            ),
            metrics={
                "rec_count": rec_count,
                "shipped": shipped,
                "deferred": deferred,
                "exit_code": 0,
            },
        )


def main() -> int:
    # Explicit agent_id — bypass the AGENT_ID env-fallback in AgentBase.
    # The implementer is invoked from dispatch.dispatch_now() which sets
    # AGENT_ID to the *dispatching* agent (the source of the recs, e.g.
    # `aisleprompt-progressive-improvement-agent`) so the implementer
    # knows whose recs it's shipping. Without this override, our
    # decisions + status would be written under the dispatcher's run-dir
    # instead of the implementer's. AgentBase's per-class agent_id is
    # the source of truth for status/runs; the dispatcher's identity is
    # already in RESPONDER_AGENT_ID + the rec metadata.
    #
    # Honor IMPLEMENTER_RUN_TS from dispatch.py — it's the same timestamp
    # the dispatch log filename uses, so the dashboard's Runs tab + the
    # implementer-queue dispatch row map 1:1.
    run_ts = os.environ.get("IMPLEMENTER_RUN_TS") or None
    return 0 if Implementer(agent_id="implementer", run_ts=run_ts).run_once().status == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
