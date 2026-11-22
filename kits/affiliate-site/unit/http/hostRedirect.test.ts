/**
 * site.json `redirectFromHosts`: GET and HEAD on a listed host get a 301 to
 * the canonical origin with the path and query exactly as sent; /health,
 * robots.txt, the IndexNow key file and site-verification files are served on
 * every host (and say noindex there); other methods, other hosts and the
 * canonical host are served as is; mountKit puts it after the AI-traffic
 * logger, so assistants fetching old URLs are logged with their 301.
 */
import express from 'express';
import request from 'supertest';
import {
  HOST_REDIRECT_CACHE_CONTROL,
  hostRedirectLocation,
  hostRedirectMiddleware,
  requestHostname,
  servedOnEveryHost,
} from '../../server/src/http/hostRedirect';
import { mountKit } from '../../server/src/http/mountKit';
import { ANTHROPIC_IP, captureLogger, CLAUDE_USER_UA, exampleCtx, FakeKitDb, HUMAN_HEADERS, stubVerifier } from './helpers';

const OLD = 'example-app.happyhill-123.eastus.azurecontainerapps.io';
const KEY = '0f4c2a9e7b1d4e3a8c6b5d2f1e0a9c7b';
const ASIN = 'B0DS2X13PH';
const KEEP = ['/health', '/robots.txt', `/${KEY}.txt`];
const ctx = exampleCtx((j) => { j.redirectFromHosts = [OLD, 'www.example.com']; });

describe('requestHostname', () => {
  it.each<[string | undefined, string | null]>([
    ['example.com', 'example.com'],
    ['Example.COM', 'example.com'],
    ['example.com:8080', 'example.com'],
    ['example.com.', 'example.com'],
    ['example.com.:443', 'example.com'],
    ['[::1]:8080', null],
    ['', null],
    [undefined, null],
    ['exa mple.com', null],
    ['example.com:http', null],
  ])('%j -> %j', (host, want) => {
    expect(requestHostname(host)).toBe(want);
  });
});

describe('hostRedirectLocation', () => {
  const req = (url: string, host: string, method = 'GET') => ({ method, url, headers: { host } });

  it('sends a listed host to the canonical origin with the path and query exactly as sent', () => {
    expect(hostRedirectLocation(ctx, req('/product/B0DS2X13PH', OLD), KEEP)).toBe('https://example.com/product/B0DS2X13PH');
    expect(hostRedirectLocation(ctx, req('/runtime/fridge?load=600&h=24', OLD), KEEP)).toBe('https://example.com/runtime/fridge?load=600&h=24');
    expect(hostRedirectLocation(ctx, req('/a%20b/%C3%A9?q=%2F&q=2&', `${OLD}:443`), KEEP)).toBe('https://example.com/a%20b/%C3%A9?q=%2F&q=2&');
    expect(hostRedirectLocation(ctx, req('/', 'WWW.Example.com'), KEEP)).toBe('https://example.com/');
    expect(hostRedirectLocation(ctx, req('/x', OLD, 'HEAD'), KEEP)).toBe('https://example.com/x');
    // An absolute-form request target keeps only its path and query.
    expect(hostRedirectLocation(ctx, req(`http://${OLD}/guides/x?y=1`, OLD), KEEP)).toBe('https://example.com/guides/x?y=1');
    // A path that looks like a host stays a path on the canonical origin.
    expect(hostRedirectLocation(ctx, req('//evil.example.org/x', OLD), KEEP)).toBe('https://example.com//evil.example.org/x');
  });

  it('serves the canonical host, other hosts, other methods and the kept paths as is', () => {
    expect(hostRedirectLocation(ctx, req('/x', 'example.com'), KEEP)).toBeNull();
    expect(hostRedirectLocation(ctx, req('/x', '127.0.0.1:10031'), KEEP)).toBeNull();
    expect(hostRedirectLocation(ctx, req('/x', 'other.example.org'), KEEP)).toBeNull();
    expect(hostRedirectLocation(ctx, req('/x', ''), KEEP)).toBeNull();
    for (const m of ['POST', 'PUT', 'DELETE', 'OPTIONS']) expect(hostRedirectLocation(ctx, req('/api/outbound-click', OLD, m), KEEP)).toBeNull();
    for (const p of ['/health', '/robots.txt', `/${KEY}.txt`, '/google1a2b3c4d5e6f7a8b.html', '/BingSiteAuth.xml',
      '/.well-known/acme-challenge/abc', '/.well-known/security.txt', '/robots.txt?x=1']) {
      expect([p, hostRedirectLocation(ctx, req(p, OLD), KEEP)]).toEqual([p, null]);
    }
    // Only those paths: their look-alikes move.
    for (const p of ['/health/x', '/robots.txt.bak', '/llms.txt', '/sitemap.xml', '/google/verify.html', '/.well-known', '/other.txt']) {
      expect([p, hostRedirectLocation(ctx, req(p, OLD), KEEP)]).toEqual([p, `https://example.com${p}`]);
    }
    // A site with no hosts listed never redirects.
    expect(hostRedirectLocation(exampleCtx(), req('/x', OLD), KEEP)).toBeNull();
  });

  it('servedOnEveryHost knows the verification files', () => {
    expect(servedOnEveryHost('/googleabc123.html', [])).toBe(true);
    expect(servedOnEveryHost('/bingsiteauth.xml', [])).toBe(true);
    expect(servedOnEveryHost('/health', [])).toBe(false);
    expect(servedOnEveryHost('/health', ['/health'])).toBe(true);
  });
});

