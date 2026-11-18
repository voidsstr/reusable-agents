/**
 * End to end: a guide page and a product page assembled from every render
 * helper (and K1's CTA and price stamp) come out as sound HTML that keeps
 * every licence rule at once: the exact 2(k) notice and the earnings
 * disclosure in view, every Amazon link tagged from the site's own set with
 * rel="sponsored nofollow noopener", every price with its stamp and the 2(i)
 * text, no Amazon-hosted image anywhere, GFM tables as table.md-table, one
 * <main> and one <h1>, and JSON-LD that parses, with an Offer only when the
 * price is quotable and never an AggregateRating.
 */
import * as fs from 'fs';
import * as path from 'path';
import { AFFILIATE_REL, cta } from '../../server/src/links';
import { AMAZON_CONTENT_NOTICE, containsNotice, priceDisclaimerFor } from '../../server/src/notices';
import { priceStamp } from '../../server/src/price';
import { loadSiteContext } from '../../server/src/siteContext';
import {
  INDEXABLE_PAGE,
  amazonTextHtml,
  articleLd,
  faqPageLd,
  faqSectionHtml,
  isSimilarToLd,
  itemListLd,
  jsonLdScripts,
  mdToHtml,
  pageTitle,
  productLd,
  renderPage,
} from '../../server/src/render';
import { browserVisibleText, htmlProblems } from './htmlCheck';

const KIT = path.resolve(__dirname, '../..');
const ctx = loadSiteContext(path.join(KIT, 'fixtures/site.example.json'), { env: {} });
const NOW = Date.parse('2026-11-17T12:00:00Z');
const FRESH = '2026-11-17T10:11:42.000Z';
const STALE = '2026-11-16T09:00:00.000Z';

