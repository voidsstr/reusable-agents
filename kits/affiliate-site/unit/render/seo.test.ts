/**
 * SEO head helpers: titles within 60 characters (escaped) that end on a whole
 * phrase, meta descriptions that end at a sentence, canonical URLs, the
 * robots decision shared with the sitemaps, and an og:image that is never an
 * Amazon-hosted image. The title and description cases are SpecPicks' own
 * (tests/unit/reviewHeadTitle.test.ts, reviewHeadDescription.test.ts).
 */
import * as path from 'path';
import { loadSiteContext } from '../../server/src/siteContext';
import { faqPageLd } from '../../server/src/render/jsonld';
import {
  INDEXABLE_PAGE,
  MAX_TITLE_CHARS,
  clampTitle,
  collapseVsPrefix,
  compactTitleHead,
  headTags,
  isAllowedOgImage,
  matchupWithTopic,
  metaDescriptionFrom,
  noindexPage,
  pageTitle,
  resolveOgImage,
  robotsMetaContent,
  type HeadFields,
} from '../../server/src/render/seo';
import { htmlLength } from '../../server/src/render/text';
import { canonicalUrl, siteUrl } from '../../server/src/render/urls';

const KIT = path.resolve(__dirname, '../..');
const ctx = loadSiteContext(path.join(KIT, 'fixtures/site.example.json'), { env: {} });

describe('compactTitleHead (ported cases)', () => {
  it('never ships a bare sub-30-char colon lead', () => {
    expect(compactTitleHead('Prime Big Deal Days 2026: 5 Local-AI and Gaming Setup Picks With Price Anchors', 48)).not.toBe('Prime Big Deal Days 2026');
    expect(compactTitleHead('Best CPU for Gaming in 2026: Intel vs AMD Showdown', 48)).toBe('Best CPU for Gaming: Intel vs AMD Showdown');
    expect(compactTitleHead('Prime Big Deal Days 2026: Best Mini PC Deals for Local AI and Home Labs', 48)).toBe('Prime Big Deal Days 2026: Best Mini PC Deals');
    expect(compactTitleHead('Prime Big Deal Days 2026 CPU Deals: Ryzen 5600X to 9800X3D', 48)).toBe('Prime Big Deal Days 2026 CPU Deals');
  });

  it('drops the repeated product line in a matchup before cutting the subject', () => {
    expect(compactTitleHead('Ryzen 5 5600X vs Ryzen 5 5600G for CPU-Only Gemma 3 12B', 48)).toBe('Ryzen 5 5600X vs 5600G for CPU-Only Gemma 3 12B');
    expect(compactTitleHead('Ryzen 5 2600 vs Ryzen 5 5600X for CPU-Only gpt-oss 20B (2026)', 48)).toBe('Ryzen 5 2600 vs 5600X for CPU-Only gpt-oss 20B');
    expect(collapseVsPrefix('RTX 5070 Ti vs RTX 5090 for 4K Gaming')).toBe('RTX 5070 Ti vs 5090 for 4K Gaming');
    expect(collapseVsPrefix('Mac Mini vs Mac Studio for Local LLMs')).toBe('Mac Mini vs Mac Studio for Local LLMs');
    expect(collapseVsPrefix('AMD Ryzen 7 vs Intel Core i7')).toBe('AMD Ryzen 7 vs Intel Core i7');
  });

  it('keeps the topic after a bare matchup lead', () => {
    expect(compactTitleHead('Mistral 7B vs Mistral Small 24B: GPU Requirements Compared', 48)).toBe('Mistral 7B vs Small 24B: GPU Requirements');
    expect(matchupWithTopic('RTX 4070 vs RTX 5070: Which Should You Buy?', 48)).toBe('RTX 4070 vs RTX 5070: Which Should You Buy?');
    expect(matchupWithTopic('Ryzen 5 5600G vs Ryzen 7 5700X: Best Budget AM4 CPU 2026', 48)).toBeNull();
    expect(matchupWithTopic('RTX 4090 vs RTX 5090 for 4K Gaming: Full Benchmarks and Prices Compared', 48)).toBeNull();
    expect(collapseVsPrefix('Mac Mini vs Mac Studio', { anyDigitAfter: true })).toBe('Mac Mini vs Mac Studio');
  });

  it('works on another niche\'s titles', () => {
    // The lead names both products; a collapse that would leave "vs 3 Plus" is refused.
    expect(compactTitleHead('EcoFlow DELTA 2 vs EcoFlow DELTA 3 Plus: Fridge Runtime Tested', 48)).toBe('EcoFlow DELTA 2 vs EcoFlow DELTA 3 Plus');
    // The matchup as written wins with the shortest topic that fits, before any collapse.
    expect(compactTitleHead('Jackery 1000 vs Jackery 2000: Fridge Runtime Tested in 2026', 48)).toBe('Jackery 1000 vs Jackery 2000: Fridge Runtime');
    expect(compactTitleHead('Jackery 1000 vs Jackery 2000: Fridge Runtime Tested in 2026', 40)).toBe('Jackery 1000 vs 2000: Fridge Runtime');
    expect(compactTitleHead('Short title', 48)).toBe('Short title');
  });
});

