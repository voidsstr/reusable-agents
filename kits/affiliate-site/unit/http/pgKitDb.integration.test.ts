/**
 * createPgKitDb against a real Postgres, on SpecPicks' table definitions.
 *
 * Skipped unless KIT_TEST_DATABASE_URL (the variable K4's real-Postgres
 * suites use, so one throwaway server runs them all) or KIT_TEST_PG_URL
 * points at a THROWAWAY server, e.g.
 *
 *   docker run -d --rm --name kit-pg -e POSTGRES_PASSWORD=pw -p 127.0.0.1:55432:5432 postgres:16
 *   KIT_TEST_DATABASE_URL=postgres://postgres:pw@127.0.0.1:55432/postgres npx jest unit/http/pgKitDb.integration.test.ts
 *
 * It creates and drops a schema, so it refuses any host but loopback (or a
 * local socket) unless KIT_TEST_PG_ALLOW_REMOTE=1 says the remote server is
 * a throwaway too. Everything runs inside a schema created for the run and
 * dropped after it.
 * The DDL is SpecPicks' (db/migrations 010_click_tracking + the bot / beacon
 * columns, 20260526 ai-traffic-log + 20260925 verification columns), so this
 * also proves the kit's SQL fits the column contracts kit_0001/kit_0002 keep.
 */
import express from 'express';
import { Pool } from 'pg';
import request from 'supertest';
import { mountKit } from '../../server/src/http/mountKit';
import { createPgKitDb, pgRedirectLookup } from '../../server/src/http/pgKitDb';
import type { AiTrafficRow, OutboundClickRow } from '../../server/src/http/types';
import { resetVerifiedClicksCache, verifiedClicks30dByAsin } from '../../server/src/verifiedClicks';
import { captureLogger, exampleCtx, HOME_IP, HUMAN_HEADERS, stubVerifier } from './helpers';

const URL_ = process.env.KIT_TEST_PG_URL || process.env.KIT_TEST_DATABASE_URL || '';
const ALLOW_REMOTE = process.env.KIT_TEST_PG_ALLOW_REMOTE === '1';

/** True for a loopback host (or a local socket directory): somewhere a throwaway server lives. */
function isLoopbackPgUrl(url: string): boolean {
  let u: URL;
  try {
    u = new URL(url);
  } catch {
    return false;
  }
  const host = u.hostname.replace(/^\[|\]$/g, '').toLowerCase();
  if (!host) return (u.searchParams.get('host') ?? '').startsWith('/');
  return host === 'localhost' || host === '::1' || /^127(?:\.\d{1,3}){3}$/.test(host);
}

const REMOTE_REFUSED = !!URL_ && !ALLOW_REMOTE && !isLoopbackPgUrl(URL_);
const describePg = URL_ && !REMOTE_REFUSED ? describe : describe.skip;
const SCHEMA = `k2test_${process.pid}_${Date.now()}`;

describe('the real-Postgres guard', () => {
  it.each<[string, boolean]>([
    ['postgres://postgres:pw@127.0.0.1:55432/postgres', true],
    ['postgres://postgres:pw@localhost/postgres', true],
    ['postgres://postgres:pw@[::1]:5432/postgres', true],
    ['postgres:///postgres?host=/var/run/postgresql', true],
    ['postgresql://u:p@LOCALHOST:5432/db', true],
    ['postgres://u:p@127.10.0.1/db', true],
    ['postgres://u:p@db.example.postgres.database.azure.com:5432/app', false],
    ['postgres://u:p@10.0.0.5/app', false],
    ['not a url', false],
  ])('%s is loopback: %s', (url, want) => {
    expect(isLoopbackPgUrl(url)).toBe(want);
  });

  (REMOTE_REFUSED ? it : it.skip)('refuses a non-loopback test server unless KIT_TEST_PG_ALLOW_REMOTE=1', () => {
    throw new Error('pgKitDb.integration: the test database URL is not on loopback; it creates and drops a schema there. '
      + 'Point it at a throwaway local server, or set KIT_TEST_PG_ALLOW_REMOTE=1 if the remote server is a throwaway.');
  });
});

