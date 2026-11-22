# Changelog: affiliate-site kit

All notable changes to `kits/affiliate-site`. The kit is vendored read-only
into each site (`vendor/affiliate-kit`, with `KIT_VERSION` and
`MANIFEST.sha256`), so every change here lands in a site only through a
version bump and a sync. Versions follow semver: a patch never changes
rendered markup or a public signature; a minor may add exports and markup;
a major may change either.

Until SpecPicks adopts the kit (P3-1, January), every fix to shared money
mechanics (price rule, CTA, Offer, click classification) has to be applied in
both places. Record each one under "Parity" in the release that carries it;
`unit/drift.test.ts` lists upstream changes since the port commit.

## 0.2.1 (2026-11-22)

### Fixed

- **Deploy, database phase: schema public on Azure.** Azure Database for
  PostgreSQL creates a new database's `public` schema owned by
  `azure_pg_admin`, not by `pg_database_owner` as stock PostgreSQL 15+ does,
  so the owner role could neither create the site's tables ("permission denied
  for schema public" on the first migration) nor converge the schema's
  privileges ("no privileges could be revoked"). The phase now hands `public`
  to `DB_OWNER_ROLE` first, as the admin, when another role owns it; it leaves
  `pg_database_owner` alone. Found on RuntimePicks' first provisioning.
  Integration tests cover both layouts.

## 0.2.0 (2026-11-21)

Staging and launch controls for a site that serves real pages before it may
be found (K2), a stricter 2(i) price rule in the gate (K5), and a database
connection ceiling in the deploy scripts (K6). Nothing changes for a site
that sets none of the new keys, except the gate rule, which every kit site's
gate now runs.

### Added

#### site.json `indexing` and `redirectFromHosts` (K2)

- `"indexing": "open" | "closed"` (default `open`; schema, loader
  `ctx.indexing`, `INDEXING_MODES`). Closed:
  - `mountKit` installs `closedIndexingMiddleware()` first, so every response
    carries `X-Robots-Tag: noindex`: pages, sitemaps (XML), robots.txt and
    llms.txt (text), `/health` and the beacon (JSON), `/r/` and every other
    redirect, 404s, error pages and static files. It also wraps the
    response's `writeHead()`, so a handler that overwrites the header, removes
    it or passes its own to `writeHead()` cannot take it away; a value scoped
    to one crawler (`googlebot: noindex`) gets a plain `noindex` beside it
    (`robotsTagNoindexForAll`, `ensureNoindexHeader`).
  - The IndexNow submit path refuses before it builds or sends anything:
    the new `indexNowSubmission()` (one POST body: host, key, keyLocation,
    URLs on the canonical origin only, at most 10,000) and `submitIndexNow()`
    (batched POSTs to api.indexnow.org, one result per batch, injectable
    fetch), and `indexableUrls()` (the sitemaps' set, "the URLs to submit to
    IndexNow or any ping") throw `IndexingClosedError` (kit barrel, with
    `assertIndexingOpen` and `indexingClosed`). `sitemapUrlsFrom()` and the
    sitemaps themselves are unchanged.
  - robots.txt does not change: a Disallow would stop crawlers from reading
    the noindex and leave URLs indexed bare. Pages keep their own robots
    meta, so the gate still checks each family's decision for opening day.
