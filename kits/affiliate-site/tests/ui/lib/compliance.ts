/**
 * The Amazon-licence and policy rules kit-compliance.spec.ts enforces, as pure
 * functions over one page's ComplianceSnapshot (complianceCollect.ts takes it
 * in the browser). Pure so tests/unit can prove every rule both ways without
 * a server, and so a failure message names the page, the rule and the text.
 *
 * Rules (binding amendment 2 of the RuntimePicks design, the Associates
 * Program Policies and the Operating Agreement; wording checked by the kit's
 * notices.ts on 2026-11-17):
 *   affiliate links  rel has sponsored, nofollow and noopener (never noreferrer),
 *                    target=_blank, the marketplace host, one tag from the site's
 *                    own tracking IDs; /r/ links carry sponsored + nofollow
 *   disclosure (OA 5) a page with a visible affiliate link shows the site's
 *                    earnings disclosure in plain view, before the first link
 *   prices (2(i))    a price or availability statement shows only inside a
 *                    stamp dated within the quotable window (24h minus the HTML
 *                    cache lifetimes) that SHOWS the date and time of the check
 *                    with a time zone (matching its <time datetime>), carries a
 *                    visible "Details" control revealing the exact 2(i)
 *                    disclaimer, and holds one price; never in head metadata;
 *                    any price elsewhere fails, inside a sentence too: text a
 *                    reader sees, text only crawlers and assistants read
 *                    (hidden), aria-label / title / alt, and an Amazon price
 *                    in data-kit-not-a-price (meant for rates and budgets);
 *                    pages with prices are not cached past the window by a
 *                    browser, a shared cache or a CDN
 *   notice (2(k))    a page that shows Amazon content (a data-amazon-content
 *                    marker, a stamp, or a known Amazon title, whole or
 *                    truncated) shows the exact 2(k) notice in plain view
 *   Offer JSON-LD    validFrom within the quotable window, USD, a real price,
 *                    no seller / returns / shipping claims, a visible dated
 *                    price on the page; no ratings we did not measure
 *   6(y)             no price-alert or price-tracking UI, no "lowest ever",
 *                    "all-time low" or price-history copy (negation-aware: only
 *                    a negation that governs the phrase lets it through)
 *   images (2(h))    no Amazon CDN image as og:image, twitter:image, an article
 *                    hero, any JSON-LD image (Product included) or a sitemap
 *                    image
 *   tables           GFM tables render as table.md-table (no raw pipes)
 *   recalls          no buy button for a recalled SKU, anywhere: a /dp/ link, a
 *                    cart link naming it, our /r/ redirect, or an Amazon search
 *                    for its model
 */
import { isAmazonHostedUrl, isAmazonImageHost, isAmazonLinkHost } from '../../../server/src/render/amazonHosts';

/** The hard storage limit for Amazon prices and availability (IP License 2(h)). */
export const MAX_STAMP_AGE_MS = 24 * 3600 * 1000;
/** A check time this far in the future is clock skew, not an error (offer.ts FUTURE_SKEW_MS). */
export const FUTURE_SKEW_MS = 5 * 60 * 1000;
/**
 * Slack on the quotable window between the server's render and the gate's
 * read of the page (the snapshot's takenAt): the same host clock, a page read
 * the moment it was served.
 */
export const RENDER_ALLOWANCE_MS = 60 * 1000;
/** Known Amazon titles shorter than this are not matched in page text (too likely to be our own words). */
export const MIN_TITLE_MATCH_CHARS = 20;
/**
 * A known Amazon title longer than this is also matched by its opening this
 * many characters, cut back to a whole word: a title truncated with an
 * ellipsis is still Amazon's text.
 */
export const TITLE_PREFIX_CHARS = 40;

// ─── snapshot ──────────────────────────────────────────────────────────────

export interface ComplianceAnchor {
  /** The href attribute as written. */
  href: string;
  /** Resolved against the page ('' when it does not parse). */
  abs: string;
  rel: string;
  target: string;
  text: string;
  visible: boolean;
  /** The closest data-cta-placement ('' when none). */
  placement: string;
}

export interface ComplianceStamp {
  /** data-price-stamp value: 'price' or 'availability'. */
  kind: string;
  /** The stamp's whole text (textContent), whitespace-collapsed: for messages. */
  text: string;
  /** What a reader sees of it without opening anything (innerText): hidden parts are not in it. */
  visibleText: string;
  /** datetime of every <time> in it. */
  times: string[];
  /**
   * Text a control inside the stamp, that a reader can see, reveals: the
   * elements its popovertarget, aria-controls or href="#..." point at, a
   * visible aria-describedby target, and the body of a <details> whose
   * <summary> shows.
   */
  linkedTexts: string[];
  /**
   * Text revealed by the stamp's visible controls labelled "Details" (a
   * popover button, an aria-controls button, an href="#..." link, or a
   * <details> whose <summary> says Details): 2(i)'s disclosure, as Amazon's
   * own example writes it ("as of 01/07/2008 14:11 PST - Details").
   */
  detailsTexts: string[];
  /** The .ssr-price text inside it ('' for availability). */
  priceText: string;
  insideAnchor: boolean;
}

export interface ComplianceForm {
  /** The form's action, or '' for a control outside a form. */
  action: string;
  /** Visible text of the form (labels, buttons). */
  text: string;
  /** name / id / placeholder / aria-label of its fields and buttons. */
  fields: string[];
}

export interface ComplianceTable {
  /** Inside a .md-table-wrap. */
  wrapped: boolean;
  headerCells: number;
  /** Header cells with scope="col". */
  scopedHeaderCells: number;
  bodyRows: number;
  /** Cell count of every body row. */
  rowCells: number[];
}

export interface ComplianceSnapshot {
  /** Site path the snapshot was taken at. */
  path: string;
  status: number;
  /** When the page was read (ms since the epoch); the age rules judge against it (default: the rules' now). */
  takenAt?: number;
  /** title, meta description, og:title, og:description, og:image:alt, twitter:title / description / image:alt. */
  headTexts: Array<{ where: string; text: string }>;
  /** product:price:*, product:sale_price:*, product:original_price:* and og:price:* meta tags (a price with no stamp). */
  priceMetas: Array<{ prop: string; content: string }>;
  /** Amazon and /r/<ASIN> anchors (a /r/ link on any of the site's hosts), in document order. */
  anchors: ComplianceAnchor[];
  stamps: ComplianceStamp[];
  /**
   * What a reader can see outside every stamp and data-kit-not-a-price
   * element: one entry per block (paragraph, list item, cell, heading ...),
   * whitespace-collapsed. The price and availability rules read it.
   */
  unstampedText: string[];
  /**
   * Text outside every stamp that a reader does not see (display:none,
   * hidden, a closed popover) but that crawlers and assistants read from the
   * HTML; one entry per block, as unstampedText.
   */
  hiddenUnstampedText: string[];
  /** aria-label, title and alt values of elements outside every stamp and data-kit-not-a-price element. */
  attributeTexts: Array<{ where: string; text: string }>;
  /** What a reader sees inside data-kit-not-a-price elements (rates and budgets, never an Amazon price). */
  notAPriceText: string[];
  /** .ssr-price elements outside any stamp (their text), data-kit-not-a-price ones included. */
  strayPriceElements: string[];
  /** Elements marked data-amazon-notice="2k". */
  noticeElements: Array<{ text: string; visible: boolean }>;
  /** What a reader sees (body innerText): spaces collapsed, one line per block. */
  visibleText: string;
  /**
   * Whether the earnings disclosure first shows before the first visible
   * affiliate link (document order of what a reader sees); null when the page
   * has no visible affiliate link or does not show the disclosure at all.
   */
  earningsBeforeFirstLink: boolean | null;
  /** Count of [data-amazon-content] elements. */
  amazonMarkers: number;
  jsonld: string[];
  /** og:image, og:image:url, og:image:secure_url, twitter:image. */
  socialImages: Array<{ prop: string; url: string }>;
  /** img[data-hero-image] sources. */
  heroImages: string[];
  forms: ComplianceForm[];
  mdTables: ComplianceTable[];
  /** Visible text that is a GFM table row or delimiter row nobody rendered. */
  rawTableText: string[];
  /** Visible elements matching the configured recall-notice selector. */
  recallNotices: number;
  /** The page response's Cache-Control header ('' when none). */
  cacheControl: string;
  /** CDN-Cache-Control ('' when none): Cloudflare and other CDNs obey it over Cache-Control. */
  cdnCacheControl: string;
  /** Surrogate-Control ('' when none): the same, for Fastly-style CDNs. */
  surrogateControl: string;
}

