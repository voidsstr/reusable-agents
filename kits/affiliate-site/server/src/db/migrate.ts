/**
 * The migration runner: applies *.sql files once each, in order, and records
 * each in a ledger table. A file that fails stops the run with its error, so a
 * site's start() exits and the replica never serves on a half-migrated schema
 * (the restart retries it).
 *
 * Ported from SpecPicks src/db/migrationRunner.ts (commit 60d8dee8 "Migration
 * runner: one implementation, under an advisory lock", plus 1deac339's
 * quote-aware splitter; see unit/db/fixtures/ported-from-db.json).
 *
 * Concurrent boots. Replicas starting together (a revision rollout, a
 * scale-out; max-replicas is 3) each read the ledger, each found the same file
 * pending, and each ran it. The slower one's INSERT into the ledger then failed
 * on its primary key and that replica exited, after a non-idempotent file (an
 * UPDATE ... SET n = n + 1, a CREATE TABLE without IF NOT EXISTS) had already
 * run twice. So pending files are applied only while this session holds a
 * Postgres advisory lock (MIGRATION_LOCK_KEY), and the ledger is re-read once
 * the lock is held: a file another replica applied while this one waited is
 * skipped, never run again.
 *   - The lock is taken only when something is pending, and released when the
 *     batch ends. A boot with nothing new takes no lock at all.
 *   - Session-level, on one dedicated connection. A migration file may commit
 *     its own transaction, which would release a pg_advisory_xact_lock before
 *     the file is recorded.
 *   - Polled with pg_try_advisory_lock, never waited for with
 *     pg_advisory_lock. A backend blocked inside that SELECT holds a snapshot
 *     for the whole wait, and CREATE INDEX CONCURRENTLY in the lock holder
 *     waits for every older snapshot, so Postgres kills one of the two with
 *     "deadlock detected" (reproduced on PG 15, 2026-11-12).
 *   - If the holder dies, Postgres drops its lock with its session, and a
 *     waiting replica applies whatever is still pending.
 * Operators applying a file by hand take the same lock in their session first
 * (SELECT pg_try_advisory_lock(1835624306)) and hold it until the file and its
 * ledger row are both committed.
 *
 * Kit changes (everything else is the reference runner, behaviour unchanged):
 *   - Ordered sources. A site runs the kit's migrations first, then its own
 *     (runKitMigrations). Each source's files run in name order; every file is
 *     recorded in ONE ledger by file name, so a name may appear in only one
 *     source, and site files may not use the kit's `kit_` prefix.
 *   - The ledger table and lock key are options; the defaults are the
 *     reference runner's (_migrations, 1835624306), which the operator
 *     runbooks name.
 *   - WARNING-level server notices raised while a file runs (RAISE WARNING in
 *     a DO block, for instance the publish guard's "role does not exist yet")
 *     are forwarded to `warn`, so they reach the boot log.
 *   - pendingMigrations() reports applied and pending files without changing
 *     anything (no ledger table is created, no lock is taken).
 *   - A command line: `node <kit>/dist/db/migrate.js --help`.
 */
import fs from 'fs';
import path from 'path';
import { Pool } from 'pg';

/**
 * The advisory-lock key every process that applies migrations to a database
 * takes: 0x6d696772, "migr" in ASCII, the same key as the reference sites'
 * runners. Advisory locks are scoped to one database, so a runner for another
 * database on the same server never contends for it. Changing it lets an image
 * built before the change run alongside one built after it, unserialized, so
 * don't.
 */
export const MIGRATION_LOCK_KEY = 1835624306;

/** The ledger table: one row per applied file (name, applied_at). */
export const DEFAULT_LEDGER_TABLE = '_migrations';

/** Kit migration files are named kit_NNNN_<what>.sql; site files may not use the prefix. */
export const KIT_MIGRATION_PREFIX = 'kit_';

/** A file every kit migrations directory contains (how kitMigrationsDir() recognises one). */
export const KIT_MIGRATION_SENTINEL = 'kit_0001_outbound_clicks.sql';

/**
 * How long a replica waits for another replica's batch. Migrations run before
 * app.listen, and the pre-listen warm that follows can take a large share of
 * Container Apps' ~240 s startup probe. 60 s keeps the two together. A replica
 * that times out exits, and Container Apps restarts it.
 */
const DEFAULT_LOCK_WAIT_MS = 60 * 1000;
const DEFAULT_LOCK_POLL_MS = 500;
const WAIT_NOTE_EVERY_MS = 15 * 1000;

