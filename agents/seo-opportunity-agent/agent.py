"""SEO + Revenue Opportunity Agent — collapsed AgentBase pipeline.

Drives the three SEO phases sequentially under one run_ts so collector,
analyzer, and finalize all write to the same Azure run-dir
(`agents/<agent_id>/runs/<run_ts>/`):

  Phase 1  collector   pull GSC + GA4 (+ DB / Ads / Articles per cfg)
  Phase 2  analyzer    build snapshot, score recs, write recommendations.json
  Phase 3  finalize    render report, queue to digest, auto-queue recs to
                       implementer, record outbound for Confirmations page

Per-site `aisleprompt-seo-opportunity-agent` and
`specpicks-seo-opportunity-agent` set `AGENT_ID` + `SEO_AGENT_CONFIG`
env, then invoke this module. AgentBase post_run() writes
`progress.json`, `run-index.json`, and a `goal-progress.json` mirror —
no synthesized shapes, no bash glue.

Replaces `agents/seo-{data-collector,analyzer,reporter}/` (which were
chained scripts driven by per-site `run.sh`) and the
`framework.cli.status` terminal-state writer (which was a workaround
for non-AgentBase pipelines).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# Ensure the repo root is on sys.path so `framework.*` and
# `shared.*` resolve when invoked via `python3 path/to/agent.py`.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from framework.core.agent_base import AgentBase, RunResult  # noqa: E402
from framework.core.run_dir import RunDir  # noqa: E402

from shared.site_config import load_config_from_env  # noqa: E402


# `agents/seo-opportunity-agent/` has a hyphen — Python can't import it
# as `agents.seo_opportunity_agent`. Load the sibling finalizer.py by
# explicit path so the same source works under any invocation form.
import importlib.util as _ilu  # noqa: E402

_spec = _ilu.spec_from_file_location(
    "_seo_opportunity_finalizer",
    Path(__file__).resolve().parent / "finalizer.py",
)
assert _spec is not None and _spec.loader is not None
_mod = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
_finalize = _mod.finalize


class SEOOpportunityAgent(AgentBase):
    # agent_id resolved at instantiation from AGENT_ID env (per-site:
    # "aisleprompt-seo-opportunity-agent" / "specpicks-seo-opportunity-agent").
    # Class-level fallback for `register-agents.sh`-style standalone
    # registration, but production always uses the env override.
    agent_id = "seo-opportunity-agent"
    name = "SEO + Revenue Opportunity Agent"
    description = (
        "Pulls GSC + GA4 (+ DB), runs the analyzer, and queues "
        "recommendations to the implementer. Every 2h per site."
    )
    category = "seo"
    # We render our own digest entry — don't double-up with the
    # AgentBase boilerplate run-summary email.
    send_run_summary_email = False

    def run(self) -> RunResult:
        cfg = load_config_from_env()
        site_id = getattr(cfg, "site_id", "")
        cfg_path = os.environ.get("SEO_AGENT_CONFIG", "")
        if not cfg_path:
            return RunResult(
                status="failure",
                summary="SEO_AGENT_CONFIG not set",
                error_text="SEO_AGENT_CONFIG env var is required to load site.yaml",
            )

        self.decide("plan",
                    f"site={site_id} agent={self.agent_id} run_ts={self.run_ts} "
                    f"phases: collect → analyze → finalize")
        self.status("loading config", progress=0.02, current_action="setup")

        # ── Phase 1: collect ─────────────────────────────────────────
        self.status("1/3 collector — GSC + GA4 + DB pulls", progress=0.05,
                    current_action="collector")
        rc = self._run_phase(
            script_rel="agents/seo-opportunity-agent/lib/collector/pull-data.py",
            label="collector",
        )
        if rc != 0:
            self.decide("warning", f"collector exited rc={rc}")
            return RunResult(
                status="failure",
                summary=f"collector exited rc={rc}",
                error_text=f"seo-data-collector returned non-zero: {rc}",
            )

        # ── Phase 2: analyze ─────────────────────────────────────────
        self.status("2/3 analyzer — scoring + LLM audit", progress=0.40,
                    current_action="analyzer")
        rc = self._run_phase(
            script_rel="agents/seo-opportunity-agent/lib/analyzer/analyzer.py",
            label="analyzer",
        )
        if rc != 0:
            self.decide("warning", f"analyzer exited rc={rc}")
            return RunResult(
                status="failure",
                summary=f"analyzer exited rc={rc}",
                error_text=f"seo-analyzer returned non-zero: {rc}",
            )

        # ── Phase 3: finalize (replaces seo-reporter) ────────────────
        self.status("3/3 finalize — render + queue", progress=0.85,
                    current_action="finalize")
        # Pull the run-dir from Azure into a local tempdir so the
        # legacy renderer (which reads files via Path) still works
        # unchanged. RunDir handles upload-back-on-exit if anything
        # writes inside the tempdir; finalize only reads.
        rd = RunDir(self.agent_id, self.run_ts, site=site_id)
        with rd.tempdir(skip_globs=["data/page-cache/*"]) as td:
            outcome = _finalize(self, cfg=cfg, run_ts=self.run_ts, run_dir=td)

        return RunResult(
            status="success",
            summary=outcome.get("summary", "done"),
            short_circuited=outcome.get("short_circuited", False),
            metrics=outcome.get("metrics", {}),
        )

    # ---- internal ----

    def _run_phase(self, *, script_rel: str, label: str) -> int:
        """Subprocess the existing collector/analyzer scripts with
        --agent-id + --run-ts, streaming their stderr through ours so
        the systemd journal captures every line.

        Inherits AGENT_ID + RUN_TS + SEO_AGENT_CONFIG from the current
        process env — those scripts read all three. PYTHONPATH is set
        to the repo root so they can import framework + shared.
        """
        ra_repo = str(_REPO_ROOT)
        env = {
            **os.environ,
            "AGENT_ID": self.agent_id,
            "RUN_TS": self.run_ts,
            "PYTHONPATH": ra_repo + os.pathsep + os.environ.get("PYTHONPATH", ""),
        }
        cmd = [
            sys.executable,
            os.path.join(ra_repo, script_rel),
            "--agent-id", self.agent_id,
            "--run-ts", self.run_ts,
        ]
        print(f"[seo-pipeline] {label}: {' '.join(cmd)}", file=sys.stderr)
        proc = subprocess.run(cmd, env=env)
        return int(proc.returncode)


def main() -> int:
    """Entry point. Per-site manifest's `entry_command` invokes this.
    AGENT_ID env (set by manifest entry_command) names the per-site
    agent; SEO_AGENT_CONFIG env points at site.yaml.

    NOTE on the return: do NOT use `cond and 0 or 1` here — Python's
    operator precedence makes `True and 0` evaluate to 0, then
    `0 or 1` evaluates to 1, so the function would always return 1
    even on a clean success run. This was the actual cause of every
    cron run showing as systemd `status=1/FAILURE` despite the agent
    completing cleanly (the wrapper at framework/agent_run_wrapper.sh
    propagates rc → systemd via `exit $RC`).
    """
    return 0 if SEOOpportunityAgent().run_once().status == "success" else 1


if __name__ == "__main__":
    raise SystemExit(0 if main() == 0 else 1)
