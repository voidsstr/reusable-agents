/**
 * The page shell: the 2(k) notice in plain view whenever the page shows
 * Amazon content (default on, forced on by kit price stamps and
 * data-amazon-content markers), the earnings disclosure in the footer and
 * above any affiliate link, the theme tokens and base CSS, one <main>, the
 * breadcrumb with its BreadcrumbList twin, and an og:image that is never
 * Amazon-hosted. Also the paired blocks (breadcrumb, FAQ, Amazon text).
 */
import * as path from 'path';
import { cta } from '../../server/src/links';
import { AMAZON_CONTENT_NOTICE, amazonContentNoticeHtml, containsNotice } from '../../server/src/notices';
import { PRICE_STAMP_CSS, priceStamp } from '../../server/src/price';
import { loadSiteContext, themeCss } from '../../server/src/siteContext';
import { BASE_CSS_MIN } from '../../server/src/render/baseCss';
import { amazonTextHtml, breadcrumbNavHtml, faqSectionHtml } from '../../server/src/render/blocks';
import { breadcrumbListLd, faqPageLd } from '../../server/src/render/jsonld';
import {
  htmlHasAffiliateLinks,
  htmlShowsAmazonContent,
  needsAmazonNotice,
  renderPage,
  siteFooterHtml,
  siteHeaderHtml,
  type PageOptions,
} from '../../server/src/render/layout';
import { INDEXABLE_PAGE, noindexPage, type HeadFields } from '../../server/src/render/seo';
import { browserVisibleText } from './htmlCheck';

const KIT = path.resolve(__dirname, '../..');
const ctx = loadSiteContext(path.join(KIT, 'fixtures/site.example.json'), { env: {} });
const NOW = Date.parse('2026-11-17T12:00:00Z');

const head: HeadFields = {
  title: 'Alpha 2000 | ExamplePicks',
  description: 'Runtime, solar and price for the Alpha 2000.',
  canonical: '/product/B0ABCDEFGH',
  indexing: INDEXABLE_PAGE,
  ogType: 'product',
  ogImage: 'https://m.media-amazon.com/images/I/71abcdEFGHL.jpg',
  defaultOgImage: { url: '/og-card.png', width: 1200, height: 630 },
};

function page(extra: Partial<PageOptions> = {}): string {
  return renderPage(ctx, { head, main: '<h1>Alpha 2000</h1><p>Body.</p>', now: NOW, ...extra });
}