describe('clampTitle and pageTitle', () => {
  it('leaves a short title alone', () => {
    expect(clampTitle('Best Power Stations | ExamplePicks')).toBe('Best Power Stations | ExamplePicks');
  });

  it('keeps the brand suffix, ends on a content word and never puts an ellipsis before it', () => {
    const t = clampTitle('The Best Portable Power Stations for Winter Storms and Long Outages Ranked | ExamplePicks');
    expect(t.endsWith(' | ExamplePicks')).toBe(true);
    expect(htmlLength(t)).toBeLessThanOrEqual(MAX_TITLE_CHARS);
    expect(t).not.toMatch(/…|\b(?:and|for|the|of)\s+\|/);
  });

  it('measures the escaped length, so ampersands cannot overflow the rendered title', () => {
    const t = clampTitle('Solar Panels & Cables & Adapters & Batteries & Inverters Guide | ExamplePicks');
    expect(htmlLength(t)).toBeLessThanOrEqual(MAX_TITLE_CHARS);
    expect(t.endsWith(' | ExamplePicks')).toBe(true);
  });

  it('charges escaping only for what the cut keeps: quote- and &-heavy titles keep the brand and are never empty', () => {
    expect(clampTitle('Solar Panel Mounting Z Brackets 4 Sets 1.5" 2" 3" 4" 5" 6" Aluminum | ExamplePicks'))
      .toBe('Solar Panel Mounting Z Brackets 4 Sets 1.5 | ExamplePicks');
    expect(clampTitle('Anker 12" x 12" x 10" Cable "Pro" "Max" "Ultra" "Plus" Kit | ExamplePicks'))
      .toBe('Anker 12" x 12" x 10" Cable | ExamplePicks');
    expect(clampTitle('A & B & C & D & E & F & G & H & I & J & K & L & M & N & O & P')).toBe('A & B & C & D & E & F & G & H');
    expect(pageTitle(ctx, '10" 12" 14" 16" 18" 20" 22" 24" 26" 28" 30" 32" Panels')).toBe('10" 12" 14" 16" 18 | ExamplePicks');
    // Nothing word-cuts into the budget: the brand stands in, else a plain cut.
    expect(clampTitle(`${'&'.repeat(80)} | ExamplePicks`)).toBe('ExamplePicks');
    expect(clampTitle('"'.repeat(80))).toBe('"'.repeat(10));
    for (const raw of ['&'.repeat(200), `${'"'.repeat(30)} | ${'&'.repeat(30)}`, `x${'"'.repeat(100)} | ExamplePicks`]) {
      const t = clampTitle(raw);
      expect(t).not.toBe('');
      expect(htmlLength(t)).toBeLessThanOrEqual(MAX_TITLE_CHARS);
    }
  });

  it('drops a question stub and an unclosed parenthesis left by the cut', () => {
    expect(clampTitle('Jackery Explorer 1000 v2 on a Winter Morning: Which Solar Panel Setup Should You Pick | ExamplePicks'))
      .toBe('Jackery Explorer 1000 v2 on a Winter Morning | ExamplePicks');
    const paren = clampTitle('A Very Long Station Title Here That Costs a Lot (2,048 Wh LiFePO4 Version With Wheels) | ExamplePicks');
    expect(paren).not.toMatch(/\([^)]*\|/);
  });

  it('pageTitle adds the brand once and fits the budget', () => {
    expect(pageTitle(ctx, 'Best Power Stations')).toBe('Best Power Stations | ExamplePicks');
    expect(pageTitle(ctx, 'Best Power Stations | ExamplePicks')).toBe('Best Power Stations | ExamplePicks');
    expect(pageTitle(ctx, '')).toBe('ExamplePicks');
    expect(pageTitle(ctx, 'ExamplePicks')).toBe('ExamplePicks');
    const long = pageTitle(ctx, 'Prime Big Deal Days 2026: Best Power Station Deals for Winter Storm Prep and Camping');
    // The lead loses its year rather than the topic.
    expect(long).toBe('Prime Big Deal Days: Best Power Station Deals | ExamplePicks');
    expect(htmlLength(long)).toBeLessThanOrEqual(MAX_TITLE_CHARS);
    const cut = pageTitle(ctx, 'How Many Hours Will a 2,048 Wh Portable Power Station Run a Full-Size Refrigerator During an Outage');
    expect(htmlLength(cut)).toBeLessThanOrEqual(MAX_TITLE_CHARS);
    // No separator to cut at: a word cut within the 45 characters left beside the brand.
    expect(cut).toBe('How Many Hours Will a 2,048 Wh Portable | ExamplePicks');
    expect(cut).not.toMatch(/…/);
  });
});

