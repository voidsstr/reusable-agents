# Activity Digest Rollup

One consolidated email every 3 hours summarizing everything across the
agent fleet. Replaces the per-agent flood of confirmation/proposal/
completion/escalation emails that used to land in the inbox throughout
the day.

## How it works

1. Every individual agent's `send_via_msmtp(...)` call is gated by
   `DIGEST_ONLY=1` (default). When the gate is on, the email body is
   silently dropped into `digest-queue/<ts>-<hash>.json` instead of
   shipping. The function returns `(True, "suppressed: digest-mode")` so
   no caller breaks.

2. This agent runs on `0 */3 * * *` (every 3h). It:
   - Drains `digest-queue/*` from the last 3h
   - Walks every `agents/*/runs/*/recommendations.json` for recs whose
     `shipped_at` or `implemented_at` falls in the window
   - Reads every agent's `run-index.json` for runs that completed in
     the window (separating successes from failures)
   - Reads `agents/responder-agent/auto-queue/*` for currently-pending
     dispatches
   - Reads `agents/agent-doctor/errors/*` for escalations in window
   - Renders ONE HTML email grouped by site → category → agent
   - Ships it with `bypass_digest=True` so it actually leaves

3. After successful send, it archives the queue entries to
   `digest-archive/` (kept for audit, can be pruned later).

## Re-enabling individual emails

Set `DIGEST_ONLY=0` in the agent host's environment. All
`send_via_msmtp(...)` calls then send normally.

To bypass digest mode for a single send (e.g. confirmation flow you
want the user to see immediately), pass `bypass_digest=True` to
`send_via_msmtp`.

## Empty-window behavior

If absolutely nothing happened AND no individual emails were suppressed,
the agent skips sending. No "nothing to report" emails.
