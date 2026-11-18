/**
 * Price and availability display: labels and the dated stamp every shown
 * Amazon price or availability statement must carry.
 *
 * priceLabel/priceHtml are ported from SpecPicks src/services/ssrRender.ts
 * (see PORTED_FROM.json). The stamp keeps SpecPicks' shape ("$X on Amazon as
 * of 2026-11-17 14:11 UTC (price may vary)") and adds what IP License 2(i)
 * requires and SpecPicks' SSR lacked: the exact disclaimer, behind an
 * adjacent "Details" control (Amazon's own example: "(as of 01/07/2008 14:11
 * PST- Details)").
 *
 * The Details control is a native popover (<button popovertarget> + <span
 * popover>): no JavaScript, keyboard accessible, valid inside a <p>. The
 * disclaimer is in the DOM either way, so a page's text always contains it;
 * browsers without popover support show it inline (PRICE_STAMP_CSS hides the
 * dead button there). Never put a stamp inside an <a>.
 *
 * A price renders only while it is quotable (offer.ts: USD, >= $1, fetched
 * within the site's window). There is deliberately no stamp-less "fresh
 * price" helper: priceLabel/priceHtml are building blocks for callers that
 * render the stamp next to them.
 */
import { esc } from './links';
import { priceDisclaimerFor } from './notices';
import { parsePriceAmount, priceCheckedAt, quotableMaxAgeMs, quotablePrice, FUTURE_SKEW_MS, type PriceFields } from './offer';
import type { SiteContext } from './siteContext';

/** Shown after every quoted price, as SpecPicks does. */
export const PRICE_MAY_VARY = '(price may vary)';

export const STAMP_CLASS = 'kit-price-stamp';
export const STAMP_ATTR = 'data-price-stamp';
export const DETAILS_CLASS = 'kit-price-details';
export const DISCLAIMER_CLASS = 'kit-price-disclaimer';
export const DETAILS_LABEL = 'Details';

/** ISO currency -> display symbol (SpecPicks' map). */
export const CURRENCY_SYMBOLS: Readonly<Record<string, string>> = Object.freeze({
  USD: '$', CAD: 'CA$', AUD: 'A$', GBP: '£', EUR: '€', JPY: '¥',
  INR: '₹', SGD: 'S$', SAR: 'SR', AED: 'AED', BRL: 'R$', MXN: 'MX$',
});

function symbolFor(currency: string | null | undefined): string {
  const cur = (currency || 'USD').toUpperCase();
  return Object.prototype.hasOwnProperty.call(CURRENCY_SYMBOLS, cur) ? CURRENCY_SYMBOLS[cur] as string : `${cur} `;
}

/**
 * Currency-aware short label ("$1,299.00"), no HTML. '' for a missing, zero,
 * negative or unparseable price (parsePriceAmount: '1,299.00' is not 1) so
 * callers can fall back to price-free copy.
 */
