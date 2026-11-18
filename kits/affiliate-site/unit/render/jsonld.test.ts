/**
 * JSON-LD builders: a Product carries an Offer only while its price is
 * quotable (validFrom = the fetch time), never an AggregateRating, never an
 * Amazon-hosted image; ItemList entries are full Products only with a current
 * Offer; isSimilarTo points at the alternatives by @id; BreadcrumbList and
 * FAQPage mirror what the page shows; serialization cannot break out of the
 * script element.
 */
import * as fs from 'fs';
import * as path from 'path';
import { loadSiteContext, parseSiteContext, type SiteJson } from '../../server/src/siteContext';
import {
  PRODUCT_NAME_MAX,
  articleLd,
  breadcrumbListLd,
  breadcrumbTrail,
  cleanBrand,
  faqPageLd,
  gtinCheckDigitOk,
  gtinFields,
  isSimilarToLd,
  itemListLd,
  jsonLdScripts,
  organizationLd,
  productLd,
  productNodeId,
  propertyValuesLd,
  sanitizeMpn,
  serializeJsonLd,
  stripAggregateRatings,
  webSiteLd,
  type JsonLdNode,
} from '../../server/src/render/jsonld';

const KIT = path.resolve(__dirname, '../..');
const ctx = loadSiteContext(path.join(KIT, 'fixtures/site.example.json'), { env: {} });
const NOW = Date.parse('2026-11-17T12:00:00Z');
const FRESH = '2026-11-17T10:11:42.000Z';
/** Older than 24h minus the 660 s of HTML caching the fixture allows. */
const STALE = new Date(NOW - (24 * 3600 - 600) * 1000).toISOString();
const ASIN = 'B0ABCDEFGH';

/** Every key anywhere in a JSON value. */
function allKeys(v: unknown, out: string[] = []): string[] {
  if (Array.isArray(v)) v.forEach((x) => allKeys(x, out));
  else if (v && typeof v === 'object') for (const [k, x] of Object.entries(v)) { out.push(k); allKeys(x, out); }
  return out;
}

