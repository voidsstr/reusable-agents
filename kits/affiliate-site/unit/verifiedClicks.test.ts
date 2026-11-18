/**
 * 30-day verified-human clicks per ASIN: one bounded, grouped query over the
 * beacon series, cached, and an empty map (never an exception) when the
 * database is slow or down. No upstream test existed; these pin the port.
 */
import type { PoolLike } from '../server/src/dbBudget';
import {
  VERIFIED_CLICKS_TIMEOUT_MS,
  resetVerifiedClicksCache,
  verifiedClicks30dByAsin,
  verifiedClicksSql,
} from '../server/src/verifiedClicks';

function fakePool(handler: (sql: string, params: any[]) => Promise<{ rows: any[] }> | { rows: any[] }) {
  const log: Array<{ sql: string; params: any[] }> = [];
  let connects = 0;
  const pool: PoolLike = {
    connect: async () => {
      connects++;
      return {
        query: async (sql: string, params: any[] = []) => {
          log.push({ sql, params });
          if (/^\s*(BEGIN|COMMIT|ROLLBACK)/.test(sql)) return { rows: [] };
          return handler(sql, params);
        },
        release: () => undefined,
      };
    },
  };
  return { pool, log, connects: () => connects };
}

beforeEach(() => resetVerifiedClicksCache());

describe('verifiedClicks30dByAsin', () => {
  it('counts verified-human Amazon beacon clicks per ASIN', async () => {
    const { pool, log } = fakePool(() => ({ rows: [{ asin: 'B0DS2X13PH', n: 12 }, { asin: 'B000J01I1G', n: '3' }] }));
    const m = await verifiedClicks30dByAsin(pool);
    expect(Array.from(m.entries())).toEqual([['B0DS2X13PH', 12], ['B000J01I1G', 3]]);
    const q = log.find((l) => /FROM outbound_clicks/.test(l.sql))!;
    expect(q.sql).toContain("via = 'beacon' AND target = 'amazon' AND NOT is_bot");
    expect(q.sql).toContain("clicked_at > NOW() - INTERVAL '30 days'");
    expect(q.sql).toContain("asin ~ '^B[A-Z0-9]{9}$'");
    expect(q.sql).toMatch(/GROUP BY asin$/);
    expect(q.params).toEqual([]);
    expect(log[0]!.sql).toBe(`BEGIN; SET LOCAL statement_timeout = ${VERIFIED_CLICKS_TIMEOUT_MS}`);
  });

  it('caches the map (one query for repeated calls)', async () => {
    const { pool, connects } = fakePool(() => ({ rows: [{ asin: 'B0DS2X13PH', n: 1 }] }));
    await verifiedClicks30dByAsin(pool);
    await verifiedClicks30dByAsin(pool);
    expect(connects()).toBe(1);
  });

  it('filters by site id when asked, cached separately', async () => {
    const { pool, log } = fakePool((_sql, params) => ({ rows: params.length ? [{ asin: 'B0DS2X13PH', n: 5 }] : [] }));
    expect((await verifiedClicks30dByAsin(pool, { siteId: 'kit-example' })).get('B0DS2X13PH')).toBe(5);
    expect((await verifiedClicks30dByAsin(pool)).size).toBe(0);
    const scoped = log.find((l) => /site_id = \$1/.test(l.sql))!;
    expect(scoped.params).toEqual(['kit-example']);
    expect(verifiedClicksSql(false)).not.toContain('site_id');
  });

  it('returns an empty map when the database errors', async () => {
    const { pool } = fakePool(() => { throw new Error('canceling statement due to statement timeout'); });
    await expect(verifiedClicks30dByAsin(pool)).resolves.toEqual(new Map());
  });

  it('returns an empty map when the pool cannot connect', async () => {
    const pool: PoolLike = { connect: async () => { throw new Error('ECONNREFUSED'); } };
    await expect(verifiedClicks30dByAsin(pool)).resolves.toEqual(new Map());
  });
});
