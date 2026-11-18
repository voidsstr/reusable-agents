/**
 * The migration runner and the kit's migrations on a real Postgres.
 *
 * Runs only with KIT_TEST_DATABASE_URL (a throwaway server's superuser; see
 * helpers/pg.ts). Each suite works in its own scratch database.
 *
 *   - the reference runner's races (three replicas, a failing file, a real
 *     CREATE INDEX CONCURRENTLY, prose that says "concurrently");
 *   - kit_0001..kit_0004 through runKitMigrations: applied once, recorded,
 *     every column, index and view column identical to the reference
 *     deployment's live schema (unit/db/fixtures/upstream-schema.json), and
 *     every file idempotent on its own;
 *   - the hero_image_url constraints and the updated_at content trigger;
 *   - adoption: a database that already has the reference tables, views and
 *     triggers takes the kit migrations without losing anything.
 */
import fs from 'fs';
import os from 'os';
import path from 'path';
import type { Pool } from 'pg';
import { MIGRATION_LOCK_KEY, runKitMigrations, runPendingMigrations } from '../../server/src/db/migrate';
import { createScratchDb, describePg, type ScratchDb } from './helpers/pg';

const KIT = path.resolve(__dirname, '..', '..');
const KIT_MIGRATIONS = path.join(KIT, 'migrations');
const KIT_FILES = fs.readdirSync(KIT_MIGRATIONS).filter((f) => f.endsWith('.sql')).sort();
const UPSTREAM = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures', 'upstream-schema.json'), 'utf8')) as {
  tables: Record<string, { columns: Array<{ name: string; type: string; nullable: boolean; default: string | null; kitDefault?: string }>; indexes: string[]; checkConstraints: string[] }>;
  views: Record<string, Array<{ name: string; type: string }>>;
};
const quiet = { log: () => {}, warn: () => {} };
const dirs: string[] = [];
afterAll(() => { for (const d of dirs) fs.rmSync(d, { recursive: true, force: true }); });

function fixtureDir(files: Record<string, string>): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'kit-migrations-pg-'));
  dirs.push(dir);
  for (const [name, sql] of Object.entries(files)) fs.writeFileSync(path.join(dir, name), sql);
  return dir;
}

async function columnsOf(pool: Pool, rel: string) {
  const r = await pool.query(
    `SELECT a.attname AS name, format_type(a.atttypid, a.atttypmod) AS type, NOT a.attnotnull AS nullable,
            pg_get_expr(d.adbin, d.adrelid) AS "default"
       FROM pg_attribute a
       LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
      WHERE a.attrelid = to_regclass($1) AND a.attnum > 0 AND NOT a.attisdropped
      ORDER BY a.attnum`,
    [rel],
  );
  return r.rows as Array<{ name: string; type: string; nullable: boolean; default: string | null }>;
}

const SNAPSHOT_SQL = `
SELECT jsonb_build_object(
  'columns', (SELECT jsonb_agg(jsonb_build_array(table_name, column_name, ordinal_position, data_type, is_nullable, column_default)
                               ORDER BY table_name, ordinal_position)
                FROM information_schema.columns WHERE table_schema = 'public'),
  'indexes', (SELECT jsonb_agg(indexdef ORDER BY indexname) FROM pg_indexes WHERE schemaname = 'public'),
  'constraints', (SELECT jsonb_agg(conname || ':' || pg_get_constraintdef(oid) || ':' || convalidated ORDER BY conname)
                    FROM pg_constraint WHERE connamespace = 'public'::regnamespace),
  'triggers', (SELECT jsonb_agg(tgname || ':' || pg_get_triggerdef(oid) ORDER BY tgname) FROM pg_trigger WHERE NOT tgisinternal),
  'views', (SELECT jsonb_agg(viewname || ':' || definition ORDER BY viewname) FROM pg_views WHERE schemaname = 'public'),
  'functions', (SELECT jsonb_agg(p.proname || ':' || md5(pg_get_functiondef(p.oid)) ORDER BY p.proname)
                  FROM pg_proc p WHERE p.pronamespace = 'public'::regnamespace)
) AS snap`;