describe('metaDescriptionFrom (ported cases)', () => {
  it('ships a <=160-char excerpt whole and cuts a longer one at a sentence, without an ellipsis', () => {
    const e159 = 'Prime Big Deal Days gaming desk deals: five L-shaped, standing and 2-person desks for a tower plus two or three monitors, all sized with real specs.';
    const padded = (`${e159} Picks.`).slice(0, 159);
    expect(metaDescriptionFrom(padded)).toBe(padded);
    const long = 'The RTX 5090 runs 70B models at Q4 on one card, where the RTX 4090 needs a second GPU or slow CPU offload to fit. This guide compares tokens per second, context length and power draw for both cards.';
    expect(metaDescriptionFrom(long)).toBe('The RTX 5090 runs 70B models at Q4 on one card, where the RTX 4090 needs a second GPU or slow CPU offload to fit.');
    expect(metaDescriptionFrom(`${'x'.repeat(10)}${' word'.repeat(40)}`)).toMatch(/…$/);
    expect(metaDescriptionFrom('Jetson Orin Nano Super (67 TOPS) vs Pi 5 + Hailo-8 AI HAT+ (26 TOPS): published YOLOv8/v11, ResNet-50, Whisper and small-LLM results compared, plus cost and power.'))
      .toBe('Jetson Orin Nano Super (67 TOPS) vs Pi 5 + Hailo-8 AI HAT+ (26 TOPS): published YOLOv8/v11, ResNet-50, Whisper and small-LLM results compared.');
  });

  it('collapses whitespace and handles empty input', () => {
    expect(metaDescriptionFrom('  a \n b ')).toBe('a b');
    expect(metaDescriptionFrom(null)).toBe('');
  });
});

describe('indexing', () => {
  it('robots content: indexable pages allow large previews; noindex pages still follow', () => {
    expect(robotsMetaContent(INDEXABLE_PAGE)).toBe('index, follow, max-image-preview:large');
    expect(robotsMetaContent(noindexPage('cable without our own fit data'))).toBe('noindex, follow');
    expect(robotsMetaContent(noindexPage('trap', { follow: false }))).toBe('noindex, nofollow');
    expect(noindexPage(' thin  page ').reason).toBe('thin page');
    expect(() => noindexPage('  ')).toThrow(TypeError);
    expect(Object.isFrozen(INDEXABLE_PAGE)).toBe(true);
  });
});

describe('canonical and site URLs', () => {
  it('normalises to the canonical origin: no fragment, tracking params or trailing slash', () => {
    expect(canonicalUrl(ctx, '/best/under-1000/?utm_source=x&sort=price#top')).toBe('https://example.com/best/under-1000');
    expect(canonicalUrl(ctx, 'https://www.example.com//kits//outage/')).toBe('https://example.com/kits/outage');
    expect(canonicalUrl(ctx, '/')).toBe('https://example.com/');
    expect(canonicalUrl(ctx, '')).toBe('https://example.com/');
    expect(canonicalUrl(ctx, '/guides?page=2&tag=x-20&fbclid=1', { keepParams: ['page'] })).toBe('https://example.com/guides?page=2');
    expect(canonicalUrl(ctx, '/guides?b=2&a=1&page=&gclid=9', { keepParams: 'all' })).toBe('https://example.com/guides?a=1&b=2');
  });

  it('refuses a URL on another host or scheme', () => {
    expect(() => canonicalUrl(ctx, 'https://elsewhere.example.org/x')).toThrow(TypeError);
    expect(() => canonicalUrl(ctx, 'javascript:alert(1)')).toThrow(TypeError);
    expect(() => siteUrl(ctx, 'https://user:pw@example.com/x')).toThrow(TypeError);
    expect(siteUrl(ctx, '/a/b/?q=1#f')).toBe('https://example.com/a/b?q=1');
  });
});

