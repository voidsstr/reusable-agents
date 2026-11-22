# affiliate-site kit

A versioned kit for Amazon-Associates review sites that run on the
reusable-agents fleet: catalog-driven product pages with fresh Creators API
prices, buyer-intent articles, verified click and AI-crawler logging, and a
deploy gate. It holds the money mechanics SpecPicks proved (price rule, CTA,
Offer JSON-LD, click classification), parameterised by one `site.json`, so a
new site is configuration plus its own niche code instead of a fork.

Status: **0.2.0**. 0.1.0 was the first complete kit (build items K1 to K7);
0.2.0 adds the site-wide `indexing` switch and `redirectFromHosts` (K2), the
2(i) stamp rule that fails any price without its shown date, time, zone and
"Details" control (K5), and `DB_CONNECTION_LIMIT` (K6). See `CHANGELOG.md`.

| Part | Where | Build item |
|---|---|---|
| SiteContext and the leaf modules (offer, links, price, notices, bot and AI-crawler verification, ...) | `server/src` | K1 |
| HTTP middleware (`mountKit`: the closed-indexing header, AI-traffic log, host redirects, beacon, `/r/`, robots, llms, IndexNow key and submit path, health) and the browser bundle | `server/src/http`, `client/` | K2 |
| Render helpers (layout shell, SEO head, JSON-LD, Markdown with GFM tables, sitemaps) | `server/src/render` | K3 |
| Migrations (clicks, AI traffic, articles, verified-human views), the runner, the publish-guard template | `migrations/`, `server/src/db` | K4 |
| The deploy gate (runner, all-pages engine, kit-compliance spec) and its fixture site | `tests/`, `fixture-site/` | K5 |
| Deploy templates (Azure Container Apps and ECS Fargate, Dockerfile) | `deploy/` | K6 |
| The site scaffold and the vendoring sync | `../../_template/affiliate-site`, `../../install/{create-affiliate-site,sync-site-kit}.sh` | K7 |

## Start a new site

```bash
bash install/create-affiliate-site.sh <slug> <domain> \
     (--content-rule-sets a,b | --content-policy-waived REASON) [--dir DIR] [--dry-run] \
     [--brand NAME] [--byline NAME] [--partner-tag TAG] [--niche NAME] \
     [--gate-port PORT] [--fixture-db-port PORT] [--host-locks NAMES] \
     [--indexnow-key KEY] [--kit DIR]
```

From the reusable-agents checkout (the kit's dependencies installed: `npm ci`
here). It writes a complete repo, by default next to the checkout
(`<parent>/<slug>`), into a missing or empty directory only (resolved to its
real path first, never inside the checkout), and `git init`s it on `main`
with no remote; nothing is committed, registered, provisioned or deployed.
`--dry-run` checks everything and writes nothing. The content gate is a
required choice: the content-policy rule sets every rendered page and
article must pass, or the reason, in words, the site needs none. The slug
must be free: not a site in the reusable-agents site registry, and with no
`<slug>_postgres` / `<slug>_backend_test` container or `<slug>_pgdata`
volume on the host. The registry also names the other sites' gates the new
gate never overlaps (`--host-locks` replaces that list).

