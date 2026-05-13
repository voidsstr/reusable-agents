# competitor-research-with-accumulator — Blueprint

Reusable pattern for an agent that periodically researches competitors,
accumulates feature-proposal recommendations across runs, and emails the
operator a growing backlog of "open" proposals (not just this run's
slice).

## When to use

You want an agent that:

- Periodically crawls competitor sites (and optionally app stores) to
  extract their feature lists
- Compares them to your site's features and proposes parity-gap +
  competitive-advantage feature ideas
- **Accumulates proposals over time** — re-running doesn't lose the
  backlog; same-titled proposals dedupe and refresh
- Emails the full open backlog every cycle so the operator sees what's
  still pending action
- Honors operator decisions — `implement rec-NNN` ships, `skip rec-NNN`
  drops it from future emails, `defer rec-NNN` revisits later

Reference deployment: `agents/competitor-research-agent/` driving
both `aisleprompt-competitor-research-agent` and
`specpicks-competitor-research-agent` per-site instances.

## What you get

```
agents/<your-agent-id>/
  AGENT.md                    # runbook
  agent.py                    # subclass of CompetitorResearchAgent
  _accumulator.py             # local copy of the accumulator (or import)
  manifest.json               # framework registration
  site.yaml                   # per-site config (one per deployment)
```

The accumulator stores at:

```
agents/<your-agent-id>/proposals/active.json
```

with this shape (one entry per unique-title proposal):

```json
{
  "schema_version": "1",
  "agent_id": "...",
  "proposals": [
    {
      "proposal_id": "<sha1[:16] of normalized title>",
      "state": "open" | "implemented" | "deferred" | "skipped",
      "first_proposed_at": "<iso>",
      "first_proposed_run_ts": "...",
      "last_seen_at": "<iso>",
      "last_seen_run_ts": "...",
      "times_seen": <int>,
      "state_changed_at": "<iso>" | null,
      "state_changed_reason": "...",
      "title":               "...",
      "category":            "parity-feature|competitive-advantage|ux-improvement|content-gap|...",
      "severity":            "critical|high|medium|low",
      "tier":                "auto|review|experimental",
      "confidence":          0.0..1.0,
      "competitor":          "...",
      "rationale":           "...",
      "expected_impact":     "...",
      "user_story":          "...",
      "blueprint":           {...},
      "success_metrics":     [...],
      "implementation_outline": {...},
      "evidence":            [...]
    }
  ]
}
```

## Per-run flow

1. **Crawl** — your site + competitors (web, optionally app stores)
2. **Extract features** via batched LLM call (one LLM call per competitor
   set or per phase)
3. **Compare → propose** — LLM emits structured blueprint per proposal
4. **Apply user replies** — translate `implement rec-NNN` etc. into
   `state` changes on the accumulator (proposals close out)
5. **Merge new proposals** — dedupe by `proposal_id`, refresh fields on
   still-open entries, append truly-new ones
6. **Email** — render the top N (configurable, default 50) of all
   `state="open"` proposals — NOT just this run's. Include backlog
   counts in the summary line.

The cumulative effect: every email shows the same proposals until the
operator acts on them. New proposals stack on top. Old ones never
disappear silently.

## Configuration

`site.yaml` extends the standard `site-quality` schema with these keys:

```yaml
site:
  id: aisleprompt
  domain: aisleprompt.com
  label: AislePrompt
  base_url: https://aisleprompt.com
  what_we_do: |
    <one-paragraph product description that the LLM uses to anchor
    the comparison>

competitors:
  seed_domains:
    - mealime.com
    - paprika.app
  max_competitors: 8
  max_pages_per_competitor: 4

# OPTIONAL — when your product is also a mobile app, list the app-store
# entries the agent should crawl in addition to the websites. The agent
# fetches iTunes API + Google Play store metadata and feeds it to the
# same comparison LLM call.
app_stores:
  ios_app_id:        "1234567890"          # numeric Apple App Store ID for our app
  android_package:   "com.example.app"     # bundle id for our app
  competitors:
    ios:                                   # list of competitor App Store IDs
      - "9876543210"
      - "1112223334"
    android:                               # list of competitor package names
      - "com.competitor.one"
      - "com.competitor.two"

crawler:
  seed_urls: ["/", "/about", "/features"]
  use_sitemap: true
  max_depth: 1
  max_pages: 12

analyzer:
  max_recs_per_run: 8
  auto_implement_threshold: 0.95

reporter:
  email:
    to: [mperry@northernsoftwareconsulting.com]
    from: automation@northernsoftwareconsulting.com
    msmtp_account: automation
    subject_template: "<Site> Competitor Research — {tag}"
    backlog_cap: 50    # cap email at top 50 proposals; rest visible on dashboard

implementer:
  agent_id: seo-implementer
  repo_path: /home/voidsstr/development/<your-site>
  branch: master
```

## Anti-patterns to avoid

- ❌ Re-rendering the email from this run's recommendations.json only.
  That's the OLD pattern; this blueprint exists specifically because it
  loses backlog visibility.
- ❌ Allowing the same proposal to "re-open" after the user skipped it.
  `merge_run` enforces sticky state — never re-opens a closed proposal.
- ❌ Deduping by `id` (rec-001/rec-002). Those are run-local; titles
  collide across runs. Use `proposal_id` (hash of normalized title).
- ❌ Sending uncapped emails when the backlog grows large. Cap to
  `reporter.email.backlog_cap` (default 50) with a footer pointing to
  the dashboard for the full list.
- ❌ Hardcoding `to:` addresses other than the canonical operator
  inbox. See the **Outbound-email recipient policy** rule in
  `reusable-agents/CLAUDE.md`.

## Integration points

- **Storage** — Azure blob via `framework.core.storage`. The
  accumulator file lives at `agents/<id>/proposals/active.json`.
- **Email transport** — msmtp `automation` account on the framework
  host. Same as every other outbound-email-sending agent.
- **Responder** — replies to the email are picked up by
  `responder-agent`, parsed, and surfaced via `self.responses` on the
  next run. The accumulator `_apply_responses_to_accumulator()` helper
  translates them into `state` changes.
- **Dashboard** — the queue tab's `recs-by-category` endpoint walks
  recommendations.json files; for the accumulator-tab view, the
  agent's own `/api/agents/<id>/storage/proposals/active.json` returns
  the full structured doc.

## Files in this blueprint

- `BLUEPRINT.md` — this file
- `_TEMPLATES/site.yaml` — example site config (with optional
  `app_stores` block)
- `_TEMPLATES/agent.py.template` — agent skeleton
- `_TEMPLATES/manifest.json.template` — framework registration

To scaffold a new instance, copy from
`agents/competitor-research-agent/` and adjust the `site.yaml` for the
new site.
