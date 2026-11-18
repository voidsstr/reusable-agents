import { STICKY_ACTIVE_CLASS, STICKY_BAR_ATTR, STICKY_HEIGHT_VAR, STICKY_SUPPRESS_ATTR } from '../../server/src/http/clientContract';
import { STICKY_BAR_CSS, STICKY_CTA_LABEL, stickyBuyBarHtml } from '../../server/src/http/stickyBar';
import { containsNotice, priceDisclaimerFor } from '../../server/src/notices';
import { AMAZON_CONTENT_ATTR } from '../../server/src/render/blocks';
import { htmlShowsAmazonContent, needsAmazonNotice } from '../../server/src/render/layout';
import { exampleCtx } from './helpers';

const ctx = exampleCtx();
const ASIN = 'B0DS2X13PH';
const NOW = Date.parse('2026-11-17T12:00:00Z');

function text(html: string): string {
  return html.replace(/<[^>]+>/g, ' ').replace(/&amp;/g, '&').replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/\s+/g, ' ');
}

describe('stickyBuyBarHtml', () => {
  it('renders the bar with the placement\'s own segment tag on the /dp/ button', () => {
    const html = stickyBuyBarHtml(ctx, { asin: ASIN, name: 'Acme <PowerBox> 1000' });
    expect(html.startsWith(`<div class="kit-sticky-bar" ${STICKY_BAR_ATTR}>`)).toBe(true);
    expect(html).toContain(`<div class="kit-sticky-bar__name" ${AMAZON_CONTENT_ATTR}>Acme &lt;PowerBox&gt; 1000</div>`);
    const a = html.match(/<a [^>]*>[^<]*<\/a>/)?.[0] ?? '';
    expect(a).toContain('class="ssr-amz-cta kit-sticky-bar__btn"');
    expect(a).toContain('target="_blank"');
    expect(a).toContain('rel="sponsored nofollow noopener"');
    expect(a).toContain('data-cta-placement="sticky-mobile"');
    expect(a).toContain(`href="https://www.amazon.com/dp/${ASIN}?tag=kitexample-sticky-20&amp;linkCode=ll1"`);
    expect(a).toContain(`>${STICKY_CTA_LABEL}</a>`);
    expect(html).not.toContain('<img');
    expect(html).not.toContain('kit-price-stamp');
  });

  it('marks the name as Amazon content (2(k) notice) unless it is the site\'s own model name', () => {
    const amazonTitle = stickyBuyBarHtml(ctx, { asin: ASIN, name: 'ACME PowerBox 1000 Portable Power Station, 1024Wh LiFePO4' });
    expect(htmlShowsAmazonContent(amazonTitle)).toBe(true);
    // a page that declares no Amazon content still gets the notice because of the bar
    expect(needsAmazonNotice({ amazonContent: false, main: `<h1>About</h1>${amazonTitle}` })).toBe(true);

    const own = stickyBuyBarHtml(ctx, { asin: ASIN, name: 'Acme PowerBox 1000', nameIsOwn: true });
    expect(own).toContain('<div class="kit-sticky-bar__name">Acme PowerBox 1000</div>');
    expect(htmlShowsAmazonContent(own)).toBe(false);
  });

  it('a substitute bar names its own placement, status line and product', () => {
    const html = stickyBuyBarHtml(ctx, {
      asin: 'B0C1234567', name: 'Other Station 1200', placement: 'pdp-sticky-live',
      status: 'This listing is unavailable. Similar, in stock:', label: 'See it on Amazon', segmentTag: 'kitexample-guides-20',
    });
    expect(html).toContain('<div class="kit-sticky-bar__status">This listing is unavailable. Similar, in stock:</div>');
    expect(html).toContain('data-cta-placement="pdp-sticky-live"');
    // no placement tag for pdp-sticky-live: the allowlisted segment tag applies
    expect(html).toContain('tag=kitexample-guides-20');
    expect(html).toContain('>See it on Amazon</a>');
  });

  it('can send the button to a tagged Amazon search instead of a /dp/ page', () => {
    const html = stickyBuyBarHtml(ctx, { url: 'https://www.amazon.com/s?k=acme+powerbox&tag=someone-20', name: 'Acme PowerBox' });
    expect(html).toContain('href="https://www.amazon.com/s?k=acme+powerbox&amp;tag=kitexample-sticky-20"');
  });

  it.each([
    ['no destination', {}],
    ['a pseudo-ASIN', { asin: 'EBAY_12345' }],
    ['a foreign URL', { url: 'https://amazon.com.example.net/s?k=x' }],
  ])('renders nothing with %s (never a dead button)', (_label, o) => {
    expect(stickyBuyBarHtml(ctx, { name: 'X', ...o })).toBe('');
  });

  it('throws on a malformed placement (a typo would split click attribution)', () => {
    expect(() => stickyBuyBarHtml(ctx, { asin: ASIN, name: 'X', placement: 'Sticky Bar' })).toThrow(/placement/);
  });

  it.each([
    ['https://img.example.com/stations/acme.webp', true],
    ['/img/stations/acme.webp', true],
    ['http://insecure.example/x.jpg', false],
    ['javascript:alert(1)', false],
    ['//cdn.example/x.jpg', false],
    ['https://x.example/a" onerror="alert(1)', false],
  ])('thumbnail %s allowed: %s', (src, ok) => {
    const html = stickyBuyBarHtml(ctx, { asin: ASIN, name: 'Acme', nameIsOwn: true, imageUrl: src, imageAlt: 'Acme "1000"' });
    expect(html.includes('<img')).toBe(ok);
    if (ok) expect(html).toContain(`src="${src}" alt="Acme &quot;1000&quot;" width="48" height="48" loading="lazy" decoding="async">`);
  });

  describe('an Amazon-hosted thumbnail (IP License 2(h): from a response inside the price window only)', () => {
    const AMZ = 'https://m.media-amazon.com/images/I/41abc.jpg';
    const bar = (o: Record<string, unknown>) => stickyBuyBarHtml(ctx, { asin: ASIN, name: 'Acme', nameIsOwn: true, imageUrl: AMZ, now: NOW, ...o });

    it('is shown, marked as Amazon content, when fetched within the window', () => {
      const html = bar({ imageFetchedAt: new Date(NOW - 3600_000).toISOString() });
      expect(html).toContain(`<img class="kit-sticky-bar__img" src="${AMZ}" alt="Acme" width="48" height="48" loading="lazy" decoding="async" ${AMAZON_CONTENT_ATTR}>`);
      expect(htmlShowsAmazonContent(html)).toBe(true);
      expect(bar({ imageFetchedAt: NOW - 60_000 })).toContain('<img');
    });

    it.each<[string, unknown]>([
      ['no fetch time', undefined],
      ['a stale fetch (25h)', new Date(NOW - 25 * 3600_000).toISOString()],
      ['past the quotable window (24h minus the HTML cache age)', NOW - (ctx.prices.quotableMaxAgeSeconds + 60) * 1000],
      ['a fetch time in the future', NOW + 3600_000],
      ['an unparseable time', 'yesterday-ish'],
    ])('is left out with %s', (_label, imageFetchedAt) => {
      const html = bar({ imageFetchedAt });
      expect(html).not.toContain('<img');
      expect(html).toContain('kit-sticky-bar__btn'); // the bar itself still renders
    });

    it.each([
      'https://images-na.ssl-images-amazon.com/images/I/x.jpg',
      'https://ws-na.amazon-adsystem.com/widgets/q?ServiceVersion=20070822&ASIN=B0DS2X13PH',
    ])('applies to every Amazon image host: %s', (src) => {
      expect(stickyBuyBarHtml(ctx, { asin: ASIN, name: 'Acme', imageUrl: src, now: NOW })).not.toContain('<img');
    });
  });

  it('shows a price only as the full stamp with the 2(i) disclaimer, and only while quotable', () => {
    const fresh = { price: 499, currency: 'USD', fetchedAt: new Date(NOW - 3600_000).toISOString() };
    const html = stickyBuyBarHtml(ctx, { asin: ASIN, name: 'Acme', price: fresh, now: NOW });
    expect(html).toContain('<div class="kit-sticky-bar__price"><span class="kit-price-stamp" data-price-stamp="price">');
    expect(text(html)).toContain('$499.00 on Amazon.com as of 2026-11-17 11:00 UTC (price may vary)');
    expect(containsNotice(text(html), priceDisclaimerFor(ctx))).toBe(true);

    const stale = { ...fresh, fetchedAt: new Date(NOW - 25 * 3600_000).toISOString() };
    const noPrice = stickyBuyBarHtml(ctx, { asin: ASIN, name: 'Acme', price: stale, now: NOW });
    expect(noPrice).not.toContain('kit-sticky-bar__price');
    expect(noPrice).not.toContain('$499');
  });

  it('never puts a price beside a search button (a search page is not one product)', () => {
    const fresh = { price: 499, currency: 'USD', fetchedAt: new Date(NOW - 3600_000).toISOString() };
    const html = stickyBuyBarHtml(ctx, { url: 'https://www.amazon.com/s?k=acme+powerbox', name: 'Acme PowerBox', price: fresh, now: NOW });
    expect(html).toContain('href="https://www.amazon.com/s?k=acme+powerbox&amp;tag=kitexample-sticky-20"');
    expect(html).not.toContain('kit-price-stamp');
    expect(html).not.toContain('$499');
  });

  it('an invalid ASIN never falls back to the url (no silent change of destination)', () => {
    expect(stickyBuyBarHtml(ctx, { asin: 'EBAY_12345', url: 'https://www.amazon.com/s?k=x', name: 'X' })).toBe('');
  });
});