describe('productLd', () => {
  const base = { asin: ASIN, name: 'Alpha 2000 Portable Power Station', brand: 'AlphaPower' };

  it('carries an Offer while the price is quotable, with validFrom = fetched_at and no merchant claims', () => {
    const p = productLd(ctx, { ...base, offer: { price: '899.00', currency: 'USD', fetchedAt: FRESH, availabilityType: 'IN_STOCK_SCARCE' } }, NOW);
    expect(p).toMatchObject({
      '@context': 'https://schema.org',
      '@type': 'Product',
      '@id': 'https://example.com/product/B0ABCDEFGH#product',
      name: 'Alpha 2000 Portable Power Station',
      url: 'https://example.com/product/B0ABCDEFGH',
      sku: ASIN,
      brand: { '@type': 'Brand', name: 'AlphaPower' },
      offers: {
        '@type': 'Offer',
        url: 'https://example.com/product/B0ABCDEFGH',
        priceCurrency: 'USD',
        price: '899.00',
        availability: 'https://schema.org/LimitedAvailability',
        itemCondition: 'https://schema.org/NewCondition',
        validFrom: FRESH,
      },
    });
    const keys = allKeys(p);
    for (const banned of ['aggregateRating', 'review', 'seller', 'hasMerchantReturnPolicy', 'shippingDetails', 'priceValidUntil']) {
      expect(keys).not.toContain(banned);
    }
  });

  it.each<[string, Record<string, unknown>]>([
    ['stale', { price: 899, currency: 'USD', fetchedAt: STALE }],
    ['non-USD', { price: 899, currency: 'CAD', fetchedAt: FRESH }],
    ['under $1', { price: 0.5, currency: 'USD', fetchedAt: FRESH }],
    ['no check time', { price: 899, currency: 'USD' }],
    ['from the future', { price: 899, currency: 'USD', fetchedAt: '2026-11-18T12:00:00Z' }],
    ['a repurposed listing', { price: 899, currency: 'USD', fetchedAt: FRESH, qualityFlags: ['shelf-20261101-listing-repurposed'] }],
  ])('has no offers when the price is %s', (_why, offer) => {
    const p = productLd(ctx, { ...base, offer }, NOW);
    expect(p.offers).toBeUndefined();
    expect(p.name).toBe(base.name);
  });

  it('honours an explicit Offer url and the stock text', () => {
    const p = productLd(ctx, { ...base, offer: { price: 10, fetchedAt: FRESH, url: '/product/B0ABCDEFGH?x=1', availabilityText: 'Currently unavailable.' } }, NOW);
    expect(p.offers).toMatchObject({ url: 'https://example.com/product/B0ABCDEFGH?x=1', availability: 'https://schema.org/OutOfStock' });
    const amazonOffer = productLd(ctx, { ...base, offer: { price: 10, fetchedAt: FRESH, url: 'https://www.amazon.com/dp/B0ABCDEFGH?tag=kitexample-20' } }, NOW);
    expect(amazonOffer.offers).toMatchObject({ url: 'https://www.amazon.com/dp/B0ABCDEFGH?tag=kitexample-20' });
    const junk = productLd(ctx, { ...base, offer: { price: 10, fetchedAt: FRESH, url: 'javascript:alert(1)' } }, NOW);
    expect(junk.offers).toMatchObject({ url: 'https://example.com/product/B0ABCDEFGH' });
  });

  it('publishes an Amazon Offer url only with one of the site\'s own tags, else the product page', () => {
    const offerUrl = (url: string, c: Parameters<typeof productLd>[0] = ctx): string =>
      (productLd(c, { ...base, offer: { price: 10, fetchedAt: FRESH, url } }, NOW).offers as { url: string }).url;
    const page = 'https://example.com/product/B0ABCDEFGH';
    expect(offerUrl('https://www.amazon.com/dp/B0ABCDEFGH?tag=specpicks-20')).toBe('https://www.amazon.com/dp/B0ABCDEFGH?tag=kitexample-20');
    expect(offerUrl('http://www.amazon.com/dp/B0ABCDEFGH?tag=someoneelse-20&psc=1')).toBe('https://www.amazon.com/dp/B0ABCDEFGH?psc=1&tag=kitexample-20');
    expect(offerUrl('https://www.amazon.com/dp/B0ABCDEFGH?tag=kitexample-pdp-20')).toBe('https://www.amazon.com/dp/B0ABCDEFGH?tag=kitexample-pdp-20');
    expect(offerUrl('https://www.amazon.com/dp/B0ABCDEFGH?tag=kitexample-pdp-20&tag=x-20')).toBe('https://www.amazon.com/dp/B0ABCDEFGH?tag=kitexample-20');
    for (const unusable of [
      'https://amzn.to/3abcd', 'https://www.amazon.co.uk/dp/B0ABCDEFGH', 'https://m.media-amazon.com/images/I/x.jpg',
      'http://maker.example.org/manual.pdf', 'https://user:pw@maker.example.org/x',
    ]) {
      expect(offerUrl(unusable)).toBe(page);
    }
    expect(offerUrl('https://maker.example.org/manual.pdf')).toBe('https://maker.example.org/manual.pdf');
    // A context without the site's amazon settings cannot re-tag, so the product page stands in.
    const { amazon: _amazon, ...narrow } = ctx;
    expect(offerUrl('https://www.amazon.com/dp/B0ABCDEFGH?tag=kitexample-20', narrow)).toBe(page);
  });

  it('cuts the name to 150 chars at a word without an ellipsis, and trims the description', () => {
    const long = `Alpha 2000 ${'Portable Power Station '.repeat(12)}with LiFePO4`;
    const p = productLd(ctx, { ...base, name: long, description: `${'word '.repeat(2000)}end` }, NOW);
    expect((p.name as string).length).toBeLessThanOrEqual(PRODUCT_NAME_MAX);
    expect(p.name).not.toMatch(/[…\s]$/);
    expect((p.description as string).length).toBeLessThanOrEqual(5000);
  });

  it('keeps only public, non-Amazon images, absolute and de-duplicated', () => {
    const p = productLd(ctx, {
      ...base,
      images: [
        'https://m.media-amazon.com/images/I/71abc.jpg',
        'https://images-na.ssl-images-amazon.com/images/I/x.jpg',
        'http://cdn.example.org/plain-http.jpg',
        '/images/alpha.webp',
        'https://cdn.example.org/a.jpg',
        'https://cdn.example.org/a.jpg',
        null,
      ],
    }, NOW);
    expect(p.image).toEqual(['https://example.com/images/alpha.webp', 'https://cdn.example.org/a.jpg']);
    expect(productLd(ctx, { ...base, images: ['https://m.media-amazon.com/x.jpg'] }, NOW).image).toBeUndefined();
  });

  it('drops placeholder brands and role suffixes; sku and @id only for a real ASIN', () => {
    expect(productLd(ctx, { ...base, brand: 'Generic' }, NOW).brand).toBeUndefined();
    expect(productLd(ctx, { ...base, brand: ' Jane Doe(Author) ' }, NOW).brand).toEqual({ '@type': 'Brand', name: 'Jane Doe' });
    const p = productLd(ctx, { asin: 'kit-alpha', name: 'Kit', url: '/kits/alpha' }, NOW);
    expect(p.sku).toBeUndefined();
    expect(p['@id']).toBe('https://example.com/kits/alpha#product');
  });

  it('publishes sanitized identifiers, spec properties and isSimilarTo refs', () => {
    const p = productLd(ctx, {
      ...base,
      mpn: 'AP-2000-US',
      gtin: '0 12345 67890 5',
      category: 'Portable power stations',
      additionalProperty: [
        { name: 'Capacity', value: 2048, unitCode: 'WHR' },
        { name: 'Surge', value: 'Not published by manufacturer' },
        { name: 'Weight', value: '22.5 kg', unitCode: 'bad code' },
        { name: '', value: 'x' },
      ],
      isSimilarTo: [{ asin: 'B0ZZZZZZZZ', name: 'Beta 2000' }, { asin: ASIN, name: 'self' }, { asin: 'EBAY_1', name: 'x' }],
    }, NOW);
    expect(p).toMatchObject({ mpn: 'AP-2000-US', gtin12: '012345678905', category: 'Portable power stations' });
    expect(p.additionalProperty).toEqual([
      { '@type': 'PropertyValue', name: 'Capacity', value: 2048, unitCode: 'WHR' },
      { '@type': 'PropertyValue', name: 'Weight', value: '22.5 kg' },
    ]);
    expect(p.isSimilarTo).toEqual([{
      '@type': 'Product', '@id': 'https://example.com/product/B0ZZZZZZZZ#product', name: 'Beta 2000', url: 'https://example.com/product/B0ZZZZZZZZ',
    }]);
  });
});

