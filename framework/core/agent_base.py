"""AgentBase — every reusable-agents agent inherits from this.

Lifecycle:
    setup()              run once per process — load config, connect to APIs
    pre_run()            framework-driven — read state, drain confirmations + responses
    run()                subclass override — the actual work
    post_run(result)     framework-driven — persist state, decisions, summary
    teardown()           run once per process — final cleanup

Subclasses override `run()` (and optionally setup/teardown). Everything
else is provided by the framework.

In `run()`, an agent has access to:
    self.storage         StorageBackend (Azure Blob in prod, Local FS in tests)
    self.status_reporter StatusReporter (call .running(), .success(), etc.)
    self.decisions       DecisionLog (call .plan(), .observe(), .choose(), …)
    self.mailer          OutboundMailer (optional — set by API service or wrapper)

Helpers:
    self.status(message, progress=, current_action=)  — shorthand for status_reporter
    self.decide(category, message, ...)               — shorthand for decisions
    self.find_context(since=...)                      — load prior run summaries
    self.message(target, body, kind, subject)         — send inter-agent message
    self.inbox()                                      — list unread messages
    self.confirm(action_description, reason)          — ad-hoc confirmation
    self.commit_and_release(files, message)           — tag + push a release
"""
from __future__ import annotations

import logging
import os
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from . import context_index, decision_log, messaging, release_tagger, scheduler
from .confirmations import (
    ConfirmationPending, ConfirmationRejected, ConfirmationRecord,
    confirmation_key, get_confirmation, list_pending_confirmations,
    write_confirmation,
)
from .registry import AgentManifest, register_agent, get_agent
from .status import StatusReporter, status_key
from .storage import StorageBackend, get_storage


logger = logging.getLogger("framework.agent")


@dataclass
class RunResult:
    """Returned from run() to hand off summary data to post_run()."""
    status: str = "success"                   # success | failure | blocked | cancelled
    summary: str = ""                         # short message for status.json
    summary_md: str = ""                      # verbose narrative for context-summary.md
    next_state: dict = field(default_factory=dict)  # state to carry to next run
    metrics: dict = field(default_factory=dict)
    progress: float = 1.0
    error_text: str = ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_run_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


