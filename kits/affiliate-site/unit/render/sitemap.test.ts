/**
 * Sitemaps carry indexable URLs only: a candidate that is noindex, off-site,
 * not canonical, robots-disallowed or already listed never becomes a <url>,
 * and the IndexNow list is the same set. Shards are a pure function of the
 * URL; each child's index lastmod is its newest URL's; an empty or failed
 * build is never a cacheable 200. Partition and lastmod cases are SpecPicks'
 * own (tests/unit/productSitemap.test.ts).
 */
import * as fs from 'fs';
import * as path from 'path';
import { loadSiteContext, parseSiteContext, type SiteJson } from '../../server/src/siteContext';
import { INDEXABLE_PAGE, noindexPage } from '../../server/src/render/seo';
import { canonicalUrl } from '../../server/src/render/urls';
import {
  SITEMAP_CACHE_CONTROL,
  SITEMAP_PROTOCOL_MAX_URLS,
  SitemapBuildError,
  buildSitemapSet,
  escapeXml,
  indexableUrls,
  maxLastmod,
  robotsDisallows,
  shardFromPgHash,
  sitemapDate,
  sitemapFileResponse,
  sitemapIndexResponse,
  sitemapIndexXml,
  sitemapShardOf,
  sitemapUnavailableResponse,
  sitemapUrlsFrom,
  urlsetXml,
  type SitemapCandidate,
} from '../../server/src/render/sitemap';
import { xmlProblems } from './xmlCheck';

const KIT = path.resolve(__dirname, '../..');
const ctx = loadSiteContext(path.join(KIT, 'fixtures/site.example.json'), { env: {} });

function idx(loc: string, extra: Partial<SitemapCandidate> = {}): SitemapCandidate {
  return { loc, indexing: INDEXABLE_PAGE, ...extra };
}

function locs(xml: string): string[] {
  return Array.from(xml.matchAll(/<loc>([^<]*)<\/loc>/g)).map((m) => (m[1] as string).replace(/&amp;/g, '&'));
}

describe('XML documents', () => {
  it('urlsetXml escapes values, carries the image namespace and at most 5 images per URL', () => {
    const xml = urlsetXml([
      { loc: 'https://example.com/a?x=1&y=<2>', lastmod: '2026-11-17', changefreq: 'weekly', priority: '0.7',
        images: Array.from({ length: 7 }, (_, k) => ({ loc: `https://example.com/i${k}.png`, title: `T&${k}` })) },
      { loc: "https://example.com/b'c" },
    ]);
    expect(xmlProblems(xml)).toEqual([]);
    expect(xml).toContain('xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"');
    expect(xml).toContain('xmlns:image="http://www.google.com/schemas/sitemap-image/1.1"');
    expect(xml).toContain('<loc>https://example.com/a?x=1&amp;y=&lt;2&gt;</loc>');
    expect(xml).toContain('<loc>https://example.com/b&apos;c</loc>');
    expect(xml.match(/<image:image>/g)).toHaveLength(5);
    expect(xml).toContain('<image:title>T&amp;0</image:title>');
    expect(xmlProblems(urlsetXml([]))).toEqual([]);
  });

  it('sitemapIndexXml lists each child with its lastmod when known', () => {
    const xml = sitemapIndexXml([{ loc: 'https://example.com/sitemap-a.xml', lastmod: '2026-11-01' }, { loc: 'https://example.com/sitemap-b.xml' }]);
    expect(xmlProblems(xml)).toEqual([]);
    expect(xml).toContain('<sitemap>\n    <loc>https://example.com/sitemap-a.xml</loc>\n    <lastmod>2026-11-01</lastmod>\n  </sitemap>');
    expect(xml).toContain('<sitemap>\n    <loc>https://example.com/sitemap-b.xml</loc>\n  </sitemap>');
  });

  it('escapeXml drops characters XML 1.0 forbids', () => {
    expect(escapeXml('a\u0000b\u0008c\u000bd\ufffe & "q"')).toBe('abcd &amp; &quot;q&quot;');
  });

  it('sitemapDate and maxLastmod (ported)', () => {
    expect(sitemapDate('2026-11-17T23:59:59Z')).toBe('2026-11-17');
    expect(sitemapDate(new Date('2026-01-02T03:04:05Z'))).toBe('2026-01-02');
    expect(sitemapDate('not a date')).toBeUndefined();
    expect(sitemapDate(null)).toBeUndefined();
    expect(maxLastmod([{ lastmod: '2026-09-24' }, {}, { lastmod: '2026-09-30' }])).toBe('2026-09-30');
    expect(maxLastmod([{}])).toBeUndefined();
  });
});

