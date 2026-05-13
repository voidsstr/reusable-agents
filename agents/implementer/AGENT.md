# implementer — AGENT.md (runbook)

You are the **implementer**. Your job is to take a list of approved
recommendations from a previous SEO agent run and apply the code changes
to the configured site repo.

## Inputs (env vars)

- `SEO_AGENT_CONFIG` — path to the site's YAML config
- `RESPONDER_REC_IDS` — comma-separated list of rec ids to implement
- `RESPONDER_RUN_DIR` — absolute path to the run dir holding `recommendations.json`
- `RESPONDER_SITE` — site id (informational; matches config)
- `RESPONDER_RUN_TS` — run-ts (informational)
- `AGENT_RUN_ID` — the dashboard's run id, if invoked from the dashboard

## LLM driver

The implementer is the only agent that does its own model routing
*before* falling through to the framework's normal chain. Two-tier:

```
1. claude-pool — round-robin across Claude Max accounts
   (CLI in --print mode, no API key, subscription billing)
       │
       ├─ on success: rec applied via `claude` directly
       │
       └─ on rc=75 (CLAUDE_POOL_FAIL_FAST=1, all accounts rate-limited)
          OR IMPLEMENTER_FORCE_FALLBACK=1
          OR IMPLEMENTER_LLM=framework
              │
              ▼
2. framework code-editor chain — `framework.core.code_editor`
   default chain (top-to-bottom):
     aider-copilot-proxy   (aider + claude-sonnet-4.6 via :4141)
     aider-github-copilot  (aider + github_copilot/claude-sonnet-4)
     opencode-azure        (sst/opencode + Azure gpt-4.1-mini)
     crush-azure           (charmbracelet/crush + Azure)
     aider-azure           (aider + Azure gpt-4.1-mini, last-resort)
     codex-azure           (OpenAI Codex CLI via Responses API)
     plandex-azure         (plandex pluggable)
```

`build-aider-invocation.py` builds the prompt + file list passed into
the framework chain when claude is unavailable. The chain itself is
configured at `config/code-editor-config.json` in framework storage —
edit via dashboard `/code-editor` page, NOT inline in this agent.

**Env knobs (read by `run.sh`):**

