import express from 'express';
import request from 'supertest';
import { BEACON_PATH } from '../../server/src/http/clientContract';
import { outboundClickHandlers, parseBeaconPayload } from '../../server/src/http/outboundClick';
import { createKitRuntime, type KitDeps } from '../../server/src/http/runtime';
import type { SiteContext } from '../../server/src/siteContext';
import {
  captureLogger, CF_EDGE_IP, CF_WORKER_IP, cloudflareCtx, exampleCtx, FakeKitDb, HOME_IP, HUMAN_HEADERS, IPHONE_UA, stubVerifier,
} from './helpers';

const exampleSite = exampleCtx();
const ASIN = 'B0DS2X13PH';

function setup(over: Partial<KitDeps> = {}, preParser?: express.RequestHandler, ctx: SiteContext = exampleSite) {
  const db = new FakeKitDb();
  const log = captureLogger();
  const rt = createKitRuntime(ctx, { db, verifier: stubVerifier(), log, env: {}, ...over });
  const app = express();
  if (preParser) app.use(preParser);
  app.post(BEACON_PATH, ...outboundClickHandlers(rt));
  return { app, db, rt, log };
}

function beacon(fields: Record<string, unknown> = {}): string {
  return JSON.stringify({
    merchant: 'amazon', asin: ASIN, page: `/product/${ASIN}`, placement: 'pdp-hero',
    tag: 'kitexample-pdp-20', pv: 2, ms: 5200, wd: false, ...fields,
  });
}

function post(app: express.Express, body: string, headers: Record<string, string> = HUMAN_HEADERS) {
  let r = request(app).post(BEACON_PATH).set('Content-Type', 'text/plain;charset=UTF-8');
  for (const [k, v] of Object.entries(headers)) r = r.set(k, v);
  return r.send(body);
}

describe('parseBeaconPayload', () => {
  it('reads the JSON text sendBeacon posts', () => {
    expect(parseBeaconPayload(beacon())).toEqual({
      merchant: 'amazon', asin: ASIN, page: `/product/${ASIN}`, placement: 'pdp-hero',
      tag: 'kitexample-pdp-20', pv: 2, ms: 5200, wd: false,
    });
  });

  it('accepts an already-parsed object or a Buffer', () => {
    expect(parseBeaconPayload(JSON.parse(beacon()))?.asin).toBe(ASIN);
    expect(parseBeaconPayload(Buffer.from(beacon()))?.asin).toBe(ASIN);
  });

  it.each<[string, unknown]>([
    ['bad JSON', '{"merchant":'],
    ['empty', ''],
    ['an array', '[]'],
    ['null', null],
    ['eBay (the kit is Amazon only)', beacon({ merchant: 'ebay' })],
    ['no merchant', JSON.stringify({ asin: ASIN })],
  ])('ignores %s', (_label, body) => {
    expect(parseBeaconPayload(body)).toBeNull();
  });

  it('keeps the click but drops fields that fail validation', () => {
    const p = parseBeaconPayload(beacon({
      asin: 'EBAY_123', page: 'https://evil.example/x', placement: 'Hero CTA!', tag: 42, pv: 'x', ms: null, wd: 'true',
    }));
    expect(p).toEqual({ merchant: 'amazon', asin: null, page: '', placement: null, tag: null, pv: null, ms: null, wd: false });
  });

  it('strips control characters, clips to the column widths and clamps to INT', () => {
    const p = parseBeaconPayload(beacon({
      page: `/product/\u0000${ASIN}${'x'.repeat(600)}`,
      tag: `tag\u0007-${'t'.repeat(100)}`,
      pv: 1e12,
      ms: -1e12,
    }))!;
    expect(p.page.startsWith(`/product/${ASIN}`)).toBe(true);
    expect(Array.from(p.page)).toHaveLength(500);
    expect(p.tag).toHaveLength(64);
    expect(p.tag?.includes('\u0007')).toBe(false);
    expect(p.pv).toBe(2147483647);
    expect(p.ms).toBe(-2147483648);
  });

  it('only a literal true counts as navigator.webdriver', () => {
    expect(parseBeaconPayload(beacon({ wd: true }))?.wd).toBe(true);
    expect(parseBeaconPayload(beacon({ wd: 1 }))?.wd).toBe(false);
  });
});

