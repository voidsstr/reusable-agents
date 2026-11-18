/**
 * Price and availability stamps: every shown price carries its UTC check time
 * and the exact IP License 2(i) disclaimer behind an adjacent "Details"
 * control; nothing renders for a price that is not quotable; availability
 * statements follow the same window. priceLabel/priceHtml keep SpecPicks'
 * behaviour.
 */
import * as path from 'path';
import {
  CURRENCY_SYMBOLS,
  PRICE_MAY_VARY,
  PRICE_STAMP_CSS,
  availabilityFallbackHtml,
  availabilityIsShowable,
  availabilityKindFromType,
  availabilityStamp,
  priceDetailsHtml,
  priceHtml,
  priceLabel,
  priceStamp,
  timeHtml,
  utcStamp,
  type AvailabilityKind,
} from '../server/src/price';
import { priceDisclaimerFor, PRICE_AVAILABILITY_DISCLAIMER_TEMPLATE } from '../server/src/notices';
import { loadSiteContext } from '../server/src/siteContext';

const ctx = loadSiteContext(path.resolve(__dirname, '../fixtures/site.example.json'), { env: {} });
const NOW = Date.parse('2026-11-17T12:00:00Z');
const CHECKED = '2026-11-17T10:11:42.000Z';
const DISCLAIMER = priceDisclaimerFor(ctx);

/** Visible text of a fragment (tags dropped, entities for & < > decoded). */
function text(html: string): string {
  return html.replace(/<[^>]+>/g, '').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
}

/** Block-level tags would break a stamp placed inside a <p> (the parser closes the paragraph). */
const BLOCK_TAG = /<(div|p|details|summary|section|ul|ol|li|table|h[1-6]|blockquote|figure|form)\b/i;

describe('priceLabel (ported)', () => {
  it.each<[unknown, string | null | undefined, string]>([
    [1400.68, 'USD', '$1,400.68'],
    ['129.5', 'USD', '$129.50'],
    [1299, undefined, '$1,299.00'],
    [25, null, '$25.00'],
    [711.75, 'cad', 'CA$711.75'],
    [12, 'GBP', '£12.00'],
    [10, 'XYZ', 'XYZ 10.00'],
    [10, '__proto__', '__PROTO__ 10.00'],
    [0, 'USD', ''],
    [-5, 'USD', ''],
    ['abc', 'USD', ''],
    [null, 'USD', ''],
    // Strict amounts (kit change): parseFloat read these as $1.00 and $12.00.
    ['1,299.00', 'USD', ''],
    ['12abc', 'USD', ''],
    ['$5', 'USD', ''],
    [' 25.50 ', 'USD', '$25.50'],
  ])('%j %j -> %j', (price, cur, want) => {
    expect(priceLabel(price as number, cur)).toBe(want);
  });

  it('knows SpecPicks\' currency symbols', () => {
    expect(CURRENCY_SYMBOLS.INR).toBe('₹');
    expect(Object.isFrozen(CURRENCY_SYMBOLS)).toBe(true);
  });
});

describe('priceHtml (ported)', () => {
  it('wraps the amount, with the listing\'s symbol, ungrouped', () => {
    expect(priceHtml(1299)).toBe('<span class="ssr-price">$1299.00</span>');
    expect(priceHtml('39.9', 'EUR')).toBe('<span class="ssr-price">€39.90</span>');
    expect(priceHtml(0)).toBe('');
    expect(priceHtml(null)).toBe('');
    expect(priceHtml('n/a')).toBe('');
    expect(priceHtml('1,299.00')).toBe('');
    expect(priceHtml('39.9abc')).toBe('');
  });
});

describe('UTC stamps', () => {
  it('formats to the minute in UTC', () => {
    expect(utcStamp(CHECKED)).toBe('2026-11-17 10:11 UTC');
    expect(utcStamp(new Date('2026-01-02T03:04:59Z'))).toBe('2026-01-02 03:04 UTC');
    expect(utcStamp(Date.parse('2026-11-17T23:59:00-05:00'))).toBe('2026-11-18 04:59 UTC');
    expect(timeHtml(CHECKED)).toBe('<time datetime="2026-11-17T10:11:42.000Z">2026-11-17 10:11 UTC</time>');
  });
  it('is empty for a missing or invalid time', () => {
    expect(utcStamp(null)).toBe('');
    expect(utcStamp('')).toBe('');
    expect(utcStamp('not a date')).toBe('');
    expect(timeHtml(undefined)).toBe('');
  });
});

