/**
 * The publish guard on a real Postgres (KIT_TEST_DATABASE_URL; see
 * helpers/pg.ts): rendered from the template, installed as a site migration
 * through the kit runner, and exercised as the roles a content_policy site
 * has (author = the agents role the Opus author writes with, app = the web
 * app, publisher = the publish gate's role).
 *
 * The last suite drives framework/cli/article_publish_gate.py's own
 * ArticleStore.promote() against the guard, so the contract the gate relies on
 * ("RETURNING status says trigger-kept-draft when the connection is not the
 * publisher") is checked end to end, not restated.
 */
import fs from 'fs';
import os from 'os';
import path from 'path';
import type { Pool, PoolClient } from 'pg';
import { runKitMigrations, runPendingMigrations } from '../../server/src/db/migrate';
import {
  PUBLISH_GUARD_TRIGGER,
  PUBLISH_RECHECK_TRIGGER,
  renderPublishTrigger,
  type PublishTriggerParams,
} from '../../server/src/db/publishTrigger';
import { Client } from 'pg';
import { asRole, createScratchDb, describePg, urlFor, type ScratchDb } from './helpers/pg';
import { frameworkRoot, pythonCan, runPythonJson } from './helpers/python';

const KIT_MIGRATIONS = path.resolve(__dirname, '..', '..', 'migrations');
const quiet = { log: () => {}, warn: () => {} };
const dirs: string[] = [];
afterAll(() => { for (const d of dirs) fs.rmSync(d, { recursive: true, force: true }); });

function siteDir(files: Record<string, string>): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'kit-site-migrations-'));
  dirs.push(dir);
  for (const [name, sql] of Object.entries(files)) fs.writeFileSync(path.join(dir, name), sql);
  return dir;
}

interface Site {
  db: ScratchDb;
  pool: Pool;
  publisher: string;
  author: string;
  /** The author is a LOGIN role, so a test can connect as it (SET ROLE checks the session user). */
  authorPassword: string;
  app: string;
  warnings: string[];
}

/** A scratch site database: kit migrations, the three roles, the rendered guard as site migration 0002. */
async function setUpSite(prefix: string, params: Omit<PublishTriggerParams, 'publisherRole'> = {}, extraSql = ''): Promise<Site> {
  const db = await createScratchDb(prefix);
  const pool = db.pool({ max: 3 });
  const publisher = (await db.createRole('kpub')).name;
  const authorRole = await db.createRole('kauth', { login: true });
  const author = authorRole.name;
  const app = (await db.createRole('kapp')).name;
  const warnings: string[] = [];
  const site = siteDir({
    '0001_roles.sql': `${extraSql}
      GRANT SELECT, INSERT, UPDATE ON editorial_articles TO ${author};
      GRANT USAGE ON SEQUENCE editorial_articles_id_seq TO ${author};
      GRANT SELECT, UPDATE (view_count) ON editorial_articles TO ${app};
      GRANT SELECT, UPDATE (status, published_at) ON editorial_articles TO ${publisher};`,
    '0002_publish_guard.sql': renderPublishTrigger({ ...params, publisherRole: publisher }),
  });
  await runKitMigrations(db.pool({ max: 1 }), { ...quiet, warn: (l: string) => warnings.push(l), kitMigrationsDir: KIT_MIGRATIONS, siteMigrationsDir: site });
  return { db, pool, publisher, author, authorPassword: authorRole.password, app, warnings };
}

const FAQS = [{ question: 'How long?', answer: 'About ten hours at 100 W.' }, { question: 'Cold?', answer: 'Charge above 0 C.' }];

async function insertAs(c: PoolClient, slug: string, status = 'published'): Promise<string> {
  const r = await c.query(
    `INSERT INTO editorial_articles (slug, title, subtitle, excerpt, body_md, category, faqs, outbound_citations, status)
     VALUES ($1, 'Title', 'Sub', 'Excerpt', 'Body', 'guide', $2::jsonb, ARRAY['https://example.org/a'], $3) RETURNING status`,
    [slug, JSON.stringify(FAQS), status],
  );
  return r.rows[0].status;
}

/** The publish gate's promote, exactly as framework/cli/article_publish_gate.py issues it. */
async function promote(c: PoolClient, slug: string, draftStatus = 'draft'): Promise<string | null> {
  const r = await c.query(
    `UPDATE editorial_articles SET status = $2, published_at = COALESCE(published_at, now())
      WHERE slug = $1 AND status = $3 RETURNING status`,
    [slug, 'published', draftStatus],
  );
  return r.rows[0]?.status ?? null;
}

const daysAgo = (days: number) => new Date(Date.now() - days * 86_400_000);

/**
 * The implementer's ARTICLE_AUTHOR upsert (reusable-agents agents/implementer/ARTICLE_AUTHOR.md),
 * with its own conflict clause: it asks for 'published', dates the row itself
 * (suggested_published_at, else now()), and asks for 'published' again on a conflict.
 */
async function authorUpsert(c: PoolClient, slug: string, body: string, publishedAt: Date = daysAgo(5)) {
  const r = await c.query(
    `INSERT INTO editorial_articles (slug, title, subtitle, excerpt, body_md, category, author, status, written_by,
                                     published_at, written_at, created_at, updated_at)
     VALUES ($1, 'Title', 'Sub', 'Excerpt', $2, 'guide', 'Editorial', 'published', 'claude-cli', $3, now(), now(), now())
     ON CONFLICT (slug) DO UPDATE SET title = EXCLUDED.title, body_md = EXCLUDED.body_md, excerpt = EXCLUDED.excerpt,
       updated_at = now(), written_at = now(), status = 'published'
     RETURNING status, published_at`,
    [slug, body, publishedAt],
  );
  return r.rows[0] as { status: string; published_at: Date | null };
}

async function statusOf(pool: Pool, slug: string): Promise<string> {
  return (await pool.query('SELECT status FROM editorial_articles WHERE slug = $1', [slug])).rows[0]?.status;
}

async function publishedAtOf(pool: Pool, slug: string): Promise<Date | null> {
  return (await pool.query('SELECT published_at FROM editorial_articles WHERE slug = $1', [slug])).rows[0]?.published_at ?? null;
}

/** Was the row's published_at stamped within the last minute (by the database clock)? */
async function stampedJustNow(pool: Pool, slug: string): Promise<boolean> {
  return (await pool.query(
    `SELECT published_at BETWEEN now() - interval '1 minute' AND now() AS fresh FROM editorial_articles WHERE slug = $1`, [slug],
  )).rows[0]?.fresh === true;
}

/** A session logged in as `role` itself (not SET ROLE), as a role an operator granted something to would connect. */
async function holderSession(st: Site, role: { name: string; password: string }): Promise<Client> {
  const c = new Client({ connectionString: urlFor(st.db.name, { user: role.name, password: role.password }) });
  await c.connect();
  return c;
}

