# affiliate-article-agent: runbook

Generic engine for **data-backed editorial guides** on an affiliate site whose
pages render from its own tables. It writes at most one guide per run, only
from the site's own SQL, and never publishes anything itself: the row goes in
as the site's queue status and `framework.cli.article_publish_gate` promotes
it. Per-site instances are config-only directories in the site repos
(`<site>/agents/<instance>/`: `manifest.json`, `site.yaml`, `goals.json`,
`AGENT.md`). The engine names no site; every table, SQL string, rule and
threshold is the instance's `article_engine:` block (template:
`site.example.yaml`).

| | |
|---|---|
| Code | `agent.py` (AgentBase + CLI), `article_engine.py` (phases), `article_settings.py`, `article_store.py`, `article_prompt.py`, `article_checks.py` |
| New framework primitives | `framework/core/authoring_capacity.py`, `editorial_calendar.py`, `facts_pack.py`, `guide_claims_guard.py` |
| Shared modules called (never edited) | `article_output_gate`, `content_policy`, `article_link_classes`, `article_claims_guard`, `required_model`, `ai_providers.ai_client_for`, `seasonal_calendar`, `featured_rotation`, `framework.cli.claude_pool.ready_profiles`, `framework.cli.article_publish_gate` |
| Config | `$AFFILIATE_ARTICLE_CONFIG` = `PATH[#key]` (default key `article_engine`) |
| Writes | site DB: one INSERT (and at most one UPDATE of that row) in the queue status; framework storage: the output-gate ledger, `runs/<run_ts>/engine-report.json`, `held-draft.json` on a hold; run state (`opus_day`, `opus_minutes_day`); host-local: the day's model ledger (`authoring_capacity.UsageLedger`) |
| Tests | `framework/tests/test_affiliate_article_agent.py`, `framework/tests/test_affiliate_article_red_team.py` (50 unsafe guide drafts + 10 controls + the regression variants in `fixtures/`, through the reference site's real config and a throwaway postgres) |
| Post-12-02 follow-ups (shared modules, not edited here) | `ai_providers`: a no-tools, no-project-memory mode for claude-cli (then drop `bin/claude-isolated`); implementer `run.sh` rc=75/124 defer path: also call `defer_backoff.record_defer`; `article_publish_gate.CheckResult.to_dict`: include `addendum` |

## One run (one run_ts, one progress.json)

0. **signals()** hashes the calendar file, the site's data timestamps
   (`signals.data_sql`), every written slug (`signals.published_sql`), the
   active occasions, the ledger date and count, and the flags. The hash is
   kept only after an outcome that depends on those inputs alone (no topic
   ready); a closed gate, a defer, a blackout or a run that had to skip an
   entry re-evaluates on the next tick.
1. **Window.** A blackout (`schedule.blackouts`) or a time before
   `schedule.not_before` (live runs) / `dry_run_not_before` (dry runs with the
   live model) stops the run: no Opus call.
2. **Held drafts first** (live, publish on). Up to `publish.retry_pending`
   of this engine's own rows still in the queue status (the publish gate held
   them, or never ran) go through the publish gate again, with no model call.
   A promotion is this run's guide.
3. **Output gate** (once). `article_output_gate.evaluate(site_id, audience,
   site_cfg=proposals.output_gate)`. Closed = no model call.
4. **Topic and facts, in code.** `editorial_calendar.choose`: the first entry
   that is active, not written yet (a row with its slug exists in any
   status), inside its window, has its occasion when it requires one, and
   whose `requires` all hold (named readiness SQL counts and operator flags).
   Then: `select_for_queue` on its tags (`lane:<entry lane>`,
   `lane:<gate_lanes>`, `holiday:<occasion>` when active); `facts_pack.build`
   runs `facts.queries` read-only with the entry's scope and raises on any
   column named like commerce data, any `forbidden_relations` table, or any
   value holding a money amount or Amazon text; the pack must offer enough
   LIVE pages for the link classes. An entry that fails any step is recorded
   (`report.json` `topic.tried`, `topic_attempts`) and the next entry is
   tried, up to `calendar.max_entries_per_run`: one bad entry never wedges
   the calendar. Only when every candidate's facts pack was refused is the run
   a failure (`facts-violation`). Occasions come from the instance's own list
   (`occasions.source: site`) or the framework calendar (`source: calendar`).
   No trends, forum or Amazon text reaches the prompt.
5. **Pool and model.** Opus only: `required_model` for `dispatch_kind`
   (`article-author`) must resolve to the `opus` tier and model, or the run
   defers (a non-Opus `required_tier` or an empty `allowed_provider_kinds` is
   a config error). The day's Opus minutes (`model.max_minutes_per_day`) and
   live-model dry-run calls (`model.max_dry_run_calls_per_day`) come from a
   host-local ledger (`authoring_capacity.UsageLedger`,
   `$AUTHORING_LEDGER_DIR` or `~/.reusable-agents/authoring-ledger/<id>.json`)
   that live runs and live-model dry runs share and that every call updates,
   even when the run fails afterwards; an unreadable ledger defers.
   `authoring_capacity.pool_decision`: `claude_pool.ready_profiles(model)`
   counts at least `pool.min_ready_profiles` distinct accounts and more than
   `CLAUDE_POOL_RESERVE`; no `peer_defer_watch` agent deferred for want of the
   model within `lookback_hours`, read from BOTH the defer-backoff records
   (`framework/defer-backoff/<agent>.json`) and the implementer's run-dir
   `agents/<agent>/runs/<run_ts>/deferred.json` (its pool-exhausted path
   writes only that); storage that cannot be read defers. The client is
   `ai_client_for(agent_id, override_model=<model>)` (never
   `chat_with_fallback`) and its provider kind must be in
   `model.allowed_provider_kinds`. A claude-cli client runs only when
   `model.claude_cli.verified` is true: each call then goes through
   `bin/claude-isolated` (no built-in tools, no MCP servers) from a fresh
   empty working directory (no project CLAUDE.md or memory). Any failure
   defers, with no model call.