const LEDGER_RE = /^(?:[a-z_][a-z0-9_]{0,62}\.)?[a-z_][a-z0-9_]{0,62}$/;

/** A server notice (pg's NoticeMessage has these fields). */
export interface MigrationNotice {
  severity?: string;
  message?: string;
}

type NoticeListener = (notice: MigrationNotice) => void;

/** The slice of pg's PoolClient the runner uses; a real PoolClient fits (tests pass a fake). */
export interface MigrationClient {
  query(text: string, values?: any[]): Promise<{ rows: any[] }>;
  release(err?: Error | boolean): void;
  on?(event: 'notice', listener: NoticeListener): unknown;
  removeListener?(event: 'notice', listener: NoticeListener): unknown;
}

/** What the runner needs from a pool; pg.Pool satisfies it. */
export interface MigrationPool {
  connect(): Promise<MigrationClient>;
}

export interface MigrationSource {
  /** A directory of *.sql files, applied in file-name order. Subdirectories are ignored. */
  dir: string;
  /** Name used in log lines (default: the directory's base name). */
  label?: string;
  /** A missing directory is a warning when true, an error when false (the default). */
  optional?: boolean;
  /** Reject any file in this source whose name starts with this prefix. */
  rejectPrefix?: string;
}

export interface MigrationRunnerOptions {
  /** One directory, as the reference runner takes it. A missing directory is a warning. */
  migrationsDir?: string;
  /**
   * Ordered sources: the first source's pending files, then the next one's.
   * A plain string is a required directory. Takes precedence over migrationsDir.
   */
  sources?: ReadonlyArray<string | MigrationSource>;
  /** The ledger table (default _migrations; may be schema-qualified). */
  ledgerTable?: string;
  /** The advisory-lock key (default MIGRATION_LOCK_KEY). */
  lockKey?: number;
  /** Give up waiting for another runner's lock after this long (default 60 s). */
  lockWaitMs?: number;
  /** Delay between pg_try_advisory_lock attempts (default 500 ms). */
  lockPollMs?: number;
  log?: (line: string) => void;
  warn?: (line: string) => void;
  /** Test seams. */
  sleep?: (ms: number) => Promise<void>;
  now?: () => number;
}

export interface MigrationRunSummary {
  /** .sql files across every source. */
  onDisk: number;
  applied: string[];
  /** Pending when this runner looked, then applied by another runner while it waited for the lock. */
  appliedElsewhere: string[];
}

/** One migration file on disk. */
export interface MigrationFile {
  /** The file name: the ledger key. */
  name: string;
  /** Absolute path. */
  path: string;
  /** The label of the source it came from. */
  source: string;
}

export class MigrationLockTimeoutError extends Error {
  constructor(readonly waitedMs: number, readonly pending: number) {
    super(
      `[migrations] another replica held the migration lock for over ${Math.round(waitedMs / 1000)} s ` +
      `while ${pending} migration(s) were pending; not starting on a schema that is still being migrated`,
    );
    this.name = 'MigrationLockTimeoutError';
  }
}

/** The runner was configured wrongly; nothing was applied. */
export class MigrationConfigError extends Error {
  constructor(message: string) {
    super(`[migrations] ${message}`);
    this.name = 'MigrationConfigError';
  }
}

interface Resolved {
  lockKey: number;
  ledgerTable: string;
  lockWaitMs: number;
  lockPollMs: number;
  log: (line: string) => void;
  warn: (line: string) => void;
  sleep: (ms: number) => Promise<void>;
  now: () => number;
}

function resolve(opts: MigrationRunnerOptions): Resolved {
  const ledgerTable = opts.ledgerTable ?? DEFAULT_LEDGER_TABLE;
  if (!LEDGER_RE.test(ledgerTable)) {
    throw new MigrationConfigError(`ledgerTable ${JSON.stringify(ledgerTable)} is not a lower-case SQL identifier`);
  }
  const lockKey = opts.lockKey ?? MIGRATION_LOCK_KEY;
  if (!Number.isSafeInteger(lockKey)) throw new MigrationConfigError(`lockKey ${String(lockKey)} is not an integer`);
  return {
    lockKey,
    ledgerTable,
    lockWaitMs: opts.lockWaitMs ?? DEFAULT_LOCK_WAIT_MS,
    lockPollMs: opts.lockPollMs ?? DEFAULT_LOCK_POLL_MS,
    log: opts.log ?? ((line: string) => console.log(line)),
    warn: opts.warn ?? ((line: string) => console.warn(line)),
    sleep: opts.sleep ?? ((ms: number) => new Promise<void>((r) => setTimeout(r, ms))),
    now: opts.now ?? Date.now,
  };
}