describePg('runPendingMigrations (real Postgres)', () => {
  let db: ScratchDb;
  beforeEach(async () => { db = await createScratchDb('kit_runner'); });
  afterEach(async () => { await db.drop(); });

  it('three replicas racing on a fresh database apply each file once', async () => {
    const dir = fixtureDir({
      '001_create.sql': 'CREATE TABLE race_counter (n integer NOT NULL); INSERT INTO race_counter VALUES (0);',
      '002_slow_bump.sql': 'SELECT pg_sleep(0.3); UPDATE race_counter SET n = n + 1;',
      '003_index.sql': '-- a real CONCURRENTLY build, run outside a transaction\nCREATE INDEX CONCURRENTLY race_counter_n ON race_counter (n);\n',
      '005_hundred.sql': 'UPDATE race_counter SET n = n + 100;',
    });
    const admin = db.pool({ max: 1 });
    const results = await Promise.all([1, 2, 3].map(() => runPendingMigrations(db.pool({ max: 1 }), { ...quiet, migrationsDir: dir, lockPollMs: 20 })));
    expect((await admin.query('SELECT n FROM race_counter')).rows[0].n).toBe(101);
    expect((await admin.query('SELECT name FROM _migrations ORDER BY name')).rows.map((r) => r.name))
      .toEqual(['001_create.sql', '002_slow_bump.sql', '003_index.sql', '005_hundred.sql']);
    expect(results.reduce((t, s) => t + s.applied.length, 0)).toBe(4);
    expect((await admin.query(`SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass('race_counter_n')`)).rows).toEqual([{ indisvalid: true }]);
    // pg_locks spans the cluster; other suites hold the same key in their own databases.
    const held = await admin.query(
      `SELECT count(*)::int AS n FROM pg_locks
        WHERE locktype = 'advisory' AND objid = $1
          AND database = (SELECT oid FROM pg_database WHERE datname = current_database())`,
      [MIGRATION_LOCK_KEY],
    );
    expect(held.rows[0].n).toBe(0);
  }, 30_000);

  it('a failing file releases the lock: the next runner gets it at once and hits the same error', async () => {
    const dir = fixtureDir({
      '001_ok.sql': 'CREATE TABLE t (n int);',
      '002_broken.sql': 'BEGIN; INSERT INTO t VALUES (1); SELECT 1/0; COMMIT;',
    });
    const admin = db.pool({ max: 1 });
    await expect(runPendingMigrations(db.pool({ max: 1 }), { ...quiet, migrationsDir: dir })).rejects.toThrow(/division by zero/);
    const started = Date.now();
    await expect(runPendingMigrations(db.pool({ max: 1 }), { ...quiet, migrationsDir: dir, lockWaitMs: 2_000 })).rejects.toThrow(/division by zero/);
    expect(Date.now() - started).toBeLessThan(1_500);
    expect((await admin.query('SELECT name FROM _migrations')).rows).toEqual([{ name: '001_ok.sql' }]);
    expect((await admin.query('SELECT count(*)::int AS n FROM t')).rows[0].n).toBe(0);
  }, 30_000);

  it('a file whose comments and prose say "concurrently" runs as written: the stored text keeps every `--` and `;`', async () => {
    const body = 'Two models can run concurrently;\n| Model | tok/s |\n| --- | --- |\n| a | 1 |\nRun `aider --model x`.';
    const dir = fixtureDir({
      '001_article.sql': 'CREATE TABLE article (body text);',
      '002_refresh.sql': `-- an article refresh whose prose says concurrently\nINSERT INTO article VALUES ($body$${body}$body$);\n`,
    });
    const s = await runPendingMigrations(db.pool({ max: 1 }), { ...quiet, migrationsDir: dir });
    expect(s.applied).toEqual(['001_article.sql', '002_refresh.sql']);
    expect((await db.pool({ max: 1 }).query('SELECT body FROM article')).rows).toEqual([{ body }]);
  }, 30_000);

  it('a file runs as one implicit transaction: SET LOCAL holds for the file and nothing after it', async () => {
    const dir = fixtureDir({
      '001_local.sql': "SET LOCAL lock_timeout = '5s';\nCREATE TABLE seen AS SELECT current_setting('lock_timeout') AS v;",
    });
    const pool = db.pool({ max: 1 });
    const before = (await pool.query('SHOW lock_timeout')).rows[0].lock_timeout;
    await runPendingMigrations(pool, { ...quiet, migrationsDir: dir });
    expect((await pool.query('SELECT v FROM seen')).rows).toEqual([{ v: '5s' }]);
    expect((await pool.query('SHOW lock_timeout')).rows[0].lock_timeout).toBe(before);
  }, 30_000);

  it("a file's SET LOCAL statement_timeout outlasts the role's short one, statement by statement (the kit files' second line)", async () => {
    // A session like a site role with a short statement_timeout (binding amendment 9).
    const short = () => db.pool({ max: 1, options: '-c statement_timeout=150' });
    const protectedDir = fixtureDir({ '001_slow.sql': "SET LOCAL statement_timeout = '15min';\nSELECT pg_sleep(0.4);\nCREATE TABLE slow_done (n int);" });
    expect((await runPendingMigrations(short(), { ...quiet, migrationsDir: protectedDir })).applied).toEqual(['001_slow.sql']);
    const bareDir = fixtureDir({ '002_slow_bare.sql': 'SELECT 1;\nSELECT pg_sleep(0.4);' });
    await expect(runPendingMigrations(short(), { ...quiet, migrationsDir: bareDir })).rejects.toThrow(/statement timeout/);
    const admin = db.pool({ max: 1 });
    expect((await admin.query('SELECT name FROM _migrations ORDER BY name')).rows).toEqual([{ name: '001_slow.sql' }]);
    expect((await admin.query(`SELECT to_regclass('slow_done') IS NOT NULL AS made`)).rows).toEqual([{ made: true }]);
  }, 30_000);

  it('forwards a RAISE WARNING from a file to warn()', async () => {
    const warns: string[] = [];
    const dir = fixtureDir({ '001_warn.sql': "DO $$ BEGIN RAISE NOTICE 'quiet'; RAISE WARNING 'heads up'; END $$;" });
    await runPendingMigrations(db.pool({ max: 1 }), { ...quiet, warn: (l: string) => warns.push(l), migrationsDir: dir });
    expect(warns).toEqual(['[migrations] 001_warn.sql: WARNING: heads up']);
  }, 30_000);
});

