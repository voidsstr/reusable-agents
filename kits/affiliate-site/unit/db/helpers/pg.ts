/**
 * Real-Postgres test support for unit/db/*.pg.test.ts.
 *
 * Those suites run only when KIT_TEST_DATABASE_URL names a THROWAWAY server
 * and its superuser (see migrations/README.md "Tests" for a recipe that keeps
 * the password off every command line): each suite creates its own database
 * (so the migration advisory lock, which is per database, never couples two
 * jest workers) and its own uniquely named roles, and drops both afterwards.
 * Without the variable they are skipped and the offline suites still run.
 *
 * Because they CREATE and DROP databases and roles, they refuse a server that
 * does not look throwaway, before running any statement:
 *   - a host that is not loopback (127.0.0.0/8, ::1, localhost or a Unix
 *     socket directory), unless KIT_TEST_DATABASE_ALLOW_REMOTE=1;
 *   - a server holding any database other than postgres, the templates and
 *     the suites' own kit_* scratch databases (a developer's local server on
 *     5432, say), unless KIT_TEST_DATABASE_ALLOW_SHARED=1.
 * Errors name the host only, never the URL (it carries the password).
 */
import * as crypto from 'crypto';
import { Client, Pool, type PoolClient, type PoolConfig } from 'pg';

export const ADMIN_URL = process.env.KIT_TEST_DATABASE_URL || '';

/** Prefix of every scratch database these helpers create (uniqueName(prefix) with a kit_ prefix). */
export const SCRATCH_DB_PREFIX = 'kit_';

function isLocalHost(host: string): boolean {
  const h = host.trim().toLowerCase();
  return h === 'localhost'
    || /^127(\.\d{1,3}){3}$/.test(h)
    || h === '::1' || h === '[::1]'
    || h.startsWith('/'); // a Unix socket directory
}

/**
 * Why `url` must not be used as the throwaway admin server, or '' when it may.
 * Checks the URL's host and any `host` query parameter (node-postgres lets one
 * override the other), else PGHOST, else node-postgres' default (localhost).
 * The message names hosts only.
 */
export function adminUrlProblem(url: string, env: NodeJS.ProcessEnv = process.env): string {
  let u: URL;
  try {
    u = new URL(url);
  } catch {
    return 'KIT_TEST_DATABASE_URL does not parse as a postgres:// URL';
  }
  if (u.protocol !== 'postgres:' && u.protocol !== 'postgresql:') {
    return `KIT_TEST_DATABASE_URL must be a postgres:// URL (got scheme ${u.protocol})`;
  }
  let hosts = [decodeURIComponent(u.hostname), ...u.searchParams.getAll('host')].filter(Boolean);
  if (!hosts.length) hosts = [env.PGHOST || 'localhost'];
  const remote = hosts.filter((h) => !isLocalHost(h));
  if (remote.length && env.KIT_TEST_DATABASE_ALLOW_REMOTE !== '1') {
    return `KIT_TEST_DATABASE_URL names host ${remote.join(', ')}, which is not loopback; the *.pg suites create and drop `
      + 'databases and roles there. Point it at a throwaway local server, or set KIT_TEST_DATABASE_ALLOW_REMOTE=1 '
      + 'for a throwaway remote one.';
  }
  return '';
}

/** The host(s) KIT_TEST_DATABASE_URL names, for messages. */
function adminHost(url: string): string {
  try {
    const u = new URL(url);
    return [u.hostname, ...u.searchParams.getAll('host')].filter(Boolean).join(', ') || 'localhost';
  } catch {
    return '?';
  }
}

const URL_PROBLEM = ADMIN_URL ? adminUrlProblem(ADMIN_URL) : '';
if (URL_PROBLEM) {
  // Fail every *.pg suite loudly instead of skipping it or touching that server.
  throw new Error(URL_PROBLEM);
}

/** describe() when a test server is configured, describe.skip() otherwise. */
export const describePg: jest.Describe = ADMIN_URL ? describe : describe.skip;

/** A unique, lower-case identifier with this prefix. */
export function uniqueName(prefix: string): string {
  return `${prefix}_${process.pid}_${crypto.randomBytes(4).toString('hex')}`;
}

/** ADMIN_URL pointed at another database (and optionally another user). */
export function urlFor(database: string, auth?: { user: string; password: string }): string {
  const u = new URL(ADMIN_URL);
  u.pathname = `/${database}`;
  if (auth) {
    u.username = encodeURIComponent(auth.user);
    u.password = encodeURIComponent(auth.password);
  }
  return u.toString();
}

