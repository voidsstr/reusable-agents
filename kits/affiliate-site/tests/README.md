# Kit gate engine (build item K5)

The deploy gate every kit site runs before it ships: the site's **production
image**, built from the tree being deployed, started on a local port against
the site's **fixture Postgres**, audited by Playwright the way readers,
crawlers and assistants see it, and checked against the Amazon Associates
licence. Ported from SpecPicks' gate (`tests/run-local-suite.sh`,
`tests/ui/all-pages.spec.ts` and its libs, `ai-fetcher-access.spec.ts`, the
compliance subset of `amazon-cta.spec.ts`, `unbuyable-pdp.spec.ts`,
`pdp-cta-placements.spec.ts`); everything SpecPicks hard-codes comes from the
site's `site.json`, `tests/suite.env` and `tests/ui/gate.config.ts`.

It proves itself on `../fixture-site/`: green on the clean fixture, red on
every deliberately seeded violation (`node tests/selftest/gate-selftest.mjs`).
The fixture runs on its own ports (app 10032, Postgres 5434), apart from every
site's gate.

## Layout

| Path | What it is |
|---|---|
| `run-local-suite.sh` | the runner (build, start, health, freshen, Playwright), parameterised by `suite.env` |
| `suite.env.example` | every runner setting, for a site to copy |
| `playwright.base.ts` | `kitPlaywrightConfig()`: a site's `playwright.config.ts` in three lines |
| `ui/all-pages.engine.ts` + `ui/all-pages.spec.ts` | every page family, JS off and on, phone and desktop (the all-pages engine) |
| `ui/kit-compliance.spec.ts` | the licence and content rules, on every family; not waivable |
| `ui/ai-fetcher-access.spec.ts` | assistants' answer-time fetchers get what a browser gets |
| `ui/unbuyable-pdp.spec.ts` | an unbuyable product page is never a dead end |
| `ui/pdp-cta-placements.spec.ts` | each buy placement reports its own name to the click beacon |
| `ui/lib/` | `site` (hosts, sampling, sitemaps), `pageAudit`, `legibility`, `manifestTypes` (the site contract), `sources`, `gateConfig`, `compliance` + `complianceCollect`, `robotsTxt`, `llmsTxt`, `contentPolicyScan`, `db`, `pdpInspect`, `routes` |
| `unit/` | jest tests of the engine itself (`npx jest -c tests/jest.config.js`) |
| `selftest/gate-selftest.mjs` | the end-to-end proof on the fixture site |

## Wiring a site

```
<site>/tests/suite.env              settings (copy suite.env.example)
<site>/tests/run-local-suite.sh     exec bash "$here/../vendor/affiliate-kit/tests/run-local-suite.sh" --suite-env "${SUITE_ENV:-$here/suite.env}" "$@"
<site>/tests/playwright.config.ts   export default kitPlaywrightConfig({ dir: __dirname, siteJson: '../site.json', gateConfig: './ui/gate.config.ts', siteSpecsDir: './ui' })
<site>/tests/ui/gate.config.ts      export const config: GateConfig = { families, routes, compliance, ... }
<site>/tests/ui/*.spec.ts           the site's own niche specs (Playwright project 'site')
<site>/docker-compose.yml           the fixture Postgres (long-lived, no restart policy)
<site>/docker-compose.test.yml      the app under test, built from the production Dockerfile
```

The site's root `package.json` must carry `@playwright/test` (the kit's pin,
1.62.1) and `pg`: the kit's specs resolve them from the site. A clean worktree
borrows the live checkout's `node_modules` (`SUITE_NODE_MODULES`). The deployer
contract is SpecPicks': `bash run-local-suite.sh --workers=N` with cwd `tests/`,
plus `--reporter=line,json --output <dir>`, `GATE_SOURCE_REPO`,
`GATE_ARTIFACT_DIR` (gets `image-under-test.txt`, and `container.log` on a red
run) and `PLAYWRIGHT_JSON_OUTPUT_FILE`. `deploy/predeploy-gate.sh` runs the same
suite before a manual deploy.

### The gate config (`ui/lib/manifestTypes.ts`)

- `families`: every public page family: route patterns, a sample source on the
  LOCAL server (`fixed`, `fromSitemaps`, `linksOn`, `fromApi` in `ui/lib/sources.ts`),
  sample size and shards, expected status, `index`/`noindex`, whether it must be
  monetized, and per-check `exceptions`, each with its reason. A family that
  cannot be sampled fails unless it is `fixtureOptional` (with the reason).
- `routes.declared`: the route table's page routes; a route with no family (or a
  `routes.nonPage` reason) fails the gate.
- `sitemaps`: the index path, the sub-sitemaps (`files`: one test each),
  fixture-empty ones with reasons, sample caps.
