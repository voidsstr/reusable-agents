/**
 * SiteContext: site.json is validated against schema/site.schema.json, kit
 * defaults fill the gaps, cross-field rules hold, derived limits are right,
 * and the result cannot be mutated at runtime.
 */
import * as fs from 'fs';
import * as path from 'path';
import {
  CRAWLER_CLASSES,
  EDGE_CDN_CHOICES,
  INDEXING_MODES,
  KIT_DEFAULTS,
  KIT_DEFAULT_CRAWLER_USER_AGENTS,
  KIT_DEFAULT_SOURCES,
  SITE_SCHEMA,
  SiteContextError,
  SUPPORTED_SCHEMA_KEYWORDS,
  THEME_CONTRAST_PAIRS,
  contrastRatio,
  htmlCacheControl,
  loadSiteContext,
  parseSiteContext,
  themeCss,
  unsupportedSchemaKeywords,
  validateAgainstSchema,
  validateSiteJson,
  type SchemaNode,
  type SiteJson,
} from '../server/src/siteContext';
import { CDN_EDGE_RANGES, KIT_DEFAULT_STATIC_RANGES } from '../server/src/aiCrawlerVerify';

const KIT = path.resolve(__dirname, '..');
const EXAMPLE = path.join(KIT, 'fixtures/site.example.json');
const SCHEMA_FILE = path.join(KIT, 'schema/site.schema.json');

function example(): SiteJson {
  return JSON.parse(fs.readFileSync(EXAMPLE, 'utf8')) as SiteJson;
}

/** A copy of the example with `edit` applied, parsed with an empty env. */
function parseWith(edit: (j: any) => void) {
  const j: any = example();
  edit(j);
  return parseSiteContext(j, { env: {} });
}

function issuesOf(edit: (j: any) => void): string[] {
  try {
    parseWith(edit);
  } catch (e) {
    if (e instanceof SiteContextError) return [...e.issues];
    throw e;
  }
  return [];
}

function minimal(): SiteJson {
  const j = example();
  return {
    schemaVersion: 1,
    site: { id: j.site.id, brandName: j.site.brandName, domain: j.site.domain, canonicalOrigin: j.site.canonicalOrigin },
    amazon: { partnerTag: j.amazon.partnerTag },
    disclosure: { earnings: j.disclosure.earnings },
    byline: { name: j.byline.name },
    theme: { light: j.theme.light, dark: j.theme.dark },
    indexNow: { key: j.indexNow.key },
  };
}

describe('the embedded schema', () => {
  it('is identical to schema/site.schema.json', () => {
    expect(SITE_SCHEMA).toEqual(JSON.parse(fs.readFileSync(SCHEMA_FILE, 'utf8')));
  });

  it('uses only keywords the validator enforces or knowingly ignores', () => {
    expect(unsupportedSchemaKeywords(SITE_SCHEMA as SchemaNode)).toEqual([]);
    expect(SUPPORTED_SCHEMA_KEYWORDS.has('patternProperties')).toBe(false);
    expect(unsupportedSchemaKeywords({ type: 'object', patternProperties: {} })).toEqual(['#/patternProperties']);
  });

  it('documents the same defaults the loader applies', () => {
    const d = (SITE_SCHEMA as any).$defs;
    expect(d.site.properties.language.default).toBe(KIT_DEFAULTS.language);
    expect(d.routes.properties.product.default).toBe(KIT_DEFAULTS.productRoute);
    expect(d.amazon.properties.marketplaceHost.default).toBe(KIT_DEFAULTS.marketplaceHost);
    expect(d.amazon.properties.siteName.default).toBe(KIT_DEFAULTS.siteName);
    expect(d.amazon.properties.currency.default).toBe(KIT_DEFAULTS.currency);
    expect(d.amazon.properties.acceptSegmentTagParam.default).toBe(KIT_DEFAULTS.acceptSegmentTagParam);
    expect(d.amazon.properties.ctaLabel.default).toBe(KIT_DEFAULTS.ctaLabel);
    expect(d.amazon.properties.searchCtaLabel.default).toBe(KIT_DEFAULTS.searchCtaLabel);
    expect(d.disclosure.properties.pagePath.default).toBe(KIT_DEFAULTS.disclosurePagePath);
    expect(d.theme.properties.fontFamily.default).toBe(KIT_DEFAULTS.fontFamily);
    expect(d.crawlers.properties.disallowPaths.default).toEqual(KIT_DEFAULTS.disallowPaths);
    expect(d.cache.properties.htmlSMaxAgeSeconds.default).toBe(KIT_DEFAULTS.htmlSMaxAgeSeconds);
    expect(d.cache.properties.htmlMaxAgeSeconds.default).toBe(KIT_DEFAULTS.htmlMaxAgeSeconds);
    expect(d.cache.properties.ssrCacheSeconds.default).toBe(KIT_DEFAULTS.ssrCacheSeconds);
    expect(d.prices.properties.freshnessHours.default).toBe(KIT_DEFAULTS.freshnessHours);
    expect(d.edge.properties.cdn.default).toBe(KIT_DEFAULTS.cdn);
    expect(d.edge.properties.cdn.enum).toEqual(EDGE_CDN_CHOICES);
    expect(d.edge.properties.extraTrustedProxies.default).toEqual([]);
    const top = (SITE_SCHEMA as any).properties;
    expect(top.indexing.default).toBe(KIT_DEFAULTS.indexing);
    expect(top.indexing.default).toBe('open');
    expect(top.indexing.enum).toEqual(INDEXING_MODES);
    expect(top.redirectFromHosts.default).toEqual([]);
    // Every CDN site.json can name has kit edge ranges.
    for (const cdn of EDGE_CDN_CHOICES) if (cdn !== 'none') expect(CDN_EDGE_RANGES[cdn].cidrs.length).toBeGreaterThan(0);
  });

  it('caps s-maxage at 600 and the freshness window at 24h', () => {
    const d = (SITE_SCHEMA as any).$defs;
    expect(d.cache.properties.htmlSMaxAgeSeconds.maximum).toBe(600);
    expect(d.prices.properties.freshnessHours.maximum).toBe(24);
  });
});

