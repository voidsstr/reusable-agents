"""End-to-end AgentBase lifecycle smoke test.

Covers: setup → pre_run (state load + response drain + confirmation resolution)
→ run → post_run (status + decisions + state). Uses LocalFilesystemStorage.
"""
import pytest

from framework.core.agent_base import AgentBase, RunResult
from framework.core.confirmations import (
    ConfirmationRecord, requires_confirmation, write_confirmation,
)
from framework.core.guardrails import declare
from framework.core.mailer import LogMailer


class DummyAgent(AgentBase):
    agent_id = "dummy"
    name = "Dummy Agent"
    category = "ops"
    capabilities = [
        declare("fetch", "Read external data", confirmation_required=False),
        declare("ship", "Push to production", confirmation_required=True,
                risk_level="high", affects=["production", "git"]),
    ]

    def __init__(self, *, observed_responses=None, **kw):
        super().__init__(**kw)
        self.observed_responses = observed_responses or []

    def setup(self):
        self.setup_called = True

    def run(self) -> RunResult:
        # Test status updates
        self.status("starting work", progress=0.1, current_action="probe")
        self.status("midway", progress=0.5)
        # Decisions
        self.decide("plan", "fetch GSC data and write a digest")
        self.decide("observation", "found 12 candidates", evidence={"count": 12})
        # Capture any responses we saw at run-start
        for r in self.responses:
            self.observed_responses.append(r)
        return RunResult(
            status="success",
            summary="ok 12",
            metrics={"items_processed": 12},
            next_state={"last_seen_count": 12},
        )


def test_basic_lifecycle(storage, monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    agent = DummyAgent(storage=storage)
    result = agent.run_once()

    assert result.status == "success"
    assert agent.setup_called is True

    # Status was finalized to success
    status = storage.read_json(f"agents/dummy/status.json")
    assert status is not None
    assert status["state"] == "success"
    assert status["iteration_count"] == 1

    # State persisted for next run
    state = storage.read_json("agents/dummy/state/latest.json")
    assert state["state"] == {"last_seen_count": 12}
    assert state["iteration_count"] == 1

    # Decisions log written
    decisions = storage.read_jsonl(f"agents/dummy/runs/{agent.run_ts}/decisions.jsonl")
    cats = [d["category"] for d in decisions]
    assert "plan" in cats and "observation" in cats

    # Run progress.json
    progress = storage.read_json(f"agents/dummy/runs/{agent.run_ts}/progress.json")
    assert progress["status"] == "success"
    assert progress["metrics"] == {"items_processed": 12}

    # Context summary recorded — uses category descriptions (e.g. "What I plan to do")
    ctx_md = storage.read_text(f"agents/dummy/runs/{agent.run_ts}/context-summary.md")
    assert "fetch GSC data" in ctx_md  # the plan message
    assert "12 candidates" in ctx_md   # the observation


def test_iteration_counter_increments(storage):
    a1 = DummyAgent(storage=storage)
    a1.run_once()
    a2 = DummyAgent(storage=storage)
    a2.run_once()
    state = storage.read_json("agents/dummy/state/latest.json")
    assert state["iteration_count"] == 2


def test_response_queue_drained_in_pre_run(storage):
    # Pre-seed a response in the queue
    storage.write_json("agents/dummy/responses-queue/r-fake-001.json", {
        "request_id": "r-fake-001",
        "rec_id": "rec-005",
        "action": "implement",
    })
    agent = DummyAgent(storage=storage)
    agent.run_once()
    assert len(agent.observed_responses) == 1
    assert agent.observed_responses[0]["rec_id"] == "rec-005"
    # And the response should now be archived (not in the queue)
    assert not storage.exists("agents/dummy/responses-queue/r-fake-001.json")
    assert storage.exists("agents/dummy/responses-archive/r-fake-001.json")


def test_run_failure_finalizes_status(storage):
    class BoomAgent(DummyAgent):
        def run(self):
            raise RuntimeError("kaboom")

    agent = BoomAgent(storage=storage)
    result = agent.run_once()
    assert result.status == "failure"
    status = storage.read_json("agents/dummy/status.json")
    assert status["state"] == "failure"


def test_confirmation_pending_blocks_method(storage):
    """A method gated with @requires_confirmation should:
       1. First invocation: queue a confirmation, raise ConfirmationPending,
          run() catches → status=blocked
       2. After approval seeded into storage, next invocation: method runs.
    """
    class GatedAgent(AgentBase):
        agent_id = "gated"
        name = "Gated Agent"
        category = "ops"
        capabilities = [declare("ship", "Ship to prod", confirmation_required=True)]

        def __init__(self, **kw):
            super().__init__(**kw)
            self.shipped = False

        @requires_confirmation(reason="ship to production")
        def ship(self):
            self.shipped = True
            return "shipped"

        def run(self) -> RunResult:
            self.ship()
            return RunResult(status="success", summary="done")

    agent = GatedAgent(storage=storage, mailer=LogMailer(storage=storage))
    result = agent.run_once()
    assert result.status == "blocked"
    assert agent.shipped is False

    # Find the pending confirmation by listing
    pending_keys = storage.list_prefix("agents/gated/confirmations/")
    assert len(pending_keys) == 1
    rec = storage.read_json(pending_keys[0])
    assert rec["state"] == "pending"

    # Approve it manually (simulates an email reply that the responder routed)
    rec["state"] = "approved"
    rec["approved_by"] = "test"
    storage.write_json(pending_keys[0], rec)

    # Next run should now ship — but we need to use the same run_ts so the
    # confirmation_id (which depends on run_ts) matches.
    agent2 = GatedAgent(storage=storage, run_ts=agent.run_ts,
                        mailer=LogMailer(storage=storage))
    result2 = agent2.run_once()
    assert result2.status == "success"
    assert agent2.shipped is True