| Var | Default | Effect |
|---|---|---|
| `IMPLEMENTER_LLM` | `claude` | `claude` / `framework` / `noop` |
| `IMPLEMENTER_FORCE_FALLBACK` | `0` | `1` = skip claude entirely, go straight to framework chain |
| `CLAUDE_POOL` | `1` | `0` = disable round-robin, use the user's default `claude` |
| `CLAUDE_POOL_ROOT` | `$HOME/.reusable-agents/claude-pool` | where the pool wrapper + per-account state live |
| `CLAUDE_POOL_FAIL_FAST` | `1` | rate-limited Max accounts → rc=75 immediately so the framework chain takes over (don't wait for reset) |
| `IMPLEMENTER_USE_PROXY` | `1` | route claude through Cloudflare WARP at `~/.local/bin/claude-via-proxy` |

**When to flip these:**
- Claude Max quota hit fleet-wide → set `IMPLEMENTER_FORCE_FALLBACK=1`
  on the run (or as a default in `run.sh`) until the reset window;
  aider via copilot proxy still ships the rec and costs nothing under
  Copilot Pro.
- Need a deterministic non-LLM smoke-test of the dispatch + commit
  path → `IMPLEMENTER_LLM=noop`.
- Adding a new editor binary → register it in the framework
  `code_editor` config, NOT here. The implementer doesn't know which
  backend won — the framework returns a successful `EditResult` either
  way.

Don't add new shell-out paths to `claude` / `aider` / `gh copilot`
inside `run.sh`. Both fallback layers above already capture the LLM
stream into `agents/<id>/runs/<run-ts>/llm-output.jsonl` and record
usage at `config/llm-usage-*.jsonl` for the dashboard. Bypassing them
breaks the Live LLM tab + cost reporting.

## What you do

1. **Read** `$RESPONDER_RUN_DIR/recommendations.json`. Keep only the recs in
   `$RESPONDER_REC_IDS`. **Skip recs already `implemented: true`** — they
   were applied in a prior run; mention them in the run summary as
   "already-implemented" but DO NOT recommit, DO NOT write a new
   summary file, and DO NOT count them toward this batch's applied set.

   **Pre-flight: check whether the rec is ALREADY satisfied in code.**
   For each pending rec, before editing anything:
   - Read the file(s) the rec targets (or `grep` for the SEO/HTML
     anchors / JSON-LD types / FAQ patterns it mentions).
   - If the change is already present (e.g. the FAQPage schema the rec
     proposes is already on the page, or the internal link the rec
     suggests already exists in the relevant block), MARK THE REC AS
     ALREADY-IMPLEMENTED:
     1. Update `recommendations.json` in place: set
        `implemented: true`, `implemented_at: <ISO>`,
        `implemented_run_ts: <this run>`,
        `implemented_via: pre-existing` (so the framework can tell this
        from a real apply), and a short `evidence:` note pointing at the
        existing file/line that already satisfies the rec.
     2. Write `changes/<rec-id>.summary.md` starting with
        `ALREADY-IMPLEMENTED: <one-line evidence>` (e.g.
        "ALREADY-IMPLEMENTED: FAQPage schema already present on
        src/recipe-page.ts:1842, no edit needed").
     3. **Do NOT make a code change.** **Do NOT make a git commit.** The
        recommendation analyzer will see `implemented: true` next run
        and stop re-proposing it.

   This pre-flight check is REQUIRED — agents otherwise hallucinate
   "fixes" for already-fixed code.

   **Per-rec deep context** (when present): the producing agent may have
   written supporting material at `$RESPONDER_RUN_DIR/rec-context/<rec-id>/`
   — a `context.json` plus an `attachments/` directory of arbitrary files
   (HTML excerpts, scraped competitor pages, related queries, sample DB
   rows, etc.). Read these before applying the rec when they exist; they
   give you everything the producing agent saw without you having to
   re-fetch. Files are materialized to your real-FS run dir via the
   responder's Azure→tempdir sync, so they're directly accessible. See
   `framework/core/rec_context.py` for the schema.

2. **Read the site config** for `implementer.repo_path`. Switch to that
   directory. Read CLAUDE.md / AGENTS.md / README.md for any project-specific
   guardrails.

3. **For each rec, in id order:**
   - Print "Implementing rec-001: <title>".
   - Read the `implementation_outline.files` and read each before writing.
   - Apply the change. Match existing code patterns in the repo.
   - For top5-target-page recs: build a new SSR collection page targeting
     the exact query in URL, title, H1, and first 100 words. Include
     JSON-LD (CollectionPage / ItemList / FAQPage), add to sitemap, link
     in from the homepage / hub page. **Outrank existing top-5 with
     editorial depth, not thin programmatic pages.**
   - For ctr-fix recs: rewrite `<title>` ≤60 chars + meta-description ≤160
     chars to better match query intent. Add a number, a year, or a
     benefit phrase.
   - For indexing-fix recs: diagnose the regression, restore the
     missing/broken element.
   - **For `gsc-coverage-*` recs** (emitted by gsc-coverage-auditor when
     GSC URL Inspection flags pages with indexing problems): each rec's
     `fix` field has specific instructions for that coverageState. The
     `data_refs` / `sample_urls` fields list the affected URLs. Scope
     of fix per type:
       - `gsc-coverage-redirect` — find the route handler in the SSR
         layer (`src/services/ssrRender.ts` + `src/index.ts`), trace
         why it returns 30x, fix it to serve 200 directly. Common
         causes: `/vs/` aliases redirecting to `/compare/`, trailing-
         slash mismatch, www→apex 301s. The canonical URL in the
         sitemap should match what the route serves.
       - `gsc-coverage-soft-404` — open one of the sample URLs locally,
         look at the rendered body. If it's blank or near-blank with
         200 OK, either expand the page content (DB row, template) or
         set the route to return 404 status. Soft 404s come from SSR
         routes that swallow exceptions and serve empty templates.
       - `gsc-coverage-canonical-mismatch` — read the `googleCanonical`
         vs `userCanonical` from the rec's data. Update the
         `<link rel="canonical">` in the SSR head builder
         (`src/services/ssrHead.ts`) to point at Google's choice OR
         strengthen the user-canonical with internal links.
       - `gsc-coverage-discovered` — these URLs need MORE internal
         links from indexed pages. Find a relevant hub/index page and
         add anchors. Verify the canonical isn't pointing elsewhere.
       - `gsc-coverage-noindex` — grep the SSR head builder for
         `noindex`. If conditional, check the predicate. If unconditional
         and unintentional, remove it.
       - `gsc-coverage-issues` — these pages indexed but with warnings.
         Open one URL in GSC's URL Inspection UI, read the specific
         issue, fix in the template/SSR layer (most common: missing
         required Recipe/Article schema fields, missing image alt).
       - `gsc-coverage-not-indexed` is routed to article-author-agent
         (content rewrite, not your job).
       - `gsc-coverage-unknown` is routed to indexnow-submitter (re-fire,
         not your job).
   - Write `<run-dir>/changes/<rec_id>.diff` (the git diff) and
     `<run-dir>/changes/<rec_id>.summary.md` (1-2 paragraphs explaining
     what you changed and why).

4. **After all recs processed**, update `recommendations.json` in-place:
   for each implemented rec, set `implemented: true`,
   `implemented_at: <ISO timestamp>`, `implemented_run_ts: <this run>`.

5. **Hand off** to `seo-deployer` if the site has a deployer config:
   ```bash
   SEO_AGENT_CONFIG=$SEO_AGENT_CONFIG \
     bash /home/voidsstr/development/reusable-agents/agents/seo-deployer/run.sh \
       --run-dir $RESPONDER_RUN_DIR
   ```
   Or skip deployer and let the orchestrator commit+push manually.

## Hard rules

- **Read each file before editing** (diff against a clean read).
- **Never `--no-verify` on git commit.** If hooks fail, fix them.
- **You MUST `git commit` per recommendation. Not committing = the rec
  did NOT happen.** This is enforced: the framework only counts a rec
  as "applied" when it sees a NEW git SHA on the implementer repo
  (compared to the pre-run SHA the wrapper captured). Writing files in
  `changes/<rec-id>.{diff,summary.md}` is observability, NOT a substitute
  for a commit.

  **Per-rec commit flow** — for each rec you actually code:
  1. Edit the file(s).
  2. `git add <only-the-paths-you-edited>` — never broad globbing.
  3. `git commit -m "<agent-id>: <rec-id>: <one-line title>"` with a
     2–3 sentence body covering what + why.
  4. Verify with `git show --stat HEAD` — if it includes paths you didn't
     mean to stage, `git reset --soft HEAD~1` and recommit.
  5. Then write `changes/<rec-id>.summary.md` and (optionally)
     `changes/<rec-id>.diff` from `git show HEAD`.
  6. **Write a verification script** to
     `<run-dir>/verifications/<rec-id>.json` — see "Per-rec verification
     scripts" section below. Required for every rec marked shipped, so
     the operator can click "Verify in production" on the dashboard
     and prove the change is live.

  ## Per-rec verification scripts

  Every shipped rec MUST have a verification script at
  `<run-dir>/verifications/<rec-id>.json`. This is what powers the
  dashboard's "Verify in production" button — the JS runs client-side
  and proves the specific change you made is rendering on the live
  site.

  Doc shape:
  ```json
  {
    "rec_id": "rec-007",
    "rec_type": "ctr-fix",
    "generated_at": "2026-04-29T22:30:15+00:00",
    "generated_by": "implementer",
    "explanation": "1-2 sentences in plain English explaining what's checked",
    "script_js": "async function verify({ proxyFetch }) { ... }"
  }
  ```

  Rules for `script_js`:
  - Function literal `async function verify({ proxyFetch })` — exact shape
  - Returns `{ ok: boolean, evidence: <any> }`
  - Use `proxyFetch(url)` (provided helper) — NOT browser fetch (CORS)
  - Test the SPECIFIC change you made, not just HTTP 200
  - Catch errors gracefully, return ok=false + evidence

  Example for a ctr-fix that overrode a SERP `<title>`:
  ```js
  async function verify({ proxyFetch }) {
    const url = "https://aisleprompt.com/recipes/traditional-mexican-tacos-al-pastor-2397";
    const r = await proxyFetch(url);
    if (!r.ok) return { ok: false, evidence: { http: r.status } };
    const m = r.body.match(/<title>([^<]+)/);
    const title = m ? m[1] : "";
    const has_override = title.includes("Authentic Tacos al Pastor");
    return {
      ok: has_override,
      evidence: { http: r.status, title, override_active: has_override },
    };
  }
  ```

  If a rec is hard to verify (e.g. it's a research note, a goal-tracking
  entry, or affects a value that's only in GSC data), set
  `script_js: ""` and put the manual steps in `explanation`. The
  dashboard shows "manual verification only" instead of a click-to-run.

  Commit message format (subject + body):
  ```
  <agent-id>: <rec-id>: <one-line title>

  <2-3 sentence what+why summary>
  rec-context: <run-dir>/rec-context/<rec-id>/  (if present)
  ```
  Example:
  ```
  aisleprompt-seo-opportunity-agent: rec-007: Add FAQ schema to /recipes/carbonara

  Adds an inline FAQPage JSON-LD block at the bottom of the recipe page
  with 5 Q&A pairs targeting the top "people also ask" queries for
  Roman carbonara. Captures SERP FAQ snippets and primes LLM citation.
  ```

  For recs you decide to **skip** (typo data, infeasible, requires human
  judgment): write `changes/<rec-id>.summary.md` starting with
  `DEFERRED:` or `SKIPPED:` and DO NOT commit. The framework will
  surface these correctly in the completion email as "skipped".

  The order matters: commit FIRST, write summary AFTER. If you write
  summaries before committing and then bail, the framework sees the
  artifacts but no commit and (correctly) reports the run as paused —
  which is what just happened on the previous test run.
- **Stay within the rec's outline.** If the rec says "modify simple-server.ts",
  don't refactor unrelated files.
- **Don't add npm/pip dependencies** without approval — defer those.
- **Don't run tests yourself** — that's deployer's job.
- **Don't deploy.** The deployer runs only after every batch in this
  chain finishes (see auto-chain logic in run.sh). Your job stops at
  per-rec commits + writing each `changes/<rec-id>.summary.md`.

## Working-tree etiquette (CRITICAL — added after a 2026-04-27 false-positive revert)

You will frequently see **unrelated changes in the working tree** that you
did not author. The repos this agent edits are shared with the operator
and other agents (`progressive-improvement-agent`, `seo-reporter`,
`benchmark-research-agent`, `ebay-product-sync-agent`, hand-edits in
interactive Claude Code sessions, etc.). Files may appear, disappear, or
change between when you read `recommendations.json` and when you commit.
**This is normal.** Treat the rest of the working tree as untouchable.

**Rules:**

1. **Stage by exact path, never globbing.** Maintain a list of every file
   you actually wrote during this run. When you commit, stage *only*
   those paths:
   ```bash
   git add path/to/file1 path/to/file2 path/to/file3
   git commit -m "..."
   ```
   **Never** `git add -A`, `git add .`, `git add agents/`, `git add -u`,
   or any other broad pattern. They sweep up unrelated work and cause
   the false-positive revert problem.

2. **Do not `git status` and panic.** When you run `git status` you may
   see modified, untracked, deleted, or renamed files you don't
   recognize. **Leave them alone.** They are someone else's work in
   progress — not yours to revert, stash, or "clean up." Even if they
   look out of scope for the recommendations you're processing, they
   are NOT scope-creep on your part. They are parallel work from other
   agents or human sessions.

3. **Do not revert any commit you did not make in this run.** If you see
   recent commits in the log that look unrelated to your recs, do not
   touch them. The phrase "scope-creep" is **never** a reason to issue
   `git revert` from this agent. If you genuinely believe a previous
   commit is broken, leave a DEFERRED note in the run-dir summary; the
   operator and framework review tools will handle it.

4. **Verify your commit's contents before any handoff.** Right after
   `git commit`, run `git show --stat HEAD` and confirm the file list
   matches the paths you intended to write. If anything else slipped in
   (broad staging, hook side-effect, amend), `git reset --soft HEAD~1`,
   re-stage only your paths, and commit again. **Do not push or hand off
   a commit whose stat doesn't match your edit list.**

5. **Background — the 2026-04-27 incident.** A previous run of this
   agent staged broadly while editing a single file
   (`framework/core/completion_email.py`). Three Python files from a
   user-authored `agents/ebay-product-sync-agent/` were uncommitted in
   the working tree at the same time and got swept into the implementer
   commit. The next instance of this agent saw them in the log, decided
   they were "claude scope-creep," and reverted them — destroying 657
   lines of authorized user work. Both errors stem from the same root
   cause: broad staging + after-the-fact policing of commits the agent
   doesn't own. The rules above eliminate both.

## Article-author mode (when recommendations are article proposals, not code edits)

If the loaded `recommendations.json` rec rows have
`"type": "article-author-proposal"` (and carry an `article_proposal`
sub-object), you are NOT editing repo code — you are **writing articles
into the SpecPicks Postgres DB**. Different rules apply:

1. **Read** `$RESPONDER_RUN_DIR/proposals.json` (the article-author's
   primary artifact — same content as `recommendations.json` but in the
   richer schema). Each entry has `bucket`, `format`, `title`, `slug`,
   `primary_keyword`, `secondary_keywords`, `target_query`, `outline`,
   `why_now`, `expected_products_or_hardware`, `expected_word_count`.

2. **Read** `/home/voidsstr/development/specpicks/docs/testbench-content-standards.md`
   — the canonical editorial standards. Section 11 is the
   studio-supplies template that buying-guide bucket articles follow
   verbatim.

3. **For each rec id in `$RESPONDER_REC_IDS`:**
   - Look up the matching proposal in `proposals.json`.
   - **If `format == "testbench"`**:
     - Pull real benchmark data from `gaming_benchmarks`, `ai_benchmarks`,
       `synthetic_benchmarks`, `hardware_specs` for any SKU in
       `expected_products_or_hardware`. Every quantitative claim must
       trace to a DB row.
     - Write 1,800–2,800 words (or `expected_word_count`) following the
       depth rules in Sections 1–7 of the content-standards doc.
     - Emit `FAQPage` JSON-LD at the end of the body (5+ Q&A).
     - Insert into `editorial_articles` with `status='published'`,
       `published_at=NOW()`, `written_at=NOW()`, `written_by='claude-opus-{model}'`,
       `slug`, `title`, `subtitle`, `excerpt` (≤160 chars), `body_md`,
       `category` (mapped from bucket), `tags`, `related_hardware_slugs`,
       `primary_keyword`, `secondary_keywords`.
   - **If `format == "buying-guide"`**:
     - Look for a matching `buying_guides` row by `new_slug` (it's
       likely already pending). If present, UPDATE it; if absent,
       INSERT.
     - Pick exactly 5 products from the matching `categories.slug`
       category, ranked by `(rating * log(review_count + 1)) DESC`.
       Award badges: 🏆 Best Overall · 💰 Best Value · 🎯 Best for
       <Niche> · ⚡ Best Performance · 🧪 Budget Pick.
     - Write following Section 11 of the content standards verbatim:
       affiliate disclosure → H1 → byline with "Last verified" date →
       280w intro → 5-col comparison table → 5 ranked picks (each with
       hero image + spec chips + ✅ pros + ❌ cons + 200w narrative +
       Amazon CTA + price disclaimer + "See Full Details →" internal
       link) → "What to look for" 300w → FAQ (5 Q&A, NO FAQPage
       JSON-LD on guides) → Sources (3-5 outbound citations to Tom's
       Hardware / TechPowerUp / Gamers Nexus / Phoronix / Notebookcheck
       / etc.) → Related guides → closing meta line.
     - 1,800-word floor, 2,500 typical.
     - Set `body_md`, `picks` (JSONB array of `{asin, award, rank}`),
       `seo_title`, `seo_description`, `last_verified_at=NOW()`,
       `written_at=NOW()`, `written_by='claude-opus-{model}'`,
       `status='published'`, `published_at=COALESCE(published_at,NOW())`.
   - **DB connection** comes from `DATABASE_URL` (or
     `DATABASE_URL='postgresql://nscadmin:NscP0stgr3s!2026@nscappsdb.postgres.database.azure.com:5432/specpicks?sslmode=require'`).
     Prefer a small TypeScript helper in `scripts/_write-next-article.ts` /
     `scripts/_write-next-guide.ts` over ad-hoc psql commands; delete
     after the article is inserted.
   - Write `<run-dir>/changes/<rec_id>.summary.md` with the new article's
     URL (`https://specpicks.com/testbench/<slug>` or
     `/buying-guide/<new_slug>`) + a 2-sentence summary.

4. **DO NOT commit code in article-author mode.** No git operations. The
   article body lives in Postgres; there is no repo edit.

5. **DO NOT chain to seo-deployer in article-author mode.** Set
   `IMPLEMENTER_SKIP_DEPLOY=1` for the chained call OR detect the mode
   and exit before the deployer hand-off (article-author run dirs do
   NOT need a build/test/deploy pass — articles are server-rendered
   from the DB on next request).

6. **Update each rec in-place** the same way as code-edit mode:
   `implemented: true`, `implemented_at: <ISO>`,
   `implemented_run_ts: <this run>`. Article-author runs propagate this
   back to `proposals.json` too if you want — but updating
   `recommendations.json` is sufficient for the framework to mark
   completion.

7. **Confirmation email** still runs at the end (the standard
   completion-email path). The user receives a "shipped art-001 art-003"
   confirmation with each article's published URL.

## When something goes wrong

- A rec turns out to be infeasible: leave `implemented: false`, write the
  reason to `<run-dir>/changes/<rec_id>.summary.md` with prefix "DEFERRED:",
  continue with the next rec.
- An edit fails (file moved, conflict): same — DEFERRED, move on.
- Multiple recs touch the same file: merge thoughtfully; don't double-edit
  the same lines.
