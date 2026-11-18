import type { Pool } from 'pg';
import type { PoolClientLike, QueryResultLike } from '../../server/src/dbBudget';
import {
  AI_CITED_PAGES_SQL, AI_TRAFFIC_INSERT_SQL, aiTrafficParams, createPgKitDb, LATEST_ARTICLES_SQL,
  OUTBOUND_CLICK_INSERT_SQL, outboundClickParams, pgRedirectLookup, REDIRECT_BUYABLE_COLUMN, redirectProductFromRow,
  type PgPoolLike,
} from '../../server/src/http/pgKitDb';
import type { AiTrafficRow, OutboundClickRow } from '../../server/src/http/types';

/** A pool that records every statement and answers from a script. */
class FakePool implements PgPoolLike {
  readonly direct: Array<{ sql: string; params?: unknown[] }> = [];
  readonly onClient: Array<{ sql: string; params?: unknown[] }> = [];
  released = 0;
  rows: unknown[] = [];

  async query(sql: string, params?: unknown[]): Promise<QueryResultLike> {
    this.direct.push({ sql, params });
    return { rows: [] };
  }

  async connect(): Promise<PoolClientLike> {
    return {
      query: async (sql: string, params?: unknown[]) => {
        this.onClient.push({ sql, params });
        return { rows: /^\s*(BEGIN|COMMIT|ROLLBACK)/.test(sql) ? [] : this.rows };
      },
      release: () => { this.released++; },
    };
  }
}

/** Compile-time check: a real pg.Pool is a PgPoolLike (never called). */
export function realPoolFits(p: Pool): PgPoolLike {
  return p;
}

const CLICK: OutboundClickRow = {
  siteId: 'kit-example', asin: 'B0DS2X13PH', trackingId: 'kitexample-pdp-20', sourcePage: '/product/B0DS2X13PH',
  sourceKind: 'product', target: 'amazon', ipAddress: '73.12.1.9', userAgent: 'UA', isBot: false, botReason: null,
  country: 'US', deviceType: 'desktop', via: 'beacon', acceptLanguage: 'en-US', sessionPageviews: 2, msOnPage: 5200,
  placement: 'pdp-hero',
};

const AI: AiTrafficRow = {
  kind: 'crawler', source: 'claude-user', path: '/guides/x', statusCode: 200, userAgent: 'UA', referer: null,
  ipAddress: '160.79.104.10', siteId: 'kit-example', durationMs: 42, ipVerified: true,
  ipCheck: 'static-range:anthropic-outbound', forwardedFor: null,
};

function insertColumns(sql: string): string[] {
  const m = sql.match(/\(([^)]*)\)\s*VALUES/i);
  return (m?.[1] ?? '').split(',').map((s) => s.trim()).filter(Boolean);
}

function placeholders(sql: string): number {
  const m = sql.match(/VALUES\s*\(([\s\S]*)\)\s*$/i);
  return (m?.[1]?.match(/\$\d+/g) ?? []).length;
}

