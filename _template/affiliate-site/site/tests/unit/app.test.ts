import request from 'supertest';
import { esc, htmlCacheControl, type SiteContext } from '../../vendor/affiliate-kit/server/src';
import { AMAZON_CONTENT_NOTICE } from '../../vendor/affiliate-kit/server/src/notices';
import { kitClientBundleFromSource, type AiTrafficRow, type KitDb, type OutboundClickRow } from '../../vendor/affiliate-kit/server/src/http';
import { findSiteRoot, loadLlmsContent, loadSite, sitePaths } from '../../src/config/site';
import type { Article, ArticleStore, ArticleSummary } from '../../src/db/articles';
import { createApp, type SiteApp } from '../../src/index';
import { ROUTES, guidePath, nonPageRoutes, pageRouteEntries, pageRoutes } from '../../src/routes/table';
import { config as gateConfig } from '../ui/gate.config';

/**
 * The scaffold's app, end to end without a server or a database: the kit's
 * routes, every route in the table, the sitemaps and the 404, on in-memory
 * stores. The deploy gate (tests/run-local-suite.sh) runs the real image.
 */

const NOW = Date.parse('2026-11-17T12:00:00Z');
const paths = sitePaths(findSiteRoot(__dirname, {}));
const ctx: SiteContext = loadSite(paths, {});
const llms = loadLlmsContent(paths.llmsJson);

const ARTICLE: Article = {
  slug: 'how-we-compare-products',
  title: 'How We Compare Products',
  subtitle: 'Where the data comes from',
  excerpt: 'How this site compares products: specifications from the makers and prices checked on Amazon.com.',
  bodyMd: 'Intro.\n\n| What | Where |\n|---|---|\n| Specs | Makers |\n\nMore text.',
  heroImageUrl: null,
  category: 'guides',
  faqs: [{ question: 'Where do prices come from?', answer: 'From Amazon.com, with the time they were checked.' }],
  publishedAt: new Date('2026-11-15T12:00:00Z'),
  updatedAt: new Date('2026-11-16T12:00:00Z'),
};

class MemoryArticles implements ArticleStore {
  constructor(private readonly rows: Article[]) {}
  async latest(limit: number): Promise<ArticleSummary[]> {
    return this.rows.slice(0, limit);
  }
  async bySlug(slug: string): Promise<Article | null> {
    return this.rows.find((a) => a.slug === slug) ?? null;
  }
  async sitemapEntries(): Promise<Array<Pick<ArticleSummary, 'slug' | 'updatedAt'>>> {
    return this.rows.map((a) => ({ slug: a.slug, updatedAt: a.updatedAt }));
  }
}

class MemoryKitDb implements KitDb {
  readonly clicks: OutboundClickRow[] = [];
  readonly aiTraffic: AiTrafficRow[] = [];
  async insertOutboundClick(row: OutboundClickRow): Promise<void> {
    this.clicks.push(row);
  }
  async insertAiTraffic(row: AiTrafficRow): Promise<void> {
    this.aiTraffic.push(row);
  }
}

function build(articles: Article[] = [ARTICLE]): SiteApp & { db: MemoryKitDb } {
  const db = new MemoryKitDb();
  const site = createApp({
    ctx,
    kitDb: db,
    articles: new MemoryArticles(articles),
    llms,
    clientBundle: kitClientBundleFromSource('window.__kitClickBeacon=true;'),
    startVerifier: false,
    now: () => NOW,
    log: () => undefined,
  });
  return { ...site, db };
}

