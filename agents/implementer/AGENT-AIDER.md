# implementer — AGENT-AIDER.md (aider fallback runbook)

You are running in **aider fallback mode**. Claude Code's tool harness
was unavailable (Max-pool rate-limit) so the implementer delegated to
you. **Use ALL the tools you have.** Aider 0.86+ supports:

- File reads + edits (whole-file or diff)
- `/run <cmd>` — shell command execution (with `--yes-always` it
  auto-approves; output flows back into the chat context)
- `/web <url>` — fetch + scrape a URL into chat context
- Multi-file batched edits in a single response

**Treat this run exactly like Claude would treat it — same goals, same
quality bar, same hard rules.** The only differences are listed in the
"Aider-specific guardrails" section below. Everything else in
[`AGENT.md`](./AGENT.md) (the full Claude runbook) applies verbatim:

- The pre-flight ALREADY-IMPLEMENTED check
- Reading `recommendations.json`, filtering to `$RESPONDER_REC_IDS`
- Per-rec `changes/<rec-id>.{summary.md,diff}` artifacts
- Updating `recommendations.json` in place after applying
- Writing per-rec verification scripts to `verifications/<rec-id>.json`
- Working-tree etiquette (stage by exact path, don't policing other
  commits, don't `git revert` what you didn't author)
- The hand-off to `seo-deployer` if the site has a deployer config

**Read `AGENT.md` end-to-end now** — it's the source of truth. This
file just adds the aider-specific bits.

---

## Aider-specific guardrails (the ONLY differences from AGENT.md)

### 1. Package installs

When you encounter a missing dependency (e.g. `playwright` for scraping,
`psycopg2` for a DB query), **try to install it via `/run`** like Claude
would. But if the install fails (network, permissions, sandbox),
**don't fake your way around the missing dep.** Defer that single rec
to `skipped-recs.json` (schema below) with `reason: "missing-dep:<name>"`
and continue with the next rec. The next claude-pool reset will retry
under the full toolchain.

Specifically: **do NOT** `pip install --user` on top of the system
Python in a way that pollutes the host. If a `/run pip install ...`
returns rc != 0, log it and SKIP the rec — don't retry with broader
flags. The rec WILL re-run cleanly next pool window.

### 2. Database writes

When a rec needs `INSERT`/`UPDATE` against a Postgres / Azure SQL
instance:

- Do `/run` to invoke `psql` or `python3 -c "import psycopg2; ..."`
  exactly like Claude would. The implementer's wrapper has already
  exported `DATABASE_URL` for you when one is configured.
- **CRITICAL: write the SQL to a file first** (e.g.
  `<run-dir>/sql/<rec-id>.sql`), then `/run psql -f <file>`. This way
  every DB mutation is auditable + replayable.
- If `DATABASE_URL` isn't set OR psql isn't on PATH, defer the rec to
  `skipped-recs.json` with `reason: "no-db-tooling"`.

### 3. Live URL verification

When a rec says "verify on the live URL after deploy" — `/web` it like
Claude would `WebFetch`. **Do not skip the verification step just
because aider's web is a different code path.** Same depth required.

### 4. Verification JS scripts (`verifications/<rec-id>.json`)

Generate these the same way Claude does. The dashboard's "Verify in
production" button runs them via `proxyFetch`; the script content is
plain JS regardless of which agent wrote it. Match the schema in
AGENT.md exactly (`async function verify({ proxyFetch }) { ... }`).

### 5. The aider-mode commit signature

After all your edits, **don't `git commit` yourself**. The wrapping
shell script (`run.sh`) does a single bulk commit with a tagged
message:

```
implementer (aider-fallback): apply <rec-ids> for <agent>/<run-ts>
Mode: aider-fallback (claude-pool rate-limited)
```

This is so the framework's `git log` / dispatch tracker can attribute
the commit to "aider-mode" vs full-claude-mode. **Do `/run git status`
liberally to confirm what's staged**, but leave the actual `git
commit` to the wrapper.

If you DO need to commit yourself (e.g. a multi-step migration where
each step needs to be a separate commit), prefix the messages
identically:

```
implementer (aider-fallback, step 1/3): <rec-id>: <title>
```

### 6. Skipped recs (the only legitimate skip path)

For recs you CAN'T complete in this run — e.g. install failed, DB
unreachable, requires a tool aider doesn't have — write
`<run-dir>/skipped-recs.json`:

```json
{
  "skipped": [
    {
      "rec_id": "rec-007",
      "reason": "missing-dep:playwright (pip install failed in sandbox)",
      "skipped_via": "aider-fallback",
      "skipped_at": "<ISO>",
      "retry_when": "claude-pool reset"
    }
  ]
}
```

Do NOT touch `recommendations.json` for skipped recs. Leaving
`implemented` unset means the next claude-mode run picks them up.

### 7. Final summary

Same as AGENT.md, plus an `<run-dir>/changes/aider-summary.md` with:

- Number of recs applied / already-done / skipped
- For skipped: rec id + reason + which tool it needed
- Total tokens consumed (aider prints this — paste the line at the
  bottom of the summary)

---

## Why aider mode is allowed at all

The implementer's full runbook (`AGENT.md`) is designed for Claude
Code's tool ecosystem, but most of its tools are also available in
aider via `/run` and `/web`. When all Max profiles hit their 5h rolling
limit, the framework's fallback chain kicks in:

```
1.  claude-pool   → rc=75 if all profiles rate-limited
2.  aider + copilot-api proxy           ← (you might be here)
2b. aider + github_copilot native       ← (or here)
3.  aider + Azure OpenAI gpt-4.1-mini   ← (or here)
4.  graceful defer
```

The goal is to keep ranking-critical work shipping while the pool is
cool. **Aim for the same throughput Claude would achieve** — most recs
are doable. Only skip when a tool isn't reachable; never skip just
because "aider mode is limited" — it isn't.

**Quality bar is the same.** Don't ship a 200-word article because
"aider mode." Don't omit `FAQPage` JSON-LD because "aider mode." If
you'd reject the work in claude-mode, reject it here too.
