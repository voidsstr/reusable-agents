/**
 * site.json `"indexing": "closed"`: X-Robots-Tag: noindex on EVERY response
 * (HTML, XML, text, JSON, redirects, 404s, errors, static files), even when a
 * handler overwrites or removes the header or hands writeHead() its own; an
 * open site is untouched; robots.txt keeps allowing crawls either way.
 */
import express from 'express';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import request from 'supertest';
import { kitClientBundleFromSource } from '../../server/src/http/clientAsset';
import {
  CLOSED_INDEXING_ROBOTS_TAG,
  closedIndexingMiddleware,
  ensureNoindexHeader,
  robotsTagNoindexForAll,
} from '../../server/src/http/indexing';
import { mountKit } from '../../server/src/http/mountKit';
import { renderRobotsTxt } from '../../server/src/http/robots';
import { captureLogger, CLAUDE_USER_UA, exampleCtx, FakeKitDb, HUMAN_HEADERS, stubVerifier } from './helpers';

const ASIN = 'B0DS2X13PH';
const KEY = '0f4c2a9e7b1d4e3a8c6b5d2f1e0a9c7b';

/** Every header value of `name` a response carried (Node joins repeated lines with ', '). */
function robotsTag(res: request.Response): string {
  return String(res.headers['x-robots-tag'] ?? '');
}

describe('robotsTagNoindexForAll', () => {
  it.each<[string, boolean]>([
    ['noindex', true],
    ['NOINDEX', true],
    ['noindex, nofollow', true],
    ['nofollow, noindex', true],
    ['none', true],
    ['max-snippet: 20, noindex', true],
    ['unavailable_after: 25 Jun 2030 15:00:00 PST, noindex', true],
    ['index, follow', false],
    ['all', false],
    ['nofollow', false],
    ['noarchive', false],
    // Scoped to one crawler: every other crawler may still index.
    ['googlebot: noindex', false],
    ['bingbot: none', false],
    ['', false],
  ])('%j -> %s', (value, want) => {
    expect(robotsTagNoindexForAll(value)).toBe(want);
  });
});

describe('ensureNoindexHeader', () => {
  function fakeRes(initial?: string | string[]) {
    const h: Record<string, unknown> = {};
    if (initial !== undefined) h['x-robots-tag'] = initial;
    return {
      h,
      getHeader: (n: string) => h[n.toLowerCase()] as string | string[] | undefined,
      setHeader: (n: string, v: unknown) => { h[n.toLowerCase()] = v; return undefined as never; },
    };
  }

  it('sets noindex, keeps a value that already says it, and adds a plain line beside scoped or weaker ones', () => {
    const none = fakeRes();
    ensureNoindexHeader(none);
    expect(none.h['x-robots-tag']).toBe('noindex');
    const already = fakeRes('noindex, nofollow');
    ensureNoindexHeader(already);
    expect(already.h['x-robots-tag']).toBe('noindex, nofollow');
    const scoped = fakeRes(['googlebot: noindex', 'noarchive']);
    ensureNoindexHeader(scoped);
    expect(scoped.h['x-robots-tag']).toEqual(['googlebot: noindex', 'noarchive', CLOSED_INDEXING_ROBOTS_TAG]);
  });
});

