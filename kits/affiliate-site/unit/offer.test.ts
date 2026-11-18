/**
 * Affiliate-honest Offer JSON-LD and the quotable-price rule.
 *
 * Ported from SpecPicks tests/unit/affiliateOfferAndClicks.test.ts (offer
 * half), repurposedListingPrice.test.ts and honestyStructuredData.test.ts
 * ("list surfaces"), adapted to the kit's SiteContext window: 24h minus the
 * longest HTML cache age (here 660 s).
 */
import * as path from 'path';
import {
  FUTURE_SKEW_MS,
  MAX_LIST_PRICE_RATIO,
  REPURPOSED_LISTING_SQL,
  affiliateOfferLd,
  honestListPrice,
  isQuotable,
  isRepurposedListing,
  offerAvailabilityFromText,
  offerAvailabilityFromType,
  offerableProductLd,
  parsePriceAmount,
  priceCheckedAt,
  productListItemLd,
  quotableMaxAgeMs,
  quotablePrice,
} from '../server/src/offer';
import { loadSiteContext } from '../server/src/siteContext';

const ctx = loadSiteContext(path.resolve(__dirname, '../fixtures/site.example.json'), { env: {} });
const NOW = Date.parse('2026-11-17T12:00:00Z');
const hoursAgo = (h: number) => new Date(NOW - h * 3600 * 1000).toISOString();
const msAgo = (ms: number) => new Date(NOW - ms).toISOString();
const FLAG = 'shelf-20261111T1325-listing-repurposed';

