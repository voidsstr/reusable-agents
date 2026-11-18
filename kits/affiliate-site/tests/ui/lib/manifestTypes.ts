import type { APIRequestContext } from '@playwright/test';

/**
 * The contract between the kit's gate engine and a site.
 *
 * A site describes itself in one gate config module (its tests/ui/gate.config.ts,
 * found through KIT_GATE_CONFIG, which tests/playwright.base.ts sets):
 *   - families: every public page family it serves, where the local server
 *     offers samples of each, and what each must be (all-pages.engine.ts audits
 *     a seeded sample of every family, kit-compliance.spec.ts checks them all);
 *   - routes: the route table, so a route with no family fails the gate;
 *   - the sources the compliance, assistant-fetcher, unbuyable-PDP and
 *     CTA-placement specs need (recalled SKUs, known Amazon titles, ...).
 *
 * Ported from SpecPicks tests/ui/pages.manifest.ts (its types and source
 * helpers). Expectations are the defaults below; a family states only where it
 * differs, and every waiver carries its reason (printed as a test annotation).
 * No kit-compliance rule is waivable per family. A site states its recall
 * source and content-policy rule sets, or (recallsWaived /
 * contentPolicyWaived) why it has none: a config with neither does not load.
 */

export type CheckId =
  | 'status' | 'title' | 'description' | 'canonical' | 'robots' | 'h1' | 'structure' | 'soft-404'
  | 'visible-code' | 'json-ld' | 'monetized' | 'cta-compliance' | 'price-text' | 'images'
  | 'bad-links' | 'links' | 'page-errors' | 'js-h1' | 'js-canonical' | 'js-robots' | 'js-ctas'
  | 'js-json-ld' | 'js-structure' | 'phone-overflow' | 'desktop-overflow' | 'legibility';

export const CHECK_IDS: readonly CheckId[] = Object.freeze([
  'status', 'title', 'description', 'canonical', 'robots', 'h1', 'structure', 'soft-404',
  'visible-code', 'json-ld', 'monetized', 'cta-compliance', 'price-text', 'images',
  'bad-links', 'links', 'page-errors', 'js-h1', 'js-canonical', 'js-robots', 'js-ctas',
  'js-json-ld', 'js-structure', 'phone-overflow', 'desktop-overflow', 'legibility',
] as CheckId[]);

export interface SourceContext {
  request: APIRequestContext;
}

/** Site paths (path + query) the local server offers for a family or check. */
export type Source = (ctx: SourceContext) => Promise<string[]>;

export interface PageFamily {
  id: string;
  /** Route patterns (express syntax: '/product/:asin') this family covers. */
  routes: string[];
  /** Where samples come from, as printed in the test annotations. */
  sourceLabel: string;
  source: Source;
  /** How many URLs to audit (seeded sample of the source). */
  sample: number;
  /** Split the sample over this many tests so workers share it. */
  shards?: number;
  /** Expected HTTP status (default 200). */
  status?: number;
  robots: 'index' | 'noindex';
  /** Expected canonical path for a sampled path; default: the path itself. */
  canonical?: { expect: (path: string) => string; reason: string };
  /**
   * true: every sampled URL shows at least one visible affiliate CTA in
   * <main> without JS. { most }: at least half of a test's URLs do (each
   * page's CTAs come from its own data, and a page with none is legitimate).
   */
  monetized: boolean | { most: string };
  jsOn?: {
    /** 'same' (default): the JS-on H1 is the JS-off H1. */
    h1?: 'same' | { own: string };
    /** 'same' (default): same visible CTA count; atLeastOne: still monetized. */
    ctas?: 'same' | { atLeastOne: string };
  };
  /** The <title> may be the bare brand name (reason). */
  siteNameTitle?: string;
  /** Zero samples on the local fixture is expected (reason); otherwise it fails. */
  fixtureOptional?: string;
  /** Checks that do not apply to this family, each with its reason. */
  exceptions?: Partial<Record<CheckId, string>>;
}

export interface RedirectCase {
  from: string;
  /** The canonical target of a 301. */
  to: string;
  /** Default 301; set another status (e.g. 410) to assert it instead. */
  status?: number;
}

export interface SweepSpec {
  /** Name in the test title. */
  id: string;
  sourceLabel: string;
  source: Source;
  /** URLs loaded (seeded sample). */
  n: number;
  shards?: number;
}

/** A product whose page must not carry its own buy button (an active recall). */
export interface RecalledProduct {
  asin: string;
  /** Its product page. */
  path: string;
  /**
   * Its model name (from the fixture DB): an Amazon search link for it is a
   * buy button too. Without it only links naming the ASIN are caught.
   */
  name?: string;
}

export interface ContentPolicyGateConfig {
  /** Rule sets to apply (repeatable --rule-set); at least one (ComplianceGateConfig.contentPolicyWaived is the only way out). */
  ruleSets: string[];
  /** A JSON config laid over the framework's config/content-policy-config.json (--config). */
  configFile?: string;
  /** --site hint (only used for rule_overrides; the rule sets come from ruleSets). */
  site?: string;
  /** The reusable-agents checkout that holds framework/cli/content_policy_scan.py. */
  frameworkDir?: string;
  /** Pages scanned beyond the compliance page set (e.g. every article). */
  extraPages?: Source;
  /** Python interpreter (default python3). */
  python?: string;
}

