/**
 * The route table: every path pattern the server answers, what renders it,
 * which sitemap lists its pages, and the deploy-gate family that audits it.
 *
 * One table, read three ways:
 *   - src/index.ts mounts every page and sitemap route from it (a page route
 *     whose handler does not exist does not compile);
 *   - src/render/sitemaps.ts lists the static page routes marked for a sitemap;
 *   - tests/ui/gate.config.ts hands pageRoutes()/nonPageRoutes() to the
 *     gate, so the gate fails on a page route with no family.
 * Plain data with no imports, so the gate config can load it outside the app.
 *
 * Adding a page: add its route here (with its gate family), its handler in
 * src/index.ts, its family in tests/ui/pages.manifest.ts, and, if it should
 * be indexed, its sitemap kind (and the sitemap in the manifest's
 * sitemaps.files). Kit routes (mountKit) are listed for the gate;
 * they are mounted by the kit, not from this table.
 */

/** Page renderers src/index.ts provides. */
export type PageHandlerId = 'home' | 'guides-index' | 'guide' | 'about' | 'disclosure' | 'privacy';
/** Sitemap responses src/index.ts provides. */
export type SitemapHandlerId = 'sitemap-index' | 'sitemap-file';
/** The sitemap kinds (src/render/sitemaps.ts): sitemap-<kind>.xml. */
export type SitemapKind = 'pages' | 'guides';

export interface PageRoute {
  readonly kind: 'page';
  /** express path pattern. */
  readonly pattern: string;
  readonly handler: PageHandlerId;
  /** The gate family (tests/ui/pages.manifest.ts) that audits these pages. */
  readonly family: string;
  /** The sitemap that lists this route's pages; omit for pages that are never submitted. */
  readonly sitemap?: SitemapKind;
}

export interface SitemapRoute {
  readonly kind: 'sitemap';
  readonly pattern: string;
  readonly handler: SitemapHandlerId;
  readonly reason: string;
}

/** A route the kit mounts (mountKit); listed so the gate knows it is not a page family. */
export interface KitRoute {
  readonly kind: 'kit';
  readonly pattern: string;
  readonly reason: string;
}

export type RouteEntry = PageRoute | SitemapRoute | KitRoute;

/** Editorial articles (editorial_articles, the kit's table) are served here. */
export const GUIDES_PATH = '/guides';

export function guidePath(slug: string): string {
  return `${GUIDES_PATH}/${encodeURIComponent(slug)}`;
}

export const ROUTES: readonly RouteEntry[] = Object.freeze([
  { kind: 'page', pattern: '/', handler: 'home', family: 'home', sitemap: 'pages' },
  { kind: 'page', pattern: GUIDES_PATH, handler: 'guides-index', family: 'guides-index', sitemap: 'pages' },
  { kind: 'page', pattern: `${GUIDES_PATH}/:slug`, handler: 'guide', family: 'guide', sitemap: 'guides' },
  { kind: 'page', pattern: '/about', handler: 'about', family: 'static', sitemap: 'pages' },
  { kind: 'page', pattern: '/disclosure', handler: 'disclosure', family: 'static', sitemap: 'pages' },
  { kind: 'page', pattern: '/privacy', handler: 'privacy', family: 'static', sitemap: 'pages' },
  { kind: 'sitemap', pattern: '/sitemap.xml', handler: 'sitemap-index', reason: 'sitemap index (audited by the sitemap-integrity tests)' },
  { kind: 'sitemap', pattern: '/sitemap-:name.xml', handler: 'sitemap-file', reason: 'child sitemaps (audited by the sitemap-integrity tests)' },
  { kind: 'kit', pattern: '/health', reason: 'kit health check' },
  { kind: 'kit', pattern: '/robots.txt', reason: 'kit robots.txt (kit-compliance checks it)' },
  { kind: 'kit', pattern: '/llms.txt', reason: 'kit llms.txt (kit-compliance checks it)' },
  { kind: 'kit', pattern: '/:indexNowKey.txt', reason: 'kit IndexNow key file' },
  { kind: 'kit', pattern: '/api/outbound-click', reason: 'kit click beacon (POST)' },
  { kind: 'kit', pattern: '/kit/:bundle', reason: 'kit browser bundle' },
  // GET /r/:asin (the logged redirect to Amazon) is mounted by the kit only when
  // src/index.ts passes a redirectLookup over the site's catalog; add it here then.
] satisfies RouteEntry[]);

export function pageRouteEntries(): PageRoute[] {
  return ROUTES.filter((r): r is PageRoute => r.kind === 'page');
}

/** Page route patterns (what the gate's route-coverage test reads). */
export function pageRoutes(): string[] {
  return pageRouteEntries().map((r) => r.pattern);
}

/** Non-page route patterns and why each is not a page family. */
export function nonPageRoutes(): Record<string, string> {
  const out: Record<string, string> = {};
  for (const r of ROUTES) if (r.kind !== 'page') out[r.pattern] = r.reason;
  return out;
}

/** Static (parameter-free) page paths a sitemap kind lists. */
export function staticPagesIn(kind: SitemapKind): string[] {
  return pageRouteEntries().filter((r) => r.sitemap === kind && !r.pattern.includes(':')).map((r) => r.pattern);
}
