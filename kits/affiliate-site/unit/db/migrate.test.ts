/**
 * The migration runner (server/src/db/migrate.ts), offline.
 *
 * Ported from SpecPicks tests/unit/migrationRunner.test.ts (the fake-database
 * races and the quote-aware splitter), plus the kit's additions: ordered
 * sources under one ledger, configurable ledger and lock key, forwarded
 * WARNING notices, the read-only status, the kit's own directory, the
 * stale-transaction sweep and the command line. The same races run against a
 * real Postgres in migrate.pg.test.ts.
 */
import fs from 'fs';
import os from 'os';
import path from 'path';
import { Pool } from 'pg';
import {
  DEFAULT_LEDGER_TABLE,
  KIT_MIGRATION_SENTINEL,
  MIGRATION_LOCK_KEY,
  MigrationConfigError,
  MigrationLockTimeoutError,
  type MigrationNotice,
  type MigrationPool,
  acquireMigrationLock,
  killStaleTransactions,
  kitMigrationSources,
  kitMigrationsDir,
  listMigrationFiles,
  migrateCli,
  needsStatementByStatement,
  pendingMigrations,
  releaseMigrationLock,
  runKitMigrations,
  runPendingMigrations,
  splitSqlStatements,
} from '../../server/src/db/migrate';

const KIT = path.resolve(__dirname, '..', '..');
const KIT_MIGRATIONS = path.join(KIT, 'migrations');
const quiet = { log: () => {}, warn: () => {} };
const dirs: string[] = [];
afterAll(() => { for (const d of dirs) fs.rmSync(d, { recursive: true, force: true }); });

function fixtureDir(files: Record<string, string>): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'kit-migrations-'));
  dirs.push(dir);
  for (const [name, sql] of Object.entries(files)) fs.writeFileSync(path.join(dir, name), sql);
  return dir;
}

// The type contract: a real pg.Pool is a MigrationPool (compile-time check).
const _poolFits: (p: Pool) => MigrationPool = (p) => p;
void _poolFits;

describe('splitSqlStatements', () => {
  it('splits on top-level semicolons only', () => {
    const sql = [
      "INSERT INTO t VALUES ('a;b', 'it''s; fine', E'x\\';y');",
      'SELECT "odd;name" FROM t; -- trailing; comment',
      '/* block; /* nested; */ still comment */ SELECT 2;',
      'CREATE FUNCTION f() RETURNS int AS $fn$ SELECT 1; $fn$ LANGUAGE sql;',
      'SELECT $1::int, a$b FROM t',
    ].join('\n');
    const parts = splitSqlStatements(sql).map((s) => s.text.replace(/\s+/g, ' '));
    expect(parts).toEqual([
      "INSERT INTO t VALUES ('a;b', 'it''s; fine', E'x\\';y')",
      'SELECT "odd;name" FROM t',
      '-- trailing; comment /* block; /* nested; */ still comment */ SELECT 2',
      'CREATE FUNCTION f() RETURNS int AS $fn$ SELECT 1; $fn$ LANGUAGE sql',
      'SELECT $1::int, a$b FROM t',
    ]);
  });

  it('blanks comments and literals in .code, never in .text', () => {
    const [s] = splitSqlStatements("-- CONCURRENTLY here\nUPDATE a SET b = $x$ concurrently | --- | $x$, c = 'concurrently'");
    expect(s.code).not.toMatch(/concurrently/i);
    expect(s.text).toContain('| --- |');
    expect(splitSqlStatements('CREATE INDEX CONCURRENTLY i ON t (a)')[0].code).toMatch(/CONCURRENTLY/);
  });

  it('keeps an article-body UPDATE whole: prose "concurrently", table rows, a ; at a line end', () => {
    const body = 'Runs two models concurrently;\n| Model | tok/s |\n| --- | --- |\n| a | 1 |\nUse `aider --model x`.';
    const sql = `-- refresh\nUPDATE editorial_articles\n   SET body_md = $body_decay001$${body}$body_decay001$\n WHERE slug = 'x';\n`;
    const parts = splitSqlStatements(sql);
    expect(parts).toHaveLength(1);
    expect(parts[0].text).toContain(body);
    expect(/\bconcurrently\b/i.test(parts[0].code)).toBe(false);
  });

  it('a comment-only chunk has empty code', () => {
    const parts = splitSqlStatements('SELECT 1;\n-- the end\n');
    expect(parts.map((p) => p.code)).toEqual(['SELECT 1', '']);
  });

  it('nested dollar quotes with different tags stay one statement (the kit_0004 shape)', () => {
    const sql = "CREATE FUNCTION f() RETURNS text LANGUAGE sql AS $fn$ SELECT CASE WHEN x ~ $re$[()<>';]|(/|$)$re$ THEN 'a' END $fn$;\nSELECT 2;";
    const parts = splitSqlStatements(sql);
    expect(parts).toHaveLength(2);
    expect(parts[0].text).toContain("$re$[()<>';]|(/|$)$re$");
    expect(parts[1].text).toBe('SELECT 2');
  });

  it('needsStatementByStatement reads only SQL, not comments or literals', () => {
    expect(needsStatementByStatement('-- concurrently\nSELECT 1')).toBe(false);
    expect(needsStatementByStatement("SELECT 'concurrently'")).toBe(false);
    expect(needsStatementByStatement('CREATE INDEX CONCURRENTLY i ON t (a)')).toBe(true);
  });
});

