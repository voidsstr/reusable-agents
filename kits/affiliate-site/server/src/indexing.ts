/**
 * site.json `indexing`: whether search engines may index the site yet.
 *
 * A site that serves real pages before it should be found (staging on a
 * temporary platform address, a new domain waiting for its launch day) sets
 * `"indexing": "closed"`. Then:
 *   - every response carries X-Robots-Tag: noindex (http/indexing.ts;
 *     mountKit installs it ahead of every other handler), whatever a page's
 *     own robots meta says, so nothing is indexed by accident;
 *   - the IndexNow submit path refuses before it builds or sends anything
 *     (http/indexNow.ts indexNowSubmission and submitIndexNow, and
 *     render/sitemap.ts indexableUrls, "the URLs to submit or ping"):
 *     a closed site pings no search engine;
 *   - robots.txt does NOT change. A Disallow would stop crawlers from fetching
 *     the pages at all, so they would never read the noindex, and a URL known
 *     from a link can still be indexed bare ("indexed, though blocked by
 *     robots.txt").
 *
 * Pages keep rendering their own robots meta (render/seo.ts), and the
 * sitemaps keep listing the URLs those decisions allow: the deploy gate goes
 * on checking every family's indexing decision for the day the operator turns
 * the switch to "open". The switch is site.json only, so opening a site is a
 * reviewed commit and a deploy, never a stray environment variable.
 */
import type { SiteContext } from './siteContext';

/** Thrown by every IndexNow / ping entry point while site.json `indexing` is "closed". */
export class IndexingClosedError extends Error {
  /** What was refused, e.g. 'IndexNow submission'. */
  readonly refused: string;
  readonly siteId: string;

  constructor(refused: string, siteId: string) {
    super(`${refused} refused: site.json "indexing" is "closed" for ${siteId}, so nothing is submitted to search engines until the operator opens it`);
    this.name = 'IndexingClosedError';
    this.refused = refused;
    this.siteId = siteId;
  }
}

/** True unless site.json says "indexing": "open" (an absent or unknown value counts as closed). */
export function indexingClosed(ctx: Pick<SiteContext, 'indexing'>): boolean {
  return ctx.indexing !== 'open';
}

/** Throws IndexingClosedError naming `refused` unless the site is open to indexing. */
export function assertIndexingOpen(ctx: Pick<SiteContext, 'indexing' | 'site'>, refused: string): void {
  if (indexingClosed(ctx)) throw new IndexingClosedError(refused, ctx.site.id);
}
