"""gsc-coverage-auditor — AgentBase wrapper (2026-05-11 conversion).

Replaces the previous bash-driven entry_command. The pre-conversion
run.sh did three things: (1) exec inspect.py to call GSC URL Inspection,
(2) compute % indexed / % unknown from the coverage JSONL, (3) push
metrics to Layer-A via metric_helper.record_many().

This AgentBase shell preserves the same data flow but:
  - registers a run in the framework run-index (visible in dashboard
    Runs tab) — was previously invisible
  - emits RunResult.metrics so Layer-B auto-track via target_metric
    works AS WELL as the existing Layer-A path (which we keep for
    backward compat — site-goals-tracker reads from the cache the
    Layer-A path populates)
  - declares signals() so an unchanged coverage file short-circuits
    the re-record loop within seconds

Why both Layer-A and Layer-B: Layer-A goal_ids that don't match a
target_metric on the goal record fail silently (Layer-A finds no
matching goal). Mapping the SAME numbers to RunResult.metrics with
canonical metric-key names + binding target_metric below makes the
Layer-B path independently work, so the dashboard goals tab populates
correctly regardless of which path the operator points to.
"""
from __future__ import annotations

import sys
# CRITICAL: drop sys.path[0] (this agent's own dir) BEFORE any other
# import. The directory contains `inspect.py` which shadows Python's
# stdlib `inspect` module — dataclass internals (used by framework.core
# imports below) call inspect.get_annotations, which then resolves to
# our local file and crashes with `AttributeError: module 'inspect' has
# no attribute 'get_annotations'`. Removing sys.path[0] forces the
# stdlib resolution. We don't need our own dir on path anyway — this
# file only imports from framework.core (added below).
if sys.path and sys.path[0]:
    sys.path.pop(0)

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

_HERE = Path(__file__).resolve()
_REPO_ROOT = _HERE.parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from framework.core.agent_base import AgentBase, RunResult  # noqa: E402
from framework.core import metric_helper  # noqa: E402
from framework.core import gsc_coverage  # noqa: E402


