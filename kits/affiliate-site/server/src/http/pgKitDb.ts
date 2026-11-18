/**
 * KitDb on Postgres (node-postgres Pool, or anything shaped like it).
 *
 * The SQL targets the kit's own tables, whose columns are identical to
 * SpecPicks' (kit migrations kit_0001 outbound_clicks, kit_0002
 * ai_traffic_log, kit_0003 editorial_articles), so the framework's readers
 * (human_clicks, placement_watch, ai_traffic) work on a kit site unchanged.
 *
 *   inserts  plain pool.query, as upstream: they run after the response, in
 *            the BackgroundWriter, which caps how many are in flight.
 *   reads    boundedQuery (SET LOCAL statement_timeout in its own
 *            transaction) at 3 s, as upstream's llms.txt sections, so the
 *            shared database cancels a slow scan instead of finishing it.
 *
 * The /r/ catalog lookup is the site's own SQL (its catalog schema is not the
 * kit's): pgRedirectLookup(pool, sql) runs it with $1 = the ASIN.
 */
import { boundedQuery, type PoolLike, type QueryResultLike } from '../dbBudget';
import type {
  AiCitedPage, AiCitedPagesQuery, AiTrafficRow, KitDb, LatestArticle, LatestArticlesQuery,
  OutboundClickRow, RedirectLookup, RedirectProduct,
} from './types';

/** What createPgKitDb needs: pg.Pool satisfies it. */
export interface PgPoolLike extends PoolLike {
  query(sql: string, params?: any[]): Promise<QueryResultLike>;
}

export const OUTBOUND_CLICK_INSERT_SQL = `INSERT INTO outbound_clicks
       (site_id, asin, tracking_id, source_page, source_kind, target,
        ip_address, user_agent, is_bot, bot_reason, country, device_type,
        via, accept_language, session_pageviews, ms_on_page, placement, classified_at)
     VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, NOW())`;

export const AI_TRAFFIC_INSERT_SQL = `INSERT INTO ai_traffic_log (kind, source, path, status_code, user_agent, referer, ip_address, site_id,
                                     duration_ms, ip_verified, ip_check, forwarded_for)
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)`;

/**
 * Upstream llmsCitedPages, parameterised: $1 sources, $2 days, $3 site id
 * (NULL = all), $4 path prefixes, $5 minimum referrals, $6 article path
 * prefix (NULL = no article join), $7 limit.
 */
export const AI_CITED_PAGES_SQL = `WITH ref AS (
         SELECT split_part(path, '?', 1) AS path, COUNT(*) AS n
           FROM ai_traffic_log
          WHERE kind = 'referral' AND status_code = 200
            AND source = ANY($1::text[]) AND ts > NOW() - make_interval(days => $2::int)
            AND ($3::text IS NULL OR site_id = $3::text)
            AND EXISTS (SELECT 1 FROM unnest($4::text[]) AS pfx(p) WHERE starts_with(split_part(path, '?', 1), pfx.p))
          GROUP BY 1 HAVING COUNT(*) >= $5::int)
       SELECT ref.path, ref.n, a.title
         FROM ref
         LEFT JOIN editorial_articles a
           ON $6::text IS NOT NULL AND starts_with(ref.path, $6::text)
          AND a.slug = substring(ref.path FROM char_length($6::text) + 1)
        WHERE $6::text IS NULL OR NOT starts_with(ref.path, $6::text) OR a.status = 'published'
        ORDER BY ref.n DESC, ref.path
        LIMIT $7::int`;

/** Upstream llmsLatestArticles: $1 days, $2 limit. */
export const LATEST_ARTICLES_SQL = `SELECT slug, title, to_char(published_at AT TIME ZONE 'UTC', 'YYYY-MM-DD') AS day
         FROM editorial_articles
        WHERE status = 'published' AND slug IS NOT NULL AND title IS NOT NULL
          AND published_at > NOW() - make_interval(days => $1::int) AND published_at <= NOW()
        ORDER BY published_at DESC
        LIMIT $2::int`;

/** Read budget for the llms.txt sections (upstream: 3000 ms). */
export const PG_KIT_READ_TIMEOUT_MS = 3000;
/** Budget for the /r/ catalog lookup. */
export const PG_REDIRECT_LOOKUP_TIMEOUT_MS = 2000;

export function outboundClickParams(r: OutboundClickRow): unknown[] {
  return [
    r.siteId, r.asin, r.trackingId, r.sourcePage, r.sourceKind, r.target,
    r.ipAddress, r.userAgent, r.isBot, r.botReason, r.country, r.deviceType,
    r.via, r.acceptLanguage, r.sessionPageviews, r.msOnPage, r.placement,
  ];
}

export function aiTrafficParams(r: AiTrafficRow): unknown[] {
  return [
    r.kind, r.source, r.path, r.statusCode, r.userAgent, r.referer, r.ipAddress, r.siteId,
    r.durationMs, r.ipVerified, r.ipCheck, r.forwardedFor,
  ];
}

export interface PgKitDbOptions {
  /** Read budget for the llms.txt queries (default 3000 ms). */
  readTimeoutMs?: number;
}

