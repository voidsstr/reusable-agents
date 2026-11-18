/**
 * The sitemap set: one sitemap per kind, fed only indexable URLs (the kit's
 * buildSitemapSet refuses anything else), so every submitted URL is a page
 * the gate can prove is 200, indexable and self-canonical.
 *
 *   sitemap-pages.xml   the static page routes src/routes/table.ts marks 'pages'
 *   sitemap-guides.xml  every published article (may be empty on a new site)
 *
 * Product and hub sitemaps join here as those routes land; give each kind a
 * gate entry (tests/ui/pages.manifest.ts sitemaps.files).
 */
import type { SiteContext } from '../../vendor/affiliate-kit/server/src';
import { INDEXABLE_PAGE, buildSitemapSet, type SitemapSet } from '../../vendor/affiliate-kit/server/src/render';
import type { ArticleSummary } from '../db/articles';
import { guidePath, staticPagesIn } from '../routes/table';

export interface SitemapData {
  readonly articles: ReadonlyArray<Pick<ArticleSummary, 'slug' | 'updatedAt'>>;
}

export function siteSitemapSet(ctx: SiteContext, data: SitemapData): SitemapSet {
  return buildSitemapSet(ctx, [
    { kind: 'pages', candidates: staticPagesIn('pages').map((loc) => ({ loc, indexing: INDEXABLE_PAGE })) },
    {
      kind: 'guides',
      allowEmpty: true,
      candidates: data.articles.map((a) => ({ loc: guidePath(a.slug), indexing: INDEXABLE_PAGE, lastmod: a.updatedAt.toISOString() })),
    },
  ]);
}