function resolveSources(opts: MigrationRunnerOptions): MigrationSource[] {
  if (opts.sources) {
    return opts.sources.map((s) => (typeof s === 'string' ? { dir: s } : { ...s }));
  }
  if (opts.migrationsDir) return [{ dir: opts.migrationsDir, optional: true }];
  throw new MigrationConfigError('no migrations directory: pass migrationsDir or sources');
}

function isFile(p: string): boolean {
  try {
    return fs.statSync(p).isFile();
  } catch {
    return false;
  }
}

/**
 * The .sql files of every source, in apply order (source order, then file name).
 * Throws MigrationConfigError on a missing required directory, a name that
 * appears in two sources, or a reserved prefix.
 */
export function listMigrationFiles(
  sources: ReadonlyArray<string | MigrationSource>,
  warn: (line: string) => void = (line) => console.warn(line),
): MigrationFile[] {
  const out: MigrationFile[] = [];
  const seen = new Map<string, string>();
  for (const raw of sources) {
    const src: MigrationSource = typeof raw === 'string' ? { dir: raw } : raw;
    const dir = path.resolve(src.dir);
    const label = src.label || path.basename(dir);
    let isDir = false;
    try {
      isDir = fs.statSync(dir).isDirectory();
    } catch {
      isDir = false;
    }
    if (!isDir) {
      if (src.optional) {
        warn(`No migrations directory found at ${dir}`);
        continue;
      }
      throw new MigrationConfigError(`migrations directory ${dir} (${label}) does not exist`);
    }
    const names = fs.readdirSync(dir).filter((f) => f.endsWith('.sql') && isFile(path.join(dir, f))).sort();
    for (const name of names) {
      if (src.rejectPrefix && name.startsWith(src.rejectPrefix)) {
        throw new MigrationConfigError(`${label} migration ${name} uses the reserved prefix '${src.rejectPrefix}'`);
      }
      const prev = seen.get(name);
      if (prev !== undefined) {
        throw new MigrationConfigError(`migration ${name} exists in both ${prev} and ${label}; the ledger records files by name`);
      }
      seen.set(name, label);
      out.push({ name, path: path.join(dir, name), source: label });
    }
  }
  return out;
}

/**
 * Take the migration lock for this session, polling until `lockWaitMs`.
 * Returns false (holding nothing) if another session still holds it then.
 * For a script that applies a file by hand: take it, apply the file and record
 * it in the ledger, commit, then releaseMigrationLock() on the same client.
 */
export async function acquireMigrationLock(client: Pick<MigrationClient, 'query'>, options: MigrationRunnerOptions = {}): Promise<boolean> {
  return takeLock(client, resolve(options));
}

/** Release the lock this session holds. Never throws; false means discard the connection. */
export async function releaseMigrationLock(client: Pick<MigrationClient, 'query'>, options: MigrationRunnerOptions = {}): Promise<boolean> {
  return releaseLock(client, resolve(options));
}

async function takeLock(client: Pick<MigrationClient, 'query'>, opts: Resolved): Promise<boolean> {
  const started = opts.now();
  let noteAt = 0;
  for (;;) {
    const r = await client.query('SELECT pg_try_advisory_lock($1::bigint) AS ok', [opts.lockKey]);
    if (r.rows[0]?.ok === true) return true;
    const waited = opts.now() - started;
    if (waited >= opts.lockWaitMs) return false;
    if (waited >= noteAt) {
      opts.log(`[migrations] another replica is applying migrations; waiting for the migration lock (${Math.round(waited / 1000)} s of up to ${Math.round(opts.lockWaitMs / 1000)} s)`);
      noteAt = waited + WAIT_NOTE_EVERY_MS;
    }
    await opts.sleep(Math.min(opts.lockPollMs, opts.lockWaitMs - waited));
  }
}

async function releaseLock(client: Pick<MigrationClient, 'query'>, opts: Resolved): Promise<boolean> {
  try {
    const r = await client.query('SELECT pg_advisory_unlock($1::bigint) AS ok', [opts.lockKey]);
    if (r.rows[0]?.ok !== true) opts.warn('[migrations] the migration lock was not held at release (did a migration file release it?)');
    return true;
  } catch (err: any) {
    opts.warn(`[migrations] could not release the migration lock: ${err?.message || err}`);
    return false;
  }
}