describe('priceStamp', () => {
  const fresh = { price: '1299', currency: 'USD', fetchedAt: CHECKED };

  it('shows the price, the UTC check time, "(price may vary)" and Details with the exact 2(i) text', () => {
    const html = priceStamp(ctx, fresh, { now: NOW, key: 'B0DS2X13PH|pdp-hero' });
    expect(html).toMatch(/^<span class="kit-price-stamp" data-price-stamp="price">/);
    expect(html).toContain('<span class="ssr-price">$1,299.00</span>');
    expect(html).toContain('on Amazon.com as of <time datetime="2026-11-17T10:11:42.000Z">2026-11-17 10:11 UTC</time> (price may vary)');
    expect(html).toContain(DISCLAIMER); // verbatim in the HTML source, not only after decoding
    expect(DISCLAIMER).toBe(PRICE_AVAILABILITY_DISCLAIMER_TEMPLATE.replace('[relevant Amazon Site(s), as applicable]', 'Amazon.com'));
    expect(text(html)).toBe(`$1,299.00 on Amazon.com as of 2026-11-17 10:11 UTC ${PRICE_MAY_VARY} Details ${DISCLAIMER}`);
  });

  it('wires Details to its disclaimer as a native popover', () => {
    const html = priceStamp(ctx, fresh, { now: NOW, key: 'B0DS2X13PH|pdp-hero' });
    const target = html.match(/popovertarget="([^"]+)"/)?.[1];
    const id = html.match(/<span id="([^"]+)" class="kit-price-disclaimer" popover role="note" data-nosnippet>/)?.[1];
    expect(target).toMatch(/^kit-pd-[0-9a-f]{8}$/);
    expect(id).toBe(target);
    expect(html).toContain('<button type="button" class="kit-price-details"');
    expect(html).toMatch(/>Details<\/button>/);
  });

  it('two un-keyed stamps on one page never share a popover id (same price, same refresh batch)', () => {
    const ids = (html: string) => [html.match(/popovertarget="([^"]+)"/)?.[1], html.match(/<span id="([^"]+)"/)?.[1]];
    const a = priceStamp(ctx, { price: '299.99', currency: 'USD', fetchedAt: CHECKED }, { now: NOW });
    const b = priceStamp(ctx, { price: '299.99', currency: 'USD', fetchedAt: CHECKED }, { now: NOW });
    const [ta, sa] = ids(a);
    const [tb, sb] = ids(b);
    expect(ta).toBe(sa);
    expect(tb).toBe(sb);
    expect(ta).not.toBe(tb);
    expect(ta).toMatch(/^kit-pd-_[0-9a-z]+$/);
    const av = (k?: string) => availabilityStamp(ctx, { kind: 'in-stock', checkedAt: CHECKED }, { now: NOW, key: k });
    expect(ids(av())[0]).not.toBe(ids(av())[0]);
    expect(ids(av('B0DS2X13PH'))[0]).toBe(ids(av('B0DS2X13PH'))[0]);
  });

  it('is phrasing content only, so it is safe inside a <p>', () => {
    expect(priceStamp(ctx, fresh, { now: NOW })).not.toMatch(BLOCK_TAG);
    expect(availabilityStamp(ctx, { kind: 'unavailable', checkedAt: CHECKED }, { now: NOW })).not.toMatch(BLOCK_TAG);
  });

  it('derives stable ids, distinct per key, and sanitises an explicit id', () => {
    const a = priceStamp(ctx, fresh, { now: NOW, key: 'hero' });
    expect(priceStamp(ctx, fresh, { now: NOW, key: 'hero' })).toBe(a);
    expect(priceStamp(ctx, fresh, { now: NOW, key: 'sticky' })).not.toBe(a);
    expect(priceStamp(ctx, fresh, { now: NOW, id: 'hero-1"><script>' })).toContain('popovertarget="kit-pd-hero-1script"');
  });

  it.each([
    ['a stale price', { ...fresh, fetchedAt: '2026-11-16T11:00:00Z' }],
    ['a price inside 24h but past 24h minus the cache age', { ...fresh, fetchedAt: '2026-11-16T12:05:00Z' }],
    ['no check time', { ...fresh, fetchedAt: null }],
    ['a non-USD listing', { ...fresh, currency: 'CAD' }],
    ['a sub-$1 price', { ...fresh, price: '0.99' }],
    ['a repurposed listing', { ...fresh, qualityFlags: ['shelf-20261111T1325-listing-repurposed'] }],
  ])('renders nothing for %s', (_label, f) => {
    expect(priceStamp(ctx, f, { now: NOW })).toBe('');
  });

  it('escapes nothing it should not: the stamp names the site\'s Amazon Site', () => {
    expect(priceStamp(ctx, fresh, { now: NOW })).toContain('on Amazon.com as of');
  });
});

