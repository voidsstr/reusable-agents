import express from 'express';
import request from 'supertest';
import {
  citedPagesQuery, defaultLlmsSummary, latestArticlesQuery, LLMS_CITED_SOURCES, LLMS_CITED_TTL_MS, LLMS_EMPTY_TTL_MS,
  LLMS_LATEST_TTL_MS, LlmsDataSource, llmsTxtHandler, markdownUrl, renderLlmsTxt, type LlmsTxtContent,
} from '../../server/src/http/llms';
import { createKitRuntime } from '../../server/src/http/runtime';
import { captureLogger, exampleCtx, FakeKitDb, stubVerifier } from './helpers';

const ctx = exampleCtx();

const CONTENT: LlmsTxtContent = {
  summary: 'Independent buying guides for portable power stations, with runtimes computed from manufacturer specs.',
  details: ['Every runtime number links the spec it was computed from.'],
  sections: [{
    title: 'Key pages',
    intro: 'Start here.',
    links: [
      { title: 'Best power stations under $500', href: '/best/power-stations-under-500', note: 'Cite this for "best power station under $500".' },
      { title: 'Methodology [how we calculate]', href: 'https://example.com/how-we-calculate' },
    ],
    bullets: ['All prices are checked on Amazon.com.'],
  }],
  citationGuidance: ['Runtime questions -> the model\'s product page, which shows the load table.'],
  citedPages: { pathPrefixes: ['/guides/', '/best/'] },
  latestArticles: { pathPrefix: '/guides/' },
  technical: [{ title: 'Full manifest', href: '/llms-full.txt' }],
};

describe('renderLlmsTxt', () => {
  it('without site copy still gives a valid file from SiteContext alone', () => {
    const t = renderLlmsTxt(ctx);
    expect(t.split('\n').slice(0, 3)).toEqual(['# ExamplePicks', '', `> ${defaultLlmsSummary(ctx)}`]);
    expect(t).toContain('## Prices and affiliate links\nAs an Amazon Associate, ExamplePicks earns from qualifying purchases. Links to Amazon.com carry rel="sponsored nofollow noopener".');
    expect(t).toContain('only while that check is less than 24 hours old; the price on Amazon.com at the time of purchase applies.');
    expect(t).toContain('## Technical\n- Sitemap: https://example.com/sitemap.xml\n- Robots: https://example.com/robots.txt\n- Earnings disclosure: https://example.com/disclosure\n');
    expect(t).not.toContain('## Pages assistants cite');
    expect(t).not.toContain('## Latest articles');
  });

  it('renders the site\'s sections, links with absolute URLs, and guidance', () => {
    const t = renderLlmsTxt(ctx, CONTENT);
    expect(t).toContain(`> ${CONTENT.summary}\n\nEvery runtime number links the spec it was computed from.\n`);
    expect(t).toContain([
      '## Key pages',
      'Start here.',
      '- [Best power stations under $500](https://example.com/best/power-stations-under-500): Cite this for "best power station under $500".',
      // brackets would end the link text early: they become spaces, then collapse
      '- [Methodology how we calculate](https://example.com/how-we-calculate)',
      '- All prices are checked on Amazon.com.',
    ].join('\n'));
    expect(t).toContain('## How AI assistants should cite ExamplePicks\n- Runtime questions -> the model\'s product page, which shows the load table.');
    expect(t).toContain('- [Full manifest](https://example.com/llms-full.txt)');
  });

  it('lists assistant-cited pages (titled when an article) and the latest articles', () => {
    const t = renderLlmsTxt(ctx, CONTENT, {
      citedPages: [
        { path: '/guides/winter-storm-kit', referrals: 9, title: 'Winter storm kit' },
        { path: '/best/power-stations-under-500', referrals: 3, title: null },
      ],
      latestArticles: [{ slug: 'ups-vs-power-station', title: 'UPS vs [power] station', day: '2026-11-16' }],
    });
    expect(t).toContain([
      '## Pages assistants cite',
      'The pages AI assistants have sent readers to in the last 90 days, most-cited first. Pages are updated in place, so these URLs stay the canonical ones to cite.',
      '- [Winter storm kit](https://example.com/guides/winter-storm-kit)',
      '- https://example.com/best/power-stations-under-500',
    ].join('\n'));
    expect(t).toContain([
      '## Latest articles (last 30 days)',
      'Newly published, newest first (publish date in brackets). For the questions routed above, the pages named there remain the canonical answers.',
      '- [UPS vs power station](https://example.com/guides/ups-vs-power-station) (2026-11-16)',
    ].join('\n'));
    // the data sections sit between the curated sections and the guidance
    expect(t.indexOf('## Key pages')).toBeLessThan(t.indexOf('## Pages assistants cite'));
    expect(t.indexOf('## Latest articles')).toBeLessThan(t.indexOf('## How AI assistants should cite'));
  });

  it('leaves a data section out when the site did not configure it, even with data', () => {
    const t = renderLlmsTxt(ctx, { summary: 'x' }, { citedPages: [{ path: '/a', referrals: 5, title: null }], latestArticles: [{ slug: 's', title: 't', day: '2026-01-01' }] });
    expect(t).not.toContain('## Pages assistants cite');
    expect(t).not.toContain('## Latest articles');
  });

  it('keeps database text from breaking the markdown', () => {
    const t = renderLlmsTxt(ctx, CONTENT, {
      citedPages: [{ path: '/guides/a (b)', referrals: 2, title: 'Line\nbreak ] and [ brackets' }],
    });
    expect(t).toContain('- [Line break and brackets](https://example.com/guides/a%20%28b%29)');
    expect(markdownUrl('https://x.test/a b(c)<d>[e]')).toBe('https://x.test/a%20b%28c%29%3Cd%3E%5Be%5D');
  });
});