describe('the example site.json', () => {
  const ctx = loadSiteContext(EXAMPLE, { env: {} });

  it('loads, with the values it states', () => {
    expect(validateSiteJson(example())).toEqual([]);
    expect(ctx.site).toEqual({
      id: 'kit-example', brandName: 'ExamplePicks', domain: 'example.com', canonicalOrigin: 'https://example.com', language: 'en-US',
    });
    expect(ctx.amazon.partnerTag).toBe('kitexample-20');
    expect(ctx.amazon.siteName).toBe('Amazon.com');
    expect(ctx.amazon.marketplaceHost).toBe('www.amazon.com');
    expect(ctx.meta.loadedFrom).toBe(EXAMPLE);
  });

  it('derives the allowed tag set, the HTML cache age and the quotable price window', () => {
    expect(ctx.amazon.allowedTags).toEqual(['kitexample-20', 'kitexample-guides-20', 'kitexample-pdp-20', 'kitexample-sticky-20']);
    expect(ctx.cache).toEqual({ htmlSMaxAgeSeconds: 600, htmlMaxAgeSeconds: 60, ssrCacheSeconds: 0, maxHtmlCacheAgeSeconds: 660 });
    expect(ctx.prices).toEqual({ freshnessHours: 24, quotableMaxAgeSeconds: 24 * 3600 - 660 });
  });

  it('carries the kit-default static AI-crawler ranges first (Anthropic outbound)', () => {
    expect(ctx.aiCrawlers.staticRanges[0]).toEqual(KIT_DEFAULT_STATIC_RANGES[0]);
    expect(ctx.aiCrawlers.staticRanges[0]!.cidrs).toEqual(['160.79.104.0/21', '153.61.208.0/20']);
  });

  it('has no CDN in front unless site.json names one', () => {
    expect(ctx.edge).toEqual({ cdn: 'none', trustedProxyCidrs: [] });
    expect(Object.isFrozen(ctx.edge.trustedProxyCidrs)).toBe(true);
  });

  it('is deep-frozen, and tag maps have no prototype to leak through', () => {
    expect(Object.isFrozen(ctx)).toBe(true);
    expect(Object.isFrozen(ctx.amazon.placementTags)).toBe(true);
    expect(Object.isFrozen(ctx.theme.light)).toBe(true);
    expect(Object.isFrozen(ctx.crawlers.userAgents.training)).toBe(true);
    expect(() => { (ctx.amazon as any).partnerTag = 'thief-20'; }).toThrow(TypeError);
    expect(Object.getPrototypeOf(ctx.amazon.placementTags)).toBeNull();
    expect(Object.getPrototypeOf(ctx.amazon.sourceTags)).toBeNull();
    expect((ctx.amazon.placementTags as any).constructor).toBeUndefined();
  });

  it('kit defaults are frozen too, so one consumer cannot change them for every site in the process', () => {
    for (const v of [KIT_DEFAULTS, KIT_DEFAULTS.disallowPaths, KIT_DEFAULT_SOURCES, KIT_DEFAULT_SOURCES[0], KIT_DEFAULT_CRAWLER_USER_AGENTS,
      KIT_DEFAULT_CRAWLER_USER_AGENTS.training, CRAWLER_CLASSES, SITE_SCHEMA, KIT_DEFAULT_STATIC_RANGES, KIT_DEFAULT_STATIC_RANGES[0]!.cidrs]) {
      expect(Object.isFrozen(v)).toBe(true);
    }
    expect(() => { (KIT_DEFAULT_CRAWLER_USER_AGENTS.training as string[]).push('NewBot'); }).toThrow(TypeError);
  });

  it('gives the HTML Cache-Control header from the same numbers', () => {
    expect(htmlCacheControl(ctx)).toBe('public, max-age=60, s-maxage=600');
    expect(htmlCacheControl(ctx)).not.toMatch(/stale-while-revalidate/);
  });

  it('renders theme custom properties for light and dark', () => {
    const css = themeCss(ctx.theme);
    expect(css).toContain('--kit-cta-bg:#f59e0b');
    expect(css).toContain('--kit-text:#0f172a');
    expect(css).toContain('@media (prefers-color-scheme: dark){:root{--kit-bg:#0b1120');
    expect(css).toContain('--kit-font:system-ui');
  });
});

