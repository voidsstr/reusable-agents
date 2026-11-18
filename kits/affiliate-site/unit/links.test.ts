/**
 * Affiliate links and CTAs: rel, target, placement attribute, and a tag that
 * is always one of the site's own tracking IDs; source classification and
 * tracking-ID choice from site.json (SpecPicks' pickTrackingId /
 * classifySource, parameterised).
 */
import * as path from 'path';
import {
  AFFILIATE_REL,
  CTA_CLASS,
  PLACEMENT_ATTR,
  allowedTags,
  amazonSearchCta,
  amazonUrl,
  assertPlacement,
  classifySource,
  cta,
  detailLink,
  esc,
  isAllowedTag,
  isAmazonAsin,
  ownTrackingId,
  pickTrackingId,
  productPath,
  productUrl,
  resolveSegmentTag,
  retagAmazonUrl,
  tagForPlacement,
} from '../server/src/links';
import { loadSiteContext, parseSiteContext, type SiteContext } from '../server/src/siteContext';
import * as fs from 'fs';

const EXAMPLE = path.resolve(__dirname, '../fixtures/site.example.json');
const ctx = loadSiteContext(EXAMPLE, { env: {} });
const ASIN = 'B0DS2X13PH';

function variant(edit: (j: any) => void): SiteContext {
  const j = JSON.parse(fs.readFileSync(EXAMPLE, 'utf8'));
  edit(j);
  return parseSiteContext(j, { env: {} });
}

/** Every href in a fragment, entity-decoded. */
function hrefs(html: string): string[] {
  return Array.from(html.matchAll(/href="([^"]*)"/g), (m) => (m[1] as string).replace(/&amp;/g, '&'));
}

describe('esc', () => {
  it('escapes markup and attribute quotes', () => {
    expect(esc('<a href="x">\'</a>')).toBe('&lt;a href=&quot;x&quot;&gt;&#39;&lt;/a&gt;');
    expect(esc(null)).toBe('');
    expect(esc(undefined)).toBe('');
    expect(esc(42)).toBe('42');
  });
  it('does not double-escape entities that leaked into stored text', () => {
    expect(esc('Strix Halo &amp; Radeon')).toBe('Strix Halo &amp; Radeon');
    expect(esc('Strix Halo & Radeon')).toBe('Strix Halo &amp; Radeon');
    expect(esc('&lt;b&gt; &#39;q&#39;')).toBe('&lt;b&gt; &#39;q&#39;');
    expect(esc('AT&T &copy;')).toBe('AT&amp;T &amp;copy;');
  });
});

describe('isAmazonAsin (allowlist)', () => {
  it.each([
    [ASIN, true],
    ['B000J01I1G', true],
    ['b0ds2x13ph', false],
    ['EBAY_406824820872', false],
    ['PCB_0123456789ab', false],
    ['B0DS2X13P', false],
    ['', false],
    [null, false],
  ])('%s -> %s', (asin, want) => {
    expect(isAmazonAsin(asin as string | null)).toBe(want);
  });
});