async function ledgerExists(client: Pick<MigrationClient, 'query'>, opts: Resolved): Promise<boolean> {
  const r = await client.query('SELECT to_regclass($1) IS NOT NULL AS ready', [opts.ledgerTable]);
  return r.rows[0]?.ready === true;
}

async function ensureMigrationsTable(client: Pick<MigrationClient, 'query'>, opts: Resolved, state: RunState): Promise<void> {
  if (await ledgerExists(client, opts)) return;
  // A fresh database. Concurrent CREATE TABLE IF NOT EXISTS from two runners
  // collides on pg_type ("duplicate key ... pg_type_typname_nsp_index"), so
  // create it under the lock. If the lock never comes, create it anyway: the
  // holder is already past this step.
  const locked = await takeLock(client, opts);
  try {
    await client.query(`
      CREATE TABLE IF NOT EXISTS ${opts.ledgerTable} (
        name TEXT PRIMARY KEY,
        applied_at TIMESTAMP DEFAULT NOW()
      )
    `);
  } finally {
    if (locked && !(await releaseLock(client, opts))) state.discard = new Error('migration lock release failed');
  }
}

async function recordedMigrations(client: Pick<MigrationClient, 'query'>, opts: Resolved): Promise<Set<string>> {
  const r = await client.query(`SELECT name FROM ${opts.ledgerTable}`);
  return new Set(r.rows.map((row: any) => String(row.name)));
}

export interface SqlStatement {
  /** The statement as written, comments and literals intact, without its ';'. */
  text: string;
  /** The same text with comments and quoted literals blanked: what keyword checks read. */
  code: string;
}

/**
 * Split SQL on top-level semicolons, the way psql does: a ';' inside a quoted
 * string, a quoted identifier, a dollar-quoted body ($tag$ ... $tag$) or a
 * comment does not end a statement.
 */
export function splitSqlStatements(sql: string): SqlStatement[] {
  const out: SqlStatement[] = [];
  let start = 0;
  let code = '';
  const isIdent = (c: string | undefined) => !!c && /[A-Za-z0-9_$]/.test(c);
  const blank = (s: string) => s.replace(/[^\n]/g, ' ');
  let i = 0;
  const n = sql.length;
  while (i < n) {
    const c = sql[i];
    const next = sql[i + 1];
    let end = -1;
    if (c === '-' && next === '-') {
      const nl = sql.indexOf('\n', i);
      end = nl === -1 ? n : nl;
    } else if (c === '/' && next === '*') {
      let depth = 1;
      let j = i + 2;
      while (j < n && depth > 0) {
        if (sql[j] === '/' && sql[j + 1] === '*') { depth++; j += 2; }
        else if (sql[j] === '*' && sql[j + 1] === '/') { depth--; j += 2; }
        else j++;
      }
      end = j;
    } else if (c === "'") {
      // E'...' strings take backslash escapes; every string takes ''.
      const escapes = (sql[i - 1] === 'E' || sql[i - 1] === 'e') && !isIdent(sql[i - 2]);
      let j = i + 1;
      while (j < n) {
        if (escapes && sql[j] === '\\') { j += 2; continue; }
        if (sql[j] === "'") {
          if (sql[j + 1] === "'") { j += 2; continue; }
          break;
        }
        j++;
      }
      end = Math.min(j + 1, n);
    } else if (c === '"') {
      let j = i + 1;
      while (j < n) {
        if (sql[j] === '"') {
          if (sql[j + 1] === '"') { j += 2; continue; }
          break;
        }
        j++;
      }
      end = Math.min(j + 1, n);
    } else if (c === '$' && !isIdent(sql[i - 1])) {
      const m = /^\$([A-Za-z_][A-Za-z0-9_]*)?\$/.exec(sql.slice(i));
      if (m) {
        const close = sql.indexOf(m[0], i + m[0].length);
        end = close === -1 ? n : close + m[0].length;
      }
    } else if (c === ';') {
      const text = sql.slice(start, i).trim();
      if (text) out.push({ text, code: code.trim() });
      start = i + 1;
      code = '';
      i++;
      continue;
    }
    if (end !== -1) {
      code += blank(sql.slice(i, end));
      i = end;
    } else {
      code += c;
      i++;
    }
  }
  const text = sql.slice(start).trim();
  if (text) out.push({ text, code: code.trim() });
  return out;
}

/** True when the file must run statement by statement: CONCURRENTLY in the SQL itself. */
export function needsStatementByStatement(sql: string): boolean {
  return splitSqlStatements(sql).some((s) => /\bconcurrently\b/i.test(s.code));
}

