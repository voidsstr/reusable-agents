import express from 'express';
import request from 'supertest';
import { IndexingClosedError, KIT_VERSION } from '../../server/src';
import { healthBody, healthHandler } from '../../server/src/http/health';
import {
  INDEXNOW_ENDPOINT,
  INDEXNOW_MAX_URLS,
  indexNowKeyHandler,
  indexNowKeyLocation,
  indexNowKeyPath,
  indexNowSubmission,
  submitIndexNow,
  type IndexNowFetch,
} from '../../server/src/http/indexNow';
import { exampleCtx } from './helpers';

const ctx = exampleCtx();
const KEY = '0f4c2a9e7b1d4e3a8c6b5d2f1e0a9c7b';

describe('IndexNow key file', () => {
  it('lives at /<key>.txt on the canonical origin', () => {
    expect(indexNowKeyPath(ctx)).toBe(`/${KEY}.txt`);
    expect(indexNowKeyLocation(ctx)).toBe(`https://example.com/${KEY}.txt`);
  });

  it('answers with exactly the key, as plain text', async () => {
    const app = express();
    app.get(indexNowKeyPath(ctx), indexNowKeyHandler(ctx));
    const res = await request(app).get(`/${KEY}.txt`).expect(200);
    expect(res.text).toBe(KEY);
    expect(res.headers['content-type']).toBe('text/plain; charset=utf-8');
    expect(res.headers['x-robots-tag']).toBe('noindex');
    await request(app).get('/someotherkey.txt').expect(404);
  });

  it('follows the key in site.json', async () => {
    const other = exampleCtx((j) => { j.indexNow.key = 'abc-DEF-12345678'; });
    const app = express();
    app.get(indexNowKeyPath(other), indexNowKeyHandler(other));
    await request(app).get('/abc-DEF-12345678.txt').expect(200, 'abc-DEF-12345678');
  });
});

describe('the IndexNow submit path', () => {
  const closed = exampleCtx((j) => { j.indexing = 'closed'; });
  const urls = ['https://example.com/product/B0DS2X13PH', 'https://example.com/guides/winter-kit?x=1', 'https://example.com/product/B0DS2X13PH'];

  function recorder(status = 202): IndexNowFetch & { calls: Array<{ url: string; body: any; headers: Record<string, string> }> } {
    const calls: Array<{ url: string; body: any; headers: Record<string, string> }> = [];
    const send: IndexNowFetch = async (url, init) => {
      calls.push({ url, body: JSON.parse(init.body), headers: init.headers });
      return { status };
    };
    const f = send as IndexNowFetch & { calls: typeof calls };
    f.calls = calls;
    return f;
  }

  it('builds one POST body: host, key, key file and the URLs, de-duplicated', () => {
    expect(indexNowSubmission(ctx, urls)).toEqual({
      host: 'example.com',
      key: KEY,
      keyLocation: `https://example.com/${KEY}.txt`,
      urlList: ['https://example.com/product/B0DS2X13PH', 'https://example.com/guides/winter-kit?x=1'],
    });
  });

  it('refuses another host\'s URL, a relative one, a fragment, and more than one batch', () => {
    expect(() => indexNowSubmission(ctx, ['https://other.example.org/x'])).toThrow(/1 URL\(s\) not on https:\/\/example\.com: https:\/\/other\.example\.org\/x/);
    expect(() => indexNowSubmission(ctx, ['/product/B0DS2X13PH'])).toThrow(TypeError);
    expect(() => indexNowSubmission(ctx, ['http://example.com/x'])).toThrow(TypeError);
    expect(() => indexNowSubmission(ctx, ['https://example.com/x#y'])).toThrow(TypeError);
    const many = Array.from({ length: INDEXNOW_MAX_URLS + 1 }, (_, i) => `https://example.com/p/${i}`);
    expect(() => indexNowSubmission(ctx, many)).toThrow(RangeError);
  });

  it('refuses while site.json indexing is closed, before building or sending anything', async () => {
    expect(() => indexNowSubmission(closed, urls)).toThrow(IndexingClosedError);
    expect(() => indexNowSubmission(closed, urls)).toThrow(/IndexNow submission refused: site\.json "indexing" is "closed" for kit-example/);
    const f = recorder();
    await expect(submitIndexNow(closed, urls, { fetch: f })).rejects.toBeInstanceOf(IndexingClosedError);
    // Even an empty list is refused: the switch is checked first.
    await expect(submitIndexNow(closed, [], { fetch: f })).rejects.toBeInstanceOf(IndexingClosedError);
    expect(f.calls).toEqual([]);
  });

  it('POSTs JSON to api.indexnow.org in batches of 10,000, one result per batch', async () => {
    const f = recorder(202);
    const many = Array.from({ length: INDEXNOW_MAX_URLS + 5 }, (_, i) => `https://example.com/p/${i}`);
    const out = await submitIndexNow(ctx, many, { fetch: f });
    expect(out).toEqual([
      { urls: INDEXNOW_MAX_URLS, status: 202, ok: true, error: null },
      { urls: 5, status: 202, ok: true, error: null },
    ]);
    expect(f.calls.map((c) => c.url)).toEqual([INDEXNOW_ENDPOINT, INDEXNOW_ENDPOINT]);
    expect(f.calls[1]?.body).toEqual({ host: 'example.com', key: KEY, keyLocation: `https://example.com/${KEY}.txt`, urlList: many.slice(INDEXNOW_MAX_URLS) });
    expect(f.calls[0]?.headers['Content-Type']).toBe('application/json; charset=utf-8');
    expect(f.calls[0]?.headers['User-Agent']).toBe('kit-example-indexnow/1.0 (+https://example.com/)');
  });

  it('reports refusals and network failures per batch instead of throwing', async () => {
    expect(await submitIndexNow(ctx, urls, { fetch: recorder(422) })).toEqual([{ urls: 2, status: 422, ok: false, error: null }]);
    const down: IndexNowFetch = async () => { throw new Error('ECONNRESET'); };
    expect(await submitIndexNow(ctx, urls, { fetch: down })).toEqual([{ urls: 2, status: 0, ok: false, error: 'ECONNRESET' }]);
    const f = recorder();
    expect(await submitIndexNow(ctx, [], { fetch: f })).toEqual([]);
    expect(f.calls).toEqual([]);
  });
});

describe('GET /health', () => {
  it('reports ok with the site id and kit version, uncached', async () => {
    const app = express();
    app.get('/health', healthHandler(ctx, () => Date.parse('2026-11-17T12:00:00Z')));
    const res = await request(app).get('/health').expect(200);
    expect(res.body).toEqual({ status: 'ok', siteId: 'kit-example', kitVersion: KIT_VERSION, timestamp: '2026-11-17T12:00:00.000Z' });
    expect(res.headers['cache-control']).toBe('no-store');
  });

  it('healthBody is pure', () => {
    expect(healthBody(ctx, 0).timestamp).toBe('1970-01-01T00:00:00.000Z');
  });
});