class AgentBase:
    """Subclass this. Override `run()`. Keep `agent_id` stable.

    Class-level config the framework reads:
      agent_id          str  — required (kebab-case)
      name              str  — display name
      description       str
      category          str  — seo | research | fleet | personal | ops | misc
      capabilities      list[Capability] — declared dangerous actions
    """

    agent_id: str = ""
    name: str = ""
    description: str = ""
    category: str = "misc"
    capabilities: list = []  # list[Capability] — see guardrails.py

    def __init__(
        self,
        *,
        agent_id: Optional[str] = None,
        run_ts: Optional[str] = None,
        storage: Optional[StorageBackend] = None,
        triggered_by: str = "cron",
        mailer: Any = None,
    ):
        self.agent_id = agent_id or self.__class__.agent_id
        if not self.agent_id:
            raise ValueError(
                f"{self.__class__.__name__}: agent_id must be set "
                "(class attribute or constructor kwarg)"
            )
        self.run_ts = run_ts or _new_run_ts()
        self.storage = storage or get_storage()
        self.triggered_by = triggered_by
        self.mailer = mailer
        self.iteration_count = 0

        # Carried state — read in pre_run, written in post_run
        self.state: dict = {}
        # Pending response queue items found at run start
        self.responses: list[dict] = []
        # Pending confirmations resolved this run
        self.resolved_confirmations: list[ConfirmationRecord] = []

        # Subsystems
        self.status_reporter = StatusReporter(
            agent_id=self.agent_id,
            run_ts=self.run_ts,
            storage=self.storage,
        )
        self.decisions = decision_log.DecisionLog(
            agent_id=self.agent_id, run_ts=self.run_ts, storage=self.storage,
        )

        # Lifecycle bookkeeping
        self._started_at = _now()
        self._ended_at: Optional[str] = None

    # ---- Lifecycle hooks ----

    def setup(self) -> None:
        """Override for one-time process init (load API keys, etc.)."""

    def pre_run(self) -> None:
        """Framework-provided: load state, drain queues. Subclasses can
        override to extend (call super().pre_run() first)."""
        # Iteration count
        state = self.storage.read_json(f"agents/{self.agent_id}/state/latest.json") or {}
        self.iteration_count = int(state.get("iteration_count", 0)) + 1
        self.state = state.get("state", {})

        # Status: starting
        self.status_reporter.iteration_count = self.iteration_count
        self.status_reporter.starting(message="loading state + queues", progress=0.0)

        # Drain response queue (parsed user replies from the responder)
        responses_prefix = f"agents/{self.agent_id}/responses-queue/"
        for key in self.storage.list_prefix(responses_prefix):
            payload = self.storage.read_json(key)
            if not payload:
                continue
            self.responses.append(payload)
            # Move processed → archive
            archive_key = f"agents/{self.agent_id}/responses-archive/{payload.get('request_id', os.path.basename(key))}.json"
            self.storage.write_json(archive_key, {**payload, "consumed_at": _now(),
                                                  "consumed_in_run_ts": self.run_ts})
            self.storage.delete(key)

        # Resolve pending confirmations whose request_id matches a fresh response
        for rec in list_pending_confirmations(self.agent_id, self.storage):
            for resp in self.responses:
                if resp.get("request_id") == rec.request_id:
                    decision = (resp.get("action") or "").lower()
                    if decision in ("approve", "yes", "implement", "ok", "confirm"):
                        rec.state = "approved"
                    elif decision in ("reject", "no", "skip", "deny"):
                        rec.state = "rejected"
                    else:
                        # Ambiguous — leave pending; agent can re-prompt next run
                        continue
                    rec.resolved_at = _now()
                    rec.approved_by = "email-reply"
                    rec.notes = (rec.notes + f"\n[reply] {resp.get('notes','')}").strip()
                    write_confirmation(rec, self.storage)
                    self.resolved_confirmations.append(rec)
                    self.decisions.observe(
                        f"Confirmation {rec.confirmation_id} resolved: {rec.state}",
                        evidence={"reason": rec.reason, "approved_by": rec.approved_by},
                    )
                    break

    def run(self) -> RunResult:
        """OVERRIDE THIS. The actual agent work."""
        raise NotImplementedError("subclasses must implement run()")

    def post_run(self, result: RunResult) -> None:
        """Framework-provided: persist run artifacts, update state, status."""
        run_dir_prefix = f"agents/{self.agent_id}/runs/{self.run_ts}/"

        # Progress + metrics
        self.storage.write_json(run_dir_prefix + "progress.json", {
            "schema_version": "1",
            "agent_id": self.agent_id,
            "run_ts": self.run_ts,
            "started_at": self._started_at,
            "ended_at": self._ended_at or _now(),
            "iteration_count": self.iteration_count,
            "status": result.status,
            "progress": result.progress,
            "metrics": result.metrics,
            "summary": result.summary,
            "triggered_by": self.triggered_by,
        })

        # Decision summary as Markdown narrative
        summary_md = result.summary_md or ""
        if not summary_md:
            summary_md = decision_log.summarize_decisions_md(
                self.agent_id, self.run_ts, self.storage,
            )
        context_index.record_run_context(
            self.agent_id, self.run_ts, summary_md, self.storage,
        )

        # Carry state to next run
        self.storage.write_json(f"agents/{self.agent_id}/state/latest.json", {
            "iteration_count": self.iteration_count,
            "state": result.next_state,
            "updated_at": _now(),
        })
        # Snapshot the prior run's state into history
        self.storage.write_json(
            f"agents/{self.agent_id}/state/history/{self.run_ts}.json",
            {
                "iteration_count": self.iteration_count,
                "state": result.next_state,
                "updated_at": _now(),
            },
        )

        # Final status update
        if result.status == "success":
            self.status_reporter.success(message=result.summary or "ok",
                                         progress=result.progress)
        elif result.status == "failure":
            self.status_reporter.failure(message=result.summary or "failed",
                                         internal={"error": result.error_text[-2000:]})
        elif result.status == "blocked":
            self.status_reporter.blocked(message=result.summary or "blocked")
        elif result.status == "cancelled":
            self.status_reporter.cancelled(message=result.summary or "cancelled")
        else:
            self.status_reporter.idle()

    def teardown(self) -> None:
        """Override for final cleanup."""

    # ---- Orchestration ----

    def run_once(self) -> RunResult:
        """Top-level lifecycle invocation. Call this to do one full pass."""
        try:
            self.setup()
            self.pre_run()
            try:
                result = self.run()
                if result is None:
                    result = RunResult(status="success", summary="run() returned None")
            except ConfirmationPending as e:
                self.decisions.warn(
                    f"Awaiting confirmation: {e.confirmation_id}",
                    evidence={"reason": e.reason},
                )
                result = RunResult(
                    status="blocked",
                    summary=f"Awaiting confirmation ({e.reason})",
                    next_state=self.state,
                )
            except ConfirmationRejected as e:
                self.decisions.warn(
                    f"Confirmation rejected: {e.confirmation_id}",
                    evidence={"reason": e.reason},
                )
                result = RunResult(
                    status="cancelled",
                    summary=f"Confirmation rejected ({e.reason})",
                )
            except Exception as e:
                err_text = traceback.format_exc()
                logger.error(f"[{self.agent_id}] run() raised: {e}\n{err_text}")
                self.decisions.warn(f"Exception in run(): {e}",
                                    evidence={"traceback": err_text[-2000:]})
                result = RunResult(
                    status="failure",
                    summary=f"{type(e).__name__}: {e}",
                    error_text=err_text,
                )
            self._ended_at = _now()
            self.post_run(result)
            return result
        finally:
            try:
                self.teardown()
            except Exception as e:
                logger.warning(f"[{self.agent_id}] teardown raised: {e}")

    # ---- Helpers (subclasses use these) ----

    def status(self, message: str = "", progress: float = 0.0,
               current_action: str = "", state: str = "running") -> None:
        self.status_reporter.update(state=state, message=message,
                                    progress=progress, current_action=current_action)

    def decide(self, category: str, message: str, **kw: Any) -> None:
        self.decisions.record(category=category, message=message, **kw)

    def find_context(self, since: Optional[str] = None,
                     limit_chars: int = 20000) -> str:
        return context_index.find_context(
            self.agent_id, since=since, limit_chars=limit_chars, storage=self.storage,
        )

    def message(self, *, to: list[str] | str, body: dict,
                kind: str = "info", subject: str = "",
                in_reply_to: Optional[str] = None) -> str:
        return messaging.send_message(
            from_agent=self.agent_id, to_agents=to, kind=kind,
            subject=subject, body=body, in_reply_to=in_reply_to,
            storage=self.storage,
        )

    def inbox(self, unread_only: bool = True, limit: int = 100) -> list[dict]:
        return messaging.list_inbox(self.agent_id, unread_only=unread_only,
                                     limit=limit, storage=self.storage)

    def mark_message_read(self, message_id: str) -> bool:
        return messaging.mark_read(self.agent_id, message_id, self.storage)

    def commit_and_release(self, *, files: list[str], message: str,
                           repo_dir: str, branch: str = "", push: bool = True) -> dict:
        return release_tagger.commit_and_release(
            agent_id=self.agent_id, run_ts=self.run_ts, repo_dir=repo_dir,
            files=files, message=message, push=push, branch=branch,
            storage=self.storage,
        )

    def ai_client(self, *, provider: Optional[str] = None,
                  model: Optional[str] = None,
                  call: Optional[str] = None):
        """Return an AIClient configured for this agent.

        Resolution order:
          1. `provider` / `model` arguments (run-time override)
          2. agent's manifest metadata.ai_calls[call] (NAMED CALL OVERRIDE)
             — lets a single agent route different operations to different
             providers, e.g. metadata.ai_calls = {
               "audit":   {"provider": "claude-cli", "model": "claude-opus-4-7"},
               "summary": {"provider": "ollama-local", "model": "qwen3:8b"}
             }
             then `self.ai_client(call="audit")` uses Claude Opus while
             `self.ai_client(call="summary")` uses qwen3:8b.
          3. agent's manifest metadata.ai.{provider,model}
          4. defaults.json agent_overrides[<this-agent-id>]
          5. defaults.json default_provider / default_model
        Raises if no provider can be resolved.

        Usage:
            # Default agent-level provider:
            client = self.ai_client()
            # Named-call override (uses metadata.ai_calls["audit"]):
            client = self.ai_client(call="audit")
            # Run-time override:
            client = self.ai_client(provider="claude-cli", model="opus")
            response = client.chat([
                {"role": "system", "content": "You summarize SEO data."},
                {"role": "user",   "content": "Here are 200 GSC rows: ..."},
            ])
        """
        # Look up the named-call override from this agent's manifest if any
        if call and not provider:
            from . import registry as _registry
            m = _registry.get_agent(self.agent_id, storage=self.storage)
            if m is not None:
                ai_calls = (m.metadata or {}).get("ai_calls") or {}
                spec = ai_calls.get(call)
                if isinstance(spec, dict):
                    provider = provider or spec.get("provider") or None
                    model = model or spec.get("model") or None

        from . import ai_providers
        return ai_providers.ai_client_for(
            self.agent_id,
            override_provider=provider,
            override_model=model,
            storage=self.storage,
        )

    # ---- Class methods for registration ----

    @classmethod
    def register(
        cls,
        *,
        cron_expr: str = "",
        timezone_str: str = "UTC",
        entry_command: str = "",
        repo_dir: str = "",
        owner: str = "",
        extra_metadata: Optional[dict] = None,
        autowire_cron: bool = True,
    ) -> AgentManifest:
        """Register this agent class in the registry. Optionally writes
        systemd timer/service if cron_expr is given."""
        from .guardrails import serialize_capabilities
        if not cls.agent_id:
            raise ValueError(f"{cls.__name__}.agent_id must be set")

        metadata = {
            "capabilities": serialize_capabilities(cls.capabilities or []),
            **(extra_metadata or {}),
        }

        manifest = AgentManifest(
            id=cls.agent_id,
            name=cls.name or cls.agent_id,
            description=cls.description,
            category=cls.category,
            cron_expr=cron_expr,
            timezone=timezone_str,
            entry_command=entry_command,
            repo_dir=repo_dir,
            owner=owner,
            capabilities=[c.name for c in (cls.capabilities or [])],
            metadata=metadata,
        )
        registered = register_agent(manifest)

        if autowire_cron and cron_expr and entry_command:
            try:
                scheduler.write_systemd_units(
                    agent_id=cls.agent_id,
                    cron_expr=cron_expr,
                    entry_command=entry_command,
                    working_directory=repo_dir or os.path.expanduser("~"),
                    timezone=timezone_str,
                )
                scheduler.reload_and_enable(cls.agent_id)
            except Exception as e:
                logger.warning(f"systemd autowire failed for {cls.agent_id}: {e}")

        return registered