describe('insert SQL', () => {
  it('writes outbound_clicks with SpecPicks\' column names, plus classified_at', () => {
    expect(insertColumns(OUTBOUND_CLICK_INSERT_SQL)).toEqual([
      'site_id', 'asin', 'tracking_id', 'source_page', 'source_kind', 'target',
      'ip_address', 'user_agent', 'is_bot', 'bot_reason', 'country', 'device_type',
      'via', 'accept_language', 'session_pageviews', 'ms_on_page', 'placement', 'classified_at',
    ]);
    expect(placeholders(OUTBOUND_CLICK_INSERT_SQL)).toBe(17);
    expect(OUTBOUND_CLICK_INSERT_SQL).toMatch(/\$17, NOW\(\)\)$/);
    expect(outboundClickParams(CLICK)).toEqual([
      'kit-example', 'B0DS2X13PH', 'kitexample-pdp-20', '/product/B0DS2X13PH', 'product', 'amazon',
      '73.12.1.9', 'UA', false, null, 'US', 'desktop', 'beacon', 'en-US', 2, 5200, 'pdp-hero',
    ]);
  });

  it('writes ai_traffic_log with SpecPicks\' column names', () => {
    expect(insertColumns(AI_TRAFFIC_INSERT_SQL)).toEqual([
      'kind', 'source', 'path', 'status_code', 'user_agent', 'referer', 'ip_address', 'site_id',
      'duration_ms', 'ip_verified', 'ip_check', 'forwarded_for',
    ]);
    expect(placeholders(AI_TRAFFIC_INSERT_SQL)).toBe(12);
    expect(aiTrafficParams(AI)).toEqual([
      'crawler', 'claude-user', '/guides/x', 200, 'UA', null, '160.79.104.10', 'kit-example',
      42, true, 'static-range:anthropic-outbound', null,
    ]);
  });
});

describe('createPgKitDb', () => {
  it('inserts with plain pool.query (they already run after the response)', async () => {
    const pool = new FakePool();
    const db = createPgKitDb(pool);
    await db.insertOutboundClick(CLICK);
    await db.insertAiTraffic(AI);
    expect(pool.direct.map((q) => q.sql)).toEqual([OUTBOUND_CLICK_INSERT_SQL, AI_TRAFFIC_INSERT_SQL]);
    expect(pool.direct[0]?.params).toHaveLength(17);
    expect(pool.onClient).toEqual([]);
  });

  it('reads the cited pages under a 3 s statement_timeout and maps the rows', async () => {
    const pool = new FakePool();
    pool.rows = [{ path: '/guides/a', n: '7', title: 'A' }, { path: '/best/b', n: 2, title: null }];
    const db = createPgKitDb(pool);
    const out = await db.aiCitedPages!({
      sources: ['chatgpt'], days: 90, pathPrefixes: ['/guides/', '/best/'], minReferrals: 2, limit: 60,
      articlePathPrefix: '/guides/', siteId: 'kit-example',
    });
    expect(out).toEqual([{ path: '/guides/a', referrals: 7, title: 'A' }, { path: '/best/b', referrals: 2, title: null }]);
    expect(pool.onClient.map((q) => q.sql)).toEqual(['BEGIN; SET LOCAL statement_timeout = 3000', AI_CITED_PAGES_SQL, 'COMMIT']);
    expect(pool.onClient[1]?.params).toEqual([['chatgpt'], 90, 'kit-example', ['/guides/', '/best/'], 2, '/guides/', 60]);
    expect(pool.released).toBe(1);
  });

  it('reads the latest articles and honours a custom read budget', async () => {
    const pool = new FakePool();
    pool.rows = [{ slug: 's', title: 'T', day: '2026-11-17' }];
    const db = createPgKitDb(pool, { readTimeoutMs: 1500 });
    expect(await db.latestArticles!({ days: 30, limit: 25 })).toEqual([{ slug: 's', title: 'T', day: '2026-11-17' }]);
    expect(pool.onClient[0]?.sql).toBe('BEGIN; SET LOCAL statement_timeout = 1500');
    expect(pool.onClient[1]).toEqual({ sql: LATEST_ARTICLES_SQL, params: [30, 25] });
  });

  it('keeps upstream\'s query shapes', () => {
    expect(AI_CITED_PAGES_SQL).toContain("kind = 'referral' AND status_code = 200");
    expect(AI_CITED_PAGES_SQL).toContain('GROUP BY 1 HAVING COUNT(*) >= $5::int');
    expect(AI_CITED_PAGES_SQL).toContain("OR a.status = 'published'");
    expect(AI_CITED_PAGES_SQL).toContain('ORDER BY ref.n DESC, ref.path');
    expect(LATEST_ARTICLES_SQL).toContain("WHERE status = 'published' AND slug IS NOT NULL AND title IS NOT NULL");
    expect(LATEST_ARTICLES_SQL).toContain('published_at > NOW() - make_interval(days => $1::int) AND published_at <= NOW()');
    expect(LATEST_ARTICLES_SQL).toContain('ORDER BY published_at DESC');
  });
});