const DDL = `
CREATE TABLE outbound_clicks (
  id BIGSERIAL PRIMARY KEY,
  site_id VARCHAR(100) NOT NULL,
  asin VARCHAR(20) NOT NULL,
  tracking_id VARCHAR(100),
  source_page VARCHAR(500),
  source_kind VARCHAR(50),
  target VARCHAR(20) NOT NULL DEFAULT 'amazon',
  ip_address INET,
  user_agent VARCHAR(500),
  country VARCHAR(10),
  device_type VARCHAR(50),
  clicked_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
  is_bot BOOLEAN NOT NULL DEFAULT FALSE,
  bot_reason TEXT,
  classified_at TIMESTAMPTZ,
  via TEXT NOT NULL DEFAULT 'redirect',
  accept_language TEXT,
  session_pageviews INT,
  ms_on_page INT,
  placement TEXT
);
CREATE TABLE ai_traffic_log (
  id BIGSERIAL PRIMARY KEY,
  ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  kind TEXT NOT NULL,
  source TEXT NOT NULL,
  path TEXT,
  status_code INTEGER,
  user_agent TEXT,
  referer TEXT,
  ip_address TEXT,
  site_id TEXT,
  ip_verified BOOLEAN,
  ip_check TEXT,
  duration_ms INTEGER,
  forwarded_for TEXT
);
CREATE TABLE editorial_articles (
  id SERIAL PRIMARY KEY,
  slug TEXT UNIQUE,
  title TEXT,
  status TEXT,
  published_at TIMESTAMPTZ
);
CREATE TABLE catalog (asin TEXT PRIMARY KEY, is_active BOOLEAN, quality_flags JSONB, recalled BOOLEAN, title TEXT, brand TEXT);
`;

const click = (o: Partial<OutboundClickRow> = {}): OutboundClickRow => ({
  siteId: 'kit-example', asin: 'B0DS2X13PH', trackingId: 'kitexample-pdp-20', sourcePage: '/product/B0DS2X13PH',
  sourceKind: 'product', target: 'amazon', ipAddress: '73.12.1.9', userAgent: 'UA', isBot: false, botReason: null,
  country: 'US', deviceType: 'desktop', via: 'beacon', acceptLanguage: 'en-US', sessionPageviews: 2, msOnPage: 5200,
  placement: 'pdp-hero', ...o,
});

const ref = (path: string, source = 'chatgpt', status = 200): AiTrafficRow => ({
  kind: 'referral', source, path, statusCode: status, userAgent: 'UA', referer: 'https://chatgpt.com/', ipAddress: '73.12.1.9',
  siteId: 'kit-example', durationMs: 12, ipVerified: null, ipCheck: null, forwardedFor: null,
});

