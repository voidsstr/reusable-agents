import express from 'express';
import request from 'supertest';
import {
  detailPageUrlFor, REDIRECT_ROUTE, redirectDecision, redirectHandler, refererPath,
} from '../../server/src/http/redirect';
import { pgRedirectLookup } from '../../server/src/http/pgKitDb';
import { createKitRuntime } from '../../server/src/http/runtime';
import type { RedirectLookup, RedirectProduct } from '../../server/src/http/types';
import type { PoolClientLike, PoolLike } from '../../server/src/dbBudget';
import { captureLogger, CF_EDGE_IP, cloudflareCtx, exampleCtx, FakeKitDb, HOME_IP, HUMAN_HEADERS, stubVerifier } from './helpers';

const ctx = exampleCtx();
const ASIN = 'B0DS2X13PH';
const OTHER = 'B0C1234567';
const TAG = 'kitexample-20';

const live: RedirectProduct = { asin: ASIN, isActive: true, buyable: true, qualityFlags: [], title: 'Acme PowerBox 1000', brand: 'Acme' };

describe('redirectDecision', () => {
  it('sends a live product to its tagged /dp/ page', () => {
    expect(redirectDecision(ctx, live, TAG)).toEqual({
      kind: 'amazon-product', reason: 'active',
      url: `https://www.amazon.com/dp/${ASIN}?tag=${TAG}&linkCode=ll1`,
    });
  });

  it('prefers a fresh detail-page URL for the same ASIN, re-tagged, keeping Amazon\'s variation parameters', () => {
    const d = redirectDecision(ctx, { ...live, detailPageUrl: `https://www.amazon.com/dp/${ASIN}?tag=someone-else-20&linkCode=ogi&th=1&psc=1` }, TAG);
    expect(d.kind).toBe('amazon-product');
    const u = new URL(d.url);
    expect(u.searchParams.get('tag')).toBe(TAG);
    expect(u.searchParams.get('th')).toBe('1');
    expect(u.searchParams.get('psc')).toBe('1');
  });

  it.each([
    ['another ASIN', `https://www.amazon.com/dp/${OTHER}?tag=x-20`],
    ['another host', `https://amazon.com.example.net/dp/${ASIN}`],
    ['another marketplace', `https://www.amazon.co.uk/dp/${ASIN}`],
    ['not a URL', 'nope'],
  ])('ignores a detail-page URL on %s', (_label, url) => {
    expect(redirectDecision(ctx, { ...live, detailPageUrl: url }, TAG).url).toBe(`https://www.amazon.com/dp/${ASIN}?tag=${TAG}&linkCode=ll1`);
  });

  it('sends a listing Amazon no longer sells to a tagged search, never its dead /dp/ page', () => {
    const d = redirectDecision(ctx, { ...live, isActive: false, qualityFlags: ['no-buybox-2026-11-01'] }, TAG);
    expect(d.kind).toBe('amazon-search');
    const u = new URL(d.url);
    expect(u.pathname).toBe('/s');
    expect(u.searchParams.get('k')).toBe('Acme PowerBox 1000');
    expect(u.searchParams.get('tag')).toBe(TAG);
  });

  it('keeps the /dp/ link for a row deactivated for other reasons (as upstream)', () => {
    expect(redirectDecision(ctx, { ...live, isActive: false, qualityFlags: ['off-vertical'] }, TAG))
      .toMatchObject({ kind: 'amazon-product', reason: 'inactive' });
  });

  it('never sends a reader to buy a product the site holds back (e.g. a recall)', () => {
    expect(redirectDecision(ctx, { ...live, buyable: false }, TAG)).toEqual({ kind: 'site', url: `/product/${ASIN}`, reason: 'not-buyable' });
    expect(redirectDecision(ctx, { ...live, buyable: false, isActive: false, qualityFlags: '["dead-amazon-link"]' }, TAG).kind).toBe('site');
  });

  it.each<[string, unknown]>([
    ['missing (a lookup that never checked)', undefined],
    ['null', null],
    ['a truthy non-boolean', 'yes'],
  ])('fails closed when buyable is %s', (_label, buyable) => {
    const p = { ...live, buyable } as unknown as RedirectProduct;
    expect(redirectDecision(ctx, p, TAG)).toEqual({ kind: 'site', url: `/product/${ASIN}`, reason: 'not-buyable' });
  });

  it('falls back to our page when an unsellable listing has nothing to search for', () => {
    expect(redirectDecision(ctx, { asin: ASIN, isActive: false, buyable: true, qualityFlags: ['amazon-not-found'] }, TAG))
      .toEqual({ kind: 'site', url: `/product/${ASIN}`, reason: 'no-search-terms' });
  });

  it('detailPageUrlFor accepts /gp/product/ and forces https', () => {
    expect(detailPageUrlFor(ctx, `http://www.amazon.com/gp/product/${ASIN}/ref=x`, ASIN, TAG))
      .toBe(`https://www.amazon.com/gp/product/${ASIN}/ref=x?tag=${TAG}`);
    expect(detailPageUrlFor(ctx, null, ASIN, TAG)).toBe('');
  });
});