describe('closedIndexingMiddleware', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'kit-indexing-'));
  fs.writeFileSync(path.join(dir, 'file.txt'), 'static');
  afterAll(() => fs.rmSync(dir, { recursive: true, force: true }));

  const app = express();
  app.use(closedIndexingMiddleware());
  app.get('/page', (_req, res) => { res.type('html').send('<!doctype html><p>page</p>'); });
  app.get('/sitemap.xml', (_req, res) => { res.type('application/xml').send('<urlset/>'); });
  app.get('/llms.txt', (_req, res) => { res.type('text/plain').send('# site'); });
  app.get('/json', (_req, res) => { res.json({ ok: true }); });
  app.get('/moved', (_req, res) => { res.redirect(301, '/page'); });
  app.get('/found', (_req, res) => { res.redirect('/page'); });
  app.get('/boom', () => { throw new Error('boom'); });
  app.get('/says-index', (_req, res) => { res.set('X-Robots-Tag', 'index, follow').send('x'); });
  app.get('/removes-it', (_req, res) => { res.removeHeader('X-Robots-Tag'); res.send('x'); });
  app.get('/scoped', (_req, res) => { res.set('X-Robots-Tag', 'googlebot: noindex').send('x'); });
  app.get('/own-noindex', (_req, res) => { res.set('X-Robots-Tag', 'noindex, nofollow').send('x'); });
  app.get('/writehead-object', (_req, res) => { res.writeHead(200, { 'Content-Type': 'text/plain', 'x-robots-tag': 'all' }); res.end('x'); });
  app.get('/writehead-message', (_req, res) => { res.writeHead(200, 'Fine', { 'X-Robots-Tag': ['index'] }); res.end('x'); });
  app.get('/writehead-array', (_req, res) => { res.writeHead(200, ['Content-Type', 'text/plain', 'X-Robots-Tag', 'all']); res.end('x'); });
  app.get('/writehead-plain', (_req, res) => { res.writeHead(204); res.end(); });
  app.get('/stream', (_req, res) => { res.write('a'); res.end('b'); });
  app.use('/static', express.static(dir));

  it.each([
    ['/page', 200, /text\/html/],
    ['/sitemap.xml', 200, /application\/xml/],
    ['/llms.txt', 200, /text\/plain/],
    ['/json', 200, /application\/json/],
    ['/moved', 301, /./],
    ['/found', 302, /./],
    ['/no-such-path', 404, /./],
    ['/boom', 500, /./],
    ['/static/file.txt', 200, /text\/plain/],
    ['/writehead-plain', 204, null],
    ['/stream', 200, null],
  ] as Array<[string, number, RegExp | null]>)('%s (HTTP %i) carries X-Robots-Tag: noindex', async (p, status, type) => {
    const res = await request(app).get(p);
    expect(res.status).toBe(status);
    if (type) expect(res.headers['content-type'] ?? '').toMatch(type);
    expect(robotsTag(res)).toBe('noindex');
  });

  it('on HEAD requests too', async () => {
    expect(robotsTag(await request(app).head('/page').expect(200))).toBe('noindex');
  });

  it('a handler cannot take it away: set to index, removed, passed to writeHead as an object, with a message, or as raw pairs', async () => {
    expect(robotsTag(await request(app).get('/says-index'))).toBe('index, follow, noindex');
    expect(robotsTag(await request(app).get('/removes-it'))).toBe('noindex');
    expect(robotsTag(await request(app).get('/writehead-object'))).toBe('all, noindex');
    const msg = await request(app).get('/writehead-message');
    expect(robotsTag(msg)).toBe('index, noindex');
    expect(robotsTag(await request(app).get('/writehead-array'))).toBe('all, noindex');
  });

  it('a value scoped to one crawler does not count; one that already says noindex is left alone', async () => {
    expect(robotsTag(await request(app).get('/scoped'))).toBe('googlebot: noindex, noindex');
    expect(robotsTag(await request(app).get('/own-noindex'))).toBe('noindex, nofollow');
  });
});

