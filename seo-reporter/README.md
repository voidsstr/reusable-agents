# seo-reporter

Reads `recommendations.json`, `comparison.json`, and `goal-progress.json` for a
run, then **emails a summary** to the configured address(es) and (optionally)
posts to a dashboard.

In `mode: recommend` this is the agent's only output — the user reads the email,
decides which recs to ship, and signals back via the dashboard or by editing
`responses.json` directly. The responder-agent picks those up and dispatches
to the implementer.

## Email content

- **Subject**: `SEO agent run — <site> — <tag>` (configurable)
- **Body** (HTML):
  - Run header (timestamp, mode, summary line)
  - 🎯 Recommendations table (rec id, priority, title, expected impact)
    - Each rec has an "Implement" link (dashboard URL + rec_id) when dashboard is configured
  - 📊 Metrics delta (impressions, clicks, CTR, avg pos, revenue KPIs)
  - 🎯 Prior-run top-5 goals — scored
  - 💰 Prior-run revenue goals — scored
  - 📈 New top-5 + revenue goals declared this run
  - Run dir path for the human's reference

## Usage

```bash
SEO_AGENT_CONFIG=my-site.yaml python3 send-report.py
SEO_AGENT_CONFIG=my-site.yaml python3 send-report.py --run-ts 20260425T140245Z --no-email --no-dashboard
```

Flags:
- `--run-ts <ts>` — report on a specific run (default: latest)
- `--no-email` — skip msmtp send
- `--no-dashboard` — skip dashboard POST
- `--dry-run` — print the rendered HTML to stdout instead of sending

## Configuration

```yaml
reporter:
  email:
    to: [you@example.com]
    from: SEO Agent <agent@example.com>
    msmtp_account: default
    subject_template: "SEO agent run — {site} — {tag}"
  dashboard:
    base_url: http://localhost:8080
    agent_id: seo-opportunity-agent
```

Set `email: null` or `dashboard: null` (or omit) to skip either channel.

## Reuse

The reporter is the most domain-neutral of the SEO agents — point it at any
`recommendations.json` matching the schema and it'll send an email. Useful for
plugging non-SEO agents into the same email digest.