describe('og:image is never an Amazon-hosted image', () => {
  it.each([
    'https://m.media-amazon.com/images/I/71abcdEFGHL._AC_SL1500_.jpg',
    'https://images-na.ssl-images-amazon.com/images/I/41x.jpg',
    'https://ssl-images-amazon.com/images/I/41x.jpg',
    'https://images-eu.ssl-images-amazon.com/images/I/41x.jpg',
    'https://g-ecx.images-amazon.com/images/G/01/x.jpg',
    'https://ws-na.amazon-adsystem.com/widgets/q?ASIN=B0ABCDEFGH',
    'https://www.amazon.com/images/I/x.jpg',
    '//m.media-amazon.com/images/I/x.jpg',
    'https://M.MEDIA-AMAZON.COM./images/I/x.jpg',
  ])('refuses %s', (url) => {
    expect(isAllowedOgImage(ctx, url)).toBe(false);
    expect(resolveOgImage(ctx, url)).toBeNull();
  });

  it('accepts our own and other https images, made absolute; refuses http and junk', () => {
    expect(resolveOgImage(ctx, '/og/alpha.png')).toEqual({ url: 'https://example.com/og/alpha.png' });
    expect(resolveOgImage(ctx, { url: 'https://cdn.example.org/h.webp', width: 1200, height: 630, alt: 'Alpha' }))
      .toEqual({ url: 'https://cdn.example.org/h.webp', width: 1200, height: 630, alt: 'Alpha' });
    expect(isAllowedOgImage(ctx, 'http://cdn.example.org/h.webp')).toBe(false);
    expect(isAllowedOgImage(ctx, '')).toBe(false);
    expect(isAllowedOgImage(ctx, 'data:image/png;base64,AAAA')).toBe(false);
  });

  it('falls back to the site card, or to none, when the candidate is refused', () => {
    const card = { url: '/og-card.png', width: 1200, height: 630 };
    expect(resolveOgImage(ctx, 'https://m.media-amazon.com/x.jpg', card)).toEqual({ ...card, url: 'https://example.com/og-card.png' });
    expect(resolveOgImage(ctx, null, { url: 'https://m.media-amazon.com/x.jpg' })).toBeNull();
  });
});