describe('sharding', () => {
  it('shardFromPgHash normalises a negative hashtextextended (ported)', () => {
    expect(shardFromPgHash('-7', 2)).toBe(1);
    expect(shardFromPgHash('-8', 3)).toBe(1);
    expect(shardFromPgHash('9223372036854775807', 2)).toBe(1);
    expect(() => shardFromPgHash('1', 0)).toThrow(RangeError);
  });

  it('sitemapShardOf is a pure, roughly uniform function of the key', () => {
    const keys = Array.from({ length: 8000 }, (_, k) => `https://example.com/product/B0${String(k).padStart(8, '0')}`);
    const counts = [0, 0, 0, 0];
    for (const k of keys) {
      const s = sitemapShardOf(k, 4);
      expect(s).toBe(sitemapShardOf(k, 4));
      counts[s] = (counts[s] as number) + 1;
    }
    for (const c of counts) expect(Math.abs(c - 2000)).toBeLessThan(200);
    expect(sitemapShardOf('anything', 1)).toBe(0);
    expect(() => sitemapShardOf('x', 0)).toThrow(RangeError);
  });
});

describe('only indexable, canonical URLs are listed', () => {
  const candidates: SitemapCandidate[] = [
    idx('/product/B0ABCDEFGH', { lastmod: '2026-11-10T09:00:00Z', changefreq: 'weekly', priority: 0.8,
      images: ['/img/a.webp', 'https://m.media-amazon.com/images/I/x.jpg', 'http://cdn.example.org/plain.jpg', { loc: 'https://cdn.example.org/b.webp', title: 'B' }] }),
    { loc: '/product/B0CABLE001', indexing: noindexPage('cable without our own fit data') },
    idx('https://elsewhere.example.org/product/B0X'),
    idx('http://example.com/product/B0HTTP0001'),
    idx('/guides/x#section'),
    idx('/best/under-1000?utm_source=newsletter'),
    idx('/r/B0ABCDEFGH'),
    idx('/api/thing'),
    idx('https://www.example.com/product/B0ABCDEFGH/'),
    idx('//example.com//kits//outage/', { priority: 7, lastmod: 'garbage' }),
    idx(''),
  ];
  const r = sitemapUrlsFrom(ctx, 'products', candidates);

  it('lists the indexable ones once, normalised to the canonical origin', () => {
    expect(r.urls.map((u) => u.loc)).toEqual(['https://example.com/product/B0ABCDEFGH', 'https://example.com/kits/outage']);
    expect(r.urls[0]).toEqual({
      loc: 'https://example.com/product/B0ABCDEFGH',
      lastmod: '2026-11-10',
      changefreq: 'weekly',
      priority: '0.8',
      images: [{ loc: 'https://example.com/img/a.webp' }, { loc: 'https://cdn.example.org/b.webp', title: 'B' }],
    });
    expect(r.urls[1]).toEqual({ loc: 'https://example.com/kits/outage', priority: '1.0' });
  });

  it('reports why each other candidate was left out', () => {
    expect(r.excluded.map((e) => [e.reason, e.loc])).toEqual([
      ['noindex', '/product/B0CABLE001'],
      ['off-site', 'https://elsewhere.example.org/product/B0X'],
      ['not-https', 'http://example.com/product/B0HTTP0001'],
      ['has-fragment', '/guides/x#section'],
      ['tracking-param', '/best/under-1000?utm_source=newsletter'],
      ['robots-disallowed', '/r/B0ABCDEFGH'],
      ['robots-disallowed', '/api/thing'],
      ['duplicate', 'https://example.com/product/B0ABCDEFGH'],
      ['bad-url', ''],
    ]);
    expect(r.excluded[0]?.detail).toBe('cable without our own fit data');
  });

  it('IndexNow gets exactly the sitemap set', () => {
    expect(indexableUrls(ctx, candidates)).toEqual(r.urls.map((u) => u.loc));
  });

  it('lists a URL in the form its own head declares, and leaves out one whose page canonicalises elsewhere', () => {
    const variants: SitemapCandidate[] = [
      idx('/best/x?b=2&a=1&page='),
      idx('/best/x?a=1&b=2'),
      idx('/best?sort=price', { canonical: '/best' }),
      idx('/best', { canonical: 'https://www.example.com/best/' }),
      idx('/product/B0VARIANT1', { canonical: '/product/B0PRIMARY1' }),
      idx('/guides/y', { canonical: 'https://elsewhere.example.org/y' }),
    ];
    const got = sitemapUrlsFrom(ctx, 'best', variants);
    expect(got.urls.map((u) => u.loc)).toEqual(['https://example.com/best/x?a=1&b=2', 'https://example.com/best']);
    // The same normalisation headTags() uses for rel=canonical.
    expect(got.urls[0]?.loc).toBe(canonicalUrl(ctx, '/best/x?b=2&a=1&page=', { keepParams: 'all' }));
    expect(got.excluded.map((e) => [e.reason, e.loc])).toEqual([
      ['duplicate', 'https://example.com/best/x?a=1&b=2'],
      ['non-canonical', '/best?sort=price'],
      ['non-canonical', '/product/B0VARIANT1'],
      ['non-canonical', '/guides/y'],
    ]);
    expect(indexableUrls(ctx, variants)).toEqual(got.urls.map((u) => u.loc));
  });

  it('never lists an Amazon-hosted image', () => {
    expect(JSON.stringify(r.urls)).not.toMatch(/amazon/i);
  });
});

