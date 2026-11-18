/**
 * Published editorial articles (editorial_articles, the kit's table; columns
 * in vendor/affiliate-kit/migrations/kit_0003_editorial_articles.sql).
 *
 * Every read is bounded (the kit's boundedQuery sets a per-statement budget),
 * so a slow shared database degrades one page instead of holding the pool.
 * Only status = 'published' rows are ever read: drafts are invisible here, so
 * a site with the publish gate never renders an unchecked article.
 */
import { boundedQuery, type PoolLike } from '../../vendor/affiliate-kit/server/src';
import type { FaqEntry } from '../../vendor/affiliate-kit/server/src/render';

export const PAGE_QUERY_BUDGET_MS = 3000;
export const SITEMAP_QUERY_BUDGET_MS = 10_000;

export interface ArticleSummary {
  readonly slug: string;
  readonly title: string;
  readonly excerpt: string | null;
  readonly publishedAt: Date | null;
  readonly updatedAt: Date;
}

export interface Article extends ArticleSummary {
  readonly subtitle: string | null;
  readonly bodyMd: string;
  readonly heroImageUrl: string | null;
  readonly category: string;
  readonly faqs: FaqEntry[] | null;
}

/** What the pages read; tests pass an in-memory one. */
export interface ArticleStore {
  /** Newest first. */
  latest(limit: number): Promise<ArticleSummary[]>;
  bySlug(slug: string): Promise<Article | null>;
  /** Every published slug, for the sitemap. */
  sitemapEntries(): Promise<Array<Pick<ArticleSummary, 'slug' | 'updatedAt'>>>;
}

function date(v: unknown): Date | null {
  if (v == null) return null;
  const d = v instanceof Date ? v : new Date(String(v));
  return Number.isNaN(d.getTime()) ? null : d;
}

function text(v: unknown): string | null {
  return v == null ? null : String(v);
}

function faqsFrom(v: unknown): FaqEntry[] | null {
  if (!Array.isArray(v)) return null;
  return v
    .filter((x): x is { question?: unknown; answer?: unknown } => !!x && typeof x === 'object')
    .map((x) => ({ question: String(x.question ?? ''), answer: String(x.answer ?? '') }))
    .filter((x) => x.question.trim() && x.answer.trim());
}

function summaryFrom(r: Record<string, unknown>): ArticleSummary {
  return {
    slug: String(r.slug),
    title: String(r.title),
    excerpt: text(r.excerpt),
    publishedAt: date(r.published_at),
    updatedAt: date(r.updated_at) ?? new Date(0),
  };
}

export class PgArticleStore implements ArticleStore {
  constructor(private readonly pool: PoolLike) {}

  async latest(limit: number): Promise<ArticleSummary[]> {
    const n = Math.max(1, Math.min(500, Math.floor(limit)));
    const r = await boundedQuery(this.pool, `
      SELECT slug, title, excerpt, published_at, updated_at
        FROM editorial_articles
       WHERE status = 'published'
       ORDER BY published_at DESC NULLS LAST, slug
       LIMIT $1`, [n], PAGE_QUERY_BUDGET_MS);
    return r.rows.map(summaryFrom);
  }

  async bySlug(slug: string): Promise<Article | null> {
    const r = await boundedQuery(this.pool, `
      SELECT slug, title, subtitle, excerpt, body_md, hero_image_url, category, faqs, published_at, updated_at
        FROM editorial_articles
       WHERE status = 'published' AND slug = $1`, [slug], PAGE_QUERY_BUDGET_MS);
    const row = r.rows[0] as Record<string, unknown> | undefined;
    if (!row) return null;
    return {
      ...summaryFrom(row),
      subtitle: text(row.subtitle),
      bodyMd: String(row.body_md ?? ''),
      heroImageUrl: text(row.hero_image_url),
      category: String(row.category ?? ''),
      faqs: faqsFrom(row.faqs),
    };
  }

  async sitemapEntries(): Promise<Array<Pick<ArticleSummary, 'slug' | 'updatedAt'>>> {
    const r = await boundedQuery(this.pool, `
      SELECT slug, updated_at FROM editorial_articles WHERE status = 'published' ORDER BY slug`,
    [], SITEMAP_QUERY_BUDGET_MS);
    return r.rows.map((row: Record<string, unknown>) => ({ slug: String(row.slug), updatedAt: date(row.updated_at) ?? new Date(0) }));
  }
}