describe('cta', () => {
  it('renders the compliance triple, the placement, and the placement\'s segment tag', () => {
    const html = cta(ctx, { asin: ASIN, placement: 'pdp-hero' });
    expect(html).toBe(
      `<a class="${CTA_CLASS}" target="_blank" rel="sponsored nofollow noopener" ${PLACEMENT_ATTR}="pdp-hero" `
      + `href="https://www.amazon.com/dp/${ASIN}?tag=kitexample-pdp-20&amp;linkCode=ll1">View current price on Amazon</a>`,
    );
    expect(AFFILIATE_REL).toBe('sponsored nofollow noopener');
    expect(html).not.toMatch(/noreferrer/);
  });

  it('uses the partner tag for a placement with no segment tag', () => {
    expect(hrefs(cta(ctx, { asin: ASIN, placement: 'pdp-alt' }))).toEqual([`https://www.amazon.com/dp/${ASIN}?tag=kitexample-20&linkCode=ll1`]);
  });

  it('honours an allowlisted segment tag, never a foreign one', () => {
    expect(hrefs(cta(ctx, { asin: ASIN, placement: 'pdp-alt', segmentTag: 'kitexample-guides-20' }))[0]).toContain('tag=kitexample-guides-20&');
    expect(hrefs(cta(ctx, { asin: ASIN, placement: 'pdp-alt', segmentTag: 'someone-else-20' }))[0]).toContain('tag=kitexample-20&');
    // A configured placement tag outranks the visit's segment.
    expect(hrefs(cta(ctx, { asin: ASIN, placement: 'pdp-hero', segmentTag: 'kitexample-guides-20' }))[0]).toContain('tag=kitexample-pdp-20&');
  });

  it('ignores ?tag= entirely unless the site opts in', () => {
    const closed = variant((j) => { j.amazon.acceptSegmentTagParam = false; });
    expect(resolveSegmentTag(closed, 'kitexample-guides-20')).toBe('kitexample-20');
    expect(resolveSegmentTag(ctx, 'kitexample-guides-20')).toBe('kitexample-guides-20');
    expect(resolveSegmentTag(ctx, '  ')).toBe('kitexample-20');
    expect(resolveSegmentTag(ctx, null)).toBe('kitexample-20');
  });

  it('escapes the label and adds only safe extra classes', () => {
    const html = cta(ctx, { asin: ASIN, placement: 'sticky-mobile', label: 'Buy <now> & "save"', className: 'big wide" onclick="x' });
    expect(html).toContain('>Buy &lt;now&gt; &amp; &quot;save&quot;</a>');
    expect(html).toContain(`class="${CTA_CLASS} big"`);
    expect(html).not.toContain('onclick');
  });

  it('renders nothing for a pseudo-ASIN rather than a dead /dp/ link', () => {
    expect(cta(ctx, { asin: 'EBAY_406824820872', placement: 'pdp-hero' })).toBe('');
    expect(cta(ctx, { asin: 'b0ds2x13ph', placement: 'pdp-hero' })).toBe('');
  });

  it('refuses a malformed placement (it would split click attribution)', () => {
    expect(() => cta(ctx, { asin: ASIN, placement: 'PDP Hero' })).toThrow(TypeError);
    expect(() => cta(ctx, { asin: ASIN, placement: '' })).toThrow(/invalid CTA placement/);
    expect(() => assertPlacement('x'.repeat(41))).toThrow(TypeError);
    expect(assertPlacement('x'.repeat(40))).toBe('x'.repeat(40));
  });

  it('a placement named like an Object.prototype member gets the partner tag, not a function', () => {
    expect(tagForPlacement(ctx, 'constructor')).toBe('kitexample-20');
    expect(tagForPlacement(ctx, 'tostring')).toBe('kitexample-20');
    expect(hrefs(cta(ctx, { asin: ASIN, placement: 'constructor' }))[0]).toContain('tag=kitexample-20&');
  });

  it('every tag any CTA can carry is one of the site\'s own', () => {
    const placements = ['pdp-hero', 'sticky-mobile', 'pdp-alt', 'ladder-slot', 'constructor'];
    const segments = [null, 'kitexample-guides-20', 'someone-else-20', 'kitexample-pdp-20'];
    for (const placement of placements) {
      for (const segmentTag of segments) {
        const tag = new URL(hrefs(cta(ctx, { asin: ASIN, placement, segmentTag }))[0] as string).searchParams.get('tag');
        expect(isAllowedTag(ctx, tag)).toBe(true);
      }
    }
    expect(allowedTags(ctx)).toEqual(ctx.amazon.allowedTags);
    expect(isAllowedTag(ctx, 'someone-else-20')).toBe(false);
    expect(isAllowedTag(ctx, '')).toBe(false);
  });
});

describe('amazonUrl', () => {
  it('builds a tagged /dp/ link on the site marketplace', () => {
    expect(amazonUrl(ctx, ASIN)).toBe(`https://www.amazon.com/dp/${ASIN}?tag=kitexample-20&linkCode=ll1`);
    expect(amazonUrl(ctx, ASIN, 'kitexample-pdp-20')).toBe(`https://www.amazon.com/dp/${ASIN}?tag=kitexample-pdp-20&linkCode=ll1`);
  });

  it('never carries someone else\'s tracking ID, whatever the caller passes', () => {
    expect(amazonUrl(ctx, ASIN, 'evil-20')).toBe(`https://www.amazon.com/dp/${ASIN}?tag=kitexample-20&linkCode=ll1`);
    expect(amazonUrl(ctx, ASIN, '')).toBe(`https://www.amazon.com/dp/${ASIN}?tag=kitexample-20&linkCode=ll1`);
    expect(amazonUrl(ctx, ASIN, 'kitexample-20&tag=evil-20')).toBe(`https://www.amazon.com/dp/${ASIN}?tag=kitexample-20&linkCode=ll1`);
  });
});

describe('ownTrackingId', () => {
  it('passes the site\'s own tags through and replaces anything else with the partner tag', () => {
    for (const t of ctx.amazon.allowedTags) expect(ownTrackingId(ctx, t)).toBe(t);
    expect(ownTrackingId(ctx, 'someone-else-20')).toBe('kitexample-20');
    expect(ownTrackingId(ctx, 'KITEXAMPLE-PDP-20')).toBe('kitexample-20');
    expect(ownTrackingId(ctx, null)).toBe('kitexample-20');
    expect(ownTrackingId(ctx, undefined)).toBe('kitexample-20');
    expect(ownTrackingId(ctx, 'constructor')).toBe('kitexample-20');
  });
});

