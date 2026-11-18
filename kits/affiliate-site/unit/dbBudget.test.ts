/**
 * Optional page modules degrade to "module missing", never "page missing".
 * Ported from SpecPicks tests/unit/pdpRelatedRail.test.ts ('dbBudget'), plus
 * work_mem, the broken-client path and pg.Pool compatibility.
 */
import type { Pool } from 'pg';
import { boundedQuery, SwrCache, withBudget, type PoolLike } from '../server/src/dbBudget';

/** A pg Pool stand-in: `handler(sql, params)` answers every non-transaction query. */
function fakePool(
  handler: (sql: string, params: any[]) => Promise<{ rows: any[] }> | { rows: any[] },
  opts: { failRollback?: boolean } = {},
) {
  const log: string[] = [];
  const releases: Array<Error | boolean | undefined> = [];
  const client = {
    query: async (sql: string, params: any[] = []) => {
      log.push(sql);
      if (/^\s*ROLLBACK/.test(sql) && opts.failRollback) throw new Error('connection terminated');
      if (/^\s*(BEGIN|COMMIT|ROLLBACK)/.test(sql)) return { rows: [] };
      return handler(sql, params);
    },
    release: (err?: Error | boolean) => { releases.push(err); },
  };
  const pool: PoolLike = { connect: async () => client };
  return { pool, log, releases };
}

describe('boundedQuery', () => {
  it('sets a LOCAL statement_timeout, commits, and releases the client', async () => {
    const { pool, log, releases } = fakePool((_sql, params) => ({ rows: [{ ok: 1, params }] }));
    const r = await boundedQuery(pool, 'SELECT $1::int', [7], 1234);
    expect(r.rows).toEqual([{ ok: 1, params: [7] }]);
    expect(log).toEqual(['BEGIN; SET LOCAL statement_timeout = 1234', 'SELECT $1::int', 'COMMIT']);
    expect(releases).toEqual([undefined]);
  });

  it('rolls back and releases on error', async () => {
    const { pool, log, releases } = fakePool(() => { throw new Error('boom'); });
    await expect(boundedQuery(pool, 'SELECT 1', [], 10)).rejects.toThrow('boom');
    expect(log[log.length - 1]).toBe('ROLLBACK');
    expect(releases).toEqual([undefined]);
  });

  it('destroys a client that cannot even roll back', async () => {
    const { pool, releases } = fakePool(() => { throw new Error('canceling statement due to statement timeout'); }, { failRollback: true });
    await expect(boundedQuery(pool, 'SELECT 1', [], 10)).rejects.toThrow(/statement timeout/);
    expect(releases).toHaveLength(1);
    expect(releases[0]).toBeInstanceOf(Error);
  });

  it('clamps the timeout to a positive integer', async () => {
    const { pool, log } = fakePool(() => ({ rows: [] }));
    await boundedQuery(pool, 'SELECT 1', [], 0);
    await boundedQuery(pool, 'SELECT 1', [], 12.9);
    await boundedQuery(pool, 'SELECT 1', [], Number.NaN);
    expect(log.filter((s) => s.startsWith('BEGIN'))).toEqual([
      'BEGIN; SET LOCAL statement_timeout = 1',
      'BEGIN; SET LOCAL statement_timeout = 12',
      'BEGIN; SET LOCAL statement_timeout = 1',
    ]);
  });

  it('sets work_mem only from a strict size, never a free-form string', async () => {
    const { pool, log } = fakePool(() => ({ rows: [] }));
    await boundedQuery(pool, 'SELECT 1', [], 50, { workMem: '64MB' });
    await boundedQuery(pool, 'SELECT 1', [], 50, { workMem: "64MB'; DROP TABLE x; --" });
    expect(log.filter((s) => s.startsWith('BEGIN'))).toEqual([
      "BEGIN; SET LOCAL statement_timeout = 50; SET LOCAL work_mem = '64MB'",
      'BEGIN; SET LOCAL statement_timeout = 50',
    ]);
  });

  it('accepts a real pg.Pool (type-level)', () => {
    const fits: Pool extends PoolLike ? true : false = true;
    expect(fits).toBe(true);
  });
});

describe('withBudget', () => {
  it('falls back on timeout and on rejection', async () => {
    await expect(withBudget(new Promise<number>(() => undefined), 20, -1)).resolves.toBe(-1);
    await expect(withBudget(Promise.reject(new Error('x')), 1000, -2)).resolves.toBe(-2);
    await expect(withBudget(Promise.resolve(5), 1000, -3)).resolves.toBe(5);
  });

  it('reports a timeout, and a throwing reporter cannot break the fallback', async () => {
    const seen: string[] = [];
    await expect(withBudget(new Promise<number>(() => undefined), 5, 0, () => { seen.push('t'); })).resolves.toBe(0);
    await expect(withBudget(new Promise<number>(() => undefined), 5, 1, () => { throw new Error('logger down'); })).resolves.toBe(1);
    expect(seen).toEqual(['t']);
  });

  it('a late rejection never becomes an unhandled rejection', async () => {
    const unhandled: unknown[] = [];
    const onUnhandled = (e: unknown) => { unhandled.push(e); };
    process.on('unhandledRejection', onUnhandled);
    try {
      let reject!: (e: Error) => void;
      const work = new Promise<number>((_, r) => { reject = r; });
      await expect(withBudget(work, 5, 9)).resolves.toBe(9);
      reject(new Error('late'));
      await new Promise((r) => setTimeout(r, 10));
      expect(unhandled).toEqual([]);
    } finally {
      process.off('unhandledRejection', onUnhandled);
    }
  });
});

describe('SwrCache', () => {
  it('serves stale while reloading once, keeps stale on reload failure, single-flights misses', async () => {
    let now = 0;
    const cache = new SwrCache<number>({ ttlMs: 100, maxEntries: 10, failTtlMs: 50, now: () => now });
    let calls = 0;
    const load = async () => { calls++; return calls; };
    const [x, y] = await Promise.all([cache.get('k', load), cache.get('k', load)]);
    expect([x, y, calls]).toEqual([1, 1, 1]);
    now = 150; // stale
    expect(await cache.get('k', async () => { throw new Error('db down'); })).toBe(1);
    await new Promise((r) => setImmediate(r));
    expect(await cache.get('k', load)).toBe(1); // failed reload keeps stale, backs off
    now = 250;
    expect(await cache.get('k', load)).toBe(1); // serves stale, reloads in background
    await new Promise((r) => setImmediate(r));
    expect(await cache.get('k', load)).toBe(2);
  });

  it('remembers a failed miss for failTtlMs, then retries', async () => {
    let now = 0;
    const cache = new SwrCache<string>({ ttlMs: 100, maxEntries: 10, failTtlMs: 50, now: () => now });
    await expect(cache.get('k', async () => { throw new Error('db down'); })).rejects.toThrow('db down');
    await expect(cache.get('k', async () => 'ok')).rejects.toThrow(/recent load failure for k/);
    now = 60;
    await expect(cache.get('k', async () => 'ok')).resolves.toBe('ok');
  });

  it('evicts the oldest-inserted entry beyond maxEntries', async () => {
    const cache = new SwrCache<string>({ ttlMs: 1000, maxEntries: 2, failTtlMs: 10 });
    await cache.get('a', async () => 'A');
    await cache.get('b', async () => 'B');
    await cache.get('c', async () => 'C');
    expect(cache.size).toBe(2);
    let reloaded = false;
    await cache.get('a', async () => { reloaded = true; return 'A2'; });
    expect(reloaded).toBe(true);
    cache.clear();
    expect(cache.size).toBe(0);
  });
});
