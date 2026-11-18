import express from 'express';
import request from 'supertest';
import { KIT_VERSION } from '../../server/src';
import { healthBody, healthHandler } from '../../server/src/http/health';
import { indexNowKeyHandler, indexNowKeyLocation, indexNowKeyPath } from '../../server/src/http/indexNow';
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