describe('kit defaults fill a minimal site.json', () => {
  const ctx = parseSiteContext(minimal(), { env: {} });

  it('applies route, label, cache, price and crawler defaults', () => {
    expect(ctx.routes.product).toBe('/product/{asin}');
    expect(ctx.amazon.ctaLabel).toBe('View current price on Amazon');
    expect(ctx.amazon.acceptSegmentTagParam).toBe(false);
    expect(ctx.amazon.allowedTags).toEqual(['kitexample-20']);
    expect(ctx.sources).toEqual(KIT_DEFAULT_SOURCES);
    expect(ctx.disclosure.pagePath).toBe('/disclosure');
    expect(ctx.byline).toEqual({ name: 'ExamplePicks Editorial Team', profilePath: null, jobTitle: null });
    expect(ctx.cache.maxHtmlCacheAgeSeconds).toBe(660);
    expect(ctx.prices.quotableMaxAgeSeconds).toBe(85740);
  });

  it('defaults to search + agent allowed, training blocked', () => {
    expect(ctx.crawlers.policy).toEqual({ search: 'allow', agent: 'allow', training: 'block' });
    for (const c of CRAWLER_CLASSES) expect(ctx.crawlers.userAgents[c]).toEqual(KIT_DEFAULT_CRAWLER_USER_AGENTS[c]);
    expect(ctx.crawlers.userAgents.training).toEqual(expect.arrayContaining(['GPTBot', 'ClaudeBot', 'CCBot', 'Google-Extended']));
    expect(ctx.crawlers.userAgents.agent).toEqual(expect.arrayContaining(['ChatGPT-User', 'Claude-User', 'Perplexity-User']));
    expect(ctx.crawlers.userAgents.search).toEqual(expect.arrayContaining(['OAI-SearchBot', 'Claude-SearchBot', 'PerplexityBot']));
    expect(ctx.crawlers.disallowPaths).toEqual(['/api/', '/r/']);
  });

  it('files Amazon\'s crawlers by what Amazon says they do: search and agent allowed, Amazonbot (may train) blocked', () => {
    expect(ctx.crawlers.userAgents.search).toContain('Amzn-SearchBot');
    expect(ctx.crawlers.userAgents.agent).toContain('Amzn-User');
    expect(ctx.crawlers.userAgents.training).toContain('Amazonbot');
  });

  it('applies the edge default: no CDN, nothing trusted', () => {
    expect(ctx.edge).toEqual({ cdn: 'none', trustedProxyCidrs: [] });
  });

  it('is open to indexing and redirects no host unless site.json says otherwise (other sites are unaffected)', () => {
    expect(ctx.indexing).toBe('open');
    expect(ctx.redirectFromHosts).toEqual([]);
    expect(Object.isFrozen(ctx.redirectFromHosts)).toBe(true);
  });

  it('a class listed in site.json replaces that class only', () => {
    const c = parseSiteContext({ ...minimal(), crawlers: { userAgents: { training: ['GPTBot'] } } }, { env: {} });
    expect(c.crawlers.userAgents.training).toEqual(['GPTBot']);
    expect(c.crawlers.userAgents.search).toEqual(KIT_DEFAULT_CRAWLER_USER_AGENTS.search);
  });
});

