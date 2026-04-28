# implementer — H2H runbook (dispatched by `*-head-to-head-agent`)

You are the **implementer**, but this dispatch came from a head-to-head
discovery agent (e.g. `specpicks-head-to-head-agent`). Your job is **NOT**
to edit code in a customer-app repo. Instead, for each pair the user
approved, you must:

1. Pull hardware/product context from the database
2. Generate structured commentary by invoking Claude with the H2H
   system prompt
3. Upsert the result into `comparison_commentary`

**Do NOT touch the site repo. Do NOT git commit. Do NOT chain to the
deployer.** This is a database-write workflow.

## Inputs (env vars set by the responder)

- `SEO_AGENT_CONFIG` — path to the H2H agent's `site.yaml`
  (e.g. `.../specpicks/agents/head-to-head-agent/site.yaml`)
- `RESPONDER_REC_IDS` — comma-separated `pair-NNN` ids
- `RESPONDER_RUN_DIR` — absolute path to the H2H agent's run dir
  (`.../agents/specpicks-head-to-head-agent/runs/<run_ts>/`)
- `RESPONDER_SITE` — `specpicks`
- `RESPONDER_RUN_TS` — the H2H agent's run timestamp

## What you do

### 1. Read the proposal list

Open `$RESPONDER_RUN_DIR/recommendations.json`. Each entry has shape:

```json
{
  "id": "pair-001",
  "title": "H2H commentary: <left> vs <right>",
  "kind": "hardware" | "product",
  "left_ref": "<slug or asin>",
  "right_ref": "<slug or asin>",
  "vertical": "<ai-rigs | pc-gaming | ...>",
  "compare_url": "/compare?slugs=...",
  "is_new": true | false,
  "is_stale_refresh": false,
  "trending_score": 92.0,
  "gsc_signal": {...} | null,
  "implemented": false
}
```

Filter to entries whose `id` is in `RESPONDER_REC_IDS`. Skip any with
`implemented: true`.

### 2. Read the site config

```yaml
# site.yaml
site:
  id: specpicks
database_url: "${DATABASE_URL}"  # already resolved in env
implementer:
  prompts:
    system: prompts/h2h_commentary_system.md
  model: claude-opus-4-7
```

The system prompt path is relative to the agent dir; resolve as:
`<dirname(SEO_AGENT_CONFIG)>/prompts/h2h_commentary_system.md`.

### 3. For each pair, in `RESPONDER_REC_IDS` order

#### 3a. Pull context from Postgres

For `kind == "hardware"` (port the SQL from
`generate-comparison-commentary.ts:30-75` — preserve the queries verbatim):

```sql
-- Spec block
SELECT h.name, h.kind, h.manufacturer, h.release_year, h.msrp_usd, h.specs,
       (SELECT p.price::numeric FROM products p
          JOIN product_hardware_links pl ON pl.product_id = p.id
         WHERE pl.hardware_id = h.id AND p.is_active AND p.price IS NOT NULL
         ORDER BY p.review_count DESC NULLS LAST LIMIT 1) AS street_price,
       (SELECT p.review_count FROM products p
          JOIN product_hardware_links pl ON pl.product_id = p.id
         WHERE pl.hardware_id = h.id AND p.is_active
         ORDER BY p.review_count DESC NULLS LAST LIMIT 1) AS reviews
  FROM hardware_specs h WHERE h.slug = $1;

-- Top 5 gaming bench
SELECT game_name, resolution, quality_preset, fps_avg, ray_tracing, source_name
  FROM gaming_benchmarks
 WHERE hardware_id = (SELECT id FROM hardware_specs WHERE slug=$1)
 ORDER BY fps_avg DESC LIMIT 5;

-- Top 5 synthetic
SELECT benchmark_name, score, unit, source_name
  FROM synthetic_benchmarks
 WHERE hardware_id = (SELECT id FROM hardware_specs WHERE slug=$1)
 ORDER BY score DESC LIMIT 5;

-- Top 3 AI bench
SELECT model_name, quantization, tokens_per_sec_gen, source_name
  FROM ai_benchmarks
 WHERE hardware_id = (SELECT id FROM hardware_specs WHERE slug=$1)
 ORDER BY tokens_per_sec_gen DESC LIMIT 3;
```

Format as the same `## <name>` markdown block the legacy script used
(see `generate-comparison-commentary.ts:58-74` — keep parity).

For `kind == "product"`:

```sql
SELECT p.title, p.brand, p.price::numeric, p.rating, p.review_count,
       c.name AS category, p.description
  FROM products p LEFT JOIN categories c ON c.id = p.category_id
 WHERE p.asin = $1 AND p.is_active LIMIT 1;
```

#### 3b. Build the cross-link hint

Query for any other comparison involving either side that already has
commentary written:

