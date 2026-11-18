/**
 * The fixture site's route table: every path pattern the app serves, and the
 * gate family that audits it. Plain data with no imports, so the gate config
 * (tests/ui/gate.config.ts) reads the same table the app mounts, and a route
 * added here without a family fails the gate's route-coverage test.
 */

export type RouteKind = 'page' | 'kit' | 'asset' | 'sitemap';

export interface RouteEntry {
  /** express path pattern. */
  readonly pattern: string;
  readonly kind: RouteKind;
  /** The gate family (tests/ui/gate.config.ts) for a page route. */
  readonly family?: string;
  /** Why a non-page route is not a page family. */
  readonly reason?: string;
}

export const ROUTES: readonly RouteEntry[] = Object.freeze([
  { pattern: '/', kind: 'page', family: 'home' },
  { pattern: '/product/:asin', kind: 'page', family: 'product' },
  { pattern: '/guides', kind: 'page', family: 'guides-index' },
  { pattern: '/guides/:slug', kind: 'page', family: 'guide' },
  { pattern: '/best/:slug', kind: 'page', family: 'best-list' },
  { pattern: '/recalls', kind: 'page', family: 'recalls-index' },
  { pattern: '/recalls/:id', kind: 'page', family: 'recall' },
  { pattern: '/notes/:slug', kind: 'page', family: 'note' },
  { pattern: '/about', kind: 'page', family: 'static' },
  { pattern: '/disclosure', kind: 'page', family: 'static' },
  { pattern: '/sitemap.xml', kind: 'sitemap', reason: 'sitemap index (audited by the sitemap-integrity tests)' },
  { pattern: '/:file.xml', kind: 'sitemap', reason: 'child sitemaps (audited by the sitemap-integrity tests)' },
  { pattern: '/assets/:file', kind: 'asset', reason: 'static images' },
  { pattern: '/health', kind: 'kit', reason: 'kit health check' },
  { pattern: '/robots.txt', kind: 'kit', reason: 'kit robots.txt (kit-compliance checks it)' },
  { pattern: '/llms.txt', kind: 'kit', reason: 'kit llms.txt (kit-compliance checks it)' },
  { pattern: '/:indexNowKey.txt', kind: 'kit', reason: 'kit IndexNow key file' },
  { pattern: '/api/outbound-click', kind: 'kit', reason: 'kit click beacon (POST)' },
  { pattern: '/r/:asin', kind: 'kit', reason: 'kit click-logging redirect to Amazon' },
  { pattern: '/kit/:bundle', kind: 'kit', reason: 'kit browser bundle' },
]);

/** Page route patterns (what the gate's route-coverage test reads). */
export function pageRoutes(): string[] {
  return ROUTES.filter((r) => r.kind === 'page').map((r) => r.pattern);
}

/** Non-page route patterns and why each is not a page family. */
export function nonPageRoutes(): Record<string, string> {
  const out: Record<string, string> = {};
  for (const r of ROUTES) if (r.kind !== 'page') out[r.pattern] = r.reason ?? r.kind;
  return out;
}
