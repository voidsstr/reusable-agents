"""Guardrail registry — declared dangerous capabilities per agent.

Agents declare what they're allowed to do via class-level metadata so
the API/UI can show capabilities up-front and reviewers can audit
guardrails by reading the manifest, not the code.

This works hand-in-hand with @requires_confirmation — that decorator
gates each call, while this registry surfaces the *list* of gated
methods to the dashboard.

Usage:

    class SeoDeployer(AgentBase):
        capabilities = [
            Capability("deploy_to_azure", "Deploy customer app to prod Azure",
                       confirmation_required=True),
            Capability("rollback", "Roll back to a prior tag",
                       confirmation_required=True),
            Capability("read_metrics", "Read GSC + GA4 data",
                       confirmation_required=False),
        ]

The framework reads `cls.capabilities` at registration time and
publishes it on the agent manifest under `metadata.capabilities`.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional


@dataclass
class Capability:
    name: str                              # method name on the agent
    description: str                       # what this method does
    confirmation_required: bool = False    # True iff @requires_confirmation
    risk_level: str = "low"                # "low" | "medium" | "high" | "critical"
    affects: list[str] = field(default_factory=list)  # ["production", "git", "billing"]
    notes: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def declare(
    name: str,
    description: str,
    *,
    confirmation_required: bool = False,
    risk_level: str = "low",
    affects: Optional[list[str]] = None,
    notes: str = "",
) -> Capability:
    """Sugar so agent classes can list capabilities readably."""
    return Capability(
        name=name, description=description,
        confirmation_required=confirmation_required,
        risk_level=risk_level,
        affects=list(affects or []),
        notes=notes,
    )


def serialize_capabilities(caps: list[Capability]) -> list[dict]:
    return [c.to_dict() for c in caps]
