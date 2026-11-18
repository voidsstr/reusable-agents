/**
 * Affiliate-honest schema.org Offer and the price-quotability rule.
 *
 * Ported from SpecPicks src/services/affiliateOffer.ts (see PORTED_FROM.json).
 * A site on the kit is an Amazon Associate, not a merchant: structured data
 * and visible copy must not claim we sell, ship, take returns or hold stock,
 * and a price may be shown only while it is current. One definition serves
 * the JSON-LD and the visible price/CTA, so the page and its schema can never
 * disagree about whether a price exists.
 *
 * What changed from SpecPicks:
 *   - The freshness window comes from SiteContext and is 24h MINUS the longest
 *     time rendered HTML can sit in a cache (ctx.prices.quotableMaxAgeSeconds),
 *     so a cached page never shows a price older than 24h (IP License 2(h)).
 *     SpecPicks' PRICE_FRESH_HOURS env override survives as a tighten-only
 *     override in siteContext.ts.
 *   - The check time is `fetchedAt` (amazon_item_cache.fetched_at);
 *     SpecPicks' `priceUpdatedAt` is accepted as an alias.
 *   - affiliateOfferLd also maps a Creators availability *type* when given.
 *   - Amounts are parsed strictly (parsePriceAmount): a formatted or junk
 *     string has no price, where parseFloat quoted '1,299.00' as $1.00.
 */
import type { SiteContext } from './siteContext';

/** Smallest price ever quoted (pricing integrity: no $0 / sub-$1 rows). */
export const MIN_QUOTABLE_PRICE = 1;

/** A plain decimal amount, as Postgres NUMERIC and the price writers produce ('1299.99', '19.990000000000002'). */
const PRICE_AMOUNT_RE = /^\d+(?:\.\d+)?$/;

/**
 * A stored amount as a number, or null unless it is a finite number or a plain
 * decimal string (surrounding whitespace allowed). Strict on purpose (pricing
 * integrity): parseFloat read '1,299.00' as 1 and '12abc' as 12, so a
 * formatted or garbled value was quoted as a wrong price instead of none.
 */
export function parsePriceAmount(v: unknown): number | null {
  if (typeof v === 'number') return Number.isFinite(v) ? v : null;
  if (typeof v !== 'string') return null;
  const s = v.trim();
  if (!PRICE_AMOUNT_RE.test(s)) return null;
  const n = Number(s);
  return Number.isFinite(n) ? n : null;
}
/** The only currency the kit quotes (US store). */
export const QUOTE_CURRENCY = 'USD';
/** A check time up to this far in the future is clock skew, not an error. */
export const FUTURE_SKEW_MS = 5 * 60 * 1000;

export interface PriceFields {
  price?: number | string | null;
  currency?: string | null;
  /** When the price was fetched from Amazon (amazon_item_cache.fetched_at). */
  fetchedAt?: string | Date | null;
  /** SpecPicks' name for the same thing (products.price_updated_at); fetchedAt wins when both are set. */
  priceUpdatedAt?: string | Date | null;
  /**
   * products.quality_flags. Optional and additive: a call that omits it
   * behaves as before. A repurposed listing (Amazon now sells a different
   * product under the ASIN) has no quotable price.
   */
  qualityFlags?: unknown;
}

/**
 * The shelf audit flags an ASIN `shelf-<ts>-listing-repurposed` when Amazon
 * now sells a DIFFERENT product under it; the price refresh keeps re-pricing
 * such rows, so without this check a fresh price, an Offer and a /dp/ button
 * for the wrong product reach the page.
 */
export const REPURPOSED_LISTING_FLAG_RE = /^shelf-.+-listing-repurposed$/;

/** True when flags is an array (or a JSON string of one) holding a repurposed-listing flag. */
export function isRepurposedListing(flags: unknown): boolean {
  let list: unknown = flags;
  if (typeof list === 'string') {
    try { list = JSON.parse(list); } catch { return false; }
  }
  if (!Array.isArray(list)) return false;
  return list.some((x) => typeof x === 'string' && REPURPOSED_LISTING_FLAG_RE.test(x));
}