describe('PRICE_FRESH_HOURS can only shorten the window', () => {
  it('tightens', () => {
    const c = parseSiteContext(minimal(), { env: { PRICE_FRESH_HOURS: '12' } });
    expect(c.prices.freshnessHours).toBe(12);
    expect(c.prices.quotableMaxAgeSeconds).toBe(12 * 3600 - 660);
  });
  it('never loosens, and ignores junk', () => {
    const tight = { ...minimal(), prices: { freshnessHours: 12 } };
    expect(parseSiteContext(tight, { env: { PRICE_FRESH_HOURS: '20' } }).prices.freshnessHours).toBe(12);
    expect(parseSiteContext(minimal(), { env: { PRICE_FRESH_HOURS: '48' } }).prices.freshnessHours).toBe(24);
    expect(parseSiteContext(minimal(), { env: { PRICE_FRESH_HOURS: 'abc' } }).prices.freshnessHours).toBe(24);
    expect(parseSiteContext(minimal(), { env: { PRICE_FRESH_HOURS: '0' } }).prices.freshnessHours).toBe(24);
  });

  it('a short override hides prices sooner instead of stopping the site from booting', () => {
    // SpecPicks reads PRICE_FRESH_HOURS=1 as a one-hour window; so does the kit (minus the cache age).
    const c = parseSiteContext(minimal(), { env: { PRICE_FRESH_HOURS: '1' } });
    expect(c.prices).toEqual({ freshnessHours: 1, quotableMaxAgeSeconds: 3600 - 660 });
    const slow = { ...minimal(), cache: { htmlSMaxAgeSeconds: 600, htmlMaxAgeSeconds: 600, ssrCacheSeconds: 900 } };
    expect(parseSiteContext(slow, { env: { PRICE_FRESH_HOURS: '1' } }).prices.quotableMaxAgeSeconds).toBe(1500);
  });
});