describe('identifiers (ported from productIds.ts)', () => {
  it('validates GTIN check digits', () => {
    expect(gtinCheckDigitOk('012345678905')).toBe(true);
    expect(gtinCheckDigitOk('012345678906')).toBe(false);
    expect(gtinCheckDigitOk('4006381333931')).toBe(true);
    expect(gtinCheckDigitOk('12345')).toBe(false);
    expect(gtinFields('4006381333931')).toEqual({ gtin13: '4006381333931' });
    expect(gtinFields('00012345678905')).toEqual({ gtin14: '00012345678905' });
    expect(gtinFields('not a barcode')).toEqual({});
  });

  it.each<[unknown, string | null]>([
    ['AP-2000-US', 'AP-2000-US'],
    ['\u200eEFD330\u200e', 'EFD330'],
    ['901 Space Gray', null],
    ['for MacBook Pro 14', null],
    ['B0ABCDEFGH', null],
    ['ciciglowantm9pgbvz6597', null],
    ['012345678905', null],
    ['ab', null],
    ['NoDigitsHere', null],
  ])('sanitizeMpn(%j) -> %j', (raw, want) => {
    expect(sanitizeMpn(raw, ASIN)).toBe(want);
  });

  it('cleanBrand', () => {
    expect(cleanBrand('Unbranded')).toBeNull();
    expect(cleanBrand(null)).toBeNull();
    expect(cleanBrand('Acme, ')).toBe('Acme');
  });

  it('propertyValuesLd caps the list', () => {
    const props = Array.from({ length: 20 }, (_, k) => ({ name: `p${k}`, value: k }));
    expect(propertyValuesLd(props)).toHaveLength(12);
    expect(propertyValuesLd(props, 3)).toHaveLength(3);
  });
});