describe('availabilityStamp', () => {
  it('dates an unavailable statement and carries the disclaimer', () => {
    const html = availabilityStamp(ctx, { kind: 'unavailable', checkedAt: CHECKED }, { now: NOW, key: 'B0DS2X13PH' });
    expect(html).toMatch(/^<span class="kit-price-stamp" data-price-stamp="availability">/);
    expect(text(html)).toBe(`Unavailable on Amazon.com as of 2026-11-17 10:11 UTC Details ${DISCLAIMER}`);
    expect(html).toContain(DISCLAIMER);
  });

  it.each<[AvailabilityKind, string]>([
    ['in-stock', 'In stock on Amazon.com'],
    ['limited', 'Limited stock on Amazon.com'],
    ['preorder', 'Available for pre-order on Amazon.com'],
    ['ships-later', 'Not in stock on Amazon.com now; ships later'],
  ])('%s -> %s', (kind, words) => {
    const html = availabilityStamp(ctx, { kind, checkedAt: CHECKED }, { now: NOW });
    expect(text(html).startsWith(`${words} as of 2026-11-17 10:11 UTC`)).toBe(true);
    expect(html).toContain(`kit-availability--${kind}`);
  });

  it('states nothing (and needs no stamp) once the check is outside the window', () => {
    const stale = availabilityStamp(ctx, { kind: 'unavailable', checkedAt: '2026-11-16T11:00:00Z' }, { now: NOW });
    expect(stale).toBe(availabilityFallbackHtml(ctx));
    expect(text(stale)).toBe('See current price and availability on Amazon.com');
    expect(stale).not.toMatch(/as of|Unavailable|In stock/);
    expect(availabilityStamp(ctx, { kind: 'in-stock', checkedAt: null }, { now: NOW })).toBe(availabilityFallbackHtml(ctx));
    expect(availabilityIsShowable(ctx, '2026-11-17T12:10:00Z', NOW)).toBe(false); // 10 min in the future
    expect(availabilityIsShowable(ctx, '2026-11-17T12:04:00Z', NOW)).toBe(true);
  });

  it.each<[string, unknown]>([
    ['null', null],
    ['an unmapped Creators type', 'in_stock'],
    ['an Object.prototype member', 'constructor'],
    ['another', '__proto__'],
    ['a number', 7],
  ])('renders the neutral copy, and does not throw, for %s as the kind', (_label, kind) => {
    const html = availabilityStamp(ctx, { kind: kind as AvailabilityKind, checkedAt: CHECKED }, { now: NOW });
    expect(html).toBe(availabilityFallbackHtml(ctx));
  });

  it('maps Creators availability types', () => {
    expect(availabilityKindFromType('IN_STOCK')).toBe('in-stock');
    expect(availabilityKindFromType('in_stock_scarce')).toBe('limited');
    expect(availabilityKindFromType('PREORDER')).toBe('preorder');
    expect(availabilityKindFromType('AVAILABLE_DATE')).toBe('preorder');
    expect(availabilityKindFromType('LEADTIME')).toBe('ships-later');
    expect(availabilityKindFromType('OUT_OF_STOCK')).toBe('unavailable');
    expect(availabilityKindFromType('UNKNOWN')).toBeNull();
    expect(availabilityKindFromType(null)).toBeNull();
  });
});

describe('priceDetailsHtml and CSS', () => {
  it('can be reused by a custom stamp layout', () => {
    const html = priceDetailsHtml(ctx, { id: 'custom' });
    expect(html).toContain('popovertarget="kit-pd-custom"');
    expect(html).toContain(`id="kit-pd-custom"`);
    expect(html).toContain(DISCLAIMER);
  });

  it('gives each call without an id or seed its own popover id; a seed is stable', () => {
    const target = (html: string) => html.match(/popovertarget="([^"]+)"/)?.[1];
    expect(target(priceDetailsHtml(ctx))).not.toBe(target(priceDetailsHtml(ctx)));
    expect(target(priceDetailsHtml(ctx, {}))).not.toBe(target(priceDetailsHtml(ctx, {})));
    expect(target(priceDetailsHtml(ctx, { seed: 'x' }))).toBe(target(priceDetailsHtml(ctx, { seed: 'x' })));
  });

  it('ships CSS that keeps the disclaimer adjacent where popovers are unsupported', () => {
    expect(PRICE_STAMP_CSS).toContain('.kit-price-details{');
    expect(PRICE_STAMP_CSS).toContain('@supports not selector(:popover-open){.kit-price-details{display:none}.kit-price-disclaimer{display:inline');
  });
});