describe('schema violations are refused, all at once', () => {
  it.each<[string, (j: any) => void, RegExp]>([
    ['a missing partner tag', (j) => { delete j.amazon.partnerTag; }, /^\/amazon\/partnerTag is required$/],
    ['a malformed partner tag', (j) => { j.amazon.partnerTag = 'Kit Example'; }, /^\/amazon\/partnerTag must match/],
    ['an unknown top-level key', (j) => { j.priceAlerts = true; }, /^\/priceAlerts is not an allowed property$/],
    ['s-maxage over 600', (j) => { j.cache.htmlSMaxAgeSeconds = 900; }, /^\/cache\/htmlSMaxAgeSeconds must be <= 600$/],
    ['a fractional s-maxage', (j) => { j.cache.htmlSMaxAgeSeconds = 1.5; }, /^\/cache\/htmlSMaxAgeSeconds must be integer$/],
    ['a 25h freshness window', (j) => { j.prices.freshnessHours = 25; }, /^\/prices\/freshnessHours must be <= 24$/],
    ['a non-US marketplace', (j) => { j.amazon.marketplaceHost = 'www.amazon.co.uk'; }, /^\/amazon\/marketplaceHost must be one of "www.amazon.com"$/],
    ['a non-USD currency', (j) => { j.amazon.currency = 'GBP'; }, /^\/amazon\/currency must be "USD"$/],
    ['a bad placement name', (j) => { j.amazon.placementTags['PDP Hero'] = 'kitexample-20'; }, /^\/amazon\/placementTags\/PDP Hero property name must match/],
    ['a trailing slash on the origin', (j) => { j.site.canonicalOrigin = 'https://example.com/'; }, /^\/site\/canonicalOrigin must match/],
    ['an http origin', (j) => { j.site.canonicalOrigin = 'http://example.com'; }, /^\/site\/canonicalOrigin must match/],
    ['an unknown schema version', (j) => { j.schemaVersion = 2; }, /^\/schemaVersion must be 1$/],
    ['a short IndexNow key', (j) => { j.indexNow.key = 'abc'; }, /^\/indexNow\/key must match/],
    ['a colour that is not #rrggbb', (j) => { j.theme.light.bg = 'white'; }, /^\/theme\/light\/bg must match/],
    ['CSS injection in the font stack', (j) => { j.theme.fontFamily = 'x;}body{display:none'; }, /^\/theme\/fontFamily must match/],
    ['a duplicate disallow path', (j) => { j.crawlers.disallowPaths = ['/api/', '/api/']; }, /^\/crawlers\/disallowPaths\/1 duplicates item 0$/],
    ['a bad crawler policy', (j) => { j.crawlers.policy.training = 'allow-some'; }, /^\/crawlers\/policy\/training must be one of "allow", "block"$/],
    ['an unknown CDN', (j) => { j.edge.cdn = 'akamai'; }, /^\/edge\/cdn must be one of "none", "cloudflare"$/],
    ['an indexing value other than open or closed', (j) => { j.indexing = 'noindex'; }, /^\/indexing must be one of "open", "closed"$/],
    ['indexing as a boolean', (j) => { j.indexing = false; }, /^\/indexing must be string$/],
    ['a redirect host with a scheme', (j) => { j.redirectFromHosts = ['https://old.example.net']; }, /^\/redirectFromHosts\/0 must match/],
    ['a redirect host with a port', (j) => { j.redirectFromHosts = ['old.example.net:8080']; }, /^\/redirectFromHosts\/0 must match/],
    ['a redirect host in upper case', (j) => { j.redirectFromHosts = ['Old.Example.net']; }, /^\/redirectFromHosts\/0 must match/],
    ['a redirect host with a path', (j) => { j.redirectFromHosts = ['old.example.net/x']; }, /^\/redirectFromHosts\/0 must match/],
    ['a redirect host listed twice', (j) => { j.redirectFromHosts = ['old.example.net', 'old.example.net']; }, /^\/redirectFromHosts\/1 duplicates item 0$/],
    ['redirect hosts as a string', (j) => { j.redirectFromHosts = 'old.example.net'; }, /^\/redirectFromHosts must be array$/],
    ['a trusted proxy that is not a CIDR', (j) => { j.edge.extraTrustedProxies = ['proxy.example.com']; }, /^\/edge\/extraTrustedProxies\/0 must match/],
  ])('%s', (_label, edit, want) => {
    const issues = issuesOf(edit);
    expect(issues.some((i) => want.test(i))).toBe(true);
  });

  it('lists every problem in one error', () => {
    const issues = issuesOf((j) => { delete j.amazon.partnerTag; j.cache.htmlSMaxAgeSeconds = 601; j.extra = 1; });
    expect(issues.length).toBe(3);
  });

  it('rejects non-objects', () => {
    expect(() => parseSiteContext(null)).toThrow(SiteContextError);
    expect(() => parseSiteContext([])).toThrow(/must be object/);
  });
});

describe('indexing and redirectFromHosts', () => {
  it('loads a closed site with the hosts that redirect to it (a temporary address, www)', () => {
    const c = parseWith((j) => {
      j.indexing = 'closed';
      j.redirectFromHosts = ['example-app.happyhill-123.eastus.azurecontainerapps.io', 'www.example.com'];
    });
    expect(c.indexing).toBe('closed');
    expect(c.redirectFromHosts).toEqual(['example-app.happyhill-123.eastus.azurecontainerapps.io', 'www.example.com']);
    expect(Object.isFrozen(c.redirectFromHosts)).toBe(true);
  });

  it('never redirects the canonical origin\'s own host (a loop), apex or www', () => {
    expect(issuesOf((j) => { j.redirectFromHosts = ['old.example.net', 'example.com']; }))
      .toEqual(["/redirectFromHosts/1 'example.com' is the canonical origin's host: it would redirect to itself"]);
    expect(issuesOf((j) => { j.site.canonicalOrigin = 'https://www.example.com'; j.redirectFromHosts = ['www.example.com']; }))
      .toEqual(["/redirectFromHosts/0 'www.example.com' is the canonical origin's host: it would redirect to itself"]);
    // The other name of the same domain is fine: that is the www -> apex (or apex -> www) fold.
    expect(parseWith((j) => { j.site.canonicalOrigin = 'https://www.example.com'; j.redirectFromHosts = ['example.com']; }).redirectFromHosts)
      .toEqual(['example.com']);
  });
});

