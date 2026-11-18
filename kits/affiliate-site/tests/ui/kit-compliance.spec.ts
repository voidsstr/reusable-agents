import { test, expect, type APIRequestContext, type Browser, type BrowserContext, type TestInfo } from '@playwright/test';
import { AMAZON_CONTENT_NOTICE, priceDisclaimerFor } from '../../server/src/notices';
import { KIT_DEFAULT_CRAWLER_USER_AGENTS } from '../../server/src/siteContext';
import {
  affiliateLinkProblems,
  availabilityTextProblems,
  cacheControlProblems,
  amazonImageProblems,
  amazonNoticeProblems,
  earningsDisclosureProblems,
  offerProblems,
  priceAgeLimitMs,
  priceDisplayProblems,
  priceTrackingProblems,
  recallProblems,
  sitemapImageProblems,
  tableProblems,
  type ComplianceRules,
  type ComplianceSnapshot,
} from './lib/compliance';
import { collectFrom, complianceSnapshot, type CollectComplianceArgs } from './lib/complianceCollect';
import { frameworkDirFor, runContentPolicyScan } from './lib/contentPolicyScan';
import { amazonBodyImageRows, amazonHeroRows, gateDbUrl, publishedArticleSlugs, publishedArticlesWithTables } from './lib/db';
import { loadGateConfig } from './lib/gateConfig';
import { llmsProblems } from './lib/llmsTxt';
import type { RecalledProduct, SourceContext } from './lib/manifestTypes';
import { newAuditContext } from './lib/pageAudit';
import { crawlerFloor, robotsProblems } from './lib/robotsTxt';
import { BASE_URL, SEED, allSitemapPaths, gateSite, seededSample, sitemapFiles, sitePath, targetsProduction } from './lib/site';
import { samplesFor } from './lib/sources';

// The Amazon Associates licence rules and the kit's own content rules, on
// every page family the site serves (kit build item K5; binding amendment 2).
// Nothing here can be waived per family: a red here is a compliance bug.
//
// Ported in part from SpecPicks tests/ui/amazon-cta.spec.ts (its compliance
// subset: tag, rel, target, price freshness, and the "As an Amazon Associate"
// disclosure on pages with CTAs, which here must also come before the first
// link), widened to the IP License 2(i)/2(h)/2(k) rules SpecPicks' SSR never
// enforced.
//
// Pages checked: every family's seeded sample, a seeded sample of every URL in
// the local sitemaps (compliance.maxSitemapPages), the config's extra pages,
// the recalled SKUs' pages and the pages that must render a table. Each page is
// loaded once per worker with JS off (what crawlers and assistants read).

test.describe.configure({ mode: 'parallel' });
test.skip(() => targetsProduction(), 'kit-compliance checks the production image on its local fixture (run-local-suite.sh), never production');

const cfg = loadGateConfig();
const site = gateSite();
const ctx = site.ctx;
const cc = cfg.compliance ?? {};
const SHARDS = Math.max(1, cc.shards ?? 2);
const RECALL_SELECTOR = cc.recallNoticeSelector ?? '[data-recall-notice]';
const ARTICLE_PATH = cc.articlePath ?? '/guides/{slug}';
const articlePath = (slug: string): string => ARTICLE_PATH.replace('{slug}', encodeURIComponent(slug));

const rules = (): ComplianceRules => ({
  allowedTags: ctx.amazon.allowedTags,
  marketplaceHost: ctx.amazon.marketplaceHost,
  priceDisclaimer: priceDisclaimerFor(ctx),
  amazonNotice: AMAZON_CONTENT_NOTICE,
  earningsDisclosure: ctx.disclosure.earnings,
  maxSharedCacheSeconds: ctx.cache.htmlSMaxAgeSeconds,
  maxBrowserCacheSeconds: ctx.cache.htmlMaxAgeSeconds,
  // 24h minus every HTML cache lifetime (binding amendment 2), not a flat 24h.
  quotableWindowMs: priceAgeLimitMs(ctx),
  now: Date.now(),
});

/** What the collector needs: the recall notice, every host a /r/ link may sit on, the earnings disclosure. */
const collectArgs = (): CollectComplianceArgs => ({
  recallSelector: RECALL_SELECTOR,
  siteHosts: [...site.siteHosts],
  earnings: ctx.disclosure.earnings,
});

