# Gate fixture site

A minimal affiliate site built only from the kit's modules (`mountKit`, the
layout shell, price and availability stamps, CTAs, the sticky bar, Markdown
with GFM tables, JSON-LD, sitemaps, the migration runner), on a seeded fixture
Postgres. It exists so the kit gate (`../tests`) can prove itself: green on the
clean fixture, red on every deliberately seeded violation.

It is never deployed and nothing here runs on its own: no restart policy, no
timers; the gate starts it and removes the app container afterwards.

## Pages

| Route | Family | Shows |
|---|---|---|
| `/` | home | product cards with stamps and CTAs |
| `/product/:asin` | product | in stock (stamp, Amazon title, hero CTA, sticky bar, Offer); stale price (no price, no title, CTA kept); checked just past the quotable window, 24h minus the HTML cache lifetimes (B0GATE0009: no price, no Offer, no title); unavailable (dated stamp + an alternative with its own dated price); recalled (notice, substitute, no own buy button) |
| `/guides`, `/guides/:slug` | guides-index, guide | editorial_articles rendered with `mdToHtml` (GFM table, FAQ, buy strip) |
| `/best/:slug` | best-list | ranked list with a GFM comparison table and an ItemList |
| `/recalls`, `/recalls/:id` | recalls-index, recall | recall notices |
| `/notes/:slug` | note | hand-written HTML (a renderer that trusts its HTML; the violations land here) |
| `/about`, `/disclosure` | static | no Amazon content |

`src/routes.ts` is the route table the app mounts and the gate config reads.

## Scenarios

| | Database | Overlays | Gate result |
|---|---|---|---|
| clean (`tests/suite.env`) | `kit_fixture` | none | every test passes |
| violations (`tests/suite.violations.env`) | `kit_fixture_violations` (`db/migrations/0003_fixture_violations.sql`, `0005_fixture_more_violations.sql` and `0006_fixture_stamp_violations.sql` seed it) | `site.violations.json` (assistants blocked, training crawlers allowed), `llms.violations.json` (a price) | every kit-compliance rule fails on its own violation |

Seeded violations (0003): an Amazon link with someone else's tag and no rel;
prices printed without a stamp; a hand-made stamp a week old without the 2(i)
disclaimer; an Amazon title with no 2(k) notice; an Offer checked in January;
a price-alert form and "lowest price ever"; an Amazon CDN og:image; an Amazon
CDN `hero_image_url` (the kit's CHECK constraints dropped first); a GFM table
broken by a blank line; an article the fixture content-policy rule set
(`content-policy.json`) rejects; a guide with a buy button for the recalled SKU.

More (0005), each slipping past a narrower check: a buy button for the
recalled SKU through the kit's own `/r/` link (written absolute on the
canonical origin), as an Amazon search for its model and as a cart link
carrying it second; a page whose CSS hides the earnings disclosure; whole-dollar
prices, a price in og:title and a `product:price` meta tag; "in stock on Amazon,
only 3 left" outside a stamp; a stamp whose 2(i) disclaimer is hidden with no
control to show it; a complete stamp 23h53m old (inside 24h, past the quotable
window); a truncated Amazon title with no 2(k) notice; "never miss a deal",
"don't miss", "no other store" and "get notified" copy; an Amazon image link
stored in an article body; raw JSON-LD with an Amazon Product image and a
rating.

And (0006, kit 0.2.0's stamp rule): a price dated by hand inside a sentence
("$349 (as of Nov 17)") with no stamp; stamps that show a date but no time or
zone, print the 2(i) disclaimer inline with no "Details" control, hold two
prices, or show a time that is not their own `<time datetime>`; and prices a
reader never sees (a hidden element, an `aria-label`) or that hide inside
`data-kit-not-a-price`. `db/freshen.sql` now re-stamps the shown time of
0005's two hand-made stamps along with their datetime, so each stays red for
its own reason only.

Both databases live in one long-lived Postgres container (`kitfixture_postgres`,
port 5434; `db/Dockerfile` adds the second database at init). The app applies
the kit's migrations and `db/migrations` at boot (`0004_fixture_edge_rows.sql`
adds the window-edge product to both); `db/freshen.sql` re-stamps the Amazon
check times, and the violations' two hand-made stamps, at every gate start.
New fixture rows go in a new migration file: the runner records each file once
by name, so an edited file never reaches a database that already applied it.

## Running

```bash
cd kits/affiliate-site
bash fixture-site/tests/run-local-suite.sh                                                     # the clean gate
SUITE_ENV="$PWD/fixture-site/tests/suite.violations.env" bash fixture-site/tests/run-local-suite.sh   # red on purpose
node tests/selftest/gate-selftest.mjs                                                          # both, checked
```

App on 10032, fixture Postgres on 5434 (fixture credentials `kitfixture`): the
kit's own ports, apart from every site's gate (a fixture database is
long-lived, so two gates must never share its port). For a
local server without Docker: `npx tsc -p fixture-site/tsconfig.build.json`,
`node scripts/build-client.mjs --outdir fixture-site/dist/client`, then
`FIXTURE_DIR=fixture-site DATABASE_URL=... node fixture-site/dist/fixture-site/src/server.js`.