/** Wait until the trigger is gone: a session's temporary objects are dropped as its backend exits, after the client has left. */
async function waitForTriggerGone(st: Site, name: string, timeoutMs = 10_000): Promise<void> {
  const until = Date.now() + timeoutMs;
  for (;;) {
    const r = await st.pool.query(
      `SELECT count(*)::int AS n FROM pg_trigger t JOIN pg_proc p ON p.oid = t.tgfoid
        WHERE t.tgrelid = 'editorial_articles'::regclass AND t.tgname = $1 AND p.pronamespace::regnamespace::text LIKE 'pg_temp%'`,
      [name],
    );
    if (r.rows[0].n === 0) return;
    if (Date.now() > until) throw new Error(`trigger ${name} still runs a temporary function after ${timeoutMs} ms`);
    await new Promise((res) => setTimeout(res, 50));
  }
}

/** Apply the rendered guard again (it is idempotent) and return the server notices it raised. */
async function reinstallNotices(st: Site, params: Omit<PublishTriggerParams, 'publisherRole'> = {}): Promise<string[]> {
  const c = await st.pool.connect();
  const notices: string[] = [];
  const onNotice = (n: { message?: string }) => notices.push(String(n.message));
  c.on('notice', onNotice);
  try {
    await c.query(renderPublishTrigger({ ...params, publisherRole: st.publisher }));
  } finally {
    c.removeListener('notice', onNotice);
    c.release();
  }
  return notices;
}

