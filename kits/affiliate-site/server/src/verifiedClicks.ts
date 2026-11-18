/**
 * 30-day verified-human Amazon clicks per ASIN.
 *
 * Ported from SpecPicks src/services/verifiedClicks.ts (see PORTED_FROM.json).
 * Beacon rows in outbound_clicks (via='beacon', target='amazon', not is_bot)
 * are the "verified human" series: they only exist when a real, JS-executing
 * tab clicked a CTA (the kit's /api/outbound-click handler writes them). The
 * framework's human_clicks reads the same series with stricter verdicts; the
 * two agree to within ~1% on SpecPicks.
 *
 * One grouped query over a partial index (via, clicked_at) WHERE
 * via='beacon', cached 15 minutes, stale-while-revalidate. A failed or slow
 * load returns an empty map: callers rank as if nobody had clicked.
 *
 * Kit change: an optional siteId filter (outbound_clicks.site_id), for a
 * database shared by more than one site id. Without it the query is
 * SpecPicks' exactly.
 */
import { boundedQuery, SwrCache, type PoolLike } from './dbBudget';

const CACHE = new SwrCache<Map<string, number>>({ ttlMs: 15 * 60 * 1000, maxEntries: 4, failTtlMs: 2 * 60 * 1000 });
export const VERIFIED_CLICKS_TIMEOUT_MS = 1500;

const BASE_SQL = `SELECT asin, COUNT(*)::int AS n
           FROM outbound_clicks
          WHERE via = 'beacon' AND target = 'amazon' AND NOT is_bot
            AND clicked_at > NOW() - INTERVAL '30 days'
            AND asin ~ '^B[A-Z0-9]{9}$'`;

/** The query for all rows, or for one site id ($1). */
export function verifiedClicksSql(siteFilter: boolean): string {
  return `${BASE_SQL}${siteFilter ? `
            AND site_id = $1` : ''}
          GROUP BY asin`;
}

export interface VerifiedClicksOptions {
  /** Only count rows written by this site id (ctx.site.id). */
  siteId?: string;
  timeoutMs?: number;
}

export async function verifiedClicks30dByAsin(pool: PoolLike, opts: VerifiedClicksOptions = {}): Promise<Map<string, number>> {
  const siteId = opts.siteId || '';
  try {
    return await CACHE.get(`amazon-30d:${siteId || '*'}`, async () => {
      const r = await boundedQuery(
        pool,
        verifiedClicksSql(!!siteId),
        siteId ? [siteId] : [],
        opts.timeoutMs ?? VERIFIED_CLICKS_TIMEOUT_MS,
      );
      return new Map<string, number>(r.rows.map((x: { asin: unknown; n: unknown }) => [String(x.asin), Number(x.n) || 0]));
    });
  } catch {
    return new Map();
  }
}

/** Test/ops hook: drop the cached counts. */
export function resetVerifiedClicksCache(): void {
  CACHE.clear();
}