// ── the page set, once per worker ───────────────────────────────────────

interface PageSet {
  paths: string[];
  /** Pages whose Markdown has a GFM table. */
  tablePaths: Set<string>;
  recalled: RecalledProduct[];
  notes: string[];
}

let pageSetPromise: Promise<PageSet> | null = null;

function pageSet(request: APIRequestContext): Promise<PageSet> {
  if (!pageSetPromise) {
    pageSetPromise = (async (): Promise<PageSet> => {
      const sctx: SourceContext = { request };
      const notes: string[] = [];
      const paths = new Set<string>();
      for (const fam of cfg.families) for (const p of await samplesFor(fam, request)) paths.add(p);
      const fromMaps = await allSitemapPaths(request, cfg.sitemaps?.index);
      const cap = cc.maxSitemapPages ?? 150;
      const mapped = seededSample(fromMaps, cap, 'kit-compliance:sitemaps');
      for (const p of mapped) paths.add(p);
      notes.push(`${mapped.length} of ${fromMaps.length} sitemap URLs (seed ${SEED})`);
      if (cc.extraPages) for (const p of await cc.extraPages(sctx)) paths.add(p);
      const recalled = cc.recalledProducts ? await cc.recalledProducts(sctx) : [];
      for (const r of recalled) paths.add(r.path);
      const tablePaths = new Set<string>();
      const db = gateDbUrl();
      if (cc.tablePages) for (const p of await cc.tablePages(sctx)) tablePaths.add(p);
      else if (db) for (const slug of await publishedArticlesWithTables(db)) tablePaths.add(articlePath(slug));
      for (const p of tablePaths) paths.add(p);
      return { paths: [...paths].sort(), tablePaths, recalled, notes };
    })();
    pageSetPromise.catch(() => { pageSetPromise = null; });
  }
  return pageSetPromise;
}

let titlesPromise: Promise<string[]> | null = null;
function knownTitles(request: APIRequestContext): Promise<string[]> {
  if (!titlesPromise) titlesPromise = cc.amazonTitles ? cc.amazonTitles({ request }) : Promise.resolve([]);
  return titlesPromise;
}

// ── snapshots, once per page per worker ─────────────────────────────────

const snapCache = new Map<string, Promise<ComplianceSnapshot>>();

function snapshot(bctx: BrowserContext, path: string): Promise<ComplianceSnapshot> {
  let p = snapCache.get(path);
  if (!p) {
    p = complianceSnapshot(bctx, path, collectArgs());
    snapCache.set(path, p);
    p.catch(() => snapCache.delete(path));
  }
  return p;
}

/** Run `check` over this shard's pages; returns every problem. */
async function overPages(
  browser: Browser,
  request: APIRequestContext,
  testInfo: TestInfo,
  shard: number,
  check: (s: ComplianceSnapshot, set: PageSet) => string[] | Promise<string[]>,
): Promise<string[]> {
  const set = await pageSet(request);
  expect(set.paths.length, 'pages to check (families, sitemaps, extras)').toBeGreaterThan(0);
  const mine = set.paths.filter((_, i) => i % SHARDS === shard);
  testInfo.annotations.push({ type: 'pages', description: `${mine.length} of ${set.paths.length} pages; ${set.notes.join('; ')}` });
  const bctx = await newAuditContext(browser, { js: false, view: 'phone' });
  const problems: string[] = [];
  try {
    for (const p of mine) problems.push(...(await check(await snapshot(bctx, p), set)));
  } finally {
    await bctx.close();
  }
  return problems;
}

function report(name: string, problems: string[]): string {
  return `${name}: ${problems.length} problem(s)\n  ${problems.slice(0, 60).join('\n  ')}${problems.length > 60 ? `\n  ... and ${problems.length - 60} more` : ''}\n`;
}

// ── per-page rules ──────────────────────────────────────────────────────

