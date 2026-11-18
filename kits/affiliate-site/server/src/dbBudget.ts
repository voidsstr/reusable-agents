/**
 * Time budgets for OPTIONAL page modules.
 *
 * Ported from SpecPicks src/services/dbBudget.ts (see PORTED_FROM.json);
 * behaviour unchanged, typed against a minimal pool interface so tests can
 * pass a stand-in and pg.Pool fits as-is.
 *
 * Why: SpecPicks' PDP "Related products" rail once ran a category-wide scan
 * under the pool's 15s statement_timeout. When the shared database was busy
 * the scan hit that timeout, the body render threw, and the whole product
 * page shipped as HTTP 200 + noindex with an empty body -- an instruction to
 * search engines and assistants to drop the URL. An optional module must
 * degrade to "module missing", never to "page missing". The kit's sites share
 * that database server (nscappsdb), so the same budgets apply.
 *
 *   boundedQuery  runs ONE query with `SET LOCAL statement_timeout` inside its
 *                 own transaction on a dedicated client, so Postgres itself
 *                 cancels the backend (a JS Promise.race only stops waiting;
 *                 the query keeps burning DB CPU).
 *   withBudget    caps how long a caller waits, resolving to a fallback. Use it
 *                 only around work whose queries are themselves bounded.
 *   SwrCache      in-process stale-while-revalidate cache with single-flight
 *                 loads and short negative caching, for precomputed per-key
 *                 data.
 */

export interface QueryResultLike {
  rows: any[];
}

export interface PoolClientLike {
  query(sql: string, params?: any[]): Promise<QueryResultLike>;
  release(err?: Error | boolean): void;
}

/** What boundedQuery needs from a pool; pg.Pool satisfies it. */
export interface PoolLike {
  connect(): Promise<PoolClientLike>;
}

export async function boundedQuery(
  pool: PoolLike,
  sql: string,
  params: any[],
  timeoutMs: number,
  opts: { workMem?: string } = {},
): Promise<{ rows: any[] }> {
  const ms = Math.max(1, Math.floor(Number(timeoutMs) || 1));
  // work_mem for one big background sort (e.g. a sitemap build), never a
  // caller-supplied free-form string.
  const workMem = opts.workMem && /^\d{1,4}(kB|MB)$/.test(opts.workMem) ? `; SET LOCAL work_mem = '${opts.workMem}'` : '';
  const client = await pool.connect();
  let broken: Error | undefined;
  try {
    // One round trip for both statements; SET LOCAL only lives inside a
    // transaction, which is why this is not a bare `SET`.
    await client.query(`BEGIN; SET LOCAL statement_timeout = ${ms}${workMem}`);
    const r = await client.query(sql, params);
    await client.query('COMMIT');
    return r;
  } catch (e) {
    // ROLLBACK outside a transaction is only a WARNING, so always issue it; a
    // client must never go back to the pool inside an aborted transaction.
    try {
      await client.query('ROLLBACK');
    } catch (rbErr) {
      broken = rbErr instanceof Error ? rbErr : new Error(String(rbErr));
    }
    throw e;
  } finally {
    // Destroy the connection if it could not even roll back.
    client.release(broken);
  }
}

/**
 * Resolve to `fallback` if `work` has not settled within `ms`. The work keeps
 * running (and may still populate a cache); its eventual rejection is handled
 * so it can never surface as an unhandledRejection.
 */
export function withBudget<T>(work: Promise<T>, ms: number, fallback: T, onTimeout?: () => void): Promise<T> {
  work.catch(() => undefined);
  let timer: NodeJS.Timeout | undefined;
  const timeout = new Promise<T>((resolve) => {
    timer = setTimeout(() => {
      try { onTimeout?.(); } catch { /* logging only */ }
      resolve(fallback);
    }, Math.max(0, ms));
  });
  return Promise.race([work.catch(() => fallback), timeout]).finally(() => {
    if (timer) clearTimeout(timer);
  });
}

type SwrEntry<V> = { value: V; freshUntil: number };

export interface SwrCacheOptions {
  ttlMs: number;
  maxEntries: number;
  failTtlMs: number;
  now?: () => number;
}

/**
 * Stale-while-revalidate cache with single-flight loads.
 *
 *   fresh hit   -> value
 *   stale hit   -> value now, one background reload (a failed reload keeps the
 *                  stale value)
 *   miss        -> await the (shared) load; a failure is remembered for
 *                  `failTtlMs` so a struggling database is not hammered by
 *                  every request retrying the same load
 *
 * Entries are evicted oldest-inserted first beyond `maxEntries`.
 */
export class SwrCache<V> {
  private entries = new Map<string, SwrEntry<V>>();
  private inflight = new Map<string, Promise<V>>();
  private failedUntil = new Map<string, number>();

  constructor(private readonly opts: SwrCacheOptions) {}

  private now(): number {
    return this.opts.now ? this.opts.now() : Date.now();
  }

  private load(key: string, loader: () => Promise<V>): Promise<V> {
    const running = this.inflight.get(key);
    if (running) return running;
    const p = (async () => {
      try {
        const value = await loader();
        this.entries.delete(key);
        this.entries.set(key, { value, freshUntil: this.now() + this.opts.ttlMs });
        this.failedUntil.delete(key);
        while (this.entries.size > this.opts.maxEntries) {
          const oldest = this.entries.keys().next().value as string;
          this.entries.delete(oldest);
        }
        return value;
      } catch (e) {
        this.failedUntil.set(key, this.now() + this.opts.failTtlMs);
        throw e;
      } finally {
        this.inflight.delete(key);
      }
    })();
    this.inflight.set(key, p);
    return p;
  }

  async get(key: string, loader: () => Promise<V>): Promise<V> {
    const hit = this.entries.get(key);
    if (hit) {
      if (hit.freshUntil <= this.now() && !this.inflight.has(key)) {
        const retryAt = this.failedUntil.get(key) || 0;
        if (retryAt <= this.now()) this.load(key, loader).catch(() => undefined);
      }
      return hit.value;
    }
    const retryAt = this.failedUntil.get(key) || 0;
    if (retryAt > this.now() && !this.inflight.has(key)) {
      throw new Error(`swr-cache: recent load failure for ${key}`);
    }
    return this.load(key, loader);
  }

  /** Test/ops hook. */
  clear(): void {
    this.entries.clear();
    this.inflight.clear();
    this.failedUntil.clear();
  }

  get size(): number {
    return this.entries.size;
  }
}