describe('affiliateOfferLd', () => {
  it('emits price + availability only for a price fetched inside the window', () => {
    const o = affiliateOfferLd(ctx, { url: 'u', price: '129.5', currency: 'USD', fetchedAt: hoursAgo(2) }, NOW);
    expect(o).toMatchObject({ '@type': 'Offer', price: '129.50', priceCurrency: 'USD', availability: 'https://schema.org/InStock' });
  });

  it.each([
    ['In Stock', 'https://schema.org/InStock'],
    ['', 'https://schema.org/InStock'],
    ['Usually ships within 2 to 3 days', 'https://schema.org/InStock'],
    ['Currently unavailable.', 'https://schema.org/OutOfStock'],
    ['Temporarily out of stock.', 'https://schema.org/OutOfStock'],
    ['This item will be released on October 30, 2026.', 'https://schema.org/PreOrder'],
    ['Usually ships within 1 to 2 months', 'https://schema.org/BackOrder'],
    ['Only 5 left in stock - order soon.', 'https://schema.org/LimitedAvailability'],
    // The framework price refresh's stand-in texts for orderable listings with an empty message.
    ['Pre-order on Amazon; not shipping yet', 'https://schema.org/PreOrder'],
    ['Available to ship in a later window; not in stock now', 'https://schema.org/BackOrder'],
  ])('maps stock text %j to %s', (text, want) => {
    expect(offerAvailabilityFromText(text)).toBe(want);
    const o = affiliateOfferLd(ctx, { url: 'u', price: 50, currency: 'USD', fetchedAt: hoursAgo(1), availabilityText: text }, NOW);
    expect(o).toMatchObject({ availability: want });
  });

  it.each([
    ['IN_STOCK_SCARCE', 'https://schema.org/LimitedAvailability'],
    ['PREORDER', 'https://schema.org/PreOrder'],
    ['AVAILABLE_DATE', 'https://schema.org/PreOrder'],
    ['LEADTIME', 'https://schema.org/BackOrder'],
    ['OUT_OF_STOCK', 'https://schema.org/OutOfStock'],
  ])('a Creators availability type %s wins over the stock text', (type, want) => {
    expect(offerAvailabilityFromType(type)).toBe(want);
    const o = affiliateOfferLd(ctx, { url: 'u', price: 50, fetchedAt: hoursAgo(1), availabilityType: type, availabilityText: 'In Stock' }, NOW);
    expect(o).toMatchObject({ availability: want });
  });

  it('IN_STOCK or an unknown type falls back to the stock text', () => {
    expect(offerAvailabilityFromType('IN_STOCK')).toBeNull();
    expect(offerAvailabilityFromType(undefined)).toBeNull();
    const o = affiliateOfferLd(ctx, { url: 'u', price: 50, fetchedAt: hoursAgo(1), availabilityType: 'IN_STOCK', availabilityText: 'Only 2 left in stock' }, NOW);
    expect(o).toMatchObject({ availability: 'https://schema.org/LimitedAvailability' });
  });

  it('dates the Offer with the fetch time (validFrom = fetched_at), and promises no validity window', () => {
    const o = affiliateOfferLd(ctx, { url: 'u', price: 50, currency: 'USD', fetchedAt: hoursAgo(5) }, NOW) as Record<string, unknown>;
    expect(o.validFrom).toBe(hoursAgo(5));
    expect(o).not.toHaveProperty('priceValidUntil');
  });

  it('never claims a seller, return policy or shipping terms', () => {
    const o = affiliateOfferLd(ctx, { url: 'u', price: 50, currency: 'USD', fetchedAt: hoursAgo(1) }, NOW) as Record<string, unknown>;
    expect(o).not.toHaveProperty('seller');
    expect(o).not.toHaveProperty('hasMerchantReturnPolicy');
    expect(o).not.toHaveProperty('shippingDetails');
    expect(o.itemCondition).toBe('https://schema.org/NewCondition');
  });

  it.each([
    ['stale price', { price: 50, currency: 'USD', fetchedAt: hoursAgo(25) }],
    ['no timestamp', { price: 50, currency: 'USD', fetchedAt: null }],
    ['an unparseable timestamp', { price: 50, currency: 'USD', fetchedAt: 'yesterday' }],
    ['$0 price', { price: 0, currency: 'USD', fetchedAt: hoursAgo(1) }],
    ['sub-$1 price', { price: 0.5, currency: 'USD', fetchedAt: hoursAgo(1) }],
    ['non-numeric price', { price: 'call', currency: 'USD', fetchedAt: hoursAgo(1) }],
    ['foreign currency', { price: 50, currency: 'INR', fetchedAt: hoursAgo(1) }],
    ['a repurposed listing', { price: 50, currency: 'USD', fetchedAt: hoursAgo(1), qualityFlags: [FLAG] }],
  ])('emits no Offer for %s', (_label, f) => {
    expect(affiliateOfferLd(ctx, { url: 'u', ...f }, NOW)).toBeNull();
    expect(quotablePrice(ctx, f, NOW)).toBeNull();
    expect(isQuotable(ctx, f, NOW)).toBe(false);
  });
});

describe('the quotable window is 24h minus the HTML cache age', () => {
  it('is derived from SiteContext', () => {
    expect(quotableMaxAgeMs(ctx)).toBe((24 * 3600 - 660) * 1000);
  });

  it('a 23h55m-old price (quotable on SpecPicks) is not quotable here', () => {
    expect(quotablePrice(ctx, { price: 99, fetchedAt: msAgo(24 * 3600e3 - 5 * 60e3) }, NOW)).toBeNull();
  });

  it('the boundary is inclusive', () => {
    const edge = quotableMaxAgeMs(ctx);
    expect(quotablePrice(ctx, { price: 99, fetchedAt: msAgo(edge) }, NOW)).toBe(99);
    expect(quotablePrice(ctx, { price: 99, fetchedAt: msAgo(edge + 1) }, NOW)).toBeNull();
  });

  it('tolerates five minutes of clock skew, not more', () => {
    expect(quotablePrice(ctx, { price: 99, fetchedAt: msAgo(-FUTURE_SKEW_MS) }, NOW)).toBe(99);
    expect(quotablePrice(ctx, { price: 99, fetchedAt: msAgo(-FUTURE_SKEW_MS - 1) }, NOW)).toBeNull();
  });

  it('follows a tighter site window', () => {
    const tight = { prices: { freshnessHours: 6, quotableMaxAgeSeconds: 6 * 3600 - 660 } };
    expect(quotablePrice(tight, { price: 99, fetchedAt: hoursAgo(5) }, NOW)).toBe(99);
    expect(quotablePrice(tight, { price: 99, fetchedAt: hoursAgo(6) }, NOW)).toBeNull();
  });
});

