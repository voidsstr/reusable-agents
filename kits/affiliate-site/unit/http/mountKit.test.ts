import express from 'express';
import request from 'supertest';
import { AiCrawlerVerifier } from '../../server/src/aiCrawlerVerify';
import { kitClientBundleFromSource } from '../../server/src/http/clientAsset';
import { createKitRuntime } from '../../server/src/http/runtime';
import { mountKit } from '../../server/src/http/mountKit';
import type { KitDb } from '../../server/src/http/types';
import {
  ANTHROPIC_IP, captureLogger, CF_EDGE_IP, CLAUDE_USER_UA, cloudflareCtx, exampleCtx, FakeKitDb, GPTBOT_UA, HOME_IP,
  HUMAN_HEADERS, stubVerifier,
} from './helpers';

const ctx = exampleCtx();
const ASIN = 'B0DS2X13PH';
const KEY = '0f4c2a9e7b1d4e3a8c6b5d2f1e0a9c7b';

describe('mountKit', () => {
  function site(extra: Partial<Parameters<typeof mountKit>[2]> = {}) {
    const db = new FakeKitDb();
    const log = captureLogger();
    const app = express();
    const kit = mountKit(app, ctx, {
      db,
      verifier: stubVerifier({ verified: true, check: 'range:test' }),
      redirectLookup: async (asin) => (asin === ASIN ? { asin, isActive: true, buyable: true } : null),
      llms: { summary: 'Test site.' },
      log,
      env: {},
      ...extra,
    });
    // the site's own routes come after the kit
    app.get('/guides/:slug', (req, res) => { res.send(`guide ${req.params.slug}`); });
    return { app, db, kit, log };
  }

  it('mounts every kit route, in order, and reports them', () => {
    const { kit } = site({ clientBundle: kitClientBundleFromSource('(()=>{})();') });
    expect(kit.routes).toEqual([
      'USE ai-traffic-logger',
      'GET /health',
      'GET /robots.txt',
      'GET /llms.txt',
      `GET /${KEY}.txt`,
      'POST /api/outbound-click',
      'GET /r/:asin',
      `GET ${kitClientBundleFromSource('(()=>{})();').path}`,
    ]);
  });

  it('serves health, robots, llms and the IndexNow key', async () => {
    const { app } = site();
    expect((await request(app).get('/health').expect(200)).body).toMatchObject({ status: 'ok', siteId: 'kit-example' });
    expect((await request(app).get('/robots.txt').expect(200)).text).toContain('User-agent: GPTBot\nDisallow: /\n');
    expect((await request(app).get('/llms.txt').expect(200)).text.startsWith('# ExamplePicks\n\n> Test site.\n')).toBe(true);
    await request(app).get(`/${KEY}.txt`).expect(200, KEY);
  });

  it('logs AI traffic on the site\'s own pages (mounted first) and on kit routes, never on /health', async () => {
    const { app, db, kit } = site();
    await request(app).get('/guides/winter-kit').set('User-Agent', CLAUDE_USER_UA).set('X-Forwarded-For', ANTHROPIC_IP).expect(200);
    await request(app).get('/robots.txt').set('User-Agent', CLAUDE_USER_UA).set('X-Forwarded-For', ANTHROPIC_IP).expect(200);
    await request(app).get('/health').set('User-Agent', CLAUDE_USER_UA).expect(200);
    await kit.drain();
    expect(db.ai.map((r) => [r.path, r.statusCode, r.ipVerified])).toEqual([
      ['/guides/winter-kit', 200, true],
      ['/robots.txt', 200, true],
    ]);
  });

  it('takes beacons and redirects end to end', async () => {
    const { app, db, kit } = site();
    await request(app).post('/api/outbound-click').set(HUMAN_HEADERS).set('X-Forwarded-For', HOME_IP)
      .set('Content-Type', 'text/plain;charset=UTF-8')
      .send(JSON.stringify({ merchant: 'amazon', asin: ASIN, page: '/guides/winter-kit', placement: 'article-buystrip', tag: 'kitexample-guides-20', pv: 3, ms: 9000, wd: false }))
      .expect(204);
    await request(app).get(`/r/${ASIN}`).set(HUMAN_HEADERS).set('Referer', 'https://example.com/guides/winter-kit')
      .set('X-Forwarded-For', HOME_IP).expect(302).expect('Location', `https://www.amazon.com/dp/${ASIN}?tag=kitexample-guides-20&linkCode=ll1`);
    await kit.drain();
    expect(db.clicks.map((c) => [c.via, c.sourceKind, c.isBot, c.placement])).toEqual([
      ['beacon', 'article', false, 'article-buystrip'],
      ['redirect', 'article', false, null],
    ]);
  });

  it('leaves /r/ and the client asset unmounted without their dependencies', async () => {
    const { app, kit } = site({ redirectLookup: undefined });
    expect(kit.routes).not.toContain('GET /r/:asin');
    expect(kit.routes.some((r) => r.startsWith('GET /kit/'))).toBe(false);
    await request(app).get(`/r/${ASIN}`).expect(404);
  });

  it('serves the client bundle when given one', async () => {
    const bundle = kitClientBundleFromSource('(()=>{window.kitTest=1})();');
    const { app } = site({ clientBundle: bundle });
    const res = await request(app).get(bundle.path).expect(200);
    expect(res.text).toBe(bundle.js);
  });

  it('builds and starts the kit verifier unless told otherwise, and stop() ends it', () => {
    const start = jest.spyOn(AiCrawlerVerifier.prototype, 'start').mockImplementation(() => undefined);
    const stop = jest.spyOn(AiCrawlerVerifier.prototype, 'stop');
    const started = site({ verifier: undefined });
    expect(started.kit.runtime.ownedVerifier).toBeInstanceOf(AiCrawlerVerifier);
    expect(start).toHaveBeenCalledTimes(1);
    started.kit.stop();
    started.kit.stop();
    expect(stop).toHaveBeenCalledTimes(2);

    start.mockClear();
    const manual = site({ verifier: undefined, startVerifier: false });
    expect(manual.kit.runtime.ownedVerifier).toBeInstanceOf(AiCrawlerVerifier);
    expect(start).not.toHaveBeenCalled();

    start.mockClear();
    const injected = site();
    expect(injected.kit.runtime.ownedVerifier).toBeNull();
    expect(start).not.toHaveBeenCalled();
    injected.kit.stop(); // nothing to stop
  });

  it('a log sink that throws never breaks a page, a beacon or the redirect', async () => {
    const db = new FakeKitDb();
    db.failWrites = 10;
    const throwing = { info: () => { throw new Error('sink down'); }, error: () => { throw new Error('sink down'); } };
    const app = express();
    const kit = mountKit(app, ctx, {
      db,
      verifier: { verify: () => { throw new Error('verifier bug'); } },
      redirectLookup: async () => { throw new Error('db timeout'); },
      log: throwing,
      env: {},
    });
    app.get('/guides/:slug', (_req, res) => { res.send('ok'); });
    await request(app).get('/guides/x').set('User-Agent', CLAUDE_USER_UA).expect(200, 'ok');
    await request(app).post('/api/outbound-click').set(HUMAN_HEADERS).set('Content-Type', 'text/plain')
      .send(JSON.stringify({ merchant: 'amazon', asin: ASIN, page: '/', pv: 1, ms: 5000 })).expect(204);
    await request(app).get(`/r/${ASIN}`).expect(302).expect('Location', `/product/${ASIN}`);
    await kit.drain();
    expect(db.clicks).toEqual([]);
  });

  it('behind Cloudflare, configured only in site.json (edge.cdn), logs each visitor\'s own address', async () => {
    const db = new FakeKitDb();
    const app = express();
    // the kit's own verifier, offline: Anthropic's outbound ranges are static kit data
    const kit = mountKit(app, cloudflareCtx(), { db, startVerifier: false, env: {}, log: captureLogger() });
    app.get('/guides/:slug', (_req, res) => { res.send('ok'); });
    await request(app).get('/guides/x').set('User-Agent', CLAUDE_USER_UA).set('X-Forwarded-For', `${ANTHROPIC_IP}, ${CF_EDGE_IP}`).expect(200);
    const readers = Array.from({ length: 25 }, (_, i) => `73.12.${Math.floor(i / 10)}.${(i % 10) + 1}`);
    for (const ip of readers) {
      await request(app).post('/api/outbound-click').set(HUMAN_HEADERS).set('X-Forwarded-For', `${ip}, ${CF_EDGE_IP}`)
        .set('Content-Type', 'text/plain;charset=UTF-8')
        .send(JSON.stringify({ merchant: 'amazon', asin: ASIN, page: `/product/${ASIN}`, placement: 'pdp-hero', tag: 'kitexample-pdp-20', pv: 2, ms: 6000, wd: false }))
        .expect(204);
    }
    await kit.drain();
    expect(db.ai.map((r) => [r.ipAddress, r.ipVerified, r.ipCheck])).toEqual([[ANTHROPIC_IP, true, 'static-range:anthropic-outbound']]);
    expect(new Set(db.clicks.map((c) => c.ipAddress))).toEqual(new Set(readers));
    expect(db.clicks.every((c) => !c.isBot)).toBe(true);
    kit.stop();
  });

  it('a crawler burst against a stalled database never crowds out a beacon', async () => {
    const { app, db, kit, log } = site();
    db.hold();
    for (let i = 0; i < 64; i++) await request(app).get(`/guides/g${i}`).set('User-Agent', GPTBOT_UA).expect(200);
    await request(app).post('/api/outbound-click').set(HUMAN_HEADERS).set('X-Forwarded-For', HOME_IP)
      .set('Content-Type', 'text/plain;charset=UTF-8')
      .send(JSON.stringify({ merchant: 'amazon', asin: ASIN, page: `/product/${ASIN}`, placement: 'pdp-hero', tag: 'kitexample-pdp-20', pv: 2, ms: 6000, wd: false }))
      .expect(204);
    expect(kit.runtime.writer.stats()).toMatchObject({ pending: 49, dropped: 16, shed: 16 });
    db.release();
    await kit.drain();
    expect(db.ai).toHaveLength(48);
    expect(db.clicks.map((c) => [c.via, c.isBot])).toEqual([['beacon', false]]);
    expect(log.errors).toEqual(['[affiliate-kit] ai_traffic_log: dropped 1 write(s), 48 already in flight']);
  });

  it('refuses a database that cannot take the kit\'s writes', () => {
    expect(() => createKitRuntime(ctx, { db: {} as unknown as KitDb })).toThrow(/insertOutboundClick/);
  });

  it('works on an express Router as well as an app', async () => {
    const db = new FakeKitDb();
    const router = express.Router();
    const kit = mountKit(router, ctx, { db, verifier: stubVerifier(), env: {}, log: captureLogger() });
    const app = express();
    app.use(router);
    await request(app).get('/health').expect(200);
    expect(kit.routes).toContain('GET /robots.txt');
  });
});