/** Apply one pending file and record it. Throws on failure. */
async function applyOne(client: MigrationClient, file: MigrationFile, opts: Resolved): Promise<void> {
  const sql = fs.readFileSync(file.path, 'utf8');
  // Forward RAISE WARNING (and anything more severe that did not abort) to
  // the boot log; NOTICEs such as "already exists, skipping" stay quiet.
  const onNotice: NoticeListener = (notice) => {
    const severity = String(notice?.severity || '').toUpperCase();
    if (severity && severity !== 'NOTICE' && severity !== 'INFO' && severity !== 'LOG' && severity !== 'DEBUG') {
      opts.warn(`[migrations] ${file.name}: ${severity}: ${notice?.message ?? ''}`);
    }
  };
  const listening = typeof client.on === 'function' && typeof client.removeListener === 'function';
  if (listening) client.on!('notice', onNotice);
  try {
    // node-postgres runs a multi-statement string as one implicit transaction.
    // CREATE INDEX CONCURRENTLY (and other non-transactional DDL) is illegal in
    // a transaction block and would throw, crash-looping the whole server at
    // startup (happened 2026-06-21). So a migration that uses CONCURRENTLY is
    // run statement-by-statement (each its own autocommit), and an individual
    // "already exists / cannot run in transaction" error is tolerated so a
    // single bad/half-applied DDL can never take the entire site down.
    //
    // CONCURRENTLY must appear in the SQL itself, not in a comment or a quoted
    // literal, and the split respects quoting (2026-11-12). The old check was a
    // bare /concurrently/i over the whole file, followed by stripping every
    // `--` to end of line and splitting on any ';' at a line end, which cut
    // stored article markdown at its first `--` (every table separator row).
    const statements = splitSqlStatements(sql);
    if (statements.some((s) => /\bconcurrently\b/i.test(s.code))) {
      for (const stmt of statements) {
        if (!stmt.code) continue; // only comments
        try {
          await client.query(stmt.text);
        } catch (e: any) {
          const msg = String(e?.message || e);
          if (/already exists|cannot run inside a transaction block/i.test(msg)) {
            opts.warn(`[migrations] tolerated in ${file.name}: ${msg.split('\n')[0]}`);
          } else {
            throw e;
          }
        }
      }
    } else {
      await client.query(sql);
    }
  } finally {
    if (listening) client.removeListener!('notice', onNotice);
  }
  await client.query(`INSERT INTO ${opts.ledgerTable} (name) VALUES ($1)`, [file.name]);
  opts.log(`Applied migration: ${file.name}`);
}

interface RunState {
  /** Set when the connection must be destroyed rather than reused. */
  discard?: Error;
}

async function runOn(client: MigrationClient, files: MigrationFile[], opts: Resolved, state: RunState): Promise<MigrationRunSummary> {
  await ensureMigrationsTable(client, opts, state);
  const summary: MigrationRunSummary = { onDisk: files.length, applied: [], appliedElsewhere: [] };
  const recorded = await recordedMigrations(client, opts);
  const pending = files.filter((f) => !recorded.has(f.name));
  if (pending.length === 0) return summary;

  if (!(await takeLock(client, opts))) throw new MigrationLockTimeoutError(opts.lockWaitMs, pending.length);
  try {
    // Re-read under the lock: whatever another replica applied while this one
    // waited is done, and running it again is the bug above.
    const appliedNow = await recordedMigrations(client, opts);
    for (const file of pending) {
      if (appliedNow.has(file.name)) {
        summary.appliedElsewhere.push(file.name);
        continue;
      }
      await applyOne(client, file, opts);
      summary.applied.push(file.name);
    }
  } catch (err) {
    // A file that failed inside its own BEGIN leaves the session aborted, and
    // the unlock below would fail on it.
    await client.query('ROLLBACK').catch(() => {});
    throw err;
  } finally {
    if (!(await releaseLock(client, opts))) state.discard = new Error('migration lock release failed');
  }
  if (summary.appliedElsewhere.length) {
    opts.log(`[migrations] ${summary.appliedElsewhere.length} applied by another replica while this one waited for the lock`);
  }
  return summary;
}

/**
 * Apply every pending migration, on one connection from `pool` (so the lock,
 * the files and their records share a session). Throws on the first failing
 * file, MigrationLockTimeoutError, or MigrationConfigError (before connecting).
 */
