# seo-analyzer (legacy standalone copy — not an agent)

> **Heads up: the live code is somewhere else.**
>
> This directory predates the unified `seo-opportunity-agent` pipeline
> (`../seo-opportunity-agent/`). Production runs the analyzer from
> `../seo-opportunity-agent/lib/analyzer/analyzer.py` as phase 2 of that
> agent's `AgentBase` flow (`_run_phase(script_rel="agents/seo-opportunity-agent/lib/analyzer/analyzer.py")`).
>
> **Commit analyzer changes only to `lib/analyzer/analyzer.py`.** This copy is
> kept only so that old callers that ran it as a script still resolve. See
> "Who still references this dir" for what is left.

## At a glance

| | |
|---|---|
| Kind | Not an agent. A legacy library/script copy with no `manifest.json`, no systemd timer, and no registry entry. |
| Files | `analyzer.py` (rule passes + orchestration), `llm_audit.py` (LLM audit pass) |
| Schedule | None. Nothing on the fleet host runs this copy. |
| Live equivalent | `reusable-agents: agents/seo-opportunity-agent/lib/analyzer/` (run by `aisleprompt-seo-opportunity-agent` and `specpicks-seo-opportunity-agent`) |
| Deep reference | [engine README](../seo-opportunity-agent/README.md): rec-type catalog, run-dir layout, troubleshooting |

## Drift between the two copies (checked 2026-09-23)

| File | State |
|---|---|
| `llm_audit.py` | Byte-identical to `lib/analyzer/llm_audit.py`. |
| `analyzer.py` | Identical except one docstring. The engine copy's `_add_amazon_tag_recs` carries the 2026-08-29 note (commit `c2de01e`) explaining that the rule is dead as written: nothing produces `amazon_outbound_*` counters, so it never emits a rec. |

The two `analyzer.py` files can live at different depths because
`_find_repo_root()` walks up to the directory that holds both `framework/` and
`shared/`. A fixed `.parent.parent.parent` resolved correctly for this copy but
not for the engine copy. That broke `_crawl_for_audit` with
`No module named 'crawler'`. See the docstring at the top of `analyzer.py`.

## Why two copies?

Before the consolidation the SEO chain ran as three separate systemd-driven
scripts (`seo-data-collector` → `seo-analyzer` → `seo-reporter`), joined together by
per-site `run.sh` files. The unified pipeline replaced that with a single
`AgentBase` driver (`seo-opportunity-agent/agent.py`) that runs each phase
script as a subprocess, and the phase scripts moved into `lib/`. The standalone
`seo-data-collector/` and `seo-reporter/` dirs were deleted on 2026-05-13 (commit
`ab2fc5e`), along with this dir's `manifest.json` and `llm_audit.py`.
`analyzer.py` stayed here. `llm_audit.py` was missing from both locations until
2026-08-14 (commit `4cf975f`), when it was reconstructed into both dirs. Until
then every analyzer run logged "LLM audit failed: No module named 'llm_audit'"
and all recs were heuristic-only.

## Who still references this dir

| Reference | Status |
|---|---|
| `specpicks: agents/seo-opportunity-agent/run.sh` (step 2/3 calls `agents/seo-analyzer/analyzer.py`) | Legacy wrapper. It is **not** the registered entry command (the manifest runs `agents/seo-opportunity-agent/agent.py`). Step 1 calls `agents/seo-data-collector/pull-data.py`, which no longer exists, so the script would fail before it reached this copy. |
| `nsc-assistant: agents/_legacy-seo-opportunity-agent-multi-site/run.sh` | Legacy, `_legacy-*` dirs are skipped by `install/register-all-from-dir.sh`. |
| `framework/tests/test_seo_llm_audit.py` | Loads the **engine** copy (`lib/analyzer/llm_audit.py`), not this one. |
| The string `seo-analyzer` as an id | Still in use, but not as a registered agent. It is the AI-provider key the analyzer passes to `ai_providers.ai_client_for("seo-analyzer")`. `install/seed-providers-local.sh` seeds a `claude-cli` / `claude-opus-5` override for it, but the live `config/ai-defaults.json` override read on 2026-09-23 is `claude-cli` / `claude-sonnet-4-6`. Per-site `analyzer.ai_provider` / `analyzer.ai_model` in `site.yaml` win over both; neither site sets them today. It also appears in the tier-1 list in `framework/core/priority.py` and in the engine's `error_text` ("seo-analyzer returned non-zero"). |