describePg('kit migrations (real Postgres)', () => {
  let db: ScratchDb;
  let pool: Pool;
  beforeAll(async () => {
    db = await createScratchDb('kit_schema');
    pool = db.pool({ max: 2 });
  });
  afterAll(async () => { await db.drop(); });

  it('runKitMigrations applies kit_0001..kit_0004 and then the site, once, under the ledger', async () => {
    const site = fixtureDir({ '0001_site.sql': 'CREATE TABLE site_marker (id int);' });
    const first = await runKitMigrations(db.pool({ max: 1 }), { ...quiet, kitMigrationsDir: KIT_MIGRATIONS, siteMigrationsDir: site });
    expect(first.applied).toEqual([...KIT_FILES, '0001_site.sql']);
    const second = await runKitMigrations(db.pool({ max: 1 }), { ...quiet, kitMigrationsDir: KIT_MIGRATIONS, siteMigrationsDir: site });
    expect(second).toEqual({ onDisk: KIT_FILES.length + 1, applied: [], appliedElsewhere: [] });
    const ledger = await pool.query('SELECT name FROM _migrations ORDER BY applied_at, name');
    expect(ledger.rows.map((r) => r.name).sort()).toEqual([...KIT_FILES, '0001_site.sql'].sort());
  }, 60_000);

  it.each(Object.keys(UPSTREAM.tables))('%s has the reference columns, in order, with the same types, nullability and defaults', async (table) => {
    const want = UPSTREAM.tables[table].columns.map((c) => ({ name: c.name, type: c.type, nullable: c.nullable, default: c.kitDefault ?? c.default }));
    expect(await columnsOf(pool, table)).toEqual(want);
  });

  it.each(Object.keys(UPSTREAM.tables))('%s has the reference indexes, and the reference CHECKs plus the Amazon CDN ban', async (table) => {
    const idx = await pool.query('SELECT indexname FROM pg_indexes WHERE schemaname = current_schema() AND tablename = $1 ORDER BY indexname', [table]);
    expect(idx.rows.map((r) => r.indexname)).toEqual([...UPSTREAM.tables[table].indexes].sort());
    const checks = await pool.query(`SELECT conname FROM pg_constraint WHERE conrelid = to_regclass($1) AND contype = 'c' ORDER BY conname`, [table]);
    const extra = table === 'editorial_articles' ? ['editorial_articles_hero_image_no_amazon_cdn'] : [];
    expect(checks.rows.map((r) => r.conname)).toEqual([...UPSTREAM.tables[table].checkConstraints, ...extra].sort());
  });

  it.each(Object.keys(UPSTREAM.views))('view %s has the reference columns', async (view) => {
    expect((await columnsOf(pool, view)).map((c) => ({ name: c.name, type: c.type }))).toEqual(UPSTREAM.views[view]);
  });

  it('every kit file is idempotent on its own: a second run succeeds and changes no catalog object', async () => {
    const before = (await pool.query(SNAPSHOT_SQL)).rows[0].snap;
    for (const f of KIT_FILES) {
      const c = await pool.connect();
      try {
        await c.query(fs.readFileSync(path.join(KIT_MIGRATIONS, f), 'utf8'));
      } finally {
        c.release();
      }
    }
    expect((await pool.query(SNAPSHOT_SQL)).rows[0].snap).toEqual(before);
  }, 60_000);

  describe('editorial_articles.hero_image_url', () => {
    const insert = (slug: string, hero: string | null) => pool.query(
      `INSERT INTO editorial_articles (slug, title, body_md, category, hero_image_url) VALUES ($1, 'T', 'B', 'guide', $2)`,
      [slug, hero],
    );
    it.each([
      [null], [''], ['/images/heroes/a.webp'], ['https://acct01.blob.core.windows.net/heroes/a.webp'], ['HTTPS://Acct.Blob.Core.Windows.Net/x.png'],
      // A file named after a brand is not an Amazon host.
      ['/images/brands/amazon.webp'],
    ])('accepts %p', async (hero) => {
      await expect(insert(`ok-${Math.random().toString(36).slice(2)}`, hero)).resolves.toBeDefined();
    });
    it.each([
      ['https://example.com/hero.jpg'],
      ['http://acct.blob.core.windows.net/x.jpg'],
      ['//evil.example/x.jpg'],
      ['/\\evil.example/x.jpg'],
      ['https://blob.core.windows.net/x.jpg'],
      ['javascript:alert(1)'],
      // Browsers drop tab, LF and CR from a URL: each of these loads from evil.example.
      ['/\t/evil.example/x.jpg'],
      ['/\n/evil.example/x.jpg'],
      ['/\r/evil.example/x.jpg'],
      ['/images/my hero.webp'],
      ['https://acct.blob.core.windows.net/x.jpg\n'],
    ])('the host allowlist rejects %j', async (hero) => {
      await expect(insert(`bad-${Math.random().toString(36).slice(2)}`, hero)).rejects.toThrow(/editorial_articles_hero_image_host_allowlist/);
    });

    const AMAZON = [
      'https://m.media-amazon.com/images/I/71abc.jpg',
      'https://images-na.ssl-images-amazon.com/images/I/x.jpg',
      '//m.media-amazon.com/images/I/x.jpg',
      'https://g-ecx.images-amazon.com/images/G/01/x.jpg',
      'https://acct.blob.core.windows.net/mirror/m.media-amazon.com/x.jpg',
      'https://ws-na.amazon-adsystem.com/widgets/q?ASIN=B000000000',
      // Amazon-owned hosts beyond the CDNs (render/amazonHosts.ts treats them as Amazon's).
      'https://images.amazon.com/images/P/B000000000.01.LZZZZZZZ.jpg',
      'https://www.amazon.com/images/I/71abc.jpg',
      'HTTPS://IMAGES.AMAZON.COM/images/P/B000000000.01.jpg',
      'https://www.amazon.co.uk/images/I/x.jpg',
      'https://images.amazon.com\\images/P/x.jpg',
      'https://ws-na.assoc-amazon.com/widgets/q?ASIN=B000000000',
      'https://amzn.to/3abcdef',
      'https://a.co/d/abcdefg',
      'https://acct.blob.core.windows.net/mirror/images.amazon.com/images/P/x.jpg',
    ];
    it('an Amazon CDN hero (or a mirror named after one) is rejected even where a site relaxed the allowlist', async () => {
      const c = await pool.connect();
      try {
        await c.query('BEGIN');
        await c.query('ALTER TABLE editorial_articles DROP CONSTRAINT editorial_articles_hero_image_host_allowlist');
        for (const [i, hero] of AMAZON.entries()) {
          await c.query(`SAVEPOINT s${i}`);
          await expect(c.query(`INSERT INTO editorial_articles (slug, title, body_md, category, hero_image_url) VALUES ($1, 'T', 'B', 'guide', $2)`, [`amz-${i}`, hero]))
            .rejects.toThrow(/editorial_articles_hero_image_no_amazon_cdn/);
          await c.query(`ROLLBACK TO SAVEPOINT s${i}`);
        }
        // ...while an S3 or other non-Amazon-retail host is not the ban's business,
        // nor is a host that merely contains the letters.
        for (const [i, other] of ['https://b.s3.amazonaws.com/x.jpg', 'https://data.co/x.jpg', 'https://notamazon.com/x.jpg', 'https://example.com/amazonia.jpg'].entries()) {
          await c.query(`INSERT INTO editorial_articles (slug, title, body_md, category, hero_image_url) VALUES ($1, 'T', 'B', 'guide', $2)`, [`other-${i}`, other]);
        }
      } finally {
        await c.query('ROLLBACK').catch(() => {});
        c.release();
      }
      for (const hero of AMAZON) {
        await expect(insert(`amz-${Math.random().toString(36).slice(2)}`, hero)).rejects.toThrow(/violates check constraint/);
      }
    });

    it('a blob mirror named after an Amazon host passes the allowlist, so the ban alone rejects it', async () => {
      for (const hero of ['https://acct.blob.core.windows.net/mirror/images.amazon.com/images/P/x.jpg', 'https://acct.blob.core.windows.net/mirror/m.media-amazon.com/x.jpg']) {
        await expect(insert(`mirror-${Math.random().toString(36).slice(2)}`, hero)).rejects.toThrow(/editorial_articles_hero_image_no_amazon_cdn/);
      }
    });
  });

  describe('updated_at tracks content edits only', () => {
    const T0 = '2026-11-01T00:00:00Z';
    beforeEach(async () => {
      await pool.query('DELETE FROM editorial_articles WHERE slug = $1', ['dated']);
      await pool.query(`INSERT INTO editorial_articles (slug, title, body_md, category, updated_at) VALUES ('dated', 'T', 'B', 'guide', $1)`, [T0]);
    });
    const updatedAt = async () => (await pool.query(`SELECT updated_at FROM editorial_articles WHERE slug = 'dated'`)).rows[0].updated_at.toISOString();

    it('a forward bump with no text change is reverted (view_count, hero swap, tags, status)', async () => {
      await pool.query(`UPDATE editorial_articles SET view_count = view_count + 1, updated_at = now() WHERE slug = 'dated'`);
      await pool.query(`UPDATE editorial_articles SET hero_image_url = '/h.webp', tags = '{lane:x}', status = 'archived', updated_at = now() WHERE slug = 'dated'`);
      expect(await updatedAt()).toBe(new Date(T0).toISOString());
    });

    it('a text change may move it forward; a backward correction is allowed', async () => {
      await pool.query(`UPDATE editorial_articles SET body_md = 'B2', updated_at = '2026-11-10T00:00:00Z' WHERE slug = 'dated'`);
      expect(await updatedAt()).toBe('2026-11-10T00:00:00.000Z');
      await pool.query(`UPDATE editorial_articles SET updated_at = '2026-11-05T00:00:00Z' WHERE slug = 'dated'`);
      expect(await updatedAt()).toBe('2026-11-05T00:00:00.000Z');
    });

    it('a deliberate bump opts in per transaction with affiliate_kit.allow_updated_at_bump', async () => {
      const c = await pool.connect();
      try {
        await c.query('BEGIN');
        await c.query(`SET LOCAL affiliate_kit.allow_updated_at_bump = 'on'`);
        await c.query(`UPDATE editorial_articles SET updated_at = '2026-11-12T00:00:00Z' WHERE slug = 'dated'`);
        await c.query('COMMIT');
      } finally {
        c.release();
      }
      expect(await updatedAt()).toBe('2026-11-12T00:00:00.000Z');
      await pool.query(`UPDATE editorial_articles SET updated_at = '2026-11-15T00:00:00Z' WHERE slug = 'dated'`);
      expect(await updatedAt()).toBe('2026-11-12T00:00:00.000Z');
    });
  });
});