6. **One model call** returning strict JSON (title, slug, subtitle, excerpt,
   body_md with placeholders, faqs, tags, buystrip_asins, expected_links).
   The system prompt carries the engine rules (numbers only from FACTS, tables
   only from placeholders, links only to FACTS paths, no price talk, the
   `holiday:<id>` tag rule) and the site's rules and voice. `holiday:` and
   `lane:` tags are always the code's, never the model's.
7. **Checks** (`article_checks.run_checks`): shape (slug fixed by the
   calendar), `content_policy` with the site's rule sets, the claims guard,
   the honesty guard, links (every internal link live and indexable per the
   site's view; link-class minimums counted on live links only), placeholders
   (grammar, position, every key resolves, every required one present).
   Buystrip ASINs outside the eligibility view are dropped. The claims guard:
   - every W / Wh / h / V / A / C figure (spelled-out numbers read as digits,
     days and weeks as hours, F converted) equals a facts value within
     rounding, and the value of the model it is about: the nearest model
     named in the sentence (whole-word, longest name first; "X Max" when only
     "X" is known is an unknown model), else the previous sentence's;
   - an hour figure about no model must be a subject-free fact (a load
     preset, a tier goal); `scenario` and `always_allowed` hours pass only
     written as the scenario ("a 24-hour outage"), never as a runtime;
     "3 full days" claims at least 72 h;
   - `claims.contexts`: a figure in a clause matching a pattern must come
     from the named columns (a temperature in a charging clause is the
     charging limit);
   - `claims.verdicts`: a pair whose verdict is not a fit is never called a
     fit in a sentence naming both; `claims.statuses`: a certification
     sentence matches the row's status and date;
   - generic wording: 6(y) price history, "lowest", deals, sales, point
     prices outside budget labels, money in any words (bucks, dollars,
     cents, 1k), list price / percent off, cheaper / best value, coupons and
     checkout, retail events (Black Friday, Cyber Monday, Prime Day...),
     buy-now timing, guarantees; the site's topic bans (any mention, link
     targets and placeholders excluded); Amazon URLs and image hosts;
     off-site links; raw HTML; inline images; hand-written tables.
   A failure re-prompts once with the addendum (`model.max_calls_per_run:
   2`), then the draft is **held** (not inserted; `held-draft.json`).
8. **INSERT** in `articles.insert_status`, hero NULL, byline from config,
   `written_by` = the instance id, citations = the facts' `*source_url`
   values. Then `record_batch` (the output-gate ledger) with holiday and lane
   ids.
9. **Promote**: `python3 -m framework.cli.article_publish_gate --site <site>
   --commit --no-requeue --slug <slug>`. A hold gets one re-prompt with the
   gate's reasons and check details if a model call is left (the row is
   UPDATEd and stays in the queue status), then stays held; step 2 re-gates
   it on later runs.
10. **Record**: metrics `guides_published`, `gate_closed`, `deferred_pool`,
    `checks_failed`, `opus_minutes` (+ `blackout`, `topic_ready`,
    `model_calls`, `held`, `gate_held`, `facts_rows`, the gate's
    `output_gate_*`); featured rotation when `post_publish.featured_rotation.enabled`.

## Dry run (no INSERT, no storage write, no state)

```bash
cd /home/voidsstr/development/reusable-agents
# canned model (built from the real facts pack), read-only DB session, no storage at all
DATABASE_URL_<SITE>=... python3 agents/affiliate-article-agent/agent.py --dry-run --no-storage \
  --site-yaml <site>/agents/<instance>/site.yaml --agent-id <instance> --out /tmp/article-dry-run
# a hand-written body through the same checks (red team, no model)
... --dry-run --no-storage --entry <entry-id> --fake-response body.json
# the real model (window + pool guards apply; storage read-only: writes are refused)
... --dry-run --live-model [--entry <entry-id>] [--adversarial prompt.txt]
```

Each run writes `<out>/<run_ts>-<slug>/`: `report.json` (outcome, gate, pool,
topic readiness, attempts), `facts.json`, `prompt.txt`, `draft.json`,
`article.md` (rendered markdown, placeholders as written) and `checks.json`.
Exit 0 when the run ended without a failure (a held draft is not a failure).

## Failure modes

| Outcome | Meaning | Fix |
|---|---|---|
| `blackout` / `not-before` | schedule says no Opus now | none (by design) |
| `no-topic` | no calendar entry has its data | the report lists each entry's unmet requirement: route it to the data agent that fills it |
| `gate-closed` | output gate cap or traffic gate | better guides / data; never raise the cap without a traffic signal |
| `deferred` | pool short, reserve, peer defer (or its signal unreadable), minutes / dry-run cap, ledger unreadable, provider kind, tier, claude-cli not verified | wait; move the cron; never add a fallback model |
| `facts-violation` (failure) | every candidate entry's facts query selects commerce data (one bad entry alone is skipped and reported in `topic.tried`) | fix the instance's SQL; never relax the assertion |
| `held` | checks or the publish gate refused the draft twice | read `attempts`; fix prompt rules or facts, never the rule |
| `model-error` (failure) | the client raised something other than a capacity error | look at the provider |