describe('isSimilarToLd', () => {
  it('attaches the alternatives to the page Product by @id; null without real alternatives', () => {
    expect(isSimilarToLd(ctx, ASIN, [{ asin: 'B0ZZZZZZZZ', name: 'Beta' }])).toEqual({
      '@context': 'https://schema.org',
      '@type': 'Product',
      '@id': productNodeId(ctx, ASIN),
      isSimilarTo: [{ '@type': 'Product', '@id': 'https://example.com/product/B0ZZZZZZZZ#product', name: 'Beta', url: 'https://example.com/product/B0ZZZZZZZZ' }],
    });
    expect(isSimilarToLd(ctx, ASIN, [])).toBeNull();
    expect(isSimilarToLd(ctx, ASIN, [{ asin: ASIN, name: 'self' }])).toBeNull();
    expect(isSimilarToLd(ctx, 'not-an-asin', [{ asin: 'B0ZZZZZZZZ', name: 'Beta' }])).toBeNull();
  });
});

describe('itemListLd', () => {
  it('makes a quotable entry a full Product with an Offer, a stale one a plain ListItem', () => {
    const list = itemListLd(ctx, {
      name: 'Best power stations under $1,000',
      url: '/best/under-1000',
      order: 'ascending',
      items: [
        { name: 'Alpha 2000', url: '/product/B0ABCDEFGH', asin: ASIN, brand: 'AlphaPower', image: 'https://m.media-amazon.com/x.jpg', offer: { price: 899, currency: 'USD', fetchedAt: FRESH } },
        { name: 'Beta 1500', url: '/product/B0ZZZZZZZZ', asin: 'B0ZZZZZZZZ', offer: { price: 799, currency: 'USD', fetchedAt: STALE } },
        { name: 'Our sizing guide', url: '/guides/sizing' },
      ],
    }, NOW) as JsonLdNode;
    expect(list).toMatchObject({
      '@context': 'https://schema.org',
      '@type': 'ItemList',
      name: 'Best power stations under $1,000',
      url: 'https://example.com/best/under-1000',
      itemListOrder: 'https://schema.org/ItemListOrderAscending',
      numberOfItems: 3,
    });
    const [a, b, c] = list.itemListElement as JsonLdNode[];
    expect(a).toEqual({
      '@type': 'ListItem',
      position: 1,
      item: {
        '@type': 'Product',
        name: 'Alpha 2000',
        url: 'https://example.com/product/B0ABCDEFGH',
        '@id': 'https://example.com/product/B0ABCDEFGH#product',
        sku: ASIN,
        brand: { '@type': 'Brand', name: 'AlphaPower' },
        offers: {
          '@type': 'Offer', url: 'https://example.com/product/B0ABCDEFGH', priceCurrency: 'USD', price: '899.00',
          availability: 'https://schema.org/InStock', itemCondition: 'https://schema.org/NewCondition', validFrom: FRESH,
        },
      },
    });
    expect(b).toEqual({ '@type': 'ListItem', position: 2, name: 'Beta 1500', url: 'https://example.com/product/B0ZZZZZZZZ' });
    expect(c).toEqual({ '@type': 'ListItem', position: 3, name: 'Our sizing guide', url: 'https://example.com/guides/sizing' });
    expect(JSON.stringify(list)).not.toMatch(/media-amazon|aggregateRating/);
  });

  it('keeps an external entry url, drops empty entries, and is null when empty', () => {
    const list = itemListLd(ctx, { items: [
      { name: 'Maker manual', url: 'https://maker.example.org/manual.pdf' },
      { name: ' ', url: '/x' },
      { name: 'Bad', url: 'javascript:alert(1)' },
    ] }, NOW) as JsonLdNode;
    expect(list.itemListElement).toEqual([{ '@type': 'ListItem', position: 1, name: 'Maker manual', url: 'https://maker.example.org/manual.pdf' }]);
    expect(itemListLd(ctx, { items: [] }, NOW)).toBeNull();
  });

  it('re-tags Amazon entry urls; an unusable one falls back to the product page, or drops the entry', () => {
    const list = itemListLd(ctx, { items: [
      { name: 'Alpha', url: 'https://www.amazon.com/dp/B0ABCDEFGH?tag=thief-20', asin: ASIN },
      { name: 'Beta', url: 'https://amzn.to/3abcd', asin: 'B0ZZZZZZZZ', offer: { price: 799, fetchedAt: FRESH, url: 'https://amzn.to/3abcd' } },
      { name: 'Gamma', url: 'https://amzn.to/3zzzz' },
      { name: 'Delta', url: 'http://maker.example.org/d' },
    ] }, NOW) as JsonLdNode;
    const [a, b, ...rest] = list.itemListElement as JsonLdNode[];
    expect(a).toEqual({ '@type': 'ListItem', position: 1, name: 'Alpha', url: 'https://www.amazon.com/dp/B0ABCDEFGH?tag=kitexample-20' });
    expect(b).toMatchObject({
      '@type': 'ListItem',
      position: 2,
      item: { url: 'https://example.com/product/B0ZZZZZZZZ', offers: { url: 'https://example.com/product/B0ZZZZZZZZ', price: '799.00' } },
    });
    expect(rest).toEqual([]);
    expect(JSON.stringify(list)).not.toMatch(/amzn\.to|thief|http:\/\//);
  });
});

describe('BreadcrumbList', () => {
  it('prepends Home, numbers positions, absolutises urls, leaves the current page without item', () => {
    expect(breadcrumbListLd(ctx, [{ name: 'Power stations', href: '/category/power-stations' }, { name: 'Alpha 2000' }])).toEqual({
      '@context': 'https://schema.org',
      '@type': 'BreadcrumbList',
      itemListElement: [
        { '@type': 'ListItem', position: 1, name: 'Home', item: 'https://example.com/' },
        { '@type': 'ListItem', position: 2, name: 'Power stations', item: 'https://example.com/category/power-stations' },
        { '@type': 'ListItem', position: 3, name: 'Alpha 2000' },
      ],
    });
  });

  it('does not repeat a home crumb, trims long names and is null for a lone crumb', () => {
    const trail = breadcrumbTrail(ctx, [{ name: 'Start', href: '/' }, { name: `${'Very long name '.repeat(10)}end`, href: '/x/' }]);
    expect(trail[0]).toEqual({ name: 'Start', url: 'https://example.com/' });
    expect(trail).toHaveLength(2);
    expect(trail[1]?.name.length).toBeLessThanOrEqual(70);
    expect(trail[1]?.url).toBe('https://example.com/x');
    expect(breadcrumbListLd(ctx, [])).toBeNull();
    expect(breadcrumbListLd(ctx, [{ name: 'Home', href: '/' }], { home: false })).toBeNull();
    expect(() => breadcrumbListLd(ctx, [{ name: 'x', href: 'https://elsewhere.example.org/' }])).toThrow(TypeError);
  });

  it('gives every crumb but the last an item: an unlinked middle crumb is left out', () => {
    expect(breadcrumbListLd(ctx, [{ name: 'Guides' }, { name: 'Winter', href: '/guides/winter' }, { name: 'Page' }])).toEqual({
      '@context': 'https://schema.org',
      '@type': 'BreadcrumbList',
      itemListElement: [
        { '@type': 'ListItem', position: 1, name: 'Home', item: 'https://example.com/' },
        { '@type': 'ListItem', position: 2, name: 'Winter', item: 'https://example.com/guides/winter' },
        { '@type': 'ListItem', position: 3, name: 'Page' },
      ],
    });
    expect(breadcrumbTrail(ctx, [{ name: 'A' }, { name: 'B' }], { home: false })).toEqual([{ name: 'B', url: null }]);
    expect(breadcrumbListLd(ctx, [{ name: 'A' }, { name: 'B' }], { home: false })).toBeNull();
  });
});

describe('FAQPage', () => {
  it('mirrors complete rows as plain text; null when there are none', () => {
    expect(faqPageLd([
      { question: 'How long will it run a <b>fridge</b>?', answer: 'About 11 to 20 hours.' },
      { question: 'Empty answer?', answer: '  ' },
    ])).toEqual({
      '@context': 'https://schema.org',
      '@type': 'FAQPage',
      mainEntity: [{ '@type': 'Question', name: 'How long will it run a fridge?', acceptedAnswer: { '@type': 'Answer', text: 'About 11 to 20 hours.' } }],
    });
    expect(faqPageLd([])).toBeNull();
    expect(faqPageLd(null)).toBeNull();
  });
});

describe('Article, Organization and WebSite', () => {
  it('Article: the site byline (Organization without a job title), honest dates, no Amazon image', () => {
    const a = articleLd(ctx, {
      headline: 'Best Portable Power Stations for a Winter Outage',
      url: '/guides/winter-outage',
      description: 'What to buy for a <em>three-day</em> outage.',
      datePublished: '2026-11-10T08:00:00Z',
      dateModified: '2026-11-01T08:00:00Z',
      image: 'https://m.media-amazon.com/images/I/x.jpg',
      section: 'Guides',
      wordCount: 1800,
    });
    expect(a).toMatchObject({
      '@type': 'Article',
      url: 'https://example.com/guides/winter-outage',
      mainEntityOfPage: { '@type': 'WebPage', '@id': 'https://example.com/guides/winter-outage' },
      author: { '@type': 'Organization', name: 'ExamplePicks Editorial Team', url: 'https://example.com/about' },
      publisher: { '@type': 'Organization', '@id': 'https://example.com/#organization', name: 'ExamplePicks' },
      description: 'What to buy for a three-day outage.',
      datePublished: '2026-11-10T08:00:00.000Z',
      dateModified: '2026-11-10T08:00:00.000Z',
      articleSection: 'Guides',
      wordCount: 1800,
      inLanguage: 'en-US',
    });
    expect(a.image).toBeUndefined();
  });

  it('Article: a byline with a job title is a Person', () => {
    const j = JSON.parse(fs.readFileSync(path.join(KIT, 'fixtures/site.example.json'), 'utf8')) as SiteJson;
    const person = parseSiteContext({ ...j, byline: { name: 'Sam Writer', jobTitle: 'Editor', profilePath: '/about/sam' } }, { env: {} });
    const a = articleLd(person, { headline: 'h', url: '/guides/x', image: '/images/hero.webp', datePublished: 'not a date' });
    expect(a.author).toEqual({ '@type': 'Person', name: 'Sam Writer', url: 'https://example.com/about/sam', jobTitle: 'Editor' });
    expect(a.image).toBe('https://example.com/images/hero.webp');
    expect(a.datePublished).toBeUndefined();
  });

  it('Organization and WebSite reference each other and name the disclosure page', () => {
    const org = organizationLd(ctx, {
      logo: { url: '/logo.png', width: 512, height: 512 },
      sameAs: ['https://social.example.org/examplepicks', 'javascript:alert(1)'],
      publishingPrinciplesPath: '/how-we-calculate',
      knowsAbout: ['Portable power stations', ' '],
    });
    expect(org).toMatchObject({
      '@id': 'https://example.com/#organization',
      logo: { '@type': 'ImageObject', url: 'https://example.com/logo.png', width: 512, height: 512 },
      sameAs: ['https://social.example.org/examplepicks'],
      publishingPrinciples: 'https://example.com/how-we-calculate',
      knowsAbout: ['Portable power stations'],
      subjectOf: { '@type': 'WebPage', name: 'Affiliate disclosure', url: 'https://example.com/disclosure' },
    });
    expect(organizationLd(ctx, { logo: { url: 'https://m.media-amazon.com/logo.png' } }).logo).toBeUndefined();
    const site = webSiteLd(ctx, { searchPath: '/search?q={search_term_string}' });
    expect(site).toMatchObject({ '@id': 'https://example.com/#website', publisher: { '@id': 'https://example.com/#organization' } });
    expect(site.potentialAction).toMatchObject({ target: { urlTemplate: 'https://example.com/search?q={search_term_string}' } });
    expect(webSiteLd(ctx, { searchPath: 'https://evil.example.org/?q={search_term_string}' }).potentialAction).toBeUndefined();
  });
});

describe('serialization', () => {
  it('never emits an AggregateRating, however a node smuggles one in', () => {
    const node = {
      '@type': 'Product',
      name: 'x',
      aggregateRating: { '@type': 'AggregateRating', ratingValue: '4.6' },
      isRelatedTo: [{ '@type': 'AggregateRating', ratingValue: 5 }, { '@type': 'Product', name: 'y', aggregateRating: { ratingValue: 1 } }],
      review: { '@type': 'Review', reviewRating: { '@type': 'Rating', ratingValue: 4 } },
    };
    const out = JSON.parse(serializeJsonLd(node));
    expect(allKeys(out)).not.toContain('aggregateRating');
    expect(JSON.stringify(out)).not.toMatch(/AggregateRating/);
    expect(out.isRelatedTo).toEqual([{ '@type': 'Product', name: 'y' }]);
    expect(out.review).toBeDefined();
    expect(stripAggregateRatings([{ '@type': ['Thing', 'AggregateRating'] }, 1])).toEqual([1]);
    expect(node.aggregateRating).toBeDefined(); // the input is not mutated
  });

  it('strips ratings written as full schema.org IRIs too', () => {
    const out = JSON.parse(serializeJsonLd({
      '@type': 'Product',
      name: 'x',
      'http://schema.org/aggregateRating': { ratingValue: 4.6, reviewCount: 10 },
      'schema:aggregateRating': { ratingValue: 4.6 },
      rating: { '@type': 'http://schema.org/AggregateRating', ratingValue: 5 },
      r2: { '@type': 'schema:AggregateRating' },
      r3: [{ '@type': ['Thing', 'https://www.schema.org/AggregateRating'] }, 'kept'],
    }));
    expect(out).toEqual({ '@context': 'https://schema.org', '@type': 'Product', name: 'x', r3: ['kept'] });
  });

  it('drops a subtree nested past 32 levels instead of publishing it unchecked', () => {
    let deep: JsonLdNode = { '@type': 'Product', name: 'deep', aggregateRating: { '@type': 'AggregateRating', ratingValue: 5 } };
    for (let k = 0; k < 40; k++) deep = { '@type': 'WebPage', subjectOf: deep };
    const json = serializeJsonLd(deep);
    expect(json).not.toMatch(/aggregateRating|AggregateRating|"deep"/);
    expect(JSON.parse(json)['@type']).toBe('WebPage');
    let shallow: JsonLdNode = { '@type': 'Product', name: 'shallow' };
    for (let k = 0; k < 20; k++) shallow = { '@type': 'WebPage', subjectOf: shallow };
    expect(serializeJsonLd(shallow)).toContain('"shallow"');
  });

  it('serialises Dates, URLs and other toJSON values exactly as JSON.stringify does', () => {
    const node = {
      '@type': 'WebPage',
      dateModified: new Date('2026-11-17T10:00:00Z'),
      about: { '@type': 'Thing', startDate: new Date('2026-11-01T00:00:00Z'), sameAs: new URL('https://example.com/x') },
      list: [new Date(0), { toJSON: () => 'custom' }],
    };
    expect(JSON.parse(serializeJsonLd(node))).toEqual(JSON.parse(JSON.stringify({ '@context': 'https://schema.org', ...node })));
    expect(JSON.parse(serializeJsonLd(node)).dateModified).toBe('2026-11-17T10:00:00.000Z');
  });

  it('drops Amazon-hosted images from the image properties of a hand-built node', () => {
    const out = JSON.parse(serializeJsonLd({
      '@type': 'Product',
      name: 'x',
      image: ['https://m.media-amazon.com/images/I/a.jpg', 'https://example.com/a.webp'],
      logo: { '@type': 'ImageObject', url: 'https://images-na.ssl-images-amazon.com/images/I/x.png' },
      thumbnailUrl: '//m.media-amazon.com/images/I/t.jpg',
      subjectOf: { '@type': 'WebPage', primaryImageOfPage: { '@type': 'ImageObject', contentUrl: 'https://m.media-amazon.com/p.jpg' } },
    }));
    expect(out.image).toEqual(['https://example.com/a.webp']);
    expect(out.logo).toBeUndefined();
    expect(out.thumbnailUrl).toBeUndefined();
    expect(out.subjectOf).toEqual({ '@type': 'WebPage' });
    expect(JSON.parse(serializeJsonLd({ '@type': 'Thing', image: 'https://m.media-amazon.com/x.jpg' })).image).toBeUndefined();
  });

  it('checks the JSON form, so a toJSON cannot smuggle a rating or an Amazon image through', () => {
    const rating = { toJSON: () => ({ '@type': 'AggregateRating', ratingValue: 5 }) };
    const out = JSON.parse(serializeJsonLd({
      '@type': 'Product',
      name: 'x',
      score: rating,
      related: [rating, 'kept'],
      image: [new URL('https://m.media-amazon.com/images/I/a.jpg'), new URL('https://example.com/a.webp')],
      logo: new URL('https://images-na.ssl-images-amazon.com/x.png'),
    }));
    expect(out).toEqual({ '@context': 'https://schema.org', '@type': 'Product', name: 'x', related: ['kept'], image: ['https://example.com/a.webp'] });
  });

  it('escapes everything that could end the script element or open a comment', () => {
    const json = serializeJsonLd({ '@type': 'Thing', name: '</script><script>alert(1)</script> <!-- & \u2028\u2029' });
    expect(json).not.toMatch(/[<>&\u2028\u2029]/);
    expect(JSON.parse(json).name).toBe('</script><script>alert(1)</script> <!-- & \u2028\u2029');
    expect(JSON.parse(json)['@context']).toBe('https://schema.org');
  });

  it('jsonLdScripts emits one parseable block per publishable node and skips the rest', () => {
    const html = jsonLdScripts([
      productLd(ctx, { asin: ASIN, name: 'Alpha' }, NOW),
      null,
      false,
      faqPageLd([]),
      { name: 'no type' },
      { '@graph': [{ '@type': 'Thing' }] },
    ]);
    const blocks = Array.from(html.matchAll(/<script type="application\/ld\+json">([\s\S]*?)<\/script>/g)).map((m) => JSON.parse(m[1] as string));
    expect(blocks).toHaveLength(2);
    for (const b of blocks) expect(b['@context']).toBe('https://schema.org');
    expect(jsonLdScripts([])).toBe('');
  });
});