const PAGE_RULES: Array<{ title: string; check: (s: ComplianceSnapshot, set: PageSet, titles: string[]) => string[] }> = [
  {
    title: 'affiliate links carry rel="sponsored nofollow noopener", target=_blank and one of the site\'s own tags',
    check: (s) => affiliateLinkProblems(s, rules()),
  },
  {
    title: 'pages with affiliate links show the earnings disclosure in plain view, before the first link (Operating Agreement section 5)',
    check: (s) => earningsDisclosureProblems(s, rules()),
  },
  {
    title: 'prices and availability show only in a dated stamp (24h) with the exact 2(i) disclaimer, on pages cached no longer than the price window',
    check: (s) => [...priceDisplayProblems(s, rules()), ...availabilityTextProblems(s), ...cacheControlProblems(s, rules())],
  },
  {
    title: 'pages that show Amazon content carry the exact 2(k) notice in plain view',
    check: (s, _set, titles) => amazonNoticeProblems(s, rules(), titles),
  },
  {
    title: 'every Offer has validFrom within 24h (the quotable window), is an honest affiliate Offer and has a visible dated price',
    check: (s) => offerProblems(s, rules()),
  },
  {
    title: 'no price alerts, price tracking or "lowest ever" claims (Participation Requirements 6(y))',
    check: (s) => priceTrackingProblems(s),
  },
  {
    title: 'GFM tables render as table.md-table and no raw table Markdown shows',
    check: (s, set) => tableProblems(s, set.tablePaths.has(s.path)),
  },
];

for (const rule of PAGE_RULES) {
  for (let shard = 0; shard < SHARDS; shard++) {
    test(`kit-compliance: ${rule.title}${SHARDS > 1 ? ` [${shard + 1}/${SHARDS}]` : ''}`, async ({ browser, request }, testInfo) => {
      test.setTimeout(240_000);
      const titles = await knownTitles(request);
      const problems = await overPages(browser, request, testInfo, shard, (s, set) => rule.check(s, set, titles));
      expect(problems, report(rule.title, problems)).toEqual([]);
    });
  }
}

// ── images: pages and the stored heroes ─────────────────────────────────

