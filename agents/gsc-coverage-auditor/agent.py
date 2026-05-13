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
        """Short-circuit when the coverage file hasn't been touched
        since the last successful run. Inspect.py only appends when a
        URL was inspected; if nothing's been inspected (quota exhausted,
        site list stable), re-recording the same metrics is wasteful."""
        cov = self._coverage_path()
        site = os.environ.get("GSC_INSPECT_SITE", "")
        if not cov.is_file():
            return None
        st = cov.stat()
        return {
            "site": site,
            "coverage_mtime": int(st.st_mtime),
            "coverage_size": st.st_size,
        }

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

        # Parse coverage file → metrics
        cov = self._coverage_path()
        if not cov.is_file():
            return RunResult(
                status="success",
                summary="inspect.py ran but no coverage file yet — first run?",
                metrics={"urls_inspected_7d": 0, "indexed_pct": 0.0, "unknown_pct": 0.0},
            )
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
        n_recent = 0
        latest: dict[str, tuple[str, str]] = {}
        with cov.open() as fh:
            for raw in fh:
                try:
                    r = json.loads(raw)
                except Exception:
                    continue
                url = r.get("url")
                ts = r.get("inspected_at", "")
                if not url:
                    continue
                if ts >= cutoff:
                    n_recent += 1
                prev = latest.get(url, ("", ""))
                if ts > prev[1]:
                    latest[url] = (r.get("coverageState", ""), ts)

        total = max(len(latest), 1)
        n_idx = sum(1 for cs, _ in latest.values() if cs == "Submitted and indexed")
        n_unk = sum(1 for cs, _ in latest.values() if cs == "URL is unknown to Google")
        n_cni = sum(1 for cs, _ in latest.values() if cs == "Crawled - currently not indexed")

        metrics = {
            "urls_inspected_7d": float(n_recent),
            "indexed_pct": round(100.0 * n_idx / total, 2),
            "unknown_pct": round(100.0 * n_unk / total, 2),
            "crawled_not_indexed_count": float(n_cni),
            "urls_in_universe": float(len(latest)),
        }

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
                f"(universe={int(metrics['urls_in_universe'])})"
            ),
            metrics=metrics,
        )


def main() -> int:
    return 0 if GscCoverageAuditor().run_once().status == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