/** Visible text of a document: head, scripts and styles dropped, tags removed, entities decoded. */
function bodyText(html: string): string {
  const body = html.slice(html.indexOf('<body'));
  return body.replace(/<(script|style)\b[\s\S]*?<\/\1>/g, ' ').replace(/<[^>]+>/g, ' ')
    .replace(/&#39;/g, "'").replace(/&quot;/g, '"').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&copy;/g, '©').replace(/&amp;/g, '&');
}

function count(html: string, needle: string | RegExp): number {
  return typeof needle === 'string' ? html.split(needle).length - 1 : (html.match(new RegExp(needle, 'g')) ?? []).length;
}

describe('renderPage: document structure', () => {
  const html = page({ nav: [{ label: 'Guides', href: '/guides' }, { label: 'Kits', href: '/kits', current: true }] });

  it('is one complete document with one main, a skip link and the site language', () => {
    expect(html.startsWith('<!doctype html>\n<html lang="en-US">\n<head>\n<meta charset="utf-8">')).toBe(true);
    expect(html.trimEnd().endsWith('</body>\n</html>')).toBe(true);
    expect(count(html, '<main ')).toBe(1);
    expect(html).toContain('<main id="main" class="ssr-main kit-main kit-wrap">');
    expect(html).toContain('<a class="kit-skip-link" href="#main">Skip to content</a>');
    expect(html).toContain('<meta name="viewport" content="width=device-width, initial-scale=1">');
    expect(count(html, '<h1')).toBe(1);
  });

  it('inlines the theme tokens, the base CSS and the price stamp CSS in style#ssr-styles', () => {
    const style = /<style id="ssr-styles">([\s\S]*?)<\/style>/.exec(html)?.[1] ?? '';
    expect(style).toContain(themeCss(ctx.theme));
    expect(style).toContain('--kit-bg:#ffffff');
    expect(style).toContain('@media (prefers-color-scheme: dark)');
    expect(style).toContain(BASE_CSS_MIN);
    expect(style).toContain(PRICE_STAMP_CSS);
    expect(count(html, '<style')).toBe(1);
    expect(html).toContain('<meta name="theme-color" content="#0b1120" media="(prefers-color-scheme: dark)">');
  });

  it('renders the header navigation with absolute links and the current page marked', () => {
    expect(html).toContain('<a class="kit-brand" href="https://example.com/">ExamplePicks</a>');
    expect(html).toContain('<nav class="kit-nav" aria-label="Main navigation"><ul><li><a href="https://example.com/guides">Guides</a></li>'
      + '<li><a href="https://example.com/kits" aria-current="page">Kits</a></li></ul></nav>');
  });

  it('puts the SEO head in, with the default card instead of the Amazon og:image', () => {
    expect(html).toContain('<title>Alpha 2000 | ExamplePicks</title>');
    expect(html).toContain('<link rel="canonical" href="https://example.com/product/B0ABCDEFGH">');
    expect(html).toContain('<meta property="og:image" content="https://example.com/og-card.png">');
    expect(html).not.toMatch(/media-amazon\.com|ssl-images-amazon\.com/);
  });

  it('dates the copyright from the clock it is given', () => {
    expect(html).toContain('<p class="kit-copyright">&copy; 2026 ExamplePicks</p>');
  });
});

describe('the IP License 2(k) notice', () => {
  it('is in plain view by default, with the exact text, once', () => {
    const html = page();
    expect(containsNotice(bodyText(html), AMAZON_CONTENT_NOTICE)).toBe(true);
    expect(count(html, 'data-amazon-notice="2k"')).toBe(1);
    expect(html).toMatch(/<footer class="kit-footer"><div class="kit-wrap"><p class="kit-amazon-notice" data-amazon-notice="2k" data-nosnippet>/);
  });

  it('can be left off a page with no Amazon content', () => {
    const html = page({ amazonContent: false, main: '<h1>About us</h1><p>Who we are.</p>' });
    expect(html).not.toContain('data-amazon-notice');
    expect(containsNotice(bodyText(html), AMAZON_CONTENT_NOTICE)).toBe(false);
  });

  it('is forced back on when the content shows an Amazon price, availability or title', () => {
    const stamp = priceStamp(ctx, { price: 899, currency: 'USD', fetchedAt: '2026-11-17T10:00:00Z' }, { now: NOW });
    expect(stamp).not.toBe('');
    for (const main of [`<h1>X</h1><p>${stamp}</p>`, `<h1>${amazonTextHtml('Alpha 2000 Portable Power Station, 2048Wh LiFePO4', { tag: 'span' })}</h1>`]) {
      const html = page({ amazonContent: false, main });
      expect(containsNotice(bodyText(html), AMAZON_CONTENT_NOTICE)).toBe(true);
    }
  });

  it('is never switched off by the content: a notice-like block there does not replace it', () => {
    for (const fake of [
      '<p class="kit-amazon-notice" data-amazon-notice="2k" data-nosnippet>CERTAIN CONTENT ...</p>',
      '<details><summary>More</summary><p data-amazon-notice="2k">Some content is from Amazon.</p></details>',
      '<span data-amazon-notice="2k" hidden></span>',
      '<p>Set data-amazon-notice=2k on the element.</p>',
      amazonContentNoticeHtml(),
    ]) {
      const html = page({ main: `<h1>${amazonTextHtml('Alpha 2000')}</h1>${fake}` });
      const footer = html.slice(html.indexOf('<footer'));
      expect(footer).toContain(amazonContentNoticeHtml());
      expect(containsNotice(browserVisibleText(footer), AMAZON_CONTENT_NOTICE)).toBe(true);
    }
  });

  it('is forced on by Amazon content in bodyEnd or headExtra, not only in main', () => {
    const bar = `<div class="sticky">${amazonTextHtml('Alpha 2000 Portable Power Station')}</div>`;
    for (const extra of [{ bodyEnd: bar }, { headExtra: '<meta name="x" content="y" data-amazon-content>' }]) {
      const html = page({ amazonContent: false, main: '<h1>About us</h1>', ...extra });
      expect(containsNotice(browserVisibleText(html), AMAZON_CONTENT_NOTICE)).toBe(true);
    }
  });

  it('needsAmazonNotice and htmlShowsAmazonContent', () => {
    expect(needsAmazonNotice({ main: '' })).toBe(true);
    expect(needsAmazonNotice({ amazonContent: false, main: '<p>plain</p>' })).toBe(false);
    expect(needsAmazonNotice({ amazonContent: false, main: '<p>plain</p>', bodyEnd: '<b data-amazon-content>T</b>' })).toBe(true);
    expect(needsAmazonNotice({ amazonContent: false, main: '<p>plain</p>', headExtra: '<i data-price-stamp="price"></i>' })).toBe(true);
    expect(htmlShowsAmazonContent('<span data-price-stamp="availability">x</span>')).toBe(true);
    expect(htmlShowsAmazonContent('<h1 data-amazon-content>T</h1>')).toBe(true);
    expect(htmlShowsAmazonContent('<p>Amazon is a store.</p>')).toBe(false);
  });
});

describe('malformed content cannot hide the footer notices', () => {
  const button = cta(ctx, { asin: 'B0ABCDEFGH', placement: 'pdp-hero' });

  /** The exact 2(k) text and the earnings disclosure are still on screen. */
  function expectNoticesVisible(html: string): void {
    const visible = browserVisibleText(html);
    expect(containsNotice(visible, AMAZON_CONTENT_NOTICE)).toBe(true);
    expect(visible).toContain(ctx.disclosure.earnings);
  }

  it.each<[string, string]>([
    ['a truncated comment', '<p>About 20 hours.</p><!-- sources: manual p.12,'],
    ['an unclosed script', '<script>track('],
    ['an unclosed style', '<style>.x{'],
    ['an unclosed textarea', '<textarea>notes'],
    ['an unclosed title', '<title>x'],
    ['plaintext', '<plaintext>x'],
  ])('%s is shown as text, and nothing after it is lost', (_why, bad) => {
    const html = page({ main: `<h1>Guide</h1>${bad}<div class="buystrip">${button}</div>` });
    expectNoticesVisible(html);
    expect(browserVisibleText(html)).toContain(ctx.amazon.ctaLabel);
  });

  it('an unclosed template keeps the rest of main inert, never the footer', () => {
    const html = page({ main: `<h1>Guide</h1><template><p>draft</p><div>${button}</div>` });
    expect(html).toContain(`<template><p>draft</p><div>${button}</div></template>\n</main>`);
    expectNoticesVisible(html);
  });

  it('closes formatting tags and escapes document tags the content leaves behind', () => {
    const html = page({ main: '<h1>T</h1><p><b hidden>note</p><select><option>a<body hidden>' });
    expect(html).toContain('<p><b hidden>note</p><select><option>a&lt;body hidden></select></b>\n</main>');
    expectNoticesVisible(html);
  });

  it('seals headExtra as well', () => {
    const html = page({ headExtra: '<!-- verification' });
    expect(html).toContain('&lt;!-- verification');
    expectNoticesVisible(html);
  });
});

describe('the earnings disclosure', () => {
  const button = cta(ctx, { asin: 'B0ABCDEFGH', placement: 'pdp-hero' });

  it('is in every footer, linked to the disclosure page', () => {
    const html = page({ amazonContent: false, main: '<h1>About</h1>' });
    expect(html).toContain('<p class="kit-earnings-disclosure" data-earnings-disclosure><a href="https://example.com/disclosure">'
      + 'As an Amazon Associate, ExamplePicks earns from qualifying purchases.</a></p>');
    expect(count(html, 'data-earnings-disclosure')).toBe(1);
  });

  it('also sits after the h1 and before the first affiliate link when the content has one', () => {
    const html = page({ main: `<h1>Alpha 2000</h1><p>Intro.</p><div>${button}</div>` });
    expect(count(html, 'data-earnings-disclosure')).toBe(2);
    const body = html.slice(html.indexOf('<body'));
    const top = body.indexOf('kit-earnings-disclosure--top"');
    expect(top).toBeGreaterThan(body.indexOf('</h1>'));
    expect(top).toBeLessThan(body.indexOf('class="ssr-amz-cta'));
  });

  it('goes first when an affiliate link comes before any h1, and follows the explicit setting', () => {
    const early = page({ main: `<div>${button}</div><h1>Late title</h1>` });
    const body = early.slice(early.indexOf('<body'));
    expect(body.indexOf('kit-earnings-disclosure--top"')).toBeLessThan(body.indexOf('class="ssr-amz-cta'));
    expect(count(page({ main: '<h1>T</h1>', disclosureTop: true }), 'data-earnings-disclosure')).toBe(2);
    expect(count(page({ main: `<h1>T</h1>${button}`, disclosureTop: false }), 'data-earnings-disclosure')).toBe(1);
  });

  it('also goes on top when only bodyEnd (a sticky bar) holds an affiliate link', () => {
    const bar = `<div class="sticky">${cta(ctx, { asin: 'B0ABCDEFGH', placement: 'sticky-mobile' })}</div>`;
    const html = page({ main: '<h1>Alpha 2000</h1><p>Intro.</p>', bodyEnd: bar });
    expect(count(html, 'data-earnings-disclosure')).toBe(2);
    expect(html.indexOf('kit-earnings-disclosure--top"')).toBeGreaterThan(html.indexOf('</h1>'));
  });

  it('htmlHasAffiliateLinks sees sponsored rels, Amazon hrefs and /r/ redirects only', () => {
    expect(htmlHasAffiliateLinks(button)).toBe(true);
    expect(htmlHasAffiliateLinks('<a href="https://www.amazon.com/dp/B0ABCDEFGH?tag=x-20">x</a>')).toBe(true);
    expect(htmlHasAffiliateLinks('<a href=\'https://amzn.to/abc\'>x</a>')).toBe(true);
    expect(htmlHasAffiliateLinks('<a href="/r/B0ABCDEFGH">x</a>')).toBe(true);
    expect(htmlHasAffiliateLinks('<a href="/guides/amazon-basics">x</a> <a href="https://docs.example.org/amazon-tips">y</a>')).toBe(false);
    // Any amazon.<suffix> host counts (deliberately broad: it can only add a disclosure).
    expect(htmlHasAffiliateLinks('<a href="https://amazon.co.uk/x">y</a>')).toBe(true);
  });
});

describe('breadcrumbs, footer and options', () => {
  it('renders the breadcrumb and adds its BreadcrumbList, unless the head carries one', () => {
    const trail = [{ name: 'Power stations', href: '/category/power-stations' }, { name: 'Alpha 2000' }];
    const html = page({ breadcrumbs: trail });
    expect(html).toContain('<main id="main" class="ssr-main kit-main kit-wrap">\n<nav class="kit-breadcrumb" aria-label="Breadcrumb">');
    expect(count(html, '"@type":"BreadcrumbList"')).toBe(1);
    const again = page({ breadcrumbs: trail, head: { ...head, jsonLd: [breadcrumbListLd(ctx, trail)] } });
    expect(count(again, '"@type":"BreadcrumbList"')).toBe(1);
    expect(page()).not.toContain('<nav class="kit-breadcrumb"');
  });

  it('renders footer columns and trust links; external https links only in the footer', () => {
    const html = page({
      footerColumns: [{ heading: 'Explore', links: [{ label: 'Kits', href: '/kits' }] }, { heading: 'Empty', links: [] }],
      footerLinks: [{ label: 'Disclosure', href: '/disclosure' }, { label: 'Us elsewhere', href: 'https://social.example.org/x' }],
    });
    expect(html).toContain('<nav class="kit-footer-cols" aria-label="Site sections"><div class="kit-footer-col"><p class="kit-footer-heading">Explore</p>'
      + '<ul><li><a href="https://example.com/kits">Kits</a></li></ul></div></nav>');
    expect(html).not.toContain('>Empty<');
    expect(html).toContain('<a href="https://social.example.org/x" rel="noopener">Us elsewhere</a>');
    expect(() => page({ nav: [{ label: 'x', href: 'https://social.example.org/x' }] })).toThrow(TypeError);
  });

  it('never renders an Amazon URL as a nav or footer link (Amazon links come from the CTA helpers)', () => {
    expect(() => page({ footerLinks: [{ label: 'Our storefront', href: 'https://www.amazon.com/shop/examplepicks?tag=other-20' }] }))
      .toThrow(/CTA helpers/);
    expect(() => siteFooterHtml(ctx, { amazonNotice: false, columns: [{ heading: 'Shop', links: [{ label: 's', href: 'https://amzn.to/x' }] }] }))
      .toThrow(TypeError);
    expect(() => siteHeaderHtml(ctx, [{ label: 'Deals', href: 'https://www.amazon.com/deals' }])).toThrow(/CTA helpers/);
  });

  it('keeps site CSS inside its style element, and appends head and body extras', () => {
    const html = page({ css: '.x{color:red}</style><script>alert(1)</script>', headExtra: '<link rel="preload" href="/f.woff2" as="font">', bodyEnd: '<script src="/kit/client.js" defer></script>', bodyClass: 'pdp bad"class', mainClass: 'pdp-main' });
    expect(count(html, '</style>')).toBe(1);
    // Only "</style" can end a style element; it is neutralised, the rest stays text.
    expect(html).toContain('.x{color:red}<\\/style><script>alert(1)</script></style>');
    expect(html).toContain('<link rel="preload" href="/f.woff2" as="font">\n</head>');
    expect(html).toContain('<script src="/kit/client.js" defer></script>\n</body>');
    expect(html).toContain('<body class="kit-page pdp">');
    expect(html).toContain('class="ssr-main kit-main kit-wrap pdp-main"');
  });

  it('a noindex page says so in its head', () => {
    expect(page({ head: { ...head, indexing: noindexPage('stale cache') } })).toContain('<meta name="robots" content="noindex, follow">');
  });

  it('siteHeaderHtml and siteFooterHtml work on their own', () => {
    expect(siteHeaderHtml(ctx)).toBe('<header class="kit-header"><div class="kit-wrap kit-header-inner"><a class="kit-brand" href="https://example.com/">ExamplePicks</a></div></header>');
    const footer = siteFooterHtml(ctx, { amazonNotice: false, now: NOW });
    expect(footer).not.toContain('data-amazon-notice');
    expect(footer).toContain('data-earnings-disclosure');
  });
});

describe('paired blocks', () => {
  it('breadcrumbNavHtml marks the current page and links the rest', () => {
    expect(breadcrumbNavHtml(ctx, [{ name: 'Guides', href: '/guides' }, { name: 'Winter <outage>' }])).toBe(
      '<nav class="kit-breadcrumb" aria-label="Breadcrumb"><ol><li><a href="https://example.com/">Home</a></li>'
      + '<li><a href="https://example.com/guides">Guides</a></li><li><span class="kit-bc-current" aria-current="page">Winter &lt;outage&gt;</span></li></ol></nav>');
    expect(breadcrumbNavHtml(ctx, [])).toBe('');
  });

  it('breadcrumbNavHtml leaves out an unlinked middle crumb, exactly as BreadcrumbList does', () => {
    const items = [{ name: 'Guides' }, { name: 'Winter', href: '/guides/winter' }, { name: 'Page' }];
    const html = breadcrumbNavHtml(ctx, items);
    expect(html).not.toContain('Guides');
    const names = (breadcrumbListLd(ctx, items) as { itemListElement: Array<{ name: string }> }).itemListElement.map((c) => c.name);
    expect(names).toEqual(['Home', 'Winter', 'Page']);
    expect(Array.from(html.matchAll(/<li>(?:<a [^>]*>|<span[^>]*>)([^<]*)/g)).map((m) => m[1])).toEqual(names);
  });

  it('faqSectionHtml shows exactly the rows faqPageLd publishes', () => {
    const rows = [{ question: 'Does it run a CPAP?', answer: 'We do not size medical loads.' }, { question: 'Blank', answer: '' }];
    const html = faqSectionHtml(rows);
    expect(html).toBe('<section class="kit-faq" id="faq" aria-labelledby="faq-h"><h2 id="faq-h">Frequently asked questions</h2>'
      + '<details class="kit-faq-item"><summary>Does it run a CPAP?</summary><p>We do not size medical loads.</p></details></section>');
    const ld = faqPageLd(rows) as { mainEntity: Array<{ name: string; acceptedAnswer: { text: string } }> };
    expect(ld.mainEntity.map((q) => [q.name, q.acceptedAnswer.text])).toEqual([['Does it run a CPAP?', 'We do not size medical loads.']]);
    expect(faqSectionHtml(rows, { heading: null, id: '9bad' })).toMatch(/^<section class="kit-faq" id="faq" aria-label="Frequently asked questions"><details/);
    expect(faqSectionHtml([])).toBe('');
  });

  it('amazonTextHtml escapes the text and marks it', () => {
    expect(amazonTextHtml('Alpha <2000> "Max"', { tag: 'h1', className: 'pdp-title bad"x' })).toBe('<h1 class="pdp-title" data-amazon-content>Alpha &lt;2000&gt; &quot;Max&quot;</h1>');
    expect(amazonTextHtml(null)).toBe('<span data-amazon-content></span>');
  });
});