// ---- A fake Postgres: just the statements the runner sends ----------------
//
// Fixture files "-- fake: add N" add N to a counter (after a few ms, so two
// runners overlap), "-- fake: fail" throws, "-- fake: warn" raises a WARNING
// and a NOTICE. Recording a file twice violates the ledger's primary key, as
// in Postgres.

class FakeDb {
  ledger = DEFAULT_LEDGER_TABLE;
  lockKey = MIGRATION_LOCK_KEY;
  tableExists = true;
  migrations = new Set<string>();
  lockHolder: FakeClient | null = null;
  lockIsBroken = false; // control: every try-lock succeeds
  counter = 0;
  bodiesRun: string[] = [];
  indexes = new Set<string>();
  staleRows: Array<{ killed: boolean; pid: number; age: string }> = [];
  failStaleQuery = false;
}

type Listener = (n: MigrationNotice) => void;

class FakeClient {
  queries: string[] = [];
  values: unknown[][] = [];
  released: 'reused' | 'destroyed' | null = null;
  listeners: Listener[] = [];
  constructor(private db: FakeDb, readonly name: string) {}

  on(_event: 'notice', l: Listener) { this.listeners.push(l); return this; }
  removeListener(_event: 'notice', l: Listener) { this.listeners = this.listeners.filter((x) => x !== l); return this; }

  async query(text: string, values: unknown[] = []): Promise<{ rows: any[] }> {
    const sql = text.trim();
    this.queries.push(sql);
    this.values.push(values);
    await new Promise((r) => setImmediate(r));
    const db = this.db;
    if (sql.startsWith('SELECT to_regclass')) {
      expect(values).toEqual([db.ledger]);
      return { rows: [{ ready: db.tableExists }] };
    }
    if (sql.startsWith('SELECT pg_try_advisory_lock')) {
      expect(values[0]).toBe(db.lockKey);
      if (db.lockIsBroken || db.lockHolder === null || db.lockHolder === this) {
        db.lockHolder = this;
        return { rows: [{ ok: true }] };
      }
      return { rows: [{ ok: false }] };
    }
    if (sql.startsWith('SELECT pg_advisory_unlock')) {
      expect(values[0]).toBe(db.lockKey);
      const ok = db.lockHolder === this;
      if (ok) db.lockHolder = null;
      return { rows: [{ ok }] };
    }
    if (sql.startsWith(`CREATE TABLE IF NOT EXISTS ${db.ledger} (`)) {
      db.tableExists = true;
      return { rows: [] };
    }
    if (sql === 'ROLLBACK') return { rows: [] };
    if (sql === `SELECT name FROM ${db.ledger}`) {
      if (!db.tableExists) throw new Error(`relation "${db.ledger}" does not exist`);
      return { rows: [...db.migrations].map((name) => ({ name })) };
    }
    if (sql.startsWith(`INSERT INTO ${db.ledger}`)) {
      const name = String(values[0]);
      if (db.migrations.has(name)) throw new Error(`duplicate key value violates unique constraint "${db.ledger}_pkey"`);
      db.migrations.add(name);
      return { rows: [] };
    }
    if (sql.includes('FROM pg_stat_activity')) {
      if (db.failStaleQuery) throw new Error('permission denied to terminate process');
      return { rows: db.staleRows };
    }
    const idx = /^(?:--[^\n]*\n\s*)*CREATE INDEX CONCURRENTLY (\w+)/.exec(sql);
    if (idx) {
      if (db.indexes.has(idx[1])) throw new Error(`relation "${idx[1]}" already exists`);
      db.indexes.add(idx[1]);
      return { rows: [] };
    }
    const m = /^-- fake: (add|fail|warn)(?: (\d+))?/.exec(sql);
    if (m) {
      db.bodiesRun.push(`${this.name}:${m[1]}${m[2] ? ' ' + m[2] : ''}`);
      await new Promise((r) => setTimeout(r, 5));
      if (m[1] === 'fail') throw new Error('division by zero');
      if (m[1] === 'warn') {
        for (const l of this.listeners) {
          l({ severity: 'NOTICE', message: 'relation "x" already exists, skipping' });
          l({ severity: 'WARNING', message: 'publish guard: role p does not exist yet' });
        }
        return { rows: [] };
      }
      db.counter += Number(m[2]);
      return { rows: [] };
    }
    throw new Error(`FakeClient: unexpected SQL: ${sql.slice(0, 80)}`);
  }

