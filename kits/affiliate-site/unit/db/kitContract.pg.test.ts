/**
 * The kit's writers and readers against the kit's schema, on a real Postgres
 * (KIT_TEST_DATABASE_URL; see helpers/pg.ts): the HTTP layer's Postgres
 * KitDb (server/src/http/pgKitDb.ts, build item K2) writes its click and
 * AI-traffic rows into the kit_0001 / kit_0002 tables and reads llms.txt
 * sections from kit_0002 / kit_0003, and the rows it writes land in the
 * kit_0004 views as a person or not.
 *
 * The HTTP module is loaded at run time and the suite skips when it does not
 * export createPgKitDb, so this contract test never blocks that module's own
 * changes; it fails only when the two disagree about the schema.
 */
import path from 'path';
import type { Pool } from 'pg';
import { runKitMigrations } from '../../server/src/db/migrate';
import { createScratchDb, describePg, type ScratchDb } from './helpers/pg';

const KIT = path.resolve(__dirname, '..', '..');

function loadPgKitDb(): ((pool: Pool) => any) | null {
  try {
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    const mod = require(path.join(KIT, 'server', 'src', 'http', 'pgKitDb'));
    return typeof mod?.createPgKitDb === 'function' ? mod.createPgKitDb : null;
  } catch {
    return null;
  }
}

const createPgKitDb = loadPgKitDb();
const describeContract = createPgKitDb ? describePg : describe.skip;
const DAY_MS = 86_400_000;
const chromeMin = 140 + Math.floor(Math.max(0, Math.floor((Date.now() - Date.parse('2025-09-02T00:00:00Z')) / DAY_MS)) / 28) - 16;
const FRESH_UA = `Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/${chromeMin + 4}.0.0.0 Safari/537.36`;

describeContract('the kit KitDb on the kit schema (real Postgres)', () => {
  let db: ScratchDb;
  let pool: Pool;
  let kitDb: any;

  beforeAll(async () => {
    db = await createScratchDb('kit_contract');
    await runKitMigrations(db.pool({ max: 1 }), { log: () => {}, warn: () => {}, kitMigrationsDir: path.join(KIT, 'migrations') });
    pool = db.pool({ max: 4 });
    kitDb = createPgKitDb!(pool);
  }, 60_000);
  afterAll(async () => { await db.drop(); });

  const click = (over: Record<string, unknown>) => ({
    siteId: 'kit-test', asin: 'B0CONTRACT', trackingId: 'kit-20', sourcePage: '/product/B0CONTRACT', sourceKind: 'product',
    target: 'amazon', ipAddress: '73.10.20.30', userAgent: FRESH_UA, isBot: false, botReason: null, country: 'US',
    deviceType: 'desktop', via: 'beacon', acceptLanguage: 'en-US,en;q=0.9', sessionPageviews: 2, msOnPage: 5400,
    placement: 'pdp-hero', ...over,
  });

  it('click rows (beacon, redirect twin, bot) are written and classified by the views', async () => {
    await kitDb.insertOutboundClick(click({}));
    await kitDb.insertOutboundClick(click({ via: 'redirect', sessionPageviews: null, msOnPage: null, placement: null }));
    await kitDb.insertOutboundClick(click({ isBot: true, botReason: 'ua-bot', userAgent: 'curl/8.5.0', ipAddress: '35.1.2.3' }));
    const rows = await pool.query(`SELECT via, is_bot, classified_at IS NOT NULL AS classified, host(ip_address) AS ip, placement
                                     FROM outbound_clicks ORDER BY id`);
    expect(rows.rows).toEqual([
      { via: 'beacon', is_bot: false, classified: true, ip: '73.10.20.30', placement: 'pdp-hero' },
      { via: 'redirect', is_bot: false, classified: true, ip: '73.10.20.30', placement: null },
      { via: 'beacon', is_bot: true, classified: true, ip: '35.1.2.3', placement: 'pdp-hero' },
    ]);
    const verdicts = await pool.query('SELECT via, verdict FROM outbound_clicks_verdicts ORDER BY id');
    expect(verdicts.rows.map((r) => `${r.via}:${r.verdict}`)).toEqual(['beacon:human', 'redirect:human', 'beacon:site-flagged-bot']);
    expect((await pool.query('SELECT count(*)::int AS n FROM outbound_clicks_verified_human')).rows[0].n).toBe(1);
    expect((await pool.query('SELECT clicks::int, product_page_clicks::int FROM amazon_clicks_verified_human_daily')).rows)
      .toEqual([{ clicks: 1, product_page_clicks: 1 }]);
  });

  it('AI-traffic rows are written and the llms.txt sections read them with the article titles', async () => {
    await pool.query(`INSERT INTO editorial_articles (slug, title, body_md, category, status, published_at)
                      VALUES ('cited-guide', 'A cited guide', 'Body', 'guide', 'published', now() - interval '1 day')`);
    const ai = {
      kind: 'referral', source: 'chatgpt', path: '/guides/cited-guide?utm_source=chatgpt.com', statusCode: 200,
      userAgent: FRESH_UA, referer: 'https://chatgpt.com/', ipAddress: '73.10.20.31', siteId: 'kit-test',
      durationMs: 41, ipVerified: null, ipCheck: null, forwardedFor: null,
    };
    await kitDb.insertAiTraffic(ai);
    await kitDb.insertAiTraffic({ ...ai, kind: 'crawler', source: 'gptbot', referer: null, ipVerified: true, ipCheck: 'range:openai-gptbot' });
    expect((await pool.query('SELECT kind, ip_verified, ip_check FROM ai_traffic_log ORDER BY id')).rows).toEqual([
      { kind: 'referral', ip_verified: null, ip_check: null },
      { kind: 'crawler', ip_verified: true, ip_check: 'range:openai-gptbot' },
    ]);
    const cited = await kitDb.aiCitedPages({
      sources: ['chatgpt'], days: 30, siteId: null, pathPrefixes: ['/guides/'], minReferrals: 1, articlePathPrefix: '/guides/', limit: 10,
    });
    expect(cited).toEqual([{ path: '/guides/cited-guide', referrals: 1, title: 'A cited guide' }]);
    const latest = await kitDb.latestArticles({ days: 30, limit: 5 });
    expect(latest.map((a: { slug: string }) => a.slug)).toEqual(['cited-guide']);
  });
});