describe('price fields', () => {
  it('accepts SpecPicks\' priceUpdatedAt as an alias; fetchedAt wins when both are set', () => {
    expect(quotablePrice(ctx, { price: 10, priceUpdatedAt: hoursAgo(1) }, NOW)).toBe(10);
    expect(quotablePrice(ctx, { price: 10, fetchedAt: hoursAgo(30), priceUpdatedAt: hoursAgo(1) }, NOW)).toBeNull();
    expect(priceCheckedAt({ fetchedAt: new Date(NOW) })?.getTime()).toBe(NOW);
    expect(priceCheckedAt({})).toBeNull();
  });

  it('treats an unset or lowercase-usd currency as USD, and numeric strings as numbers', () => {
    expect(quotablePrice(ctx, { price: '1299.99', fetchedAt: hoursAgo(1) }, NOW)).toBe(1299.99);
    expect(quotablePrice(ctx, { price: 10, currency: 'usd', fetchedAt: hoursAgo(1) }, NOW)).toBe(10);
    expect(quotablePrice(ctx, { price: 1, currency: null, fetchedAt: hoursAgo(1) }, NOW)).toBe(1);
  });

  it.each<[string, unknown]>([
    ['a thousands separator (parseFloat read it as $1)', '1,299.00'],
    ['trailing junk (parseFloat read it as $12)', '12abc'],
    ['a currency symbol', '$5'],
    ['exponent notation', '1e3'],
    ['a negative amount', '-25'],
    ['NaN', Number.NaN],
    ['Infinity', Number.POSITIVE_INFINITY],
    ['an object', { amount: 50 }],
  ])('quotes nothing for %s, and emits no Offer', (_label, price) => {
    const f = { price: price as string, currency: 'USD', fetchedAt: hoursAgo(1) };
    expect(quotablePrice(ctx, f, NOW)).toBeNull();
    expect(affiliateOfferLd(ctx, { url: 'u', ...f }, NOW)).toBeNull();
  });

  it('accepts the plain decimals Postgres NUMERIC and the price writers produce', () => {
    expect(quotablePrice(ctx, { price: ' 25.50 ', fetchedAt: hoursAgo(1) }, NOW)).toBe(25.5);
    expect(quotablePrice(ctx, { price: '19.990000000000002', fetchedAt: hoursAgo(1) }, NOW)).toBe(19.990000000000002);
    expect(affiliateOfferLd(ctx, { url: 'u', price: '19.990000000000002', fetchedAt: hoursAgo(1) }, NOW)).toMatchObject({ price: '19.99' });
  });
});

describe('parsePriceAmount', () => {
  it.each<[unknown, number | null]>([
    [1299.99, 1299.99],
    ['1299.99', 1299.99],
    ['  42 ', 42],
    ['0', 0],
    ['1,299.00', null],
    ['1.299,00', null],
    ['12abc', null],
    ['$5', null],
    ['', null],
    ['.99', null],
    ['12.', null],
    ['1e3', null],
    ['9'.repeat(400), null],
    [Number.NaN, null],
    [null, null],
    [undefined, null],
    [true, null],
  ])('%j -> %j', (v, want) => {
    expect(parsePriceAmount(v)).toBe(want);
  });
});

