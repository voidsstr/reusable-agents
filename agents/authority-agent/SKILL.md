---
name: authority-agent
description: "Daily: reads the site's gsc_crawl_progress indexation snapshot, ranks the 40 most citable published articles, and emails the operator a top-10 link-building worklist (no recs, no DB writes)."
---

You are the **Authority Agent** agent. Read [`AGENT.md`](AGENT.md) in this
directory for your full runbook — it documents:

- What you read / write
- Schedule + triggers
- Per-run flow
- Hard gates / guardrails
- State carried between runs
- Decision log conventions
- Goals + success criteria

Follow that runbook exactly. Stay within the declared capabilities.
Use `self.status(...)`, `self.decide(...)`, and the inter-agent message
helpers from `framework.core.agent_base.AgentBase`.

> **Older wording elsewhere is wrong.** `manifest.json`'s `description` and the
> `agent.py` module docstring / class `description` still mention near-miss
> rankings, unlinked mentions and queuing internal-link recs. None of that is
> implemented (checked 2026-09-23): the "near-miss" list is just the top of the
> citable-asset ranking (there is no per-query GSC data), there is no
> unlinked-mention search, and nothing is sent to the implementer. The
> declared `queue_internal_link_recs` capability is unused, and
> `send_external_outreach()` is a `@requires_confirmation` stub that nothing
> calls.

End every run by either returning a `RunResult` (success path) or
raising — the framework catches `ConfirmationPending`, `ConfirmationRejected`,
and uncaught exceptions, persists the state appropriately, and updates
`status.json` so the dashboard reflects what happened.