- `compliance`: `amazonTitles` and `recalledProducts` (read from the fixture DB,
  not from the pages, so a renderer cannot hide what it forgot; give each
  recalled SKU its model `name` so an Amazon search for it is caught too),
  `recallNoticeSelector`, `tablePages` / `articlePath`, `contentPolicy`
  (`ruleSets`, an optional `configFile` overlay, `frameworkDir`). The recall
  source and at least one content-policy rule set are required (the recall and
  content gates are never cut): a site that has none states why in
  `recallsWaived` / `contentPolicyWaived`, printed as the skip reason in every
  run. A config with neither does not load, so every kit spec fails.
- `aiFetchers`, `unbuyable`, `placements`, `phoneSweep`, `chrome`,
  `redirects`, `cacheKeyCases`, `prodOnlyLinkTargets`.

## The runner

```bash
bash tests/run-local-suite.sh --workers=4                    # the gate (from a site: its shim)
bash tests/run-local-suite.sh --print-config                 # resolved settings, nothing run
bash tests/run-local-suite.sh ui/kit-compliance.spec.ts      # one spec (args go to Playwright)
KEEP_UP=1 bash tests/run-local-suite.sh                      # leave the app container up
```

Exit codes (the deployer's `framework/core/deploy_repair.py` classifies them):
Playwright's own (0 pass, 1 tests failed); **2** fixture DB not up; **3** the
image does not build; **4** the container did not start; **5** it exited before
it was healthy; **6** it never became healthy; **7** gave up waiting for another
run's lock; **8** the freshen SQL failed (it runs after the commit's own
migrations, so this is usually the commit); **78** bad settings; 130/143
interrupted/terminated (cleanup still runs, and stops the step in flight).

