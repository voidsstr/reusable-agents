# llm-code-editor — Blueprint

## When to use

Your agent reads a list of approved actions (recommendations, fixes,
implementation requests) and applies them as code edits in a target
repo:

- Reads `responses-queue/` to find which recs the user wants shipped
- Loads the prior run's `recommendations.json` to get the implementation
  outline for each
- Drives an LLM (Claude CLI / Anthropic API / Copilot) to make the
  edits in the target repo
- Runs the repo's tests
- Commits + tags + (optionally) deploys

Example: `seo-implementer` — applies SEO recs to AislePrompt /
SpecPicks codebases.

## What you get

```
<agent-id>/
  AGENT.md
  manifest.json
  agent.py                  # AgentBase subclass — orchestrates the edit loop
  run.sh                    # CLI wrapper (kept for backwards compat with cron)
  prompts/
    apply-rec.md            # template for "apply this one rec to this repo"
  requirements.txt
  README.md
```

## LLM backend choice

Two viable strategies:

### A. Claude CLI (interactive)

```bash
cd <target-repo>
claude --dangerously-skip-permissions \
    --max-turns 30 \
    -p "$(cat prompts/apply-rec.md | substitute_rec_id)"
```

Pros:
- Best at multi-file edits, complex refactors, test-fix loops
- Auto-uses your Claude Max subscription
- No API key juggling

Cons:
- Subprocess management
- Less observable (you parse stdout/stderr post-hoc)

### B. Framework AI client (programmatic)

```python
client = self.ai_client()
files_to_change = client.chat([...]).extract_files()
for f, new_content in files_to_change:
    Path(f).write_text(new_content)
```

Pros:
- Backend swappable per agent (Anthropic / Copilot / Ollama)
- Structured I/O, easier to log + replay

Cons:
- You handle the file-IO yourself
- Multi-file edits require careful prompting

`seo-implementer/run.sh` supports both via `IMPLEMENTER_LLM=claude|framework|noop`.
Default is `claude` for production, `framework` for testing the
backend-swap path, `noop` for smoke tests.

## Integration points

| Touchpoint | How |
|---|---|
| Inputs | `responses-queue/*.json` (auto-drained by AgentBase pre_run) + the prior `recommendations.json` referenced by `request_id`. |
| LLM | `IMPLEMENTER_LLM` env var or per-agent provider override (`/api/providers/defaults/agent-override`). |
| Test gate | Hardcoded test command in site config (`deployer.test.cmd`). Non-zero exit aborts the run BEFORE any commit. |
| Commit + tag | `framework.core.release_tagger.commit_and_tag(...)` — writes a release id like `agent/<id>/release/<UTC-ts>`. |
| Deploy | Optional. If `deployer` block in site config, chain to `seo-deployer` (or whatever is configured). |
| Confirmation | `@requires_confirmation` for production deploys. The decorator handles the email gate; the body only runs after explicit approval. |

## Reference implementation

`seo-implementer/run.sh` — drives the Claude CLI against the AislePrompt
+ SpecPicks repos. ~200 lines of bash + a substantial markdown prompt
template.

## Anti-patterns

1. **Don't commit without running tests.** ALWAYS test-gate. If tests
   are slow, scope the test command to relevant files (e.g., only
   smoke-test the SEO-impacted pages).
2. **Don't push without `@requires_confirmation`.** Anything that hits
   prod must go through the email gate unless the site explicitly
   opted into auto-implement (and even then, only `tier=auto` recs).
3. **Don't trust the LLM's claim that it edited correctly.** Diff what
   it produced. If the diff is empty or massive (> 500 lines for a
   single rec), abort.
4. **Don't bundle multiple unrelated recs into one commit.** One rec =
   one commit. Makes review + revert clean.
5. **Don't skip the release tag.** Tags are how the dashboard's
   changelog timeline works.

## Confirmation flow

```python
@requires_confirmation(reason="commits + force-push to production")
def deploy_to_azure(self, tag): ...
```

First call: framework writes `confirmations/<conf-id>.json` with
`pending=true`, sends an email via the mailer, raises
`ConfirmationPending` (caught by run loop — agent saves state and
exits cleanly). Next run: pre_run drains responses-queue, resolves the
confirmation, the method body runs.