describePg('publish guard: the rules (real Postgres)', () => {
  let s: Site;
  let n = 0;
  const slug = () => `article-${++n}`;
  beforeAll(async () => { s = await setUpSite('kit_guard'); }, 60_000);
  afterAll(async () => { await s.db.drop(); });

  /** A published row: the author writes it (a draft), the publisher promotes it. */
  async function published(): Promise<string> {
    const sl = slug();
    await asRole(s.pool, s.author, (c) => insertAs(c, sl));
    expect(await asRole(s.pool, s.publisher, (c) => promote(c, sl))).toBe('published');
    return sl;
  }

  it('installs through the runner without warnings when the role exists', () => {
    expect(s.warnings).toEqual([]);
  });

  it("the author's INSERT is always a draft, whatever status it asked for", async () => {
    for (const asked of ['published', 'archived', 'draft', 'live']) {
      expect(await asRole(s.pool, s.author, (c) => insertAs(c, slug(), asked))).toBe('draft');
    }
  });

  it("an author's upsert (ARTICLE_AUTHOR's INSERT ... ON CONFLICT DO UPDATE ... status = 'published') stays a draft", async () => {
    const sl = slug();
    const upsert = (c: PoolClient, body: string) => c.query(
      `INSERT INTO editorial_articles (slug, title, body_md, category, status) VALUES ($1, 'Title', $2, 'guide', 'published')
       ON CONFLICT (slug) DO UPDATE SET body_md = EXCLUDED.body_md, updated_at = now(), status = 'published' RETURNING status`,
      [sl, body],
    );
    expect((await asRole(s.pool, s.author, (c) => upsert(c, 'v1'))).rows[0].status).toBe('draft');
    expect((await asRole(s.pool, s.author, (c) => upsert(c, 'v2'))).rows[0].status).toBe('draft');
  });

  it('only the publisher promotes: the same UPDATE as the author returns draft (trigger-kept-draft)', async () => {
    const sl = slug();
    await asRole(s.pool, s.author, (c) => insertAs(c, sl));
    expect(await asRole(s.pool, s.author, (c) => c.query(`UPDATE editorial_articles SET status = 'published' WHERE slug = $1 RETURNING status`, [sl]))
      .then((r) => r.rows[0].status)).toBe('draft');
    expect(await statusOf(s.pool, sl)).toBe('draft');
    expect(await asRole(s.pool, s.publisher, (c) => promote(c, sl))).toBe('published');
    expect(await statusOf(s.pool, sl)).toBe('published');
  });

  it('housekeeping writes keep a published article published (view_count per page view, hero, tags, featured, trend, links)', async () => {
    const sl = await published();
    await asRole(s.pool, s.app, (c) => c.query('UPDATE editorial_articles SET view_count = view_count + 1 WHERE slug = $1', [sl]));
    await asRole(s.pool, s.author, (c) => c.query(
      `UPDATE editorial_articles SET hero_image_url = '/h.webp', tags = '{lane:x}', featured = true, trend_score = 4.2,
              related_article_slugs = '{a,b}', related_product_asins = '{B000000001}', series_slug = 's', updated_at = now()
        WHERE slug = $1`, [sl]));
    expect(await statusOf(s.pool, sl)).toBe('published');
  });

  it.each([
    ['title', `title = 'New title'`],
    ['subtitle', `subtitle = NULL`],
    ['excerpt', `excerpt = 'Backfeed through the dryer outlet.'`],
    ['body_md', `body_md = body_md || ' more'`],
    ['faqs', `faqs = '[{"question":"CPAP?","answer":"20 hours."}]'::jsonb`],
    ['outbound_citations', `outbound_citations = '{}'`],
    // category picks the gate's per-bucket claims and link policies: re-bucketing is a content change.
    ['category', `category = 'deal'`],
  ])('an edit to %s of a published article sends it back to draft', async (_col, set) => {
    const sl = await published();
    const r = await asRole(s.pool, s.author, (c) => c.query(`UPDATE editorial_articles SET ${set} WHERE slug = $1 RETURNING status`, [sl]));
    expect(r.rows[0].status).toBe('draft');
  });

  it('a semantically equal faqs rewrite (key order, spacing) is not a content change', async () => {
    const sl = await published();
    const reordered = JSON.stringify(FAQS.map((f) => ({ answer: f.answer, question: f.question })), null, 2);
    await asRole(s.pool, s.author, (c) => c.query('UPDATE editorial_articles SET faqs = $2::jsonb WHERE slug = $1', [sl, reordered]));
    expect(await statusOf(s.pool, sl)).toBe('published');
  });

  it('an upsert that repeats the published text keeps it published; one that changes it drafts it', async () => {
    const sl = await published();
    const upsert = (c: PoolClient, body: string) => c.query(
      `INSERT INTO editorial_articles (slug, title, body_md, category, status) VALUES ($1, 'Title', $2, 'guide', 'published')
       ON CONFLICT (slug) DO UPDATE SET title = EXCLUDED.title, body_md = EXCLUDED.body_md, status = 'published' RETURNING status`,
      [sl, body],
    );
    expect((await asRole(s.pool, s.author, (c) => upsert(c, 'Body'))).rows[0].status).toBe('published');
    expect((await asRole(s.pool, s.author, (c) => upsert(c, 'Body, revised'))).rows[0].status).toBe('draft');
  });

  it('leaving the published state is always allowed; coming back needs the publisher', async () => {
    const sl = await published();
    expect((await asRole(s.pool, s.author, (c) => c.query(`UPDATE editorial_articles SET status = 'archived' WHERE slug = $1 RETURNING status`, [sl]))).rows[0].status).toBe('archived');
    // A hold stays a hold: asking for 'published' keeps 'archived'; only an explicit draft status resubmits it.
    expect((await asRole(s.pool, s.author, (c) => c.query(`UPDATE editorial_articles SET status = 'published' WHERE slug = $1 RETURNING status`, [sl]))).rows[0].status).toBe('archived');
    expect(await asRole(s.pool, s.publisher, (c) => promote(c, sl))).toBeNull();
    expect((await asRole(s.pool, s.author, (c) => c.query(`UPDATE editorial_articles SET status = 'draft' WHERE slug = $1 RETURNING status`, [sl]))).rows[0].status).toBe('draft');
    expect(await asRole(s.pool, s.publisher, (c) => promote(c, sl))).toBe('published');
    const sl2 = await published();
    expect((await asRole(s.pool, s.author, (c) => c.query(`UPDATE editorial_articles SET status = 'draft' WHERE slug = $1 RETURNING status`, [sl2]))).rows[0].status).toBe('draft');
  });

  it("an author's INSERT lands with published_at NULL, whatever date it wrote; the publisher's promote stamps the go-live time", async () => {
    const sl = slug();
    const r = await asRole(s.pool, s.author, (c) => c.query(
      `INSERT INTO editorial_articles (slug, title, body_md, category, status, published_at)
       VALUES ($1, 'Title', 'Body', 'guide', 'published', $2) RETURNING status, published_at`, [sl, daysAgo(5)]));
    expect(r.rows[0]).toEqual({ status: 'draft', published_at: null });
    // ARTICLE_AUTHOR's upsert on its insert path, dated by the author (suggested_published_at)
    const sl2 = slug();
    expect(await asRole(s.pool, s.author, (c) => authorUpsert(c, sl2, 'Body', daysAgo(5)))).toEqual({ status: 'draft', published_at: null });
    for (const x of [sl, sl2]) {
      expect(await asRole(s.pool, s.publisher, (c) => promote(c, x))).toBe('published');
      expect(await stampedJustNow(s.pool, x)).toBe(true);   // the go-live time, not the 5-day-old date
    }
  });

  it('published_at stays the first go-live time: an edit, an upsert, a takedown and an EXCLUDED.published_at copy all keep it', async () => {
    const sl = await published();
    // A dating UPDATE by another role (the scaffold's fixture freshen.sql): published_at is not content.
    expect((await s.pool.query(`UPDATE editorial_articles SET published_at = now() - interval '10 days'
      WHERE slug = $1 AND status = 'published' RETURNING status`, [sl])).rows[0].status).toBe('published');
    const first = await publishedAtOf(s.pool, sl);
    expect(Date.now() - first!.getTime()).toBeGreaterThan(9 * 86_400_000);
    // an edit sends it back to the gate; the re-promote keeps the first date
    await asRole(s.pool, s.author, (c) => c.query(`UPDATE editorial_articles SET body_md = 'Body, edited' WHERE slug = $1`, [sl]));
    expect(await asRole(s.pool, s.publisher, (c) => promote(c, sl))).toBe('published');
    expect(await publishedAtOf(s.pool, sl)).toEqual(first);
    // ARTICLE_AUTHOR's upsert re-drafts it on its conflict path (which never writes published_at)
    expect((await asRole(s.pool, s.author, (c) => authorUpsert(c, sl, 'Body, regenerated', daysAgo(1)))).status).toBe('draft');
    expect(await asRole(s.pool, s.publisher, (c) => promote(c, sl))).toBe('published');
    expect(await publishedAtOf(s.pool, sl)).toEqual(first);
    // a takedown, then an explicit resubmit
    await asRole(s.pool, s.author, (c) => c.query(`UPDATE editorial_articles SET status = 'archived' WHERE slug = $1`, [sl]));
    await asRole(s.pool, s.author, (c) => c.query(`UPDATE editorial_articles SET status = 'draft' WHERE slug = $1`, [sl]));
    expect(await asRole(s.pool, s.publisher, (c) => promote(c, sl))).toBe('published');
    expect(await publishedAtOf(s.pool, sl)).toEqual(first);
    // An improvised upsert that copies EXCLUDED.published_at (the NULL the INSERT trigger gave it) cannot clear it.
    const r = await asRole(s.pool, s.author, (c) => c.query(
      `INSERT INTO editorial_articles (slug, title, body_md, category, status, published_at)
       VALUES ($1, 'Title', 'Body, regenerated', 'guide', 'published', now())
       ON CONFLICT (slug) DO UPDATE SET body_md = EXCLUDED.body_md, published_at = EXCLUDED.published_at, status = 'published'
       RETURNING status, published_at`, [sl]));
    expect(r.rows[0]).toEqual({ status: 'published', published_at: first });
    await asRole(s.pool, s.author, (c) => c.query(`UPDATE editorial_articles SET status = 'draft', published_at = NULL WHERE slug = $1`, [sl]));
    expect(await publishedAtOf(s.pool, sl)).toEqual(first);
  });

  it('every other role is guarded too, including the table owner and a superuser', async () => {
    const sl = await published();
    expect((await s.pool.query(`UPDATE editorial_articles SET body_md = 'owner edit' WHERE slug = $1 RETURNING status`, [sl])).rows[0].status).toBe('draft');
    expect((await s.pool.query(`INSERT INTO editorial_articles (slug, title, body_md, category, status) VALUES ($1, 'T', 'B', 'guide', 'published') RETURNING status`, [slug()])).rows[0].status).toBe('draft');
  });

  it('the author, logged in as itself, cannot become the publisher', async () => {
    const c = new Client({ connectionString: urlFor(s.db.name, { user: s.author, password: s.authorPassword }) });
    await c.connect();
    try {
      expect((await c.query('SELECT current_user AS u')).rows[0].u).toBe(s.author);
      await expect(c.query(`SET ROLE ${s.publisher}`)).rejects.toThrow(/permission denied/);
      const r = await c.query(`INSERT INTO editorial_articles (slug, title, body_md, category, status) VALUES ($1, 'T', 'B', 'guide', 'published') RETURNING status`, [slug()]);
      expect(r.rows[0].status).toBe('draft');
    } finally {
      await c.end();
    }
  });

  it('the publisher writes pass unchanged', async () => {
    const sl = slug();
    await asRole(s.pool, s.author, (c) => insertAs(c, sl));
    expect((await asRole(s.pool, s.publisher, (c) => c.query(`UPDATE editorial_articles SET status = 'published' WHERE slug = $1 RETURNING status`, [sl]))).rows[0].status).toBe('published');
  });

  it('re-applying the rendered migration changes nothing, and both triggers stay ENABLE ALWAYS', async () => {
    const def = async () => (await s.pool.query(`SELECT
        md5(pg_get_functiondef('editorial_articles_publish_guard'::regproc)) AS guard,
        md5(pg_get_functiondef('editorial_articles_publish_recheck'::regproc)) AS recheck,
        (SELECT jsonb_agg(jsonb_build_array(tgname, tgenabled, pg_get_triggerdef(oid)) ORDER BY tgname)
           FROM pg_trigger WHERE tgrelid = 'editorial_articles'::regclass AND NOT tgisinternal) AS triggers`)).rows[0];
    const before = await def();
    expect(before.triggers.filter((t: string[]) => t[0].startsWith('editorial_articles_zzz_publish_')).map((t: string[]) => t.slice(0, 2)))
      .toEqual([[PUBLISH_GUARD_TRIGGER, 'A'], [PUBLISH_RECHECK_TRIGGER, 'A']]);
    expect(await reinstallNotices(s)).toEqual([]);
    expect(await def()).toEqual(before);
  });

  it('the recheck is a NOT DEFERRABLE constraint trigger; the guard is a plain BEFORE trigger', async () => {
    const r = await s.pool.query(`SELECT t.tgname, t.tgconstraint <> 0 AS constraint_trigger, pg_get_triggerdef(t.oid) AS def,
        (SELECT c.condeferrable FROM pg_constraint c WHERE c.oid = t.tgconstraint) AS deferrable
       FROM pg_trigger t WHERE t.tgrelid = 'editorial_articles'::regclass AND t.tgname LIKE 'editorial_articles_zzz_publish_%' ORDER BY t.tgname`);
    expect(r.rows.map((x) => [x.tgname, x.constraint_trigger, x.deferrable])).toEqual([
      [PUBLISH_GUARD_TRIGGER, false, null],
      [PUBLISH_RECHECK_TRIGGER, true, false],
    ]);
    expect(r.rows[1].def).toMatch(/^CREATE CONSTRAINT TRIGGER editorial_articles_zzz_publish_recheck_trg AFTER INSERT OR UPDATE ON \S*editorial_articles NOT DEFERRABLE INITIALLY IMMEDIATE FOR EACH ROW WHEN/);
  });
});

