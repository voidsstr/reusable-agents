/**
 * The same-spec substitute an unbuyable product page leads its hero with,
 * shared between the body render and the head render.
 *
 * Ported from SpecPicks src/services/pdpSubstituteQuote.ts (see
 * PORTED_FROM.json). The products assistants fetch most are often ones Amazon
 * cannot sell; their hero sells the best substitute. The head and the body
 * render in parallel, so the body records the substitute it chose here and
 * the head can NAME that same product (title, brand, matched spec). The memo
 * adds no rule of its own: the body's pick decides. A miss (first render
 * after a restart) leaves the head as it was.
 *
 * What the head must not do with it (where SpecPicks went wrong): put the
 * substitute's price, or any stock or availability claim, in
 * <meta name="description">, og:description or twitter:description. Head
 * metadata cannot carry the "as of" time and the IP License 2(i) disclaimer
 * that every shown Amazon price and availability statement needs, and search
 * engines keep a snippet long after the 24-hour window. SpecPicks' head
 * printed "In stock instead: <sub>, $X on Amazon (checked <date>)": no time,
 * no disclaimer. Prices belong in the body, through price.ts priceStamp().
 * recall()'s `quote` says the substitute's price is still quotable (a fair
 * test of whether to name it at all, and the number a body stamp may show);
 * it is never head text.
 *
 * Kit changes: the price is re-checked against the site's quotable window
 * (offer.ts), state lives in a SubstituteMemo instance (a module default
 * backs the convenience functions), and the check time is `fetchedAt`.
 */
import { quotablePrice } from './offer';
import type { SiteContext } from './siteContext';

export interface SubstituteQuote {
  asin: string;
  /** What the page calls the substitute (our model name where we have one). */
  title: string;
  brand: string;
  /** The matched spec in reader words ("2,048 Wh LiFePO4, 2,400 W"). */
  spec: string;
  price: number;
  currency: string;
  /** When that price was fetched from Amazon (ISO). */
  fetchedAt: string;
}

export const SUBSTITUTE_TTL_MS = 6 * 60 * 60 * 1000;
export const SUBSTITUTE_MAX_ENTRIES = 5000;

export class SubstituteMemo {
  private readonly memo = new Map<string, { q: SubstituteQuote; at: number }>();

  constructor(private readonly opts: { ttlMs?: number; maxEntries?: number; now?: () => number } = {}) {}

  private now(): number {
    return this.opts.now ? this.opts.now() : Date.now();
  }

  /** Record (or, with null, clear) the substitute a product page leads with. */
  remember(pageAsin: string, q: SubstituteQuote | null): void {
    const key = String(pageAsin || '');
    if (!key) return;
    if (!q) { this.memo.delete(key); return; }
    const max = this.opts.maxEntries ?? SUBSTITUTE_MAX_ENTRIES;
    if (this.memo.size >= max && !this.memo.has(key)) {
      const oldest = this.memo.keys().next().value;
      if (oldest !== undefined) this.memo.delete(oldest);
    }
    this.memo.delete(key);
    this.memo.set(key, { q: { ...q }, at: this.now() });
  }

  /**
   * The recorded substitute, only while its price is still quotable; adds
   * `quote` (the quotable price). Name the substitute in head metadata if you
   * like, but never print `quote` there (see the module doc).
   */
  recall(
    ctx: Pick<SiteContext, 'prices'>,
    pageAsin: string,
    now: number = this.now(),
  ): (SubstituteQuote & { quote: number }) | null {
    const key = String(pageAsin || '');
    const e = this.memo.get(key);
    if (!e) return null;
    if (now - e.at > (this.opts.ttlMs ?? SUBSTITUTE_TTL_MS)) { this.memo.delete(key); return null; }
    const quote = quotablePrice(ctx, { price: e.q.price, currency: e.q.currency, fetchedAt: e.q.fetchedAt }, now);
    return quote == null ? null : { ...e.q, quote };
  }

  clear(): void {
    this.memo.clear();
  }

  get size(): number {
    return this.memo.size;
  }
}

const DEFAULT_MEMO = new SubstituteMemo();

/** Module-default memo: record the substitute a product page leads with. */
export function rememberSubstitute(pageAsin: string, q: SubstituteQuote | null): void {
  DEFAULT_MEMO.remember(pageAsin, q);
}

/** Module-default memo: the recorded substitute while its price is quotable. */
export function recallSubstitute(
  ctx: Pick<SiteContext, 'prices'>,
  pageAsin: string,
  now?: number,
): (SubstituteQuote & { quote: number }) | null {
  return DEFAULT_MEMO.recall(ctx, pageAsin, now);
}

/** Test/ops hook for the module-default memo. */
export function resetSubstituteMemo(): void {
  DEFAULT_MEMO.clear();
}
