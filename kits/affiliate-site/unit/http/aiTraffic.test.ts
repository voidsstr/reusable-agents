import express from 'express';
import * as http from 'http';
import type { AddressInfo } from 'net';
import request from 'supertest';
import { createAiCrawlerVerifier } from '../../server/src/aiCrawlerVerify';
import {
  AI_TRAFFIC_ABORTED_STATUS, aiTrafficMiddleware, shouldLogAiTraffic, type AiTrafficOptions,
} from '../../server/src/http/aiTraffic';
import { createKitRuntime, type KitDeps } from '../../server/src/http/runtime';
import type { CrawlerVerifierLike } from '../../server/src/http/types';
import type { SiteContext } from '../../server/src/siteContext';
import {
  ANTHROPIC_IP, captureLogger, CF_EDGE_IP, CF_WORKER_IP, CHROME_UA, CLAUDE_USER_UA, cloudflareCtx, exampleCtx, FakeKitDb,
  GPTBOT_UA, HOME_IP, OAI_SEARCHBOT_UA, stubVerifier, waitFor,
} from './helpers';

const exampleSite = exampleCtx();

function setup(over: Partial<KitDeps> = {}, opts?: AiTrafficOptions, ctx: SiteContext = exampleSite) {
  const db = new FakeKitDb();
  const log = captureLogger();
  let t = 1_000;
  const rt = createKitRuntime(ctx, {
    db,
    verifier: over.verifier ?? createAiCrawlerVerifier(ctx, { env: {}, fetchImpl: async () => { throw new Error('offline'); } }),
    log,
    now: () => (t += 7),
    env: {},
    ...over,
  });
  const app = express();
  app.use(aiTrafficMiddleware(rt, opts));
  app.get('/page', (_req, res) => { res.send('ok'); });
  app.get('/apiary', (_req, res) => { res.send('bees'); });
  app.get('/slow', (_req, res) => { setTimeout(() => { if (!res.destroyed) res.send('late'); }, 400); });
  app.get('/robots.txt', (_req, res) => { res.type('text/plain').send('User-agent: *'); });
  app.get('/api/thing', (_req, res) => { res.json({}); });
  app.get('/health', (_req, res) => { res.json({ ok: true }); });
  app.get('/assets/app.css', (_req, res) => { res.type('text/css').send(''); });
  app.get('/kit/client.abc.js', (_req, res) => { res.type('application/javascript').send(''); });
  app.get('/logo.png', (_req, res) => { res.type('image/png').send(''); });
  return { app, db, rt, log };
}

describe('shouldLogAiTraffic', () => {
  it.each([
    ['/', true],
    ['/product/B0DS2X13PH', true],
    ['/robots.txt', true],
    ['/llms.txt', true],
    ['/sitemap.xml', true],
    ['/r/B0DS2X13PH', true],
    ['/apiary', true],
    ['/api', false],
    ['/api/outbound-click', false],
    ['/assets/x.js', false],
    ['/images/hero.webp', false],
    ['/static/a', false],
    ['/kit/client.0123abcd.js', false],
    ['/health', false],
    ['/favicon.ico', false],
    ['/photo.AVIF', false],
    ['/fonts/a.woff2', false],
  ])('%s -> %s', (p, want) => {
    expect(shouldLogAiTraffic(p)).toBe(want);
  });

  it('takes site skip lists', () => {
    expect(shouldLogAiTraffic('/admin/x', { skipPrefixes: ['/admin/'] })).toBe(false);
    expect(shouldLogAiTraffic('/api/x', { skipPrefixes: ['/admin'] })).toBe(true);
    expect(shouldLogAiTraffic('/status', { skipPaths: ['/status'] })).toBe(false);
  });
});

