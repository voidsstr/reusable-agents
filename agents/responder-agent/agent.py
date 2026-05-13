"""Responder agent — AgentBase wrapper.

Polls an IMAP inbox, parses replies, and dispatches actions
(`implement rec-001 rec-005`, etc.) to downstream agents. The heavy
lifting lives in `responder.py` — this module wraps `tick()` in
AgentBase lifecycle so runs show up in the dashboard with proper
progress.json + run-index + status.

One tick per scheduled run (every 15 min). Daemon mode (--daemon) is
not used in production — the framework's systemd timer drives the
cadence.

NOTE: this agent is reserved for email-reply flows. Producer agents
that want their recs implemented should call `framework.core.dispatch
.dispatch_now()` directly — see catalog-audit-agent for the canonical
pattern. The responder's auto-queue drain is now opt-in (set env
RESPONDER_DRAIN_AUTO_QUEUE=1 or `responder.drain_auto_queue=true` in
site cfg) and exists only as a manual-flush operator tool.
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

# Path-load `responder.py` (sibling file with no module-system hyphen
# concern, but consistent with the seo-opportunity pattern).
_spec = _ilu.spec_from_file_location(
    "_responder_impl",
    Path(__file__).resolve().parent / "responder.py",
)
assert _spec is not None and _spec.loader is not None
_responder = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_responder)


class ResponderAgent(AgentBase):
    agent_id = "responder-agent"
    name = "Responder Agent"
    description = (
        "Polls IMAP inbox, parses email replies, and dispatches "
        "actions to downstream agents. Producer agents that don't "
        "need an email round-trip call dispatch_now() directly."
    )
    category = "ops"
    # Operator already gets the digest rollup — no per-tick summary.
    send_run_summary_email = False

    def run(self) -> RunResult:
        cfg_path = os.environ.get("RESPONDER_CONFIG", "") or str(
            _responder.DEFAULT_CONFIG_PATH,
        )
        if not Path(cfg_path).exists():
            return RunResult(
                status="failure",
                summary=f"responder config not found: {cfg_path}",
                error_text=f"missing {cfg_path}",
            )
        # IMAP password sidecar — was env-injected by run.sh; now we
        # read it directly so the manifest entry_command stays simple.
        if not os.environ.get("REUSABLE_AGENTS_IMAP_PASS"):
            pwf = Path(os.path.expanduser("~/.reusable-agents/imap-password"))
            if pwf.exists():
                os.environ["REUSABLE_AGENTS_IMAP_PASS"] = pwf.read_text().strip()

        self.decide("plan", f"loading config={cfg_path}")
        cfg = _responder.load_config(Path(cfg_path))
        state = _responder.load_state()

        self.status("polling IMAP for replies", progress=0.20,
                    current_action="tick")
        new_state = _responder.tick(cfg, state)
        _responder.save_state(new_state)

        # Surface per-tick metrics if responder.tick() recorded them
        # (best-effort: the existing tick logs to stderr; we capture the
        # state delta as a coarse summary).
        msgs = int(new_state.get("last_tick_messages", 0) or 0)
        dispatched = int(new_state.get("last_tick_dispatched", 0) or 0)
        drained = int(new_state.get("last_tick_auto_queue_drained", 0) or 0)
        summary = (
            f"messages={msgs} dispatched={dispatched} auto-queue-drained={drained}"
        )
        self.decide("observation", summary)

        return RunResult(
            status="success",
            summary=summary,
            metrics={
                "messages": msgs,
                "dispatched": dispatched,
                "auto_queue_drained": drained,
            },
        )


def main() -> int:
    return 0 if ResponderAgent().run_once().status == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