describe('pgRedirectLookup', () => {
  const SQL = 'SELECT asin, is_active, quality_flags, buyable, detail_page_url, title, brand FROM v_redirect WHERE asin = $1 LIMIT 1';

  it('runs the site\'s SQL with the ASIN under a 2 s budget and maps the first row', async () => {
    const pool = new FakePool();
    pool.rows = [{ asin: 'B0DS2X13PH', is_active: true, quality_flags: [], buyable: false, detail_page_url: 'https://www.amazon.com/dp/B0DS2X13PH', title: 'T', brand: 'B' }];
    const lookup = pgRedirectLookup(pool, SQL);
    expect(await lookup('B0DS2X13PH')).toEqual({
      asin: 'B0DS2X13PH', isActive: true, qualityFlags: [], buyable: false,
      detailPageUrl: 'https://www.amazon.com/dp/B0DS2X13PH', title: 'T', brand: 'B',
    });
    expect(pool.onClient[0]?.sql).toBe('BEGIN; SET LOCAL statement_timeout = 2000');
    expect(pool.onClient[1]).toEqual({ sql: SQL, params: ['B0DS2X13PH'] });
    pool.rows = [];
    expect(await lookup('B0DS2X13PH')).toBeNull();
  });

  it('refuses SQL that does not take the ASIN as $1', () => {
    expect(() => pgRedirectLookup(new FakePool(), 'SELECT 1')).toThrow(/\$1/);
  });

  it('defaults the optional columns and coerces text booleans', () => {
    expect(redirectProductFromRow({ buyable: true }, 'B0DS2X13PH')).toEqual({
      asin: 'B0DS2X13PH', isActive: true, qualityFlags: null, buyable: true, detailPageUrl: null, title: null, brand: null,
    });
    expect(redirectProductFromRow({ is_active: 'f', buyable: 't', title: '' }, 'B0DS2X13PH')).toMatchObject({ isActive: false, buyable: true, title: null });
  });

  it('treats a lookup without a buyable column as a configuration error (the recall gate never defaults open)', () => {
    expect(REDIRECT_BUYABLE_COLUMN).toBe('buyable');
    expect(() => redirectProductFromRow({}, 'B0DS2X13PH')).toThrow(/no 'buyable' column/);
    expect(() => redirectProductFromRow({ asin: 'B0DS2X13PH', is_active: true, title: 'T' }, 'B0DS2X13PH')).toThrow(TypeError);
  });

  it.each<[string, unknown, boolean]>([
    ['true', true, true],
    ["'t' (text)", 't', true],
    ["'true' (text)", 'true', true],
    ['1', 1, true],
    ['false', false, false],
    ['NULL (e.g. lifecycle IS NULL in the example SQL)', null, false],
    ["'f'", 'f', false],
    ["'no'", 'no', false],
    ["'0'", '0', false],
    ['an object', {}, false],
  ])('buyable %s -> %s (only an explicit true lets /r/ through)', (_label, v, want) => {
    expect(redirectProductFromRow({ buyable: v }, 'B0DS2X13PH').buyable).toBe(want);
  });

  it('pgRedirectLookup rejects a row without the column, and 404s on no row', async () => {
    const pool = new FakePool();
    const lookup = pgRedirectLookup(pool, 'SELECT asin, is_active FROM v WHERE asin = $1');
    pool.rows = [{ asin: 'B0DS2X13PH', is_active: true }];
    await expect(lookup('B0DS2X13PH')).rejects.toThrow(/no 'buyable' column/);
    pool.rows = [];
    await expect(lookup('B0DS2X13PH')).resolves.toBeNull();
  });
});