/**
 * The SQL twin of isRepurposedListing() for a table alias: the same predicate
 * as the framework price refresh's _repurposed_sql() (regex, not LIKE, so it
 * holds no bare '%'). Use as `AND NOT ${REPURPOSED_LISTING_SQL('p')}`. An
 * empty alias yields bare column refs.
 */
export function REPURPOSED_LISTING_SQL(alias: string = 'p'): string {
  if (alias && !/^[A-Za-z_][A-Za-z0-9_]*$/.test(alias)) throw new Error(`REPURPOSED_LISTING_SQL: bad alias ${alias}`);
  const col = alias ? `${alias}.quality_flags` : 'quality_flags';
  return `(jsonb_typeof(${col}) = 'array' AND EXISTS (SELECT 1 FROM jsonb_array_elements_text(${col}) rf WHERE rf ~ '^shelf-.+-listing-repurposed$'))`;
}

/**
 * A stored list price more than this many times the price is not a list price
 * (B07L5VKTNQ: $20.69 vs a $494.18 "list"), so no strike-through, "% off" or
 * "Save" is drawn from it.
 */
export const MAX_LIST_PRICE_RATIO = 3;

/** originalPrice when it is an honest list price for price; else null. */
export function honestListPrice(price: unknown, originalPrice: unknown): number | null {
  if (price == null || originalPrice == null) return null;
  const p = parsePriceAmount(price);
  const o = parsePriceAmount(originalPrice);
  if (p == null || o == null) return null;
  if (p < 1) return null;
  if (o < p) return null;
  if (o > MAX_LIST_PRICE_RATIO * p) return null;
  return o;
}

/** The price's check time (fetchedAt, else priceUpdatedAt) as a Date; null when missing or unparseable. */
export function priceCheckedAt(f: Pick<PriceFields, 'fetchedAt' | 'priceUpdatedAt'>): Date | null {
  const raw = f.fetchedAt ?? f.priceUpdatedAt;
  if (!raw) return null;
  const d = new Date(raw as string | Date);
  return Number.isFinite(d.getTime()) ? d : null;
}

/** The oldest a price may be and still be quoted, in ms (ctx.prices.quotableMaxAgeSeconds). */
export function quotableMaxAgeMs(ctx: Pick<SiteContext, 'prices'>): number {
  return ctx.prices.quotableMaxAgeSeconds * 1000;
}

/**
 * The price, as a number, when it may be shown/emitted right now; else null.
 * Valid = a plain amount (parsePriceAmount), USD (or unset), >= $1, not a
 * repurposed listing, and fetched within the site's quotable window (no more
 * than FUTURE_SKEW_MS in the future).
 */
export function quotablePrice(ctx: Pick<SiteContext, 'prices'>, f: PriceFields, now: number = Date.now()): number | null {
  if (f.price == null) return null;
  if (isRepurposedListing(f.qualityFlags)) return null;
  const n = parsePriceAmount(f.price);
  if (n == null || n < MIN_QUOTABLE_PRICE) return null;
  if (f.currency && String(f.currency).toUpperCase() !== QUOTE_CURRENCY) return null;
  const at = priceCheckedAt(f);
  if (!at) return null;
  const age = now - at.getTime();
  if (age < -FUTURE_SKEW_MS || age > quotableMaxAgeMs(ctx)) return null;
  return n;
}

export function isQuotable(ctx: Pick<SiteContext, 'prices'>, f: PriceFields, now: number = Date.now()): boolean {
  return quotablePrice(ctx, f, now) != null;
}

/**
 * The schema.org availability the listing's own stock text states. Every
 * Offer used to say InStock, so PDPs with a fresh price and "Currently
 * unavailable." (plus pre-orders and listings that ship in weeks or months)
 * told shopping answers they could be bought today. No text, or text that
 * names no stock problem, stays InStock.
 */
