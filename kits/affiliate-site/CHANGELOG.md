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

- `deploy/Dockerfile.azure.tmpl` copies the build context with the checkout's
  file modes and runs the server as `node`, so a checkout whose files are not
  readable by others (0640 or 0660: a restrictive umask or default ACL, as on
  the fleet host's deploy worktrees under `~/.reusable-agents`) builds an
  image that cannot read `site.json`, `package.json`, `vendor/affiliate-kit`
  or the `DOCKER_EXTRA_PATHS` (reproduced 2026-11-17). Sites from
  `create-affiliate-site.sh` work around it with `BUILD_SCRIPT=build:image`
  (`chmod -R a+rX` on those paths inside the build stage). The template should
  normalise the modes itself (`COPY --chmod`, or a `chmod` in the build stage
  as `fixture-site/Dockerfile` does).
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
  file, `tests/ui/gate.config.ts` and the gate wiring can all be edited and
  committed. The site's own checks narrow this but do not close it: the gate
  shim and `kit-integrity.test.ts` refuse a vendored copy that differs from
  its kit version in reusable-agents, so a rewritten manifest no longer
  passes, but both are files in the site, the anchor only warns for a kit
  version no commit holds yet, and nothing in a site checks `site.json`, the
  policy or `gate.config.ts` (`composeGateConfig` refuses a waiver only when
  the page manifest sets it).
  **Prerequisite: a kit site's implementer dispatches stay disabled** (no
  agent that dispatches to the implementer for it registered or enabled,
  starting with S12's `agents/seo-opportunity-agent/site.yaml` and A1's
  registration) until either
  - the implementer's claude path applies `ScopePolicy` to its own commits
    (reverts or refuses excluded paths), or
  - the deployer refuses a release whose diff since the last green deploy
    touches an excluded path.
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