describe('aiTrafficMiddleware', () => {
  it('logs an answer-time fetch from Anthropic\'s published range as verified', async () => {
    const { app, db, rt } = setup();
    await request(app).get('/page').set('User-Agent', CLAUDE_USER_UA).set('X-Forwarded-For', ANTHROPIC_IP).expect(200, 'ok');
    await rt.writer.drain();
    expect(db.ai).toEqual([{
      kind: 'crawler',
      source: 'claude-user',
      path: '/page',
      statusCode: 200,
      userAgent: CLAUDE_USER_UA,
      referer: null,
      ipAddress: ANTHROPIC_IP,
      siteId: 'kit-example',
      durationMs: 7,
      ipVerified: true,
      ipCheck: 'static-range:anthropic-outbound',
      forwardedFor: null,
    }]);
  });

  it('marks a crawler outside its vendor\'s strict list as unverified, inside as verified', async () => {
    const verifier = createAiCrawlerVerifier(exampleSite, { env: {}, fetchImpl: async () => { throw new Error('offline'); } });
    verifier.setRangeList('openai-searchbot', { prefixes: [{ ipv4Prefix: '20.42.10.0/24' }] });
    const { app, db, rt } = setup({ verifier });
    await request(app).get('/page').set('User-Agent', OAI_SEARCHBOT_UA).set('X-Forwarded-For', '20.42.10.5').expect(200);
    await request(app).get('/page').set('User-Agent', OAI_SEARCHBOT_UA).set('X-Forwarded-For', HOME_IP).expect(200);
    await rt.writer.drain();
    expect(db.ai.map((r) => [r.source, r.ipAddress, r.ipVerified, r.ipCheck])).toEqual([
      ['oai-searchbot', '20.42.10.5', true, 'range:openai-searchbot'],
      ['oai-searchbot', HOME_IP, false, 'not-in-range:openai-searchbot'],
    ]);
  });

  it('keeps the raw X-Forwarded-For only when it held more than the connecting address', async () => {
    const { app, db, rt } = setup();
    await request(app).get('/page').set('User-Agent', GPTBOT_UA).set('X-Forwarded-For', `198.51.100.3, ${HOME_IP}`);
    await request(app).get('/page').set('User-Agent', GPTBOT_UA).set('X-Forwarded-For', HOME_IP);
    await rt.writer.drain();
    expect(db.ai.map((r) => [r.ipAddress, r.forwardedFor])).toEqual([
      [HOME_IP, `198.51.100.3, ${HOME_IP}`],
      [HOME_IP, null],
    ]);
  });

  it('logs people arriving from an assistant (Referer, or utm_source when the Referer is gone)', async () => {
    const { app, db, rt } = setup();
    await request(app).get('/page').set('User-Agent', CHROME_UA).set('Referer', 'https://chatgpt.com/').set('X-Forwarded-For', HOME_IP);
    await request(app).get('/page?utm_source=perplexity.ai').set('User-Agent', CHROME_UA).set('X-Forwarded-For', HOME_IP);
    await rt.writer.drain();
    expect(db.ai.map((r) => [r.kind, r.source, r.path, r.ipVerified, r.ipCheck])).toEqual([
      ['referral', 'chatgpt', '/page', null, null],
      ['referral', 'perplexity', '/page', null, null],
    ]);
  });

  it('writes nothing for ordinary visitors or a malformed utm_source', async () => {
    const { app, db, rt } = setup();
    await request(app).get('/page').set('User-Agent', CHROME_UA);
    await request(app).get('/page?utm_source=a&utm_source=b').set('User-Agent', CHROME_UA);
    await request(app).get('/page?utm_source=constructor').set('User-Agent', CHROME_UA);
    await rt.writer.drain();
    expect(db.ai).toEqual([]);
  });

  it('skips API, health and static paths even for crawler UAs, but not look-alike page paths', async () => {
    const { app, db, rt } = setup();
    for (const p of ['/api/thing', '/health', '/assets/app.css', '/kit/client.abc.js', '/logo.png']) {
      await request(app).get(p).set('User-Agent', GPTBOT_UA);
    }
    await request(app).get('/apiary').set('User-Agent', GPTBOT_UA);
    await request(app).get('/robots.txt').set('User-Agent', GPTBOT_UA);
    await rt.writer.drain();
    expect(db.ai.map((r) => r.path)).toEqual(['/apiary', '/robots.txt']);
  });

  it('records the status the page actually returned', async () => {
    const { app, db, rt } = setup();
    await request(app).get('/missing').set('User-Agent', GPTBOT_UA).expect(404);
    await rt.writer.drain();
    expect(db.ai.map((r) => [r.path, r.statusCode])).toEqual([['/missing', 404]]);
  });

  it('marks our own fleet host (AI_TRAFFIC_EXCLUDE_IPS) as not the crawler', async () => {
    const verifier = createAiCrawlerVerifier(exampleSite, { env: { AI_TRAFFIC_EXCLUDE_IPS: HOME_IP }, fetchImpl: async () => { throw new Error('offline'); } });
    const { app, db, rt } = setup({ verifier });
    await request(app).get('/page').set('User-Agent', CLAUDE_USER_UA).set('X-Forwarded-For', HOME_IP);
    await rt.writer.drain();
    expect(db.ai.map((r) => [r.ipVerified, r.ipCheck])).toEqual([[false, 'excluded-ip']]);
  });

  it('behind Cloudflare (site.json edge.cdn), verifies the crawler\'s own address, not the edge', async () => {
    const { app, db, rt } = setup({}, undefined, cloudflareCtx());
    await request(app).get('/page').set('User-Agent', CLAUDE_USER_UA).set('X-Forwarded-For', `${ANTHROPIC_IP}, ${CF_EDGE_IP}`);
    await rt.writer.drain();
    expect(db.ai.map((r) => [r.ipAddress, r.ipVerified, r.ipCheck, r.forwardedFor])).toEqual([
      [ANTHROPIC_IP, true, 'static-range:anthropic-outbound', `${ANTHROPIC_IP}, ${CF_EDGE_IP}`],
    ]);
  });

  it('without edge.cdn, the edge is what connected: the same fetch is not verified', async () => {
    const { app, db, rt } = setup({ env: { KIT_TRUSTED_PROXIES: 'cloudflare' } });
    await request(app).get('/page').set('User-Agent', CLAUDE_USER_UA).set('X-Forwarded-For', `${ANTHROPIC_IP}, ${CF_EDGE_IP}`);
    await rt.writer.drain();
    expect(db.ai.map((r) => [r.ipAddress, r.ipVerified])).toEqual([[CF_EDGE_IP, null]]);
  });

  it('a Cloudflare Worker writing an Anthropic address into X-Forwarded-For is logged as itself, never verified', async () => {
    const { app, db, rt } = setup({}, undefined, cloudflareCtx());
    await request(app).get('/page').set('User-Agent', CLAUDE_USER_UA)
      .set('X-Forwarded-For', `${ANTHROPIC_IP}, ${CF_WORKER_IP}, ${CF_EDGE_IP}`);
    await request(app).get('/page').set('User-Agent', GPTBOT_UA)
      .set('X-Forwarded-For', `20.171.207.1, ${CF_WORKER_IP}, ${CF_EDGE_IP}`);
    await rt.writer.drain();
    expect(db.ai.map((r) => r.ipAddress)).toEqual([CF_WORKER_IP, CF_WORKER_IP]);
    expect(db.ai.map((r) => r.ipVerified)).not.toContain(true);
  });

  it('is shed before click rows when the database stops answering', async () => {
    const { app, db, rt, log } = setup({ maxPendingWrites: 8 });
    expect([rt.writer.maxPending, rt.writer.maxSheddable]).toEqual([8, 6]);
    db.hold();
    for (let i = 0; i < 10; i++) await request(app).get('/page').set('User-Agent', GPTBOT_UA).expect(200);
    expect(rt.writer.stats()).toMatchObject({ pending: 6, dropped: 4, shed: 4 });
    // the reserved slots still take click rows
    expect(rt.writer.submit('outbound_clicks beacon', async () => undefined)).toBe(true);
    expect(rt.writer.submit('outbound_clicks beacon', async () => undefined)).toBe(true);
    expect(rt.writer.submit('outbound_clicks beacon', async () => undefined)).toBe(false);
    db.release();
    await rt.writer.drain();
    expect(db.ai).toHaveLength(6);
    expect(log.errors[0]).toBe('[affiliate-kit] ai_traffic_log: dropped 1 write(s), 6 already in flight');
  });

  it('clips long values to the column caps', async () => {
    const { app, db, rt } = setup();
    const longPath = `/${'p'.repeat(400)}`;
    app.get(longPath, (_req, res) => { res.send('x'); });
    await request(app).get(longPath).set('User-Agent', `${GPTBOT_UA} ${'x'.repeat(600)}`).set('Referer', `https://example.com/${'r'.repeat(600)}`);
    await rt.writer.drain();
    const row = db.ai[0]!;
    expect(row.path).toHaveLength(300);
    expect(row.userAgent).toHaveLength(500);
    expect(row.referer).toHaveLength(500);
  });

  it('a failing verifier or database never costs the page', async () => {
    const broken: CrawlerVerifierLike = { verify: () => { throw new Error('verifier bug'); } };
    const { app, db, log } = setup({ verifier: broken });
    await request(app).get('/page').set('User-Agent', GPTBOT_UA).expect(200, 'ok');
    expect(log.errors.join('\n')).toContain('ai-traffic classification failed for /page: verifier bug');

    const second = setup();
    second.db.failWrites = 1;
    await request(second.app).get('/page').set('User-Agent', GPTBOT_UA).expect(200, 'ok');
    await second.rt.writer.drain();
    expect(second.db.ai).toEqual([]);
    expect(second.log.errors).toEqual(['[affiliate-kit] ai_traffic_log write failed: db down']);
    expect(db.ai).toEqual([]);
  });

  describe('a fetch the client abandons', () => {
    async function abandon(app: express.Express): Promise<void> {
      const server = http.createServer(app);
      await new Promise<void>((r) => server.listen(0, '127.0.0.1', () => r()));
      const { port } = server.address() as AddressInfo;
      try {
        await new Promise<void>((resolve) => {
          const req = http.get({ host: '127.0.0.1', port, path: '/slow', headers: { 'User-Agent': CLAUDE_USER_UA, 'X-Forwarded-For': ANTHROPIC_IP } });
          req.on('error', () => resolve());
          setTimeout(() => { req.destroy(); resolve(); }, 60);
        });
        await new Promise((r) => setTimeout(r, 450)); // let the handler finish (or not) on a dead socket
      } finally {
        await new Promise<void>((r) => server.close(() => r()));
      }
    }

    it('is logged once, as 499, so an assistant timing out on a slow page is visible', async () => {
      const { app, db, rt } = setup();
      await abandon(app);
      await waitFor(() => db.ai.length > 0, 2000, 'the 499 row');
      await rt.writer.drain();
      expect(db.ai).toHaveLength(1);
      expect(db.ai[0]).toMatchObject({ source: 'claude-user', path: '/slow', statusCode: AI_TRAFFIC_ABORTED_STATUS, ipVerified: true });
    });

    it('is not logged when logAborted is false', async () => {
      const { app, db, rt } = setup({}, { logAborted: false });
      await abandon(app);
      await rt.writer.drain();
      expect(db.ai).toEqual([]);
    });
  });

  it('asks the verifier with the kind, source and client address', async () => {
    const verifier = stubVerifier({ verified: true, check: 'range:x' });
    const { app, rt } = setup({ verifier });
    await request(app).get('/page').set('User-Agent', GPTBOT_UA).set('X-Forwarded-For', HOME_IP);
    await rt.writer.drain();
    expect(verifier.calls).toEqual([['crawler', 'gptbot', HOME_IP]]);
  });
});
