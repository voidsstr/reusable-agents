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

## What you do

1. **Read** `$RESPONDER_RUN_DIR/recommendations.json`. Keep only the recs in
   `$RESPONDER_REC_IDS`. Bail with a no-op if any are already
   `implemented: true`.

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
- **One commit per implementer run** (covering all recs in this batch).
- **Stay within the rec's outline.** If the rec says "modify simple-server.ts",
  don't refactor unrelated files.
- **Don't add npm/pip dependencies** without approval — defer those.
- **Don't run tests yourself** — that's deployer's job.

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
