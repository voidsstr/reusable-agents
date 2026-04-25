# seo-analyzer

Reads the data dir produced by **seo-data-collector**, scores opportunities, and writes
the canonical `recommendations.json` (the contract for everything downstream).

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