- `"redirectFromHosts": [host, ...]` (default `[]`; lowercase host names, never
  the canonical origin's host). `hostRedirectMiddleware()`, mounted by
  `mountKit` right after the AI-traffic logger (an assistant fetching an old
  URL is logged with its 301): GET and HEAD on a listed host get a 301 to
  `canonicalOrigin` + the path and query exactly as sent (no decoding or
  re-encoding; an absolute-form target keeps only its path and query),
  `Cache-Control: public, max-age=86400` so a redirect can be taken back.
  Served on every host instead, and marked noindex there: `/health`,
  `/robots.txt`, the IndexNow key file and site-verification files
  (`/google*.html`, `/BingSiteAuth.xml`, `/.well-known/*`). robots.txt is
  served, not redirected: RFC 9309 asks crawlers to follow up to five
  robots.txt redirects, but one that does not reads "no robots.txt" (crawl
  everything, training crawlers included), and the same policy on both
  hosts lets every crawler fetch the old URLs and see their 301s. Other
  methods are served as is: a 301 would make a browser repeat a click
  beacon's POST as a GET and lose the click.
- `mountKit().routes` reports `USE closed-indexing` and `USE host-redirect`
  when they are mounted.

### Changed

- `indexableUrls(ctx, candidates)` now reads `ctx.indexing` (its context type
  is `Pick<SiteContext, 'site' | 'crawlers' | 'indexing'>`; a full
  SiteContext has it) and refuses while it is closed.

#### The 2(i) stamp rule (K5)

kit-compliance's price rule (`priceDisplayProblems`, the same test title) now
fails, besides what it failed before:
- a stamp that does not SHOW its check time as a date and a time with a time
  zone (`SHOWN_CHECK_TIME_RE`: "2026-11-17 14:11 UTC", Amazon's
  "01/07/2008 14:11 PST", "Nov 17, 2026, 9:11 AM EST", ISO with `T`/`Z`,
  offsets); a `<time datetime>` attribute alone is not shown to readers;
- a stamp whose shown time is another instant than its `<time datetime>`
  (`shownCheckTimeMs`; compared when the shown time names one instant: a
  year and a fixed zone; generic ET/PT and year-less dates are not compared);
- a stamp without a visible control labelled "Details" (`DETAILS_LABEL_RE`: a
  popover button, an aria-controls button, an `href="#..."` link or a
  `<details>` summary) that reveals the exact 2(i) disclaimer. An inline
  disclaimer alone, which 2(i) allows as "adjacent", no longer passes: the
  kit's stamp and Amazon's example both use "Details";
- a price stamp holding a second price, or an availability stamp holding
  one (`priceAmounts`): every price, inside a sentence too, is its own stamp;
- a price outside every stamp that only crawlers and assistants read (text in
  a hidden element), or in an `aria-label`, `title` or `alt`;
- an Amazon price ("$N on Amazon", "$N (as of ...)") or a `.ssr-price` inside
  `data-kit-not-a-price`, which is for rates and budgets.
`looksLikePrice` (the unstamped-text, head-metadata and llms.txt checks) also
reads a currency-code amount ("USD 349.99", "349.99 USD", `PRICE_CURRENCY_CODE_RE`)
and a whole-dollar amount given a check time ("$349 (as of Nov 17)",
"$349 (checked ...)": `DATED_PRICE_RE` / `datedPrices`, budgets excepted).
The collector records what these need (`ComplianceStamp.detailsTexts`;
`ComplianceSnapshot.hiddenUnstampedText`, `attributeTexts`, `notAPriceText`).
Each rule is proven both ways in `tests/unit/compliance.test.ts` and in the
in-browser detector test; the fixture's violations scenario seeds one note per
new failure (`fixture-site/db/migrations/0006_fixture_stamp_violations.sql`,
`tests/selftest` names each), and `db/freshen.sql` now re-stamps the shown
time of 0005's two hand-made stamps with their datetime, so each stays red
for its own reason only.

#### DB_CONNECTION_LIMIT (K6)

- `deploy.env` `DB_CONNECTION_LIMIT` (optional; empty or unset leaves the
  database's limit as the server has it): the database phase of
  `azure/provision.sh` and `aws/provision.sh` (`kd_sql_cluster`, shared) runs
  `ALTER DATABASE ... WITH CONNECTION LIMIT n` once the database exists and is
  marked, then checks on the server, like the role limits, that `datconnlimit`
  is n and the four roles' limits fit under it. Validated before anything
  runs: an integer from 1 to 5000, at least the sum of the four
  `DB_*_CONNECTION_LIMIT` (refused below it; a warning when equal, since on
  Azure flexible server and RDS the admin is no superuser and its own
  sessions count too). Both clouds' check and database phases log the limits
  (`kd_db_limits_summary`). In `deploy.env.example`; dry-run tests for both
  clouds and parity; `unit/deploy/postgres.it.test.ts` proves it on a real
  Postgres as a non-superuser admin (set, converged, left alone when unset,
  enforced).

### What a site must do

- Nothing, to keep today's behaviour: both site.json keys default to what a
  0.1.0 site had, and `DB_CONNECTION_LIMIT` is optional.
- Re-render `Dockerfile.azure` after the sync (its header names the kit
  version): `bash vendor/affiliate-kit/deploy/render-dockerfile.sh --env deploy.env --write`.
- A renderer that prints its own stamps (rather than `priceStamp()` /
  `availabilityStamp()`) must show the check time with a zone and a
  "Details" control, one price per stamp; the kit's own stamps already do.

### Verification

`./test.sh` and `npm run typecheck`. `tests/selftest/gate-selftest.mjs` was not
run for this release (it takes the host's gate locks); instead the fixture
app was run from this tree against a throwaway Postgres and the gate's
Playwright specs were run directly: the clean scenario passed all 58 tests,
and in the violations scenario every kit-compliance rule failed with every
needle the self-test names (0006's included) while the detector test passed.
Run the self-test before relying on it as the release proof.

### Parity

- SpecPicks' SSR stamps ("as of <date> (price may vary)", no "Details") would
  fail the new rule; queue with 0.1.0's 2(i) parity item for P3-1.

## 0.1.0 (2026-11-17)

First release: the complete kit, build items K1 to K7. Ported from SpecPicks
at `b5415b08` (`PORTED_FROM.json` holds a sha256 per ported declaration).

### Added

#### Leaf modules and SiteContext (K1)

- Toolchain: TypeScript (strict, CommonJS, node 22), jest + ts-jest, exact
  dependency pins for every kit item (express 4, pg, supertest, esbuild,
  Playwright 1.62.1, whose Chromium build matches the host's cached browsers).
- `schema/site.schema.json` and `server/src/siteContext.ts`: the typed
  SiteContext loaded from a site's `site.json`. Validates the schema (with an
  embedded copy kept identical by a test), applies kit defaults, checks the
  cross-field rules (origin host, source rules, WCAG contrast of every theme
  pair, the Operating Agreement section 5 disclosure, the price window left
  after HTML caching), derives the allowed tag set and the quotable price age,
  and deep-freezes the result.
- `notices.ts`: the exact IP License 2(i) disclaimer and 2(k) notice (curly
  quotes as on the live page) and the Operating Agreement section 5 statement,
  verified against the policy pages on 2026-11-17.
- `offer.ts` (from `affiliateOffer.ts`): `quotablePrice` with the window
  "24h minus the longest HTML cache age"; `honestListPrice` (list at most 3x
  price); Offer JSON-LD with `validFrom` = fetch time; repurposed-listing
  guard; Creators availability types mapped to schema.org.
- `links.ts`: `esc`, `amazonUrl`, `retagAmazonUrl`, `cta`, `amazonSearchCta`,
  `detailLink` (from `ssrRender.ts`) and `classifySource` / `pickTrackingId`
  (from `index.ts`), parameterised by SiteContext. Every CTA carries
  `rel="sponsored nofollow noopener"`, `target="_blank"`, a
  `data-cta-placement`, and a tag from the site's own set (per-placement
  segment tags supported).
- `price.ts`: `priceLabel` / `priceHtml` (ported) and the price and
  availability stamps: "$X on Amazon.com as of <UTC> (price may vary)" plus a
  native-popover "Details" control carrying the exact 2(i) text.
- `botDetect.ts`, `dbBudget.ts`, `verifiedClicks.ts`,
  `productAvailability.ts`, `substituteQuote.ts` (from `pdpSubstituteQuote.ts`).
- `aiCrawlerVerify.ts`: bots.json and vendor range lists, plus Anthropic's
  published outbound ranges `160.79.104.0/21` and `153.61.208.0/20` as kit
  defaults; state moved into an `AiCrawlerVerifier` instance.
- `site.json` `edge` (`cdn`: `none` | `cloudflare`, `extraTrustedProxies`) and
  `ctx.edge.trustedProxyCidrs`; `CDN_EDGE_RANGES` (Cloudflare's published
  ranges, checked 2026-11-17); `clientIp(xff, remote, trusted)` skips exactly
  one trusted CDN hop; `siteClientIp`, `edgeProxyMatcher`,
  `trustedProxyMatcher`. Set `edge` only once the origin accepts its own zone
  alone (Authenticated Origin Pulls with the site's own certificate,
  Cloudflare Tunnel, or a secret header the zone adds): a Worker that reaches
  an unlocked origin directly chooses the logged address (README, "Client
  address behind a CDN"). The kit ships no origin-side check.
- `cidrPolicyIssue` with `MIN_STATIC_RANGE_PREFIX` (/16, /32) and
  `MIN_TRUSTED_PROXY_PREFIX` (/12, /24); non-public space is refused, and so
  is any IPv6 range overlapping IPv4-mapped space (`::ffff:0:0/96`), which
  Node's BlockList matches IPv4 addresses against: IPv4 is written as IPv4.
- `ownTrackingId` (links) and `parsePriceAmount` (offer).
- Amazon's Amzn-SearchBot and Amzn-User: classified by `classifyAiTraffic`,
  and robots defaults for search and agent respectively.
- `unit/drift.test.ts`: re-hashes the ported upstream declarations when a
  SpecPicks checkout is present; reports drift, fails only with
  `KIT_DRIFT_STRICT=1`. Covers 81 declarations, including the bots.json URL
  and the refresh and fetch timeouts.
- `unit/index.test.ts`: the no-site-names guard walks every file the kit
  ships (comments removed per language), and the browser bundle's sources
  (`client/src` plus the `links.ts` and `clientContract.ts` it compiles in)
  are re-checked with DOM typings and the ES2018 library only, failing on any
  Node typing in that program, so browser code cannot lean on Node globals.

#### HTTP middleware and browser bundle (K2)

- `server/src/http`: `mountKit(app, ctx, deps)` mounts, in order, the
  AI-traffic logger (IP-verified `ai_traffic_log` rows), `/health`,
  `/robots.txt` (from `site.json` crawlers: search and agent allowed, training
  blocked), `/llms.txt` (the site's copy plus assistant-cited pages and the
  latest articles), the IndexNow key file, the `POST /api/outbound-click`
  beacon (`via='beacon'`), `GET /r/:asin` (Amazon only; with a catalog lookup)
  and the content-hashed browser bundle. Also `createPgKitDb`,
  `pgRedirectLookup`, a capped background writer that sheds AI-log rows before
  click rows, and the sticky mobile buy bar.
- `client/`: the vanilla click beacon and sticky-bar sizing, bundled by
  `scripts/build-client.mjs` (esbuild, deterministic, 8 KB budget).

#### Render helpers (K3)

- `server/src/render`: `renderPage` (theme tokens, base.css, the earnings
  disclosure, the 2(k) notice whenever a page shows Amazon content, sealed
  fragments), the SEO head (titles, descriptions, canonicals, indexing, never
  an Amazon `og:image`), JSON-LD builders (Product with an Offer only while
  the price is quotable, ItemList, isSimilarTo, BreadcrumbList, FAQPage,
  Article; never AggregateRating), `mdToHtml` with GFM tables
  (`table.md-table`) and affiliate-link enforcement, and sitemap sets fed
  indexable URLs only.

#### Migrations and the runner (K4)

- `migrations/kit_0001` to `kit_0004`: `outbound_clicks`, `ai_traffic_log`
  and `editorial_articles` with the reference deployment's columns (plus
  hero-image CHECKs that refuse Amazon hosts), and the verified-human views.
  `server/src/db`: the migration runner (kit files first, one ledger, one
  advisory lock), its command line, and the publish-guard template for
  content-policy sites. See `migrations/README.md`.
- The publish guard's recheck is a `NOT DEFERRABLE` constraint trigger, so a
  role holding `TRIGGER` cannot swap it with `CREATE OR REPLACE TRIGGER`, and
  it refuses the publisher's writes while any trigger on `editorial_articles`
  runs a function that neither the table owner (or a member of it) nor a
  superuser owns: code such a role plants to run as the publisher publishes
  nothing. Applying the migration drops and re-creates the recheck (an
  `ACCESS EXCLUSIVE` lock, bounded by its 5 s `lock_timeout`).

#### Deploy gate (K5)

- `tests/`: the runner (`run-local-suite.sh`: the site's production image
  against its fixture Postgres, host gate locks, nice and a CPU cap, exit codes
  2-7 and 78), `kitPlaywrightConfig`, the all-pages engine, and the specs:
  kit-compliance (tags and rel, 2(i), 2(k), Offer `validFrom`, 6(y), Amazon
  images, GFM tables, recalls, robots.txt, llms.txt, the content policy), AI
  answer-time fetchers, unbuyable product pages, CTA placements.
  `fixture-site/` and `tests/selftest/gate-selftest.mjs` prove it. See
  `tests/README.md`.

#### Deploy templates (K6)

- `deploy/`: Azure Container Apps and ECS Fargate provision, deploy and
  teardown driven by one `deploy.env` (unknown keys refused, secrets only by
  reference and never on a command line), a domain phase with a managed
  certificate, at least one replica, least-privilege database roles with
  connection limits and statement timeouts, the `s-maxage` <= 600 rule checked
  before a build and after a release, `Dockerfile.azure.tmpl` with
  `render-dockerfile.sh`, and `predeploy-gate.sh`. See `deploy/README.md`.
- The deployer block in `deploy/README.md` leaves `rg` out of `deploy.vars`,
  so the deployer never reads the serving image to turn a failed kit deploy
  (for instance exit 4 after a smoke failure whose rollback failed) into a
  success; `unit/deploy/deployerBlock.test.ts` checks the block against the
  deployer's post-flight condition.
- Teardown deletes only this site's resources on shared infrastructure: on
  AWS the ACM certificate and the host-header rule (found by `SITE_DOMAIN`)
  and, with `--with-images`, the ECR repository must carry this site's tag
  (provision tags them, and `--adopt-existing` takes over untagged ones); on
  Azure `--with-images` stops when another Container App or job runs an image
  from the repository, and `--with-certificates` deletes only the
  certificates the site's app binds, plus failed attempts.
- `Dockerfile.azure.tmpl` normalises file modes: the build stage runs
  `chmod -R a+rX,go-w` on everything the runtime stage copies (`package.json`,
  `site.json`, `dist`, `vendor/affiliate-kit` and the `DOCKER_EXTRA_PATHS`), so
  a checkout whose files others cannot read (0640 or 0660: a restrictive umask
  or default ACL, as on the fleet host's deploy worktrees) no longer builds an
  image the `node` user cannot read (reproduced 2026-11-17), and nothing there
  is writable by the server. Scaffolded sites' `BUILD_SCRIPT=build:image`
  (the same `chmod` inside the build script) stays as a second layer.

#### Scaffold and sync (K7)

- `install/create-affiliate-site.sh` with `_template/affiliate-site`: a new
  site repository with `site.json` (checked by this kit's loader), the kit's
  exact pins and lockfile, `src/index.ts` (`mountKit`, then a route table:
  home, guides from `editorial_articles`, about, disclosure, privacy,
  sitemaps), migrations applied after the kit's, the deploy-gate wiring and a
  fixture, `deploy.env` and `Dockerfile.azure` rendered from `deploy/`, azure
  and aws wrappers, `CLAUDE.md`, a KTLO config, and an implementer scope that
  keeps `vendor/**`, `site.json`, the niche policy file and the gate policy
  out of every dispatch kind wherever the framework applies it (a
  catalog-audit dispatch may write only a new migration and its summary;
  Known issues names the path that applies none). Assembled in a scratch
  directory and moved into place only when every check passes; refuses a
  non-empty directory, a path
  that resolves into the reusable-agents checkout, a slug the site registry
  already gives a site, and a slug whose gate containers or fixture volume
  exist on the host; `git init` on `main` without a remote. Generated as is,
  a site passes this kit's gate on its fixture.
- The content gate is a required choice (`--content-rule-sets` or
  `--content-policy-waived REASON`), never a silent empty list, and the
  scaffold waives the recall check in words until the site has a catalog, as
  the gate's config validation requires.
- The gate config is two files: `tests/ui/pages.manifest.ts` (the page
  families, editable by code changes, protected from deploy repairs by the
  framework's manifest glob) and `tests/ui/gate.config.ts` (operator-owned:
  compliance sources, content rule sets or the waiver, and every waiver),
  which composes the manifest and refuses one that sets a compliance source
  or grants itself a check exception, an empty-fixture allowance, a sitemap
  fixture waiver or a production-only link target.
- The site's gate shim checks `vendor/affiliate-kit` before every run, in two
  steps, each exit 78: against its `MANIFEST.sha256` (any edit, addition,
  removal or symbolic link), then against the kit version it claims in the
  reusable-agents checkout (`install/sync-site-kit.sh --verify-anchored`),
  so an edit whose manifest line was rewritten to match is refused too. With
  nothing to compare (no checkout, or no commit of that version yet) it
  warns; `KIT_ANCHOR_REQUIRED=1` makes that fatal. Both steps live in the
  site, so they narrow the read-only boundary on the deploy path but do not
  close it (Known issues).
- `tests/suite.env` names the registry sites' gate projects in
  `SUITE_HOST_LOCKS` (their locks are created when missing) plus `*`;
  `--host-locks` replaces the list. Both compose files set the runner's CPU
  cap from creation.
- `deploy.env`: `SMOKE_PATHS` lists only routes the scaffold serves, and
  `AWS_SECRET_ID` is `nsc/<slug>`; the site's deploy-config test checks the
  smoke paths against the route table and that no value of the kit example's
  placeholders is left.
- Guides render with the 2(k) notice (the layout's default); only pages that
  by construction show no Amazon content opt out.
- `install/sync-site-kit.sh`: vendors the kit into `<site>/vendor/affiliate-kit`
  with `KIT_VERSION` and `MANIFEST.sha256` (an atomic swap; refuses a
  downgrade unless asked and a half-bumped kit; `--check`, `--verify`,
  `--dry-run`; a vendored file replaced by a symbolic link counts as an
  edit), and reports the dependency pins and Dockerfile render a site must
  follow. `--verify-anchored` (read-only) passes only when the vendored copy
  is byte-identical to the kit version it claims as this checkout holds it:
  at a commit whose `VERSION` is that version (history reachable from HEAD,
  read without locks), or in the working tree with a warning. It exits 1 for
  a copy that matches no commit of a committed version or claims a version
  older than the newest committed one that no commit holds, and 4 when there
  is nothing to compare. Each site's `tests/unit/kit-integrity.test.ts`
  checks the manifest, the anchor (when the checkout is found), the versions
  and the pins.
- `unit/scaffold/`: generates sites and runs their typecheck, build and unit
  tests (the gate config through this kit's validator) offline, and loads
  their gate through Playwright; `KIT_SCAFFOLD_SMOKE=1` adds the full smoke
  (npm ci, the image build and this kit's gate on the generated site's
  fixture).

### Known issues

- A scaffolded content-policy site has no publish guard until its own
  migrations install one (`migrations/README.md`, "The publish guard"); the
  generated `CLAUDE.md`, README and the create script's closing notes say so.
  Until then `editorial_articles.status` defaults to `published`.
- **A kit site's operator-owned files are protected only by instructions on
  the implementer's claude path, its default.** The implementer applies a
  site's scope on its framework-chain path and the deployer to its repairs;
  on the claude path the session commits by itself with no scope check
  (reusable-agents `agents/implementer/README.md`, "Path scope"). There
  `vendor/**` (with its `MANIFEST.sha256`), `site.json`, the niche policy
  file, `tests/ui/gate.config.ts`, `deploy.env` and the gate wiring can all
  be edited and committed. The site's own checks narrow this but do not
  close it: the gate shim and `kit-integrity.test.ts` refuse a vendored copy
  that differs from its kit version in reusable-agents, so a rewritten
  manifest no longer passes, but both are files in the site, the anchor only
  warns for a kit version no commit holds yet, and nothing in a site checks
  `site.json`, the policy, `gate.config.ts` (`composeGateConfig` refuses a
  waiver only when the page manifest sets it) or `deploy.env`.
  `deploy.env` and `vendor/affiliate-kit/deploy` are also read from the live
  checkout, committed or not: the deployer gates and builds a clean checkout
  of the commit, but runs the push, deploy and rollback stages in the site's
  working tree (`implementer.repo_path`), and `deploy.env` sets
  `MIGRATE_COMMAND` (run with the owner's DSN), the container env, the
  replica counts and `SMOKE_PATHS`. An edit left uncommitted there ships with
  a release whose diff is clean, and no scope check sees it.
  **Prerequisite: a kit site's implementer dispatches stay disabled** (no
  agent that dispatches to the implementer for it registered or enabled,
  starting with S12's `agents/seo-opportunity-agent/site.yaml` and A1's
  registration) until either
  - the implementer's claude path applies `ScopePolicy` to its own sessions:
    it reverts or refuses excluded paths in the working tree, not only in the
    commit, or
  - the deployer refuses a release whose diff since the last green deploy
    touches an excluded path, and also refuses while the live checkout's
    `deploy.env` or `vendor/affiliate-kit` differs from the commit it ships
    (or runs the push, deploy and rollback stages from its clean checkout).
  Independently, the deployer should run `install/sync-site-kit.sh
  <worktree> --verify-anchored` from its own checkout before a kit site's
  gate and refuse the release on exit 1, and on exit 4 once the kit is
  committed: that check is the one a site cannot edit. Until then,
  `KIT_ANCHOR_REQUIRED=1` in the deployer's environment makes the site's
  own gate shim treat exit 4 as fatal.
- The anchor cannot be strict until the kit is committed: while
  `kits/affiliate-site` exists only in a working tree, a vendored copy that
  differs from it is reported as "cannot anchor" (exit 4, a warning in the
  gate), not as tampering.

### Behaviour differences from SpecPicks (deliberate)

- A price is quotable for 24h minus the HTML cache age (default 660 s), not 24h.
- `utm_source` lookups use own properties only (upstream returned
  `Object.prototype` members for `utm_source=constructor`).
- CIDR parsing is strict: `"1.2.3.4/"` is rejected instead of becoming `/0`.
- `retagAmazonUrl` accepts only the site's marketplace host and forces https.
- `amazonUrl`, `retagAmazonUrl` and `amazonSearchUrlFor` replace a tag that
  is not one of the site's own with the partner tag.
- Amounts are parsed strictly in `quotablePrice`, `honestListPrice`,
  `priceLabel` and `priceHtml`: `parseFloat` read `'1,299.00'` as 1.
- `clientIp` can skip one trusted CDN hop (SpecPicks has no CDN in front).
- Static crawler ranges broader than /16 (IPv4) or /32 (IPv6), in
  non-public space, or written as IPv4-mapped IPv6, are refused.
- Placement tags take precedence over an allowlisted segment `?tag=`.
- `PRICE_FRESH_HOURS` below the one-hour minimum that `site.json` must keep is
  honoured, not fatal.
- No eBay or retro routing anywhere.

### Parity

- Upstream fixes to queue for SpecPicks (P3-1): the two SSR notices (2(i)
  and 2(k)); the `utm_source` prototype lookup; strict amount parsing in the
  price rule, list price and labels; and its PDP head snippet, which prints a
  substitute's price in the meta description with a date and no 2(i)
  disclaimer (`src/services/ssrHead.ts`, "In stock instead: ..., $X on Amazon
  (checked <day>)").