describe('hostRedirectMiddleware', () => {
  const app = express();
  app.use(hostRedirectMiddleware(ctx, { keepPaths: KEEP }));
  app.get('/health', (_req, res) => { res.json({ status: 'ok' }); });
  app.get('/robots.txt', (_req, res) => { res.type('text/plain').send('User-agent: *\nAllow: /\n'); });
  app.get('/google1a2b3c4d5e6f7a8b.html', (_req, res) => { res.type('html').send('google-site-verification: google1a2b3c4d5e6f7a8b.html'); });
  app.post('/api/outbound-click', (_req, res) => { res.status(204).end(); });
  app.get('/guides/:slug', (req, res) => { res.type('html').send(`guide ${req.params.slug}`); });

  it('answers GET and HEAD on a listed host with a 301, cacheable for a day', async () => {
    const r = await request(app).get('/guides/winter-kit?utm_source=chatgpt.com').set('Host', OLD).expect(301);
    expect(r.headers.location).toBe('https://example.com/guides/winter-kit?utm_source=chatgpt.com');
    expect(r.headers['cache-control']).toBe(HOST_REDIRECT_CACHE_CONTROL);
    const head = await request(app).head('/guides/winter-kit').set('Host', `www.example.com:443`).expect(301);
    expect(head.headers.location).toBe('https://example.com/guides/winter-kit');
    // Percent-encoding, repeated parameters and an empty one arrive as sent: nothing decoded or re-encoded.
    const enc = await request(app).get('/a%20b/%C3%A9?q=%2F&q=2&x=').set('Host', OLD).expect(301);
    expect(enc.headers.location).toBe('https://example.com/a%20b/%C3%A9?q=%2F&q=2&x=');
  });

  it('serves the canonical host as is, with no robots header of its own', async () => {
    const r = await request(app).get('/guides/winter-kit').set('Host', 'example.com').expect(200);
    expect(r.text).toBe('guide winter-kit');
    expect(r.headers['x-robots-tag']).toBeUndefined();
  });

  it('serves health, robots.txt and verification files on the old host, marked noindex there', async () => {
    for (const p of ['/health', '/robots.txt', '/google1a2b3c4d5e6f7a8b.html']) {
      const r = await request(app).get(p).set('Host', OLD).expect(200);
      expect([p, r.headers['x-robots-tag']]).toEqual([p, 'noindex']);
    }
    expect((await request(app).get('/health').set('Host', 'example.com').expect(200)).headers['x-robots-tag']).toBeUndefined();
  });

  it('never redirects a beacon POST (a 301 would turn it into a GET and lose the click)', async () => {
    await request(app).post('/api/outbound-click').set('Host', OLD).send('{}').expect(204);
  });
});

describe('mountKit and site.json redirectFromHosts', () => {
  function site(edit: (j: Record<string, any>) => void) {
    const c = exampleCtx(edit);
    const db = new FakeKitDb();
    const app = express();
    const kit = mountKit(app, c, {
      db,
      verifier: stubVerifier({ verified: true, check: 'range:test' }),
      redirectLookup: async (asin) => (asin === ASIN ? { asin, isActive: true, buyable: true } : null),
      log: captureLogger(),
      env: {},
    });
    app.get('/guides/:slug', (req, res) => { res.type('html').send(`guide ${req.params.slug}`); });
    return { app, kit, db };
  }

  it('mounts after the AI-traffic logger, and not at all without hosts', () => {
    const { kit } = site((j) => { j.redirectFromHosts = [OLD]; });
    expect(kit.routes.slice(0, 3)).toEqual(['USE ai-traffic-logger', 'USE host-redirect', 'GET /health']);
    expect(site(() => undefined).kit.routes).not.toContain('USE host-redirect');
  });

  it('redirects pages, llms.txt, sitemaps and /r/ on the old host; serves health, robots.txt and the key file there', async () => {
    const { app } = site((j) => { j.redirectFromHosts = [OLD]; });
    for (const p of ['/guides/winter-kit', '/llms.txt', '/sitemap.xml', `/r/${ASIN}`, '/']) {
      const r = await request(app).get(p).set('Host', OLD).set(HUMAN_HEADERS).expect(301);
      expect(r.headers.location).toBe(`https://example.com${p}`);
    }
    await request(app).get('/health').set('Host', OLD).expect(200);
    expect((await request(app).get('/robots.txt').set('Host', OLD).expect(200)).text).toContain('Sitemap: https://example.com/sitemap.xml');
    await request(app).get(`/${KEY}.txt`).set('Host', OLD).expect(200, KEY);
    // The canonical host is untouched.
    await request(app).get('/guides/winter-kit').set('Host', 'example.com').expect(200, 'guide winter-kit');
  });

  it('logs an assistant that still fetches an old URL, with the 301 it got', async () => {
    const { app, kit, db } = site((j) => { j.redirectFromHosts = [OLD]; });
    await request(app).get('/guides/winter-kit').set('Host', OLD).set('User-Agent', CLAUDE_USER_UA).set('X-Forwarded-For', ANTHROPIC_IP).expect(301);
    await kit.drain();
    expect(db.ai.map((r) => [r.path, r.statusCode, r.ipVerified])).toEqual([['/guides/winter-kit', 301, true]]);
  });

  it('a closed site\'s redirects say noindex too', async () => {
    const { app, kit } = site((j) => { j.redirectFromHosts = [OLD]; j.indexing = 'closed'; });
    expect(kit.routes.slice(0, 3)).toEqual(['USE closed-indexing', 'USE ai-traffic-logger', 'USE host-redirect']);
    const r = await request(app).get('/guides/winter-kit').set('Host', OLD).expect(301);
    expect(r.headers['x-robots-tag']).toBe('noindex');
    expect(r.headers.location).toBe('https://example.com/guides/winter-kit');
  });
});