export function offerAvailabilityFromText(text?: string | null): string {
  const t = String(text ?? '').toLowerCase();
  if (/currently unavailable|out of stock|no longer available/.test(t)) return 'https://schema.org/OutOfStock';
  if (/will be released|pre-?order/.test(t)) return 'https://schema.org/PreOrder';
  if (/ships within[^.]*\b(weeks?|months?)\b|available to ship in/.test(t)) return 'https://schema.org/BackOrder';
  if (/only \d+ left|limited availability/.test(t)) return 'https://schema.org/LimitedAvailability';
  return 'https://schema.org/InStock';
}

/**
 * schema.org availability for a Creators/PA-API Offers availability type, or
 * null when the type says nothing beyond "in stock" (the caller then falls
 * back to the stock text).
 */
export function offerAvailabilityFromType(type?: string | null): string | null {
  switch (String(type ?? '').toUpperCase()) {
    case 'IN_STOCK_SCARCE': return 'https://schema.org/LimitedAvailability';
    case 'PREORDER':
    case 'AVAILABLE_DATE': return 'https://schema.org/PreOrder';
    case 'LEADTIME': return 'https://schema.org/BackOrder';
    case 'OUT_OF_STOCK':
    case 'UNAVAILABLE': return 'https://schema.org/OutOfStock';
    default: return null;
  }
}

export interface OfferInput extends PriceFields {
  url: string;
  priceValidUntil?: string;
  used?: boolean;
  availabilityText?: string | null;
  /** Creators availability type (IN_STOCK, IN_STOCK_SCARCE, LEADTIME, ...). */
  availabilityType?: string | null;
}

/**
 * schema.org Offer for an affiliate product page, or null when no current
 * price exists (callers then omit `offers` entirely). Never carries seller,
 * hasMerchantReturnPolicy or shippingDetails. validFrom is the time the price
 * was fetched, so the dated price the page shows ("as of <time> UTC") reaches
 * an assistant's structured read too; it is not a priceValidUntil -- we cannot
 * promise how long the price holds.
 */
export function affiliateOfferLd(
  ctx: Pick<SiteContext, 'prices'>,
  f: OfferInput,
  now: number = Date.now(),
): Record<string, unknown> | null {
  const price = quotablePrice(ctx, f, now);
  if (price == null) return null;
  const offer: Record<string, unknown> = {
    '@type': 'Offer',
    url: f.url,
    priceCurrency: QUOTE_CURRENCY,
    price: price.toFixed(2),
    availability: offerAvailabilityFromType(f.availabilityType) ?? offerAvailabilityFromText(f.availabilityText),
    itemCondition: f.used ? 'https://schema.org/UsedCondition' : 'https://schema.org/NewCondition',
  };
  if (f.priceValidUntil) offer.priceValidUntil = f.priceValidUntil;
  offer.validFrom = (priceCheckedAt(f) as Date).toISOString();
  return offer;
}

/**
 * A Product node for a LIST surface -- `base` plus a current Offer -- or null
 * when no price is quotable. Callers then emit a plain name + url ListItem: a
 * Product with no offers / review / aggregateRating is an invalid item to
 * Google, and a stale Offer breaks the freshness rule.
 */
export function offerableProductLd(
  ctx: Pick<SiteContext, 'prices'>,
  base: Record<string, unknown>,
  f: OfferInput,
  now: number = Date.now(),
): Record<string, unknown> | null {
  const offer = affiliateOfferLd(ctx, f, now);
  return offer ? { ...base, offers: offer } : null;
}

/**
 * ItemList entry for one product: the full Product when it has a current
 * Offer, otherwise just its name and page URL.
 */
export function productListItemLd(
  ctx: Pick<SiteContext, 'prices'>,
  position: number,
  base: Record<string, unknown> & { name?: unknown; url?: unknown },
  f: OfferInput,
  now: number = Date.now(),
): Record<string, unknown> {
  const product = offerableProductLd(ctx, base, f, now);
  if (product) {
    const { ['@context']: _ctx, ...item } = product;
    return { '@type': 'ListItem', position, item };
  }
  return { '@type': 'ListItem', position, name: base.name, url: base.url };
}
