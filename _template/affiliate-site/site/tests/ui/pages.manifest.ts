import { fixed, fromSitemaps } from '../../vendor/affiliate-kit/tests/ui/lib/sources';
import { GUIDES_PATH } from '../../src/routes/table';
import type { PageManifest } from './gate.config';

/**
 * This site's page manifest: every page family the site serves, where the
 * LOCAL server under test offers samples of each (never production), and what
 * each must be (vendor/affiliate-kit/tests/ui/lib/manifestTypes.ts documents
 * every field). The gate audits a seeded sample of every family.
 *
 * Every page route in src/routes/table.ts needs a family here (the gate fails
 * on one without), and every sub-sitemap the index lists needs an entry in
 * sitemaps.files. A change that adds a page route adds its family here.
 *
 * What the gate may skip is not set here: a check exception, a family whose
 * fixture may be empty, a sitemap empty on the fixture, a production-only link
 * target, and every compliance source are granted in tests/ui/gate.config.ts
 * (operator-owned), which refuses this manifest if it sets them. A deploy
 * repair never edits this file (reusable-agents deploy_repair protects every
 * manifest under tests/, as it does the reference sites' page manifests): a
 * red gate is fixed in the code, not by describing the site differently.
 */
export const PAGE_MANIFEST: PageManifest = {
  families: [
    {
      id: 'home', routes: ['/'], sourceLabel: 'fixed', source: fixed('/'), sample: 1, robots: 'index',
      // The scaffold's home page lists guides, not products. Make it true once product cards with CTAs land.
      monetized: false,
    },
    {
      id: 'guides-index', routes: [GUIDES_PATH], sourceLabel: 'fixed', source: fixed(GUIDES_PATH), sample: 1, robots: 'index',
      monetized: false,
    },
    {
      id: 'guide', routes: [`${GUIDES_PATH}/:slug`], sourceLabel: 'sitemap-guides.xml',
      source: fromSitemaps(['sitemap-guides.xml']), sample: 4, robots: 'index', monetized: false,
    },
    {
      id: 'static', routes: ['/about', '/disclosure', '/privacy'], sourceLabel: 'fixed',
      source: fixed('/about', '/disclosure', '/privacy'), sample: 3, robots: 'index', monetized: false,
    },
    {
      id: 'not-found', routes: [], sourceLabel: 'fixed (a path no route serves)', source: fixed('/no-such-page'),
      sample: 1, status: 404, robots: 'noindex', monetized: false,
    },
  ],
  sitemaps: { files: ['sitemap-pages.xml', 'sitemap-guides.xml'] },
  chrome: { page: '/', minLinks: 4 },
};