describe('cross-field rules', () => {
  it('the origin host must be the domain or www.<domain>', () => {
    expect(issuesOf((j) => { j.site.canonicalOrigin = 'https://other.com'; }).join()).toMatch(/canonicalOrigin host other\.com/);
    expect(parseWith((j) => { j.site.canonicalOrigin = 'https://www.example.com'; }).site.canonicalOrigin).toBe('https://www.example.com');
  });

  it('the product route names {asin} exactly once', () => {
    // The schema pattern refuses it first; the loader re-checks in case the pattern is ever loosened.
    expect(issuesOf((j) => { j.routes.product = '/p/{asin}/{asin}'; }).join()).toMatch(/^\/routes\/product /);
    expect(issuesOf((j) => { j.routes.product = '/p/asin'; }).join()).toMatch(/^\/routes\/product /);
    expect(parseWith((j) => { j.routes.product = '/p/{asin}'; }).routes.product).toBe('/p/{asin}');
  });

  it('the earnings disclosure says what Operating Agreement section 5 requires', () => {
    expect(issuesOf((j) => { j.disclosure.earnings = 'We may earn a commission from links on this page.'; }).join())
      .toMatch(/As an Amazon Associate/);
    expect(parseWith((j) => { j.disclosure.earnings = 'As an Amazon Associate I earn from qualifying purchases.'; })).toBeTruthy();
  });

  it('every palette keeps text, links and the CTA legible (WCAG AA)', () => {
    const issues = issuesOf((j) => { j.theme.dark.ctaText = '#f59e0b'; j.theme.light.muted = '#cbd5e1'; });
    expect(issues).toEqual(expect.arrayContaining([
      expect.stringMatching(/^\/theme\/dark ctaText on ctaBg contrast 1\.00 < 4\.5$/),
      expect.stringMatching(/^\/theme\/light muted on bg contrast/),
    ]));
  });

  it('links, accents and focus rings stay legible on cards (surface), not only on the page background', () => {
    // link on bg passes (5.17), link on this surface does not (4.19).
    expect(contrastRatio('#2563eb', '#ffffff')).toBeGreaterThan(4.5);
    expect(issuesOf((j) => { j.theme.light.surface = '#e2e8f0'; j.theme.light.link = '#2563eb'; }))
      .toEqual(['/theme/light link on surface contrast 4.19 < 4.5']);
    expect(issuesOf((j) => { j.theme.dark.surface = '#334155'; j.theme.dark.accent = '#64748b'; j.theme.dark.focus = '#64748b'; }))
      .toEqual(expect.arrayContaining([
        expect.stringMatching(/^\/theme\/dark accent on surface contrast \d\.\d\d < 3$/),
        expect.stringMatching(/^\/theme\/dark focus on surface contrast \d\.\d\d < 3$/),
      ]));
    for (const pair of [['link', 'surface'], ['accent', 'surface'], ['focus', 'surface']]) {
      expect(THEME_CONTRAST_PAIRS.map(([fg, bg]) => `${fg}/${bg}`)).toContain(pair.join('/'));
    }
  });

  it('source rules: no reserved or repeated kinds, no shared paths, no empty rules', () => {
    expect(issuesOf((j) => { j.sources.push({ kind: 'home', exact: ['/start'] }); }).join()).toMatch(/'home' is computed by classifySource/);
    expect(issuesOf((j) => { j.sources.push({ kind: 'product', prefixes: ['/p/'] }); }).join()).toMatch(/'product' is defined twice/);
    expect(issuesOf((j) => { j.sources.push({ kind: 'compat', prefixes: ['/product/'] }); }).join()).toMatch(/prefix \/product\/ is already claimed by 'product'/);
    expect(issuesOf((j) => { j.sources.push({ kind: 'compat' }); }).join()).toMatch(/needs at least one exact path or prefix/);
  });

  it('a source tag must name a kind a rule (or classifySource) defines', () => {
    expect(issuesOf((j) => { j.amazon.sourceTags.compat = 'kitexample-compat-20'; }).join()).toMatch(/sourceTags\/compat names a source kind no rule defines/);
    expect(parseWith((j) => { j.amazon.sourceTags.home = 'kitexample-home-20'; }).amazon.allowedTags).toContain('kitexample-home-20');
  });

  it('a user agent belongs to one crawler class', () => {
    expect(issuesOf((j) => { j.crawlers.userAgents = { search: ['GPTBot'] }; }).join()).toMatch(/GPTBot is listed under both search and training/);
  });

  it('extra static ranges: new id, known crawler sources, valid CIDRs', () => {
    const r = { id: 'anthropic-outbound', sources: ['claude-user'], cidrs: ['192.0.2.0/24'], evidenceUrl: 'https://example.com/ranges' };
    expect(issuesOf((j) => { j.aiCrawlers.extraStaticRanges = [r]; }).join()).toMatch(/'anthropic-outbound' is already defined/);
    expect(issuesOf((j) => { j.aiCrawlers.extraStaticRanges = [{ ...r, id: 'x-1', sources: ['nobody'] }]; }).join()).toMatch(/'nobody' is not a crawler source/);
    expect(issuesOf((j) => { j.aiCrawlers.extraStaticRanges = [{ ...r, id: 'x-1', cidrs: ['192.0.2.0/33'] }]; }).join()).toMatch(/'192\.0\.2\.0\/33' is not a valid CIDR/);
    const ok = parseWith((j) => { j.aiCrawlers.extraStaticRanges = [{ ...r, id: 'vendor-x' }]; });
    expect(ok.aiCrawlers.staticRanges.map((s) => s.id)).toEqual(['anthropic-outbound', 'vendor-x']);
  });

  it('extra static ranges may not be broad or internal: they verify before the strict vendor lists', () => {
    const r = { id: 'all', sources: ['gptbot', 'claudebot', 'chatgpt-user'], cidrs: ['0.0.0.0/0'], evidenceUrl: 'https://x.example/' };
    expect(issuesOf((j) => { j.aiCrawlers.extraStaticRanges = [r]; }))
      .toEqual(["/aiCrawlers/extraStaticRanges/0/cidrs '0.0.0.0/0' is broader than /16, the widest IPv4 range allowed"]);
    expect(issuesOf((j) => { j.aiCrawlers.extraStaticRanges = [{ ...r, cidrs: ['2600::/16', '10.9.0.0/16'] }] }))
      .toEqual([
        "/aiCrawlers/extraStaticRanges/0/cidrs '2600::/16' is broader than /32, the widest IPv6 range allowed",
        "/aiCrawlers/extraStaticRanges/0/cidrs '10.9.0.0/16' overlaps non-public address space 10.0.0.0/8",
      ]);
  });

  it('edge: cloudflare trusts Cloudflare\'s published ranges, plus any extra proxies, one hop', () => {
    const cf = parseWith((j) => { j.edge = { cdn: 'cloudflare' }; });
    expect(cf.edge.cdn).toBe('cloudflare');
    expect(cf.edge.trustedProxyCidrs).toEqual(CDN_EDGE_RANGES.cloudflare.cidrs);
    const more = parseWith((j) => { j.edge = { cdn: 'cloudflare', extraTrustedProxies: ['198.51.100.0/24', '173.245.48.0/20'] }; });
    expect(more.edge.trustedProxyCidrs).toEqual([...CDN_EDGE_RANGES.cloudflare.cidrs, '198.51.100.0/24']);
    const onlyExtra = parseWith((j) => { j.edge = { extraTrustedProxies: ['198.51.100.0/24'] }; });
    expect(onlyExtra.edge).toEqual({ cdn: 'none', trustedProxyCidrs: ['198.51.100.0/24'] });
  });

  it('edge: a trusted proxy may not be broad or internal (it would hand the client\'s claim through)', () => {
    expect(issuesOf((j) => { j.edge.extraTrustedProxies = ['0.0.0.0/0', '10.0.0.0/16', '2001:db8::/16', '1.2.3.4/33']; }))
      .toEqual([
        "/edge/extraTrustedProxies/0 '0.0.0.0/0' is broader than /12, the widest IPv4 range allowed",
        "/edge/extraTrustedProxies/1 '10.0.0.0/16' overlaps non-public address space 10.0.0.0/8",
        "/edge/extraTrustedProxies/2 '2001:db8::/16' is broader than /24, the widest IPv6 range allowed",
        "/edge/extraTrustedProxies/3 '1.2.3.4/33' is not a valid CIDR",
      ]);
  });

  it('a trusted proxy or static range may not be IPv4 written as IPv4-mapped IPv6 (all of IPv4 would match it)', () => {
    expect(issuesOf((j) => { j.edge.extraTrustedProxies = ['::ffff:0:0/96']; }))
      .toEqual(["/edge/extraTrustedProxies/0 '::ffff:0:0/96' overlaps IPv4-mapped IPv6 space ::ffff:0:0/96; write it as an IPv4 range"]);
    const r = { id: 'mapped', sources: ['gptbot'], cidrs: ['::ffff:0:0/96'], evidenceUrl: 'https://example.com/x' };
    expect(issuesOf((j) => { j.aiCrawlers.extraStaticRanges = [r]; }))
      .toEqual(["/aiCrawlers/extraStaticRanges/0/cidrs '::ffff:0:0/96' overlaps IPv4-mapped IPv6 space ::ffff:0:0/96; write it as an IPv4 range"]);
    expect(() => parseWith((j) => { j.edge = { cdn: 'cloudflare', extraTrustedProxies: ['::ffff:a00:0/104'] }; })).toThrow(SiteContextError);
  });

  it('caching may not eat the price window', () => {
    const issues = issuesOf((j) => {
      j.prices.freshnessHours = 1;
      j.cache = { htmlSMaxAgeSeconds: 600, htmlMaxAgeSeconds: 600, ssrCacheSeconds: 900 };
    });
    expect(issues.join()).toMatch(/leaves 1500s; at least 3600s is required/);
  });

  it('a file that is not JSON is a SiteContextError naming the file', () => {
    expect(() => loadSiteContext(path.join(KIT, 'VERSION'))).toThrow(/invalid site config \(.*VERSION\)/);
    expect(() => loadSiteContext(path.join(KIT, 'no-such-site.json'))).toThrow(SiteContextError);
  });
});

