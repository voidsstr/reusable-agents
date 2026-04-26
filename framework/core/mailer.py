"""Outbound mailer abstraction — used by AgentBase + @requires_confirmation.

Concrete implementations:
  - GraphMailer (Microsoft Graph sendMail) — primary
  - SmtpMailer (smtplib XOAUTH2) — fallback
  - LogMailer (writes to storage only, no real send) — for tests

The framework's email_service in the API layer constructs a real mailer
from config; tests construct LogMailer.

Every send writes a record to agents/<agent-id>/outbound-emails/<request-id>.json
so the responder can route replies back to the originating agent.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Optional

from .storage import StorageBackend, get_storage


logger = logging.getLogger("framework.mailer")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def outbound_email_key(agent_id: str, request_id: str) -> str:
    return f"agents/{agent_id}/outbound-emails/{request_id}.json"


class Mailer(ABC):
    """Send an email + record the routing metadata to storage."""

    @abstractmethod
    def send(
        self,
        *,
        agent_id: str,
        request_id: str,
        subject: str,
        body_html: str,
        to: Optional[list[str]] = None,
        expects_response: bool = False,
    ) -> bool: ...


class LogMailer(Mailer):
    """No-op mailer — records the would-be email to storage. Useful for
    tests, dry-runs, or sites where outbound email isn't configured."""

    def __init__(self, storage: Optional[StorageBackend] = None):
        self.storage = storage or get_storage()

    def send(
        self,
        *,
        agent_id: str,
        request_id: str,
        subject: str,
        body_html: str,
        to: Optional[list[str]] = None,
        expects_response: bool = False,
    ) -> bool:
        record = {
            "schema_version": "1",
            "request_id": request_id,
            "agent_id": agent_id,
            "subject": subject,
            "to": list(to or []),
            "body_html": body_html,
            "expects_response": expects_response,
            "sent_at": _now(),
            "transport": "log-only",
            "ok": True,
        }
        self.storage.write_json(outbound_email_key(agent_id, request_id), record)
        logger.info(f"[log-mailer] would send: {subject!r} to {to}")
        return True