describePg('publish guard: installation and configuration (real Postgres)', () => {
  const sites: Site[] = [];
  afterAll(async () => { for (const st of sites) await st.db.drop(); });

  it('fires after every other BEFORE trigger, so a content change made by one is still caught', async () => {
    const st = await setUpSite('kit_guard_order', {}, `
      CREATE FUNCTION normalize_body() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF NEW.tags IS DISTINCT FROM OLD.tags THEN NEW.body_md := NEW.body_md || ' (normalized)'; END IF;
        RETURN NEW;
      END $$;
      CREATE TRIGGER editorial_articles_zz_normalize_trg BEFORE UPDATE ON editorial_articles
        FOR EACH ROW EXECUTE FUNCTION normalize_body();`);
    sites.push(st);
    await asRole(st.pool, st.author, (c) => insertAs(c, 'a'));
    await asRole(st.pool, st.publisher, (c) => promote(c, 'a'));
    const r = await asRole(st.pool, st.author, (c) => c.query(`UPDATE editorial_articles SET tags = '{x}' WHERE slug = 'a' RETURNING status, body_md`));
    expect(r.rows[0]).toEqual({ status: 'draft', body_md: 'Body (normalized)' });
  }, 60_000);

  it('follows custom statuses and content columns', async () => {
    const st = await setUpSite('kit_guard_custom', { draftStatus: 'pending', publishedStatus: 'live', contentColumns: ['body_md'] });
    sites.push(st);
    expect(await asRole(st.pool, st.author, (c) => insertAs(c, 'a', 'live'))).toBe('pending');
    await asRole(st.pool, st.publisher, (c) => c.query(`UPDATE editorial_articles SET status = 'live' WHERE slug = 'a'`));
    await asRole(st.pool, st.author, (c) => c.query(`UPDATE editorial_articles SET title = 'not a content column here' WHERE slug = 'a'`));
    expect(await statusOf(st.pool, 'a')).toBe('live');
    await asRole(st.pool, st.author, (c) => c.query(`UPDATE editorial_articles SET body_md = 'changed' WHERE slug = 'a'`));
    expect(await statusOf(st.pool, 'a')).toBe('pending');
  }, 60_000);

  it("rendered for the gate's 'pending' queue: a queued row stays queued, and a hold (draft, archived) stays a hold until it is resubmitted", async () => {
    const st = await setUpSite('kit_guard_holds', { draftStatus: 'pending' });
    sites.push(st);
    const ask = (slug: string, status: string) => asRole(st.pool, st.author, (c) =>
      c.query(`UPDATE editorial_articles SET status = $2 WHERE slug = $1 RETURNING status`, [slug, status])).then((r) => r.rows[0].status);
    expect(await asRole(st.pool, st.author, (c) => insertAs(c, 'a'))).toBe('pending');
    expect(await ask('a', 'published')).toBe('pending');
    expect(await asRole(st.pool, st.publisher, (c) => promote(c, 'a', 'pending'))).toBe('published');
    // A takedown to 'draft' is a hold. Asking for 'published', directly or through ARTICLE_AUTHOR's
    // upsert, keeps it a hold (the upsert's new text still lands), and the gate never selects it.
    expect(await ask('a', 'draft')).toBe('draft');
    expect(await ask('a', 'published')).toBe('draft');
    expect((await asRole(st.pool, st.author, (c) => authorUpsert(c, 'a', 'Body, regenerated'))).status).toBe('draft');
    expect(await asRole(st.pool, st.publisher, (c) => promote(c, 'a', 'pending'))).toBeNull();
    expect((await st.pool.query(`SELECT status, body_md FROM editorial_articles WHERE slug = 'a'`)).rows[0])
      .toEqual({ status: 'draft', body_md: 'Body, regenerated' });
    // 'archived' too
    expect(await ask('a', 'archived')).toBe('archived');
    expect(await ask('a', 'published')).toBe('archived');
    // Only an explicit 'pending' resubmits it to the gate.
    expect(await ask('a', 'pending')).toBe('pending');
    expect(await asRole(st.pool, st.publisher, (c) => promote(c, 'a', 'pending'))).toBe('published');
    // An upsert that writes status = EXCLUDED.status resubmits a hold: EXCLUDED carries the 'pending' the
    // INSERT trigger forced, which the guard cannot tell from a deliberate resubmit. That is why an
    // author's upsert leaves status out of its DO UPDATE (migrations/README.md, "The publish guard").
    expect(await ask('a', 'draft')).toBe('draft');
    expect((await asRole(st.pool, st.author, (c) => c.query(
      `INSERT INTO editorial_articles (slug, title, body_md, category, status) VALUES ('a', 'Title', 'Body, again', 'guide', 'published')
       ON CONFLICT (slug) DO UPDATE SET body_md = EXCLUDED.body_md, status = EXCLUDED.status RETURNING status`))).rows[0].status)
      .toBe('pending');
  }, 60_000);

  it('a content column dropped after installation makes guarded writes fail, never unpublish, until the guard is re-rendered', async () => {
    const st = await setUpSite('kit_guard_drop', { contentColumns: ['title', 'body_md', 'quick_answer'] },
      'ALTER TABLE editorial_articles ADD COLUMN quick_answer TEXT;');
    sites.push(st);
    await asRole(st.pool, st.author, (c) => insertAs(c, 'a'));
    await asRole(st.pool, st.publisher, (c) => promote(c, 'a'));
    const bump = (slug: string) => asRole(st.pool, st.app, (c) => c.query('UPDATE editorial_articles SET view_count = view_count + 1 WHERE slug = $1', [slug]));
    // Warm every pooled session on the guard first: nothing it cached may outlive the DROP.
    await Promise.all([bump('a'), bump('a'), bump('a')]);
    expect(await statusOf(st.pool, 'a')).toBe('published');
    await st.pool.query('ALTER TABLE editorial_articles DROP COLUMN quick_answer');
    for (let i = 0; i < 3; i++) await expect(bump('a')).rejects.toThrow(/column "quick_answer" not found/);
    await expect(asRole(st.pool, st.author, (c) => c.query(`UPDATE editorial_articles SET body_md = 'edited' WHERE slug = 'a'`)))
      .rejects.toThrow(/column "quick_answer" not found/);
    // Nothing was unpublished or changed; an INSERT is still a draft, a takedown still works,
    // and nothing can be published (the recheck compares the publisher's write too).
    expect((await st.pool.query(`SELECT status, body_md, view_count FROM editorial_articles WHERE slug = 'a'`)).rows[0])
      .toEqual({ status: 'published', body_md: 'Body', view_count: 3 });
    expect(await asRole(st.pool, st.author, (c) => insertAs(c, 'b'))).toBe('draft');
    await expect(asRole(st.pool, st.publisher, (c) => promote(c, 'b'))).rejects.toThrow(/column "quick_answer" not found/);
    expect(await statusOf(st.pool, 'b')).toBe('draft');
    // Re-rendered without the column (the migration that drops it should do this), all is well again.
    await st.pool.query(renderPublishTrigger({ publisherRole: st.publisher, contentColumns: ['title', 'body_md'] }));
    expect(await asRole(st.pool, st.publisher, (c) => promote(c, 'b'))).toBe('published');
    await bump('b');
    expect(await statusOf(st.pool, 'b')).toBe('published');
    expect((await asRole(st.pool, st.author, (c) => c.query(`UPDATE editorial_articles SET status = 'archived' WHERE slug = 'a' RETURNING status`))).rows[0].status)
      .toBe('archived');
  }, 60_000);

  it('a BEFORE trigger that sorts after the guard is named at install, and a write it turns into a rule break fails (author or publisher)', async () => {
    const st = await setUpSite('kit_guard_late', {}, `
      CREATE FUNCTION append_on_tags() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF NEW.tags IS DISTINCT FROM OLD.tags THEN NEW.body_md := NEW.body_md || ' [appended after the guard]'; END IF;
        RETURN NEW;
      END $$;
      CREATE TRIGGER set_body_trg BEFORE UPDATE ON editorial_articles FOR EACH ROW EXECUTE FUNCTION append_on_tags();
      CREATE FUNCTION swap_on_publish() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF NEW.status IS DISTINCT FROM OLD.status AND NEW.title = 'swap me' THEN NEW.body_md := 'Backfeed through the dryer outlet.'; END IF;
        RETURN NEW;
      END $$;
      CREATE TRIGGER trg_swap_body BEFORE UPDATE ON editorial_articles FOR EACH ROW EXECUTE FUNCTION swap_on_publish();`);
    sites.push(st);
    expect(st.warnings).toEqual([
      expect.stringMatching(/WARNING: publish guard: BEFORE trigger\(s\) set_body_trg, trg_swap_body fire after editorial_articles_zzz_publish_guard_trg/),
    ]);
    // The author: a tags update that the later trigger turns into a body edit of a published row.
    await asRole(st.pool, st.author, (c) => insertAs(c, 'a'));
    expect(await asRole(st.pool, st.publisher, (c) => promote(c, 'a'))).toBe('published');
    await expect(asRole(st.pool, st.author, (c) => c.query(`UPDATE editorial_articles SET tags = '{x}' WHERE slug = 'a'`)))
      .rejects.toThrow(/publish guard: editorial_articles row a was edited by \S+ and left published/);
    expect((await st.pool.query(`SELECT status, body_md, tags FROM editorial_articles WHERE slug = 'a'`)).rows[0])
      .toEqual({ status: 'published', body_md: 'Body', tags: [] });
    // The publisher: a promote during which a later trigger swaps the checked body.
    await asRole(st.pool, st.author, (c) => c.query(`INSERT INTO editorial_articles (slug, title, body_md, category) VALUES ('b', 'swap me', 'Checked body', 'guide')`));
    await expect(asRole(st.pool, st.publisher, (c) => promote(c, 'b')))
      .rejects.toThrow(/publish guard: editorial_articles row b was changed in a write by \S+ that left it published/);
    expect((await st.pool.query(`SELECT status, body_md FROM editorial_articles WHERE slug = 'b'`)).rows[0])
      .toEqual({ status: 'draft', body_md: 'Checked body' });
  }, 60_000);

  it('a role holding TRIGGER (GRANT ALL) is named at install, and its own writes cannot publish through a trigger of its own', async () => {
    const st = await setUpSite('kit_guard_trigpriv');
    sites.push(st);
    const holder = await st.db.createRole('kall', { login: true });
    await st.pool.query(`GRANT ALL ON editorial_articles TO ${holder.name}; GRANT USAGE ON SEQUENCE editorial_articles_id_seq TO ${holder.name}`);
    expect(await reinstallNotices(st)).toEqual([
      expect.stringMatching(new RegExp(`^publish guard: role\\(s\\) ${holder.name} can create triggers on editorial_articles`)),
    ]);
    await asRole(st.pool, st.author, (c) => insertAs(c, 'held'));
    // Logged in as itself: a temp function needs no CREATE on any schema, and the
    // trigger vanishes with the session, so nothing would be left to find.
    const c = await holderSession(st, holder);
    try {
      await c.query(`CREATE FUNCTION pg_temp.republish() RETURNS trigger LANGUAGE plpgsql AS $f$ BEGIN NEW.status := 'published'; RETURN NEW; END $f$`);
      await c.query('CREATE TRIGGER zzzz_republish BEFORE INSERT OR UPDATE ON editorial_articles FOR EACH ROW EXECUTE FUNCTION pg_temp.republish()');
      await expect(c.query(`INSERT INTO editorial_articles (slug, title, body_md, category, status) VALUES ('self', 'T', 'Backfeed through the dryer outlet.', 'guide', 'published')`))
        .rejects.toThrow(new RegExp(`publish guard: editorial_articles row self was inserted as published by ${holder.name}`));
      await expect(c.query(`UPDATE editorial_articles SET status = 'published' WHERE slug = 'held'`))
        .rejects.toThrow(new RegExp(`publish guard: editorial_articles row held was made published by ${holder.name}`));
      // A table owner's tools stay out of reach: TRIGGER cannot drop, rename or disable the
      // recheck, and CREATE OR REPLACE TRIGGER (TRIGGER alone suffices) cannot swap a constraint trigger.
      await expect(c.query(`ALTER TABLE editorial_articles DISABLE TRIGGER ${PUBLISH_RECHECK_TRIGGER}`)).rejects.toThrow(/must be owner/);
      await expect(c.query(`DROP TRIGGER ${PUBLISH_RECHECK_TRIGGER} ON editorial_articles`)).rejects.toThrow(/must be owner/);
      await expect(c.query(`ALTER TRIGGER ${PUBLISH_RECHECK_TRIGGER} ON editorial_articles RENAME TO x`)).rejects.toThrow(/must be owner/);
      await expect(c.query(`CREATE OR REPLACE TRIGGER ${PUBLISH_RECHECK_TRIGGER} AFTER INSERT OR UPDATE ON editorial_articles
          FOR EACH ROW EXECUTE FUNCTION pg_temp.republish()`)).rejects.toThrow(/is a constraint trigger/);
    } finally {
      await c.end();
    }
    expect(await statusOf(st.pool, 'held')).toBe('draft');
    expect((await st.pool.query(`SELECT count(*)::int AS n FROM editorial_articles WHERE slug = 'self'`)).rows[0].n).toBe(0);
  }, 60_000);

  it('code a TRIGGER holder plants to run as the publisher publishes nothing: every publish fails while it exists', async () => {
    const st = await setUpSite('kit_guard_plant');
    sites.push(st);
    const holder = await st.db.createRole('kplant', { login: true });
    await st.pool.query(`GRANT ALL ON editorial_articles TO ${holder.name}`);
    for (const sl of ['held', 'other']) await asRole(st.pool, st.author, (c) => insertAs(c, sl));
    const c = await holderSession(st, holder);
    try {
      // A statement trigger that sorts first. While the publisher writes, it shadows the table
      // in the publisher's session (the gate's next promote would hit the view) and publishes a
      // draft the gate never checked; the nested write passes the guard as the publisher.
      const plant = (catchNested: boolean) => `CREATE OR REPLACE FUNCTION pg_temp.plant() RETURNS trigger LANGUAGE plpgsql AS $f$
        BEGIN
          IF current_user = '${st.publisher}' AND pg_trigger_depth() = 1 THEN
            CREATE TEMP VIEW editorial_articles AS SELECT * FROM public.editorial_articles;
            ${catchNested
    ? `BEGIN UPDATE public.editorial_articles SET status = 'published' WHERE slug = 'held'; EXCEPTION WHEN OTHERS THEN NULL; END;`
    : `UPDATE public.editorial_articles SET status = 'published' WHERE slug = 'held';`}
          END IF;
          RETURN NULL;
        END $f$`;
      await c.query(plant(false));
      await c.query('CREATE TRIGGER aaa_plant BEFORE UPDATE ON editorial_articles FOR EACH STATEMENT EXECUTE FUNCTION pg_temp.plant()');
      for (const catchNested of [false, true]) {
        await c.query(plant(catchNested));
        await asRole(st.pool, st.publisher, async (p) => {
          await expect(promote(p, 'other'))
            .rejects.toThrow(new RegExp(`publish guard: editorial_articles row (held|other) was published by ${st.publisher} while trigger\\(s\\) aaa_plant \\(pg_temp_\\d+\\.plant\\(\\), owned by ${holder.name}\\) run a function that neither the table owner \\(\\S+\\) nor a superuser owns`));
          expect((await p.query(`SELECT to_regclass('pg_temp.editorial_articles') AS v`)).rows[0].v).toBeNull();
        });
        expect([await statusOf(st.pool, 'held'), await statusOf(st.pool, 'other')]).toEqual(['draft', 'draft']);
      }
      // The error says how to resume.
      const err = await asRole(st.pool, st.publisher, (p) => promote(p, 'other').then(() => null, (e: { hint?: string }) => e));
      expect(err?.hint).toMatch(/ALTER FUNCTION \.\.\. OWNER TO the table owner.*otherwise drop the trigger and revoke TRIGGER/);
    } finally {
      await c.end();
    }
    // The plant goes with the holder's session; publishing resumes.
    await waitForTriggerGone(st, 'aaa_plant');
    expect(await asRole(st.pool, st.publisher, (p) => promote(p, 'other'))).toBe('published');
    expect(await statusOf(st.pool, 'held')).toBe('draft');
  }, 60_000);

  it('a TRIGGER holder that swaps the BEFORE guard publishes nothing either; its session ends and the guard must be re-applied', async () => {
    const st = await setUpSite('kit_guard_swap');
    sites.push(st);
    const holder = await st.db.createRole('kswap', { login: true });
    await st.pool.query(`GRANT ALL ON editorial_articles TO ${holder.name}; GRANT USAGE ON SEQUENCE editorial_articles_id_seq TO ${holder.name}`);
    for (const sl of ['held', 'other']) await asRole(st.pool, st.author, (c) => insertAs(c, sl));
    const c = await holderSession(st, holder);
    try {
      await c.query(`CREATE FUNCTION pg_temp.noguard() RETURNS trigger LANGUAGE plpgsql AS $f$
        BEGIN
          IF current_user = '${st.publisher}' AND pg_trigger_depth() = 1 THEN
            UPDATE public.editorial_articles SET status = 'published' WHERE slug = 'held';
          END IF;
          RETURN NEW;
        END $f$`);
      // CREATE OR REPLACE TRIGGER needs only the TRIGGER privilege: the plain guard can be swapped.
      await c.query(`CREATE OR REPLACE TRIGGER ${PUBLISH_GUARD_TRIGGER} BEFORE INSERT OR UPDATE ON editorial_articles
          FOR EACH ROW EXECUTE FUNCTION pg_temp.noguard()`);
      // Its own writes still cannot publish: the recheck judges the row as written.
      await expect(c.query(`UPDATE editorial_articles SET status = 'published' WHERE slug = 'held'`))
        .rejects.toThrow(new RegExp(`row held was made published by ${holder.name}`));
      await expect(c.query(`INSERT INTO editorial_articles (slug, title, body_md, category, status) VALUES ('self', 'T', 'B', 'guide', 'published')`))
        .rejects.toThrow(new RegExp(`row self was inserted as published by ${holder.name}`));
      // And the publisher's promote fails, naming the swapped guard.
      await expect(asRole(st.pool, st.publisher, (p) => promote(p, 'other')))
        .rejects.toThrow(new RegExp(`while trigger\\(s\\) ${PUBLISH_GUARD_TRIGGER} \\(pg_temp_\\d+\\.noguard\\(\\), owned by ${holder.name}\\)`));
      expect([await statusOf(st.pool, 'held'), await statusOf(st.pool, 'other')]).toEqual(['draft', 'draft']);
    } finally {
      await c.end();
    }
    // The swapped guard went with the session: writes the guard would have drafted now fail
    // at the recheck (a denial of service, not a publish) until the migration is applied again.
    await waitForTriggerGone(st, PUBLISH_GUARD_TRIGGER);
    await expect(asRole(st.pool, st.author, (a) => insertAs(a, 'after'))).rejects.toThrow(/row after was inserted as published by/);
    await st.pool.query(renderPublishTrigger({ publisherRole: st.publisher }));
    expect(await asRole(st.pool, st.author, (a) => insertAs(a, 'after'))).toBe('draft');
    expect(await asRole(st.pool, st.publisher, (p) => promote(p, 'other'))).toBe('published');
  }, 60_000);

  it('triggers run by functions of the owner, a member of the owner or a superuser leave publishing alone; any other owner blocks it', async () => {
    const st = await setUpSite('kit_guard_owners');
    sites.push(st);
    const owner = (await st.db.createRole('kown')).name;
    const admin = (await st.db.createRole('kadm')).name;
    await st.pool.query(`
      ALTER TABLE editorial_articles OWNER TO ${owner};
      GRANT ${owner} TO ${admin};
      CREATE FUNCTION site_touch() RETURNS trigger LANGUAGE plpgsql AS $f$ BEGIN RETURN NEW; END $f$;
      ALTER FUNCTION site_touch() OWNER TO ${owner};
      CREATE FUNCTION admin_touch() RETURNS trigger LANGUAGE plpgsql AS $f$ BEGIN RETURN NEW; END $f$;
      ALTER FUNCTION admin_touch() OWNER TO ${admin};
      CREATE TRIGGER editorial_articles_aa_min_update_trg BEFORE UPDATE ON editorial_articles
        FOR EACH ROW EXECUTE FUNCTION suppress_redundant_updates_trigger();
      CREATE TRIGGER editorial_articles_ab_site_trg BEFORE UPDATE ON editorial_articles FOR EACH ROW EXECUTE FUNCTION site_touch();
      CREATE TRIGGER editorial_articles_ac_admin_trg AFTER UPDATE ON editorial_articles FOR EACH ROW EXECUTE FUNCTION admin_touch();`);
    for (const sl of ['a', 'b']) await asRole(st.pool, st.author, (c) => insertAs(c, sl));
    expect(await asRole(st.pool, st.publisher, (p) => promote(p, 'a'))).toBe('published');
    // A function the author owns, attached by a superuser: the author's code would run as the publisher.
    await st.pool.query(`
      CREATE FUNCTION author_touch() RETURNS trigger LANGUAGE plpgsql AS $f$ BEGIN RETURN NULL; END $f$;
      ALTER FUNCTION author_touch() OWNER TO ${st.author};
      CREATE TRIGGER editorial_articles_ad_author_trg AFTER UPDATE ON editorial_articles FOR EACH ROW EXECUTE FUNCTION author_touch();`);
    await expect(asRole(st.pool, st.publisher, (p) => promote(p, 'b')))
      .rejects.toThrow(new RegExp(`while trigger\\(s\\) editorial_articles_ad_author_trg \\(public\\.author_touch\\(\\), owned by ${st.author}\\) run a function that neither the table owner \\(${owner}\\) nor a superuser owns`));
    expect(await statusOf(st.pool, 'b')).toBe('draft');
    // The remedy the hint names, for a trigger of the site's own.
    await st.pool.query(`ALTER FUNCTION author_touch() OWNER TO ${owner}`);
    expect(await asRole(st.pool, st.publisher, (p) => promote(p, 'b'))).toBe('published');
  }, 60_000);

  it('both triggers are ENABLE ALWAYS: replica mode still drafts, and the install names who may SET session_replication_role', async () => {
    const st = await setUpSite('kit_guard_replica');
    sites.push(st);
    const enabled = await st.pool.query(`SELECT tgname, tgenabled FROM pg_trigger
      WHERE tgrelid = 'editorial_articles'::regclass AND tgname LIKE 'editorial_articles_zzz_publish_%' ORDER BY tgname`);
    expect(enabled.rows).toEqual([{ tgname: PUBLISH_GUARD_TRIGGER, tgenabled: 'A' }, { tgname: PUBLISH_RECHECK_TRIGGER, tgenabled: 'A' }]);
    const version = Number((await st.pool.query('SHOW server_version_num')).rows[0].server_version_num);
    if (version < 150000) return; // GRANT SET ON PARAMETER needs PostgreSQL 15
    await asRole(st.pool, st.author, (c) => insertAs(c, 'live'));
    expect(await asRole(st.pool, st.publisher, (c) => promote(c, 'live'))).toBe('published');
    await st.pool.query(`GRANT SET ON PARAMETER session_replication_role TO ${st.author}`);
    try {
      const inReplicaMode = <T>(fn: (c: PoolClient) => Promise<T>) => asRole(st.pool, st.author, async (c) => {
        await c.query('SET session_replication_role = replica');
        try {
          return await fn(c);
        } finally {
          await c.query('RESET session_replication_role');
        }
      });
      expect(await inReplicaMode((c) => insertAs(c, 'replica'))).toBe('draft');
      expect((await inReplicaMode((c) => c.query(`UPDATE editorial_articles SET body_md = 'CPAP runs 20 hours.' WHERE slug = 'live' RETURNING status`))).rows[0].status)
        .toBe('draft');
      expect(await reinstallNotices(st)).toEqual([
        expect.stringMatching(new RegExp(`^publish guard: role\\(s\\) ([a-z0-9_]+, )*${st.author}(, [a-z0-9_]+)* may SET session_replication_role`)),
      ]);
    } finally {
      await st.pool.query(`REVOKE SET ON PARAMETER session_replication_role FROM ${st.author}`);
    }
  }, 60_000);

  it('refuses to install without the table, or with a content column or published_at the table lacks', async () => {
    const db = await createScratchDb('kit_guard_bad');
    try {
      const pool = db.pool({ max: 1 });
      await expect(pool.query(renderPublishTrigger({ publisherRole: 'nobody_x' }))).rejects.toThrow(/table editorial_articles does not exist \(apply kit_0003 first\)/);
      await runKitMigrations(db.pool({ max: 1 }), { ...quiet, kitMigrationsDir: KIT_MIGRATIONS });
      await expect(pool.query(renderPublishTrigger({ publisherRole: 'nobody_x', contentColumns: ['body_md', 'quick_answr'] })))
        .rejects.toThrow(/editorial_articles has no column\(s\) \{quick_answr\}/);
      // A type without an equality operator would fail every guarded write: it fails the install instead.
      await pool.query('ALTER TABLE editorial_articles ADD COLUMN extra_json json');
      await expect(pool.query(renderPublishTrigger({ publisherRole: 'nobody_x', contentColumns: ['body_md', 'extra_json'] })))
        .rejects.toThrow(/content column editorial_articles\.extra_json cannot be compared: operator does not exist: json = json/);
      // The guard clears published_at on every INSERT it guards: without the column every such INSERT would fail.
      await pool.query('ALTER TABLE editorial_articles DROP COLUMN published_at');
      await expect(pool.query(renderPublishTrigger({ publisherRole: 'nobody_x' })))
        .rejects.toThrow(/editorial_articles has no column\(s\) \{published_at\}/);
      expect((await pool.query(`SELECT count(*)::int AS n FROM pg_trigger WHERE tgname = 'editorial_articles_zzz_publish_guard_trg'`)).rows[0].n).toBe(0);
    } finally {
      await db.drop();
    }
  }, 60_000);

  it('a publisher role that does not exist yet is a boot-log WARNING, and nothing can be published (fails closed)', async () => {
    const db = await createScratchDb('kit_guard_norole');
    try {
      const warnings: string[] = [];
      const site = siteDir({ '0001_guard.sql': renderPublishTrigger({ publisherRole: 'publisher_not_created_yet' }) });
      await runKitMigrations(db.pool({ max: 1 }), { ...quiet, warn: (l: string) => warnings.push(l), kitMigrationsDir: KIT_MIGRATIONS, siteMigrationsDir: site });
      expect(warnings).toEqual(['[migrations] 0001_guard.sql: WARNING: publish guard: role publisher_not_created_yet does not exist yet; until it does nothing can be published']);
      const pool = db.pool({ max: 1 });
      await pool.query(`INSERT INTO editorial_articles (slug, title, body_md, category) VALUES ('a', 'T', 'B', 'guide')`);
      expect((await pool.query(`UPDATE editorial_articles SET status = 'published' WHERE slug = 'a' RETURNING status`)).rows[0].status).toBe('draft');
    } finally {
      await db.drop();
    }
  }, 60_000);

  it('a re-render for another role, applied as a new migration, hands publishing to that role', async () => {
    const st = await setUpSite('kit_guard_rerole');
    sites.push(st);
    const next = (await st.db.createRole('kpub2')).name;
    await st.pool.query(`GRANT SELECT, UPDATE (status, published_at) ON editorial_articles TO ${next}`);
    const site = siteDir({ '0003_publish_guard_v2.sql': renderPublishTrigger({ publisherRole: next }) });
    await runPendingMigrations(st.db.pool({ max: 1 }), { ...quiet, sources: [{ dir: site, label: 'site' }] });
    await asRole(st.pool, st.author, (c) => insertAs(c, 'a'));
    expect(await asRole(st.pool, st.publisher, (c) => promote(c, 'a'))).toBe('draft');
    expect(await asRole(st.pool, next, (c) => promote(c, 'a'))).toBe('published');
  }, 60_000);
});