export interface ComplianceGateConfig {
  /**
   * Sitemap URLs checked per run (seeded sample of every URL in the local
   * sitemaps), on top of every family's own samples. Default 150.
   */
  maxSitemapPages?: number;
  /** Pages checked that are in no family and no sitemap (noindex pages, variants). */
  extraPages?: Source;
  /** Split the page set over this many tests per assertion (default 2). */
  shards?: number;
  /**
   * Amazon titles the fixture can show (2(k)): a page whose visible text
   * holds one must show the 2(k) notice even when its renderer forgot the
   * data-amazon-content marker. Titles shorter than 20 characters are ignored.
   */
  amazonTitles?: Source;
  /**
   * SKUs under an active recall: no buy button for them on any page. Required
   * (binding amendment 11: the recall gate is never cut) unless
   * recallsWaived says why the site has none to check.
   */
  recalledProducts?: (ctx: SourceContext) => Promise<RecalledProduct[]>;
  /**
   * The reason a site has no recall source yet (e.g. a scaffold with no
   * product pages). The recall test is then skipped with this reason, printed
   * in every run. Set this or recalledProducts, not both.
   */
  recallsWaived?: string;
  /** The visible recall notice on a recalled product's page (default '[data-recall-notice]'). */
  recallNoticeSelector?: string;
  /** Status a recalled product's page answers with (default 200: the page stays, the button goes). */
  recalledStatus?: number;
  /** Pages that must render at least one GFM table (their source Markdown has one). */
  tablePages?: Source;
  /** Article page path for an editorial_articles slug (default '/guides/{slug}'). */
  articlePath?: string;
  /** robots.txt paths assistants and search engines must be allowed (default: '/', and one sample per family). */
  robotsKeyPaths?: Source;
  /**
   * A site that deliberately lets training crawlers in (the kit default
   * blocks them) states why here; robots.txt is then not checked for that.
   */
  trainingCrawlersAllowed?: string;
  /**
   * The content-policy scan of the rendered pages. Required, with at least one
   * rule set (binding amendment 11: the content gate is never cut), unless
   * contentPolicyWaived says why.
   */
  contentPolicy?: ContentPolicyGateConfig | null;
  /** The reason a site runs no content-policy rule set; the scan is then skipped with it. Set this or contentPolicy, not both. */
  contentPolicyWaived?: string;
}

export interface AiFetcher {
  /** robots.txt token. */
  token: string;
  /** The full User-Agent string the fetcher sends. */
  ua: string;
}

export interface UnbuyableGateConfig {
  /** Product pages that are unavailable on Amazon (they must still offer something buyable). */
  products: Source;
  /** Visible text that says the listing cannot be bought (default: the kit's unavailable stamp). */
  unavailableRe?: RegExp;
}

export interface PlacementGateConfig {
  /** An in-stock product page with a hero CTA and a sticky bar. */
  inStock?: Source;
  /** Unbuyable product pages, one of which shows an alternative. */
  unbuyable?: Source;
  /** Placement names, by role. */
  hero?: string[];
  sticky?: string[];
  alt?: string[];
}

export interface GateConfig {
  /** Page families (all-pages engine + kit-compliance sampling). */
  families: PageFamily[];
  routes?: {
    /** Every page route pattern the site's route table declares. */
    declared: () => string[] | Promise<string[]>;
    /** Route patterns that are not page families, with the reason. */
    nonPage?: Record<string, string>;
  };
  sitemaps?: {
    /** Default '/sitemap.xml'. */
    index?: string;
    /**
     * The sub-sitemaps the index lists, when known: each gets its own test
     * (and `shards`), and the index may list no other. Without it, one test
     * checks every file the index lists.
     */
    files?: string[];
    /** Sub-sitemaps that answer 503 on the fixture by design (reason). */
    emptyOnFixture?: Record<string, string>;
    /** Sub-sitemaps that are a 200 empty urlset on the fixture (reason). */
    emptyUrlsetOnFixture?: Record<string, string>;
    /** Seeded sample size per large sub-sitemap (default: all URLs). */
    sample?: Record<string, number>;
    /** Shards per sub-sitemap test. */
    shards?: Record<string, number>;
  };
  /** Internal link targets that exist in production but not in the fixture (reason). */
  prodOnlyLinkTargets?: Record<string, string>;
  /** Legacy URL shapes that must redirect. */
  redirects?: (ctx: SourceContext) => Promise<RedirectCase[]>;
  /** Pairs of a hub and a query-driven render the HTML cache must never confuse. */
  cacheKeyCases?: (ctx: SourceContext) => Promise<Array<{ hub: string; variant: string }>>;
  /** A wider no-JS phone sweep of long-tail families (overflow, leaked source, one h1). */
  phoneSweep?: SweepSpec[];
  /** The page whose nav and footer links are resolved in full (default '/'), and the fewest links expected. */
  chrome?: { page?: string; minLinks?: number };
  compliance?: ComplianceGateConfig;
  aiFetchers?: { paths?: Source; fetchers?: AiFetcher[] };
  unbuyable?: UnbuyableGateConfig;
  placements?: PlacementGateConfig;
}