  release(err?: unknown) {
    this.released = err ? 'destroyed' : 'reused';
  }
}

function fakePool(db: FakeDb, name: string) {
  const clients: FakeClient[] = [];
  return {
    clients,
    ended: false,
    async connect() {
      const c = new FakeClient(db, name);
      clients.push(c);
      return c;
    },
    async end() { this.ended = true; },
  };
}

const RACE_FILES = {
  '001_one.sql': '-- fake: add 1',
  '002_ten.sql': '-- fake: add 10',
  '003_hundred.sql': '-- fake: add 100',
};

describe('runPendingMigrations (fake database)', () => {
  it('two replicas booting together apply each pending migration exactly once', async () => {
    const db = new FakeDb();
    const dir = fixtureDir(RACE_FILES);
    const [a, b] = [fakePool(db, 'a'), fakePool(db, 'b')];
    const opts = { ...quiet, migrationsDir: dir, lockPollMs: 1 };
    const [sa, sb] = await Promise.all([runPendingMigrations(a, opts), runPendingMigrations(b, opts)]);
    expect(db.counter).toBe(111);
    expect([...db.migrations].sort()).toEqual(['001_one.sql', '002_ten.sql', '003_hundred.sql']);
    expect([sa.applied.length, sb.applied.length].sort()).toEqual([0, 3]);
    expect([sa.appliedElsewhere.length, sb.appliedElsewhere.length].sort()).toEqual([0, 3]);
    expect(db.lockHolder).toBeNull();
    expect([a.clients[0].released, b.clients[0].released]).toEqual(['reused', 'reused']);
  });

  it('control: without mutual exclusion the same race applies a file twice', async () => {
    const db = new FakeDb();
    db.lockIsBroken = true;
    const dir = fixtureDir(RACE_FILES);
    const opts = { ...quiet, migrationsDir: dir, lockPollMs: 1 };
    const results = await Promise.allSettled([runPendingMigrations(fakePool(db, 'a'), opts), runPendingMigrations(fakePool(db, 'b'), opts)]);
    expect(db.bodiesRun.filter((x) => x.endsWith('add 1'))).toHaveLength(2);
    expect(results.some((r) => r.status === 'rejected' && /_migrations_pkey/.test(String((r as PromiseRejectedResult).reason)))).toBe(true);
  });

  it('a file another replica applied while this one waited is skipped, not run again', async () => {
    const db = new FakeDb();
    const dir = fixtureDir({ '001_one.sql': '-- fake: add 1', '002_ten.sql': '-- fake: add 10' });
    db.lockHolder = new FakeClient(db, 'other');
    setTimeout(() => {
      db.migrations.add('001_one.sql');
      db.counter += 1;
      db.lockHolder = null;
    }, 20);
    const pool = fakePool(db, 'me');
    const s = await runPendingMigrations(pool, { ...quiet, migrationsDir: dir, lockPollMs: 1 });
    expect(db.counter).toBe(11);
    expect(db.bodiesRun).toEqual(['me:add 10']);
    expect(s.appliedElsewhere).toEqual(['001_one.sql']);
    expect(s.applied).toEqual(['002_ten.sql']);
    const q = pool.clients[0].queries;
    const tries = q.filter((x) => x.startsWith('SELECT pg_try_advisory_lock')).length;
    const lastTry = q.lastIndexOf('SELECT pg_try_advisory_lock($1::bigint) AS ok');
    const reread = q.lastIndexOf('SELECT name FROM _migrations');
    const firstBody = q.findIndex((x) => x.startsWith('-- fake:'));
    expect(tries).toBeGreaterThan(1);
    expect(lastTry).toBeLessThan(reread);
    expect(reread).toBeLessThan(firstBody);
    expect(db.lockHolder).toBeNull();
  });

  it('a lock held past lockWaitMs throws MigrationLockTimeoutError and runs nothing', async () => {
    const db = new FakeDb();
    const holder = new FakeClient(db, 'holder');
    db.lockHolder = holder;
    const dir = fixtureDir(RACE_FILES);
    let clock = 0;
    const notes: string[] = [];
    const pool = fakePool(db, 'me');
    const run = runPendingMigrations(pool, {
      ...quiet, log: (l: string) => notes.push(l), migrationsDir: dir, lockWaitMs: 60_000, lockPollMs: 5_000,
      now: () => clock, sleep: async (ms: number) => { clock += ms; },
    });
    await expect(run).rejects.toBeInstanceOf(MigrationLockTimeoutError);
    await expect(run).rejects.toThrow(/over 60 s while 3 migration\(s\) were pending/);
    expect(db.bodiesRun).toEqual([]);
    expect(db.migrations.size).toBe(0);
    expect(db.lockHolder).toBe(holder);
    expect(pool.clients[0].queries.some((x) => x.startsWith('SELECT pg_advisory_unlock'))).toBe(false);
    expect(pool.clients[0].released).toBe('destroyed');
    // A wait note every 15 s of waiting, not one per poll.
    expect(notes.filter((l) => /waiting for the migration lock/.test(l)).length).toBe(4);
  });

  it('nothing pending: no lock, and the connection goes back to the pool', async () => {
    const db = new FakeDb();
    const dir = fixtureDir({ '001_one.sql': '-- fake: add 1' });
    db.migrations.add('001_one.sql');
    const pool = fakePool(db, 'me');
    const s = await runPendingMigrations(pool, { ...quiet, migrationsDir: dir });
    expect(s).toEqual({ onDisk: 1, applied: [], appliedElsewhere: [] });
    expect(pool.clients[0].queries.some((x) => /advisory/.test(x))).toBe(false);
    expect(pool.clients[0].queries.some((x) => x.startsWith('CREATE TABLE'))).toBe(false);
    expect(pool.clients[0].released).toBe('reused');
  });

  it('a failing file stops the run: nothing after it runs, ROLLBACK then unlock, connection destroyed', async () => {
    const db = new FakeDb();
    const dir = fixtureDir({ '001_one.sql': '-- fake: add 1', '002_broken.sql': '-- fake: fail', '003_ten.sql': '-- fake: add 10' });
    const pool = fakePool(db, 'me');
    await expect(runPendingMigrations(pool, { ...quiet, migrationsDir: dir })).rejects.toThrow('division by zero');
    expect(db.counter).toBe(1);
    expect([...db.migrations]).toEqual(['001_one.sql']);
    const q = pool.clients[0].queries;
    const bad = q.indexOf('-- fake: fail');
    expect(q.slice(bad, bad + 3).map((x) => x.split('(')[0])).toEqual(['-- fake: fail', 'ROLLBACK', 'SELECT pg_advisory_unlock']);
    expect(db.lockHolder).toBeNull();
    expect(pool.clients[0].released).toBe('destroyed');
  });

  it('a CONCURRENTLY file runs statement by statement on the locked connection, tolerating "already exists"', async () => {
    const db = new FakeDb();
    db.indexes.add('idx_b');
    const warns: string[] = [];
    const dir = fixtureDir({
      '001_idx.sql': '-- builds two indexes\nCREATE INDEX CONCURRENTLY idx_a ON t (a);\nCREATE INDEX CONCURRENTLY idx_b ON t (b);\n',
    });
    const pool = fakePool(db, 'me');
    const s = await runPendingMigrations(pool, { ...quiet, warn: (l: string) => warns.push(l), migrationsDir: dir });
    expect(s.applied).toEqual(['001_idx.sql']);
    expect([...db.indexes].sort()).toEqual(['idx_a', 'idx_b']);
    const q = pool.clients[0].queries;
    expect(q.filter((x) => x.includes('CREATE INDEX CONCURRENTLY'))).toEqual([
      expect.stringMatching(/CREATE INDEX CONCURRENTLY idx_a ON t \(a\)$/),
      'CREATE INDEX CONCURRENTLY idx_b ON t (b)',
    ]);
    expect(warns).toEqual(['[migrations] tolerated in 001_idx.sql: relation "idx_b" already exists']);
  });

  it('a fresh database creates the ledger under the lock', async () => {
    const db = new FakeDb();
    db.tableExists = false;
    const pool = fakePool(db, 'me');
    await runPendingMigrations(pool, { ...quiet, migrationsDir: fixtureDir({}) });
    const q = pool.clients[0].queries;
    const lock = q.findIndex((x) => x.startsWith('SELECT pg_try_advisory_lock'));
    const create = q.findIndex((x) => x.startsWith('CREATE TABLE IF NOT EXISTS _migrations'));
    const unlock = q.findIndex((x) => x.startsWith('SELECT pg_advisory_unlock'));
    expect(lock).toBeGreaterThanOrEqual(0);
    expect(lock).toBeLessThan(create);
    expect(create).toBeLessThan(unlock);
    expect(db.lockHolder).toBeNull();
    expect(q[create]).toMatch(/name TEXT PRIMARY KEY,\s+applied_at TIMESTAMP DEFAULT NOW\(\)/);
  });

  it('a missing single directory is a warning, as in the reference runner', async () => {
    const db = new FakeDb();
    const warns: string[] = [];
    const s = await runPendingMigrations(fakePool(db, 'me'), { ...quiet, warn: (l: string) => warns.push(l), migrationsDir: path.join(os.tmpdir(), 'kit-no-such-dir-x') });
    expect(s).toEqual({ onDisk: 0, applied: [], appliedElsewhere: [] });
    expect(warns[0]).toMatch(/No migrations directory found/);
  });
});