const gateReady = !!frameworkRoot() && pythonCan(['psycopg2']);
const describeGate = gateReady ? describePg : describe.skip;

describeGate('publish guard with the framework publish gate (framework/cli/article_publish_gate.py)', () => {
  let st: Site;
  beforeAll(async () => { st = await setUpSite('kit_guard_gate'); }, 60_000);
  afterAll(async () => { await st.db.drop(); });

  const GATE = `
import json, os
import psycopg2
from framework.cli import article_publish_gate as g
st = g.GateSettings(site='kittest', hint='kittest', enabled=True, table='editorial_articles', slug_column='slug',
    body_column='body_md', status_column='status', window_column='updated_at', draft_status='draft',
    published_status='published', window_hours=6.0, dsn_env='DATABASE_URL', publisher_dsn_env='',
    publisher_role=os.environ['KIT_GATE_PUBLISHER'], requeue_agent_id='kittest-article-publish-gate',
    requeue_subject_tag='refresh', requeue_max_attempts=3, enforce_checks=('claims_guard', 'content_policy'),
    written_by_like=(), faqs_column='faqs', limit=200, guard_function='editorial_articles_publish_guard')
out = []
for step in os.environ['KIT_GATE_STEPS'].split(','):
    who, action = step.split(':')
    conn = psycopg2.connect(os.environ['KIT_GATE_DSN'])
    with conn.cursor() as cur:
        cur.execute('SET ROLE ' + os.environ['KIT_GATE_' + who.upper()])
    conn.commit()
    store = g.ArticleStore(conn, st)
    status = 'published' if action == 'unpublish' else 'draft'
    rows = store.select(status, since_hours=0, slugs=[os.environ['KIT_GATE_SLUG']])
    result = (store.promote(rows[0]) if action == 'promote' else store.unpublish(rows[0])) if rows else 'not-selected'
    with conn.cursor() as cur:
        cur.execute('SELECT published_at FROM editorial_articles WHERE slug = %s', (os.environ['KIT_GATE_SLUG'],))
        at = cur.fetchone()[0]
    out.append({'step': step, 'user': store.current_user(), 'result': result, 'guard': store.guard_status(),
                'published_at': at.isoformat() if at else None})
    store.close()
print(json.dumps(out))
`;

  it("promote() is 'trigger-kept-draft' as the author and 'published' as the publisher; unpublish() works for the author", async () => {
    // The author dates its row itself, five days back: the gate's promote stamps the go-live time instead.
    await asRole(st.pool, st.author, (c) => c.query(
      `INSERT INTO editorial_articles (slug, title, subtitle, excerpt, body_md, category, faqs, outbound_citations, status, published_at)
       VALUES ('gate-article', 'Title', 'Sub', 'Excerpt', 'Body', 'guide', $1::jsonb, ARRAY['https://example.org/a'], 'published', $2)`,
      [JSON.stringify(FAQS), daysAgo(5)]));
    const out = runPythonJson<Array<{ step: string; user: string; result: string; guard: Record<string, unknown>; published_at: string | null }>>(GATE, {
      KIT_GATE_DSN: st.db.url,
      KIT_GATE_PUBLISHER: st.publisher,
      KIT_GATE_AUTHOR: st.author,
      KIT_GATE_SLUG: 'gate-article',
      KIT_GATE_STEPS: 'author:promote,publisher:promote,author:unpublish,publisher:promote',
    });
    expect(out.map((o) => [o.user, o.result])).toEqual([
      [st.author, 'trigger-kept-draft'],
      [st.publisher, 'published'],
      [st.author, 'unpublished'],
      [st.publisher, 'published'],
    ]);
    expect(await statusOf(st.pool, 'gate-article')).toBe('published');
    // published_at: still NULL after the author's promote (rolled back), the go-live time after the
    // publisher's, and that same first time through the unpublish and the re-publish.
    const at = out.map((o) => o.published_at);
    expect(at[0]).toBeNull();
    expect(Math.abs(Date.now() - Date.parse(at[1]!))).toBeLessThan(60_000);
    expect(at.slice(2)).toEqual([at[1], at[1]]);
    // The gate's own check accepts the guard: ENABLE ALWAYS counts as enabled, and the guard
    // function names the gate's statuses.
    for (const o of out) {
      expect(o.guard).toEqual({ installed: true, enabled: true, statuses: true, function: 'editorial_articles_publish_guard' });
    }
  }, 60_000);
});