/** What the rules need to know about the site. */
export interface ComplianceRules {
  /** The site's own tracking IDs (ctx.amazon.allowedTags). */
  allowedTags: readonly string[];
  /** ctx.amazon.marketplaceHost, e.g. www.amazon.com. */
  marketplaceHost: string;
  /** The exact 2(i) disclaimer for the site's Amazon Site. */
  priceDisclaimer: string;
  /** The exact 2(k) notice. */
  amazonNotice: string;
  /** site.json disclosure.earnings (Operating Agreement section 5). */
  earningsDisclosure: string;
  /**
   * The longest a shared cache may keep a page that shows an Amazon price
   * (site.json cache.htmlSMaxAgeSeconds, at most MAX_SHARED_CACHE_SECONDS).
   */
  maxSharedCacheSeconds: number;
  /** The longest a browser may keep it (site.json cache.htmlMaxAgeSeconds); unset: not checked on its own. */
  maxBrowserCacheSeconds?: number;
  /**
   * The oldest a shown price or availability check may be when the gate reads
   * the page (priceAgeLimitMs: the site's quotable window); unset: 24h.
   */
  quotableWindowMs?: number;
  now: number;
}

/** Binding amendment 2: s-maxage at most 600 on SSR HTML, so the price window holds through any CDN. */
export const MAX_SHARED_CACHE_SECONDS = 600;

/**
 * The oldest a shown Amazon price may be when the gate reads the page: the
 * site's quotable window (prices.quotableMaxAgeSeconds: the freshness limit
 * minus every HTML cache lifetime), plus the SSR cache the local server may
 * have served the page from, plus RENDER_ALLOWANCE_MS; never over 24h.
 */
export function priceAgeLimitMs(ctx: {
  prices: { quotableMaxAgeSeconds: number };
  cache: { ssrCacheSeconds: number };
}): number {
  const ms = (ctx.prices.quotableMaxAgeSeconds + ctx.cache.ssrCacheSeconds) * 1000 + RENDER_ALLOWANCE_MS;
  return Math.min(ms, MAX_STAMP_AGE_MS);
}

// ─── shared helpers ────────────────────────────────────────────────────────

export function collapse(s: string | null | undefined): string {
  return String(s ?? '').replace(/\s+/g, ' ').trim();
}

/** A price with cents ($349.99, $1,299.00): what a product price display looks like. */
export const PRICE_WITH_CENTS_RE = /(?<![\w$])\$\s?(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2}(?![\d])/;
/** Any amount said to be the price on Amazon ("$349 on Amazon", "$1,299.99 at Amazon"). */
export const PRICE_AT_AMAZON_RE = /(?<![\w$])\$\s?\d[\d,]*(?:\.\d{1,2})?\s*(?:on|at|from|via)\s+Amazon\b/i;
/** An amount with cents written with a currency code ("USD 349.99", "349.99 USD", "US$349.99"). */
export const PRICE_CURRENCY_CODE_RE = /(?<![\w$])(?:USD|US\$)\s?(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2}(?!\d)|(?<![\w$.,])(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2}\s?USD\b/i;
/**
 * An amount given a check time, the shape of a hand-made stamp in a sentence
 * ("the cheapest is X, $349 (as of Nov 17)", "$349 on Amazon.com as of 14:11
 * UTC", "$349 (checked Nov 17)"): a quoted price, whatever its cents or cue
 * words, unless a budget word comes right before it (datedPrices).
 */