describe('retagAmazonUrl', () => {
  it('sets (not defaults) our tag on an Amazon URL', () => {
    expect(retagAmazonUrl(ctx, 'https://www.amazon.com/s?k=jackery+1000&tag=someone-else-20', 'kitexample-20'))
      .toBe('https://www.amazon.com/s?k=jackery+1000&tag=kitexample-20');
    expect(retagAmazonUrl(ctx, `https://amazon.com/dp/${ASIN}`)).toBe(`https://amazon.com/dp/${ASIN}?tag=kitexample-20`);
    expect(retagAmazonUrl(ctx, `http://www.amazon.com/dp/${ASIN}`)).toBe(`https://www.amazon.com/dp/${ASIN}?tag=kitexample-20`);
  });

  it('removes a foreign tag in any letter case, keeping the other parameters', () => {
    expect(retagAmazonUrl(ctx, `https://www.amazon.com/dp/${ASIN}?Tag=thief-20&TAG=thief-21&th=1`, 'kitexample-pdp-20'))
      .toBe(`https://www.amazon.com/dp/${ASIN}?th=1&tag=kitexample-pdp-20`);
  });

  it('a tag argument that is not the site\'s own becomes the partner tag', () => {
    expect(retagAmazonUrl(ctx, 'https://www.amazon.com/s?k=a', 'evil-20')).toBe('https://www.amazon.com/s?k=a&tag=kitexample-20');
    expect(retagAmazonUrl(ctx, `https://www.amazon.com/dp/${ASIN}?tag=evil-20`, 'evil-20')).toBe(`https://www.amazon.com/dp/${ASIN}?tag=kitexample-20`);
  });

  it.each([
    'https://www.amazon.com.evil.io/dp/B0DS2X13PH',
    'https://amazon.evil.com/dp/B0DS2X13PH',
    'https://www.amazon.co.uk/dp/B0DS2X13PH',
    'https://smile.amazon.com/dp/B0DS2X13PH',
    'https://user:pw@www.amazon.com/dp/B0DS2X13PH',
    'javascript:alert(1)',
    '//www.amazon.com/dp/B0DS2X13PH',
    'not a url',
    '',
  ])('refuses %j', (url) => {
    expect(retagAmazonUrl(ctx, url)).toBe('');
  });
});

describe('amazonSearchCta', () => {
  it('re-tags a stored search URL with the placement tag', () => {
    const html = amazonSearchCta(ctx, { url: 'https://www.amazon.com/s?k=EcoFlow+Delta+2&tag=specpicks-20', placement: 'pdp-hero' });
    expect(html).toContain('rel="sponsored nofollow noopener"');
    expect(html).toContain('data-cta-placement="pdp-hero"');
    expect(hrefs(html)).toEqual(['https://www.amazon.com/s?k=EcoFlow+Delta+2&tag=kitexample-pdp-20']);
    expect(html).toContain('>Search Amazon for this item</a>');
  });
  it('renders nothing for a non-Amazon URL', () => {
    expect(amazonSearchCta(ctx, { url: 'https://www.amazon.com.evil.io/s?k=x', placement: 'pdp-hero' })).toBe('');
  });
});

describe('product links', () => {
  it('detailLink is an internal, absolute, non-affiliate link', () => {
    expect(detailLink(ctx, ASIN)).toBe(`<a class="ssr-detail-link" href="https://example.com/product/${ASIN}">See Full Details →</a>`);
    expect(detailLink(ctx, 'EBAY_1')).toBe('');
  });
  it('follows the site\'s product route', () => {
    const c = variant((j) => { j.routes.product = '/p/{asin}/specs'; });
    expect(productPath(c, ASIN)).toBe(`/p/${ASIN}/specs`);
    expect(productUrl(c, ASIN)).toBe(`https://example.com/p/${ASIN}/specs`);
  });
});

describe('classifySource (from site.json sources)', () => {
  it.each([
    [undefined, 'direct'],
    ['', 'direct'],
    ['/', 'home'],
    [`/product/${ASIN}`, 'product'],
    ['/categories', 'category'],
    ['/category/power-stations', 'category'],
    ['/brands', 'brand'],
    ['/brand/jackery', 'brand'],
    ['/best/power-stations-under-500', 'best'],
    ['/guides/winter-storm-outage-kit', 'article'],
    ['/deals', 'deal'],
    ['/deals/black-friday', 'deal'],
    ['/search', 'search'],
    ['/about', 'other'],
    ['/categoriesx', 'other'],
  ])('%j -> %s', (ref, want) => {
    expect(classifySource(ctx, ref)).toBe(want);
  });

  it('first matching rule wins', () => {
    const c = variant((j) => {
      j.sources = [{ kind: 'kits', prefixes: ['/kits/'] }, { kind: 'compat', prefixes: ['/kits/compat'] }];
      j.amazon.sourceTags = {};
    });
    expect(classifySource(c, '/kits/compat/x')).toBe('kits');
  });
});

describe('pickTrackingId (from site.json sourceTags)', () => {
  it('uses the kind\'s tag, else the partner tag', () => {
    expect(pickTrackingId(ctx, 'article')).toBe('kitexample-guides-20');
    expect(pickTrackingId(ctx, 'product')).toBe('kitexample-20');
    expect(pickTrackingId(ctx, 'direct')).toBe('kitexample-20');
    expect(pickTrackingId(ctx, undefined)).toBe('kitexample-20');
    expect(pickTrackingId(ctx, 'constructor')).toBe('kitexample-20');
  });
});