describe('headTags', () => {
  const base: HeadFields = {
    title: 'Best "Winter" Stations & Kits | ExamplePicks',
    description: 'What to buy for a <three-day> outage.',
    canonical: '/best/winter?page=2&utm_medium=x',
    indexing: INDEXABLE_PAGE,
    ogType: 'article',
    ogImage: 'https://m.media-amazon.com/images/I/x.jpg',
    defaultOgImage: { url: '/og-card.png', width: 1200, height: 630, alt: 'ExamplePicks card' },
    jsonLd: [faqPageLd([{ question: 'Q?', answer: 'A.' }]), null],
    extraMeta: [{ property: 'article:published_time', content: '2026-11-10T08:00:00Z' }, { name: 'x"y', content: '<z>' }],
    linkPrev: '/best/winter',
    linkNext: '/best/winter?page=3',
    alternates: [{ type: 'application/atom+xml', href: '/feed.xml', title: 'Guides' }],
  };
  const html = headTags(ctx, base);
  const tags = html.split('\n');

  it('emits one title, description, robots and canonical, all escaped', () => {
    expect(tags.filter((t) => t.startsWith('<title>'))).toEqual(['<title>Best &quot;Winter&quot; Stations &amp; Kits | ExamplePicks</title>']);
    expect(html).toContain('<meta name="description" content="What to buy for a &lt;three-day&gt; outage.">');
    expect(html).toContain('<meta name="robots" content="index, follow, max-image-preview:large">');
    expect(tags.filter((t) => t.startsWith('<link rel="canonical"'))).toEqual(['<link rel="canonical" href="https://example.com/best/winter?page=2">']);
    expect(html).toContain('<meta name="x&quot;y" content="&lt;z&gt;">');
  });

  it('uses the default card instead of an Amazon og:image, everywhere', () => {
    expect(html).toContain('<meta property="og:image" content="https://example.com/og-card.png">');
    expect(html).toContain('<meta property="og:image:width" content="1200">');
    expect(html).toContain('<meta name="twitter:image" content="https://example.com/og-card.png">');
    expect(html).toContain('<meta name="twitter:card" content="summary_large_image">');
    expect(html).not.toMatch(/amazon/i);
  });

  it('carries Open Graph basics, alternates, prev/next and the JSON-LD', () => {
    for (const want of [
      '<meta property="og:type" content="article">',
      '<meta property="og:url" content="https://example.com/best/winter?page=2">',
      '<meta property="og:site_name" content="ExamplePicks">',
      '<meta property="og:locale" content="en_US">',
      '<meta property="article:published_time" content="2026-11-10T08:00:00Z">',
      '<link rel="alternate" type="application/atom+xml" title="Guides" href="https://example.com/feed.xml">',
      '<link rel="prev" href="https://example.com/best/winter">',
      '<link rel="next" href="https://example.com/best/winter?page=3">',
    ]) expect(html).toContain(want);
    expect(html.match(/<script type="application\/ld\+json">/g)).toHaveLength(1);
  });

  it('extraMeta cannot slip an Amazon image past the og:image gate', () => {
    const h = headTags(ctx, { title: 'X', canonical: '/x', indexing: INDEXABLE_PAGE, extraMeta: [
      { property: 'og:image', content: 'https://m.media-amazon.com/images/I/abc.jpg' },
      { name: 'twitter:image', content: 'https://images-na.ssl-images-amazon.com/images/I/abc.jpg' },
      { property: 'OG:IMAGE:SECURE_URL', content: 'http://cdn.example.org/plain.jpg' },
      { name: 'twitter:image:src', content: '//m.media-amazon.com/x.jpg' },
      { property: 'og:image', content: '/og/extra.png' },
      { property: 'og:image:alt', content: 'Alpha on a bench' },
    ] });
    expect(h).not.toMatch(/amazon|plain\.jpg/);
    expect(h).toContain('<meta property="og:image" content="https://example.com/og/extra.png">');
    expect(h).toContain('<meta property="og:image:alt" content="Alpha on a bench">');
  });

  it.each<[string, { name?: string; property?: string }]>([
    ['robots', { name: 'robots' }],
    ['a crawler-specific robots meta', { name: 'GoogleBot' }],
    ['googlebot-news', { name: 'googlebot-news' }],
    ['bingbot', { name: 'bingbot' }],
    ['og:url', { property: 'og:url' }],
    ['og:type', { property: 'og:type' }],
    ['description', { name: 'description' }],
    ['twitter:card', { name: 'twitter:card' }],
    ['og:title given as a name', { name: 'og:title' }],
  ])('extraMeta may not set %s (HeadFields owns it)', (_what, meta) => {
    expect(() => headTags(ctx, { title: 'X', canonical: '/x', indexing: INDEXABLE_PAGE, extraMeta: [{ ...meta, content: 'index, follow' }] }))
      .toThrow(TypeError);
  });

  it('noindex pages say so; no image means a summary card and no og:image', () => {
    const h = headTags(ctx, { title: 'Thin cable page', canonical: '/product/B0ABCDEFGH', indexing: noindexPage('no fit data') });
    expect(h).toContain('<meta name="robots" content="noindex, follow">');
    expect(h).toContain('<meta name="twitter:card" content="summary">');
    expect(h).not.toContain('og:image');
    expect(h).not.toContain('name="description"');
  });

  it('clamps an overlong title', () => {
    const h = headTags(ctx, { ...base, title: `${'Very Long Words '.repeat(10)}| ExamplePicks` });
    const title = /<title>(.*)<\/title>/.exec(h)?.[1] as string;
    expect(title.length).toBeLessThanOrEqual(MAX_TITLE_CHARS);
    expect(title.endsWith('| ExamplePicks')).toBe(true);
  });
});