describe('mountKit and site.json indexing', () => {
  function site(indexing: 'open' | 'closed') {
    const ctx = exampleCtx((j) => { j.indexing = indexing; });
    const db = new FakeKitDb();
    const app = express();
    const kit = mountKit(app, ctx, {
      db,
      verifier: stubVerifier({ verified: true, check: 'range:test' }),
      redirectLookup: async (asin) => (asin === ASIN ? { asin, isActive: true, buyable: true } : null),
      llms: { summary: 'Test site.' },
      clientBundle: kitClientBundleFromSource('(()=>{})();'),
      log: captureLogger(),
      env: {},
    });
    app.get('/guides/:slug', (req, res) => { res.type('html').send(`<!doctype html><p>guide ${req.params.slug}</p>`); });
    return { app, kit, db, ctx };
  }

  it('closed: mounted first, before the AI-traffic logger, and reported', () => {
    const { kit } = site('closed');
    expect(kit.routes[0]).toBe('USE closed-indexing');
    expect(kit.routes[1]).toBe('USE ai-traffic-logger');
  });

  it('closed: every kit route and every site page says noindex', async () => {
    const { app, kit } = site('closed');
    const bundle = kitClientBundleFromSource('(()=>{})();');
    const responses: Array<[string, request.Response]> = [
      ['page', await request(app).get('/guides/winter-kit').expect(200)],
      ['health', await request(app).get('/health').expect(200)],
      ['robots', await request(app).get('/robots.txt').expect(200)],
      ['llms', await request(app).get('/llms.txt').expect(200)],
      ['key file', await request(app).get(`/${KEY}.txt`).expect(200)],
      ['client bundle', await request(app).get(bundle.path).expect(200)],
      ['click redirect', await request(app).get(`/r/${ASIN}`).set(HUMAN_HEADERS).expect(302)],
      ['beacon', await request(app).post('/api/outbound-click').set(HUMAN_HEADERS).set('Content-Type', 'text/plain;charset=UTF-8')
        .send(JSON.stringify({ merchant: 'amazon', asin: ASIN, page: '/guides/winter-kit', pv: 1, ms: 5000 })).expect(204)],
      ['404', await request(app).get('/no-such-page').expect(404)],
    ];
    // /r/ sets its own "noindex, nofollow", which already says it; everything else gets the plain line.
    for (const [what, r] of responses) expect([what, robotsTag(r)]).toEqual([what, what === 'click redirect' ? 'noindex, nofollow' : 'noindex']);
    await kit.drain();
  });

  it('closed: robots.txt still lets search engines and assistants crawl (a Disallow would hide the noindex)', async () => {
    const closed = site('closed');
    const open = site('open');
    const text = (await request(closed.app).get('/robots.txt')).text;
    expect(text).toBe((await request(open.app).get('/robots.txt')).text);
    expect(text).toBe(renderRobotsTxt(closed.ctx));
    expect(text).toContain('User-agent: *\nDisallow: /api/\nDisallow: /r/\nAllow: /');
    expect(text).toContain('User-agent: Googlebot\nDisallow: /api/\nDisallow: /r/\nAllow: /');
    expect(text).toContain('User-agent: Claude-User\nDisallow: /api/\nDisallow: /r/\nAllow: /');
    // Only the training crawlers' groups are closed.
    expect(text).not.toMatch(/User-agent: (?:\*|Googlebot|Bingbot|Claude-User|ChatGPT-User)\nDisallow: \/\n/);
    expect(text).toContain('User-agent: GPTBot\nDisallow: /\n');
  });

  it('closed: the AI-traffic logger still logs assistants\' fetches', async () => {
    const { app, kit, db } = site('closed');
    await request(app).get('/guides/winter-kit').set('User-Agent', CLAUDE_USER_UA).expect(200);
    await kit.drain();
    expect(db.ai.map((r) => [r.path, r.statusCode])).toEqual([['/guides/winter-kit', 200]]);
  });

  it('open (the default): nothing is mounted and pages carry no X-Robots-Tag', async () => {
    const { app, kit } = site('open');
    expect(kit.routes).not.toContain('USE closed-indexing');
    expect(kit.routes[0]).toBe('USE ai-traffic-logger');
    const page = await request(app).get('/guides/winter-kit').expect(200);
    expect(page.headers['x-robots-tag']).toBeUndefined();
    // The key file was always noindex on its own.
    expect(robotsTag(await request(app).get(`/${KEY}.txt`))).toBe('noindex');
  });
});
