# seo-analyzer (legacy standalone)

> **Heads up — active code has moved.**
>
> This directory predates the unified
> [`seo-opportunity-agent`](../seo-opportunity-agent/) pipeline. The
> production analyzer now runs from
> `../seo-opportunity-agent/lib/analyzer/analyzer.py` as phase 2 of
> that agent's `AgentBase` flow.
>
> The two `analyzer.py` files are kept in sync but **commit changes
> only to `lib/analyzer/analyzer.py`** going forward. This standalone
> copy survives so external scripts that imported it as a library
> still work; we'll retire it once the last caller migrates.

## Why two copies?

Before the consolidation, the SEO chain ran as three separate
systemd-driven scripts (`seo-data-collector` → `seo-analyzer` →
`seo-reporter`) glued together by per-site `run.sh` files. The
unified pipeline replaces that with a single `AgentBase` driver
(`seo-opportunity-agent/agent.py`) that subprocesses each phase
script. The phase scripts moved into `lib/`; the standalone dirs
were left in place to avoid breaking imports during the transition.

## What the analyzer does

Reads the data dir produced by the collector, scores opportunities,
and writes the canonical `recommendations.json`. Two passes:

1. **Deterministic rule passes** (in `analyzer.py`) — striking
   distance, zero-click, indexing fixes, conversion-path, schema
   completeness, on-page, FAQ quality, freshness, affiliate-tag leak,
   revenue-focus PDP audit, etc.
2. **LLM audit pass** (in `lib/analyzer/llm_audit.py`) — adaptive,
   whitelist-gated by `SEO_AUDIT_CHECKLIST` so SEO experts can audit
   exactly what the agent looks for. Disable with
   `SEO_DISABLE_LLM_AUDIT=1`.

Full rule + check-id catalog: see the
[engine README](../seo-opportunity-agent/README.md#goals--recommendation-types).

## Standalone use

If you have your own collector pipeline producing the standardized
run-dir layout and just want recommendations out of it:

```bash
SEO_AGENT_CONFIG=my-site.yaml python3 analyzer.py --run-ts <ts>
```

For everything else, use
[`seo-opportunity-agent`](../seo-opportunity-agent/) which gives you
the full collector → analyzer → finalize chain under one run-ts.