export const DATED_PRICE_RE = /(?<![\w$])\$\s?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?\s*\(?\s*(?:(?:on|at)\s+Amazon(?:\.com)?\s*)?(?:as\s+of|checked)\b/i;

/** A whole-dollar amount ("$449", "$1,099"): no cents, not a "$1k" shorthand. */
const WHOLE_DOLLAR_RE = /(?<![\w$])\$\s?(?:\d{1,3}(?:,\d{3})+|\d+)(?![\d,.]?\d)(?![kKmM]\b)/g;
/** Words that make a whole-dollar amount a quoted price when they come shortly before it. */
const PRICE_CUE_RE = /^(?:at|cost|costs|costing|priced|sell|sells|selling|sold|just|only|now|from|total|totals|totaled|totalled|totaling|totalling|retail|retails|retailing|deal|deals|pay|paying|paid|discounted|reduced|list|lists|listed)$/i;
/** How many words before "$N" a price cue may be ("sells for $449", "Total for this kit: $1,249"). */
export const PRICE_CUE_WINDOW_WORDS = 4;
/** Right before "$N": a budget, an approximation or the second end of a range, not a price. */
const BUDGET_BEFORE_RE = /(?:^|[\s(])(?:under|below|over|above|around|about|approx(?:imately|\.)?|roughly|nearly|almost|up\s+to|less\s+than|more\s+than|no\s+more\s+than|within|max(?:imum)?|budget(?:\s+of)?|between|sub|than|spend(?:ing)?|capped\s+at|tier|~)\s*$|\$\s?[\d,]+\s*(?:to|-|–|and)\s*$/i;
/** Right after "$N": a budget, a range, a rate ("a year", "per kWh"), not a product price. */
const BUDGET_AFTER_RE = /^\s*(?:\+|[-–]\s*\$|to\s+\$|budgets?\b|tiers?\b|range\b|price\s+range\b|price\s+point\b|or\s+(?:less|under|below|so|more)\b|and\s+(?:under|below|up|over)\b|max\b|cap\b|ceiling\b|limit\b|class\b|bracket\b|(?:a|an|per|each|every)\s+(?:year|month|week|day|night|hour|kwh|kw|unit|person|charge|cycle|refill)\b|\/\s*(?:yr|year|mo|month|wk|week|day|hr|hour|kwh)\b)/i;

/** Where the sentence holding position `at` starts: after the last terminator, or a line break (a block boundary). */
function sentenceStart(text: string, at: number): number {
  for (let i = at - 1; i >= 0; i--) {
    const ch = text[i];
    if (ch === '\n' || ch === '!' || ch === '?') return i + 1;
    // A period ends a sentence when a space or the end follows it ("$349.99" does not).
    if (ch === '.' && (i + 1 >= text.length || /\s/.test(text[i + 1] as string))) return i + 1;
  }
  return 0;
}

/** The words of `s`, punctuation trimmed from each ("$9.99" -> "9.99", "kit:" -> "kit"). */
function wordsOf(s: string): string[] {
  return s.split(/\s+/).map((w) => w.replace(/^[^\w']+|[^\w']+$/g, '')).filter(Boolean);
}

/**
 * Whole-dollar amounts the text quotes as a price: "$N" with a price cue
 * ("sells for", "now just", "total", "from") among the few words before it,
 * and no budget, range or rate around it ("under $500", "$200 to $500",
 * "$40 a year").
 */
export function wholeDollarPrices(text: string): string[] {
  const s = String(text ?? '');
  const out: string[] = [];
  for (const m of s.matchAll(WHOLE_DOLLAR_RE)) {
    const at = m.index ?? 0;
    const before = s.slice(sentenceStart(s, at), at);
    const after = s.slice(at + m[0].length, at + m[0].length + 40);
    if (BUDGET_BEFORE_RE.test(before) || BUDGET_AFTER_RE.test(after)) continue;
    if (wordsOf(before).slice(-PRICE_CUE_WINDOW_WORDS).some((w) => PRICE_CUE_RE.test(w))) out.push(m[0]);
  }
  return out;
}

/**
 * Amounts the text gives a check time ("$349 (as of Nov 17)"), except a
 * budget ("Best under $500 as of November"): DATED_PRICE_RE with the same
 * budget words wholeDollarPrices skips.
 */
export function datedPrices(text: string): string[] {
  const s = String(text ?? '');
  const out: string[] = [];
  for (const m of s.matchAll(new RegExp(DATED_PRICE_RE.source, 'gi'))) {
    const at = m.index ?? 0;
    if (BUDGET_BEFORE_RE.test(s.slice(sentenceStart(s, at), at))) continue;
    out.push(m[0]);
  }
  return out;
}

/** True when the text holds something that reads as a quoted product price. */
export function looksLikePrice(text: string): boolean {
  return PRICE_WITH_CENTS_RE.test(text) || PRICE_AT_AMAZON_RE.test(text) || PRICE_CURRENCY_CODE_RE.test(text)
    || datedPrices(text).length > 0 || wholeDollarPrices(text).length > 0;
}

/** The amount of a price mention ('$1,299.00 on Amazon' -> '1299.00'). */
function amountOf(mention: string): string {
  return (/\d[\d,]*(?:\.\d+)?/.exec(mention)?.[0] ?? mention).replace(/,/g, '');
}

/**
 * The distinct amounts `text` quotes as prices (every price pattern, cents or
 * not), in order: one stamp may hold exactly one.
 */
export function priceAmounts(text: string): string[] {
  const s = String(text ?? '');
  const found: Array<{ at: number; amount: string }> = [];
  for (const re of [PRICE_WITH_CENTS_RE, PRICE_AT_AMAZON_RE, PRICE_CURRENCY_CODE_RE]) {
    for (const m of s.matchAll(new RegExp(re.source, `${re.flags.replace('g', '')}g`))) found.push({ at: m.index ?? 0, amount: amountOf(m[0]) });
  }
  for (const list of [datedPrices(s), wholeDollarPrices(s)]) {
    let from = 0;
    for (const w of list) {
      const at = s.indexOf(w, from);
      from = at < 0 ? from : at + w.length;
      found.push({ at: at < 0 ? s.length : at, amount: amountOf(w) });
    }
  }
  const out: string[] = [];
  for (const f of found.sort((a, b) => a.at - b.at)) if (!out.includes(f.amount)) out.push(f.amount);
  return out;
}

// ─── the 2(i) date and time a stamp shows ──────────────────────────────────

const MONTHS_SRC = 'jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?';
/** A date a reader can read: 2026-11-17, 11/17/2026, Nov 17[, 2026], 17 Nov [2026]. */
const DATE_SRC = `(?:\\d{4}-\\d{2}-\\d{2}|\\d{1,2}/\\d{1,2}/\\d{2,4}|(?:${MONTHS_SRC})\\.?\\s+\\d{1,2}(?:st|nd|rd|th)?(?:,?\\s+\\d{4})?|\\d{1,2}\\s+(?:${MONTHS_SRC})\\.?(?:,?\\s+\\d{4})?)`;
/** A clock time: 14:11, 2:11 PM, 14:11:05. */
const TIME_SRC = '(?<![\\d:])(?:[01]?\\d|2[0-3]):[0-5]\\d(?::[0-5]\\d)?(?:\\s?[ap]\\.?m\\.?)?';
/** A time zone right after it: UTC or GMT (with an offset or not), Z, a numeric offset, a US zone abbreviation. */
const ZONE_SRC = '\\s?\\(?(?:(?:UTC|GMT)(?:\\s?[+-]\\d{1,2}(?::?\\d{2})?)?|Z|[+-]\\d{2}:?\\d{2}|[ECMP][SD]?T|AK[SD]T|H[SD]T)(?![A-Za-z0-9])\\)?';
/**
 * A check time as a reader sees it: a date, then (after a comma, "at", "@"
 * or ISO's "T") a time with its zone, or the time with its zone, then "on"
 * the date. "2026-11-17 14:11 UTC" (the kit's), "01/07/2008 14:11 PST"
 * (Amazon's example), "Nov 17, 2026, 9:11 AM EST", "14:11 UTC on 2026-11-17".
 */
export const SHOWN_CHECK_TIME_RE = new RegExp(
  `${DATE_SRC}(?:,?\\s+(?:at\\s+|@\\s*)?|T)${TIME_SRC}${ZONE_SRC}|${TIME_SRC}${ZONE_SRC},?\\s+(?:on\\s+)?${DATE_SRC}`,
  'i',
);

const MONTH_INDEX: Readonly<Record<string, number>> = Object.freeze({
  jan: 0, feb: 1, mar: 2, apr: 3, may: 4, jun: 5, jul: 6, aug: 7, sep: 8, oct: 9, nov: 10, dec: 11,
});
/** Fixed offsets (minutes) of the zones a check time may name; the generic ET/CT/MT/PT are not fixed. */
const ZONE_OFFSET_MIN: Readonly<Record<string, number>> = Object.freeze({
  UTC: 0, GMT: 0, Z: 0, EST: -300, EDT: -240, CST: -360, CDT: -300, MST: -420, MDT: -360, PST: -480, PDT: -420,
  AKST: -540, AKDT: -480, HST: -600, HDT: -540,
});

/**
 * The instant a shown check time names (ms), or null when it cannot be told
 * exactly: no year, a two-digit year, a generic zone (ET, PT), a day that
 * does not exist. Minute precision unless it shows seconds.
 */
export function shownCheckTimeMs(shown: string): number | null {
  const t = String(shown ?? '');
  let y: number; let mo: number; let d: number;
  let m = /(\d{4})-(\d{2})-(\d{2})/.exec(t);
  if (m) { y = +(m[1] as string); mo = +(m[2] as string) - 1; d = +(m[3] as string); }
  else if ((m = /(\d{1,2})\/(\d{1,2})\/(\d{4})/.exec(t))) { mo = +(m[1] as string) - 1; d = +(m[2] as string); y = +(m[3] as string); }
  else if ((m = new RegExp(`(${MONTHS_SRC})\\.?\\s+(\\d{1,2})(?:st|nd|rd|th)?,?\\s+(\\d{4})`, 'i').exec(t))) {
    mo = MONTH_INDEX[(m[1] as string).slice(0, 3).toLowerCase()] as number; d = +(m[2] as string); y = +(m[3] as string);
  } else if ((m = new RegExp(`(\\d{1,2})\\s+(${MONTHS_SRC})\\.?,?\\s+(\\d{4})`, 'i').exec(t))) {
    d = +(m[1] as string); mo = MONTH_INDEX[(m[2] as string).slice(0, 3).toLowerCase()] as number; y = +(m[3] as string);
  } else return null;
  const tm = /(?<![\d:])(\d{1,2}):(\d{2})(?::(\d{2}))?(?:\s?([ap])\.?m\.?)?/i.exec(t);
  if (!tm) return null;
  let h = +(tm[1] as string);
  const ap = (tm[4] ?? '').toLowerCase();
  if (ap) {
    if (h < 1 || h > 12) return null;
    h = (h % 12) + (ap === 'p' ? 12 : 0);
  }
  const rest = t.slice((tm.index ?? 0) + tm[0].length);
  const z = /^\s?\(?(?:(UTC|GMT)\s?([+-])(\d{1,2})(?::?(\d{2}))?|(UTC|GMT|Z)(?![A-Za-z0-9])|([+-])(\d{2}):?(\d{2})|(EST|EDT|CST|CDT|MST|MDT|PST|PDT|AKST|AKDT|HST|HDT)(?![A-Za-z0-9]))/i.exec(rest);
  if (!z) return null;
  let offset: number;
  if (z[1]) offset = (z[2] === '-' ? -1 : 1) * (+(z[3] as string) * 60 + +(z[4] ?? '0'));
  else if (z[5]) offset = 0;
  else if (z[6]) offset = (z[6] === '-' ? -1 : 1) * (+(z[7] as string) * 60 + +(z[8] as string));
  else offset = ZONE_OFFSET_MIN[(z[9] as string).toUpperCase()] as number;
  const local = Date.UTC(y, mo, d, h, +(tm[2] as string), +(tm[3] ?? '0'));
  const back = new Date(local);
  if (back.getUTCFullYear() !== y || back.getUTCMonth() !== mo || back.getUTCDate() !== d) return null;
  return local - offset * 60_000;
}

/** Every check time `text` shows (date, time and zone), as written. */
export function shownCheckTimes(text: string): string[] {
  const re = new RegExp(SHOWN_CHECK_TIME_RE.source, 'gi');
  return Array.from(String(text ?? '').matchAll(re), (m) => collapse(m[0]));
}

/** A "Details" control's label: the word alone, punctuation around it allowed ("Details", "(Details)", "Details ›"). */
export const DETAILS_LABEL_RE = /^[^\p{L}\p{N}]*details[^\p{L}\p{N}]*$/iu;

const ASIN_IN_PATH_RE = /\/(?:dp|gp\/product|gp\/aw\/d|exec\/obidos\/ASIN)\/([A-Z0-9]{10})(?=[/?#]|$)/i;
const REDIRECT_PATH_RE = /^\/r\/([A-Z0-9]{10})\/?$/i;
/** Query parameters that name ASINs: ?asin=, ?ASIN=, cart links' ASIN.1=, ASIN.2=, ... */
const ASIN_PARAM_RE = /^asins?(?:\.\d+)?$/i;

/**
 * Every ASIN an affiliate link names, upper-cased, in order: the /dp/ or
 * /gp/product/ path, our /r/<ASIN>, and every asin / ASIN.N parameter (a cart
 * link adds several at once).
 */
export function asinsOfLink(abs: string): string[] {
  let u: URL;
  try { u = new URL(abs); } catch { return []; }
  const out = new Set<string>();
  const m = u.pathname.match(ASIN_IN_PATH_RE) || u.pathname.match(REDIRECT_PATH_RE);
  if (m) out.add((m[1] as string).toUpperCase());
  for (const [k, v] of u.searchParams) {
    if (!ASIN_PARAM_RE.test(k)) continue;
    for (const part of v.split(/[\s,]+/)) if (/^[A-Z0-9]{10}$/i.test(part)) out.add(part.toUpperCase());
  }
  return [...out];
}

/** The first ASIN an affiliate link names ('' when none). */
export function asinOfLink(abs: string): string {
  return asinsOfLink(abs)[0] ?? '';
}

/** Lower-case words of `s` with a space at each end, so a containment test matches whole words only. */
function wordKey(s: string): string {
  return ` ${String(s ?? '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim()} `;
}

/** The search words of an Amazon search link (/s?k=, field-keywords=, keywords=) as a word key; '' for any other link. */
export function amazonSearchKey(abs: string): string {
  let u: URL;
  try { u = new URL(abs); } catch { return ''; }
  if (!isAmazonLinkHost(u.hostname)) return '';
  const q = u.searchParams.get('k') ?? u.searchParams.get('field-keywords') ?? u.searchParams.get('keywords');
  return q && q.trim() ? wordKey(q) : '';
}

function isRedirectLink(abs: string): boolean {
  try { return REDIRECT_PATH_RE.test(new URL(abs).pathname); } catch { return false; }
}

function where(snap: Pick<ComplianceSnapshot, 'path'>): string {
  return snap.path;
}

function hours(ms: number): string {
  return (ms / 3600000).toFixed(1);
}

/**
 * Why a check time `at` is too old or too new to show at `now` (null when it
 * may show): past 24h it is past the storage limit; past `windowMs` it is
 * past the site's quotable window.
 */
function checkTimeProblem(at: number, now: number, windowMs: number | undefined): string | null {
  const age = now - at;
  const window = Math.min(windowMs ?? MAX_STAMP_AGE_MS, MAX_STAMP_AGE_MS);
  if (age > MAX_STAMP_AGE_MS) return `${hours(age)}h old (older than 24h)`;
  if (age > window) return `${hours(age)}h old (past the ${hours(window)}h quotable window: the price freshness limit minus the HTML cache lifetimes)`;
  if (age < -FUTURE_SKEW_MS) return 'in the future';
  return null;
}

// ─── affiliate links ───────────────────────────────────────────────────────

/** rel, target, host and tag of every affiliate link. */
export function affiliateLinkProblems(snap: ComplianceSnapshot, rules: Pick<ComplianceRules, 'allowedTags' | 'marketplaceHost'>): string[] {
  const out: string[] = [];
  const allowed = new Set(rules.allowedTags);
  const market = rules.marketplaceHost.toLowerCase().replace(/^www\./, '');
  for (const a of snap.anchors) {
    const label = `${where(snap)} link ${a.href.slice(0, 120)}`;
    const rel = new Set(a.rel.toLowerCase().split(/\s+/).filter(Boolean));
    if (isRedirectLink(a.abs)) {
      const missing = ['sponsored', 'nofollow'].filter((r) => !rel.has(r));
      if (missing.length) out.push(`${label}: rel="${a.rel}" lacks ${missing.join(', ')}`);
      continue;
    }
    let u: URL;
    try {
      u = new URL(a.abs);
    } catch {
      out.push(`${label}: unparseable Amazon URL`);
      continue;
    }
    const host = u.hostname.toLowerCase().replace(/^www\./, '');
    if (host !== market) {
      out.push(`${label}: ${u.hostname} cannot carry the site's tag (only ${rules.marketplaceHost} links are allowed; short links and other storefronts are not)`);
      continue;
    }
    if (u.protocol !== 'https:') out.push(`${label}: not https`);
    const missing = ['sponsored', 'nofollow', 'noopener'].filter((r) => !rel.has(r));
    if (missing.length) out.push(`${label}: rel="${a.rel}" lacks ${missing.join(', ')}`);
    if (rel.has('noreferrer')) out.push(`${label}: rel has noreferrer (it drops the Referer the click attribution reads)`);
    if (a.target !== '_blank') out.push(`${label}: target="${a.target}", expected _blank`);
    const tags = [...u.searchParams.entries()].filter(([k]) => k.toLowerCase() === 'tag').map(([, v]) => v);
    if (!tags.length) out.push(`${label}: no tag= (an untagged Amazon link earns nothing)`);
    else if (tags.length > 1) out.push(`${label}: ${tags.length} tag= parameters`);
    for (const t of tags) if (!allowed.has(t)) out.push(`${label}: tag "${t}" is not one of the site's tracking IDs`);
    const assoc = u.searchParams.get('AssociateTag');
    if (assoc !== null && !allowed.has(assoc)) out.push(`${label}: AssociateTag "${assoc}" is not one of the site's tracking IDs`);
  }
  return out;
}

// ─── the earnings disclosure: Operating Agreement section 5 ────────────────

/**
 * A page a reader can follow an affiliate link from shows the site's
 * earnings disclosure in plain view, and before the first such link (the
 * layout's top disclosure; FTC: clear and conspicuous, before the link).
 * Compared case-insensitively (CSS may change its case, never its words).
 */
export function earningsDisclosureProblems(snap: ComplianceSnapshot, rules: Pick<ComplianceRules, 'earningsDisclosure'>): string[] {
  const links = snap.anchors.filter((a) => a.visible);
  if (!links.length) return [];
  const want = collapse(rules.earningsDisclosure);
  const shown = !!want && collapse(snap.visibleText).toLowerCase().includes(want.toLowerCase());
  if (!shown) {
    return [`${where(snap)}: ${links.length} visible affiliate link(s) but the earnings disclosure ("${want.slice(0, 60)}...") is not in plain view (Operating Agreement section 5)`];
  }
  if (snap.earningsBeforeFirstLink === false) {
    return [`${where(snap)}: the earnings disclosure first shows after the first affiliate link (${(links[0] as ComplianceAnchor).href.slice(0, 100)}); it must come before it`];
  }
  return [];
}

// ─── prices and availability: IP License 2(i) ──────────────────────────────

/**
 * Every stamp dated within the quotable window and carrying the exact 2(i)
 * disclaimer (in view, or behind a control inside the stamp); no price
 * outside a stamp, in head metadata or in a price meta tag.
 */
export function priceDisplayProblems(snap: ComplianceSnapshot, rules: Pick<ComplianceRules, 'priceDisclaimer' | 'now' | 'quotableWindowMs'>): string[] {
  const out: string[] = [];
  const disclaimer = collapse(rules.priceDisclaimer);
  const now = snap.takenAt ?? rules.now;
  snap.stamps.forEach((s, i) => {
    const label = `${where(snap)} ${s.kind || 'unknown'} stamp ${i + 1} ("${s.text.slice(0, 70)}")`;
    if (s.kind !== 'price' && s.kind !== 'availability') out.push(`${label}: data-price-stamp="${s.kind}" is neither price nor availability`);
    const times = s.times.map((t) => Date.parse(t)).filter((t) => Number.isFinite(t));
    if (!times.length) out.push(`${label}: no <time datetime> (2(i) needs the date and time it was checked)`);
    for (const t of times) {
      const why = checkTimeProblem(t, now, rules.quotableWindowMs);
      if (why) out.push(`${label}: checked ${new Date(t).toISOString()}, ${why === 'in the future' ? why : why.replace(/ old /, ' ago ')}`);
    }
    const hasDisclaimer = collapse(s.visibleText).includes(disclaimer) || s.linkedTexts.some((t) => collapse(t).includes(disclaimer));
    if (!hasDisclaimer) {
      const hidden = collapse(s.text).includes(disclaimer);
      out.push(`${label}: without the exact 2(i) disclaimer ("${disclaimer.slice(0, 60)}...")${hidden ? ' in view or behind a control inside the stamp (it is in the stamp but hidden)' : ''}`);
    }
    if (s.insideAnchor) out.push(`${label}: inside a link (its Details control cannot work there)`);
    if (s.kind === 'price' && !PRICE_WITH_CENTS_RE.test(s.priceText)) out.push(`${label}: price stamp without a $X.XX price ("${s.priceText}")`);
    // 2(i): the date and time a reader sees, with its zone (a <time datetime> attribute alone is not shown).
    const shown = shownCheckTimes(s.visibleText);
    if (!shown.length) {
      out.push(`${label}: does not show the date and time of the check with a time zone (2(i): "as of <date> <time> <zone>", e.g. 2026-11-17 14:11 UTC; a <time datetime> attribute is not shown to readers)`);
    } else {
      for (const text of shown) {
        const at = shownCheckTimeMs(text);
        if (at === null || !times.length) continue;
        const withSeconds = /\d:\d{2}:\d{2}/.test(text);
        const unit = withSeconds ? 1000 : 60_000;
        if (!times.some((t) => Math.floor(t / unit) === Math.floor(at / unit))) {
          out.push(`${label}: shows "${text}" but its <time datetime> says ${times.map((t) => new Date(t).toISOString()).join(', ')}`);
        }
      }
    }
    // 2(i)'s disclosure behind a visible "Details" control, as the kit's stamp and Amazon's example do.
    if (!s.detailsTexts.some((t) => collapse(t).includes(disclaimer))) {
      out.push(`${label}: no visible "Details" control that reveals the exact 2(i) disclaimer`);
    }
    // One price per stamp: a second amount rides along without its own check time.
    const amounts = priceAmounts(s.visibleText);
    if (s.kind === 'price' && amounts.length > 1) out.push(`${label}: holds ${amounts.length} prices ($${amounts.join(', $')}): each price needs its own stamp`);
    if (s.kind === 'availability' && amounts.length) out.push(`${label}: an availability stamp holds a price ($${amounts.join(', $')}): a price needs its own price stamp`);
  });
  let orphans = 0;
  for (const t of snap.unstampedText) {
    if (orphans >= 20) break;
    if (looksLikePrice(t)) {
      orphans++;
      out.push(`${where(snap)}: price outside a dated stamp: "${t.slice(0, 120)}"`);
    }
  }
  for (const t of snap.hiddenUnstampedText) {
    if (orphans >= 20) break;
    if (looksLikePrice(t)) {
      orphans++;
      out.push(`${where(snap)}: price outside a dated stamp, in text a reader does not see but crawlers and assistants read: "${t.slice(0, 120)}"`);
    }
  }
  for (const a of snap.attributeTexts) {
    if (orphans >= 20) break;
    if (looksLikePrice(a.text)) {
      orphans++;
      out.push(`${where(snap)}: price outside a dated stamp, in ${a.where}: "${a.text.slice(0, 120)}"`);
    }
  }
  for (const t of snap.notAPriceText) {
    if (orphans >= 20) break;
    if (PRICE_AT_AMAZON_RE.test(t) || datedPrices(t).length) {
      orphans++;
      out.push(`${where(snap)}: an Amazon price inside data-kit-not-a-price (that marker is for rates and budgets): "${t.slice(0, 120)}"`);
    }
  }
  for (const p of snap.strayPriceElements.slice(0, 20)) out.push(`${where(snap)}: price outside a dated stamp: "${p.slice(0, 60)}" (a .ssr-price outside a stamp)`);
  for (const h of snap.headTexts) {
    if (looksLikePrice(h.text)) out.push(`${where(snap)}: price in ${h.where} (head metadata cannot carry the 2(i) time and disclaimer): "${h.text.slice(0, 120)}"`);
  }
  for (const m of snap.priceMetas) {
    out.push(`${where(snap)}: price meta tag ${m.prop}="${m.content.slice(0, 40)}" (a price with no time or disclaimer)`);
  }
  return out;
}

/** Availability copy: what Amazon has, or will have, in stock. */
export const AVAILABILITY_TEXT: ReadonlyArray<readonly [string, RegExp]> = Object.freeze([
  ['stock status', /\b(?:in|out\s+of|back\s+in)[ -]stock\b/i],
  ['sold out', /\bsold[ -]out\b/i],
  ['currently unavailable', /\b(?:currently|temporarily)\s+(?:unavailable|not\s+available|out\s+of\s+stock|sold\s+out)\b/i],
  ['availability on Amazon', /\b(?:unavailable|available|not\s+available)\s+(?:on|at|from|via)\s+amazon\b/i],
  ['units left', /\bonly\s+\d+\s+(?:left|remaining)\b/i],
  ['shipping estimate', /\b(?:usually\s+)?ships\s+(?:in|within)\s+\d/i],
  ['pre-order', /\bavailable\s+(?:for|to)\s+pre-?order\b/i],
] as Array<readonly [string, RegExp]>);

/**
 * Availability is held to the same rule as a price (2(i)): stated only inside
 * a dated stamp. Copy a reader sees outside every stamp, or head metadata,
 * that says what Amazon has in stock fails. Not negation-aware: "not in stock"
 * and "no longer in stock" are availability statements too.
 */
export function availabilityTextProblems(snap: ComplianceSnapshot): string[] {
  const out: string[] = [];
  for (const t of snap.unstampedText) {
    for (const [name, re] of AVAILABILITY_TEXT) {
      for (const sentence of sentencesMatching(t, re, false)) out.push(`${where(snap)}: availability outside a dated stamp (${name}): "${sentence}"`);
    }
    if (out.length >= 20) break;
  }
  for (const h of snap.headTexts) {
    for (const [name, re] of AVAILABILITY_TEXT) {
      if (re.test(h.text)) out.push(`${where(snap)}: availability in ${h.where} (${name}; head metadata cannot carry the 2(i) time and disclaimer): "${h.text.slice(0, 120)}"`);
    }
  }
  return [...new Set(out)];
}

/** Cache-Control directives, lower-cased: { 's-maxage': '600', public: '' }. */
export function cacheDirectives(header: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const part of String(header ?? '').split(',')) {
    const [k, v] = part.split('=');
    const key = (k ?? '').trim().toLowerCase();
    if (key) out[key] = (v ?? '').trim().replace(/^"|"$/g, '');
  }
  return out;
}

const isWholeSeconds = (v: string): boolean => /^\d+$/.test(v);

/**
 * A page that shows an Amazon price or availability (a stamp or an Offer) may
 * be kept no longer than the price window allows: a shared cache for at most
 * s-maxage (max-age when there is none) <= maxSharedCacheSeconds, a browser
 * for at most max-age <= maxBrowserCacheSeconds, a CDN obeying
 * CDN-Cache-Control or Surrogate-Control no longer than the shared limit, and
 * never with stale-while-revalidate / stale-if-error (they stretch the age
 * unboundedly).
 */
export function cacheControlProblems(snap: ComplianceSnapshot, rules: Pick<ComplianceRules, 'maxSharedCacheSeconds' | 'maxBrowserCacheSeconds'>): string[] {
  const showsPrice = snap.stamps.length > 0 || jsonLdNodes(snap.jsonld).some((n) => typesOf(n).some((t) => t === 'Offer' || t === 'AggregateOffer'));
  if (!showsPrice) return [];
  const out: string[] = [];
  const limit = Math.min(rules.maxSharedCacheSeconds, MAX_SHARED_CACHE_SECONDS);
  const d = cacheDirectives(snap.cacheControl);
  if (!('no-store' in d)) {
    if (!('private' in d)) {
      const shared = d['s-maxage'] ?? d['max-age'];
      if (shared === undefined) out.push(`${where(snap)}: shows an Amazon price but its Cache-Control ("${snap.cacheControl}") sets no s-maxage or max-age`);
      else if (!isWholeSeconds(shared) || Number(shared) > limit) out.push(`${where(snap)}: shows an Amazon price but may be cached ${shared}s by a shared cache (at most ${limit}s): "${snap.cacheControl}"`);
    }
    const browser = d['max-age'];
    if (rules.maxBrowserCacheSeconds !== undefined && browser !== undefined && (!isWholeSeconds(browser) || Number(browser) > rules.maxBrowserCacheSeconds)) {
      out.push(`${where(snap)}: shows an Amazon price but a browser may keep it ${browser}s (max-age; at most ${rules.maxBrowserCacheSeconds}s): "${snap.cacheControl}"`);
    }
    for (const k of ['stale-while-revalidate', 'stale-if-error']) {
      if (k in d) out.push(`${where(snap)}: shows an Amazon price but its Cache-Control has ${k} (a stale copy outlives the price window)`);
    }
  }
  for (const [name, value] of [['CDN-Cache-Control', snap.cdnCacheControl], ['Surrogate-Control', snap.surrogateControl]] as const) {
    if (!value) continue;
    const c = cacheDirectives(value);
    if ('no-store' in c || 'private' in c) continue;
    const life = c['s-maxage'] ?? c['max-age'];
    if (life !== undefined && (!isWholeSeconds(life) || Number(life) > limit)) {
      out.push(`${where(snap)}: shows an Amazon price but ${name} lets a CDN keep it ${life}s (at most ${limit}s): "${value}"`);
    }
    for (const k of ['stale-while-revalidate', 'stale-if-error']) {
      if (k in c) out.push(`${where(snap)}: shows an Amazon price but its ${name} has ${k} (a stale copy outlives the price window)`);
    }
  }
  return out;
}

// ─── the 2(k) notice ───────────────────────────────────────────────────────

/**
 * What of a known Amazon title the gate looks for in a page's text: the whole
 * title and, for a long one, its first TITLE_PREFIX_CHARS characters cut back
 * to a whole word (a truncated title). Nothing for a title shorter than
 * MIN_TITLE_MATCH_CHARS. Original case; match case-insensitively.
 */
export function titleNeedles(title: string): string[] {
  const t = collapse(title);
  if (t.length < MIN_TITLE_MATCH_CHARS) return [];
  const out = [t];
  if (t.length > TITLE_PREFIX_CHARS) {
    const cut = t.slice(0, TITLE_PREFIX_CHARS);
    const whole = /\s/.test(t[TITLE_PREFIX_CHARS] as string) ? cut : cut.replace(/\s+\S*$/, '');
    const prefix = whole.replace(/[\s,;:–—-]+$/, '');
    if (prefix.length >= MIN_TITLE_MATCH_CHARS && prefix !== t) out.push(prefix);
  }
  return out;
}

/** Why a page shows Amazon content ('' when it does not, as far as the gate can tell). */
export function amazonContentReason(snap: ComplianceSnapshot, knownTitles: readonly string[]): string {
  if (snap.amazonMarkers > 0) return `${snap.amazonMarkers} data-amazon-content element(s)`;
  if (snap.stamps.length) return `${snap.stamps.length} price/availability stamp(s)`;
  const text = collapse(snap.visibleText).toLowerCase();
  for (const title of knownTitles) {
    const needles = titleNeedles(title);
    const hit = needles.find((n) => text.includes(n.toLowerCase()));
    if (hit) return hit === needles[0] ? `the Amazon title "${hit.slice(0, 80)}"` : `the Amazon title "${hit}..." (truncated)`;
  }
  return '';
}

/** The exact 2(k) notice, in plain view, on every page that shows Amazon content. */
export function amazonNoticeProblems(snap: ComplianceSnapshot, rules: Pick<ComplianceRules, 'amazonNotice'>, knownTitles: readonly string[] = []): string[] {
  const out: string[] = [];
  const notice = collapse(rules.amazonNotice);
  for (const n of snap.noticeElements) {
    if (!collapse(n.text).includes(notice)) out.push(`${where(snap)}: a data-amazon-notice="2k" element does not hold the exact 2(k) notice ("${collapse(n.text).slice(0, 80)}")`);
  }
  const why = amazonContentReason(snap, knownTitles);
  if (!why) return out;
  if (!collapse(snap.visibleText).includes(notice)) {
    const hidden = snap.noticeElements.some((n) => !n.visible && collapse(n.text).includes(notice));
    out.push(`${where(snap)}: shows Amazon content (${why}) but not the 2(k) notice${hidden ? ' in plain view (it is in the page but hidden)' : ''}`);
  }
  return out;
}

// ─── Offer JSON-LD ─────────────────────────────────────────────────────────

type Node = Record<string, unknown>;

function typesOf(n: Node): string[] {
  const t = n['@type'];
  return (Array.isArray(t) ? t : t ? [t] : []).map(String);
}

/** Every object node in the page's JSON-LD (blocks that do not parse are skipped: all-pages reports them). */
export function jsonLdNodes(blocks: readonly string[]): Node[] {
  const out: Node[] = [];
  const walk = (v: unknown, depth: number): void => {
    if (depth > 40 || !v || typeof v !== 'object') return;
    if (Array.isArray(v)) { v.forEach((x) => walk(x, depth + 1)); return; }
    out.push(v as Node);
    for (const x of Object.values(v as Node)) walk(x, depth + 1);
  };
  for (const b of blocks) {
    try { walk(JSON.parse(b), 0); } catch { /* reported by all-pages */ }
  }
  return out;
}

/** validFrom within the quotable window, an honest affiliate Offer, a visible dated price; no unmeasured ratings. */
export function offerProblems(snap: ComplianceSnapshot, rules: Pick<ComplianceRules, 'now' | 'quotableWindowMs'>): string[] {
  const out: string[] = [];
  const now = snap.takenAt ?? rules.now;
  const nodes = jsonLdNodes(snap.jsonld);
  const offers = nodes.filter((n) => typesOf(n).some((t) => t === 'Offer' || t === 'AggregateOffer'));
  offers.forEach((o, i) => {
    const label = `${where(snap)} Offer ${i + 1}${o.url ? ` (${String(o.url).slice(0, 80)})` : ''}`;
    const vf = o.validFrom;
    if (vf == null || vf === '') out.push(`${label}: no validFrom (the time the price was checked)`);
    else {
      const t = Date.parse(String(vf));
      if (!Number.isFinite(t)) out.push(`${label}: validFrom "${String(vf)}" does not parse`);
      else {
        const why = checkTimeProblem(t, now, rules.quotableWindowMs);
        if (why) out.push(`${label}: validFrom ${String(vf)} is ${why}`);
      }
    }
    if (typesOf(o).includes('Offer')) {
      if (String(o.priceCurrency ?? '') !== 'USD') out.push(`${label}: priceCurrency "${String(o.priceCurrency ?? '')}", expected USD`);
      const price = Number(o.price);
      if (!Number.isFinite(price) || price < 1) out.push(`${label}: price "${String(o.price ?? '')}" is not a real price`);
    }
    for (const k of ['seller', 'hasMerchantReturnPolicy', 'shippingDetails']) {
      if (o[k] != null) out.push(`${label}: carries ${k} (an affiliate does not sell, ship or take returns)`);
    }
  });
  if (offers.length && !snap.stamps.some((s) => s.kind === 'price')) {
    out.push(`${where(snap)}: ${offers.length} Offer(s) in JSON-LD but no visible dated price on the page`);
  }
  for (const n of nodes) {
    if (typesOf(n).some((t) => t === 'AggregateRating') || n.aggregateRating != null) {
      out.push(`${where(snap)}: JSON-LD carries an AggregateRating (no ratings the site did not measure)`);
      break;
    }
  }
  return out;
}

// ─── Participation Requirements 6(y): no price tracking ────────────────────

/** Visible copy that offers price alerts or tracking, or claims a price history. */
export const PRICE_TRACKING_TEXT: ReadonlyArray<readonly [string, RegExp]> = Object.freeze([
  ['price alert', /\bprice[- ]?(?:drop[- ]|watch[- ])?alerts?\b/i],
  ['price-drop notification', /\bprice[- ]drop[- ](?:notifications?|emails?)\b/i],
  ['notify me about the price', /\b(?:notify|email|alert|text|tell)\s+me\b[^.!?\n]{0,40}\bprices?\b/i],
  ['price-drop notification', /\b(?:get|be)\s+(?:notified|alerted|emailed|texted|told)\b[^.!?\n]{0,40}\bprices?\b/i],
  ['price-drop notification', /\b(?:notify|alert|email|text|tell|ping|let)\s+(?:you|readers|subscribers)\b[^.!?\n]{0,40}\bprices?\s+(?:drops?|falls?|goes\s+down|changes?|dips?)\b/i],
  ['price tracking', /\btrack(?:s|ed|ing)?\s+(?:this|the|its|their|that|these|those|your|a|an)\s+(?:\w+(?:'s)?\s+)?prices?\b/i],
  ['price history', /\bprice[- ]history\b/i],
  ['last tracked price', /\blast[- ]tracked\b/i],
  ['lowest ever', /\blowest[\s-]+(?:[\w']+[\s-]+){0,2}ever\b/i],
  ['all-time low', /\ball[- ]time[- ]lows?\b/i],
  ['record low', /\b(?:record|historic(?:al)?)[- ]lows?\b/i],
  ['never been cheaper', /\bnever\s+been\s+(?:this\s+)?(?:cheaper|lower)\b/i],
] as Array<readonly [string, RegExp]>);

/** A form, field or button that collects price alerts or tracks prices. */
export const PRICE_TRACKING_FIELD_RE = /price[\s_-]*(?:alert|watch|track|drop|history|notif)|(?:alert|notify|track)[\s_-]*(?:me[\s_-]*)?(?:on[\s_-]*)?price/i;

const NEGATION_RE = /\b(?:no|not|never|without|none|nor|neither|don't|doesn't|didn't|do not|does not|did not|won't|will not|wouldn't|cannot|can't)\b/gi;
/**
 * Selling idioms built on a negation word ("never miss a deal", "don't miss
 * out", "no other store", "can't beat"): they never turn a claim around.
 */
const NEGATION_IDIOM_RE = /\b(?:never|don't|do\s+not|won't|will\s+not|can't|cannot)\s+(?:miss|wait|hesitate|forget|overpay|settle|pass\s+(?:it\s+|this\s+)?up)\b|\bno\s+(?:other|one\s+else|doubt|wonder|matter|need\s+to\s+wait|better)\b|\bnot\s+(?:only|just)\b|\bcan(?:'t|not)\s+(?:beat|go\s+wrong)\b|\bwon't\s+(?:last|find)\b|\bnothing\s+(?:beats|else)\b/gi;
/** Clause breaks a negation does not reach across. */
const CLAUSE_BREAK_RE = /[;:—–]|\s-\s|\b(?:but|however|yet|though|although|instead|so)\b/i;
/** A negation this many words or fewer before a phrase governs it ("no price alerts", "never claim a lowest-ever price"). */
export const DIRECT_NEGATION_WORDS = 3;
/** Or through a verb right after it ("does not offer price alerts or a price history"), up to this many words. */
export const VERB_NEGATION_WORDS = 12;
const CLAIM_VERB_RE = /^(?:offer|send|provide|have|has|had|keep|track|show|display|claim|make|use|run|publish|store|log|record|collect|support|do|does|did|give|list|print|quote|post)(?:s|es|ed|ing)?$/i;
/** How far back in its sentence a negation still governs a match. */
export const NEGATION_WINDOW_CHARS = 120;

/**
 * True when a negation in `before` (the sentence up to a phrase) turns the
 * phrase around: the last negation that is not a selling idiom, within
 * DIRECT_NEGATION_WORDS words of the phrase, or within VERB_NEGATION_WORDS
 * with a claim verb right after it, and no clause break in between.
 */
export function negationGoverns(before: string): boolean {
  const masked = String(before ?? '').replace(NEGATION_IDIOM_RE, (s) => ' '.repeat(s.length));
  let last: RegExpMatchArray | null = null;
  for (const m of masked.matchAll(NEGATION_RE)) last = m;
  if (!last) return false;
  const between = masked.slice((last.index ?? 0) + last[0].length);
  if (CLAUSE_BREAK_RE.test(between)) return false;
  const words = wordsOf(between);
  if (words.length <= DIRECT_NEGATION_WORDS) return true;
  return words.length <= VERB_NEGATION_WORDS && words.slice(0, 3).some((w) => CLAIM_VERB_RE.test(w));
}

/** Each match of `re` in `text` as its whole sentence; with `negationAware`, only those no governing negation turns around. */
function sentencesMatching(text: string, re: RegExp, negationAware: boolean): string[] {
  const out: string[] = [];
  const g = new RegExp(re.source, re.flags.includes('g') ? re.flags : `${re.flags}g`);
  for (const m of text.matchAll(g)) {
    const at = m.index ?? 0;
    const start = sentenceStart(text, at);
    if (negationAware && negationGoverns(text.slice(Math.max(start, at - NEGATION_WINDOW_CHARS), at))) continue;
    const rest = text.slice(at).search(/[!?\n]|\.(?:\s|$)/);
    out.push(collapse(text.slice(start, rest < 0 ? text.length : at + rest + 1)).slice(0, 160));
  }
  return out;
}

/** Matches of `re` in `text` that no governing negation earlier in the same sentence turns around; each as its sentence. */
export function unnegatedMatches(text: string, re: RegExp): string[] {
  return sentencesMatching(text, re, true);
}

export function priceTrackingProblems(snap: ComplianceSnapshot): string[] {
  const out: string[] = [];
  for (const [name, re] of PRICE_TRACKING_TEXT) {
    for (const sentence of unnegatedMatches(snap.visibleText, re)) out.push(`${where(snap)}: ${name} copy: "${sentence}"`);
  }
  for (const f of snap.forms) {
    const hit = [f.action, f.text, ...f.fields].find((s) => PRICE_TRACKING_FIELD_RE.test(s));
    if (hit) out.push(`${where(snap)}: price-tracking UI (${f.action ? `form action ${f.action}` : 'a control'}: "${collapse(hit).slice(0, 80)}")`);
  }
  return [...new Set(out)];
}

// ─── images: IP License 2(h) ───────────────────────────────────────────────

/** JSON-LD properties that hold an image (lower-cased; render/jsonld.ts strips Amazon images from the same set). */
const IMAGE_PROPERTIES: ReadonlySet<string> = new Set(['image', 'logo', 'thumbnail', 'thumbnailurl', 'photo', 'primaryimageofpage']);

function imageUrls(v: unknown): string[] {
  if (typeof v === 'string') return [v];
  if (Array.isArray(v)) return v.flatMap(imageUrls);
  if (v && typeof v === 'object') {
    const o = v as Node;
    return [o.url, o.contentUrl, o.thumbnailUrl].filter((x): x is string => typeof x === 'string');
  }
  return [];
}

/** True for a URL on Amazon's image CDN (or any Amazon host). */
export function isAmazonImageUrl(url: string): boolean {
  if (isAmazonHostedUrl(url)) return true;
  try { return isAmazonImageHost(new URL(url, 'https://kit.invalid/').hostname); } catch { return false; }
}

/** No Amazon CDN image as a social image, an article hero, or any JSON-LD image (Product, Article, logo, ...). */
export function amazonImageProblems(snap: ComplianceSnapshot): string[] {
  const out = new Set<string>();
  for (const s of snap.socialImages) if (isAmazonImageUrl(s.url)) out.add(`${where(snap)}: ${s.prop} on an Amazon host: ${s.url}`);
  for (const h of snap.heroImages) if (isAmazonImageUrl(h)) out.add(`${where(snap)}: hero image on an Amazon host: ${h}`);
  // Each Amazon image once, named by the first node that holds it (a parent comes before its ImageObject).
  const seen = new Set<string>();
  const add = (u: string, what: string): void => {
    if (seen.has(u) || !isAmazonImageUrl(u)) return;
    seen.add(u);
    out.add(`${where(snap)}: ${what} on an Amazon host: ${u}`);
  };
  for (const n of jsonLdNodes(snap.jsonld)) {
    const type = typesOf(n).join('/') || 'node';
    for (const [k, v] of Object.entries(n)) {
      if (IMAGE_PROPERTIES.has(k.toLowerCase())) for (const u of imageUrls(v)) add(u, `${type} JSON-LD ${k}`);
    }
    if (typesOf(n).includes('ImageObject')) {
      for (const u of imageUrls({ url: n.url, contentUrl: n.contentUrl, thumbnailUrl: n.thumbnailUrl })) add(u, 'ImageObject JSON-LD');
    }
  }
  return [...out];
}

/** No Amazon image in a sitemap: every <image:loc> (and any other *loc) of `xml`, entity-decoded. */
export function sitemapImageProblems(file: string, xml: string): string[] {
  const out = new Set<string>();
  for (const m of String(xml ?? '').matchAll(/<((?:[A-Za-z][\w-]*:)?[A-Za-z_]*loc)>\s*([^<\s]+)\s*<\/\1>/g)) {
    const url = (m[2] as string).replace(/&amp;/g, '&');
    if (isAmazonImageUrl(url)) out.add(`${file}: <${m[1]}> on an Amazon host: ${url}`);
  }
  return [...out];
}

// ─── GFM tables ────────────────────────────────────────────────────────────

/** A visible line that is a GFM delimiter row, or a pipe row of three or more cells. */
export const RAW_TABLE_ROW_RE = /(?:^|\n)\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*(?:$|\n)|(?:^|\n)\s*\|[^|\n]+\|[^|\n]+\|[^|\n]*\|?\s*(?:$|\n)/;

/** Tables render as well-formed table.md-table inside .md-table-wrap; no raw table Markdown; required tables present. */
export function tableProblems(snap: ComplianceSnapshot, mustHaveTable = false): string[] {
  const out: string[] = [];
  for (const t of snap.rawTableText) out.push(`${where(snap)}: unrendered Markdown table text: "${t.slice(0, 120)}"`);
  snap.mdTables.forEach((t, i) => {
    const label = `${where(snap)} table.md-table ${i + 1}`;
    if (!t.wrapped) out.push(`${label}: not inside .md-table-wrap (it widens the page on a phone)`);
    if (t.headerCells < 1) out.push(`${label}: no header cells`);
    else if (t.scopedHeaderCells !== t.headerCells) out.push(`${label}: ${t.headerCells - t.scopedHeaderCells} header cell(s) without scope="col"`);
    if (t.bodyRows < 1) out.push(`${label}: no body rows`);
    t.rowCells.forEach((n, r) => {
      if (n !== t.headerCells) out.push(`${label}: body row ${r + 1} has ${n} cells, the header ${t.headerCells}`);
    });
  });
  if (mustHaveTable && snap.mdTables.length === 0) out.push(`${where(snap)}: its Markdown has a table, but no table.md-table rendered`);
  return out;
}

// ─── recalls ───────────────────────────────────────────────────────────────

/**
 * No buy button for a recalled SKU on this page: no link naming it (/dp/, a
 * cart link's ASIN.N, our /r/<ASIN>) and no Amazon search for its model name
 * (when the gate config gives one). On the SKU's own page, a visible recall
 * notice and no Offer for it.
 */
export function recallProblems(
  snap: ComplianceSnapshot,
  recalled: ReadonlyArray<{ asin: string; path: string; name?: string }>,
): string[] {
  const out: string[] = [];
  const bad = new Map(recalled.map((r) => [r.asin.toUpperCase(), r]));
  const named = recalled.filter((r) => r.name && wordKey(r.name).trim()).map((r) => ({ r, key: wordKey(r.name as string) }));
  for (const a of snap.anchors) {
    const placed = a.placement ? ` (placement ${a.placement})` : '';
    for (const asin of asinsOfLink(a.abs)) {
      if (bad.has(asin)) out.push(`${where(snap)}: buy button for recalled ${asin}: ${a.href.slice(0, 120)}${placed}`);
    }
    const search = amazonSearchKey(a.abs);
    if (search) {
      for (const { r, key } of named) {
        if (search.includes(key)) out.push(`${where(snap)}: Amazon search for recalled ${r.asin.toUpperCase()} ("${r.name}"): ${a.href.slice(0, 120)}${placed}`);
      }
    }
  }
  const own = recalled.find((r) => r.path === snap.path);
  if (own) {
    if (snap.recallNotices < 1) out.push(`${where(snap)}: recalled ${own.asin}'s page shows no recall notice`);
    for (const n of jsonLdNodes(snap.jsonld)) {
      if (typesOf(n).includes('Product') && String(n.sku ?? '').toUpperCase() === own.asin.toUpperCase() && n.offers != null) {
        out.push(`${where(snap)}: recalled ${own.asin} still has an Offer in its Product JSON-LD`);
      }
    }
  }
  return out;
}