describe('refererPath', () => {
  it.each<[string | undefined, string | undefined]>([
    [undefined, undefined],
    ['https://example.com/guides/x?y=1', '/guides/x'],
    ['/product/B0DS2X13PH', '/product/B0DS2X13PH'],
    ['https://elsewhere.example/r/buildapc', '/r/buildapc'],
    ['http://[bad', undefined],
  ])('%s -> %s', (ref, want) => {
    expect(refererPath(ref, ctx)).toBe(want);
  });
});

function setup(lookup: RedirectLookup) {
  const db = new FakeKitDb();
  const log = captureLogger();
  const rt = createKitRuntime(ctx, { db, verifier: stubVerifier(), log, env: {} });
  const app = express();
  app.get(REDIRECT_ROUTE, redirectHandler(rt, lookup));
  return { app, db, rt, log };
}

describe('GET /r/:asin', () => {
  it('redirects with the source kind\'s tracking ID and logs the click', async () => {
    const calls: string[] = [];
    const { app, db, rt } = setup(async (asin) => { calls.push(asin); return live; });
    const res = await request(app).get(`/r/${ASIN}`).set(HUMAN_HEADERS)
      .set('Referer', 'https://example.com/guides/best-power-stations').set('X-Forwarded-For', HOME_IP);
    expect(res.status).toBe(302);
    expect(res.headers.location).toBe(`https://www.amazon.com/dp/${ASIN}?tag=kitexample-guides-20&linkCode=ll1`);
    expect(res.headers['cache-control']).toBe('private, no-store');
    expect(res.headers['x-robots-tag']).toBe('noindex, nofollow');
    expect(calls).toEqual([ASIN]);
    await rt.writer.drain();
    expect(db.clicks).toEqual([{
      siteId: 'kit-example', asin: ASIN, trackingId: 'kitexample-guides-20',
      sourcePage: '/guides/best-power-stations', sourceKind: 'article', target: 'amazon',
      ipAddress: HOME_IP, userAgent: HUMAN_HEADERS['User-Agent'], isBot: false, botReason: null,
      country: null, deviceType: 'desktop', via: 'redirect', acceptLanguage: 'en-US,en;q=0.9',
      sessionPageviews: null, msOnPage: null, placement: null,
    }]);
  });

  it('uses the partner tag for a kind with no tag of its own, and logs crawler hits as bots', async () => {
    const { app, db, rt } = setup(async () => live);
    await request(app).get(`/r/${ASIN}`).set(HUMAN_HEADERS).set('Referer', `https://example.com/product/${ASIN}`).set('X-Forwarded-For', HOME_IP)
      .expect(302).expect('Location', `https://www.amazon.com/dp/${ASIN}?tag=${TAG}&linkCode=ll1`);
    await request(app).get(`/r/${ASIN}`).set('User-Agent', 'Sogou web spider/4.0').expect(302);
    await request(app).get(`/r/${ASIN}`).set(HUMAN_HEADERS).set('X-Forwarded-For', HOME_IP).expect(302);
    await rt.writer.drain();
    expect(db.clicks.map((c) => [c.sourceKind, c.isBot, c.botReason])).toEqual([
      ['product', false, null],
      ['direct', true, 'ua-bot'],
      ['direct', true, 'no-referer'],
    ]);
  });

  it('sends an unsellable listing to a tagged search and still logs it', async () => {
    const { app, db, rt } = setup(async () => ({ ...live, isActive: false, qualityFlags: ['no-buybox'] }));
    const res = await request(app).get(`/r/${ASIN}`).set(HUMAN_HEADERS).set('Referer', '/category/power-stations').expect(302);
    expect(new URL(res.headers.location as string).pathname).toBe('/s');
    await rt.writer.drain();
    expect(db.clicks.map((c) => [c.sourceKind, c.trackingId])).toEqual([['category', TAG]]);
  });

  it('sends a held-back product to its own page and logs no Amazon click', async () => {
    const { app, db, rt } = setup(async () => ({ ...live, buyable: false }));
    await request(app).get(`/r/${ASIN}`).set(HUMAN_HEADERS).expect(302).expect('Location', `/product/${ASIN}`);
    await rt.writer.drain();
    expect(db.clicks).toEqual([]);
  });

  it('404s anything that is not a real ASIN, without a lookup', async () => {
    const calls: string[] = [];
    const { app, db } = setup(async (a) => { calls.push(a); return live; });
    for (const key of ['EBAY_123456789', 'b0ds2x13ph', 'B0DS2X13P', 'abc']) {
      await request(app).get(`/r/${key}`).expect(404).expect('Product not found');
    }
    expect(calls).toEqual([]);
    expect(db.clicks).toEqual([]);
  });

  it('404s an ASIN that is not in the catalog', async () => {
    const { app, db } = setup(async () => null);
    await request(app).get(`/r/${OTHER}`).expect(404);
    expect(db.clicks).toEqual([]);
  });

  it('on a lookup failure sends the reader to our product page instead of guessing a destination', async () => {
    const { app, db, log } = setup(async () => { throw new Error('statement timeout'); });
    await request(app).get(`/r/${ASIN}`).expect(302).expect('Location', `/product/${ASIN}`);
    expect(db.clicks).toEqual([]);
    expect(log.errors).toEqual([`[affiliate-kit] /r/${ASIN} lookup failed: statement timeout`]);
  });

  it('a lookup SQL without a buyable column fails closed, loudly', async () => {
    const rows = [{ asin: ASIN, is_active: true, title: 'Acme' }];
    const pool: PoolLike = {
      connect: async (): Promise<PoolClientLike> => ({
        query: async (sql: string) => ({ rows: /^\s*(BEGIN|COMMIT|ROLLBACK)/.test(sql) ? [] : rows }),
        release: () => undefined,
      }),
    };
    const { app, db, log } = setup(pgRedirectLookup(pool, 'SELECT asin, is_active, title FROM catalog WHERE asin = $1'));
    await request(app).get(`/r/${ASIN}`).set(HUMAN_HEADERS).expect(302).expect('Location', `/product/${ASIN}`);
    expect(db.clicks).toEqual([]);
    expect(log.errors).toHaveLength(1);
    expect(log.errors[0]).toMatch(new RegExp(`^\\[affiliate-kit\\] /r/${ASIN} lookup failed: redirect lookup: the SQL returned no 'buyable' column`));
  });

  it('behind Cloudflare (site.json edge.cdn), logs the reader\'s address, not the edge', async () => {
    const db = new FakeKitDb();
    const rt = createKitRuntime(cloudflareCtx(), { db, verifier: stubVerifier(), log: captureLogger(), env: {} });
    const app = express();
    app.get(REDIRECT_ROUTE, redirectHandler(rt, async () => live));
    await request(app).get(`/r/${ASIN}`).set(HUMAN_HEADERS).set('Referer', '/guides/x').set('X-Forwarded-For', `${HOME_IP}, ${CF_EDGE_IP}`).expect(302);
    await rt.writer.drain();
    expect(db.clicks.map((c) => [c.ipAddress, c.isBot])).toEqual([[HOME_IP, false]]);
  });

  it('a failed click write does not delay or break the redirect', async () => {
    const { app, db, rt, log } = setup(async () => live);
    db.failWrites = 1;
    await request(app).get(`/r/${ASIN}`).set(HUMAN_HEADERS).set('Referer', '/').expect(302);
    await rt.writer.drain();
    expect(log.errors).toEqual(['[affiliate-kit] outbound_clicks redirect write failed: db down']);
  });
});