describe('kit additions to the runner', () => {
  it('applies sources in order: every pending file of the first, then the next, each in name order', async () => {
    const db = new FakeDb();
    const kit = fixtureDir({ 'kit_0002_b.sql': '-- fake: add 2', 'kit_0001_a.sql': '-- fake: add 1' });
    const site = fixtureDir({ '0001_catalog.sql': '-- fake: add 10', '0000_first.sql': '-- fake: add 20' });
    const s = await runPendingMigrations(fakePool(db, 'me'), { ...quiet, sources: [kit, site] });
    expect(s.applied).toEqual(['kit_0001_a.sql', 'kit_0002_b.sql', '0000_first.sql', '0001_catalog.sql']);
    expect(db.bodiesRun).toEqual(['me:add 1', 'me:add 2', 'me:add 20', 'me:add 10']);
    expect(s.onDisk).toBe(4);
  });

  it('a file name in two sources, a reserved prefix or a missing required directory is a config error before connecting', async () => {
    const db = new FakeDb();
    const a = fixtureDir({ '0001_x.sql': '-- fake: add 1' });
    const b = fixtureDir({ '0001_x.sql': '-- fake: add 2' });
    const pool = fakePool(db, 'me');
    await expect(runPendingMigrations(pool, { ...quiet, sources: [a, b] })).rejects.toThrow(/exists in both/);
    await expect(runPendingMigrations(pool, { ...quiet, sources: [{ dir: fixtureDir({ 'kit_0009_mine.sql': '' }), rejectPrefix: 'kit_', label: 'site' }] }))
      .rejects.toThrow(/site migration kit_0009_mine.sql uses the reserved prefix 'kit_'/);
    await expect(runPendingMigrations(pool, { ...quiet, sources: [path.join(os.tmpdir(), 'kit-missing-required')] }))
      .rejects.toBeInstanceOf(MigrationConfigError);
    await expect(runPendingMigrations(pool, { ...quiet })).rejects.toThrow(/no migrations directory/);
    expect(pool.clients).toHaveLength(0);
    expect(db.bodiesRun).toEqual([]);
  });

  it('ignores subdirectories and non-.sql files (templates/, _quarantine/, README)', () => {
    const dir = fixtureDir({ '0001_a.sql': '', 'README.md': '', 'x.sql.tmpl': '' });
    fs.mkdirSync(path.join(dir, '_quarantine'));
    fs.writeFileSync(path.join(dir, '_quarantine', '0002_bad.sql'), '');
    fs.mkdirSync(path.join(dir, 'odd.sql'));
    expect(listMigrationFiles([dir]).map((f) => f.name)).toEqual(['0001_a.sql']);
  });

  it('takes a custom ledger table and lock key', async () => {
    const db = new FakeDb();
    db.ledger = 'kit_ledger';
    db.lockKey = 42;
    db.tableExists = false;
    const s = await runPendingMigrations(fakePool(db, 'me'), { ...quiet, migrationsDir: fixtureDir({ '001.sql': '-- fake: add 5' }), ledgerTable: 'kit_ledger', lockKey: 42 });
    expect(s.applied).toEqual(['001.sql']);
    expect([...db.migrations]).toEqual(['001.sql']);
  });

  it('rejects a ledger name that is not a plain identifier, before connecting', async () => {
    const pool = fakePool(new FakeDb(), 'me');
    for (const bad of ['x; DROP TABLE y', 'Migrations', '"m"', 'a.b.c', '']) {
      await expect(runPendingMigrations(pool, { ...quiet, migrationsDir: fixtureDir({}), ledgerTable: bad })).rejects.toBeInstanceOf(MigrationConfigError);
    }
    expect(pool.clients).toHaveLength(0);
  });

  it('forwards WARNING notices raised by a file to warn(), not NOTICEs, and detaches the listener', async () => {
    const db = new FakeDb();
    const warns: string[] = [];
    const pool = fakePool(db, 'me');
    await runPendingMigrations(pool, { ...quiet, warn: (l: string) => warns.push(l), migrationsDir: fixtureDir({ '001_guard.sql': '-- fake: warn' }) });
    expect(warns).toEqual(['[migrations] 001_guard.sql: WARNING: publish guard: role p does not exist yet']);
    expect(pool.clients[0].listeners).toHaveLength(0);
  });

  it('the exported lock helpers take and release the same key', async () => {
    const db = new FakeDb();
    const c = new FakeClient(db, 'manual');
    expect(await acquireMigrationLock(c, quiet)).toBe(true);
    expect(db.lockHolder).toBe(c);
    expect(await releaseMigrationLock(c, quiet)).toBe(true);
    expect(db.lockHolder).toBeNull();
  });
});

