/**
 * The fixture's reads. Every query is bounded (the pool sets
 * statement_timeout) and returns plain typed rows; renderers never see SQL.
 */
import type { Pool } from 'pg';
import type { FaqEntry } from '../../server/src/render';

export interface ProductRow {
  asin: string;
  modelName: string;
  brand: string;
  category: string;
  lifecycle: 'live' | 'unavailable' | 'retired';
  isActive: boolean;
  qualityFlags: unknown;
  summary: string;
  capacityWh: number | null;
  weightKg: string | null;
  alternativeAsin: string | null;
  rank: number;
  /** Amazon data (fixture_item_cache); null when never fetched. */
  title: string | null;
  imageUrl: string | null;
  detailPageUrl: string | null;
  price: string | null;
  listPrice: string | null;
  currency: string | null;
  availabilityType: string | null;
  fetchedAt: Date | null;
  /** The active recall, if any. */
  recallId: string | null;
}

export interface RecallRow {
  id: string;
  asin: string;
  modelName: string;
  title: string;
  hazard: string;
  remedy: string;
  status: 'stop-use' | 'repair-hold' | 'closed';
  announcedOn: string;
  sourceUrl: string;
}

export interface ArticleRow {
  slug: string;
  title: string;
  subtitle: string | null;
  excerpt: string | null;
  bodyMd: string;
  heroImageUrl: string | null;
  category: string;
  relatedProductAsins: string[];
  faqs: FaqEntry[] | null;
  publishedAt: Date | null;
  updatedAt: Date;
}

export interface BestListRow {
  slug: string;
  title: string;
  introMd: string;
  asins: string[];
  updatedAt: Date;
}

export interface NoteRow {
  slug: string;
  title: string;
  description: string;
  bodyHtml: string;
  headHtml: string | null;
  jsonld: Record<string, unknown> | null;
  amazonContent: boolean;
  updatedAt: Date;
}

const PRODUCT_SELECT = `
SELECT p.asin, p.model_name, p.brand, p.category, p.lifecycle, p.is_active, p.quality_flags, p.summary,
       p.capacity_wh, p.weight_kg::text AS weight_kg, p.alternative_asin, p.rank,
       c.title, c.image_url, c.detail_page_url, c.price::text AS price, c.list_price::text AS list_price,
       c.currency, c.availability_type, c.fetched_at,
       (SELECT r.id FROM fixture_recalls r WHERE r.asin = p.asin AND r.status <> 'closed'
         ORDER BY r.announced_on DESC LIMIT 1) AS recall_id
  FROM fixture_products p
  LEFT JOIN fixture_item_cache c ON c.asin = p.asin`;

function productFrom(r: Record<string, unknown>): ProductRow {
  return {
    asin: String(r.asin),
    modelName: String(r.model_name),
    brand: String(r.brand),
    category: String(r.category),
    lifecycle: r.lifecycle as ProductRow['lifecycle'],
    isActive: r.is_active === true,
    qualityFlags: r.quality_flags,
    summary: String(r.summary),
    capacityWh: r.capacity_wh == null ? null : Number(r.capacity_wh),
    weightKg: r.weight_kg == null ? null : String(r.weight_kg),
    alternativeAsin: r.alternative_asin == null ? null : String(r.alternative_asin),
    rank: Number(r.rank),
    title: r.title == null ? null : String(r.title),
    imageUrl: r.image_url == null ? null : String(r.image_url),
    detailPageUrl: r.detail_page_url == null ? null : String(r.detail_page_url),
    price: r.price == null ? null : String(r.price),
    listPrice: r.list_price == null ? null : String(r.list_price),
    currency: r.currency == null ? null : String(r.currency),
    availabilityType: r.availability_type == null ? null : String(r.availability_type),
    fetchedAt: r.fetched_at instanceof Date ? r.fetched_at : r.fetched_at == null ? null : new Date(String(r.fetched_at)),
    recallId: r.recall_id == null ? null : String(r.recall_id),
  };
}

function textArray(v: unknown): string[] {
  return Array.isArray(v) ? v.map((x) => String(x)) : [];
}

function faqsFrom(v: unknown): FaqEntry[] | null {
  if (!Array.isArray(v)) return null;
  return v
    .filter((x): x is { question: unknown; answer: unknown } => !!x && typeof x === 'object')
    .map((x) => ({ question: String(x.question ?? ''), answer: String(x.answer ?? '') }));
}

export class FixtureDb {
  constructor(private readonly pool: Pool) {}

  async products(): Promise<ProductRow[]> {
    const r = await this.pool.query(`${PRODUCT_SELECT} WHERE p.is_active ORDER BY p.rank, p.asin`);
    return r.rows.map(productFrom);
  }