export function createPgKitDb(pool: PgPoolLike, opts: PgKitDbOptions = {}): KitDb {
  const readMs = opts.readTimeoutMs ?? PG_KIT_READ_TIMEOUT_MS;
  return {
    async insertOutboundClick(row: OutboundClickRow): Promise<void> {
      await pool.query(OUTBOUND_CLICK_INSERT_SQL, outboundClickParams(row));
    },
    async insertAiTraffic(row: AiTrafficRow): Promise<void> {
      await pool.query(AI_TRAFFIC_INSERT_SQL, aiTrafficParams(row));
    },
    async aiCitedPages(q: AiCitedPagesQuery): Promise<AiCitedPage[]> {
      const r = await boundedQuery(pool, AI_CITED_PAGES_SQL, [
        [...q.sources], q.days, q.siteId, [...q.pathPrefixes], q.minReferrals, q.articlePathPrefix, q.limit,
      ], readMs);
      return r.rows.map((row: { path: unknown; n: unknown; title: unknown }) => ({
        path: String(row.path),
        referrals: Number(row.n) || 0,
        title: row.title == null ? null : String(row.title),
      }));
    },
    async latestArticles(q: LatestArticlesQuery): Promise<LatestArticle[]> {
      const r = await boundedQuery(pool, LATEST_ARTICLES_SQL, [q.days, q.limit], readMs);
      return r.rows.map((row: { slug: unknown; title: unknown; day: unknown }) => ({
        slug: String(row.slug),
        title: String(row.title),
        day: String(row.day),
      }));
    },
  };
}

function bool(v: unknown, dflt: boolean): boolean {
  if (typeof v === 'boolean') return v;
  if (v == null) return dflt;
  if (v === 't' || v === 'true' || v === 1) return true;
  if (v === 'f' || v === 'false' || v === 0) return false;
  return dflt;
}

/** Only an explicit true (boolean, or Postgres' 't' / 'true' text, or 1) is true; NULL and anything else are false. */
function strictTrue(v: unknown): boolean {
  return v === true || v === 't' || v === 'true' || v === 1;
}

function textOrNull(v: unknown): string | null {
  return v == null || v === '' ? null : String(v);
}

/** The lookup column that carries the recall gate. */
export const REDIRECT_BUYABLE_COLUMN = 'buyable';

/**
 * A RedirectProduct from one row of the site's lookup SQL. Recognised
 * columns: asin, is_active (default true), quality_flags, buyable
 * (REQUIRED), detail_page_url, title, brand.
 *
 * `buyable` is the recall gate, so it fails closed: only true counts (NULL,
 * false or any other value sends /r/ to our own page), and a row without the
 * column is a configuration error (thrown; the /r/ handler then logs it and
 * sends the reader to our product page) instead of a silent "buyable".
 */
export function redirectProductFromRow(row: Record<string, unknown>, asin: string): RedirectProduct {
  if (!row || !Object.prototype.hasOwnProperty.call(row, REDIRECT_BUYABLE_COLUMN)) {
    throw new TypeError(
      `redirect lookup: the SQL returned no '${REDIRECT_BUYABLE_COLUMN}' column; it must return one `
      + '(false or NULL while the recall gate blocks buying it or a hold applies), see pgRedirectLookup',
    );
  }
  return {
    asin,
    isActive: bool(row.is_active, true),
    qualityFlags: row.quality_flags ?? null,
    buyable: strictTrue(row[REDIRECT_BUYABLE_COLUMN]),
    detailPageUrl: textOrNull(row.detail_page_url),
    title: textOrNull(row.title),
    brand: textOrNull(row.brand),
  };
}

/**
 * The /r/ lookup from the site's SQL ($1 = the ASIN; first row wins, so
 * order it newest active first). The SQL must return a `buyable` column
 * (see redirectProductFromRow; NULL counts as not buyable). Example for a
 * cache-model catalog:
 *
 *   SELECT p.asin, p.is_active, p.quality_flags,
 *          COALESCE(p.lifecycle = 'live', false) AND NOT EXISTS (SELECT 1 FROM v_recall_buy_blocks b
 *             WHERE b.product_id = p.id OR b.model_id = p.model_id) AS buyable,
 *          c.detail_page_url, COALESCE(mo.model_name, c.title) AS title, b.name AS brand
 *     FROM products p LEFT JOIN v_offer_display c ON c.asin = p.asin ...
 *    WHERE p.asin = $1 ORDER BY p.is_active DESC LIMIT 1
 *
 * The recall check reads the recall gate's views (framework/core/recall_gate.py
 * `schema`), never recall_matches directly: v_recall_buy_blocks is the same
 * rule the product page's buy button follows, and a match row may carry only
 * a model_id or only a product_id, so join on both. To also hold back
 * matches still awaiting review, use v_recall_pick_exclusions instead.
 */
export function pgRedirectLookup(pool: PoolLike, sql: string, opts: { timeoutMs?: number } = {}): RedirectLookup {
  if (!/\$1\b/.test(sql)) throw new TypeError('pgRedirectLookup: the SQL must take the ASIN as $1');
  const ms = opts.timeoutMs ?? PG_REDIRECT_LOOKUP_TIMEOUT_MS;
  return async (asin: string) => {
    const r = await boundedQuery(pool, sql, [asin], ms);
    const row = r.rows[0] as Record<string, unknown> | undefined;
    return row ? redirectProductFromRow(row, asin) : null;
  };
}