## What the analyzer does

Both copies do the same thing. It reads the run dir that the collector
produced, scores opportunities, and writes the canonical
`recommendations.json`:

1. **Snapshot**: build `snapshot.json` from GSC 90d, GA4 28d, DB stats and optional
   Ads files under `data/`.
2. **Short-circuit**: if the snapshot signature matches the prior run, or the prior
   run was less than `SEO_MIN_RERUN_HOURS` ago, replay the prior
   `recommendations.json` with `metadata.short_circuited=true` and stop.
3. **Compare and score**: write `comparison.json` against the prior snapshot, and
   score the prior run's declared goals into `goal-progress.json`. It mirrors each
   score to `record_goal_progress()` on `<site>-seo-opportunity-agent`, or on the
   agent in `reporter.dashboard.agent_id`.
4. **Deterministic rule passes**: striking distance, zero-click, indexing fixes,
   conversion path, schema completeness, on-page, FAQ quality, freshness,
   GSC-coverage, and more. Recs already shipped or skipped in prior runs are
   deduped.
5. **LLM audit pass** (`llm_audit.py`): runs in batches of 4 pages. Only
   check_ids in `ALL_CHECK_IDS` (a module constant, not an env var; rendered
   into the prompt as `SEO_AUDIT_CHECKLIST`) survive. LLM recs are appended after the rule recs, deduped by
   `(url, llm_check_id)`, and the list is then hard-capped at
   `analyzer.max_recs_per_run`.
6. **Handoff tagging**: every rec gets `work_type` and `handoff_target` via
   `framework.core.work_types.handler_for()` plus `site.yaml` `handoff_routes` /
   `site_handler_overrides`.
7. **Write** `recommendations.json` and `goals.json`.

For the full rule and check-id catalog, see the engine README section
[Recommendation types](../seo-opportunity-agent/README.md#recommendation-types).

## Configuration (read by both copies)

| Env / knob | Default | Meaning |
|---|---|---|
| `SEO_AGENT_CONFIG` | (required) | path to the site's `site.yaml` (read by `shared.site_config.load_config_from_env`) |
| `SEO_DISABLE_LLM_AUDIT` | unset | `1` skips the LLM audit pass |
| `SEO_DISABLE_UNCHANGED_SHORTCIRCUIT` | unset | `1` always runs the rule passes |
| `SEO_MIN_RERUN_HOURS` | `6` | min-rerun gate used by the short-circuit |
| `SEO_DISABLE_HANDLED_DEDUPE` | unset | `1` re-proposes recs already handled in prior runs |
| `GSC_INSPECT_STATE_DIR` | `~/.reusable-agents/gsc-coverage-auditor` | where the GSC coverage-auditor output is read from |
| `analyzer.max_recs_per_run` | `12` | final rec cap |
| `analyzer.max_llm_audit_pages` | `30` | pages sent to the LLM audit |
| `analyzer.primary_objective` | `top5-rank` | passed into the LLM audit prompt |

## Standalone use

This is only for a collector pipeline of your own that produces the standard
run-dir layout:

```bash
# Azure-backed run dir (agents/<agent-id>/runs/<run-ts>/ in framework storage)
SEO_AGENT_CONFIG=my-site.yaml python3 analyzer.py --agent-id <agent-id> --run-ts <ts>

# Legacy local-FS run dir (latest run when --run-ts is omitted)
SEO_AGENT_CONFIG=my-site.yaml python3 analyzer.py --run-ts <ts>
```

For anything else, use `seo-opportunity-agent`. It runs the full
collector → analyzer → finalize chain under one `run_ts`. The engine README's
"Manual operations" section shows how to re-run just the analyzer phase against
an existing run.
