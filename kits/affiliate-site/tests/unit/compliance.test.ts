/**
 * The kit-compliance rules (tests/ui/lib/compliance.ts), both ways: each one
 * passes what the kit renders and catches what a careless renderer emits. The
 * snapshots are built by hand; the in-browser collector is covered by the
 * detector self-test inside kit-compliance.spec.ts and the fixture site.
 */
import { AMAZON_CONTENT_NOTICE, priceDisclaimerFor } from '../../server/src/notices';
import { affiliateOfferLd } from '../../server/src/offer';
import { priceStamp } from '../../server/src/price';
import { htmlCacheControl } from '../../server/src/siteContext';
import { parseSiteContext, type SiteContext } from '../../server/src/siteContext';
import {
  DATED_PRICE_RE,
  DETAILS_LABEL_RE,
  MAX_STAMP_AGE_MS,
  PRICE_AT_AMAZON_RE,
  PRICE_CURRENCY_CODE_RE,
  PRICE_WITH_CENTS_RE,
  RENDER_ALLOWANCE_MS,
  SHOWN_CHECK_TIME_RE,
  affiliateLinkProblems,
  amazonContentReason,
  amazonSearchKey,
  asinsOfLink,
  availabilityTextProblems,
  cacheControlProblems,
  cacheDirectives,
  amazonImageProblems,
  amazonNoticeProblems,
  asinOfLink,
  earningsDisclosureProblems,
  jsonLdNodes,
  looksLikePrice,
  negationGoverns,
  offerProblems,
  priceAgeLimitMs,
  priceAmounts,
  priceDisplayProblems,
  priceTrackingProblems,
  recallProblems,
  shownCheckTimeMs,
  shownCheckTimes,
  sitemapImageProblems,
  tableProblems,
  titleNeedles,
  unnegatedMatches,
  wholeDollarPrices,
  type ComplianceAnchor,
  type ComplianceRules,
  type ComplianceSnapshot,
  type ComplianceStamp,
} from '../ui/lib/compliance';
import { collectCompliance } from '../ui/lib/complianceCollect';
import * as fs from 'fs';
import * as path from 'path';

const KIT = path.resolve(__dirname, '..', '..');
const ctx: SiteContext = parseSiteContext(JSON.parse(fs.readFileSync(path.join(KIT, 'fixture-site', 'site.json'), 'utf8')), { env: {} });
const NOW = Date.parse('2026-11-17T12:00:00Z');
const rules: ComplianceRules = {
  allowedTags: ctx.amazon.allowedTags,
  marketplaceHost: ctx.amazon.marketplaceHost,
  priceDisclaimer: priceDisclaimerFor(ctx),
  amazonNotice: AMAZON_CONTENT_NOTICE,
  earningsDisclosure: ctx.disclosure.earnings,
  maxSharedCacheSeconds: ctx.cache.htmlSMaxAgeSeconds,
  maxBrowserCacheSeconds: ctx.cache.htmlMaxAgeSeconds,
  quotableWindowMs: priceAgeLimitMs(ctx),
  now: NOW,
};

function snap(over: Partial<ComplianceSnapshot> = {}): ComplianceSnapshot {
  return {
    path: '/p', status: 200, headTexts: [], priceMetas: [], anchors: [], stamps: [], unstampedText: [], hiddenUnstampedText: [],
    attributeTexts: [], notAPriceText: [], strayPriceElements: [],
    noticeElements: [], visibleText: '', earningsBeforeFirstLink: null, amazonMarkers: 0, jsonld: [], socialImages: [], heroImages: [],
    forms: [], mdTables: [], rawTableText: [], recallNotices: 0, cacheControl: '', cdnCacheControl: '', surrogateControl: '', ...over,
  };
}

function anchor(href: string, over: Partial<ComplianceAnchor> = {}): ComplianceAnchor {
  return { href, abs: href, rel: 'sponsored nofollow noopener', target: '_blank', text: 'View', visible: true, placement: 'pdp-hero', ...over };
}

const textOf = (html: string): string => html.replace(/<[^>]+>/g, ' ').replace(/&#39;/g, "'").replace(/\s+/g, ' ').trim();

/**
 * The stamp the kit renders, as the collector reads it in a browser with
 * popover support: the disclaimer is in the stamp but hidden (not in its
 * visible text), and its visible Details button points at it.
 */
function kitStamp(fetchedAt: Date): ComplianceStamp {
  const html = priceStamp(ctx, { price: '349.99', currency: 'USD', fetchedAt }, { now: NOW, key: 'B0GATE0001|pdp-hero' });
  expect(html).not.toBe('');
  const popover = /<span id="[^"]+" class="kit-price-disclaimer" popover[^>]*>([\s\S]*?)<\/span>/.exec(html);
  expect(popover).not.toBeNull();
  const time = /datetime="([^"]+)"/.exec(html)?.[1] as string;
  return {
    kind: 'price',
    text: textOf(html),
    visibleText: textOf(html.replace((popover as RegExpExecArray)[0], '')),
    times: [time],
    linkedTexts: [textOf((popover as RegExpExecArray)[1] as string)],
    // Its one control is labelled Details and points at the disclaimer.
    detailsTexts: [textOf((popover as RegExpExecArray)[1] as string)],
    priceText: '$349.99',
    insideAnchor: false,
  };
}

/** The kit stamp as if checked at `at` (shown time and <time datetime> both), even outside the window priceStamp quotes in. */
function stampAt(at: Date): ComplianceStamp {
  const base = kitStamp(new Date(NOW - 3600_000));
  const shown = `${at.toISOString().slice(0, 10)} ${at.toISOString().slice(11, 16)} UTC`;
  const retime = (t: string) => t.replace(/\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC/, shown);
  return { ...base, times: [at.toISOString()], text: retime(base.text), visibleText: retime(base.visibleText) };
}

