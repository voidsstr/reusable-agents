# _template/affiliate-site

The scaffold for a new Amazon Associates site on the affiliate-site kit
(`kits/affiliate-site`). Never copied by hand: `install/create-affiliate-site.sh`
renders it, vendors the kit, derives the package pins, `deploy.env` and
`Dockerfile.azure` from the kit, checks the result, and moves it into place.

```bash
bash install/create-affiliate-site.sh <slug> <domain> \
     (--content-rule-sets a,b | --content-policy-waived REASON) [--dir DIR] [--dry-run] \
     [--brand NAME] [--byline NAME] [--partner-tag TAG] [--niche NAME] \
     [--gate-port PORT] [--fixture-db-port PORT] [--host-locks NAMES] \
     [--indexnow-key KEY] [--kit DIR]
bash install/sync-site-kit.sh <site> [--check | --verify | --verify-anchored | --dry-run] [--kit DIR]
```

Needs bash 4.4+, python3, node 22+, git, realpath and the kit's dependencies
(`npm ci` in `kits/affiliate-site`: `site.json` is validated with the kit's
own loader). The target directory is resolved to its real path first and
must be missing or empty, outside this checkout; the new repo is assembled in
a scratch directory and moved into place only when every check passes, then
`git init` on `main` with no remote. Nothing is committed, registered,
provisioned or deployed.

The content gate is never cut, so the script makes you choose it: the
content-policy rule sets every rendered page and article must pass, or the
reason in words the site needs none (rendered into the gate config, printed
with the skip on every gate run). The slug must be free: not a site the
reusable-agents site registry has (`framework/core/site_registry.py`, built-in
defaults, never framework storage), and, when docker is present, with no
`<slug>_postgres` or `<slug>_backend_test` container and no `<slug>_pgdata`
volume on the host. The registry also names the other gates the new gate
never overlaps (`--host-locks` replaces that list).

## Layout

| Path | What it is |
|---|---|
| `site/` | the site skeleton, rendered into the new repo |
| `scaffold/render_template.py` | renders `site/` (placeholders, renames, modes) |
| `scaffold/vendor_kit.py` | vendors / checks / verifies the kit, and anchors a vendored copy to this checkout's commits of its version (`install/sync-site-kit.sh`) |
| `scaffold/site_files.py` | `package.json` pins + `package-lock.json` from the kit's; `deploy.env` from the kit's `deploy.env.example` |
| `scaffold/validate_site_json.cjs` | `site.json` through the kit's `loadSiteContext` (bundled with the kit's esbuild) |
| `scaffold/fleet_sites.py` | the site registry, read for the slug check and the other gates' projects |

The tests are in `kits/affiliate-site/unit/scaffold/` (they run with the kit's
`./test.sh`).

## Template rules (`scaffold/render_template.py`)

- A file ending in `.tmpl` is rendered and loses the suffix; every other file
  is copied byte for byte.
- A path segment starting with `dot-` becomes a dotfile (`dot-gitignore` ->
  `.gitignore`), so the template's own dotfiles never act on this checkout;
  `__NICHE__` becomes the `--niche` value.
- Placeholders: `{{NAME}}` inserts the value as is, and is refused outside
  Markdown for any value with characters beyond `[A-Za-z0-9._:/@+,=-]`;
  `{{json:NAME}}` (also a valid YAML double-quoted scalar and TypeScript
  string), `{{sql:NAME}}`, `{{jsonlist:NAME}}` and `{{shq:NAME}}` (one
  single-quoted shell word) insert a quoted literal.
  An unknown name is an error, and no placeholder may survive rendering.
- Modes come from the renderer, never the checkout: `*.sh` 0755, everything
  else 0644.
- No niche values in the template: everything site-specific is a placeholder
  or comes from `site.json` at runtime. A test walks the template for the
  reference sites' names.