function text(html: string): string {
  const body = html.slice(html.indexOf('<body'));
  return body.replace(/<(script|style)\b[\s\S]*?<\/\1>/g, ' ').replace(/<[^>]+>/g, ' ')
    .replace(/&#39;/g, "'").replace(/&quot;/g, '"').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&copy;/g, '©').replace(/&amp;/g, '&');
}

function jsonLd(html: string): Array<Record<string, unknown>> {
  return Array.from(html.matchAll(/<script type="application\/ld\+json">([\s\S]*?)<\/script>/g)).map((m) => JSON.parse(m[1] as string));
}

/** The licence and structure rules every page must keep. */
function expectCompliantPage(html: string): void {
  expect(htmlProblems(html)).toEqual([]);
  expect(html.match(/<main\b/g)).toHaveLength(1);
  expect(html.match(/<h1\b/g)).toHaveLength(1);
  expect(containsNotice(text(html), AMAZON_CONTENT_NOTICE)).toBe(true);
  expect(text(html)).toContain(ctx.disclosure.earnings);
  // ... and nothing before them swallows them.
  expect(containsNotice(browserVisibleText(html), AMAZON_CONTENT_NOTICE)).toBe(true);
  expect(browserVisibleText(html)).toContain(ctx.disclosure.earnings);
  expect(html).not.toMatch(/media-amazon\.com|ssl-images-amazon\.com|images-amazon\.com|amzn\.to/);
  for (const m of html.matchAll(/<a\b([^>]*)>/g)) {
    const attrs = m[1] as string;
    const href = /href="([^"]*)"/.exec(attrs)?.[1]?.replace(/&amp;/g, '&') ?? '';
    const amazon = /^https?:\/\/(?:[a-z0-9-]+\.)*amazon\./i.test(href);
    const redirect = /^https:\/\/example\.com\/r\/[A-Z0-9]{10}\/?(?:[?#]|$)/.test(href);
    if (!amazon && !redirect) continue;
    if (amazon) {
      expect(new URL(href).searchParams.getAll('tag').every((t) => ctx.amazon.allowedTags.includes(t))).toBe(true);
      expect(new URL(href).searchParams.getAll('tag')).toHaveLength(1);
    }
    expect(attrs).toContain(`rel="${AFFILIATE_REL}"`);
    expect(attrs).toContain('target="_blank"');
    expect(attrs).toMatch(/data-cta-placement="[a-z0-9][a-z0-9-]{0,39}"/);
  }
  for (const block of jsonLd(html)) {
    expect(block['@context']).toBe('https://schema.org');
    expect(block['@type']).toBeDefined();
  }
  expect(JSON.stringify(jsonLd(html))).not.toMatch(/aggregateRating|AggregateRating/);
}

describe('a guide page', () => {
  const md = `${fs.readFileSync(path.join(__dirname, 'fixtures/guide.md'), 'utf8')}\n\nOr [buy it through our link](/r/B0ABCDEFGH).\n`;
  const body = mdToHtml(ctx, md, { amazonPlacement: 'guide-body' });
  const faq = [{ question: 'How long will a 2 kWh station run a fridge?', answer: 'About 11 to 20 hours.' }];
  const html = renderPage(ctx, {
    head: {
      title: pageTitle(ctx, 'Best Portable Power Stations for a Winter Outage (2026)'),
      description: 'What to buy for a three-day outage, by usable watt-hours per dollar.',
      canonical: '/guides/winter-outage',
      indexing: INDEXABLE_PAGE,
      ogType: 'article',
      ogImage: 'https://m.media-amazon.com/images/I/71abc.jpg',
      jsonLd: [
        articleLd(ctx, { headline: 'Best Portable Power Stations for a Winter Outage', url: '/guides/winter-outage', datePublished: '2026-11-10T08:00:00Z' }),
        faqPageLd(faq),
      ],
    },
    main: `<article><h1>Best Portable Power Stations for a Winter Outage</h1>${body}${faqSectionHtml(faq)}</article>`,
    breadcrumbs: [{ name: 'Guides', href: '/guides' }, { name: 'Winter outage' }],
    nav: [{ label: 'Guides', href: '/guides', current: true }],
    footerLinks: [{ label: 'Disclosure', href: '/disclosure' }, { label: 'How we calculate', href: '/how-we-calculate' }],
    now: NOW,
  });

  it('keeps every licence and structure rule', () => {
    expectCompliantPage(html);
  });

  it('renders the body\'s tables as md-table, re-tags its links and carries the top disclosure', () => {
    expect(html.match(/<table class="md-table">/g)).toHaveLength(2);
    expect(html).toContain('data-cta-placement="guide-body"');
    expect(html).toContain(`<a href="https://example.com/r/B0ABCDEFGH" rel="${AFFILIATE_REL}" target="_blank" data-cta-placement="guide-body">`);
    expect(html).toContain('kit-earnings-disclosure--top');
    expect(jsonLd(html).map((b) => b['@type'])).toEqual(['Article', 'FAQPage', 'BreadcrumbList']);
  });
});

describe('a guide whose body holds a truncated agent note', () => {
  // The review's case: the note's "<!--" never closes. Unsealed, everything after it
  // (the buy box, the footer's 2(k) notice and earnings disclosure) became a comment.
  const body = mdToHtml(ctx, '## Quick Answer\n\nAbout 20 hours.\n\n<!-- sources: jackery manual p.12, \n\n## Details\n\nMore.');
  const html = renderPage(ctx, {
    head: { title: pageTitle(ctx, 'Winter Outage Guide'), canonical: '/guides/winter-outage', indexing: INDEXABLE_PAGE },
    main: `<h1>Guide</h1>${body}<div class="buystrip">${cta(ctx, { asin: 'B0ABCDEFGH', placement: 'guide-buystrip' })}</div>`,
    now: NOW,
  });

  it('keeps every licence and structure rule, with the note shown as text', () => {
    expectCompliantPage(html);
    expect(html).not.toContain('<!--');
    const visible = browserVisibleText(html);
    expect(visible).toContain('<!-- sources: jackery manual p.12,');
    expect(visible).toContain(ctx.amazon.ctaLabel);
    expect(html).toContain('class="ssr-amz-cta"');
  });
});

describe('a product page', () => {
  const asin = 'B0ABCDEFGH';
  const price = { price: 899, currency: 'USD', fetchedAt: FRESH };
  const alts = [{ asin: 'B0ZZZZZZZZ', name: 'Beta 2000', price: 849, fetchedAt: FRESH }, { asin: 'B0YYYYYYYY', name: 'Gamma 1800', price: 799, fetchedAt: STALE }];

  function productPage(withPrice: boolean): string {
    const stamp = withPrice ? priceStamp(ctx, price, { now: NOW, key: `${asin}|pdp-hero` }) : '';
    const main = [
      `<h1>${amazonTextHtml('Alpha 2000 Portable Power Station, 2048Wh LiFePO4 Solar Generator', { tag: 'span' })}</h1>`,
      `<div class="pdp-hero">${stamp ? `<p>${stamp}</p>` : ''}${cta(ctx, { asin, placement: 'pdp-hero' })}</div>`,
      `<section><h2>What to buy instead</h2><ul>${alts.map((a) => `<li>${a.name} ${cta(ctx, { asin: a.asin, placement: 'pdp-alt' })}</li>`).join('')}</ul>`,
      jsonLdScripts([
        isSimilarToLd(ctx, asin, alts),
        itemListLd(ctx, { name: 'In-stock alternatives', items: alts.map((a) => ({ name: a.name, url: `/product/${a.asin}`, asin: a.asin, offer: { price: a.price, currency: 'USD', fetchedAt: a.fetchedAt } })) }, NOW),
      ]),
      '</section>',
    ].join('\n');
    return renderPage(ctx, {
      head: {
        title: pageTitle(ctx, 'Alpha 2000 Review: Runtime, Solar and Price'),
        description: 'How long the Alpha 2000 runs a fridge, how fast it recharges from solar, and what it costs.',
        canonical: `/product/${asin}`,
        indexing: INDEXABLE_PAGE,
        ogType: 'product',
        ogImage: 'https://images-na.ssl-images-amazon.com/images/I/41x.jpg',
        defaultOgImage: { url: '/og-card.png', width: 1200, height: 630 },
        jsonLd: [productLd(ctx, {
          asin, name: 'Alpha 2000', brand: 'AlphaPower', images: ['https://m.media-amazon.com/images/I/71abc.jpg', '/images/alpha.webp'],
          offer: withPrice ? price : { ...price, fetchedAt: STALE },
          isSimilarTo: alts,
        }, NOW)],
      },
      main,
      amazonContent: false, // the Amazon title marker must force the notice back on
      breadcrumbs: [{ name: 'Power stations', href: '/category/power-stations' }, { name: 'Alpha 2000' }],
      now: NOW,
    });
  }

  it('keeps every licence and structure rule, priced or not', () => {
    expectCompliantPage(productPage(true));
    expectCompliantPage(productPage(false));
  });

  it('shows a price only with its stamp and the exact 2(i) text, and an Offer only then', () => {
    const priced = productPage(true);
    expect(containsNotice(text(priced), '$899.00 on Amazon.com as of 2026-11-17 10:11 UTC (price may vary)')).toBe(true);
    expect(containsNotice(text(priced), priceDisclaimerFor(ctx))).toBe(true);
    const product = jsonLd(priced).find((b) => b['@type'] === 'Product' && b.name) as Record<string, unknown>;
    expect(product.offers).toMatchObject({ price: '899.00', validFrom: FRESH });
    expect(product.image).toEqual(['https://example.com/images/alpha.webp']);

    const unpriced = productPage(false);
    expect(unpriced).not.toContain('data-price-stamp');
    expect(text(unpriced)).not.toMatch(/\$899/);
    const bare = jsonLd(unpriced).find((b) => b['@type'] === 'Product' && b.name) as Record<string, unknown>;
    expect(bare.offers).toBeUndefined();
  });

  it('lists the stale alternative without an Offer and points isSimilarTo at both', () => {
    const blocks = jsonLd(productPage(true));
    const list = blocks.find((b) => b['@type'] === 'ItemList') as { itemListElement: Array<Record<string, unknown>> };
    expect(list.itemListElement[0]).toHaveProperty('item.offers.validFrom', FRESH);
    expect(list.itemListElement[1]).toEqual({ '@type': 'ListItem', position: 2, name: 'Gamma 1800', url: 'https://example.com/product/B0YYYYYYYY' });
    const similar = blocks.find((b) => b['@type'] === 'Product' && b.isSimilarTo && !b.name) as { isSimilarTo: unknown[] };
    expect(similar.isSimilarTo).toHaveLength(2);
  });

  it('uses the site card for og:image, never the Amazon one', () => {
    expect(productPage(true)).toContain('<meta property="og:image" content="https://example.com/og-card.png">');
  });
});