export async function runPendingMigrations(pool: MigrationPool, options: MigrationRunnerOptions): Promise<MigrationRunSummary> {
  const opts = resolve(options);
  const files = listMigrationFiles(resolveSources(options), opts.warn);
  const client = await pool.connect();
  const state: RunState = {};
  try {
    return await runOn(client, files, opts, state);
  } catch (err: any) {
    state.discard = state.discard ?? (err instanceof Error ? err : new Error(String(err)));
    throw err;
  } finally {
    // Back to the pool only when clean. After any error, destroy the
    // connection instead, as pool.query did when a statement threw. ROLLBACK
    // closes a transaction a file opened and never committed, so it cannot
    // leak into a request.
    if (state.discard) {
      client.release(state.discard);
    } else {
      try {
        await client.query('ROLLBACK');
        client.release();
      } catch (rbErr: any) {
        client.release(rbErr instanceof Error ? rbErr : new Error(String(rbErr)));
      }
    }
  }
}

export interface MigrationStatus {
  onDisk: number;
  /** Files on disk that the ledger records, in apply order. */
  applied: string[];
  /** Files on disk the ledger does not record, in apply order. */
  pending: MigrationFile[];
  /** Ledger rows with no file on disk (quarantined, renamed or from another branch). */
  unknown: string[];
  /** False when the ledger table does not exist yet (everything is pending). */
  ledgerExists: boolean;
}

/**
 * What runPendingMigrations would apply, without changing anything: no ledger
 * table is created and no lock is taken.
 */
export async function pendingMigrations(pool: MigrationPool, options: MigrationRunnerOptions): Promise<MigrationStatus> {
  const opts = resolve(options);
  const files = listMigrationFiles(resolveSources(options), opts.warn);
  const client = await pool.connect();
  let broken: Error | undefined;
  try {
    const exists = await ledgerExists(client, opts);
    const recorded = exists ? await recordedMigrations(client, opts) : new Set<string>();
    const onDisk = new Set(files.map((f) => f.name));
    return {
      onDisk: files.length,
      applied: files.filter((f) => recorded.has(f.name)).map((f) => f.name),
      pending: files.filter((f) => !recorded.has(f.name)),
      unknown: [...recorded].filter((name) => !onDisk.has(name)).sort(),
      ledgerExists: exists,
    };
  } catch (err) {
    broken = err instanceof Error ? err : new Error(String(err));
    throw err;
  } finally {
    client.release(broken);
  }
}

// ─── The kit's own migrations ─────────────────────────────────────────────

function hasSentinel(dir: string): boolean {
  return isFile(path.join(dir, KIT_MIGRATION_SENTINEL));
}

/**
 * The kit's migrations directory. KIT_MIGRATIONS_DIR in the environment wins;
 * otherwise the nearest ancestor of this module holding `migrations/` (the kit
 * checkout, or a build next to it) or `vendor/affiliate-kit/migrations/` (a
 * site that compiled the vendored kit into its own dist/). Throws
 * MigrationConfigError when none contains the kit's files.
 */
export function kitMigrationsDir(opts: { env?: NodeJS.ProcessEnv; startDir?: string } = {}): string {
  const env = opts.env ?? process.env;
  const fromEnv = env.KIT_MIGRATIONS_DIR;
  if (fromEnv) {
    const dir = path.resolve(fromEnv);
    if (!hasSentinel(dir)) throw new MigrationConfigError(`KIT_MIGRATIONS_DIR=${dir} has no ${KIT_MIGRATION_SENTINEL}`);
    return dir;
  }
  let at = path.resolve(opts.startDir ?? __dirname);
  for (let depth = 0; depth < 10; depth++) {
    for (const candidate of [path.join(at, 'migrations'), path.join(at, 'vendor', 'affiliate-kit', 'migrations')]) {
      if (hasSentinel(candidate)) return candidate;
    }
    const up = path.dirname(at);
    if (up === at) break;
    at = up;
  }
  throw new MigrationConfigError(`could not find the kit migrations directory above ${opts.startDir ?? __dirname}; set KIT_MIGRATIONS_DIR`);
}

export interface KitMigrationOptions extends Omit<MigrationRunnerOptions, 'migrationsDir' | 'sources'> {
  /** The site's own migrations, applied after the kit's. A missing directory is a warning. */
  siteMigrationsDir?: string;
  /** Override the kit's directory (default kitMigrationsDir()). */
  kitMigrationsDir?: string;
}

/** The sources runKitMigrations applies: the kit's, then the site's. */
export function kitMigrationSources(opts: Pick<KitMigrationOptions, 'siteMigrationsDir' | 'kitMigrationsDir'> = {}): MigrationSource[] {
  const sources: MigrationSource[] = [{ dir: opts.kitMigrationsDir ?? kitMigrationsDir(), label: 'kit' }];
  if (opts.siteMigrationsDir) {
    sources.push({ dir: opts.siteMigrationsDir, label: 'site', optional: true, rejectPrefix: KIT_MIGRATION_PREFIX });
  }
  return sources;
}