// The reference deployment's DDL for the parts the kit treats differently,
// as its migrations wrote them (site names replaced; the shapes are what matter).
const REFERENCE_SHAPED_DB = `
CREATE TABLE outbound_clicks (
  id BIGSERIAL PRIMARY KEY, site_id VARCHAR(100) NOT NULL, asin VARCHAR(20) NOT NULL, tracking_id VARCHAR(100),
  source_page VARCHAR(500), source_kind VARCHAR(50), target VARCHAR(20) NOT NULL DEFAULT 'amazon', ip_address INET,
  user_agent VARCHAR(500), country VARCHAR(10), device_type VARCHAR(50), clicked_at TIMESTAMP WITH TIME ZONE DEFAULT NOW());
ALTER TABLE outbound_clicks ADD COLUMN IF NOT EXISTS is_bot BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE outbound_clicks ADD COLUMN IF NOT EXISTS bot_reason TEXT;
ALTER TABLE outbound_clicks ADD COLUMN IF NOT EXISTS classified_at TIMESTAMPTZ;
CREATE OR REPLACE VIEW outbound_clicks_human AS
SELECT * FROM (SELECT o.*, COUNT(*) OVER (PARTITION BY o.ip_address, o.clicked_at::date) AS per_ip_day
                 FROM outbound_clicks o WHERE NOT o.is_bot) t WHERE t.per_ip_day <= 20;
ALTER TABLE outbound_clicks ADD COLUMN IF NOT EXISTS via TEXT NOT NULL DEFAULT 'redirect';
ALTER TABLE outbound_clicks ADD COLUMN IF NOT EXISTS accept_language TEXT;
ALTER TABLE outbound_clicks ADD COLUMN IF NOT EXISTS session_pageviews INT;
ALTER TABLE outbound_clicks ADD COLUMN IF NOT EXISTS ms_on_page INT;
ALTER TABLE outbound_clicks ADD COLUMN IF NOT EXISTS placement TEXT;
CREATE OR REPLACE VIEW outbound_clicks_verified_human AS
SELECT t.* FROM (
  SELECT o.id, o.site_id, o.asin, o.tracking_id, o.source_page, o.source_kind, o.target, o.ip_address, o.user_agent,
         o.country, o.device_type, o.clicked_at, o.placement, o.session_pageviews, o.ms_on_page,
         COUNT(*) OVER (PARTITION BY o.ip_address, o.clicked_at::date) AS per_ip_day
    FROM outbound_clicks o WHERE o.via = 'beacon' AND NOT o.is_bot) t
 WHERE t.per_ip_day <= 20;
CREATE OR REPLACE VIEW amazon_clicks_verified_human_daily AS
SELECT (clicked_at AT TIME ZONE 'America/New_York')::date AS day, COUNT(*) AS clicks, COUNT(DISTINCT ip_address) AS people,
       COUNT(*) FILTER (WHERE source_kind = 'product') AS product_page_clicks
  FROM outbound_clicks_verified_human WHERE target = 'amazon' GROUP BY 1;
CREATE TABLE editorial_articles (
  id SERIAL PRIMARY KEY, slug VARCHAR(200) UNIQUE NOT NULL, title VARCHAR(300) NOT NULL, subtitle VARCHAR(500),
  excerpt TEXT, body_md TEXT NOT NULL, hero_image_url TEXT, category VARCHAR(50) NOT NULL,
  tags TEXT[] NOT NULL DEFAULT '{}', difficulty VARCHAR(20), estimated_read_time INT,
  related_hardware_slugs TEXT[] NOT NULL DEFAULT '{}', related_product_asins TEXT[] NOT NULL DEFAULT '{}',
  related_article_slugs TEXT[] NOT NULL DEFAULT '{}', primary_keyword VARCHAR(200), secondary_keywords TEXT[] NOT NULL DEFAULT '{}',
  author VARCHAR(100) NOT NULL DEFAULT 'Reference Editorial', author_bio TEXT,
  status VARCHAR(20) NOT NULL DEFAULT 'published', featured BOOLEAN NOT NULL DEFAULT FALSE, view_count INT NOT NULL DEFAULT 0,
  published_at TIMESTAMPTZ, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  written_at TIMESTAMPTZ DEFAULT NOW(), written_by VARCHAR(100) DEFAULT 'generator', faqs JSONB, outbound_citations TEXT[],
  trend_score NUMERIC, trend_score_updated_at TIMESTAMPTZ, trend_source VARCHAR(40), vertical VARCHAR(20),
  series_slug TEXT, series_order INTEGER,
  CONSTRAINT editorial_articles_hero_image_host_allowlist CHECK (
    hero_image_url IS NULL OR hero_image_url = '' OR hero_image_url ~ '^/'
    OR hero_image_url ~* '^https?://([a-z0-9-]+\\.)?(blob\\.core\\.windows\\.net|m\\.media-amazon\\.com)/'));
CREATE FUNCTION editorial_articles_keep_updated_at_on_non_content() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RETURN NEW; END $$;
CREATE TRIGGER editorial_articles_updated_at_content_trg BEFORE UPDATE ON editorial_articles
  FOR EACH ROW EXECUTE FUNCTION editorial_articles_keep_updated_at_on_non_content();
INSERT INTO editorial_articles (slug, title, body_md, category, hero_image_url)
  VALUES ('amazon-hero', 'T', 'B', 'guide', 'https://m.media-amazon.com/images/I/x.jpg');
`;