describe('the schema validator (subset of draft 2020-12)', () => {
  const schema = {
    $defs: { tag: { type: 'string', pattern: '^[a-z]+-[0-9]{2}$' } },
    type: 'object',
    required: ['a'],
    additionalProperties: { type: 'integer', minimum: 0 },
    properties: {
      a: { $ref: '#/$defs/tag' },
      b: { type: 'array', items: { enum: ['x', 'y'] }, uniqueItems: true, minItems: 1, maxItems: 2 },
      c: { type: ['string', 'null'], minLength: 2, maxLength: 3 },
      d: { const: { k: [1, 2] } },
      e: false,
      f: { type: 'number', exclusiveMinimum: 0, exclusiveMaximum: 1 },
    },
    propertyNames: { pattern: '^[a-z]+$' },
  };

  it('accepts a conforming document', () => {
    expect(validateAgainstSchema({ a: 'tag-20', b: ['x'], c: null, d: { k: [1, 2] }, f: 0.5, n: 3 }, schema)).toEqual([]);
  });

  it('reports each failing keyword with a JSON pointer', () => {
    const issues = validateAgainstSchema({ a: 'TAG', b: ['x', 'x', 'z'], c: 'é', d: { k: [2, 1] }, e: 1, f: 1, n: -1, N: 1 }, schema);
    const by = (p: string) => issues.filter((i) => i.path === p).map((i) => i.message);
    expect(by('/a')).toEqual(['must match ^[a-z]+-[0-9]{2}$']);
    expect(by('/b')).toEqual(['must have at most 2 items']);
    expect(by('/b/1')).toEqual(['duplicates item 0']);
    expect(by('/b/2')).toEqual(['must be one of "x", "y"']);
    expect(by('/c')).toEqual(['must be at least 2 characters']);
    expect(by('/d')).toEqual(['must be {"k":[1,2]}']);
    expect(by('/e')).toEqual(['is not allowed']);
    expect(by('/f')).toEqual(['must be < 1']);
    expect(by('/n')).toEqual(['must be >= 0']);
    expect(by('/N')).toEqual(['property name must match ^[a-z]+$']);
  });

  it('counts characters by code point and escapes pointer segments', () => {
    expect(validateAgainstSchema('😀😀', { type: 'string', maxLength: 2 })).toEqual([]);
    expect(validateAgainstSchema({ 'a/b~c': 1 }, { type: 'object', additionalProperties: false }))
      .toEqual([{ path: '/a~1b~0c', message: 'is not an allowed property' }]);
  });

  it('refuses non-local refs and unknown types as schema bugs', () => {
    expect(() => validateAgainstSchema(1, { $ref: 'https://example.com/s.json' })).toThrow(/only local \$ref/);
    expect(() => validateAgainstSchema(1, { $ref: '#/$defs/missing' })).toThrow(/unresolvable/);
    expect(() => validateAgainstSchema(1, { type: 'decimal' })).toThrow(/unknown type/);
  });
});

describe('contrastRatio', () => {
  it('matches WCAG reference values', () => {
    expect(contrastRatio('#000000', '#ffffff')).toBeCloseTo(21, 5);
    expect(contrastRatio('#777777', '#777777')).toBe(1);
    expect(contrastRatio('#767676', '#ffffff')).toBeGreaterThanOrEqual(4.5);
    expect(contrastRatio('#777777', '#ffffff')).toBeLessThan(4.5);
    expect(() => contrastRatio('#fff', '#000000')).toThrow(/#rrggbb/);
  });
});