for (let shard = 0; shard < SHARDS; shard++) {
  test(`kit-compliance: no Amazon CDN image as og:image, twitter:image, an article hero, a JSON-LD image or a sitemap image${SHARDS > 1 ? ` [${shard + 1}/${SHARDS}]` : ''}`, async ({ browser, request }, testInfo) => {
    test.setTimeout(240_000);
    const problems = await overPages(browser, request, testInfo, shard, (s) => amazonImageProblems(s));
    if (shard === 0) {
      // The sitemaps never carry an Amazon image (<image:loc>).
      const index = cfg.sitemaps?.index ?? '/sitemap.xml';
      for (const f of [index.replace(/^\//, ''), ...(await sitemapFiles(request, index))]) {
        const r = await request.get(`${BASE_URL}/${f}`, { timeout: 60_000 });
        if (r.status() === 200) problems.push(...sitemapImageProblems(f, await r.text()));
      }
      // What is stored, once (IP License 2(h): never store an Amazon image): the hero column and article bodies.
      const db = gateDbUrl();
      if (db) {
        for (const r of await amazonHeroRows(db)) problems.push(`editorial_articles ${r.slug} (${r.status}): hero_image_url on an Amazon host: ${r.heroImageUrl}`);
        for (const r of await amazonBodyImageRows(db)) problems.push(`editorial_articles ${r.slug} (${r.status}): body_md stores an Amazon image link${r.url ? `: ${r.url.slice(0, 120)}` : ''}`);
      } else {
        testInfo.annotations.push({ type: 'db-check-skipped', description: 'KIT_GATE_DB_URL is not set: editorial_articles.hero_image_url and body_md were not read' });
      }
    }
    expect(problems, report('Amazon images', problems)).toEqual([]);
  });
}

// ── recalls ─────────────────────────────────────────────────────────────

for (let shard = 0; shard < SHARDS; shard++) {
  test(`kit-compliance: a recalled SKU has no buy button anywhere, and its own page says why${SHARDS > 1 ? ` [${shard + 1}/${SHARDS}]` : ''}`, async ({ browser, request }, testInfo) => {
    test.setTimeout(240_000);
    // Never cut (binding amendment 11): the config names its recall source, or says why it has none.
    if (!cc.recalledProducts && cc.recallsWaived?.trim()) {
      testInfo.annotations.push({ type: 'waiver', description: `recalls: ${cc.recallsWaived}` });
      test.skip(true, `recalls waived: ${cc.recallsWaived}`);
      return;
    }
    expect(cc.recalledProducts, 'compliance.recalledProducts (or compliance.recallsWaived with the reason)').toBeDefined();
    const set = await pageSet(request);
    expect(set.recalled.length, 'recalled fixture SKUs (compliance.recalledProducts): the fixture must hold at least one').toBeGreaterThan(0);
    testInfo.annotations.push({ type: 'recalled', description: set.recalled.map((r) => `${r.asin} ${r.path}${r.name ? ` (${r.name})` : ''}`).join(', ') });
    const problems = await overPages(browser, request, testInfo, shard, (s) => recallProblems(s, set.recalled));
    if (shard === 0) {
      const want = cc.recalledStatus ?? 200;
      for (const r of set.recalled) {
        const page = await request.get(`${BASE_URL}${r.path}`, { maxRedirects: 0 });
        if (page.status() !== want) problems.push(`${r.path}: HTTP ${page.status()}, expected ${want}`);
        // The click redirect must not send a reader to Amazon for it either.
        const hop = await request.get(`${BASE_URL}/r/${r.asin}`, { maxRedirects: 0 });
        const loc = hop.headers()['location'] || '';
        if (hop.status() >= 300 && hop.status() < 400) {
          let host = '';
          try { host = new URL(loc, BASE_URL).hostname; } catch { host = ''; }
          if (/(^|\.)amazon\.|(^|\.)amzn\.|(^|\.)a\.co$/i.test(host)) problems.push(`/r/${r.asin} redirects a recalled SKU to Amazon: ${loc}`);
        } else if (hop.status() === 200) {
          problems.push(`/r/${r.asin} answers 200 instead of redirecting to the product page`);
        }
      }
    }
    expect(problems, report('recalled SKUs', problems)).toEqual([]);
  });
}

// ── robots.txt and llms.txt ─────────────────────────────────────────────

test('kit-compliance: robots.txt lets search engines and assistants read the money pages and blocks training crawlers', async ({ request }, testInfo) => {
  const resp = await request.get(`${BASE_URL}/robots.txt`);
  expect(resp.status(), '/robots.txt').toBe(200);
  expect(resp.headers()['content-type'] || '', '/robots.txt content type').toMatch(/^text\/plain/);
  const keyPaths = cc.robotsKeyPaths
    ? await cc.robotsKeyPaths({ request })
    : ['/', ...(await Promise.all(cfg.families.filter((f) => (f.status ?? 200) === 200).map(async (f) => (await samplesFor(f, request))[0])))]
      .filter((p): p is string => !!p);
  const trainingWaived = cc.trainingCrawlersAllowed;
  if (trainingWaived) testInfo.annotations.push({ type: 'exception', description: `training crawlers allowed: ${trainingWaived}` });
  testInfo.annotations.push({ type: 'key-paths', description: [...new Set(keyPaths)].join(', ') });
  // The kit's crawler lists are the floor: site.json can add tokens or move one to another class, never drop one.
  const floor = crawlerFloor(ctx.crawlers.userAgents, KIT_DEFAULT_CRAWLER_USER_AGENTS);
  if (floor.reclassified.length) testInfo.annotations.push({ type: 'reclassified', description: `site.json moves kit crawler tokens: ${floor.reclassified.join('; ')}` });
  const problems = robotsProblems(await resp.text(), {
    mustAllow: [...floor.byClass.search, ...floor.byClass.agent],
    mustBlock: trainingWaived ? [] : floor.byClass.training,
    keyPaths: [...new Set(keyPaths.map((p) => p.split('?')[0] as string))],
    disallowPaths: ctx.crawlers.disallowPaths,
    sitemapUrl: `${ctx.site.canonicalOrigin}${cfg.sitemaps?.index ?? '/sitemap.xml'}`,
  });
  expect(problems, report('robots.txt', problems)).toEqual([]);
});

test('kit-compliance: llms.txt is present, well formed, price-free, and its site links resolve', async ({ request }) => {
  const resp = await request.get(`${BASE_URL}/llms.txt`);
  expect(resp.status(), '/llms.txt').toBe(200);
  expect(resp.headers()['content-type'] || '', '/llms.txt content type').toMatch(/^text\/plain/);
  const { problems, siteLinks } = llmsProblems(await resp.text(), {
    brandName: ctx.site.brandName,
    earnings: ctx.disclosure.earnings,
    canonicalOrigin: ctx.site.canonicalOrigin,
  });
  for (const link of siteLinks.slice(0, 40)) {
    const p = sitePath(link);
    if (!p) continue;
    const r = await request.get(`${BASE_URL}${p}`, { maxRedirects: 0 });
    if (r.status() !== 200) problems.push(`llms.txt links ${link}, which answers HTTP ${r.status()} locally`);
  }
  expect(problems, report('llms.txt', problems)).toEqual([]);
});

// ── content policy over the rendered pages ──────────────────────────────

test('kit-compliance: rendered pages pass the content policy scan', async ({ request }, testInfo) => {
  test.setTimeout(300_000);
  const cp = cc.contentPolicy;
  // Never cut (binding amendment 11): rule sets, or the stated reason there are none.
  if ((!cp || !cp.ruleSets.length) && cc.contentPolicyWaived?.trim()) {
    testInfo.annotations.push({ type: 'waiver', description: `content policy: ${cc.contentPolicyWaived}` });
    test.skip(true, `content policy waived: ${cc.contentPolicyWaived}`);
    return;
  }
  expect(cp?.ruleSets.length ?? 0, 'compliance.contentPolicy.ruleSets (or compliance.contentPolicyWaived with the reason)').toBeGreaterThan(0);
  if (!cp) return;
  const frameworkDir = frameworkDirFor(cp.frameworkDir);
  expect(frameworkDir, 'a reusable-agents checkout with framework/cli/content_policy_scan.py (set KIT_GATE_FRAMEWORK_DIR)').not.toBeNull();
  const set = await pageSet(request);
  const paths = new Set(set.paths);
  const db = gateDbUrl();
  if (db) for (const slug of await publishedArticleSlugs(db)) paths.add(articlePath(slug));
  if (cp.extraPages) for (const p of await cp.extraPages({ request })) paths.add(p);
  const pages: Array<{ name: string; html: string }> = [];
  for (const p of [...paths].sort()) {
    const r = await request.get(`${BASE_URL}${p}`);
    if (r.status() === 200 && /text\/html/.test(r.headers()['content-type'] || '')) pages.push({ name: p, html: await r.text() });
  }
  testInfo.annotations.push({ type: 'pages', description: `${pages.length} rendered pages; rule sets ${cp.ruleSets.join(', ')}` });
  const result = await runContentPolicyScan(pages, {
    ruleSets: cp.ruleSets,
    configFile: cp.configFile,
    site: cp.site,
    frameworkDir: frameworkDir as string,
    python: cp.python,
    workDir: testInfo.outputPath('content-policy'),
  });
  expect(result.problems, report(`content policy (scanner exit ${result.exitCode})`, result.problems)).toEqual([]);
  expect(result.exitCode, `content_policy_scan exit code (stderr: ${result.stderr.trim().slice(-400)})`).toBe(0);
  expect(result.scanned, 'pages the scanner read').toBe(pages.length);
});

// ── the detectors catch what they must, and nothing else ────────────────
// Every rule above runs on the fixture's clean pages, where a detector that
// silently matches nothing would pass forever. This renders one document with
// a violation of each kind and one clean document, and checks both ways.

const FIXTURE_ORIGIN = 'http://compliance.fixture';

test('kit-compliance detectors: catch each violation in a fixture document, pass a clean one', async ({ browser }) => {
  const r = rules();
  const tag = ctx.amazon.partnerTag;
  const canonical = ctx.site.canonicalOrigin;
  const now = new Date(r.now - 2 * 3600 * 1000).toISOString();
  // Past the quotable window (24h minus the cache lifetimes) but inside 24h: only the window rule catches it.
  const edge = new Date(r.now - Math.round(((r.quotableWindowMs as number) + 24 * 3600 * 1000) / 2)).toISOString();
  const disclaimer = r.priceDisclaimer;
  const earnings = ctx.disclosure.earnings;
  const longTitle = 'Contoso Volt 1000 Widget with Four AC Outlets and Pass-Through Charging, 1024Wh';
  const goodStamp = `<span data-price-stamp="price"><span class="ssr-price">$129.00</span> on Amazon.com as of <time datetime="${now}">x</time> <button popovertarget="pd1">Details</button> <span id="pd1" popover>${disclaimer}</span></span>`;
  const clean = `<!doctype html><html><head><meta charset="utf-8"><title>Clean page</title><meta name="description" content="A clean page about widgets under $500.">
    <meta property="og:title" content="Clean page">
    <meta property="og:image" content="https://cdn.example.org/card.png">
    <script type="application/ld+json">{"@context":"https://schema.org","@type":"Product","name":"W","sku":"B0FIXCLEAN","image":"https://cdn.example.org/w.png","offers":{"@type":"Offer","url":"${FIXTURE_ORIGIN}/p","priceCurrency":"USD","price":"129.00","validFrom":"${now}"}}</script>
    </head><body><main>
    <h1>Clean</h1>
    <p data-earnings-disclosure>${earnings}</p>
    <p><span data-amazon-content>Widget Title From Amazon, 512Wh, Gray</span> ${goodStamp}</p>
    <p><a href="https://www.amazon.com/dp/B0FIXCLEAN?tag=${tag}&amp;linkCode=ll1" rel="sponsored nofollow noopener" target="_blank" data-cta-placement="pdp-hero">View</a></p>
    <p><a href="${canonical}/r/B0FIXCLEAN" rel="sponsored nofollow">Through our redirect</a></p>
    <p>We do not offer price alerts or a price history, and we never claim a lowest-ever price.</p>
    <p>The best widgets for under $500, and the $1,000 tier.</p>
    <p data-kit-not-a-price>Electricity costs about $0.15 per kWh, so it costs $40 a year to run.</p>
    <p>See current price and availability on Amazon.com.</p>
    <div class="md-table-wrap"><table class="md-table"><thead><tr><th scope="col">A</th><th scope="col">B</th></tr></thead><tbody><tr><td>1</td><td>2</td></tr></tbody></table></div>
    <p data-amazon-notice="2k">${AMAZON_CONTENT_NOTICE}</p>
    </main></body></html>`;
  const dirty = `<!doctype html><html><head><meta charset="utf-8"><title>Only $349.99 on Amazon</title>
    <meta property="og:title" content="Widget review: from $449">
    <meta property="product:price:amount" content="349.00">
    <meta property="og:image" content="https://m.media-amazon.com/images/I/x.jpg">
    <script type="application/ld+json">{"@context":"https://schema.org","@type":"Product","name":"W","sku":"B0FIXRECAL","image":"https://m.media-amazon.com/images/I/p.jpg","aggregateRating":{"@type":"AggregateRating","ratingValue":"5","reviewCount":"1"},"offers":{"@type":"Offer","url":"${FIXTURE_ORIGIN}/p","priceCurrency":"USD","price":"129.00","validFrom":"2026-01-02T00:00:00Z","seller":{"@type":"Organization","name":"Us"}}}</script>
    </head><body><main>
    <h1>Dirty</h1>
    <p>Amazon lists it as Widget Title From Amazon, 512Wh, Gray.</p>
    <p><span data-price-stamp="price"><span class="ssr-price">$129.00</span> as of <time datetime="2026-01-02T00:00:00Z">x</time></span></p>
    <p><span data-price-stamp="price"><span class="ssr-price">$139.00</span> as of <time datetime="${now}">x</time> <span style="display:none">${disclaimer}</span></span></p>
    <p><span data-price-stamp="price"><span class="ssr-price">$149.00</span> as of <time datetime="${edge}">x</time> <button popovertarget="pd2">Details</button> <span id="pd2" popover>${disclaimer}</span></span></p>
    <p>The widget is $349.99 right now.</p>
    <p>It sells for $449 at the moment, and the full kit totals $1,249.</p>
    <p>In stock on Amazon today, and only 2 left.</p>
    <p><a href="https://www.amazon.com/dp/B0FIXCLEAN?tag=someoneelse-20">Buy</a> <a href="https://amzn.to/abc" rel="sponsored nofollow noopener" target="_blank">Short</a></p>
    <p><a href="https://www.amazon.com/dp/B0FIXRECAL?tag=${tag}" rel="sponsored nofollow noopener" target="_blank">Recalled</a></p>
    <p><a href="${canonical}/r/B0FIXRECAL" rel="sponsored nofollow">Recalled, through our redirect</a></p>
    <p><a href="https://www.amazon.com/gp/aws/cart/add.html?ASIN.1=B0FIXCLEAN&amp;ASIN.2=B0FIXRECAL&amp;tag=${tag}" rel="sponsored nofollow noopener" target="_blank">Add both to cart</a></p>
    <p><a href="https://www.amazon.com/s?k=Recalled+Widget+X+charger&amp;tag=${tag}" rel="sponsored nofollow noopener" target="_blank">Search</a></p>
    <p>It is at its lowest price ever.</p>
    <p>Never miss a deal: set a price alert for this widget.</p>
    <p>No other store beats this all-time low.</p>
    <p>Get notified when the price drops.</p>
    <p>Track this price and we will email you.</p>
    <p>Listed as Contoso Volt 1000 Widget with Four AC Outlets and…</p>
    <form action="/api/price-alert"><input name="price_alert_email"><button>Set a price alert</button></form>
    <p>| Model | Wh | Price |</p><p>| --- | --- | --- |</p>
    <img data-hero-image src="https://images-na.ssl-images-amazon.com/images/I/y.jpg" alt="">
    </main></body></html>`;

  // The disclosure is shown, but only after the first affiliate link (a footer-only disclosure).
  const late = `<!doctype html><html><head><meta charset="utf-8"><title>Late disclosure</title></head><body><main>
    <h1>Late</h1>
    <p><a href="https://www.amazon.com/dp/B0FIXCLEAN?tag=${tag}" rel="sponsored nofollow noopener" target="_blank">View</a></p>
    </main><footer><p>${earnings}</p></footer></body></html>`;
  const docs: Record<string, string> = { '/clean': clean, '/dirty': dirty, '/late': late };

  const bctx = await browser.newContext({ javaScriptEnabled: false });
  await bctx.route('**/*', (route) => {
    const u = new URL(route.request().url());
    if (u.origin !== FIXTURE_ORIGIN) return route.fulfill({ status: 200, contentType: 'image/svg+xml', body: '<svg xmlns="http://www.w3.org/2000/svg"/>' });
    return route.fulfill({ status: 200, contentType: 'text/html; charset=utf-8', body: docs[u.pathname] ?? dirty });
  });
  const take = async (p: string): Promise<ComplianceSnapshot> => {
    const page = await bctx.newPage();
    try {
      await page.goto(`${FIXTURE_ORIGIN}${p}`);
      const headers: Record<string, string> = p === '/clean'
        ? { 'cache-control': 'public, max-age=60, s-maxage=600' }
        : { 'cache-control': 'public, max-age=3600, s-maxage=86400, stale-while-revalidate=600', 'cdn-cache-control': 'max-age=86400' };
      return await collectFrom(page, collectArgs(), { path: p, status: 200, headers });
    } finally {
      await page.close();
    }
  };
  const ok = await take('/clean');
  const bad = await take('/dirty');
  const lateSnap = await take('/late');
  await bctx.close();

  // Where the disclosure falls against the first link is read from the rendered page, both ways.
  expect(ok.earningsBeforeFirstLink, 'the clean document shows the disclosure before its first affiliate link').toBe(true);
  expect(lateSnap.earningsBeforeFirstLink, 'the late document shows it only after').toBe(false);
  expect(earningsDisclosureProblems(lateSnap, r).join('\n')).toMatch(/the earnings disclosure first shows after the first affiliate link/);
  expect(bad.earningsBeforeFirstLink, 'no disclosure at all: nothing to order').toBeNull();

  const titles = ['Widget Title From Amazon, 512Wh, Gray', longTitle];
  const recalled = [{ asin: 'B0FIXRECAL', path: '/dirty', name: 'Recalled Widget X' }];
  const all = (s: ComplianceSnapshot): string[] => [
    ...affiliateLinkProblems(s, r), ...earningsDisclosureProblems(s, r), ...priceDisplayProblems(s, r), ...availabilityTextProblems(s),
    ...amazonNoticeProblems(s, r, titles), ...offerProblems(s, r), ...priceTrackingProblems(s), ...amazonImageProblems(s),
    ...tableProblems(s), ...recallProblems(s, recalled), ...cacheControlProblems(s, r),
  ];
  expect(all(ok), 'the clean document').toEqual([]);
  // A truncated Amazon title alone is still Amazon content.
  const truncatedOnly: ComplianceSnapshot = { ...ok, amazonMarkers: 0, stamps: [], visibleText: 'Listed as Contoso Volt 1000 Widget with Four AC Outlets and…' };
  expect(amazonNoticeProblems(truncatedOnly, r, titles).join('\n'), 'a truncated title').toMatch(/the Amazon title "Contoso Volt 1000 Widget with Four AC\.\.\." \(truncated\)/);

  const esc = (v: string): string => v.replace(/[.*+?^${}()|[\]\\/]/g, '\\$&');
  const found = all(bad).join('\n');
  for (const [what, re] of [
    ['foreign tag', /tag "someoneelse-20" is not one of the site's tracking IDs/],
    ['missing rel', /lacks sponsored, nofollow, noopener/],
    ['short link', /amzn\.to cannot carry the site's tag/],
    ['no earnings disclosure', /visible affiliate link\(s\) but the earnings disclosure .* is not in plain view/],
    ['stale stamp', /checked 2026-01-02T00:00:00\.000Z.*older than 24h/],
    ['stamp past the quotable window', new RegExp(`checked ${esc(edge)}, .*h ago \\(past the .*h quotable window`)],
    ['stamp without 2(i)', /without the exact 2\(i\) disclaimer/],
    ['hidden 2(i) disclaimer', /\$139\.00.*without the exact 2\(i\) disclaimer .*\(it is in the stamp but hidden\)/],
    ['orphan price', /price outside a dated stamp: "The widget is \$349\.99 right now\."/],
    ['whole-dollar price', /price outside a dated stamp: "It sells for \$449 at the moment, and the full kit totals \$1,249\."/],
    ['price in <title>', /price in <title>/],
    ['price in og:title', /price in og:title .*from \$449/],
    ['price meta tag', /price meta tag product:price:amount="349\.00"/],
    ['availability outside a stamp', /availability outside a dated stamp \(stock status\): "In stock on Amazon today, and only 2 left\."/],
    ['units left', /availability outside a dated stamp \(units left\)/],
    ['missing 2(k)', /shows Amazon content \(.+\) but not the 2\(k\) notice/],
    ['stale Offer', /validFrom 2026-01-02T00:00:00Z is .*h old/],
    ['seller on Offer', /carries seller/],
    ['AggregateRating', /AggregateRating/],
    ['lowest ever', /lowest ever copy: "It is at its lowest price ever\."/],
    ['never miss a deal', /price alert copy: "Never miss a deal: set a price alert for this widget\."/],
    ['no other store', /all-time low copy: "No other store beats this all-time low\."/],
    ['get notified', /price-drop notification copy: "Get notified when the price drops\."/],
    ['track this price', /price tracking copy: "Track this price and we will email you\."/],
    ['price-alert form', /price-tracking UI \(form action \/api\/price-alert/],
    ['Amazon og:image', /og:image on an Amazon host/],
    ['Amazon hero', /hero image on an Amazon host/],
    ['Amazon Product image', /Product JSON-LD image on an Amazon host: https:\/\/m\.media-amazon\.com\/images\/I\/p\.jpg/],
    ['raw table', /unrendered Markdown table text/],
    ['recalled buy button', /buy button for recalled B0FIXRECAL: https:\/\/www\.amazon\.com\/dp\/B0FIXRECAL/],
    ['recalled /r/ link on the canonical origin', new RegExp(`buy button for recalled B0FIXRECAL: ${esc(canonical)}/r/B0FIXRECAL`)],
    ['recalled ASIN.2 in a cart link', /buy button for recalled B0FIXRECAL: https:\/\/www\.amazon\.com\/gp\/aws\/cart\/add\.html/],
    ['Amazon search for a recalled model', /Amazon search for recalled B0FIXRECAL \("Recalled Widget X"\)/],
    ['long shared cache', /may be cached 86400s by a shared cache/],
    ['long browser cache', /a browser may keep it 3600s/],
    ['CDN-Cache-Control', /CDN-Cache-Control lets a CDN keep it 86400s/],
    ['stale-while-revalidate', /has stale-while-revalidate/],
  ] as Array<[string, RegExp]>) {
    expect(re.test(found), `detector missed: ${what}\nfound:\n${found}`).toBe(true);
  }
});