describe('pendingMigrations (read-only status)', () => {
  it('with no ledger: everything pending, no table created, no lock taken', async () => {
    const db = new FakeDb();
    db.tableExists = false;
    const pool = fakePool(db, 'me');
    const s = await pendingMigrations(pool, { ...quiet, migrationsDir: fixtureDir(RACE_FILES) });
    expect(s.ledgerExists).toBe(false);
    expect(s.pending.map((f) => f.name)).toEqual(['001_one.sql', '002_ten.sql', '003_hundred.sql']);
    expect(pool.clients[0].queries.some((q) => /CREATE|advisory|INSERT/.test(q))).toBe(false);
    expect(pool.clients[0].released).toBe('reused');
  });

  it('splits applied, pending and ledger rows with no file', async () => {
    const db = new FakeDb();
    db.migrations = new Set(['001_one.sql', '000_quarantined.sql']);
    const s = await pendingMigrations(fakePool(db, 'me'), { ...quiet, migrationsDir: fixtureDir(RACE_FILES) });
    expect(s.applied).toEqual(['001_one.sql']);
    expect(s.pending.map((f) => f.name)).toEqual(['002_ten.sql', '003_hundred.sql']);
    expect(s.unknown).toEqual(['000_quarantined.sql']);
  });
});

describe('the kit migrations directory', () => {
  it('is found from this module and holds kit_0001..kit_0004 in order', () => {
    expect(kitMigrationsDir({ env: {} })).toBe(KIT_MIGRATIONS);
    expect(listMigrationFiles([KIT_MIGRATIONS]).map((f) => f.name)).toEqual([
      'kit_0001_outbound_clicks.sql', 'kit_0002_ai_traffic_log.sql', 'kit_0003_editorial_articles.sql', 'kit_0004_views.sql',
    ]);
  });

  it('is found under vendor/affiliate-kit from a site build, or from KIT_MIGRATIONS_DIR', () => {
    const site = fixtureDir({});
    const vendored = path.join(site, 'vendor', 'affiliate-kit', 'migrations');
    fs.mkdirSync(vendored, { recursive: true });
    fs.writeFileSync(path.join(vendored, KIT_MIGRATION_SENTINEL), '');
    const deep = path.join(site, 'dist', 'vendor', 'affiliate-kit', 'server', 'src', 'db');
    fs.mkdirSync(deep, { recursive: true });
    expect(kitMigrationsDir({ env: {}, startDir: deep })).toBe(vendored);
    expect(kitMigrationsDir({ env: { KIT_MIGRATIONS_DIR: vendored }, startDir: '/' })).toBe(vendored);
    expect(() => kitMigrationsDir({ env: { KIT_MIGRATIONS_DIR: site } })).toThrow(/has no kit_0001/);
    expect(() => kitMigrationsDir({ env: {}, startDir: fixtureDir({}) })).toThrow(MigrationConfigError);
  });

  it('runKitMigrations applies the kit first, then the site, and refuses site files named kit_*', async () => {
    const db = new FakeDb();
    const kit = fixtureDir({ [KIT_MIGRATION_SENTINEL]: '-- fake: add 1' });
    const site = fixtureDir({ '0001_catalog.sql': '-- fake: add 10' });
    const s = await runKitMigrations(fakePool(db, 'me'), { ...quiet, kitMigrationsDir: kit, siteMigrationsDir: site });
    expect(s.applied).toEqual([KIT_MIGRATION_SENTINEL, '0001_catalog.sql']);
    const bad = fixtureDir({ 'kit_0005_mine.sql': '' });
    await expect(runKitMigrations(fakePool(db, 'me'), { ...quiet, kitMigrationsDir: kit, siteMigrationsDir: bad }))
      .rejects.toThrow(/reserved prefix/);
    expect(kitMigrationSources({ kitMigrationsDir: kit }).map((s2) => s2.label)).toEqual(['kit']);
    // A site with no migrations yet: a warning, the kit's still apply.
    const db2 = new FakeDb();
    const warns: string[] = [];
    const s2 = await runKitMigrations(fakePool(db2, 'me'), { ...quiet, warn: (l: string) => warns.push(l), kitMigrationsDir: kit, siteMigrationsDir: path.join(site, 'none') });
    expect(s2.applied).toEqual([KIT_MIGRATION_SENTINEL]);
    expect(warns.join('\n')).toMatch(/No migrations directory found/);
  });
});

