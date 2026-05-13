"""SEO deployer — AgentBase wrapper.

Chained agent: only fires when responder-agent or implementer dispatches
to it with `RESPONDER_RUN_DIR` (or `--run-dir`) pointing at a run-dir
that has a built artifact ready to ship. Reads `deployer.*` from site
config, runs test → build → push → deploy → smoke.

Heavy lifting stays in `deployer.py` — this module wraps `main()` in
AgentBase lifecycle so runs show up in the dashboard with proper
progress.json + run-index + status.
"""
from __future__ import annotations

import importlib.util as _ilu
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from framework.core.agent_base import AgentBase, RunResult  # noqa: E402

_spec = _ilu.spec_from_file_location(
    "_seo_deployer_impl",
    Path(__file__).resolve().parent / "deployer.py",
)
assert _spec is not None and _spec.loader is not None
_deployer = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_deployer)


class SEODeployer(AgentBase):
    agent_id = "seo-deployer"
    name = "SEO Deployer"
    description = (
        "Pluggable test → build → push → deploy → smoke pipeline driven "
        "off site.yaml's deployer.* block. Hard-gates on test or smoke "
        "failure. Fired by upstream agents (responder + implementer)."
    )
    category = "seo"
    send_run_summary_email = False

    def run(self) -> RunResult:
        run_dir = os.environ.get("RESPONDER_RUN_DIR", "") or os.environ.get(
            "SEO_DEPLOYER_RUN_DIR", "",
        )
        if not run_dir:
            return RunResult(
                status="failure",
                summary="RESPONDER_RUN_DIR not set",
                error_text="seo-deployer requires RESPONDER_RUN_DIR pointing at the run-dir to ship",
            )
        rd = Path(run_dir)
        if not rd.is_dir():
            return RunResult(
                status="failure",
                summary=f"run-dir not found: {run_dir}",
                error_text=f"missing dir {run_dir}",
            )

        self.decide("plan", f"deploying from run-dir={run_dir}")
        self.status("running test → build → push → deploy → smoke",
                    progress=0.10, current_action="deploy-pipeline")

        # deployer.main() reads sys.argv — patch it so we don't need to
        # refactor 200 lines of CLI arg handling. Same in-process call.
        old_argv = sys.argv[:]
        sys.argv = ["deployer.py", "--run-dir", str(rd)]
        # DEPLOYER_SKIP_TEST=1 lets operators bypass the test stage when
        # they need to ship a fix that test infrastructure (broken/flaky
        # tests, dev-server downtime) is independently blocking.
        if os.environ.get("DEPLOYER_SKIP_TEST") == "1":
            sys.argv.append("--skip-test")
        try:
            _deployer.main()
            ok = True
        except SystemExit as e:
            ok = (e.code in (0, None))
        finally:
            sys.argv = old_argv

        # Read the deploy.json that deployer.py wrote to summarize
        import json as _json
        deploy_path = rd / "deploy.json"
        meta = {}
        if deploy_path.exists():
            try:
                meta = _json.loads(deploy_path.read_text())
            except Exception:
                pass
        status = meta.get("status", "success" if ok else "failure")
        summary = (
            f"deploy {status} tag={meta.get('tag','')} "
            f"smoke_ok={meta.get('smoke',{}).get('ok','?')}"
        )
        self.decide("result", summary)

        return RunResult(
            status="success" if (ok and status not in ("failure",)) else "failure",
            summary=summary,
            metrics={
                "tag": meta.get("tag", ""),
                "image": meta.get("image", ""),
                "test_rc": (meta.get("test") or {}).get("rc"),
                "build_rc": (meta.get("build") or {}).get("rc"),
                "deploy_rc": (meta.get("deploy") or {}).get("rc"),
                "smoke_ok": (meta.get("smoke") or {}).get("ok"),
            },
        )


def main() -> int:
    # Explicit agent_id — bypass the AGENT_ID env-fallback in AgentBase
    # because seo-deployer is invoked from inside the implementer chain
    # which propagates the dispatching agent's AGENT_ID. Without this
    # override, seo-deployer's status + decisions would be written under
    # whichever agent triggered the chain (e.g. responder-agent), not
    # under seo-deployer's own run-dir.
    return 0 if SEODeployer(agent_id="seo-deployer").run_once().status == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