describe('robotsDisallows', () => {
  it('follows Google\'s robots.txt matching: prefixes, * runs and $ anchors', () => {
    const j = JSON.parse(fs.readFileSync(path.join(KIT, 'fixtures/site.example.json'), 'utf8')) as SiteJson;
    const c = parseSiteContext({ ...j, crawlers: { ...j.crawlers, disallowPaths: ['/api/', '/*?sort=', '/print$'] } }, { env: {} });
    expect(robotsDisallows(c, '/api/x')).toBe(true);
    expect(robotsDisallows(c, '/apix')).toBe(false);
    expect(robotsDisallows(c, '/best/x?sort=price')).toBe(true);
    expect(robotsDisallows(c, '/print')).toBe(true);
    expect(robotsDisallows(c, '/print/page')).toBe(false);
    expect(robotsDisallows(c, '/product/B0ABCDEFGH')).toBe(false);
  });
});

describe('buildSitemapSet', () => {
  const products = Array.from({ length: 7 }, (_, k) => idx(`/product/B0PROD000${k}`, { lastmod: `2026-11-0${k + 1}` }));
  const guides = [idx('/guides/winter', { lastmod: '2026-10-01' }), idx('/product/B0PROD0000'), idx('/guides/new')];

  it('builds one file per kind, numbered shards for a sharded kind, and an index dated by its children', () => {
    const set = buildSitemapSet(ctx, [
      { kind: 'products', candidates: products, sharded: true },
      { kind: 'guides', candidates: guides },
    ]);
    expect(set.files.map((f) => f.path)).toEqual(['/sitemap-products-1.xml', '/sitemap-guides.xml']);
    expect(set.counts).toEqual({ products: 7, guides: 2 });
    expect(set.total).toBe(9);
    expect(set.excluded).toEqual([{ kind: 'guides', loc: 'https://example.com/product/B0PROD0000', reason: 'duplicate' }]);
    expect(locs(set.indexXml)).toEqual(['https://example.com/sitemap-products-1.xml', 'https://example.com/sitemap-guides.xml']);
    expect(set.indexXml).toContain('<lastmod>2026-11-07</lastmod>');
    expect(set.indexXml).toContain('<lastmod>2026-10-01</lastmod>');
    expect(set.indexXml).not.toContain(new Date().toISOString().slice(0, 10));
    for (const f of set.files) expect(xmlProblems(f.xml)).toEqual([]);
    expect(xmlProblems(set.indexXml)).toEqual([]);
  });

  it('splits a kind past the shard target by hash, stably, whatever the input order', () => {
    const a = buildSitemapSet(ctx, [{ kind: 'products', candidates: products }], { shardTarget: 3 });
    const shuffled = [...products].reverse();
    const b = buildSitemapSet(ctx, [{ kind: 'products', candidates: shuffled }], { shardTarget: 3 });
    expect(a.files.map((f) => f.path)).toEqual(['/sitemap-products-1.xml', '/sitemap-products-2.xml', '/sitemap-products-3.xml']);
    const members = (s: typeof a) => s.files.map((f) => f.urls.map((u) => u.loc).sort());
    expect(members(b)).toEqual(members(a));
    expect(a.files.reduce((n, f) => n + f.urls.length, 0)).toBe(7);
    for (const f of a.files) expect(f.urls.length).toBeLessThanOrEqual(SITEMAP_PROTOCOL_MAX_URLS);
  });

  it('never drops a URL for size: a hash bucket that would pass 50,000 makes the kind re-split', () => {
    // With 2 shards these 100,000 URLs hash 50,024 / 49,976: one bucket is over the protocol limit.
    const many = Array.from({ length: 100_000 }, (_, k) => idx(`/p/${k}`));
    const set = buildSitemapSet(ctx, [{ kind: 'p', candidates: many }], { shardTarget: SITEMAP_PROTOCOL_MAX_URLS });
    expect(set.total).toBe(100_000);
    expect(set.excluded).toEqual([]);
    expect(set.files.map((f) => f.path)).toEqual(['/sitemap-p-1.xml', '/sitemap-p-2.xml', '/sitemap-p-3.xml']);
    for (const f of set.files) expect(f.urls.length).toBeLessThanOrEqual(SITEMAP_PROTOCOL_MAX_URLS);
  }, 20_000);

  it('treats a kind with no candidates as an outage unless it may be empty', () => {
    expect(() => buildSitemapSet(ctx, [{ kind: 'products', candidates: [] }])).toThrow(SitemapBuildError);
    const set = buildSitemapSet(ctx, [{ kind: 'products', candidates: products }, { kind: 'guides', candidates: [], allowEmpty: true }]);
    expect(set.emptyKinds).toEqual(['guides']);
    expect(set.indexXml).not.toContain('sitemap-guides');
  });

  it('leaves a kind whose candidates are all noindex out of the index instead of failing', () => {
    const set = buildSitemapSet(ctx, [
      { kind: 'products', candidates: products },
      { kind: 'cables', candidates: [{ loc: '/product/B0CABLE001', indexing: noindexPage('no fit data') }] },
    ]);
    expect(set.emptyKinds).toEqual(['cables']);
    expect(set.files.map((f) => f.kind)).toEqual(['products']);
    expect(set.excluded).toEqual([{ kind: 'cables', loc: '/product/B0CABLE001', reason: 'noindex', detail: 'no fit data' }]);
  });

  it('never lists the index inside a child, and rejects bad kinds and targets', () => {
    const set = buildSitemapSet(ctx, [{ kind: 'hub', candidates: [idx('/sitemap.xml'), idx('/')] }]);
    expect(locs(set.files[0]?.xml ?? '')).toEqual(['https://example.com/']);
    expect(() => buildSitemapSet(ctx, [{ kind: 'Bad Kind', candidates: products }])).toThrow(SitemapBuildError);
    expect(() => buildSitemapSet(ctx, [{ kind: 'a', candidates: products }, { kind: 'a', candidates: guides }])).toThrow(/twice/);
    expect(() => buildSitemapSet(ctx, [{ kind: 'a', candidates: products }], { shardTarget: 60_000 })).toThrow(RangeError);
  });
});