describe('POST /api/outbound-click', () => {
  it('answers 204 with no body, and writes a verified-human beacon row', async () => {
    const { app, db, rt } = setup();
    const res = await post(app, beacon(), { ...HUMAN_HEADERS, 'X-Forwarded-For': HOME_IP, 'CF-IPCountry': 'us' });
    expect(res.status).toBe(204);
    expect(res.text).toBe('');
    expect(res.headers['cache-control']).toBe('no-store');
    await rt.writer.drain();
    expect(db.clicks).toEqual([{
      siteId: 'kit-example',
      asin: ASIN,
      trackingId: 'kitexample-pdp-20',
      sourcePage: `/product/${ASIN}`,
      sourceKind: 'product',
      target: 'amazon',
      ipAddress: HOME_IP,
      userAgent: HUMAN_HEADERS['User-Agent'],
      isBot: false,
      botReason: null,
      country: 'US',
      deviceType: 'desktop',
      via: 'beacon',
      acceptLanguage: 'en-US,en;q=0.9',
      sessionPageviews: 2,
      msOnPage: 5200,
      placement: 'pdp-hero',
    }]);
  });

  it.each<[string, Record<string, unknown>, Record<string, string>, string]>([
    ['navigator.webdriver', { wd: true }, {}, 'webdriver'],
    ['no page view reported', { pv: undefined }, {}, 'no-pageview'],
    ['a click 50 ms after load', { ms: 50 }, {}, 'instant-click'],
    ['no Accept-Language', {}, { 'Accept-Language': '' }, 'no-accept-language'],
    ['a cross-site post', {}, { 'Sec-Fetch-Site': 'cross-site' }, 'sec-fetch-cross-site'],
    ['a headless client hint', {}, { 'Sec-CH-UA': '"HeadlessChrome";v="140"' }, 'headless-client-hint'],
  ])('classifies %s as a bot (and still writes the row)', async (_label, fields, headers, reason) => {
    const { app, db, rt } = setup();
    await post(app, beacon(fields), { ...HUMAN_HEADERS, 'X-Forwarded-For': HOME_IP, ...headers }).expect(204);
    await rt.writer.drain();
    expect(db.clicks.map((c) => [c.isBot, c.botReason])).toEqual([[true, reason]]);
  });

  it('judges the address that connected, not the one the client claims', async () => {
    const { app, db, rt } = setup();
    // a GCP scraper prepending a residential address
    await post(app, beacon(), { ...HUMAN_HEADERS, 'X-Forwarded-For': `${HOME_IP}, 35.190.1.1` }).expect(204);
    await rt.writer.drain();
    expect(db.clicks.map((c) => [c.ipAddress, c.isBot, c.botReason])).toEqual([['35.190.1.1', true, 'datacenter-ip']]);
  });

  it('behind Cloudflare (site.json edge.cdn), stores each reader\'s own address, not the edge', async () => {
    const { app, db, rt } = setup({}, undefined, cloudflareCtx());
    await post(app, beacon(), { ...HUMAN_HEADERS, 'X-Forwarded-For': `${HOME_IP}, ${CF_EDGE_IP}` }).expect(204);
    await post(app, beacon(), { ...HUMAN_HEADERS, 'X-Forwarded-For': `73.12.1.10, ${CF_EDGE_IP}` }).expect(204);
    // a Worker cannot launder its clicks through a residential address it writes into the header
    await post(app, beacon(), { ...HUMAN_HEADERS, 'X-Forwarded-For': `${HOME_IP}, ${CF_WORKER_IP}, ${CF_EDGE_IP}` }).expect(204);
    await rt.writer.drain();
    expect(db.clicks.map((c) => c.ipAddress)).toEqual([HOME_IP, '73.12.1.10', CF_WORKER_IP]);
  });

  it('attributes the page kind from site.json source rules and the device from the UA', async () => {
    const { app, db, rt } = setup();
    await post(app, beacon({ page: '/guides/best-power-stations', placement: 'article-buystrip' }), { ...HUMAN_HEADERS, 'User-Agent': IPHONE_UA, 'X-Forwarded-For': HOME_IP }).expect(204);
    await post(app, beacon({ page: '/', placement: undefined }), { ...HUMAN_HEADERS, 'X-Forwarded-For': HOME_IP }).expect(204);
    await rt.writer.drain();
    expect(db.clicks.map((c) => [c.sourceKind, c.deviceType, c.placement])).toEqual([
      ['article', 'mobile', 'article-buystrip'],
      ['home', 'desktop', null],
    ]);
  });

  it('records a click without an ASIN (a search link) with asin \'\'', async () => {
    const { app, db, rt } = setup();
    await post(app, beacon({ asin: '' }), { ...HUMAN_HEADERS, 'X-Forwarded-For': HOME_IP }).expect(204);
    await rt.writer.drain();
    expect(db.clicks[0]?.asin).toBe('');
  });

  it('works behind a global JSON body parser', async () => {
    const { app, db, rt } = setup({}, express.json());
    await request(app).post(BEACON_PATH).set(HUMAN_HEADERS).set('X-Forwarded-For', HOME_IP)
      .set('Content-Type', 'application/json').send(beacon()).expect(204);
    await rt.writer.drain();
    expect(db.clicks).toHaveLength(1);
  });

  it.each<[string, string]>([
    ['bad JSON', '{nope'],
    ['an eBay click', beacon({ merchant: 'ebay' })],
    ['an oversized body', JSON.stringify({ merchant: 'amazon', pad: 'x'.repeat(4096) })],
  ])('answers 204 and writes nothing for %s', async (_label, body) => {
    const { app, db, rt } = setup();
    const res = await post(app, body, { ...HUMAN_HEADERS, 'X-Forwarded-For': HOME_IP });
    expect(res.status).toBe(204);
    expect(res.text).toBe('');
    await rt.writer.drain();
    expect(db.clicks).toEqual([]);
  });

  it('still answers 204 when the database is down, and says so in the log', async () => {
    const { app, db, rt, log } = setup();
    db.failWrites = 1;
    await post(app, beacon(), { ...HUMAN_HEADERS, 'X-Forwarded-For': HOME_IP }).expect(204);
    await rt.writer.drain();
    expect(log.errors).toEqual(['[affiliate-kit] outbound_clicks beacon write failed: db down']);
  });

  it('is POST only', async () => {
    const { app } = setup();
    await request(app).get(BEACON_PATH).expect(404);
  });
});