Variables: `SLUG`, `DOMAIN`, `BRAND`, `BYLINE`, `DISPLAY_NAME`, `DESCRIPTION`,
`EARNINGS`, `PARTNER_TAG`, `NICHE`, `POLICY_FILE`, `GATE_PORT`,
`FIXTURE_DB_PORT`, `FIXTURE_DB_NAME`, `FIXTURE_DB_USER`,
`FIXTURE_DB_PASSWORD`, `CONTENT_RULE_SETS`, `CONTENT_POLICY_WAIVED`,
`CONTENT_RULE_SETS_TEXT`, `HOST_LOCKS`, `INDEXNOW_KEY`, `KIT_VERSION`,
`SITE_DIR`, `ENV_IDENT`, `CREATED_ON`.

## What a generated site holds

| Path | From |
|---|---|
| `site.json` | `site/site.json.tmpl`, checked by the kit's `loadSiteContext` |
| `package.json`, `package-lock.json` | the template plus the kit's exact pins and lockfile |
| `tsconfig*.json`, `jest.config.js` | the kit's compiler options (the vendored kit type-checks unchanged) |
| `src/index.ts` | `createApp()` (kit middleware via `mountKit`, then the route table, then the 404) and `main()` |
| `src/routes/table.ts` | the route table: renderer, sitemap kind and gate family per route |
| `src/render/`, `src/db/`, `src/config/` | page renderers on kit helpers, bounded reads, site paths |
| `src/<niche>/policy.ts` | the operator-owned niche policy (excluded from every implementer dispatch kind) |
| `db/migrations/0000_site_defaults.sql` | site defaults on the kit's tables (the byline as the article author default) |
| `db/fixtures/` | the gate fixture's seed and re-stamp SQL |
| `tests/suite.env`, `tests/run-local-suite.sh`, `tests/playwright.config.ts` | the deploy gate on the vendored kit runner; the shim first checks `vendor/affiliate-kit` against `MANIFEST.sha256`, then against its kit version in the reusable-agents checkout (`--verify-anchored`), exit 78 on any difference; a file in the site itself, so it narrows the read-only boundary without closing it (kit CHANGELOG, Known issues) |
| `tests/ui/gate.config.ts` | the gate's policy, operator-owned: compliance sources, content-policy rule sets or the waiver, the recall waiver, every other waiver; composes the manifest and refuses one that grants itself a skip |
| `tests/ui/pages.manifest.ts`, `tests/ui/site.spec.ts` | the page families (agent-editable; never by a deploy repair) and the site's own specs |
| `tests/unit/` | kit integrity (MANIFEST.sha256, the anchor when the reusable-agents checkout is found, versions, pins), the gate config (the kit's validator), the app, the deploy files |
| `deploy.env` | the kit's `deploy.env.example` with the site's values (the smoke paths are routes the scaffold serves; the AWS secret is `nsc/<slug>`) |
| `Dockerfile.azure`, `.dockerignore` | rendered / copied from the kit's deploy templates |
| `azure/`, `aws/` | wrappers around the kit's provision, deploy and teardown scripts |
| `docker-compose.yml`, `docker-compose.test.yml` | the gate's fixture Postgres and the app under test |
| `agents/` | `register-with-framework.sh` (never run by the scaffold), `implementer-scope.yaml` (applied by the framework chain and deploy repairs, not the implementer's claude path: dispatches stay disabled until it is) |
| `CLAUDE.md`, `README.md`, `keep-the-lights-on.yaml`, `prompts/implementer/` | docs and config templates |
| `vendor/affiliate-kit/` | the kit, read-only, with `KIT_VERSION` and `MANIFEST.sha256` |

## Changing the template

Keep it niche-agnostic and keep the generated site green: the scaffold tests
generate a site, link the kit's `node_modules` (the same pins), and run its
typecheck, build and unit tests offline, and load its gate config through
Playwright. `KIT_SCAFFOLD_SMOKE=1` adds the full smoke (npm ci from the
registry, the image build and the kit gate on the site's fixture, in docker)
under `KIT_SCAFFOLD_SMOKE_DIR` (default: the OS temp directory).
