# seo-analyzer

Reads the data dir produced by **seo-data-collector**, scores opportunities, and writes
the canonical `recommendations.json` (the contract for everything downstream).

> **Two-pass analysis (NEW):** the agent runs both a deterministic pass
> (everything in `analyzer.py`) and an **LLM-driven adaptive pass**
> (`llm_audit.py`). The LLM pass uses a SPECIFIC, VERIFIABLE checklist
> of evolving SEO best practices so SEO experts can audit what the agent
> looks for. Disable with `SEO_DISABLE_LLM_AUDIT=1`.

## Why two passes

Search-engine signals are a moving target — Core Web Vitals weight
grew, AI-search citation factors emerged (Perplexity / ChatGPT / Google
AI Overviews), schema-markup support shifts. A deterministic analyzer
can only flag things its code knows about. The LLM pass closes that
gap, but ONLY by checking against an explicit list
(`SEO_AUDIT_CHECKLIST` in `llm_audit.py`) so an SEO expert can validate
the agent isn't making things up.

The two passes' recs are de-duplicated by `(url, llm_check_id)` before
being written.

## What the LLM pass checks

Every check has a stable `check_id` so you can audit the agent's
behavior:

| Category | Example check_ids |
|---|---|
| Title + meta | `meta-title-length`, `meta-title-keyword`, `meta-description-cta`, `meta-description-missing` |
| Headings + content | `h1-missing`, `h1-multiple`, `heading-hierarchy`, `content-thin` |
| Structured data | `schema-product-missing`, `schema-faqpage-missing`, `schema-incomplete`, `schema-invalid` |
| E-E-A-T | `eeat-author-missing`, `eeat-publish-date-missing`, `eeat-citations-missing` |
| Internal linking | `link-orphan`, `link-anchor-generic`, `link-redirect-chain` |
| Mobile + CWV | `mobile-viewport-missing`, `cwv-render-blocking`, `cwv-image-no-dimensions` |
| Indexing | `indexing-canonical-self`, `indexing-soft-404`, `indexing-pagination-rel` |
| URLs + images | `url-non-descriptive`, `image-alt-missing`, `image-filename-non-descriptive` |
| AI search (GEO) | `geo-direct-answer-missing`, `geo-faq-missing`, `geo-llms-txt-missing` |
| Conversion | `cta-missing`, `cta-weak`, `trust-signals-missing` |

Full list with explanations: `llm_audit.py` (`SEO_AUDIT_CHECKLIST`).
Hallucinated check_ids are FILTERED OUT before recs are emitted — the
agent can only flag from the whitelist.

## Goals reflect the checklist

Each per-site SEO agent has 9 goals split across deterministic +
adaptive concerns: top-5 keywords, MoM revenue, indexing issues, CTR,
schema coverage, EEAT baseline, CWV pass, AI-search readiness,
internal-linking. Goal directives reference the specific check_ids the
agent should run for that goal — see `install/seed-default-goals.sh`.

## Configuring the LLM provider

The LLM pass uses the framework's AI provider config. The agent_id for
provider resolution is `seo-analyzer`. Override per-site in YAML:

```yaml
analyzer:
  ai_provider: anthropic       # optional, falls back to default
  ai_model: claude-opus-4-7    # optional, falls back to provider default
```

Or set the global `seo-analyzer` override at
`http://localhost:8091/providers`.

---


Deterministic Python — runs without Claude. The implementer agent is where
LLM-driven code editing happens; the analyzer just identifies *what* should
change, not *how*.

## What it does

1. **Snapshot** current metrics → `snapshot.json` (impressions, clicks, position,
   per-query positions, position buckets, revenue KPIs from the configured events).
2. **Compare** to the prior snapshot → `comparison.json` (deltas, % changes).
3. **Score prior-run goals** → `goal-progress.json` (achieved / improving / stalled).
4. **Identify recommendations**, ranked by:
   - Top-5 rank targets (queries pos 6-50, ≥3 impr/90d) — primary focus
   - Striking-distance queries (pos 4-10) — close to top-3
   - Indexing fixes (broken slugs, sitemap gaps from data scan)
   - CTR fixes (high-impression zero-click)
   - Conversion-path tightening (revenue events down vs prior)
5. **Declare goals** for the top recs → `goals.json` (top-5 targets, revenue lifts).
6. **Write** the run's `recommendations.json` — used by reporter, implementer, dashboard.

## Usage

```bash
# Default: analyze the latest run for the configured site
SEO_AGENT_CONFIG=my-site.yaml python3 analyzer.py

# Or analyze a specific run
SEO_AGENT_CONFIG=my-site.yaml python3 analyzer.py --run-ts 20260425T140245Z
```

## Output

In the run dir (`<runs_root>/<site>/<run-ts>/`):

- `snapshot.json` — full metric record
- `comparison.json` — deltas vs prior snapshot
- `goal-progress.json` — prior run's goals scored against current snapshot
- `goals.json` — this run's declared goals
- `recommendations.json` — ranked list of actionable recs (the contract)
- `run-summary.md` — appended human-readable digest

## Recommendations schema

See [`../shared/schemas/recommendations.schema.json`](../shared/schemas/recommendations.schema.json).
Each rec includes: `id`, `type`, `priority`, `title`, `rationale`, expected
impact, implementation outline, data refs.

## Reuse: standalone

The analyzer is the most portable piece — point it at any data dir produced
by a compatible collector and it'll write recommendations. Useful if you
have your own SEO data pipeline and just want recs out of it.