/** A site's boot path: the kit's migrations, then the site's, under the one lock and ledger. */
export async function runKitMigrations(pool: MigrationPool, opts: KitMigrationOptions = {}): Promise<MigrationRunSummary> {
  const { siteMigrationsDir, kitMigrationsDir: kitDir, ...rest } = opts;
  return runPendingMigrations(pool, { ...rest, sources: kitMigrationSources({ siteMigrationsDir, kitMigrationsDir: kitDir }) });
}

/**
 * Terminate this database's sessions that have sat 'idle in transaction' for
 * longer than `olderThanSeconds` (default 120), so a migration's ALTER is not
 * blocked behind a lock a crashed or looping client still holds. Best-effort
 * and opt-in: without pg_signal_backend (or the same role) nothing is
 * terminated, and any error is only a warning. Returns the number terminated.
 */
export async function killStaleTransactions(
  pool: MigrationPool,
  opts: { olderThanSeconds?: number; log?: (line: string) => void; warn?: (line: string) => void } = {},
): Promise<number> {
  const log = opts.log ?? ((line: string) => console.log(line));
  const warn = opts.warn ?? ((line: string) => console.warn(line));
  const seconds = Math.max(1, Math.floor(opts.olderThanSeconds ?? 120));
  let client: MigrationClient | undefined;
  let broken: Error | undefined;
  try {
    client = await pool.connect();
    const r = await client.query(
      `SELECT pg_terminate_backend(pid) AS killed, pid, age(now(), query_start)::text AS age
         FROM pg_stat_activity
        WHERE datname = current_database()
          AND state IN ('idle in transaction', 'idle in transaction (aborted)')
          AND query_start < now() - make_interval(secs => $1)
          AND pid <> pg_backend_pid()`,
      [seconds],
    );
    const killed = r.rows.filter((row: any) => row.killed === true);
    if (r.rows.length > 0) {
      log(`[migrations] cleared ${killed.length} of ${r.rows.length} stale 'idle in transaction' session(s) before migrating`);
      for (const row of r.rows) log(`     pid=${row.pid} age=${row.age} killed=${row.killed}`);
    }
    return killed.length;
  } catch (err: any) {
    broken = err instanceof Error ? err : new Error(String(err));
    warn(`[migrations] could not clear stale transactions: ${err?.message || err}`);
    return 0;
  } finally {
    client?.release(broken);
  }
}

// ─── Command line ──────────────────────────────────────────────────────────

export interface MigrateCliDeps {
  env?: NodeJS.ProcessEnv;
  cwd?: string;
  stdout?: (line: string) => void;
  stderr?: (line: string) => void;
  /** Test seam: the pool for a connection string. */
  createPool?: (connectionString: string) => MigrationPool & { end(): Promise<void> };
}

export const MIGRATE_USAGE = `usage: migrate [options]

Apply the affiliate-site kit's migrations, then the site's, under the
migration advisory lock, recording each file in the ledger table.

  --status              list applied and pending files; change nothing
  --site-dir <dir>      the site's migrations (default: ./db/migrations)
  --no-site             the kit's migrations only
  --kit-dir <dir>       the kit's migrations (default: found next to this module,
                        or $KIT_MIGRATIONS_DIR)
  --dsn-env <NAME>      environment variable holding the connection string
                        (default: DATABASE_URL); never pass a DSN as an argument
  --lock-wait-ms <n>    wait this long for another runner's lock (default 60000)
  --kill-stale          first terminate this database's sessions idle in a
                        transaction for over 2 minutes (best-effort)
  -h, --help            this text

Exit codes: 0 done, 1 a migration failed or the lock never came, 2 usage or
configuration error.`;

interface CliArgs {
  status: boolean;
  siteDir?: string;
  noSite: boolean;
  kitDir?: string;
  dsnEnv: string;
  lockWaitMs?: number;
  killStale: boolean;
  help: boolean;
}

/** A flag is echoed; anything else (it may be a pasted DSN) is only numbered. */
export function describeCliArg(arg: string, index: number): string {
  return /^-{1,2}[A-Za-z0-9][A-Za-z0-9-]{0,40}$/.test(arg) ? JSON.stringify(arg) : `#${index + 1} (not shown)`;
}

