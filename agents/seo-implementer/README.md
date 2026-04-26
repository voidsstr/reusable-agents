# seo-implementer

Reads `recommendations.json` and a list of `rec_ids` (from `responses.json`,
the responder-agent, or a CLI arg) and applies the proposed code changes to the
configured repo.

**This is the only agent in the suite that needs Claude / an LLM** — the
others are deterministic Python. The implementer drives an LLM to write
the actual code edits, following each rec's `implementation_outline`.

## Inputs

- `<run-dir>/recommendations.json` — what was recommended
- `RESPONDER_REC_IDS` env var OR `--recs rec-001,rec-005` flag — which to
  implement
- `RESPONDER_RUN_DIR` env var OR `--run-dir <path>` — which run to act on
- Site config (`SEO_AGENT_CONFIG`) — repo path, branch

## What it does per rec

1. Reads the `implementation_outline` from the rec
2. Drives an LLM session with the runbook in [`AGENT.md`](AGENT.md)
3. Writes diffs to `<run-dir>/changes/<rec_id>.diff`
4. Writes a per-rec summary to `<run-dir>/changes/<rec_id>.summary.md`
5. Marks `rec.implemented = true` in `recommendations.json`

This is the only step that opens an LLM session. The rest of the suite
runs as plain scripts — useful if you want recommendations without
auto-coding.

## Usage

```bash
# Triggered by responder-agent — reads RESPONDER_* env
bash run.sh

# Manual trigger
SEO_AGENT_CONFIG=my-site.yaml ./run.sh --recs rec-001,rec-005 --run-dir ~/.reusable-agents/seo/runs/my-site/20260425T140245Z
```

## What this DOESN'T do

- Run tests (that's `seo-deployer`)
- Deploy (that's `seo-deployer`)
- Commit + push (orchestrator script does)

The implementer is *just* the code-writing step. Combining with deployer is
how you get the full daily-loop behavior.

## Reuse

For projects that don't want auto-coding (pure recommendation mode), don't run
this agent — `seo-data-collector + seo-analyzer + seo-reporter` will email
you actionable recs and you implement them yourself.