describe('killStaleTransactions', () => {
  it('terminates this database\'s idle-in-transaction sessions older than the cutoff and reports them', async () => {
    const db = new FakeDb();
    db.staleRows = [{ killed: true, pid: 11, age: '00:05:00' }, { killed: false, pid: 12, age: '00:03:00' }];
    const pool = fakePool(db, 'me');
    const logs: string[] = [];
    expect(await killStaleTransactions(pool, { log: (l) => logs.push(l), warn: () => {} })).toBe(1);
    const q = pool.clients[0].queries[0];
    expect(q).toMatch(/datname = current_database\(\)/);
    expect(q).toMatch(/idle in transaction/);
    expect(q).toMatch(/pid <> pg_backend_pid\(\)/);
    expect(pool.clients[0].values[0]).toEqual([120]);
    expect(logs[0]).toMatch(/cleared 1 of 2/);
    expect(pool.clients[0].released).toBe('reused');
  });

  it('is best-effort: an error is a warning and 0', async () => {
    const db = new FakeDb();
    db.failStaleQuery = true;
    const warns: string[] = [];
    const pool = fakePool(db, 'me');
    expect(await killStaleTransactions(pool, { log: () => {}, warn: (l) => warns.push(l), olderThanSeconds: 30 })).toBe(0);
    expect(warns[0]).toMatch(/could not clear stale transactions: permission denied/);
    expect(pool.clients[0].released).toBe('destroyed');
  });
});