describe('HTTP responses', () => {
  const set = buildSitemapSet(ctx, [{ kind: 'products', candidates: [idx('/product/B0ABCDEFGH')], sharded: true }]);

  it('serves the index and shards as cacheable XML', () => {
    const index = sitemapIndexResponse(set);
    expect(index).toMatchObject({ status: 200, headers: { 'Content-Type': 'application/xml; charset=utf-8', 'Cache-Control': SITEMAP_CACHE_CONTROL } });
    expect(sitemapFileResponse(set, '/sitemap-products-1.xml')).toMatchObject({ status: 200, body: set.files[0]?.xml });
  });

  it('answers 404 for a shard that does not exist, 503 (no-store, Retry-After) when there is no set', () => {
    expect(sitemapFileResponse(set, '/sitemap-products-2.xml').status).toBe(404);
    for (const r of [sitemapIndexResponse(null), sitemapFileResponse(undefined, '/sitemap-products-1.xml')]) {
      expect(r.status).toBe(503);
      expect(r.headers['Cache-Control']).toBe('no-store');
      expect(r.headers['Retry-After']).toBe('300');
      expect(xmlProblems(r.body)).toEqual([]);
    }
    expect(sitemapIndexResponse(null).body).toContain('<sitemapindex');
  });

  it('keeps the reason comment well-formed', () => {
    const r = sitemapUnavailableResponse('db -- timeout <x> & more');
    expect(xmlProblems(r.body)).toEqual([]);
    expect(r.body).toContain('db - - timeout &lt;x&gt; &amp; more');
  });
});
