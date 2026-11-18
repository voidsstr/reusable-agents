/**
 * The app's Postgres pool.
 *
 * In production DATABASE_URL is the app role's DSN (a Container App / ECS
 * secret): SELECT on the site's tables, INSERT on outbound_clicks and
 * ai_traffic_log, with its own CONNECTION LIMIT and statement_timeout
 * (deploy.env DB_APP_*). Keep PG_POOL_MAX x MAX_REPLICAS within that limit;
 * the deploy scripts refuse a deploy.env that does not.
 */
import { Pool, type PoolConfig } from 'pg';

export const DEFAULT_POOL_MAX = 5;
export const STATEMENT_TIMEOUT_MS = 15_000;

function intFrom(raw: string | undefined, dflt: number, min: number, max: number, name: string): number {
  if (raw === undefined || raw.trim() === '') return dflt;
  const n = Number(raw);
  if (!Number.isInteger(n) || n < min || n > max) throw new Error(`${name}=${raw} must be an integer from ${min} to ${max}`);
  return n;
}

export function poolConfig(env: NodeJS.ProcessEnv, siteId: string): PoolConfig {
  const connectionString = env.DATABASE_URL;
  if (!connectionString) throw new Error('DATABASE_URL is not set');
  return {
    connectionString,
    max: intFrom(env.PG_POOL_MAX, DEFAULT_POOL_MAX, 1, 50, 'PG_POOL_MAX'),
    idleTimeoutMillis: 30_000,
    connectionTimeoutMillis: 10_000,
    statement_timeout: STATEMENT_TIMEOUT_MS,
    application_name: `${siteId}-web`.slice(0, 63),
  };
}

export function createPool(env: NodeJS.ProcessEnv, siteId: string): Pool {
  const pool = new Pool(poolConfig(env, siteId));
  // An idle client that loses its connection must not crash the process.
  pool.on('error', (e) => console.error(`[db] idle client error: ${e.message}`));
  return pool;
}