describe('pages', () => {
  const { app, kit } = build();
  afterAll(async () => {
    kit.stop();
    await kit.drain();
  });

  it.each(pageRouteEntries().filter((r) => !r.pattern.includes(':')).map((r) => r.pattern))('%s renders one page in the kit layout', async (p) => {
    const r = await request(app).get(p);
    expect(r.status).toBe(200);
    expect(r.headers['content-type']).toMatch(/^text\/html/);
    expect(r.headers['cache-control']).toBe(htmlCacheControl(ctx));
    expect(r.text.match(/<h1[\s>]/g)).toHaveLength(1);
    expect(r.text.match(/<main id="main"/g)).toHaveLength(1);
    expect(r.text).toContain(`<link rel="canonical" href="${ctx.site.canonicalOrigin}${p === '/' ? '/' : p}">`);
    expect(r.text).toContain('data-earnings-disclosure');
    expect(r.text).toMatch(/<meta name="description" content="[^"]{20,}">/);
    expect(r.text).toMatch(/<script defer src="\/kit\/client\.[0-9a-f]{12}\.js"><\/script>/);
  });

  it('an article renders its GFM table, FAQ and Article JSON-LD', async () => {
    const r = await request(app).get(guidePath(ARTICLE.slug));
    expect(r.status).toBe(200);
    expect(r.text).toContain('<table class="md-table">');
    expect(r.text).toContain('"@type":"Article"');
    expect(r.text).toContain('"@type":"FAQPage"');
    expect(r.text).toContain(esc(ctx.byline.name));
    // Every guide carries the 2(k) notice: its Markdown may quote an Amazon title.
    expect(r.text).toContain(AMAZON_CONTENT_NOTICE);
  });

  it('pages that by construction show no Amazon content leave the 2(k) notice out', async () => {
    for (const p of ['/about', '/privacy', ctx.disclosure.pagePath]) {
      expect((await request(app).get(p)).text).not.toContain(AMAZON_CONTENT_NOTICE);
    }
  });

  it('a missing article and an unknown path are 404 noindex pages', async () => {
    for (const p of [guidePath('no-such-guide'), '/no-such-page']) {
      const r = await request(app).get(p);
      expect(r.status).toBe(404);
      expect(r.headers['cache-control']).toBe('no-store');
      expect(r.text).toContain('<meta name="robots" content="noindex, follow">');
    }
  });

  it('the home page and the guides index list the published guides', async () => {
    for (const p of ['/', '/guides']) {
      const r = await request(app).get(p);
      expect(r.text).toContain(`href="${guidePath(ARTICLE.slug)}"`);
    }
  });
});

describe('kit routes', () => {
  const { app, kit } = build();
  afterAll(async () => {
    kit.stop();
    await kit.drain();
  });

  it('/health names this site', async () => {
    const r = await request(app).get('/health');
    expect(r.status).toBe(200);
    expect(r.body).toMatchObject({ status: 'ok', siteId: ctx.site.id });
  });

  it('robots.txt points at the sitemap and llms.txt starts with the brand', async () => {
    const robots = await request(app).get('/robots.txt');
    expect(robots.text).toContain(`Sitemap: ${ctx.site.canonicalOrigin}/sitemap.xml`);
    const llmsTxt = await request(app).get('/llms.txt');
    expect(llmsTxt.text.split('\n')[0]).toBe(`# ${ctx.site.brandName}`);
    expect(llmsTxt.text).toContain(ctx.disclosure.earnings);
  });

  it('the IndexNow key file answers with the key', async () => {
    const r = await request(app).get(`/${ctx.indexNow.key}.txt`);
    expect(r.status).toBe(200);
    expect(r.text.trim()).toBe(ctx.indexNow.key);
  });
});

describe('sitemaps', () => {
  it('list the static pages and every published guide, and nothing else', async () => {
    const { app, kit } = build();
    const index = await request(app).get('/sitemap.xml');
    expect(index.status).toBe(200);
    expect(index.text).toContain(`${ctx.site.canonicalOrigin}/sitemap-pages.xml`);
    expect(index.text).toContain(`${ctx.site.canonicalOrigin}/sitemap-guides.xml`);
    const pages = await request(app).get('/sitemap-pages.xml');
    for (const p of ['/', '/guides', '/about', '/disclosure', '/privacy']) {
      expect(pages.text).toContain(`<loc>${ctx.site.canonicalOrigin}${p}</loc>`);
    }
    const guides = await request(app).get('/sitemap-guides.xml');
    expect(guides.text).toContain(`<loc>${ctx.site.canonicalOrigin}${guidePath(ARTICLE.slug)}</loc>`);
    expect((await request(app).get('/sitemap-nothing.xml')).status).toBe(404);
    kit.stop();
  });

  it('a site with no guides yet still serves its page sitemap', async () => {
    const { app, kit } = build([]);
    const index = await request(app).get('/sitemap.xml');
    expect(index.status).toBe(200);
    expect(index.text).not.toContain('sitemap-guides.xml');
    kit.stop();
  });
});

describe('the route table', () => {
  it('declares each pattern once', () => {
    const patterns = ROUTES.map((r) => r.pattern);
    expect(new Set(patterns).size).toBe(patterns.length);
  });

  it('serves the disclosure page site.json names', () => {
    expect(pageRoutes()).toContain(ctx.disclosure.pagePath);
  });

  it('every page route has a family in the gate config, and every kit route a reason', () => {
    const covered = new Set(gateConfig.families.flatMap((f) => f.routes));
    expect(pageRoutes().filter((p) => !covered.has(p))).toEqual([]);
    for (const [pattern, reason] of Object.entries(nonPageRoutes())) expect(`${pattern}: ${reason}`).toMatch(/: \S/);
  });
});