describe('STICKY_BAR_CSS', () => {
  it('shows the bar on phones only, pads the page by its measured height, and hides it in print or when suppressed', () => {
    expect(STICKY_BAR_CSS).toContain('.kit-sticky-bar{display:none;position:fixed;');
    expect(STICKY_BAR_CSS).toContain(
      `@media (max-width:768px){.kit-sticky-bar{display:block}html.${STICKY_ACTIVE_CLASS} body{padding-bottom:var(${STICKY_HEIGHT_VAR},96px)}`
      + `body:has(.kit-sticky-bar){padding-bottom:var(${STICKY_HEIGHT_VAR},96px)}}`,
    );
    expect(STICKY_BAR_CSS).toContain(`body:has([${STICKY_SUPPRESS_ATTR}]) .kit-sticky-bar{display:none!important}`);
    expect(STICKY_BAR_CSS).toContain('@media print{.kit-sticky-bar{display:none!important}}');
  });

  it('hides a price stamp in the bar where popovers are unsupported, and uses the theme tokens', () => {
    expect(STICKY_BAR_CSS).toContain('@supports not selector(:popover-open){.kit-sticky-bar .kit-price-stamp{display:none}}');
    for (const v of ['--kit-surface', '--kit-text', '--kit-border', '--kit-cta-bg', '--kit-cta-text', '--kit-focus']) {
      expect(STICKY_BAR_CSS).toContain(`var(${v}`);
    }
    // balanced braces
    expect((STICKY_BAR_CSS.match(/{/g) || []).length).toBe((STICKY_BAR_CSS.match(/}/g) || []).length);
  });

  it('never lists :has() with another selector (an unsupported selector voids the whole list)', () => {
    const selectors = STICKY_BAR_CSS.replace(/@media[^{]*\{/g, '').replace(/@supports[^{]*\{/g, '').split('}').map((r) => r.split('{')[0]?.trim() ?? '');
    const lists = selectors.filter((sel) => sel.includes(':has(') && sel.includes(','));
    expect(lists).toEqual([]);
  });
});