describe('price patterns', () => {
  it.each([
    ['$349.99', true], ['$1,299.00', true], ['now $12.50!', true], ['$349 on Amazon', true], ['$1,000 at Amazon today', true],
    ['under $500', false], ['the $1,000 tier', false], ['$40 a year', false], ['3.99 kWh', false], ['US$', false],
    // Whole dollars with a price cue shortly before them (review round 1).
    ['The EcoFlow Delta 2 sells for $449 right now.', true], ['Now just $1,099!', true], ['Total for this kit: $1,249', true],
    ['Jackery Explorer 1000 v2 review: from $449, 1070Wh', true], ['Get it for only $299', true], ['Listed at $899 this week', true],
    // Budgets, ranges, rates and approximations are not prices.
    ['The best widgets for under $500', false], ['Best widgets for $500', false], ['priced around $500', false],
    ['models from $200 to $500', false], ['It costs about $40 a year to run', false], ['now $300+ models', false],
    ['a $1,000 budget', false], ['costs $0.15 per kWh at home', true], ['it costs $40 per year', false], ['spend $1k', false],
    // A currency code instead of a dollar sign, and a whole-dollar amount given a check time (K5, kit 0.2.0).
    ['USD 349.99', true], ['349.99 USD', true], ['US$1,299.00', true], ['a USD 500 budget', false], ['1.5 USD/kWh', false],
    ['The cheapest that runs it all day is the EcoFlow River 3, $349 (as of Nov 17).', true],
    ['$349 on Amazon.com as of 14:11 UTC', true], ['$349 (checked Nov 17)', true],
    ['Our $500 tier, as of this winter, holds three stations.', false], ['the $500 updated ladder', false],
    ['Best power stations under $500 as of November 2026', false], ['between $300 and $500 (as of Nov 17)', false],
  ])('%s -> %s', (text, want) => {
    expect(looksLikePrice(text)).toBe(want);
  });

  it('keeps the patterns separate', () => {
    expect(PRICE_WITH_CENTS_RE.test('$5.00')).toBe(true);
    expect(PRICE_AT_AMAZON_RE.test('$5 via Amazon')).toBe(true);
    expect(PRICE_AT_AMAZON_RE.test('$5 via eBay')).toBe(false);
    expect(wholeDollarPrices('It sells for $449, and the kit totals $1,249.')).toEqual(['$449', '$1,249']);
    expect(wholeDollarPrices('From $200 to $500.')).toEqual([]);
    expect(PRICE_CURRENCY_CODE_RE.test('USD 12.50')).toBe(true);
    expect(DATED_PRICE_RE.test('$12 (as of today)')).toBe(true);
    expect(DATED_PRICE_RE.test('$12 a month')).toBe(false);
  });

  it('priceAmounts lists each distinct amount once, in order', () => {
    expect(priceAmounts('$349.99 on Amazon.com as of 2026-11-17 14:11 UTC (price may vary) Details')).toEqual(['349.99']);
    expect(priceAmounts('$1,299.00 on Amazon, list $1,499.00')).toEqual(['1299.00', '1499.00']);
    expect(priceAmounts('It sells for $449; the kit totals $1,249 and the cord $19.99.')).toEqual(['449', '1249', '19.99']);
    expect(priceAmounts('Best widgets under $500, as of 2026-11-17 14:11 UTC')).toEqual([]);
    expect(priceAmounts('Best widgets under $500 as of 2026-11-17 14:11 UTC')).toEqual([]);
    expect(priceAmounts('The Volt, $349 (as of Nov 17), or the Cell, $229 (as of Nov 17)')).toEqual(['349', '229']);
  });
});

describe('the check time a stamp shows (2(i): date and time, with a zone)', () => {
  it.each<[string, string | null]>([
    // The kit's own stamp, Amazon's example, and the other common ways to write it.
    ['$349.99 on Amazon.com as of 2026-11-17 14:11 UTC (price may vary) Details', '2026-11-17T14:11:00.000Z'],
    ['Amazon.com Price: $32.77 (as of 01/07/2008 14:11 PST- Details)', '2008-01-07T22:11:00.000Z'],
    ['as of Nov 17, 2026, 9:11 AM EST', '2026-11-17T14:11:00.000Z'],
    ['as of November 17, 2026 at 9:11 a.m. EST', '2026-11-17T14:11:00.000Z'],
    ['as of 17 Nov 2026 14:11 GMT', '2026-11-17T14:11:00.000Z'],
    ['as of 2026-11-17T14:11:30Z', '2026-11-17T14:11:30.000Z'],
    ['as of 2026-11-17 15:11 +01:00', '2026-11-17T14:11:00.000Z'],
    ['as of 2026-11-17 06:11 (UTC-8)', '2026-11-17T14:11:00.000Z'],
    ['checked 14:11 UTC on 2026-11-17', '2026-11-17T14:11:00.000Z'],
    ['as of 12:05 AM PST, Nov 18, 2026', '2026-11-18T08:05:00.000Z'],
    // Shown with a zone, but not one instant: no year, or a generic zone.
    ['as of Nov 17, 2:11 PM ET', null],
    ['as of Nov 17 14:11 UTC', null],
  ])('%s', (text, iso) => {
    const shown = shownCheckTimes(text);
    expect(shown).toHaveLength(1);
    const at = shownCheckTimeMs(shown[0] as string);
    expect(at === null ? null : new Date(at).toISOString()).toBe(iso);
  });

  it.each([
    'as of 2026-11-17',
    'as of 14:11 UTC',
    'as of 2026-11-17 14:11',
    'as of the last check',
    'as of x',
    'updated today at noon',
    '(price may vary) Details',
  ])('not a check time a reader can use: %s', (text) => {
    expect(SHOWN_CHECK_TIME_RE.test(text)).toBe(false);
  });

  it('the Details label: the word alone', () => {
    for (const ok of ['Details', 'details', '(Details)', ' Details › ', '- Details']) expect([ok, DETAILS_LABEL_RE.test(ok)]).toEqual([ok, true]);
    for (const no of ['More', 'Price details for this item', 'Disclaimer', 'Info']) expect([no, DETAILS_LABEL_RE.test(no)]).toEqual([no, false]);
    // The in-browser collector carries the same pattern (it cannot import it).
    expect(collectCompliance.toString()).toContain(DETAILS_LABEL_RE.source);
  });
});

