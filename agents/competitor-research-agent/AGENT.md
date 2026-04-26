# Competitor Research Agent — Runbook

## Purpose

For each configured site, identify what similar / competitor websites do
and recommend:

1. **Parity gaps** — features competitors have that we don't, ranked by
   how widely they're adopted and how directly they match our site's
   purpose.
2. **Competitive advantages** — features we could build that no
   competitor has yet, or that we could do meaningfully better. These
   are speculative by nature (`tier=experimental` by default).
3. **UX improvements** — patterns competitors use that improve onboarding,
   conversion, or retention that we should consider.

Output is ranked `recommendations.json` + an email with rec-id reply
syntax. Top-rated recs become eligible for auto-pilot once the agent
has a track record (`auto_implement: true` in site config).

## Inputs

- `$COMPETITOR_RESEARCH_CONFIG` — path to per-site YAML.
- `~/.reusable-agents/competitor-research-agent/runs/<site-id>/<UTC-ts>/` —
  written each run.

## Process

1. **Build competitor list:**
   - Use `competitors.seed_domains` from config (preferred — gives the
     agent a curated starting point).
   - If empty, ask the LLM to brainstorm `max_competitors` competitor
     domains based on `site.what_we_do`. Save these in
     `competitors.json` for the run so subsequent runs can re-use.
2. **Fetch competitor pages:**
   - Homepage + a few key pages (auto-discovered: `/features`, `/pricing`,
     `/products`, top sitemap entries — capped at `max_pages_per_competitor`).
3. **Fetch our site:**
   - Same crawl as progressive-improvement-agent's, but smaller
     (homepage + sitemap roots only — we want a "what do we do today"
     snapshot, not a deep audit).
4. **Extract features per competitor:**
   - LLM call: "what discrete features does this competitor offer?
     Return a JSON list."
5. **Compare:**
   - LLM call: given our feature list + each competitor's feature list,
     return parity-gap, competitive-advantage, and UX-improvement
     recommendations.
6. **Score + tier** (same as progressive-improvement-agent):
   - `tier=auto` for high-confidence + meaningful severity
   - `tier=review` for medium confidence
   - `tier=experimental` for speculative (most "competitive advantage" recs land here)
7. **Email + reply gating** (same convention as the SEO suite +
   progressive-improvement-agent).

## Outputs

```
runs/<site>/<UTC-ts>/
  competitors.json             # which sites we used + how we found them
  features-ours.json           # what features we extracted from our site
  features-theirs.json         # per-competitor feature lists
  recommendations.json         # validated against quality-recommendations.schema.json
  email-rendered.html
```

## Auto-implement caveats

Auto-tier recs from this agent should be **rare**. By default a parity-gap
rec is at most `tier=review` because adding a "feature" is rarely as
mechanical as fixing a typo or broken page. The LLM is instructed to set
`tier=auto` only for: very narrow, unambiguous feature additions (e.g.,
"add JSON-LD product schema — competitor X has it, we don't, schema
content is fully derivable from our existing data").

## Reply syntax

Same as progressive-improvement-agent. Two selection modes:

**By rec id:**

```
implement rec-001 rec-005
skip rec-002
modify rec-003: build the simpler version, no admin UI
merge rec-004 rec-006
```

**Bulk by tier or severity:**

```
implement all
implement auto
implement high                # all severity=high recs
implement critical and high   # multiple filters union
skip experimental
```

Filters: `all` | `auto` | `review` | `experimental` | `critical` | `high` |
`medium` | `low`. Combine with `and` / `+` / commas. Mix with explicit
rec ids on the same line.