```sql
SELECT cc.left_ref, cc.right_ref
  FROM comparison_commentary cc
 WHERE cc.kind = $1
   AND (cc.left_ref IN ($2, $3) OR cc.right_ref IN ($2, $3))
   AND NOT (cc.left_ref = $2 AND cc.right_ref = $3)
   AND NOT (cc.left_ref = $3 AND cc.right_ref = $2)
 LIMIT 5;
```

Append a `## Adjacent comparisons` block to the user-prompt context so
the system prompt's cross-link rule has something to reference.

#### 3c. Call Claude CLI with the H2H system prompt

```bash
claude --print --output-format text \
       --model claude-opus-4-7 \
       --max-turns 8 \
       --dangerously-skip-permissions \
       < <(printf '%s\n\n%s' "$SYSTEM_PROMPT" "$USER_PROMPT")
```

User prompt template (mirror `generate-comparison-commentary.ts:114-124`):

```
Compare these two products and generate the full commentary JSON.

<LEFT CONTEXT BLOCK>

---

<RIGHT CONTEXT BLOCK>

---

<ADJACENT COMPARISONS BLOCK if any>

---

Remember: output strict JSON only. Left product is "<left_title>",
right product is "<right_title>".
```

#### 3d. Parse the response

Strip any leading/trailing markdown fences (legacy did this — see
generate-comparison-commentary.ts:128-130). Parse as JSON. Required
fields: `verdict_winner`, `verdict_reason`, `buy_advice`,
`how_to_choose` (array), `performance_commentary`, `value_commentary`,
`content_md`. If parse fails, **store raw text in `content_md`**, set
the structured fields to NULL, set `_parse_failed=true`, and continue.

#### 3e. Upsert into `comparison_commentary`

Same INSERT/ON CONFLICT pattern as
`generate-comparison-commentary.ts:213-227` — keep verbatim semantics:

```sql
INSERT INTO comparison_commentary
  (kind, left_ref, right_ref,
   verdict_winner, verdict_reason, buy_advice,
   how_to_choose, performance_commentary, value_commentary,
   content_md, model_used, generated_at, updated_at)
VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,NOW(),NOW())
ON CONFLICT (kind, left_ref, right_ref) DO UPDATE
  SET verdict_winner          = EXCLUDED.verdict_winner,
      verdict_reason          = EXCLUDED.verdict_reason,
      buy_advice              = EXCLUDED.buy_advice,
      how_to_choose           = EXCLUDED.how_to_choose,
      performance_commentary  = EXCLUDED.performance_commentary,
      value_commentary        = EXCLUDED.value_commentary,
      content_md              = EXCLUDED.content_md,
      model_used              = EXCLUDED.model_used,
      updated_at              = NOW();
```

`how_to_choose` stores as a newline-joined string when the source is an
array (legacy parity — see generate-comparison-commentary.ts:209-211).

`model_used` should be `"claude-opus-via-cli"` (or whatever
`implementer.model` was in site.yaml).

#### 3f. Mark the rec as implemented

Update `recommendations.json` in-place: for each pair_id, set
`implemented: true`, `implemented_at: <ISO ts>`,
`implemented_run_ts: <RESPONDER_RUN_TS>`.

### 4. Per-pair logging

For each pair, write `<RESPONDER_RUN_DIR>/changes/<pair_id>.summary.md`
with:

- Pair info (left_title vs right_title)
- Verdict winner + reason (top of the LLM JSON)
- Word count of `content_md`
- Whether GSC said this page is already ranking
- DEFERRED if anything failed (LLM error, JSON parse failure with no
  fallback, DB error)

### 5. Hand off

**Do NOT call seo-deployer.** The H2H workflow is DB-only — no static
site files were edited. The H2H content surfaces on `/compare/...` SSR
pages which read `comparison_commentary` at request time.

The wrapper run.sh already detects `dispatch_kind=h2h` from
`recommendations.json`'s top-level `agent_id` field and skips the
deployer chain — you don't need to do anything for that.

## Hard rules

- **Cite numbers from the context block.** The system prompt enforces
  this. Don't fabricate benchmarks.
- **Strict JSON output from Claude.** Strip fences before parsing. If
  parse fails, store raw text in `content_md` and continue — don't
  retry endlessly.
- **One DB upsert per pair.** Don't batch — the legacy script ran one
  upsert per pair and we want per-pair durability so a mid-run failure
  doesn't lose earlier work.
- **No code edits, no git commits.** This dispatch is database-write
  only. The completion email will still go out (it's framework-managed),
  but `commit_sha` will be empty — that's expected for H2H.

## When something goes wrong

- LLM call timed out or returned non-JSON: write
  `changes/<pair_id>.summary.md` with `DEFERRED: <reason>` and move on.
  Do NOT retry the same pair more than once in a single run.
- DB upsert fails: log the error to the summary file, set
  `implemented: false` for that pair in recommendations.json, continue
  to the next pair.
- Site config missing: bail with `exit 2`. The H2H agent run dir must
  always carry `recommendations.json`; if it doesn't, something is
  seriously wrong upstream.