export function priceLabel(price: number | string | null | undefined, currency: string | null | undefined = 'USD'): string {
  const p = parsePriceAmount(price);
  if (p == null || p <= 0) return '';
  return `${symbolFor(currency)}${p.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

/** <span class="ssr-price">$X.XX</span>, with the listing's own currency symbol; '' when invalid. */
export function priceHtml(price: number | string | null | undefined, currency: string | null | undefined = 'USD'): string {
  const p = parsePriceAmount(price);
  if (p == null || p <= 0) return '';
  return `<span class="ssr-price">${symbolFor(currency)}${p.toFixed(2)}</span>`;
}

function toDate(at: Date | string | number | null | undefined): Date | null {
  if (at == null || at === '') return null;
  const d = at instanceof Date ? at : new Date(at);
  return Number.isFinite(d.getTime()) ? d : null;
}

/** "2026-11-17 14:11 UTC", or '' for a missing/invalid time. */
export function utcStamp(at: Date | string | number | null | undefined): string {
  const d = toDate(at);
  return d ? `${d.toISOString().slice(0, 16).replace('T', ' ')} UTC` : '';
}

/** <time datetime="ISO">2026-11-17 14:11 UTC</time>, or '' for a missing/invalid time. */
export function timeHtml(at: Date | string | number | null | undefined): string {
  const d = toDate(at);
  return d ? `<time datetime="${esc(d.toISOString())}">${esc(utcStamp(d))}</time>` : '';
}

/** 32-bit FNV-1a, hex: stable popover ids without a per-request counter. */
function fnv1a(s: string): string {
  let h = 0x811c9dc5;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return (h >>> 0).toString(16).padStart(8, '0');
}

let popoverSeq = 0;

/**
 * Popover id: `explicit` (sanitised) when given, else a hash of `seed`, else
 * a process-unique `kit-pd-_<n>` (the '_' keeps it apart from hash ids). The
 * fallback exists because two un-keyed stamps for the same price and fetch
 * time (one refresh batch) used to share an id: invalid HTML, and the second
 * "Details" opened the first popover.
 */
function popoverId(seed: string | undefined, explicit?: string): string {
  const clean = String(explicit || '').replace(/[^A-Za-z0-9_-]/g, '');
  if (clean) return `kit-pd-${clean}`;
  if (seed) return `kit-pd-${fnv1a(seed)}`;
  popoverSeq = popoverSeq >= Number.MAX_SAFE_INTEGER ? 1 : popoverSeq + 1;
  return `kit-pd-_${popoverSeq.toString(36)}`;
}

/**
 * The "Details" control and the exact 2(i) disclaimer it reveals. Exported so
 * a renderer that builds its own stamp layout still carries the same text.
 * The popover id is `id` when given (sanitised), else derived from `seed`,
 * else unique per call (so the markup then differs between renders).
 */
export function priceDetailsHtml(ctx: Pick<SiteContext, 'amazon'>, ref: { id?: string; seed?: string } = {}): string {
  const pid = popoverId(ref.seed, ref.id);
  // The space keeps "Details" and the disclaimer separate words in the page's
  // extracted text (what assistants and scanners read).
  return `<button type="button" class="${DETAILS_CLASS}" popovertarget="${pid}">${DETAILS_LABEL}</button> `
    + `<span id="${pid}" class="${DISCLAIMER_CLASS}" popover role="note" data-nosnippet>${esc(priceDisclaimerFor(ctx))}</span>`;
}

export interface StampOptions {
  now?: number;
  /**
   * Names this stamp on the page (e.g. `${asin}|${placement}`) and makes its
   * popover id stable across renders. Without a key (or id) the id is unique
   * per call, so ids never collide but the markup changes on every render.
   * Two stamps on one page must not share a key.
   */
  key?: string;
  /** Explicit popover id suffix (letters, digits, '-' and '_'); overrides the derived one. */
  id?: string;
}

type StampCtx = Pick<SiteContext, 'amazon' | 'prices'>;

/**
 * "$1,299.00 on Amazon.com as of 2026-11-17 14:11 UTC (price may vary) Details",
 * or '' when the price is not quotable now (stale, non-USD, under $1,
 * repurposed listing, no or bad timestamp). The only way the kit prints a price.
 */
export function priceStamp(ctx: StampCtx, f: PriceFields, opts: StampOptions = {}): string {
  const now = opts.now ?? Date.now();
  const q = quotablePrice(ctx, f, now);
  if (q == null) return '';
  const at = priceCheckedAt(f) as Date; // quotablePrice() proved it parses
  const label = priceLabel(q, 'USD');
  const iso = at.toISOString();
  return `<span class="${STAMP_CLASS}" ${STAMP_ATTR}="price">`
    + `<span class="ssr-price">${esc(label)}</span> `
    + `<span class="ssr-price-checked">on ${esc(ctx.amazon.siteName)} as of ${timeHtml(at)} ${PRICE_MAY_VARY}</span> `
    + priceDetailsHtml(ctx, { id: opts.id, seed: opts.key ? `p|${opts.key}|${iso}|${label}` : undefined })
    + '</span>';
}

export type AvailabilityKind = 'unavailable' | 'in-stock' | 'limited' | 'preorder' | 'ships-later';

const AVAILABILITY_TEXT: Readonly<Record<AvailabilityKind, (site: string) => string>> = Object.freeze({
  'unavailable': (s: string) => `Unavailable on ${s}`,
  'in-stock': (s: string) => `In stock on ${s}`,
  'limited': (s: string) => `Limited stock on ${s}`,
  'preorder': (s: string) => `Available for pre-order on ${s}`,
  'ships-later': (s: string) => `Not in stock on ${s} now; ships later`,
});

/** The stamp wording for a kind; undefined for anything else (own properties only: 'constructor' is not a kind). */
function availabilityText(kind: unknown): ((site: string) => string) | undefined {
  return typeof kind === 'string' && Object.prototype.hasOwnProperty.call(AVAILABILITY_TEXT, kind)
    ? AVAILABILITY_TEXT[kind as AvailabilityKind]
    : undefined;
}

/** Creators/PA-API Offers availability type -> stamp kind; null when unknown. */
export function availabilityKindFromType(type: string | null | undefined): AvailabilityKind | null {
  switch (String(type || '').toUpperCase()) {
    case 'IN_STOCK': return 'in-stock';
    case 'IN_STOCK_SCARCE': return 'limited';
    case 'PREORDER':
    case 'AVAILABLE_DATE': return 'preorder';
    case 'LEADTIME': return 'ships-later';
    case 'OUT_OF_STOCK':
    case 'UNAVAILABLE': return 'unavailable';
    default: return null;
  }
}

export interface AvailabilityInput {
  kind: AvailabilityKind;
  /** When we last checked Amazon (the price refresh's fetched_at). */
  checkedAt: Date | string | number | null | undefined;
}

/**
 * True when an availability check time may still be shown: same window and
 * clock-skew rule as a price (IP License 2(h) caps availability at 24h too).
 */
export function availabilityIsShowable(ctx: Pick<SiteContext, 'prices'>, checkedAt: AvailabilityInput['checkedAt'], now: number = Date.now()): boolean {
  const d = toDate(checkedAt);
  if (!d) return false;
  const age = now - d.getTime();
  return age >= -FUTURE_SKEW_MS && age <= quotableMaxAgeMs(ctx);
}

/** Neutral copy for when no availability may be stated: it asserts nothing, so it needs no stamp. */
export function availabilityFallbackHtml(ctx: Pick<SiteContext, 'amazon'>): string {
  return `<span class="kit-availability kit-availability--unknown">See current price and availability on ${esc(ctx.amazon.siteName)}</span>`;
}

/**
 * "Unavailable on Amazon.com as of 2026-11-17 14:11 UTC Details" for the
 * unbuyable / currently-unavailable variants (and the other kinds). When the
 * check is older than the quotable window (or missing), or the kind is not
 * one of AvailabilityKind (a row value the caller did not map), returns the
 * neutral availabilityFallbackHtml() instead: an undated, stale or invented
 * availability claim is exactly what 2(i) forbids, and a render must not throw.
 */
export function availabilityStamp(ctx: StampCtx, a: AvailabilityInput, opts: StampOptions = {}): string {
  const now = opts.now ?? Date.now();
  const words = availabilityText(a.kind);
  if (!words || !availabilityIsShowable(ctx, a.checkedAt, now)) return availabilityFallbackHtml(ctx);
  const at = toDate(a.checkedAt) as Date;
  const text = words(ctx.amazon.siteName);
  return `<span class="${STAMP_CLASS}" ${STAMP_ATTR}="availability">`
    + `<span class="kit-availability kit-availability--${a.kind}">${esc(text)}</span> as of ${timeHtml(at)} `
    + priceDetailsHtml(ctx, { id: opts.id, seed: opts.key ? `a|${opts.key}|${at.toISOString()}|${a.kind}` : undefined })
    + '</span>';
}

/**
 * Minimal CSS for stamps; the layout's base.css should include it (or an
 * equivalent). Without popover support the button is hidden and the
 * disclaimer shows inline, which 2(i) also allows ("adjacent").
 */
export const PRICE_STAMP_CSS = [
  `.${DETAILS_CLASS}{font:inherit;font-size:.85em;margin:0 0 0 .3em;padding:0;border:0;background:none;color:inherit;text-decoration:underline dotted;text-underline-offset:2px;cursor:pointer}`,
  `.${DETAILS_CLASS}:focus-visible{outline:2px solid currentColor;outline-offset:2px}`,
  `.${DISCLAIMER_CLASS}{max-width:min(28rem,calc(100vw - 2rem));padding:.75rem 1rem;border:1px solid currentColor;border-radius:.5rem;font-size:.875rem;line-height:1.45}`,
  `@supports not selector(:popover-open){.${DETAILS_CLASS}{display:none}.${DISCLAIMER_CLASS}{display:inline;padding:0;border:0;font-size:.8em}}`,
].join('');
