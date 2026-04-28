# catalog-audit-agent

Reusable framework agent that runs a per-site catalog quality audit, converts
findings into the standard recommendations.json schema, emails ranked
recommendations with reply-syntax, and supports the email-reply →
implementer → completion-email loop.

## How it works

1. Run the configured `audit.command` (the existing per-site audit script).
2. Read its findings file (JSON for aisleprompt, CSV for specpicks).
3. Convert each finding into a `rec-NNN` with severity, tier, evidence.
4. Render an HTML email and send via the framework's `send_via_msmtp` (which
   tries Microsoft Graph first since the host's msmtp is sandboxed by AppArmor
   and can't exec the OAuth passwordeval).
5. Record outbound-email metadata at `agents/<agent_id>/outbound-emails/<request_id>.json`.
6. User replies with `implement rec-001 rec-005` (or `implement critical and high`).
7. Responder (cron, every minute) picks up the reply, dispatches `implementer`
   in a transient systemd-run scope.
8. implementer (claude --print, tool use) applies the changes.
9. implementer sends a completion email back to the user.

## Per-site instance pattern

Code lives once here. Each site is a thin manifest + `site.yaml`:

```
nsc-assistant/agents/aisleprompt-catalog-audit-agent/
  manifest.json
  site.yaml
  README.md
specpicks/agents/specpicks-catalog-audit-agent/
  ...
```

The manifest's `entry_command` exports `CATALOG_AUDIT_CONFIG=...` and runs
this `agent.py`.

## Site config (site.yaml) shape

```yaml
site_id: aisleprompt
label: AislePrompt
what_we_do: "AI grocery / meal-planning catalog (recipes + kitchen products)"

audit:
  command:
    - bash
    - "-c"
    - |
      cd "/path/to/site/repo" && \
      DATABASE_URL=... npx tsx scripts/catalog-quality-audit.ts --no-email
  findings_path: "/path/to/site/repo/audit-history"
  findings_format: aisleprompt-catalog-audit
  max_recs: 30
  timeout_s: 1800

reporter:
  email:
    from: "AislePrompt Audit <automation@northernsoftwareconsulting.com>"
    to: ["mperry@northernsoftwareconsulting.com"]
    subject_template: "[{agent_id}:{request_id}] Catalog Audit — {site} — {tag}"
  dashboard:
    base_url: http://localhost:8080
    agent_id: aisleprompt-catalog-audit-agent

implementer:
  repo_path: /path/to/site/repo
  branch: master
```

## Supported `findings_format` values

- `aisleprompt-catalog-audit` — reads the latest `audit-history/YYYY-MM-DD.json`
  written by `scripts/catalog-quality-audit.ts`.
- `specpicks-image-csv` — reads the latest `audit-product-images-*.csv`
  produced by `scripts/audit-product-images.ts`. Filters to mismatch rows.

Add a new converter in `agent.py` and register it in the `CONVERTERS` dict
to support a new audit script.