The new repo holds `site.json` (checked by this kit's loader), `package.json`
with this kit's exact pins and a lockfile, `src/index.ts` (`mountKit`, then a
route table with the home page, guides from `editorial_articles`, about,
disclosure, privacy and the sitemaps), `db/migrations` (applied after the
kit's), the deploy-gate wiring with a fixture (`tests/suite.env`,
`tests/run-local-suite.sh`, `db/fixtures`), the gate config in two files
(`tests/ui/pages.manifest.ts`, the page families; `tests/ui/gate.config.ts`,
the operator-owned policy: compliance sources, the content rule sets or the
waiver, the recall waiver until the site has a catalog, every other waiver),
`deploy.env` (smoke paths the scaffold serves, the site's own AWS secret)
and `Dockerfile.azure` rendered from `deploy/`, `azure/` and `aws/`
wrappers, kit-integrity and gate-config unit tests, `CLAUDE.md` (north star,
Amazon data rules, the read-only kit boundary, the gate contract),
`keep-the-lights-on.yaml`, and `agents/implementer-scope.yaml`, whose
excluded paths keep `vendor/**`, `site.json`, the niche policy file
(`src/<niche>/policy.ts`) and the gate policy out of every implementer
dispatch kind, and the page manifest out of deploy repairs, wherever the
framework applies the scope (not yet on the implementer's claude path: see
below). Generated as is, it builds and passes this kit's gate on its own
fixture.
`_template/affiliate-site/README.md` documents the template.

## How a site uses it

The kit is vendored **read-only** into each site as `vendor/affiliate-kit`
by `install/sync-site-kit.sh`, which writes `KIT_VERSION` and
`MANIFEST.sha256`. Before every run the site's deploy gate
(`tests/run-local-suite.sh`) checks the vendored copy in two steps and stops
with exit 78 on any difference:

1. against that manifest: an edit, an addition, a removal or a symbolic link;
2. against the kit version it claims as the reusable-agents checkout holds
   it (`install/sync-site-kit.sh --verify-anchored`): byte-identical to the
   kit at one of the commits whose `VERSION` is that version, or to the
   working tree (with a warning). The manifest lives in the site, so an
   edit can rewrite it and pass step 1; it cannot rewrite reusable-agents'
   commits. A copy claiming a version older than the newest committed one,
   which no commit holds, fails too. The checkout is `KIT_GATE_FRAMEWORK_DIR`,
   else `REUSABLE_AGENTS_DIR`, else `reusable-agents` next to
   `GATE_SOURCE_REPO` or the site. With nothing to compare (no checkout, or
   no commit holding that version yet) the gate warns and goes on;
   `KIT_ANCHOR_REQUIRED=1` makes that exit 78.

The site's `tests/unit/kit-integrity.test.ts` runs the same checks under
`npm test`.

What this does not cover: the shim, the test and the manifest are files in
the site. A session that rewrites the shim too, or claims a kit version no
commit holds yet, gets an edited kit through. Agents never edit the vendored
copy, but only some paths enforce it: the implementer scope excludes it (and
`site.json`, the niche policy and `tests/ui/gate.config.ts`) where the
framework applies the scope, on the implementer's framework-chain path and
on every deploy repair. The implementer's default claude path commits
without a scope check, so there those files are protected only by
instructions, and nothing in a site checks `site.json`, the policy or the
gate policy. Keep a site's implementer dispatches disabled until the
framework closes that (CHANGELOG, "Known issues"). Kit changes are made
here, released with a version bump, committed, and synced (see "Upgrading a
site").

A site imports four entry points (each a barrel; their export names never
clash):

| Import | For |
|---|---|
| `vendor/affiliate-kit/server/src` | SiteContext, prices, CTAs, notices, JSON-LD offers, click and crawler verification |
| `vendor/affiliate-kit/server/src/http` | `mountKit`, `createPgKitDb`, `pgRedirectLookup`, the sticky bar, the client bundle |
| `vendor/affiliate-kit/server/src/render` | `renderPage`, `headTags`, `pageTitle`, JSON-LD builders, `mdToHtml`, sitemaps |
| `vendor/affiliate-kit/server/src/db` | `runKitMigrations`, `pendingMigrations`, `renderPublishTrigger` |

The scaffold's server, in short:

```ts
import { htmlCacheControl, loadSiteContext, priceStamp, cta } from '../vendor/affiliate-kit/server/src';
import { createPgKitDb, loadKitClientBundle, mountKit, pgRedirectLookup } from '../vendor/affiliate-kit/server/src/http';
import { renderPage, pageTitle, INDEXABLE_PAGE } from '../vendor/affiliate-kit/server/src/render';
import { pendingMigrations, kitMigrationSources } from '../vendor/affiliate-kit/server/src/db';

const ctx = loadSiteContext('site.json');             // throws SiteContextError listing every problem
const app = express();
const kit = mountKit(app, ctx, {                      // first: noindex header (closed), AI-traffic logger, host redirects, /health, robots, llms, IndexNow, beacon
  db: createPgKitDb(pool),
  redirectLookup: pgRedirectLookup(pool, CATALOG_SQL),    // GET /r/:asin, once the site has a catalog
  llms: LLMS_CONTENT,
  clientBundle: loadKitClientBundle('dist/client/kit-client.js'),
});
// ... the site's routes: every Amazon price, CTA and title through the kit
html += priceStamp(ctx, { price, currency, fetchedAt }, { key: `${asin}|pdp-hero` }); // '' unless quotable
html += cta(ctx, { asin, placement: 'pdp-hero' });
res.set('Cache-Control', htmlCacheControl(ctx));      // the same numbers that bound price quotability
process.on('SIGTERM', () => { kit.stop(); void kit.drain(); });
```

Migrations: production runs them in the deploy's `migrate` stage as the
owner role (`deploy.env MIGRATE_COMMAND`, the compiled `server/src/db/migrate.js`),
and the app refuses to serve while `pendingMigrations()` reports any; the
gate's fixture app applies them at boot (`runKitMigrations`). See
`migrations/README.md`.

## site.json (SiteContext)

Schema: `schema/site.schema.json` (JSON Schema 2020-12). Example:
`fixtures/site.example.json`. Everything SpecPicks hard-codes lives here:

| Section | Holds |
|---|---|
| `site` | `id` (written to `site_id` columns), `brandName`, `domain`, `canonicalOrigin`, `language` |
| `indexing` | `open` (default) or `closed`. Closed: every response carries `X-Robots-Tag: noindex` (HTML, XML, text, JSON, redirects, errors; mountKit installs it ahead of everything and it survives a handler that overwrites or removes the header) and the IndexNow submit path refuses (`indexNowSubmission`, `submitIndexNow`, `indexableUrls` throw `IndexingClosedError`). robots.txt does not change: a Disallow would hide the noindex from crawlers and leave URLs indexed bare. Pages keep their own robots meta, so the gate still checks each family's decision for the day the site opens |
| `redirectFromHosts` | hosts (a temporary platform address, `www`) whose GET and HEAD requests get a 301 to `canonicalOrigin` with the path and query exactly as sent, cacheable for a day. Served on every host instead: `/health`, `/robots.txt` (crawlers that do not follow a robots.txt redirect would otherwise read "no robots.txt"), the IndexNow key file and verification files (`/google*.html`, `/BingSiteAuth.xml`, `/.well-known/*`), marked noindex there. Other methods are served as is (a 301 would turn a click beacon's POST into a GET). Never the canonical host itself |
| `routes` | product page template, default `/product/{asin}` |
| `amazon` | `partnerTag`; `placementTags` (segment tag per `data-cta-placement`); `sourceTags` (tag per page kind, for `/r/`); `acceptSegmentTagParam`; CTA labels. The marketplace is the US store: `www.amazon.com`, `Amazon.com`, USD |
| `sources` | referer-path rules for `classifySource` (first match wins) |
| `disclosure` | the earnings disclosure (must say "As an Amazon Associate ... from qualifying purchases") and its page |
| `byline` | the editorial byline (no invented people) |
| `theme` | light and dark palettes; every text pair must pass WCAG AA (4.5:1) on both `bg` and `surface` (cards), accents and focus rings 3:1 |
| `crawlers` | robots policy per class (default: search allow, agent allow, training block), the user-agent tokens per class, disallowed paths. Amazon's Amzn-SearchBot is a search default and Amzn-User an agent default; Amazonbot ("may be used to train Amazon AI models") is a training default, so it is blocked unless a site lists its own tokens |
| `aiCrawlers` | extra static vendor IP ranges (Anthropic's are kit defaults); public space only, no broader than /16 (IPv4) or /32 (IPv6); IPv4 ranges written as IPv4 (IPv4-mapped IPv6 is refused) |
| `edge` | `cdn`: `none` (default) or `cloudflare`; `extraTrustedProxies` (public, no broader than /12 or /24, IPv4 written as IPv4). Behind a proxying CDN the kit skips exactly one X-Forwarded-For hop inside the CDN's published ranges to find the visitor (`siteClientIp`). Set it only once the origin answers your own zone alone (see "Client address behind a CDN") |
| `indexNow` | the public IndexNow key |
| `cache` | `htmlSMaxAgeSeconds` (at most 600), browser `htmlMaxAgeSeconds`, in-process `ssrCacheSeconds` |
| `prices` | `freshnessHours` (at most 24) |

Derived on load: `amazon.allowedTags` (every tag a link may carry),
`edge.trustedProxyCidrs` (the CDN's ranges plus `extraTrustedProxies`),
`aiCrawlers.staticRanges` (the kit defaults, then the site's extras),
`cache.maxHtmlCacheAgeSeconds` (sum of the cache layers) and
`prices.quotableMaxAgeSeconds` = `freshnessHours * 3600 - maxHtmlCacheAgeSeconds`.
`site.json` must leave a window of at least an hour. `PRICE_FRESH_HOURS` in the
environment can only shorten the window, and is honoured even below that hour
(hiding prices sooner is always safe), so it never stops a site from booting.

### SiteContext reference (`server/src/siteContext.ts`)

| Export | What it does |
|---|---|
| `loadSiteContext(file, { env? })` | read, validate and resolve a `site.json`; the returned `SiteContext` is deep-frozen |
| `parseSiteContext(raw, { env?, loadedFrom? })` | the same from a parsed object (overlays, tests) |
| `SiteContextError` | thrown by both, with every problem at once in `.issues` (schema first, then the cross-field rules) |
| `validateSiteJson(value)`, `validateAgainstSchema(value, schema)`, `SITE_SCHEMA`, `unsupportedSchemaKeywords` | the schema check alone; `SITE_SCHEMA` is `schema/site.schema.json`, kept identical by a test |
| `htmlCacheControl(ctx)` | `public, max-age=<htmlMaxAgeSeconds>, s-maxage=<htmlSMaxAgeSeconds>`: the header every rendered page carries |
| `themeCss(ctx.theme)`, `THEME_TOKEN_VARS`, `THEME_FONT_VAR`, `contrastRatio` | the `--kit-*` CSS tokens the layout and base.css use, and the WCAG math that checked them |
| `KIT_DEFAULTS`, `KIT_DEFAULT_CRAWLER_USER_AGENTS`, `KIT_DEFAULT_SOURCES`, `RESERVED_SOURCE_KINDS`, `CRAWLER_CLASSES` | what an omitted key resolves to |
| Types | `SiteContext`, `SiteJson` (the file), `ThemePalette`, `SourceRule`, `CrawlerClass`, `CrawlerPolicy`, `StaticCrawlerRange` |

Cross-field rules checked on load: the canonical origin's host is the domain
or `www.` of it, and is not one of `redirectFromHosts`; source rules name each kind once, claim each exact path once
and never use a reserved kind (`home`, `direct`, `other`); every `sourceTags`
kind has a rule; the disclosure states the Operating Agreement section 5
sentence; every theme pair passes its contrast ratio; static crawler and
trusted-proxy ranges are public, narrow enough and (for IPv4) written as IPv4,
not IPv4-mapped IPv6; and the price window left
after the cache layers is at least an hour. The scaffold runs the same loader
on a new site's `site.json` (`_template/affiliate-site/scaffold/validate_site_json.cjs`).

### Client address behind a CDN

`clientIp()` (SpecPicks' rule) takes the right-most public X-Forwarded-For
entry, the one the container platform appended. Behind Cloudflare that entry
is a Cloudflare edge, so every crawler would be judged by the edge's address.
With `edge.cdn: "cloudflare"`, `siteClientIp(ctx, xff, remote)` skips that one
hop and returns what Cloudflare appended: the address that connected to it.
Exactly one hop, never a run: Cloudflare Workers fetch from Cloudflare's own
ranges (a cross-zone Worker arrives as `2a06:98c0:3600::103`), so skipping
every trusted entry would let a Worker's author choose the logged address.
`CF-Connecting-IP` is never read. Cloudflare's ranges are kit data
(`CDN_EDGE_RANGES`, checked against cloudflare.com/ips on 2026-11-17).

**Precondition: lock the origin to your own zone before setting `edge.cdn`.**
A Worker that connects to the origin directly, instead of through your zone,
also arrives from a Cloudflare address, and for such a subrequest the Worker
controls the client IP headers. The kit cannot tell that request from a real
proxied one, so it returns whatever address the Worker wrote to the left of
its own: that address lands in `ai_traffic_log` and the click rows, and
`botDetect`'s datacenter rule and the per-IP views of `kit_0004` read it.
So set `edge.cdn` (or `extraTrustedProxies`, for any proxy service shared by
many customers) only when the origin accepts requests from your own zone
alone:

- Authenticated Origin Pulls with **your own** certificate (zone-level or
  per-hostname), or Cloudflare Tunnel. The global AOP certificate is shared by
  every Cloudflare account, so it proves only that a request came through
  Cloudflare, not through your zone.
- Or a secret header that a Transform Rule in your zone adds and the origin
  checks.

Allowing only Cloudflare's IP ranges at the origin is not enough: Workers
egress from those same ranges. The kit ships no origin-side check (no
secret-header middleware, no client-certificate validation), so the lock is
the deployment's to build and verify. The kit's deploy scripts create DNS-only
records (`deploy/README.md`), where `edge.cdn` stays `none`.

## Amazon compliance built in

- **Quotable price** (`offer.ts`): a plain amount (`parsePriceAmount`:
  `'1,299.00'` or `'12abc'` is no price, not $1 or $12), USD, at least $1, not
  a repurposed listing, fetched within 24h **minus the longest HTML cache
  age**, so a cached page never shows a price older than 24h (IP License
  2(h)). Nothing renders a price or an Offer otherwise.
- **2(i)** (`price.ts`, `notices.ts`): every price and availability stamp
  carries its UTC time and the exact disclaimer behind an adjacent "Details"
  control (a native popover; the text is in the DOM either way, and shows
  inline where popovers are unsupported). Include `PRICE_STAMP_CSS` in the
  layout CSS. Never put a stamp inside an `<a>`. Pass each stamp a `key`
  (e.g. `` `${asin}|${placement}` ``) for stable markup; without one its
  popover id is unique per call, so ids never collide but change per render.
  A kind `availabilityStamp` does not know renders the neutral copy. Every
  price, inside a sentence too, is one `priceStamp()`: the gate (0.2.0) fails
  a stamp that does not show its date and time with a zone (or shows another
  time than its `<time datetime>`), has no visible "Details" control
  revealing the exact disclaimer, or holds a second price; and any price
  outside a stamp, in visible text, in text only crawlers and assistants read
  (hidden elements), in an `aria-label` / `title` / `alt`, or an Amazon price
  marked `data-kit-not-a-price`.
- **No prices in head metadata**: `<meta name="description">`,
  `og:description` and `twitter:description` cannot carry the "as of" time or
  the 2(i) disclaimer, and search engines keep them for days. Name a
  substitute there (`recallSubstitute`), never its price or stock.
- **2(k)** (`notices.ts`): `amazonContentNoticeHtml()` renders the exact notice
  in plain view; put it on every page that shows an Amazon title. The layout
  (`renderPage`) does it on its own for any page holding a stamp or a
  `data-amazon-content` marker (`amazonTextHtml()`).
- **Links** (`links.ts`): `rel="sponsored nofollow noopener"`,
  `target="_blank"`, `data-cta-placement`, and a tag from `allowedTags` on
  every affiliate anchor. Every URL builder (`amazonUrl`, `retagAmazonUrl`,
  `amazonSearchUrlFor`) replaces a tag that is not the site's own with the
  partner tag (`ownTrackingId`); `retagAmazonUrl` validates the parsed host.
- **List price** (`honestListPrice`): a list price above 3x the price is not
  shown. There is no price history, price alert or "lowest ever" helper, by
  design (Participation Requirements 6(y)).
- **Images** (2(h)): `og:image` is never an Amazon-hosted image
  (`resolveOgImage`), and `editorial_articles.hero_image_url` carries CHECK
  constraints that refuse Amazon hosts (`migrations/kit_0003`).
- **The gate** (`tests/ui/kit-compliance.spec.ts`) asserts every rule above on
  every page family of a site, on its fixture, before any deploy.

The wording in `notices.ts` was checked against
<https://affiliate-program.amazon.com/help/operating/policies> and
<https://affiliate-program.amazon.com/help/operating/agreement> on 2026-11-17.
`unit/notices.test.ts` pins it with an independent copy.

## What is in the kit

| Path | What it is | Docs |
|---|---|---|
| `server/src/*.ts` | the leaf modules (table below) | this file |
| `server/src/http/` | `mountKit` and every handler it mounts, `createPgKitDb`, the sticky bar, client-bundle serving | `server/src/http/index.ts` |
| `server/src/render/` | `renderPage`, SEO head, JSON-LD, Markdown with GFM tables, sitemaps, `base.css` | `server/src/render/index.ts` |
| `server/src/db/` | the migration runner and its CLI, the publish-guard renderer | `migrations/README.md` |
| `client/` | the browser bundle (click beacon, sticky-bar sizing), built by `scripts/build-client.mjs` | `client/README.md` |
| `migrations/` | `kit_0001` to `kit_0004` and `templates/publish_trigger.sql.tmpl` | `migrations/README.md` |
| `tests/` | the deploy-gate runner, the Playwright config builder, the all-pages engine and the kit's specs | `tests/README.md` |
| `deploy/` | Azure and AWS provision, deploy and teardown, `Dockerfile.azure.tmpl`, `deploy.env.example` | `deploy/README.md` |
| `schema/`, `fixtures/` | the `site.json` schema and an example | |
| `fixture-site/` | the gate's own fixture site (not vendored) | `fixture-site/README.md` |
| `unit/` | the kit's jest suites, the scaffold's included (`unit/scaffold/`; not vendored) | |

### Modules (server/src)

| Module | What it does | Ported from (SpecPicks) |
|---|---|---|
| `siteContext.ts` | load, validate and resolve `site.json`; theme CSS; Cache-Control | new (SpecPicks' unused `sites/<slug>/site.json` idea) |
| `indexing.ts` | the `indexing` switch's refusal: `IndexingClosedError`, `assertIndexingOpen`, `indexingClosed` | new |
| `notices.ts` | 2(i), 2(k) and section 5 texts and HTML | new |
| `offer.ts` | quotable price, honest list price, Offer / Product / ListItem JSON-LD | `affiliateOffer.ts` |
| `links.ts` | `esc`, CTA and link builders, tags, `classifySource`, `pickTrackingId` | `ssrRender.ts`, `index.ts` |
| `price.ts` | price labels and the price / availability stamps | `ssrRender.ts` |
| `botDetect.ts` | human-vs-bot click verdicts, device, country | `botDetect.ts` |
| `aiCrawlerVerify.ts` | AI-traffic classification, client IP (with one trusted CDN hop), CDN edge ranges, crawler IP verification | `aiCrawlerVerify.ts` |
| `dbBudget.ts` | `boundedQuery`, `withBudget`, `SwrCache` | `dbBudget.ts` |
| `verifiedClicks.ts` | 30-day verified-human clicks per ASIN | `verifiedClicks.ts` |
| `productAvailability.ts` | inactive-product reasons, Amazon search fallback | `productAvailability.ts` |
| `substituteQuote.ts` | the substitute an unbuyable page leads with | `pdpSubstituteQuote.ts` |
| `index.ts` | barrel, `KIT_VERSION` | |

## Upgrading a site

The vendored copy only ever changes through a sync, from a released kit:

1. **Release the kit** (here): the change, its tests (`./test.sh`, and
   `node tests/selftest/gate-selftest.mjs` when the gate changed), then
   `VERSION`, `package.json` and `KIT_VERSION` bumped together and a
   CHANGELOG entry that says what a site must do (a new dependency pin, a
   new `deploy.env` key, a migration, a markup change its specs read).
   Commit it: a site's gate anchors its vendored copy to these commits.
2. **Check first** (read-only): `bash install/sync-site-kit.sh <site> --check`
   says whether the vendored copy is intact and current, and whether the
   site follows the kit's pins and template. `--dry-run` lists the files a
   sync would add, remove and update. `--verify-anchored` is what the site's
   gate runs: it passes only for a copy byte-identical to its kit version in
   this checkout, so commit the release before syncing it.
3. **Sync**: `bash install/sync-site-kit.sh <site>` replaces
   `vendor/affiliate-kit` (assembled beside the old copy and swapped in, so
   a failure leaves the old one), writes `KIT_VERSION` and `MANIFEST.sha256`,
   and reports what the site must follow. It refuses an older kit unless
   `--allow-downgrade`, and a kit whose `VERSION`, `package.json` and
   `KIT_VERSION` disagree. Local edits under `vendor/affiliate-kit` are
   discarded (and listed): fix the kit instead.
4. **Follow the kit** in the site:
   - dependency pins the sync reported: `npm install <pkg>@<pin> --save-exact`
     (the kit-integrity test fails until `package.json` matches);
   - the Dockerfile: `bash vendor/affiliate-kit/deploy/render-dockerfile.sh --env deploy.env --write`
     (the deploy-config test and `deploy.sh` refuse a render from another kit);
   - new `deploy.env` keys: `vendor/affiliate-kit/deploy/deploy.env.example`
     documents them (unknown or missing required keys stop the deploy scripts);
   - new kit migrations need nothing: they run before the site's in the next
     deploy's `migrate` stage (and at boot in the gate).
5. **Prove it**: `npm ci && npm run typecheck && npm test && bash tests/run-local-suite.sh`.
6. **Commit** on `main` (vendor, `package*.json`, `Dockerfile.azure`):
   `Sync affiliate-kit <version>`, then deploy as usual.

The sync never touches a site's own files, and nothing about it runs on a
schedule. A kit change that a site must follow (a pin, a `deploy.env` key, a
file the scaffold generates) also updates `_template/affiliate-site`, so new
sites start current.

## Developing

```bash
cd kits/affiliate-site
npm ci                 # once; the lockfile pins every kit item's dependencies
./test.sh              # typecheck (server without DOM, everything else with) + jest
npm run build          # server -> dist/ (CommonJS + .d.ts)
npm run build:client   # the browser bundle -> client/dist/
node tests/selftest/gate-selftest.mjs        # the gate proves itself on fixture-site/ (docker)
KIT_SCAFFOLD_SMOKE=1 npx jest unit/scaffold/smoke.test.ts   # a generated site passes the gate (docker, npm registry)
```

Unit tests live in `unit/**/*.test.ts`; Playwright specs (gate engine) live in
`tests/ui/` and never run under jest. `unit/scaffold/` covers the scaffold and
the sync offline: it generates sites, links this kit's `node_modules` into
them and runs their typecheck, build and unit tests.

### Drift against SpecPicks

`PORTED_FROM.json` records the SpecPicks commit of the port and a sha256 per
ported declaration. `unit/drift.test.ts` re-hashes them when
`/home/voidsstr/development/specpicks` exists and prints what changed. It fails
only with `KIT_DRIFT_STRICT=1`. After porting an upstream fix, refresh the
hashes:

```bash
KIT_DRIFT_REF=<specpicks-commit> KIT_DRIFT_REPORT=/tmp/drift.json npx jest unit/drift.test.ts
# copy the sha256 values for the re-ported symbols into PORTED_FROM.json and update upstream.commit
```

## Rules for changing the kit

- No site names in anything the kit ships: `unit/index.test.ts` walks the
  whole kit (code, scripts, SQL, templates, CSS, JSON) with comments removed
  per language, skipping only installed or generated trees, fixtures, `unit/`,
  docs and `PORTED_FROM.json`. Comments may cite provenance; niche values
  belong in `site.json` or the site repo. The scaffold template has the same
  rule (`unit/scaffold/render.test.ts`).
- Browser code (`client/src`) must not use Node globals: `unit/index.test.ts`
  re-checks it with DOM typings only (`process`, `Buffer` or `require` there
  would type-check under `tsconfig.json` and then throw in the bundle).
- Keep `schema/site.schema.json` and `SITE_SCHEMA` identical (a test enforces
  it) and only use schema keywords the validator supports.
- Bump `VERSION`, `package.json` and `KIT_VERSION` together and add a
  CHANGELOG entry; note any money-mechanics fix that SpecPicks also needs.
- Dependencies are pinned exactly; add one only with a lockfile update in the
  same change. Sites pin the same versions (the scaffold copies them, the
  sync and each site's kit-integrity test compare them).
- What a site vendors is every kit file except installed and generated trees,
  `unit/`, `fixture-site/`, `tests/unit/`, `tests/selftest/` and kit-repo
  tooling (`_template/affiliate-site/scaffold/vendor_kit.py`, `EXCLUDED_*`).
  Nothing a site runs may live in those.