describePg('createPgKitDb on Postgres', () => {
  let admin: Pool;
  let pool: Pool;

  beforeAll(async () => {
    admin = new Pool({ connectionString: URL_, max: 1 });
    await admin.query(`CREATE SCHEMA ${SCHEMA}`);
    pool = new Pool({ connectionString: URL_, max: 4, options: `-c search_path=${SCHEMA}` });
    await pool.query(DDL);
  });

  afterAll(async () => {
    await pool?.end();
    await admin?.query(`DROP SCHEMA IF EXISTS ${SCHEMA} CASCADE`);
    await admin?.end();
  });

  it('inserts beacon and redirect clicks into the SpecPicks column set', async () => {
    const db = createPgKitDb(pool);
    await db.insertOutboundClick(click());
    await db.insertOutboundClick(click({ via: 'redirect', ipAddress: null, sessionPageviews: null, msOnPage: null, placement: null, isBot: true, botReason: 'no-referer' }));
    const r = await pool.query(`SELECT via, host(ip_address) AS ip, is_bot, bot_reason, placement, session_pageviews, classified_at IS NOT NULL AS classified
                                  FROM outbound_clicks ORDER BY id`);
    expect(r.rows).toEqual([
      { via: 'beacon', ip: '73.12.1.9', is_bot: false, bot_reason: null, placement: 'pdp-hero', session_pageviews: 2, classified: true },
      { via: 'redirect', ip: null, is_bot: true, bot_reason: 'no-referer', placement: null, session_pageviews: null, classified: true },
    ]);
  });

  it('inserts AI-traffic rows', async () => {
    const db = createPgKitDb(pool);
    await db.insertAiTraffic({ ...ref('/guides/a'), kind: 'crawler', source: 'claude-user', ipVerified: true, ipCheck: 'static-range:anthropic-outbound' });
    const r = await pool.query('SELECT kind, source, status_code, ip_verified, ip_check, duration_ms FROM ai_traffic_log');
    expect(r.rows).toEqual([{ kind: 'crawler', source: 'claude-user', status_code: 200, ip_verified: true, ip_check: 'static-range:anthropic-outbound', duration_ms: 12 }]);
    await pool.query('DELETE FROM ai_traffic_log');
  });

  it('lists assistant-cited pages: 2+ referrals, 200s, configured prefixes, published articles only', async () => {
    const db = createPgKitDb(pool);
    for (const r of [
      ref('/guides/live'), ref('/guides/live?x=1', 'perplexity'), ref('/guides/live', 'claude'),
      ref('/guides/draft'), ref('/guides/draft'),
      ref('/best/under-500'), ref('/best/under-500'),
      ref('/product/B0DS2X13PH'), ref('/product/B0DS2X13PH'),
      ref('/best/once'),
      ref('/best/broken', 'chatgpt', 404), ref('/best/broken', 'chatgpt', 404),
      ref('/best/other-ai', 'grok'), ref('/best/other-ai', 'grok'),
    ]) await db.insertAiTraffic(r);
    await pool.query(`INSERT INTO editorial_articles (slug, title, status, published_at) VALUES
      ('live', 'Live guide', 'published', NOW() - INTERVAL '2 days'),
      ('draft', 'Draft guide', 'draft', NULL)`);
    const out = await db.aiCitedPages!({
      sources: ['chatgpt', 'perplexity', 'claude'], days: 90, pathPrefixes: ['/guides/', '/best/'], minReferrals: 2,
      limit: 60, articlePathPrefix: '/guides/', siteId: 'kit-example',
    });
    expect(out).toEqual([
      { path: '/guides/live', referrals: 3, title: 'Live guide' },
      { path: '/best/under-500', referrals: 2, title: null },
    ]);
    const otherSite = await db.aiCitedPages!({
      sources: ['chatgpt'], days: 90, pathPrefixes: ['/best/'], minReferrals: 2, limit: 60, articlePathPrefix: null, siteId: 'another-site',
    });
    expect(otherSite).toEqual([]);
  });

  it('lists the latest published articles, newest first', async () => {
    const db = createPgKitDb(pool);
    await pool.query(`INSERT INTO editorial_articles (slug, title, status, published_at) VALUES
      ('newer', 'Newer', 'published', NOW() - INTERVAL '1 day'),
      ('old', 'Old', 'published', NOW() - INTERVAL '45 days'),
      ('future', 'Future', 'published', NOW() + INTERVAL '1 day')`);
    const out = await db.latestArticles!({ days: 30, limit: 25 });
    expect(out.map((a) => a.slug)).toEqual(['newer', 'live']);
    expect(out[0]?.day).toMatch(/^\d{4}-\d{2}-\d{2}$/);
  });

  it('beacons posted through mountKit are what verifiedClicks30dByAsin (K1) counts', async () => {
    await pool.query('DELETE FROM outbound_clicks');
    const app = express();
    const kit = mountKit(app, exampleCtx(), { db: createPgKitDb(pool), verifier: stubVerifier(), env: {}, log: captureLogger() });
    const body = (asin: string, wd = false) => JSON.stringify({ merchant: 'amazon', asin, page: `/product/${asin}`, placement: 'pdp-hero', tag: 'kitexample-pdp-20', pv: 2, ms: 4000, wd });
    for (const [asin, wd] of [['B0DS2X13PH', false], ['B0DS2X13PH', false], ['B0C1234567', false], ['B0C1234567', true]] as Array<[string, boolean]>) {
      await request(app).post('/api/outbound-click').set(HUMAN_HEADERS).set('X-Forwarded-For', HOME_IP)
        .set('Content-Type', 'text/plain;charset=UTF-8').send(body(asin, wd)).expect(204);
    }
    await kit.drain();
    resetVerifiedClicksCache();
    const counts = await verifiedClicks30dByAsin(pool, { siteId: 'kit-example' });
    expect(Object.fromEntries(counts)).toEqual({ B0DS2X13PH: 2, B0C1234567: 1 });
    resetVerifiedClicksCache();
  });

  it('runs a site redirect lookup', async () => {
    await pool.query(`INSERT INTO catalog VALUES ('B0DS2X13PH', true, '[]', true, 'Acme PowerBox', 'Acme')`);
    const lookup = pgRedirectLookup(pool, `SELECT asin, is_active, quality_flags, NOT recalled AS buyable, title, brand
                                             FROM catalog WHERE asin = $1 ORDER BY is_active DESC LIMIT 1`);
    expect(await lookup('B0DS2X13PH')).toEqual({
      asin: 'B0DS2X13PH', isActive: true, qualityFlags: [], buyable: false, detailPageUrl: null, title: 'Acme PowerBox', brand: 'Acme',
    });
    expect(await lookup('B0C1234567')).toBeNull();
    // NULL from the gate expression (recall status unknown) is not buyable; an explicit TRUE is
    await pool.query(`INSERT INTO catalog VALUES ('B0NULLBUY1', true, '[]', NULL, 'Unknown status', 'Acme'),
                                                 ('B0CLEARED1', true, '[]', false, 'Cleared', 'Acme')`);
    expect((await lookup('B0NULLBUY1'))?.buyable).toBe(false);
    expect((await lookup('B0CLEARED1'))?.buyable).toBe(true);
    // a lookup SQL that forgot the gate column fails (the /r/ handler then sends readers to our page)
    const ungated = pgRedirectLookup(pool, 'SELECT asin, is_active, title FROM catalog WHERE asin = $1');
    await expect(ungated('B0CLEARED1')).rejects.toThrow(/no 'buyable' column/);
  });
});