function parseCliArgs(argv: string[]): CliArgs {
  const args: CliArgs = { status: false, noSite: false, dsnEnv: 'DATABASE_URL', killStale: false, help: false };
  const value = (i: number, flag: string): string => {
    const v = argv[i + 1];
    if (v === undefined || v.startsWith('--')) throw new MigrationConfigError(`${flag} needs a value`);
    return v;
  };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    switch (a) {
      case '--status': args.status = true; break;
      case '--no-site': args.noSite = true; break;
      case '--kill-stale': args.killStale = true; break;
      case '-h': case '--help': args.help = true; break;
      case '--site-dir': args.siteDir = value(i, a); i++; break;
      case '--kit-dir': args.kitDir = value(i, a); i++; break;
      case '--dsn-env': {
        const v = value(i, a);
        // Never echo the value: a DSN pasted here would land in the log.
        if (!/^[A-Z_][A-Z0-9_]*$/.test(v)) throw new MigrationConfigError('--dsn-env takes an environment variable NAME (A-Z, 0-9, _), not a connection string');
        args.dsnEnv = v;
        i++;
        break;
      }
      case '--lock-wait-ms': {
        const v = Number(value(i, a));
        if (!Number.isFinite(v) || v < 0) throw new MigrationConfigError('--lock-wait-ms takes a non-negative number');
        args.lockWaitMs = v;
        i++;
        break;
      }
      default:
        throw new MigrationConfigError(`unknown argument ${describeCliArg(a, i)}`);
    }
  }
  if (args.noSite && args.siteDir) throw new MigrationConfigError('--no-site and --site-dir are exclusive');
  return args;
}

/** `migrate` command line. Returns the exit code; never prints the connection string. */
export async function migrateCli(argv: string[], deps: MigrateCliDeps = {}): Promise<number> {
  const env = deps.env ?? process.env;
  const out = deps.stdout ?? ((line: string) => console.log(line));
  const err = deps.stderr ?? ((line: string) => console.error(line));
  let args: CliArgs;
  try {
    args = parseCliArgs(argv);
  } catch (e: any) {
    err(String(e?.message || e));
    err(MIGRATE_USAGE);
    return 2;
  }
  if (args.help) {
    out(MIGRATE_USAGE);
    return 0;
  }
  const dsn = env[args.dsnEnv];
  if (!dsn) {
    err(`[migrations] $${args.dsnEnv} is not set`);
    return 2;
  }
  let sources: MigrationSource[];
  try {
    const kitDir = args.kitDir ? path.resolve(deps.cwd ?? process.cwd(), args.kitDir) : kitMigrationsDir({ env });
    const siteDir = args.noSite ? undefined : path.resolve(deps.cwd ?? process.cwd(), args.siteDir ?? path.join('db', 'migrations'));
    sources = kitMigrationSources({ kitMigrationsDir: kitDir, siteMigrationsDir: siteDir });
    listMigrationFiles(sources, () => {}); // configuration errors before connecting
  } catch (e: any) {
    err(String(e?.message || e));
    return 2;
  }
  const createPool = deps.createPool
    ?? ((connectionString: string) => new Pool({ connectionString, max: 1, application_name: 'affiliate-kit-migrate' }));
  const pool = createPool(dsn);
  const runnerOpts: MigrationRunnerOptions = { sources, log: out, warn: err, lockWaitMs: args.lockWaitMs };
  try {
    if (args.status) {
      const s = await pendingMigrations(pool, runnerOpts);
      out(`${s.applied.length} applied, ${s.pending.length} pending, ${s.onDisk} on disk${s.ledgerExists ? '' : ' (no ledger table yet)'}`);
      for (const f of s.pending) out(`pending  ${f.source}  ${f.name}`);
      for (const name of s.unknown) out(`unknown  ledger    ${name}  (recorded, not on disk)`);
      return 0;
    }
    if (args.killStale) await killStaleTransactions(pool, { log: out, warn: err });
    const summary = await runPendingMigrations(pool, runnerOpts);
    out(`[migrations] ${summary.applied.length} applied, ${summary.appliedElsewhere.length} applied elsewhere, ${summary.onDisk} on disk`);
    return 0;
  } catch (e: any) {
    err(`[migrations] failed: ${String(e?.message || e)}`);
    return e instanceof MigrationConfigError ? 2 : 1;
  } finally {
    await pool.end().catch(() => {});
  }
}

if (require.main === module) {
  migrateCli(process.argv.slice(2)).then(
    (code) => process.exit(code),
    (e) => {
      console.error(e);
      process.exit(1);
    },
  );
}
