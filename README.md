# reusable-agents

> Composable, configuration-driven Claude Code agents you can clone for your own
> site. Each agent has one job and reads/writes standardized files so you can
> mix-and-match.

The headline workflow today is **SEO + top-5 rank work** for any site you
have GSC + GA4 access to. The pieces are independent: pull data, analyze,
report, optionally implement, optionally deploy, plus a feedback loop that
lets a human pick which recommendations to ship.

```
seo-data-collector ──► /run/data/*.json
                            │
                  seo-analyzer ──► /run/recommendations.json ◄──┐
                                          │                      │
                                ┌─────────┴───────┐              │
                                │                 │              │
                       (mode=recommend)   (mode=implement)       │
                                │                 │              │
                          seo-reporter   seo-implementer         │
                          emails recs    LLM writes code         │
                                │                 │              │
                          ▼ user reads ▼   seo-deployer          │
                          ▼ replies   ▼   tests + ships          │
                                │                                │
                                └─► responder-agent ◄ polls IMAP ┘
                                    parses replies,
                                    dispatches actions
```

## Why this exists

Most "SEO agents" are monoliths — they pull data, analyze, write code, deploy,
all in one black-box session. Hard to reuse. Hard to inspect. Hard to fork
just one piece.

This repo splits the work into 6 small agents that talk through standardized
JSON files. You can:

- Run only the **collector + analyzer + reporter** to get daily SEO recommendations
  emailed to you, without ever auto-shipping anything.
- Add the **implementer + deployer** when you're ready to let the agent ship
  changes itself.
- Use the **responder-agent** to close the loop — read replies to the agent's
  emails, dispatch the marked recommendations.

Every agent is configuration-driven. No hardcoded site names, repo paths, or
deploy commands. One YAML file per site = full pipeline.

## Quick start: get daily SEO recommendations emailed to you

```bash
git clone https://github.com/voidsstr/reusable-agents
cd reusable-agents

# 1. Install deps (one-time)
pip install -r seo-data-collector/requirements.txt
pip install -r seo-analyzer/requirements.txt
pip install -r responder-agent/requirements.txt

# 2. Bootstrap Google OAuth (one-time, opens browser)
SEO_AGENT_CLIENT_ID="..." SEO_AGENT_CLIENT_SECRET="..." \
  python3 seo-data-collector/refresh-token.py --bootstrap

# 3. Make a config for your site
cp examples/sites/generic.yaml ~/.reusable-agents/seo/my-site.yaml
$EDITOR ~/.reusable-agents/seo/my-site.yaml
# (set GSC site_url, GA4 property_id, your email address)

# 4. Run a one-off
export SEO_AGENT_CONFIG=~/.reusable-agents/seo/my-site.yaml
python3 seo-data-collector/pull-data.py
python3 seo-analyzer/analyzer.py
python3 seo-reporter/send-report.py

# 5. Schedule it — the orchestrator script chains the 3 above.
```

You'll get an HTML email with ranked recommendations. Reply with `implement
rec-001 rec-005` (or `skip rec-002`) to mark which ones to ship — the
**responder-agent** picks up the reply and triggers the implementer.

## What each agent does

| Agent | Reads | Writes | LLM? |
|---|---|---|---|
| [`seo-data-collector`](seo-data-collector) | GSC + GA4 + (optional) DB | `data/*.json` | no |
| [`seo-analyzer`](seo-analyzer) | `data/*` | `recommendations.json`, `goals.json`, `snapshot.json`, `comparison.json` | no |
| [`seo-reporter`](seo-reporter) | `recommendations.json` + comparison + goal-progress | sends email, pings dashboard | no |
| [`seo-implementer`](seo-implementer) | `recommendations.json` + selected `rec_ids` | `changes/*.diff`, code commits | **yes** (Claude Code) |
| [`seo-deployer`](seo-deployer) | site config `deployer:` block | `deploy.json` (test+build+push+deploy+smoke) | no |
| [`responder-agent`](responder-agent) | IMAP inbox | `responses.json`, dispatches to seo-implementer | no |

Only `seo-implementer` needs Claude. The other five run as plain Python /
shell scripts. Useful if you want recommendations without auto-coding, or
if you want to run the deterministic pieces on cheap infra and only spin
up Claude when there's something to ship.

## Standardized run dir

Every run writes to `<runs_root>/<site>/<UTC-ts>/`:

```
data/                       # raw collector output
  gsc-queries-90d.json      gsc-pages-90d.json
  ga4-organic-landing-90d.json   db-stats.json
  gsc-top5-targets.json     gsc-zero-click.json
  ...
snapshot.json               # analyzer's metric record
comparison.json             # snapshot vs prior
recommendations.json        # the contract — what reporter/implementer/dashboard read
goals.json                  # this run's declared goals
goal-progress.json          # prior run's goals scored
changes/                    # implementer output (only if implement mode)
  rec-001.diff
  rec-001.summary.md
deploy.json                 # deployer output (only if implement mode)
responses.json              # user feedback (input to responder)
run.json                    # status + timing
```

Schemas live in [`shared/schemas/`](shared/schemas/) — see
[`site-config.schema.json`](shared/schemas/site-config.schema.json),
[`recommendations.schema.json`](shared/schemas/recommendations.schema.json), and
[`responses.schema.json`](shared/schemas/responses.schema.json).

## Modes: recommend vs implement

Every site config has `site.mode`:

- **`recommend`** (safe default): collector → analyzer → reporter. The agent
  emails recommendations; you decide what to ship and when. No code is
  written and no deploys happen.
- **`implement`**: full loop — collector → analyzer → implementer → deployer
  → reporter. The agent ships up to N changes per run, runs tests as a
  hard gate, and emails a summary.

Switching from recommend → implement is a one-line edit. The implementer +
deployer blocks in your config are inert in recommend mode but ready to go.

## The feedback loop (recommend mode)

1. Agent runs collector + analyzer + reporter on a schedule.
2. You get an email with recommendations:
   ```
   Recommendations
   rec-001  HIGH    🎯 Build /best/best-tacos for query "tacos"
   rec-002  MEDIUM  ✏️ Rewrite snippet for "easy weeknight dinners"
   rec-003  HIGH    💰 Zero Amazon click-throughs in 30d
   ```
3. You reply: `implement rec-001 rec-003` (or `skip rec-002`).
4. **responder-agent** (cron, every minute) polls the inbox, parses the reply,
   appends to `responses.json`, and triggers `seo-implementer` with rec-001
   and rec-003.
5. Implementer applies, deployer ships, reporter emails the result.

## Composability

Each agent's input/output schemas are stable contracts. You can:

- Replace the `seo-analyzer` with your own scoring logic — anything that
  writes a valid `recommendations.json`.
- Plug in a different deployer for different cloud / framework.
- Use `responder-agent` for non-SEO use cases — it routes any
  `[<agent>:<site>] implement <id>` reply to a configured dispatcher.

## Repo structure

```
reusable-agents/
  shared/
    schemas/                # JSON Schemas — the contracts
    site_config.py          # YAML loader + validator
    run_files.py            # read/write helpers for the run dir
    agent_recorder.py       # optional dashboard-recording lib
  seo-data-collector/
  seo-analyzer/
  seo-reporter/
  seo-implementer/
  seo-deployer/
  responder-agent/
  examples/
    sites/                  # generic + per-site configs
  docs/
```

## License

MIT — see [LICENSE](LICENSE).
