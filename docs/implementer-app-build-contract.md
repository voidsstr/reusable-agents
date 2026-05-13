# Implementer contract — `rec_type: app-build-from-blueprint`

> **Status:** spec only. The implementer's existing dispatch logic
> needs new code to handle this rec_type. Until that lands, recs of
> this type will defer-via-allowlist (visible on the dashboard's
> deferred tab; you'll see them under app-store-opportunity-agent in
> the lifetime stats).

## Trigger

The operator replies to the daily app-store-opportunity email:

```
implement opp-007
```

The responder:
1. Looks up `opp-007` in the run's `opp-id-map.json` artifact →
   resolves to the canonical `opportunity_id` (sha1[:16]).
2. Routes via the `app-store-opportunity-agent` route in
   `~/.reusable-agents/responder/config.yaml` → implementer dispatcher.
3. Auto-queue file written to
   `agents/responder-agent/auto-queue/<request_id>.json` with the
   resolved opportunity_id in `rec_ids`.

## What the implementer receives

Standard auto-queue payload, with `rec_ids` containing the
opportunity_id. The recommendations.json the implementer normally
expects ISN'T at the agent's `runs/<ts>/` — instead it's at:

```
agents/app-store-opportunity-agent/blueprints/<opportunity_id>/
  manifest.json
  README.md
  product-spec.md
  ux-research.md
  ux-spec.md
  tech-stack.md
  api-spec.md
  data-model.md
  implementation-plan.md
```

`manifest.json` carries:

```json
{
  "schema_version": "1",
  "opportunity_id": "...",
  "app_name": "...",
  "v2_pitch": "...",
  "feature_gaps": [...],
  "moats_to_take": [...],
  "tech_stack": {...},
  "files": ["product-spec.md", "ux-research.md", ...],
  "rec_type": "app-build-from-blueprint"
}
```

## What the implementer does (target behavior)

1. **Read `docs/reference-app-architecture.md`** — the canonical "how
   our apps are structured" doc. Every build follows this shape.

2. **Read every blueprint file** in
   `agents/app-store-opportunity-agent/blueprints/<opp-id>/`. The
   `implementation-plan.md` is the ordered build plan.

3. **Decide the destination repo:**
   - Default: scaffold a new repo at
     `/home/voidsstr/development/<app-slug>/` where `app-slug` is
     derived from `manifest.app_name` (kebab-case, ≤30 chars).
   - If a repo already exists at that path, branch off `main` instead
     of overwriting.

4. **Scaffold the standard file tree** from
   `docs/reference-app-architecture.md` § "File-tree the implementer
   scaffolds for a new app." Use `npm init -w` + `npx expo init` +
   `npx prisma init` etc. as one-shot commands, not by hand-writing
   every file.

5. **Walk `implementation-plan.md` milestone by milestone.** Each
   milestone is a commit. After each commit:
   - run `npm run lint` + `npm run typecheck` + `npm test`
   - if any fail, the LLM patches and retries (max 3 attempts per
     milestone before deferring)
   - smoke-test the running container on each milestone past M2

6. **Add the new app's agents.** The blueprint's
   `implementation-plan.md` includes an agent stub list (typically:
   `<app>-seo-opportunity-agent`, `<app>-catalog-audit-agent`,
   `<app>-progressive-improvement-agent`). Run
   `bash reusable-agents/install/create-agent.sh` for each, fill in
   the manifest from the spec, register with the framework.

7. **Wire deployment.** Generate `Dockerfile.azure` + `azure/deploy.sh`
   per the reference doc. Don't run the deploy unless
   `IMPLEMENTER_DEPLOY=1` (default off) — the operator should review
   the build first.

8. **Email the operator** when each milestone completes:
   - Subject: `[<app-slug>:M3] Built milestone — feature complete`
   - Body: list of commits, test results, smoke-test output, what
     remains.

9. **Mark the opportunity `pursued`** in the accumulator
   (`agents/app-store-opportunity-agent/opportunities/active.json`)
   so it stops appearing in the daily email.

## Constraints / safety

- **Never modify aisleprompt or specpicks** when handling
  this rec_type — those are existing apps and the build path is
  scaffolding-a-new-app, not patching-an-existing-one.
- **Never push to `main`** of the new repo. Build on a working
  branch; require operator approval to merge.
- **Cap LLM tokens per milestone** at the framework default for
  code-editor calls. A full v2 build takes 50+ aider/copilot calls;
  budget $5-15 per app at current rates.
- **Don't deploy without smoke-test pass.** A build that passes lint
  but fails the `/api/health` smoke is broken and gets deferred.

## Until the implementer is updated

Until the implementer code lands, recs of this type will:

1. Be queued by the responder as normal.
2. Hit the implementer's `TRUSTED_REC_TYPES` allowlist gate
   (`agents/implementer/build-aider-invocation.py:378`) — and defer
   because `app-build-from-blueprint` isn't allowlisted.
3. Show up on the dashboard's deferred tab.

That's intentional safety: full-app builds are a big-blast-radius
operation and should only run after the implementer's
build-from-blueprint code path is itself reviewed + tested. Add
`app-build-from-blueprint` to `TRUSTED_REC_TYPES` only after the
flow above is implemented and validated against one supervised
build.

## Handoff to the implementer team

When you're ready to enable end-to-end builds:

1. Implement the flow in
   `agents/implementer/build-aider-invocation.py` (new branch in
   `build_prompt` for `rec_type == "app-build-from-blueprint"`).
2. The implementer's `run.sh` already accepts arbitrary recs;
   the new code path just needs to:
   - read the blueprint manifest
   - call `aider`/`copilot` once per milestone with the spec
   - persist commits + smoke-test results
3. Add `app-build-from-blueprint` to `TRUSTED_REC_TYPES`.
4. Test with one low-stakes opportunity end-to-end.
5. Document any divergences from this contract back into this file.