  async product(asin: string): Promise<ProductRow | null> {
    const r = await this.pool.query(`${PRODUCT_SELECT} WHERE p.asin = $1`, [asin]);
    return r.rows[0] ? productFrom(r.rows[0]) : null;
  }

  async productsByAsin(asins: readonly string[]): Promise<Map<string, ProductRow>> {
    if (!asins.length) return new Map();
    const r = await this.pool.query(`${PRODUCT_SELECT} WHERE p.asin = ANY($1::text[])`, [asins]);
    return new Map(r.rows.map((row: Record<string, unknown>) => [String(row.asin), productFrom(row)]));
  }

  async recalls(): Promise<RecallRow[]> {
    const r = await this.pool.query(`
      SELECT r.id, r.asin, p.model_name, r.title, r.hazard, r.remedy, r.status,
             to_char(r.announced_on, 'YYYY-MM-DD') AS announced_on, r.source_url
        FROM fixture_recalls r JOIN fixture_products p ON p.asin = r.asin
       ORDER BY r.announced_on DESC, r.id`);
    return r.rows.map((row: Record<string, unknown>) => ({
      id: String(row.id),
      asin: String(row.asin),
      modelName: String(row.model_name),
      title: String(row.title),
      hazard: String(row.hazard),
      remedy: String(row.remedy),
      status: row.status as RecallRow['status'],
      announcedOn: String(row.announced_on),
      sourceUrl: String(row.source_url),
    }));
  }

  async articles(): Promise<ArticleRow[]> {
    const r = await this.pool.query(`
      SELECT slug, title, subtitle, excerpt, body_md, hero_image_url, category, related_product_asins, faqs,
             published_at, updated_at
        FROM editorial_articles WHERE status = 'published' ORDER BY published_at DESC NULLS LAST, slug`);
    return r.rows.map((row: Record<string, unknown>) => this.articleFrom(row));
  }

  async article(slug: string): Promise<ArticleRow | null> {
    const r = await this.pool.query(`
      SELECT slug, title, subtitle, excerpt, body_md, hero_image_url, category, related_product_asins, faqs,
             published_at, updated_at
        FROM editorial_articles WHERE status = 'published' AND slug = $1`, [slug]);
    return r.rows[0] ? this.articleFrom(r.rows[0]) : null;
  }

  private articleFrom(row: Record<string, unknown>): ArticleRow {
    return {
      slug: String(row.slug),
      title: String(row.title),
      subtitle: row.subtitle == null ? null : String(row.subtitle),
      excerpt: row.excerpt == null ? null : String(row.excerpt),
      bodyMd: String(row.body_md),
      heroImageUrl: row.hero_image_url == null ? null : String(row.hero_image_url),
      category: String(row.category),
      relatedProductAsins: textArray(row.related_product_asins),
      faqs: faqsFrom(row.faqs),
      publishedAt: row.published_at instanceof Date ? row.published_at : null,
      updatedAt: row.updated_at instanceof Date ? row.updated_at : new Date(String(row.updated_at)),
    };
  }

  async bestLists(): Promise<BestListRow[]> {
    const r = await this.pool.query('SELECT slug, title, intro_md, asins, updated_at FROM fixture_best_lists ORDER BY slug');
    return r.rows.map((row: Record<string, unknown>) => ({
      slug: String(row.slug),
      title: String(row.title),
      introMd: String(row.intro_md),
      asins: textArray(row.asins),
      updatedAt: row.updated_at as Date,
    }));
  }

  async bestList(slug: string): Promise<BestListRow | null> {
    return (await this.bestLists()).find((b) => b.slug === slug) ?? null;
  }

  async notes(): Promise<NoteRow[]> {
    const r = await this.pool.query(
      'SELECT slug, title, description, body_html, head_html, jsonld, amazon_content, updated_at FROM fixture_notes ORDER BY slug');
    return r.rows.map((row: Record<string, unknown>) => ({
      slug: String(row.slug),
      title: String(row.title),
      description: String(row.description),
      bodyHtml: String(row.body_html),
      headHtml: row.head_html == null ? null : String(row.head_html),
      jsonld: row.jsonld && typeof row.jsonld === 'object' ? (row.jsonld as Record<string, unknown>) : null,
      amazonContent: row.amazon_content === true,
      updatedAt: row.updated_at as Date,
    }));
  }

  async note(slug: string): Promise<NoteRow | null> {
    return (await this.notes()).find((n) => n.slug === slug) ?? null;
  }
}

/** The /r/ redirect lookup SQL ($1 = ASIN), over the view 0001 defines. */
export const REDIRECT_LOOKUP_SQL =
  'SELECT asin, is_active, quality_flags, buyable, detail_page_url, title, brand FROM fixture_redirect_lookup WHERE asin = $1';