describe('isRepurposedListing', () => {
  it('matches the shelf flag in an array or a JSON string of one', () => {
    expect(isRepurposedListing([FLAG])).toBe(true);
    expect(isRepurposedListing(['no-buybox', 'shelf-20261015T1225-listing-repurposed'])).toBe(true);
    expect(isRepurposedListing(JSON.stringify([FLAG]))).toBe(true);
  });
  it('is false for {}, null, other flags and other shapes', () => {
    expect(isRepurposedListing({})).toBe(false);
    expect(isRepurposedListing(null)).toBe(false);
    expect(isRepurposedListing(undefined)).toBe(false);
    expect(isRepurposedListing(['no-buybox'])).toBe(false);
    expect(isRepurposedListing('[]')).toBe(false);
    expect(isRepurposedListing('not json')).toBe(false);
    expect(isRepurposedListing(['listing-repurposed'])).toBe(false);
  });
  it('leaves the price rule unchanged for other flag shapes and when omitted', () => {
    const base = { price: '99', currency: 'USD', fetchedAt: hoursAgo(1) };
    expect(quotablePrice(ctx, { ...base, qualityFlags: {} }, NOW)).toBe(99);
    expect(quotablePrice(ctx, { ...base, qualityFlags: null }, NOW)).toBe(99);
    expect(quotablePrice(ctx, { ...base, qualityFlags: ['no-buybox'] }, NOW)).toBe(99);
    expect(quotablePrice(ctx, { ...base, qualityFlags: '[]' }, NOW)).toBe(99);
    expect(quotablePrice(ctx, base, NOW)).toBe(99);
  });
});

describe('REPURPOSED_LISTING_SQL', () => {
  it('is the price refresh predicate: array guard + regex, no bare %', () => {
    const sql = REPURPOSED_LISTING_SQL('p');
    expect(sql).toContain("jsonb_typeof(p.quality_flags) = 'array'");
    expect(sql).toContain("rf ~ '^shelf-.+-listing-repurposed$'");
    expect(sql).not.toContain('%');
    expect(REPURPOSED_LISTING_SQL('x')).toContain('x.quality_flags');
    expect(REPURPOSED_LISTING_SQL('')).toContain('jsonb_typeof(quality_flags)');
  });
  it('refuses an alias that is not an identifier', () => {
    expect(() => REPURPOSED_LISTING_SQL('p; DROP TABLE products')).toThrow(/bad alias/);
  });
});

describe('honestListPrice', () => {
  it('drops a list price above 3x, below the price, or for a sub-$1 price', () => {
    expect(MAX_LIST_PRICE_RATIO).toBe(3);
    expect(honestListPrice(20.69, 494.18)).toBeNull();
    expect(honestListPrice(100, 300)).toBe(300);
    expect(honestListPrice(100, 300.01)).toBeNull();
    expect(honestListPrice(100, 90)).toBeNull();
    expect(honestListPrice(0.5, 1)).toBeNull();
    expect(honestListPrice('100', '150.00')).toBe(150);
    expect(honestListPrice(null, 150)).toBeNull();
    expect(honestListPrice(100, null)).toBeNull();
    expect(honestListPrice('abc', 150)).toBeNull();
  });

  it('parses both amounts strictly: no strike-through drawn from a misread number', () => {
    // parseFloat made '1,299.00' a $1 price, so a $2.50 "list" looked honest.
    expect(honestListPrice('1,299.00', '2.50')).toBeNull();
    expect(honestListPrice('100', '150,00')).toBeNull();
    expect(honestListPrice('100', '150abc')).toBeNull();
    expect(honestListPrice(' 100 ', '150.00')).toBe(150);
  });
});

describe('list surfaces', () => {
  const base = { '@context': 'https://schema.org', '@type': 'Product', name: 'X', url: 'https://example.com/product/B0EXAMPLE1', sku: 'B0EXAMPLE1' };

  it('a stale price becomes a name + url ListItem, a fresh one a Product with an Offer', () => {
    expect(productListItemLd(ctx, 1, base, { url: 'u', price: '99.99', fetchedAt: hoursAgo(30) }, NOW))
      .toEqual({ '@type': 'ListItem', position: 1, name: 'X', url: 'https://example.com/product/B0EXAMPLE1' });
    const fresh = productListItemLd(ctx, 2, base, { url: 'u', price: '99.99', fetchedAt: hoursAgo(2) }, NOW) as any;
    expect(fresh.item.offers.price).toBe('99.99');
    expect(fresh.item).not.toHaveProperty('@context');
    expect(offerableProductLd(ctx, base, { url: 'u', price: '0.5', fetchedAt: hoursAgo(2) }, NOW)).toBeNull();
  });
});