class GscCoverageAuditor(AgentBase):
    # agent_id resolved from env so the same code runs per-site.
    # Defaults to "gsc-coverage-auditor" when run directly (no site env).
    agent_id = "gsc-coverage-auditor"
    category = "seo"

    def __init__(self, *args, **kwargs):
        env_id = os.environ.get("AGENT_ID")
        if env_id:
            kwargs.setdefault("agent_id", env_id)
        super().__init__(*args, **kwargs)

    def _coverage_path(self) -> Path:
        site = os.environ.get("GSC_INSPECT_SITE", "").strip()
        return Path(os.path.expanduser(
            f"~/.reusable-agents/gsc-coverage-auditor/{site}-coverage.jsonl"
        ))

    def signals(self) -> dict | None:
        """No short-circuit. This agent's input is Google's index, which
        changes without anything local changing. The old signal -- the
        coverage file's mtime and size -- was taken BEFORE run(), and only
        run() (inspect.py) writes that file, so one successful run that
        appended nothing (every inspection rate-limited) would have matched
        its own hash and skipped every run after it, for good."""
        return None

    def run(self) -> RunResult:
        site = os.environ.get("GSC_INSPECT_SITE", "").strip()
        if not site:
            return RunResult(
                status="failure",
                summary="GSC_INSPECT_SITE env var not set — set it in the "
                        "per-site manifest's run.sh wrapper.",
            )

        self.status(f"inspecting {site}", progress=0.1)
        inspect_script = _HERE.parent / "inspect.py"
        if not inspect_script.is_file():
            return RunResult(status="failure", summary=f"inspect.py not found at {inspect_script}")

        # Run inspect.py — it appends per-URL records to the coverage
        # JSONL at ~/.reusable-agents/gsc-coverage-auditor/<site>-coverage.jsonl
        try:
            proc = subprocess.run(
                ["python3", str(inspect_script)],
                capture_output=True, text=True,
                timeout=int(os.environ.get("GSC_INSPECT_TIMEOUT_S", "1800")),
            )
            self.decide(
                "observation",
                f"inspect.py rc={proc.returncode} stdout={len(proc.stdout)}b stderr={len(proc.stderr)}b",
            )
        except subprocess.TimeoutExpired as e:
            return RunResult(
                status="failure",
                summary=f"inspect.py timed out after {e.timeout}s",
                error_text=(e.stderr.decode() if e.stderr else "")[-2000:],
            )
        except Exception as e:
            return RunResult(status="failure", summary=f"inspect.py raised: {e}")

        self.status("computing coverage metrics", progress=0.7)

        # A non-zero rc means inspect.py never got to write anything.
        # This used to be swallowed: the rc was captured but never
        # checked, so a hard crash (for ~90 consecutive runs, a dead
        # refresh-token.py path raising CalledProcessError) surfaced as
        # a *successful* run reporting "no coverage file yet — first
        # run?" with urls_inspected_7d=0. The one agent that exists to
        # explain the indexing wall was reporting green while producing
        # nothing. Fail loudly instead.
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip()[-2000:]
            return RunResult(
                status="failure",
                summary=f"inspect.py exited rc={proc.returncode} for {site}",
                error_text=tail or f"inspect.py returned {proc.returncode} with no output",
                metrics={"urls_inspected_7d": 0, "indexed_pct": 0.0, "unknown_pct": 0.0},
            )

        # Parse coverage file → metrics
        cov = self._coverage_path()
        if not cov.is_file():
            # rc==0 but nothing on disk: a real anomaly, not a benign
            # first run. Report it as a failure so it can't sit green.
            return RunResult(
                status="failure",
                summary=(
                    f"inspect.py exited 0 but wrote no coverage file at {cov}"
                ),
                error_text=(proc.stderr or "").strip()[-2000:],
                metrics={"urls_inspected_7d": 0, "indexed_pct": 0.0, "unknown_pct": 0.0},
            )
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
        with cov.open() as fh:
            lines = fh.readlines()
        n_recent = 0
        for raw in lines:
            try:
                if (json.loads(raw).get("inspected_at") or "") >= cutoff:
                    n_recent += 1
            except Exception:
                continue
        latest = gsc_coverage.latest_by_url(lines)

        total = max(len(latest), 1)
        n_idx = sum(1 for r in latest.values() if gsc_coverage.is_indexed(r["state"], r["verdict"]))
        n_unk = sum(1 for r in latest.values() if r["state"] == gsc_coverage.UNKNOWN_STATE)
        n_cni = sum(1 for r in latest.values() if r["state"] in gsc_coverage.CRAWLED_NOT_INDEXED_STATES)

        metrics = {
            "urls_inspected_7d": float(n_recent),
            "indexed_pct": round(100.0 * n_idx / total, 2),
            "unknown_pct": round(100.0 * n_unk / total, 2),
            "crawled_not_indexed_count": float(n_cni),
            "urls_in_universe": float(len(latest)),
        }
        # Indexed % per section. The blended figure above is dominated by
        # whichever section has been inspected most; these say whether the
        # pages that matter are getting in. Sections under 20 URLs are noise.
        sections = gsc_coverage.section_summary(latest)
        ranked = sorted(sections.items(), key=lambda kv: -kv[1]["inspected"])
        for name, c in ranked:
            if c["inspected"] >= 20:
                metrics[f"indexed_pct__{gsc_coverage.metric_key(name)}"] = round(100.0 * c["indexed"] / c["inspected"], 2)
        by_section = " · ".join(f"{name} {c['indexed']}/{c['inspected']}" for name, c in ranked[:6])

        # Layer-A: also push to metric_helper using the legacy goal_id
        # keys so site-goals-tracker rollups stay consistent.
        try:
            metric_helper.record_many(
                self.agent_id,
                {
                    "goal-urls-inspected-7d": metrics["urls_inspected_7d"],
                    "goal-indexed-pct": metrics["indexed_pct"],
                    "goal-unknown-pct": metrics["unknown_pct"],
                },
                run_ts=self.run_ts,
                note=f"recorded by AgentBase wrapper (urls_in_universe={int(metrics['urls_in_universe'])})",
            )
        except Exception as e:
            self.decide("warning", f"metric_helper.record_many failed (non-fatal): {e}")

        return RunResult(
            status="success",
            summary=(
                f"{site}: inspected {n_recent} URLs in last 7d, "
                f"indexed={metrics['indexed_pct']}%, unknown={metrics['unknown_pct']}% "
                f"(universe={int(metrics['urls_in_universe'])}); indexed by section: {by_section}"
            ),
            metrics=metrics,
        )


def main() -> int:
    return 0 if GscCoverageAuditor().run_once().status == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