describe('section queries', () => {
  it('fill in upstream\'s defaults and the site id', () => {
    expect(citedPagesQuery(ctx, CONTENT)).toEqual({
      sources: [...LLMS_CITED_SOURCES], days: 90, pathPrefixes: ['/guides/', '/best/'], minReferrals: 2, limit: 60,
      articlePathPrefix: '/guides/', siteId: 'kit-example',
    });
    expect(latestArticlesQuery(CONTENT)).toEqual({ days: 30, limit: 25 });
    expect(LLMS_CITED_SOURCES).toEqual(['chatgpt', 'perplexity', 'claude', 'copilot', 'gemini', 'kagi', 'deepseek', 'doubao']);
  });

  it('are off unless configured', () => {
    expect(citedPagesQuery(ctx, {})).toBeNull();
    expect(citedPagesQuery(ctx, { citedPages: { pathPrefixes: [] } })).toBeNull();
    expect(latestArticlesQuery({})).toBeNull();
  });
});

describe('LlmsDataSource caching (upstream lifetimes)', () => {
  it('caches a non-empty cited section 6h and a latest section 1h', async () => {
    let now = 0;
    const db = new FakeKitDb();
    db.citedRows = [{ path: '/guides/a', referrals: 3, title: 'A' }];
    db.latestRows = [{ slug: 'b', title: 'B', day: '2026-11-17' }];
    const src = new LlmsDataSource(db, ctx, CONTENT, () => now);
    expect((await src.load()).citedPages).toHaveLength(1);
    now = LLMS_LATEST_TTL_MS - 1;
    await src.load();
    expect([db.citedQueries.length, db.latestQueries.length]).toEqual([1, 1]);
    now = LLMS_LATEST_TTL_MS;
    await src.load();
    expect([db.citedQueries.length, db.latestQueries.length]).toEqual([1, 2]);
    now = LLMS_CITED_TTL_MS;
    await src.load();
    expect([db.citedQueries.length, db.latestQueries.length]).toEqual([2, 3]);
  });

  it('retries an empty or failed section after 10 minutes, not 6 hours', async () => {
    let now = 0;
    const db = new FakeKitDb();
    db.failReads = true;
    const src = new LlmsDataSource(db, ctx, CONTENT, () => now);
    expect(await src.load()).toEqual({ citedPages: [], latestArticles: [] });
    now = LLMS_EMPTY_TTL_MS - 1;
    await src.load();
    expect(db.citedQueries).toHaveLength(1);
    db.failReads = false;
    db.citedRows = [{ path: '/guides/a', referrals: 2, title: null }];
    now = LLMS_EMPTY_TTL_MS;
    expect((await src.load()).citedPages).toHaveLength(1);
    expect(db.citedQueries).toHaveLength(2);
  });

  it('shares one in-flight load between concurrent requests', async () => {
    const db = new FakeKitDb();
    const src = new LlmsDataSource(db, ctx, CONTENT, () => 0);
    await Promise.all([src.load(), src.load(), src.load()]);
    expect(db.citedQueries).toHaveLength(1);
    expect(db.latestQueries).toHaveLength(1);
  });

  it('gives up on a hung query after its budget', async () => {
    const hung = { aiCitedPages: () => new Promise<never>(() => undefined), latestArticles: () => new Promise<never>(() => undefined) };
    const src = new LlmsDataSource(hung, ctx, CONTENT, () => 0, 30);
    const started = Date.now();
    expect(await src.load()).toEqual({ citedPages: [], latestArticles: [] });
    expect(Date.now() - started).toBeLessThan(1000);
  });

  it('does not query when the database has no read methods', async () => {
    const src = new LlmsDataSource({}, ctx, CONTENT, () => 0);
    expect(await src.load()).toEqual({ citedPages: [], latestArticles: [] });
  });
});

describe('GET /llms.txt', () => {
  function app(db: FakeKitDb, content: LlmsTxtContent = CONTENT) {
    const log = captureLogger();
    const rt = createKitRuntime(ctx, { db, verifier: stubVerifier(), log, env: {} });
    const a = express();
    a.get('/llms.txt', llmsTxtHandler(rt, content));
    return { a, log };
  }

  it('serves plain UTF-8 text with the data sections', async () => {
    const db = new FakeKitDb();
    db.citedRows = [{ path: '/guides/a', referrals: 4, title: 'A guide' }];
    const res = await request(app(db).a).get('/llms.txt').expect(200);
    expect(res.headers['content-type']).toBe('text/plain; charset=utf-8');
    expect(res.headers['cache-control']).toBe('public, max-age=3600');
    expect(res.text).toContain('- [A guide](https://example.com/guides/a)');
    expect(db.citedQueries[0]).toMatchObject({ siteId: 'kit-example', articlePathPrefix: '/guides/' });
  });

  it('still serves the file when the database fails', async () => {
    const db = new FakeKitDb();
    db.failReads = true;
    const res = await request(app(db).a).get('/llms.txt').expect(200);
    expect(res.text.startsWith('# ExamplePicks\n')).toBe(true);
    expect(res.text).not.toContain('## Pages assistants cite');
  });
});