/** The slice of a pg Client foreignDatabases() uses (tests pass a fake). */
export interface QueryRunner {
  query(text: string, values?: unknown[]): Promise<{ rows: any[] }>;
}

/**
 * The databases on the admin server that are neither postgres, a template,
 * nor one of these suites' scratch databases. Any of them means the server is
 * not throwaway.
 */
export async function foreignDatabases(c: QueryRunner): Promise<string[]> {
  const r = await c.query(
    `SELECT datname FROM pg_database
      WHERE NOT datistemplate AND datname <> 'postgres' AND left(datname, length($1)) <> $1
      ORDER BY datname`,
    [SCRATCH_DB_PREFIX],
  );
  return r.rows.map((x: { datname: string }) => x.datname);
}

let serverChecked: Promise<void> | null = null;

async function admin<T>(fn: (c: Client) => Promise<T>): Promise<T> {
  const c = new Client({ connectionString: ADMIN_URL });
  await c.connect();
  try {
    return await fn(c);
  } finally {
    await c.end().catch(() => {});
  }
}

/** Throws unless the admin server looks throwaway (checked once per process, before the first statement that changes it). */
function checkServer(): Promise<void> {
  if (!serverChecked) {
    serverChecked = (async () => {
      if (process.env.KIT_TEST_DATABASE_ALLOW_SHARED === '1') return;
      const foreign = await admin(foreignDatabases);
      if (foreign.length) {
        const shown = foreign.slice(0, 5).join(', ') + (foreign.length > 5 ? `, ... (${foreign.length} in all)` : '');
        throw new Error(`KIT_TEST_DATABASE_URL's server (host ${adminHost(ADMIN_URL)}) holds other databases (${shown}), `
          + 'so it is not a throwaway server; the *.pg suites create and drop databases and roles there. Point it at a '
          + 'fresh one, or set KIT_TEST_DATABASE_ALLOW_SHARED=1 if this one is disposable.');
      }
    })();
  }
  return serverChecked;
}

export interface ScratchDb {
  name: string;
  url: string;
  /** A pool on the scratch database (closed by drop()). */
  pool(config?: Omit<PoolConfig, 'connectionString'>): Pool;
  /** Create a role for this suite (dropped by drop()). */
  createRole(prefix: string, opts?: { login?: boolean }): Promise<{ name: string; password: string }>;
  /** Close every pool, drop the database, then the roles. */
  drop(): Promise<void>;
}

export async function createScratchDb(prefix: string): Promise<ScratchDb> {
  if (!prefix.startsWith(SCRATCH_DB_PREFIX)) throw new Error(`scratch database prefixes start with ${SCRATCH_DB_PREFIX} (got ${prefix})`);
  await checkServer();
  const name = uniqueName(prefix);
  await admin((c) => c.query(`CREATE DATABASE ${name}`));
  const url = urlFor(name);
  const pools: Pool[] = [];
  const roles: string[] = [];
  return {
    name,
    url,
    pool(config = {}) {
      const p = new Pool({ ...config, connectionString: url });
      pools.push(p);
      return p;
    },
    async createRole(rolePrefix, opts = {}) {
      const role = uniqueName(rolePrefix);
      const password = crypto.randomBytes(12).toString('hex');
      await admin((c) => c.query(`CREATE ROLE ${role} ${opts.login ? 'LOGIN' : 'NOLOGIN'} PASSWORD '${password}'`));
      roles.push(role);
      return { name: role, password };
    },
    async drop() {
      while (pools.length) await pools.pop()!.end().catch(() => {});
      await admin(async (c) => {
        await c.query(`DROP DATABASE IF EXISTS ${name} WITH (FORCE)`);
        for (const r of roles) await c.query(`DROP ROLE IF EXISTS ${r}`).catch(() => {});
      });
    },
  };
}

/** Run `fn` on one client of `pool` as `role` (SET ROLE), resetting afterwards. */
export async function asRole<T>(pool: Pool, role: string, fn: (c: PoolClient) => Promise<T>): Promise<T> {
  const c = await pool.connect();
  try {
    await c.query(`SET ROLE ${role}`);
    return await fn(c);
  } finally {
    await c.query('RESET ROLE').catch(() => {});
    c.release();
  }
}
