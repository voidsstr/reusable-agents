import * as path from 'path';
import { gateDbUrl, withDb } from '../../../tests/ui/lib/db';
import type { GateConfig, RecalledProduct, Source, SourceContext } from '../../../tests/ui/lib/manifestTypes';
import { fixed, fromSitemaps, linksOn } from '../../../tests/ui/lib/sources';
import { nonPageRoutes, pageRoutes } from '../../src/routes';

/**
 * The gate fixture's gate config: every page family it serves, and the
 * fixture facts the kit's specs need. A real site's tests/ui/gate.config.ts
 * has the same shape (its families come from its route table).
 *
 * Facts the gate must not learn from the pages themselves (which SKUs are
 * recalled, which titles came from Amazon) are read from the fixture
 * database, so a renderer that forgets one cannot also hide it from the gate.
 */

const SITEMAPS = ['sitemap-hub.xml', 'sitemap-products-1.xml', 'sitemap-guides.xml', 'sitemap-best.xml', 'sitemap-notes.xml', 'sitemap-recalls.xml'];

/** Rows from the fixture DB, or `fallback` when the gate runs without one. */
function fromDb(sql: string, map: (row: Record<string, unknown>) => string, fallback: Source): Source {
  return async (ctx) => {
    const url = gateDbUrl();
    if (!url) return fallback(ctx);
    return withDb(url, async (c) => (await c.query(sql)).rows.map(map));
  };
}

const productPath = (row: Record<string, unknown>): string => `/product/${String(row.asin)}`;

/**
 * SKUs under an active recall, with their model names (an Amazon search for
 * the model is a buy button too). Without the fixture DB, the recall index's
 * product links (no names).
 */
async function recalledProducts(ctx: SourceContext): Promise<RecalledProduct[]> {
  const url = gateDbUrl();
  if (!url) {
    const paths = await linksOn('/recalls', /^\/product\/[A-Z0-9]{10}$/)(ctx);
    return paths.map((p) => ({ asin: p.split('/').pop() as string, path: p }));
  }
  return withDb(url, async (c) => (await c.query(
    `SELECT DISTINCT r.asin, p.model_name FROM fixture_recalls r JOIN fixture_products p ON p.asin = r.asin
      WHERE r.status <> 'closed' ORDER BY r.asin`,
  )).rows.map((row: Record<string, unknown>) => ({ asin: String(row.asin), path: productPath(row), name: String(row.model_name) })));
}

const unbuyablePaths = fromDb(
  "SELECT asin FROM fixture_products WHERE is_active AND lifecycle = 'unavailable' ORDER BY asin",
  productPath,
  fixed('/product/B0GATE0006'),
);

const inStockPaths = fromDb(
  `SELECT p.asin FROM fixture_products p JOIN fixture_item_cache c ON c.asin = p.asin
    WHERE p.is_active AND p.lifecycle = 'live' AND c.price IS NOT NULL AND c.fetched_at > now() - interval '12 hours'
      AND NOT EXISTS (SELECT 1 FROM fixture_recalls r WHERE r.asin = p.asin AND r.status <> 'closed')
    ORDER BY p.rank, p.asin LIMIT 1`,
  productPath,
  fixed('/product/B0GATE0002'),
);

export const config: GateConfig = {
  families: [
    { id: 'home', routes: ['/'], sourceLabel: 'fixed', source: fixed('/'), sample: 1, robots: 'index', monetized: true },
    {
      id: 'product', routes: ['/product/:asin'], sourceLabel: 'sitemap-products-1.xml',
      source: fromSitemaps(['sitemap-products-1.xml']), sample: 8, shards: 2, robots: 'index', monetized: true,
    },
    { id: 'guides-index', routes: ['/guides'], sourceLabel: 'fixed', source: fixed('/guides'), sample: 1, robots: 'index', monetized: false },
    {
      id: 'guide', routes: ['/guides/:slug'], sourceLabel: 'sitemap-guides.xml',
      source: fromSitemaps(['sitemap-guides.xml']), sample: 4, robots: 'index',
      monetized: { most: 'a guide carries a buy strip only when it names in-stock products' },
    },
    {
      id: 'best-list', routes: ['/best/:slug'], sourceLabel: 'sitemap-best.xml',
      source: fromSitemaps(['sitemap-best.xml']), sample: 2, robots: 'index', monetized: true,
    },
    { id: 'recalls-index', routes: ['/recalls'], sourceLabel: 'fixed', source: fixed('/recalls'), sample: 1, robots: 'index', monetized: false },
    {
      id: 'recall', routes: ['/recalls/:id'], sourceLabel: 'sitemap-recalls.xml',
      source: fromSitemaps(['sitemap-recalls.xml']), sample: 2, robots: 'index', monetized: false,
    },
    {
      id: 'note', routes: ['/notes/:slug'], sourceLabel: 'sitemap-notes.xml',
      source: fromSitemaps(['sitemap-notes.xml']), sample: 4, robots: 'index', monetized: false,
    },
    { id: 'static', routes: ['/about', '/disclosure'], sourceLabel: 'fixed', source: fixed('/about', '/disclosure'), sample: 2, robots: 'index', monetized: false },
    {
      id: 'not-found', routes: [], sourceLabel: 'fixed (a path no route serves)', source: fixed('/no-such-page'),
      sample: 1, status: 404, robots: 'noindex', monetized: false,
    },
  ],
  routes: { declared: pageRoutes, nonPage: nonPageRoutes() },
  sitemaps: { files: SITEMAPS },
  chrome: { page: '/', minLinks: 5 },
  phoneSweep: [
    { id: 'product', sourceLabel: 'sitemap-products-1.xml', source: fromSitemaps(['sitemap-products-1.xml']), n: 8 },
  ],
  compliance: {
    shards: 2,
    amazonTitles: fromDb('SELECT title FROM fixture_item_cache WHERE title IS NOT NULL ORDER BY asin', (r) => String(r.title), fixed()),
    recalledProducts,
    recallNoticeSelector: '[data-recall-notice]',
    articlePath: '/guides/{slug}',
    contentPolicy: {
      ruleSets: ['kit-fixture'],
      configFile: path.resolve(__dirname, '..', '..', 'content-policy.json'),
    },
  },
  unbuyable: { products: unbuyablePaths },
  placements: { inStock: inStockPaths, unbuyable: unbuyablePaths },
};