describe('migrateCli', () => {
  const SECRET_DSN = 'postgres://u:hunter2-secret@db.invalid:5432/site';

  function cli(argv: string[], db: FakeDb, opts: { env?: NodeJS.ProcessEnv; cwd?: string } = {}) {
    const out: string[] = [];
    const err: string[] = [];
    const pools: Array<ReturnType<typeof fakePool>> = [];
    const dsns: string[] = [];
    const code = migrateCli(argv, {
      env: opts.env ?? { DATABASE_URL: SECRET_DSN, KIT_MIGRATIONS_DIR: '' },
      cwd: opts.cwd,
      stdout: (l) => out.push(l),
      stderr: (l) => err.push(l),
      createPool: (dsn) => {
        dsns.push(dsn);
        const p = fakePool(db, 'cli');
        pools.push(p);
        return p;
      },
    });
    return { code, out, err, pools, dsns };
  }

  it('--help prints usage and exits 0 without connecting', async () => {
    const r = cli(['--help'], new FakeDb());
    expect(await r.code).toBe(0);
    expect(r.out.join('\n')).toMatch(/usage: migrate/);
    expect(r.pools).toHaveLength(0);
  });

  it('usage errors exit 2: unknown flag, missing value, a DSN where a variable name belongs, exclusive flags', async () => {
    for (const argv of [['--bogus'], ['--site-dir'], ['--dsn-env', SECRET_DSN], ['--no-site', '--site-dir', 'x'], ['--lock-wait-ms', '-1']]) {
      const r = cli(argv, new FakeDb());
      expect(await r.code).toBe(2);
      expect(r.pools).toHaveLength(0);
      expect(r.err.join('\n')).not.toContain('hunter2');
    }
  });

  it('exits 2 when the DSN variable is unset', async () => {
    const r = cli(['--no-site', '--kit-dir', KIT_MIGRATIONS], new FakeDb(), { env: {} });
    expect(await r.code).toBe(2);
    expect(r.err.join('\n')).toMatch(/\$DATABASE_URL is not set/);
  });

  it('applies the kit and site migrations from the named variable, never printing the DSN', async () => {
    const db = new FakeDb();
    const kit = fixtureDir({ [KIT_MIGRATION_SENTINEL]: '-- fake: add 1' });
    const site = fixtureDir({});
    fs.mkdirSync(path.join(site, 'db', 'migrations'), { recursive: true });
    fs.writeFileSync(path.join(site, 'db', 'migrations', '0001_x.sql'), '-- fake: add 10');
    const r = cli(['--kit-dir', kit, '--dsn-env', 'SITE_DSN'], db, { env: { SITE_DSN: SECRET_DSN }, cwd: site });
    expect(await r.code).toBe(0);
    expect(r.dsns).toEqual([SECRET_DSN]);
    expect(db.counter).toBe(11);
    expect(r.out.join('\n')).toMatch(/2 applied, 0 applied elsewhere, 2 on disk/);
    expect([...r.out, ...r.err].join('\n')).not.toContain('hunter2');
    expect(r.pools[0].ended).toBe(true);
  });

  it('--status lists pending files and changes nothing', async () => {
    const db = new FakeDb();
    db.tableExists = false;
    const kit = fixtureDir({ [KIT_MIGRATION_SENTINEL]: '-- fake: add 1' });
    const r = cli(['--status', '--no-site', '--kit-dir', kit], db);
    expect(await r.code).toBe(0);
    expect(r.out[0]).toMatch(/0 applied, 1 pending, 1 on disk \(no ledger table yet\)/);
    expect(r.out[1]).toMatch(/pending\s+kit\s+kit_0001_outbound_clicks.sql/);
    expect(db.counter).toBe(0);
    expect(db.tableExists).toBe(false);
  });

  it('a failing migration exits 1; a reserved site file name exits 2 before connecting', async () => {
    const db = new FakeDb();
    const kit = fixtureDir({ [KIT_MIGRATION_SENTINEL]: '-- fake: fail' });
    const r = cli(['--no-site', '--kit-dir', kit], db);
    expect(await r.code).toBe(1);
    expect(r.err.join('\n')).toMatch(/failed: division by zero/);
    const site = fixtureDir({ 'kit_0007_x.sql': '' });
    const r2 = cli(['--kit-dir', kit, '--site-dir', site], new FakeDb());
    expect(await r2.code).toBe(2);
    expect(r2.pools).toHaveLength(0);
  });

  it('--kill-stale sweeps before migrating', async () => {
    const db = new FakeDb();
    const kit = fixtureDir({ [KIT_MIGRATION_SENTINEL]: '-- fake: add 1' });
    const r = cli(['--no-site', '--kit-dir', kit, '--kill-stale'], db);
    expect(await r.code).toBe(0);
    const all = r.pools[0].clients.flatMap((c) => c.queries);
    expect(all.findIndex((q) => q.includes('pg_stat_activity'))).toBeLessThan(all.findIndex((q) => q.startsWith('-- fake:')));
  });
});