Sharing the host: the run holds `local-suite-<SUITE_PROJECT>.lock` (the lock
SpecPicks' runner takes) and every lock `SUITE_HOST_LOCKS` names, waiting up to
`SUITE_LOCK_WAIT_S` with a line a minute. A named project's lock file is
created when missing, so the exclusion holds even before that gate has ever run
(the lock directory is tmpfs, empty after a reboot). `'*'` adds every
`local-suite-*.lock` present when the locks are taken: on its own it is best
effort, so a site names the gates it must not overlap and keeps `'*'` beside
them (`SUITE_HOST_LOCKS='other-site another-site *'`). The locks are taken all
or nothing: when one is busy every lock already taken is released and the set
is retried after `SUITE_LOCK_POLL_S`, so a waiting run never blocks a gate it
does not conflict with, and two runners cannot deadlock.

CPU: the run is niced (`SUITE_NICE`, default 10), which reaches Playwright and
Chromium, and the app container is capped with `docker update --cpus`
(`SUITE_CPUS`, default 2) once it starts. Containers and image builds run under
dockerd, which nice does not reach: a site's compose files set
`cpus: ${SUITE_CPUS:-0}` on the app and fixture-DB services so the cap holds
from creation (the runner exports `SUITE_CPUS`; the fixture site does this).
BuildKit image builds are not capped.

The target: `kitPlaywrightConfig()` runs against `TEST_URL` (the runner sets it
to the image it started; a production smoke sets the live origin), else
`SUITE_APP_HOST:SUITE_APP_PORT`, else a `webServer` the site's overrides start.
There is no default port: without any of them it refuses to run, so a hand run
never audits whatever happens to listen on some port.

The freshen SQL runs once the app is healthy: the commit's own migrations have
run by then, and nothing has been rendered (or cached) yet.

## What kit-compliance asserts

On every family's sample, a seeded sample of every sitemap URL, the recalled
SKUs' pages and the pages that must render a table, loaded with JS off:

| Rule | Fails on |
|---|---|
| affiliate links | rel without sponsored / nofollow / noopener (or with noreferrer), no `target=_blank`, a tag that is not one of `site.json`'s tracking IDs, no tag, two tags, short links or other storefronts; a `/r/` link (relative, or absolute on any of the site's hosts) without sponsored / nofollow |
| disclosure (Operating Agreement 5) | a page with a visible affiliate link that does not show `site.json` `disclosure.earnings` in plain view, or shows it only after the first such link (ported from SpecPicks' amazon-cta disclosure check) |
| prices, 2(i) | a stamp (`[data-price-stamp]`) without a `<time>` inside the quotable window (24h minus the HTML cache lifetimes: `site.json` `prices` and `cache`), or whose exact 2(i) disclaimer is neither in view nor behind a visible control inside it (popovertarget, aria-controls, `href="#..."`, a `<details>`); a stamp that does not SHOW its check time as a date and a time with a zone ("2026-11-17 14:11 UTC", "01/07/2008 14:11 PST", "Nov 17, 2026, 9:11 AM EST"), or shows another instant than its `<time datetime>`; a stamp without a visible control labelled "Details" that reveals the exact disclaimer (Amazon's own example; an inline disclaimer alone does not do); a price stamp holding a second price, or an availability stamp holding one; a price with cents, "$N on Amazon", a currency-code amount ("USD 349.99"), a "$N (as of ...)" or "$N (checked ...)", or a whole-dollar "$N" after a price cue ("sells for", "now just", "total", "from"; not a budget, range or rate) outside a stamp, whether a reader sees it, only crawlers and assistants read it (a hidden element) or it sits in an `aria-label`, `title` or `alt`; an Amazon price ("$N on Amazon", "$N as of") inside `data-kit-not-a-price`; a `.ssr-price` outside a stamp (`data-kit-not-a-price` included); availability ("in stock", "only 3 left", "currently unavailable", "ships within") outside a stamp; a price or availability in the title, a description, og:title, twitter:title or an image alt; a `product:price:*` / `og:price:*` meta tag; a page with a price whose Cache-Control lets a shared cache keep it longer than `cache.htmlSMaxAgeSeconds` (at most 600) or a browser longer than `cache.htmlMaxAgeSeconds`, whose CDN-Cache-Control / Surrogate-Control lets a CDN keep it longer, or that carries stale-while-revalidate / stale-if-error |
| notice, 2(k) | a page with a `data-amazon-content` marker, a stamp, or a known Amazon title (from the fixture DB; whole, or its first 40 characters as a truncated title shows it) that does not show the exact 2(k) notice in plain view |
| Offer JSON-LD | `validFrom` missing, unparseable, past the quotable window or in the future; not USD; not a real price; `seller` / returns / shipping claims; an Offer with no visible dated price; any AggregateRating |
| 6(y) | price-alert or price-tracking forms and controls; "price alert", "price history", "lowest ever", "all-time low", "record low", "never been cheaper", "last tracked", "get notified ... price", "track this price" copy. A negation lets a phrase through only when it governs it ("no price alerts", "we do not offer price alerts or a price history"); selling idioms ("never miss", "don't miss", "no other") never do |
| images, 2(h) | an Amazon CDN host in og:image, twitter:image, an article hero (`img[data-hero-image]`), any JSON-LD image property (Product, Article, logo, ImageObject), a sitemap `<image:loc>`, or `editorial_articles.hero_image_url` / `body_md` (fixture DB) |
| tables | raw GFM table text on the page; a `table.md-table` outside `.md-table-wrap`, without `scope="col"` headers or with ragged rows; an article whose Markdown has a table but whose page renders none |
| recalls | any buy button naming a recalled SKU, on any page: a `/dp/` link, a cart link's `ASIN.N`, a `/r/` link (relative or on the canonical origin, as the kit's Markdown writes it), an Amazon search for its model name; the recalled page without its visible notice, or with an Offer; `/r/<ASIN>` sending a recalled SKU to Amazon |
| robots.txt | a search or assistant token blocked from `/` or a family's page; a training token not blocked (unless `trainingCrawlersAllowed` says why); `site.json` private paths open to `*` or to any allowed crawler's own group; no Sitemap line. The tokens are the kit's default lists united with `site.json`'s: a site can add a token or move one to another class (reported), never drop one |
| llms.txt | not `# <brand>` first, no `>` summary, no earnings disclosure, a price anywhere, a relative link, an on-site link that does not answer 200 |
| content policy | any reject-level finding of `framework/cli/content_policy_scan.py` over the rendered pages and every published article (`--no-storage`, the site's rule sets) |

Money that is not an Amazon price (an electricity rate, a budget) goes inside
an element marked `data-kit-not-a-price`; budgets, ranges and rates without
cents ("under $500", "$200 to $500", "$40 a year") need nothing. The marker
never hides an Amazon price: "$N on Amazon", "$N (as of ...)" and the kit's
`.ssr-price` still fail inside it.

Production runs. `kit-compliance` and `all-pages` skip themselves against
production. The `@prod-safe` specs may run there (a site's keep-the-lights-on
smoke runs `--grep @prod-safe`): `pdp-cta-placements` answers the merchant, the
`/r/` hop and the click beacon itself on the browser context, so no click row is
written and nothing reaches Amazon from any tab; `unbuyable-pdp` reads pages
with the gate's plain-browser User-Agent there, so it writes nothing;
`ai-fetcher-access` must send assistants' User-Agents, so each production run
logs a few dozen `ai_traffic_log` rows: the host that runs it belongs in
`AI_TRAFFIC_EXCLUDE_IPS`, which the AI-traffic readers drop.

## Tests

```bash
cd kits/affiliate-site
npx jest -c tests/jest.config.js           # the engine's unit tests (offline; runs the real content-policy scanner)
node tests/selftest/gate-selftest.mjs       # the end-to-end proof (docker; ~1 min warm)
```
