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

#### 3a-bench. Benchmark coverage check + research proposal

After fetching the benchmarks above for BOTH sides, count what you got:
each side should have ≥3 gaming, ≥3 synthetic, ≥1 AI benchmark to
write a credible H2H. If either side falls short, the comparison page
will look thin — the user expects numbers, not vibes.

For each missing benchmark category on each side:

1. **Try harder lookup first** — query `gaming_benchmarks` /
   `synthetic_benchmarks` / `ai_benchmarks` with looser predicates:
   match by `hardware_id` OR by hardware name in `source_name`,
   accept lower-quality scores (`source_name LIKE 'manufacturer%%'`),
   etc. The strict-tier query above filters aggressively; second-pass
   loosens that.

2. **Propose new benchmark targets** when the looser query still
   returns 0 rows. Write to `benchmark_research_targets` so the
   benchmark-research-agent picks them up on its next run:

   ```sql
   INSERT INTO benchmark_research_targets (
       hardware_slug, hardware_id, category, priority,
       reason, requested_by, requested_at, status
   ) VALUES (
       $1, $2, 'gaming' | 'synthetic' | 'ai',
       'high',  -- H2H needs it for a public page
       'H2H pair art-NNN missing <category> bench for <name>',
       'specpicks-head-to-head-agent',
       now(), 'pending'
   ) ON CONFLICT (hardware_slug, category) DO UPDATE SET
       priority = 'high',
       requested_at = now(),
       status = 'pending'
   RETURNING id;
   ```

3. **Use third-party benchmarks inline** as a fallback. Hit
   techpowerup.com, anandtech.com, tomshardware.com via curl + parse
   for the relevant numbers IF the proposed-benchmark gap is critical
   for this pair (e.g. you literally can't write the verdict without
   it). Cite the source explicitly in the commentary.

4. **NEVER fabricate**. If after 1+2+3 you still don't have numbers,
   write the section as "Insufficient benchmark coverage — see
   pending research target #<id>" and DEFER the rec rather than
   ship a thin commentary.

The implementer's eventual output should ALWAYS include real numeric
data for both sides — that's the whole point of the H2H page format.

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
`implemented_run_ts: <RESPONDER_RUN_TS>`,
`shipped: true`, `shipped_at: <ISO ts>`, `shipped_via: "db-insert"`,
`public_url: "https://specpicks.com/compare?slugs=<left_ref>,<right_ref>"`.

Also write `<source_run_dir>/applied-recs.json`:

```python
import json, pathlib
applied = pathlib.Path(source_run_dir) / "applied-recs.json"
applied.write_text(json.dumps({
    "applied_rec_ids": [pair_id_1, pair_id_2, ...],
    "method": "h2h-commentary-upsert",
    "public_urls": { pair_id_1: "https://specpicks.com/compare?slugs=...",
                       pair_id_2: "..." },
}, indent=2))
```

Without `applied-recs.json` the dispatcher marks the run "paused" because
there's no git commit to detect.

#### 3g. MANDATORY: write per-pair verification.json

For each pair, write
`<source_run_dir>/verifications/<pair_id>.json`:

```json
{
  "rec_id": "pair-001",
  "rec_type": "h2h-comparison",
  "generated_at": "<iso ts>",
  "generated_by": "implementer",
  "explanation": "1-2 sentences saying you verify by querying /api/compare/commentary, which is the same DB-backed API the SPA hydrates from.",
  "script_js": "async function verify({ proxyFetch }) { const r = await proxyFetch('https://specpicks.com/api/compare/commentary?kind=<kind>&left_ref=<left_ref>&right_ref=<right_ref>'); if (!r.ok) return { ok:false, evidence:{ http_status:r.status }}; const obj = JSON.parse(r.body||'{}'); const c = obj.commentary; if (!c) return { ok:false, evidence:{ reason:'commentary row missing' }}; return { ok: !!(c.verdict_winner && (c.buy_advice||'').length>50), evidence:{ verdict_winner: c.verdict_winner, buy_advice_length:(c.buy_advice||'').length, updated_at: c.updated_at }}; }"
}
```

The dashboard's "Verify" button on the shipped tab fetches this doc and
runs `script_js` in a sandbox. The script must use `proxyFetch` (not
browser fetch — the dashboard runs cross-origin), return
`{ ok: boolean, evidence: object }`, and check the API endpoint NOT the
SPA `/compare?slugs=...` page (the page is client-rendered and contains
no product names in its SSR shell).

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