describe('affiliateLinkProblems', () => {
  const tag = ctx.amazon.partnerTag;

  it('passes the kit CTA, a search CTA and a /r/ link (local or on the canonical origin)', () => {
    const s = snap({
      anchors: [
        anchor(`https://www.amazon.com/dp/B0GATE0001?tag=${tag}&linkCode=ll1`),
        anchor(`https://www.amazon.com/s?k=widget&tag=${ctx.amazon.placementTags['pdp-hero']}`),
        anchor('http://127.0.0.1:10032/r/B0GATE0001', { rel: 'sponsored nofollow', target: '' }),
        anchor('https://gate-fixture.test/r/B0GATE0001', { rel: 'sponsored nofollow noopener', target: '_blank' }),
      ],
    });
    expect(affiliateLinkProblems(s, rules)).toEqual([]);
  });

  it('catches a foreign tag, a missing tag, missing rel, noreferrer, no target and short links', () => {
    const s = snap({
      anchors: [
        anchor('https://www.amazon.com/dp/B0GATE0001?tag=someoneelse-20', { rel: '', target: '' }),
        anchor('https://www.amazon.com/dp/B0GATE0001'),
        anchor(`https://www.amazon.com/dp/B0GATE0001?tag=${tag}`, { rel: 'sponsored nofollow noopener noreferrer' }),
        anchor(`https://www.amazon.com/dp/B0GATE0001?tag=${tag}&tag=${tag}`),
        anchor('https://amzn.to/abc'),
        anchor(`https://www.amazon.co.uk/dp/B0GATE0001?tag=${tag}`),
        anchor('http://127.0.0.1:10032/r/B0GATE0001', { rel: 'nofollow' }),
      ],
    });
    const out = affiliateLinkProblems(s, rules).join('\n');
    expect(out).toMatch(/tag "someoneelse-20" is not one of the site's tracking IDs/);
    expect(out).toMatch(/rel="" lacks sponsored, nofollow, noopener/);
    expect(out).toMatch(/target="", expected _blank/);
    expect(out).toMatch(/no tag= \(an untagged Amazon link earns nothing\)/);
    expect(out).toMatch(/noreferrer/);
    expect(out).toMatch(/2 tag= parameters/);
    expect(out).toMatch(/amzn\.to cannot carry the site's tag/);
    expect(out).toMatch(/www\.amazon\.co\.uk cannot carry the site's tag/);
    expect(out).toMatch(/\/r\/B0GATE0001: rel="nofollow" lacks sponsored/);
  });
});

describe('earningsDisclosureProblems (Operating Agreement section 5)', () => {
  const link = anchor('https://www.amazon.com/dp/B0GATE0001?tag=gatefixture-20');
  const shown = `Widget\n${ctx.disclosure.earnings}\nView`;

  it('passes a page that shows the disclosure before its first affiliate link, and pages without links', () => {
    expect(earningsDisclosureProblems(snap({ anchors: [link], visibleText: shown, earningsBeforeFirstLink: true }), rules)).toEqual([]);
    expect(earningsDisclosureProblems(snap({ visibleText: 'About us' }), rules)).toEqual([]);
    // A link nobody can see (a desktop-hidden sticky bar) needs nothing.
    expect(earningsDisclosureProblems(snap({ anchors: [{ ...link, visible: false }], visibleText: 'x' }), rules)).toEqual([]);
    // CSS may change its case, never its words.
    expect(earningsDisclosureProblems(snap({ anchors: [link], visibleText: shown.toUpperCase(), earningsBeforeFirstLink: true }), rules)).toEqual([]);
  });

  it('catches a hidden or missing disclosure, and one that only comes after the first link', () => {
    expect(earningsDisclosureProblems(snap({ anchors: [link, link], visibleText: 'Widget\nView' }), rules)).toEqual([
      `/p: 2 visible affiliate link(s) but the earnings disclosure ("${ctx.disclosure.earnings.slice(0, 60)}...") is not in plain view (Operating Agreement section 5)`,
    ]);
    const after = earningsDisclosureProblems(snap({ anchors: [link], visibleText: shown, earningsBeforeFirstLink: false }), rules);
    expect(after.join('\n')).toMatch(/the earnings disclosure first shows after the first affiliate link \(https:\/\/www\.amazon\.com\/dp\/B0GATE0001/);
  });
});

describe('priceDisplayProblems', () => {
  it('passes the kit stamp (disclaimer behind its visible Details control, checked within the window)', () => {
    const s = snap({ stamps: [kitStamp(new Date(NOW - 2 * 3600_000))] });
    expect(priceDisplayProblems(s, rules)).toEqual([]);
  });

  it('accepts any visible Details control that reveals the disclaimer: a popover, a link to it, a <details>', () => {
    const base = kitStamp(new Date(NOW - 3600_000));
    const at = base.times[0] as string;
    const shownAt = `${at.slice(0, 10)} ${at.slice(11, 16)} UTC`;
    const other = { ...base, text: `$10.00 on Amazon.com as of ${shownAt} Details ${rules.priceDisclaimer}`, visibleText: `$10.00 on Amazon.com as of ${shownAt} Details`, priceText: '$10.00' };
    expect(priceDisplayProblems(snap({ stamps: [{ ...other, linkedTexts: [rules.priceDisclaimer], detailsTexts: [rules.priceDisclaimer] }] }), rules)).toEqual([]);
  });

  it('a stamp needs the Details control even when the disclaimer is in view beside it (K5, kit 0.2.0)', () => {
    const base = kitStamp(new Date(NOW - 3600_000));
    // The disclaimer printed inline, with no Details control: 2(i) allows "adjacent", the kit's rule asks for Details.
    const inline = { ...base, visibleText: base.text, linkedTexts: [], detailsTexts: [] };
    expect(priceDisplayProblems(snap({ stamps: [inline] }), rules)).toEqual([
      `/p price stamp 1 ("${base.text.slice(0, 70)}"): no visible "Details" control that reveals the exact 2(i) disclaimer`,
    ]);
    // A control that reveals it but is not labelled Details ("More info") does not count either.
    const more = { ...base, detailsTexts: [] };
    expect(priceDisplayProblems(snap({ stamps: [more] }), rules).join('\n')).toMatch(/no visible "Details" control/);
    // A Details control that reveals something else does not count.
    const wrong = { ...base, detailsTexts: ['Prices may change.'] };
    expect(priceDisplayProblems(snap({ stamps: [wrong] }), rules).join('\n')).toMatch(/no visible "Details" control that reveals the exact 2\(i\) disclaimer/);
  });

  it('a stamp shows its check time with a zone, and that time is its <time datetime> (K5, kit 0.2.0)', () => {
    const base = kitStamp(new Date(NOW - 3600_000));
    const at = base.times[0] as string;
    const swap = (visible: string) => ({ ...base, visibleText: base.visibleText.replace(/as of .* UTC/, visible) });
    const problems = (visible: string) => priceDisplayProblems(snap({ stamps: [swap(visible)] }), rules).join('\n');
    // The machine-readable time alone ("as of the last check"), a date without a time, a time without a zone.
    expect(problems('as of the last check')).toMatch(/does not show the date and time of the check with a time zone/);
    expect(problems(`as of ${at.slice(0, 10)}`)).toMatch(/does not show the date and time/);
    expect(problems(`as of ${at.slice(0, 10)} ${at.slice(11, 16)}`)).toMatch(/does not show the date and time/);
    // Another format with a zone is fine; a generic zone cannot be pinned, so it is not compared.
    expect(problems('as of Nov 17, 2026, 6:00 AM EST')).toBe('');
    expect(problems('as of Nov 17, 6:00 AM ET')).toBe('');
    // A shown time that is not the stamp's <time>: a renderer printing the render time instead of the fetch time.
    expect(problems('as of 2026-11-17 12:00 UTC')).toBe(`/p price stamp 1 ("${base.text.slice(0, 70)}"): shows "2026-11-17 12:00 UTC" but its <time datetime> says ${at}`);
  });

  it('one price per stamp; no price in an availability stamp (K5, kit 0.2.0)', () => {
    const base = kitStamp(new Date(NOW - 3600_000));
    const two = { ...base, visibleText: base.visibleText.replace('$349.99', '$349.99 (list $399.99)') };
    expect(priceDisplayProblems(snap({ stamps: [two] }), rules).join('\n')).toMatch(/holds 2 prices \(\$349\.99, \$399\.99\): each price needs its own stamp/);
    const sentence = { ...base, visibleText: `The cheapest is the Volt at ${base.visibleText}, and the Cell 300 sells for $229 there.` };
    expect(priceDisplayProblems(snap({ stamps: [sentence] }), rules).join('\n')).toMatch(/holds 2 prices \(\$349\.99, \$229\)/);
    const avail = { ...base, kind: 'availability', priceText: '', visibleText: base.visibleText.replace('$349.99 on Amazon.com', 'In stock on Amazon.com at $349.99') };
    expect(priceDisplayProblems(snap({ stamps: [avail] }), rules).join('\n')).toMatch(/an availability stamp holds a price \(\$349\.99\)/);
  });

  it('catches a price in a sentence outside the stamp, in hidden text, in an attribute, and an Amazon price marked not-a-price (K5, kit 0.2.0)', () => {
    const s = snap({
      stamps: [kitStamp(new Date(NOW - 3600_000))],
      unstampedText: [
        '5 of 12 stations run a fridge for 24 h; the cheapest is the River 3, $349 (as of Nov 17).',
        'It costs USD 349.99 today.',
      ],
      hiddenUnstampedText: ['Price: $349.99'],
      attributeTexts: [{ where: 'aria-label of <a>', text: 'Buy the Volt 1000 for $699.00 on Amazon' }, { where: 'title of <abbr>', text: 'Watt-hours' }],
      notAPriceText: ['Electricity costs about $0.15 per kWh.', 'The Volt is $699 on Amazon.'],
    });
    expect(priceDisplayProblems(s, rules)).toEqual([
      '/p: price outside a dated stamp: "5 of 12 stations run a fridge for 24 h; the cheapest is the River 3, $349 (as of Nov 17)."',
      '/p: price outside a dated stamp: "It costs USD 349.99 today."',
      '/p: price outside a dated stamp, in text a reader does not see but crawlers and assistants read: "Price: $349.99"',
      '/p: price outside a dated stamp, in aria-label of <a>: "Buy the Volt 1000 for $699.00 on Amazon"',
      '/p: an Amazon price inside data-kit-not-a-price (that marker is for rates and budgets): "The Volt is $699 on Amazon."',
    ]);
  });

  it('catches a stale stamp, a future one, no time, no disclaimer, a stamp in a link, a stamp without a price', () => {
    const base = kitStamp(new Date(NOW - 3600_000));
    const s = snap({
      stamps: [
        { ...base, times: [new Date(NOW - MAX_STAMP_AGE_MS - 60_000).toISOString()] },
        { ...base, times: [new Date(NOW + 3600_000).toISOString()] },
        { ...base, times: [] },
        { ...base, text: '$349.99 on Amazon.com as of x (price may vary)', visibleText: '$349.99 on Amazon.com as of x (price may vary)', linkedTexts: [] },
        { ...base, insideAnchor: true },
        { ...base, priceText: 'see Amazon' },
        { ...base, kind: 'deal' },
      ],
    });
    const out = priceDisplayProblems(s, rules).join('\n');
    expect(out).toMatch(/older than 24h/);
    expect(out).toMatch(/in the future/);
    expect(out).toMatch(/no <time datetime>/);
    expect(out).toMatch(/without the exact 2\(i\) disclaimer/);
    expect(out).toMatch(/inside a link/);
    expect(out).toMatch(/price stamp without a \$X\.XX price/);
    expect(out).toMatch(/data-price-stamp="deal" is neither price nor availability/);
  });

  it('catches a disclaimer that is in the stamp but hidden, with no control to show it (review round 1)', () => {
    const base = kitStamp(new Date(NOW - 3600_000));
    const hidden = { ...base, linkedTexts: [] };
    expect(priceDisplayProblems(snap({ stamps: [hidden] }), rules).join('\n')).toMatch(/without the exact 2\(i\) disclaimer .* \(it is in the stamp but hidden\)/);
  });

  it('holds stamps to the quotable window (24h minus the HTML cache lifetimes), judged when the page was read', () => {
    const windowMs = priceAgeLimitMs(ctx);
    // 600 s s-maxage + 60 s max-age + 0 s SSR cache off 24h, plus the render allowance.
    expect(windowMs).toBe((24 * 3600 - 660) * 1000 + RENDER_ALLOWANCE_MS);
    const past = stampAt(new Date(NOW - 23 * 3600_000 - 55 * 60_000));
    const out = priceDisplayProblems(snap({ stamps: [past] }), rules).join('\n');
    expect(out).toMatch(/23\.9h ago \(past the 23\.8h quotable window/);
    expect(out).not.toMatch(/older than 24h/);
    // Without a window it is 24h.
    expect(priceDisplayProblems(snap({ stamps: [past] }), { ...rules, quotableWindowMs: undefined })).toEqual([]);
    // takenAt, not the clock at judging time: a snapshot judged an hour later is not an hour older.
    const edge = stampAt(new Date(NOW - windowMs + 60_000));
    expect(priceDisplayProblems(snap({ stamps: [edge], takenAt: NOW }), { ...rules, now: NOW + 10 * 60_000 })).toEqual([]);
    expect(priceDisplayProblems(snap({ stamps: [edge] }), { ...rules, now: NOW + 10 * 60_000 }).join('\n')).toMatch(/quotable window/);
  });

  it('catches prices outside stamps, whole-dollar prices, prices in head metadata and price meta tags, not budgets', () => {
    const s = snap({
      unstampedText: ['The widget is $349.99 right now.', 'Best widgets under $500', 'The EcoFlow Delta 2 sells for $449 right now.'],
      strayPriceElements: ['$699.00'],
      headTexts: [
        { where: '<title>', text: 'Widget, now $349.99' },
        { where: 'meta description', text: 'Best widgets under $500' },
        { where: 'meta description', text: 'Jackery Explorer 1000 v2 review: from $449, 1070Wh' },
        { where: 'og:title', text: 'Northwind PW-500: $349.99 on Amazon' },
      ],
      priceMetas: [{ prop: 'product:price:amount', content: '349.00' }],
    });
    expect(priceDisplayProblems(s, rules)).toEqual([
      '/p: price outside a dated stamp: "The widget is $349.99 right now."',
      '/p: price outside a dated stamp: "The EcoFlow Delta 2 sells for $449 right now."',
      '/p: price outside a dated stamp: "$699.00" (a .ssr-price outside a stamp)',
      '/p: price in <title> (head metadata cannot carry the 2(i) time and disclaimer): "Widget, now $349.99"',
      '/p: price in meta description (head metadata cannot carry the 2(i) time and disclaimer): "Jackery Explorer 1000 v2 review: from $449, 1070Wh"',
      '/p: price in og:title (head metadata cannot carry the 2(i) time and disclaimer): "Northwind PW-500: $349.99 on Amazon"',
      '/p: price meta tag product:price:amount="349.00" (a price with no time or disclaimer)',
    ]);
  });
});

describe('availabilityTextProblems (availability is held to 2(i) too)', () => {
  it('catches availability said outside a stamp, in the body or in head metadata', () => {
    const s = snap({
      unstampedText: [
        'The Fabrikam Cell 300 is in stock on Amazon today.',
        'Only 3 left!',
        'Currently unavailable.',
        'Unavailable on Amazon. Similar, in stock:',
        'Usually ships within 2 days.',
        'Not in stock on Amazon right now.',
      ],
      headTexts: [{ where: 'meta description', text: 'A 640 Wh widget that Amazon no longer has in stock.' }],
    });
    const out = availabilityTextProblems(s).join('\n');
    expect(out).toContain('/p: availability outside a dated stamp (stock status): "The Fabrikam Cell 300 is in stock on Amazon today."');
    expect(out).toContain('(units left): "Only 3 left!"');
    expect(out).toContain('(currently unavailable): "Currently unavailable."');
    expect(out).toContain('(availability on Amazon): "Unavailable on Amazon.');
    expect(out).toContain('(stock status): "Similar, in stock:"');
    expect(out).toContain('(shipping estimate)');
    // Saying it is NOT in stock is an availability statement too.
    expect(out).toContain('(stock status): "Not in stock on Amazon right now."');
    expect(out).toContain('/p: availability in meta description (stock status; head metadata cannot carry the 2(i) time and disclaimer)');
  });

  it('passes the kit fallback, negated statements and ordinary copy', () => {
    const s = snap({
      unstampedText: [
        'See current price and availability on Amazon.com',
        'A similar widget you can buy',
        'Keep the stock cord: it is rated for the full load.',
        'Prices and availability come from Amazon and carry the time they were checked.',
      ],
    });
    expect(availabilityTextProblems(s)).toEqual([]);
  });
});

describe('amazonNoticeProblems (2(k))', () => {
  it('passes a page with Amazon content and the notice in plain view', () => {
    const s = snap({ amazonMarkers: 1, visibleText: `Title\n${AMAZON_CONTENT_NOTICE}`, noticeElements: [{ text: AMAZON_CONTENT_NOTICE, visible: true }] });
    expect(amazonNoticeProblems(s, rules)).toEqual([]);
  });

  it('passes a page with no Amazon content and no notice', () => {
    expect(amazonNoticeProblems(snap({ visibleText: 'About us' }), rules, ['Contoso Volt 1000 Widget with Four AC Outlets'])).toEqual([]);
  });

  it('needs the notice for a marker, a stamp, or a known Amazon title in the text', () => {
    const title = 'Contoso Volt 1000 Widget with Four AC Outlets';
    expect(amazonContentReason(snap({ amazonMarkers: 2 }), [])).toBe('2 data-amazon-content element(s)');
    expect(amazonContentReason(snap({ stamps: [kitStamp(new Date(NOW))] }), [])).toBe('1 price/availability stamp(s)');
    expect(amazonContentReason(snap({ visibleText: `Amazon lists it as ${title.toUpperCase()}.` }), [title])).toContain('the Amazon title');
    // Short titles are too likely to be our own words.
    expect(amazonContentReason(snap({ visibleText: 'Widget 500' }), ['Widget 500'])).toBe('');
    const out = amazonNoticeProblems(snap({ visibleText: `Amazon lists it as ${title}.` }), rules, [title]);
    expect(out).toEqual([`/p: shows Amazon content (the Amazon title "${title}") but not the 2(k) notice`]);
  });

  it('matches a truncated title by its opening words (review round 1)', () => {
    const title = 'Contoso Volt 1000 Widget with Four AC Outlets and Pass-Through Charging, 1024Wh';
    expect(titleNeedles(title)).toEqual([title, 'Contoso Volt 1000 Widget with Four AC']);
    expect(titleNeedles('Widget Title From Amazon, 512Wh, Gray')).toEqual(['Widget Title From Amazon, 512Wh, Gray']);
    expect(titleNeedles('Too short')).toEqual([]);
    const text = 'Amazon lists it as Contoso Volt 1000 Widget with Four AC Outlets and Pass-Through…';
    expect(amazonContentReason(snap({ visibleText: text }), [title])).toBe('the Amazon title "Contoso Volt 1000 Widget with Four AC..." (truncated)');
    expect(amazonNoticeProblems(snap({ visibleText: text }), rules, [title])).toHaveLength(1);
    // Our own model name and our summary are not the title.
    expect(amazonContentReason(snap({ visibleText: 'Contoso Volt 1000\nA 1,024 Wh widget with four AC outlets and pass-through charging.' }), [title])).toBe('');
  });

  it('catches a hidden notice and a notice with altered wording', () => {
    const hidden = snap({ amazonMarkers: 1, visibleText: 'Title', noticeElements: [{ text: AMAZON_CONTENT_NOTICE, visible: false }] });
    expect(amazonNoticeProblems(hidden, rules).join('\n')).toMatch(/in plain view \(it is in the page but hidden\)/);
    const altered = AMAZON_CONTENT_NOTICE.replace('‘AS IS’', "'AS IS'");
    const wrong = snap({ amazonMarkers: 1, visibleText: altered, noticeElements: [{ text: altered, visible: true }] });
    const out = amazonNoticeProblems(wrong, rules).join('\n');
    expect(out).toMatch(/does not hold the exact 2\(k\) notice/);
    expect(out).toMatch(/but not the 2\(k\) notice/);
  });
});

describe('offerProblems', () => {
  const fresh = new Date(NOW - 2 * 3600_000);
  const kitOffer = affiliateOfferLd(ctx, { price: '349.99', currency: 'USD', fetchedAt: fresh, url: 'https://gate-fixture.test/product/B0GATE0001' }, NOW);
  const product = (offers: unknown, extra: Record<string, unknown> = {}) => JSON.stringify({ '@context': 'https://schema.org', '@type': 'Product', name: 'W', sku: 'B0GATE0001', offers, ...extra });

  it('passes the kit Offer beside a price stamp', () => {
    expect(kitOffer).not.toBeNull();
    expect(offerProblems(snap({ jsonld: [product(kitOffer)], stamps: [kitStamp(fresh)] }), rules)).toEqual([]);
  });

  it('catches stale, missing and future validFrom, non-USD, junk price, seller claims, no visible price and ratings', () => {
    const offers = [
      { ...kitOffer, validFrom: new Date(NOW - MAX_STAMP_AGE_MS - 1).toISOString() },
      { ...kitOffer, validFrom: undefined },
      { ...kitOffer, validFrom: 'yesterday' },
      { ...kitOffer, validFrom: new Date(NOW + 3600_000).toISOString() },
      { ...kitOffer, priceCurrency: 'CAD' },
      { ...kitOffer, price: '0.00' },
      { ...kitOffer, seller: { '@type': 'Organization', name: 'Us' }, shippingDetails: {} },
      { ...kitOffer, validFrom: new Date(NOW - 23 * 3600_000 - 55 * 60_000).toISOString() },
    ];
    const s = snap({ jsonld: [product(offers, { aggregateRating: { '@type': 'AggregateRating', ratingValue: '5' } })] });
    const out = offerProblems(s, rules).join('\n');
    expect(out).toMatch(/older than 24h/);
    expect(out).toMatch(/is 23\.9h old \(past the 23\.8h quotable window/);
    expect(out).toMatch(/no validFrom/);
    expect(out).toMatch(/validFrom "yesterday" does not parse/);
    expect(out).toMatch(/is in the future/);
    expect(out).toMatch(/priceCurrency "CAD", expected USD/);
    expect(out).toMatch(/price "0.00" is not a real price/);
    expect(out).toMatch(/carries seller/);
    expect(out).toMatch(/carries shippingDetails/);
    expect(out).toMatch(/8 Offer\(s\) in JSON-LD but no visible dated price/);
    expect(out).toMatch(/AggregateRating/);
  });

  it('walks @graph and ItemList nodes, and skips blocks that do not parse', () => {
    const list = JSON.stringify({ '@context': 'https://schema.org', '@graph': [{ '@type': 'ItemList', itemListElement: [{ '@type': 'ListItem', item: { '@type': 'Product', offers: { ...kitOffer, validFrom: '2026-01-01T00:00:00Z' } } }] }] });
    expect(jsonLdNodes([list, '{not json']).length).toBeGreaterThan(3);
    expect(offerProblems(snap({ jsonld: [list], stamps: [kitStamp(fresh)] }), rules).join('\n')).toMatch(/validFrom 2026-01-01T00:00:00Z is .*h old/);
  });
});

describe('priceTrackingProblems (6(y))', () => {
  it('catches alert and history copy, and alert forms', () => {
    const s = snap({
      visibleText: 'Deals\nThe PW-500 is at its lowest price ever.\nSet a price alert and we will email you.\nAll-time low!\nLast tracked at $199.\nIt has never been cheaper.\nA lowest-ever deal.',
      forms: [{ action: '/api/watch', text: 'Watch', fields: ['price_watch_email'] }, { action: '', text: 'Track price', fields: [] }],
    });
    const out = priceTrackingProblems(s).join('\n');
    expect(out).toContain('lowest ever copy: "A lowest-ever deal."');
    for (const want of ['lowest ever copy', 'price alert copy', 'all-time low copy', 'last tracked price copy', 'never been cheaper copy', 'form action /api/watch', 'a control: "Track price"']) {
      expect(out).toContain(want);
    }
  });

  it('catches selling copy built on negation words, price-drop notices and price tracking (review round 1)', () => {
    const s = snap({
      visibleText: [
        'Never miss a deal: set a price alert for this power station.',
        "Don't miss out on the lowest price ever for the Jackery 1000.",
        'No other store beats this all-time low.',
        'Get notified when the price drops.',
        'Track this price and we will email you.',
        "We'll email you when the price drops.",
        "We don't spam, but we will send a price alert.",
      ].join('\n'),
    });
    const out = priceTrackingProblems(s);
    for (const want of [
      'price alert copy: "Never miss a deal: set a price alert for this power station."',
      'lowest ever copy: "Don\'t miss out on the lowest price ever for the Jackery 1000."',
      'all-time low copy: "No other store beats this all-time low."',
      'price-drop notification copy: "Get notified when the price drops."',
      'price tracking copy: "Track this price and we will email you."',
      'price-drop notification copy: "We\'ll email you when the price drops."',
      'price alert copy: "We don\'t spam, but we will send a price alert."',
    ]) {
      expect(out).toContain(`/p: ${want}`);
    }
  });

  it('lets negated statements and ordinary copy through', () => {
    const s = snap({
      visibleText: [
        'This site does not track prices over time and does not send price alerts.',
        'We never claim a lowest-ever price.',
        'We do not offer price alerts or a price history.',
        'No price alerts, no price history.',
        'We do not track the price of anything.',
        'The price of $349.99 dropped in March.',
        'A lowest-in-class weight.',
      ].join('\n'),
      forms: [{ action: '/search', text: 'Search', fields: ['q'] }],
    });
    expect(priceTrackingProblems(s)).toEqual([]);
  });

  it('a negation governs a phrase only up close, or through a claim verb, never through an idiom or a clause break', () => {
    expect(negationGoverns('We do not ')).toBe(true);
    expect(negationGoverns('We never claim a ')).toBe(true);
    expect(negationGoverns('We do not offer price alerts or a ')).toBe(true);
    expect(negationGoverns('Never miss a deal: set a ')).toBe(false);
    expect(negationGoverns("Don't miss out on the ")).toBe(false);
    expect(negationGoverns('No other store beats this ')).toBe(false);
    expect(negationGoverns('Not only the best kit, it is also at its ')).toBe(false);
    expect(negationGoverns('We never spam you, but we send a ')).toBe(false);
    expect(negationGoverns('Never pay full price again with our handy weekly ')).toBe(false);
  });

  it('reads sentences by block and by terminator, not across a price\'s decimal point', () => {
    expect(unnegatedMatches('Not available.\nSet a price alert', /price alert/i)).toEqual(['Set a price alert']);
    expect(unnegatedMatches('Now $349.99 - set a price alert', /price alert/i)).toEqual(['Now $349.99 - set a price alert']);
    expect(unnegatedMatches('We do not offer a $9.99 price alert', /price alert/i)).toEqual([]);
  });
});

describe('amazonImageProblems', () => {
  it('passes our own images', () => {
    const s = snap({
      socialImages: [{ prop: 'og:image', url: 'https://gate-fixture.test/assets/og-card.png' }],
      heroImages: ['https://gate-fixture.test/assets/hero-guide.svg'],
      jsonld: [JSON.stringify({ '@context': 'https://schema.org', '@type': 'Article', image: 'https://gate-fixture.test/assets/hero.svg', publisher: { '@type': 'Organization', logo: { '@type': 'ImageObject', url: 'https://gate-fixture.test/logo.png' } } })],
    });
    expect(amazonImageProblems(s)).toEqual([]);
  });

  it('catches Amazon CDN images in og:image, twitter:image, the hero and Article JSON-LD', () => {
    const s = snap({
      socialImages: [{ prop: 'og:image', url: 'https://m.media-amazon.com/images/I/x.jpg' }, { prop: 'twitter:image', url: '//images-na.ssl-images-amazon.com/images/I/y.jpg' }],
      heroImages: ['https://images-amazon.com/z.jpg'],
      jsonld: [JSON.stringify({ '@context': 'https://schema.org', '@type': 'BlogPosting', image: [{ '@type': 'ImageObject', url: 'https://m.media-amazon.com/images/I/w.jpg' }] })],
    });
    const out = amazonImageProblems(s);
    expect(out.join('\n')).toMatch(/og:image on an Amazon host/);
    expect(out.join('\n')).toMatch(/twitter:image on an Amazon host/);
    expect(out.join('\n')).toMatch(/hero image on an Amazon host/);
    // Named once, by the Article that holds it (not again by its ImageObject).
    expect(out.filter((l) => l.includes('w.jpg'))).toEqual(['/p: BlogPosting JSON-LD image on an Amazon host: https://m.media-amazon.com/images/I/w.jpg']);
  });

  it('catches an Amazon image in any JSON-LD image property, Product included (review round 1)', () => {
    const s = snap({
      jsonld: [
        JSON.stringify({ '@context': 'https://schema.org', '@type': 'Product', image: 'https://m.media-amazon.com/images/I/x.jpg' }),
        JSON.stringify({ '@context': 'https://schema.org', '@type': 'Organization', logo: 'https://images-na.ssl-images-amazon.com/images/I/l.png' }),
      ],
    });
    expect(amazonImageProblems(s)).toEqual([
      '/p: Product JSON-LD image on an Amazon host: https://m.media-amazon.com/images/I/x.jpg',
      '/p: Organization JSON-LD logo on an Amazon host: https://images-na.ssl-images-amazon.com/images/I/l.png',
    ]);
  });

  it('catches an Amazon image in a sitemap', () => {
    const xml = '<urlset xmlns:image="http://www.google.com/schemas/sitemap-image/1.1"><url><loc>https://gate-fixture.test/product/B0GATE0001</loc>'
      + '<image:image><image:loc>https://m.media-amazon.com/images/I/s.jpg?x=1&amp;y=2</image:loc></image:image></url>'
      + '<url><loc>https://gate-fixture.test/guides/a</loc><image:image><image:loc>https://gate-fixture.test/assets/hero.svg</image:loc></image:image></url></urlset>';
    expect(sitemapImageProblems('sitemap-products-1.xml', xml)).toEqual(['sitemap-products-1.xml: <image:loc> on an Amazon host: https://m.media-amazon.com/images/I/s.jpg?x=1&y=2']);
    expect(sitemapImageProblems('sitemap-guides.xml', xml.replace('m.media-amazon.com', 'cdn.example.org'))).toEqual([]);
  });
});

describe('tableProblems', () => {
  const good = { wrapped: true, headerCells: 3, scopedHeaderCells: 3, bodyRows: 2, rowCells: [3, 3] };

  it('passes a well-formed table.md-table', () => {
    expect(tableProblems(snap({ mdTables: [good] }), true)).toEqual([]);
  });

  it('catches raw Markdown, an unwrapped or malformed table, and a missing required table', () => {
    const s = snap({
      rawTableText: ['| --- | --- |'],
      mdTables: [{ ...good, wrapped: false }, { ...good, scopedHeaderCells: 1 }, { ...good, bodyRows: 0, rowCells: [] }, { ...good, rowCells: [3, 2] }],
    });
    const out = tableProblems(s).join('\n');
    expect(out).toMatch(/unrendered Markdown table text/);
    expect(out).toMatch(/not inside \.md-table-wrap/);
    expect(out).toMatch(/2 header cell\(s\) without scope="col"/);
    expect(out).toMatch(/no body rows/);
    expect(out).toMatch(/body row 2 has 2 cells, the header 3/);
    expect(tableProblems(snap(), true)).toEqual(['/p: its Markdown has a table, but no table.md-table rendered']);
  });
});

describe('recallProblems', () => {
  const recalled = [{ asin: 'B0GATE0007', path: '/product/B0GATE0007', name: 'Tailspin Spark 800' }];

  it('reads every ASIN out of every affiliate link shape', () => {
    expect(asinOfLink('https://www.amazon.com/dp/b0gate0007?tag=x')).toBe('B0GATE0007');
    expect(asinOfLink('https://www.amazon.com/gp/product/B0GATE0007/ref=x')).toBe('B0GATE0007');
    expect(asinOfLink('https://www.amazon.com/Some-Name/dp/B0GATE0007')).toBe('B0GATE0007');
    expect(asinOfLink('http://127.0.0.1:10032/r/B0GATE0007')).toBe('B0GATE0007');
    expect(asinOfLink('https://gate-fixture.test/r/B0GATE0007')).toBe('B0GATE0007');
    expect(asinOfLink('https://www.amazon.com/gp/aws/cart/add.html?ASIN.1=B0GATE0007')).toBe('B0GATE0007');
    expect(asinOfLink('https://www.amazon.com/s?k=widget')).toBe('');
    expect(asinsOfLink('https://www.amazon.com/gp/aws/cart/add.html?ASIN.1=B0GATE0001&Quantity.1=1&ASIN.2=B0GATE0007&tag=x')).toEqual(['B0GATE0001', 'B0GATE0007']);
    expect(asinsOfLink('https://www.amazon.com/dp/B0GATE0001?asin=B0GATE0002')).toEqual(['B0GATE0001', 'B0GATE0002']);
  });

  it('reads the words of an Amazon search link', () => {
    expect(amazonSearchKey('https://www.amazon.com/s?k=Tailspin+Spark+800&tag=x')).toBe(' tailspin spark 800 ');
    expect(amazonSearchKey('https://www.amazon.com/s/ref=nb?field-keywords=Tailspin%20Spark-800')).toBe(' tailspin spark 800 ');
    expect(amazonSearchKey('https://www.amazon.com/dp/B0GATE0007')).toBe('');
    expect(amazonSearchKey('https://example.com/s?k=Tailspin+Spark+800')).toBe('');
  });

  it('passes the recalled page itself: a notice, substitutes, no own button or Offer; and searches for other models', () => {
    const s = snap({
      path: '/product/B0GATE0007',
      recallNotices: 1,
      anchors: [
        anchor('https://www.amazon.com/dp/B0GATE0002?tag=gatefixture-20', { placement: 'pdp-substitute' }),
        anchor('https://www.amazon.com/s?k=Tailspin+Spark+8000&tag=gatefixture-20', { placement: 'pdp-search' }),
      ],
      jsonld: [JSON.stringify({ '@context': 'https://schema.org', '@type': 'Product', sku: 'B0GATE0007', name: 'X' })],
    });
    expect(recallProblems(s, recalled)).toEqual([]);
  });

  it('catches a buy button for the recalled SKU on any page, and a recalled page with no notice or with an Offer', () => {
    const elsewhere = snap({ path: '/guides/x', anchors: [anchor('https://www.amazon.com/dp/B0GATE0007?tag=gatefixture-20', { placement: 'in-article' })] });
    expect(recallProblems(elsewhere, recalled)).toEqual(['/guides/x: buy button for recalled B0GATE0007: https://www.amazon.com/dp/B0GATE0007?tag=gatefixture-20 (placement in-article)']);
    const own = snap({ path: '/product/B0GATE0007', jsonld: [JSON.stringify({ '@context': 'https://schema.org', '@type': 'Product', sku: 'B0GATE0007', offers: { '@type': 'Offer' } })] });
    const out = recallProblems(own, recalled).join('\n');
    expect(out).toMatch(/shows no recall notice/);
    expect(out).toMatch(/still has an Offer/);
  });

  it('catches the recalled SKU through our /r/ link on the canonical origin, a cart link and a model search (review round 1)', () => {
    const s = snap({
      path: '/guides/y',
      anchors: [
        anchor('https://gate-fixture.test/r/B0GATE0007', { placement: 'in-article', rel: 'sponsored nofollow noopener' }),
        anchor('https://www.amazon.com/gp/aws/cart/add.html?ASIN.1=B0GATE0001&ASIN.2=B0GATE0007&tag=gatefixture-20', { placement: '' }),
        anchor('https://www.amazon.com/s?k=Tailspin+Spark+800&tag=gatefixture-20', { placement: '' }),
      ],
    });
    expect(recallProblems(s, recalled)).toEqual([
      '/guides/y: buy button for recalled B0GATE0007: https://gate-fixture.test/r/B0GATE0007 (placement in-article)',
      '/guides/y: buy button for recalled B0GATE0007: https://www.amazon.com/gp/aws/cart/add.html?ASIN.1=B0GATE0001&ASIN.2=B0GATE0007&tag=gatefixture-20',
      '/guides/y: Amazon search for recalled B0GATE0007 ("Tailspin Spark 800"): https://www.amazon.com/s?k=Tailspin+Spark+800&tag=gatefixture-20',
    ]);
    // Without a model name only links naming the ASIN are caught.
    expect(recallProblems(s, [{ asin: 'B0GATE0007', path: '/product/B0GATE0007' }])).toHaveLength(2);
  });
});

describe('cacheControlProblems', () => {
  const priced = (cacheControl: string, over: Partial<ComplianceSnapshot> = {}) => snap({ cacheControl, stamps: [kitStamp(new Date(NOW - 3600_000))], ...over });

  it('parses directives', () => {
    expect(cacheDirectives('public, max-age=60, S-MaxAge="600"')).toEqual({ public: '', 'max-age': '60', 's-maxage': '600' });
  });

  it('passes the kit header, no-store, and pages without prices', () => {
    expect(cacheControlProblems(priced(htmlCacheControl(ctx)), rules)).toEqual([]);
    expect(cacheControlProblems(priced('no-store'), rules)).toEqual([]);
    expect(cacheControlProblems(snap({ cacheControl: 'public, max-age=86400' }), rules)).toEqual([]);
    expect(cacheControlProblems(priced(htmlCacheControl(ctx), { cdnCacheControl: 'max-age=600', surrogateControl: 'max-age=300' }), rules)).toEqual([]);
  });

  it('catches a long shared cache, a missing lifetime and stale-* on a page with prices', () => {
    expect(cacheControlProblems(priced('public, s-maxage=3600'), rules).join('\n')).toMatch(/may be cached 3600s by a shared cache \(at most 600s\)/);
    expect(cacheControlProblems(priced('public, max-age=86400'), rules).join('\n')).toMatch(/may be cached 86400s/);
    expect(cacheControlProblems(priced('public'), rules).join('\n')).toMatch(/sets no s-maxage or max-age/);
    expect(cacheControlProblems(priced('public, s-maxage=300, stale-while-revalidate=60, stale-if-error=600'), rules)).toHaveLength(2);
    expect(cacheControlProblems(priced('public, s-maxage=600'), { maxSharedCacheSeconds: 300 }).join('\n')).toMatch(/at most 300s/);
  });

  it('bounds the browser\'s max-age even beside a short s-maxage, and the CDN headers (review round 1)', () => {
    expect(cacheControlProblems(priced('public, max-age=86400, s-maxage=600'), rules)).toEqual([
      '/p: shows an Amazon price but a browser may keep it 86400s (max-age; at most 60s): "public, max-age=86400, s-maxage=600"',
    ]);
    expect(cacheControlProblems(priced('private, max-age=3600'), rules).join('\n')).toMatch(/a browser may keep it 3600s/);
    const cdn = cacheControlProblems(priced(htmlCacheControl(ctx), { cdnCacheControl: 'max-age=86400', surrogateControl: 'max-age=7200, stale-while-revalidate=30' }), rules);
    expect(cdn).toEqual([
      '/p: shows an Amazon price but CDN-Cache-Control lets a CDN keep it 86400s (at most 600s): "max-age=86400"',
      '/p: shows an Amazon price but Surrogate-Control lets a CDN keep it 7200s (at most 600s): "max-age=7200, stale-while-revalidate=30"',
      '/p: shows an Amazon price but its Surrogate-Control has stale-while-revalidate (a stale copy outlives the price window)',
    ]);
  });

  it('applies to Offer pages without a stamp too', () => {
    const offer = JSON.stringify({ '@context': 'https://schema.org', '@type': 'Product', offers: { '@type': 'Offer', price: '1.00' } });
    expect(cacheControlProblems(snap({ cacheControl: 'public, s-maxage=7200', jsonld: [offer] }), rules)).toHaveLength(1);
  });
});
