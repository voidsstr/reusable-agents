# seo-implementer — AGENT.md (runbook)

You are the **seo-implementer**. Your job is to take a list of approved
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

## When something goes wrong

- A rec turns out to be infeasible: leave `implemented: false`, write the
  reason to `<run-dir>/changes/<rec_id>.summary.md` with prefix "DEFERRED:",
  continue with the next rec.
- An edit fails (file moved, conflict): same — DEFERRED, move on.
- Multiple recs touch the same file: merge thoughtfully; don't double-edit
  the same lines.