describePg('adopting the kit on a database that already has the reference tables (real Postgres)', () => {
  let db: ScratchDb;
  beforeAll(async () => { db = await createScratchDb('kit_adopt'); });
  afterAll(async () => { await db.drop(); });

  it('applies cleanly and keeps the existing table, constraint, trigger and rows; the views take the kit definitions in place', async () => {
    const pool = db.pool({ max: 1 });
    await pool.query(REFERENCE_SHAPED_DB);
    const constraintBefore = (await pool.query(`SELECT pg_get_constraintdef(oid) AS d FROM pg_constraint WHERE conname = 'editorial_articles_hero_image_host_allowlist'`)).rows[0].d;
    const s = await runKitMigrations(pool, { ...quiet, kitMigrationsDir: KIT_MIGRATIONS });
    expect(s.applied).toEqual(KIT_FILES);
    expect((await pool.query(`SELECT pg_get_constraintdef(oid) AS d FROM pg_constraint WHERE conname = 'editorial_articles_hero_image_host_allowlist'`)).rows[0].d).toBe(constraintBefore);
    expect((await pool.query(`SELECT count(*)::int AS n FROM pg_constraint WHERE conname = 'editorial_articles_hero_image_no_amazon_cdn'`)).rows[0].n).toBe(0);
    expect((await pool.query(`SELECT hero_image_url FROM editorial_articles WHERE slug = 'amazon-hero'`)).rows[0].hero_image_url).toMatch(/media-amazon/);
    const trg = await pool.query(`SELECT p.proname FROM pg_trigger t JOIN pg_proc p ON p.oid = t.tgfoid WHERE t.tgname = 'editorial_articles_updated_at_content_trg'`);
    expect(trg.rows).toEqual([{ proname: 'editorial_articles_keep_updated_at_on_non_content' }]);
    expect((await pool.query(`SELECT column_default FROM information_schema.columns WHERE table_name = 'editorial_articles' AND column_name = 'author'`)).rows[0].column_default)
      .toBe(`'Reference Editorial'::character varying`);
    // The views now carry the kit's verdicts, with the reference column lists.
    expect((await columnsOf(pool, 'outbound_clicks_human')).map((c) => c.name)).toEqual(UPSTREAM.views.outbound_clicks_human.map((c) => c.name));
    expect((await pool.query(`SELECT pg_get_viewdef('outbound_clicks_verified_human'::regclass) AS d`)).rows[0].d).toMatch(/outbound_clicks_verdicts/);
  }, 60_000);
});
