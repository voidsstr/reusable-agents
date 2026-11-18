/**
 * Why an inactive product is inactive, from its quality_flags, and the honest
 * Amazon destination for a listing Amazon no longer sells.
 *
 * Ported from SpecPicks src/services/productAvailability.ts (see
 * PORTED_FROM.json). The flag vocabulary is the framework agents' (price
 * refresh, shelf audit), shared by every site. Kit changes: no eBay/retro
 * branch in inactivePageRenders, and the search URL uses the site's
 * marketplace host.
 *
 * Product pages assistants have cited keep getting fetched after the product
 * is deactivated. For a product Amazon simply stopped selling (no buy box,
 * dead listing) the page is still worth rendering (noindexed): its specs,
 * current alternatives and a search link. For a row deactivated as JUNK
 * (off-vertical, wrong image, miscategorised, foreign-marketplace listing) it
 * is not: that page stays a thin noindexed shell.
 */
import { ownTrackingId } from './links';
import type { SiteContext } from './siteContext';

/** Flags meaning "Amazon no longer sells this listing". */
const UNAVAILABLE_FLAG_RE = /^(no-buybox|dead-amazon-link|unavailable-listing|amazon-not-found|stale-price-unbuyable)\b/;

/** Flags meaning "this row should never have been a product page". */
const JUNK_FLAG_RE = /^(off-vertical|no-image|image-mismatch|shelf-image-mismatch|shelf-audit-image-mismatch|miscategori[sz]ed|ai-unclassifiable|bulk-multipack-swag|foreign-marketplace|no-brand|decor-dummy)/;

export type InactiveReason = 'unavailable-on-amazon' | 'junk' | null;

function flagList(flags: unknown): string[] {
  if (Array.isArray(flags)) return flags.map((f) => String(f || '').toLowerCase());
  if (typeof flags === 'string') {
    try {
      const v: unknown = JSON.parse(flags);
      return Array.isArray(v) ? v.map((f) => String(f || '').toLowerCase()) : [];
    } catch {
      return [];
    }
  }
  return [];
}

/**
 * 'unavailable-on-amazon' when a flag says Amazon stopped selling it and no
 * flag says the row is junk; 'junk' when any junk flag is present; null when
 * the flags say nothing either way.
 */
export function inactiveReason(flags: unknown): InactiveReason {
  const list = flagList(flags);
  if (list.some((f) => JUNK_FLAG_RE.test(f))) return 'junk';
  if (list.some((f) => UNAVAILABLE_FLAG_RE.test(f))) return 'unavailable-on-amazon';
  return null;
}

export interface InactiveProductRow {
  /** Our own name for the product (model name), or a fresh cached title. */
  title?: string | null;
  /** A displayable image (our own, or a cached Amazon image link younger than 24h). */
  main_image_url?: string | null;
  category_slug?: string | null;
  quality_flags?: unknown;
}

/**
 * Whether a product page renders a real (noindexed) retired-product page for a
 * product with no active row: it needs a title, an image and a category, and
 * a flag saying Amazon stopped selling it. Junk rows stay a thin shell. Share
 * this with any link guard, so "this link lands on a page" means one thing.
 */
export function inactivePageRenders(p: InactiveProductRow | null | undefined): boolean {
  if (!p || !p.category_slug || !p.main_image_url || !p.title) return false;
  return inactiveReason(p.quality_flags) === 'unavailable-on-amazon';
}

/** Search terms for a product that has no live listing: brand + model head. */
export function productSearchTerms(title?: string | null, brand?: string | null): string {
  const words = String(title || '')
    .replace(/\([^)]*\)/g, ' ')
    .split(/[,|]/)[0]!
    .replace(/[^a-zA-Z0-9 .+-]/g, ' ')
    .split(/\s+/)
    .filter((w) => w.length > 1)
    .slice(0, 7);
  const b = String(brand || '').trim();
  const head = words.join(' ');
  return (b && !head.toLowerCase().includes(b.toLowerCase()) ? `${b} ${head}` : head).trim()
    || String(title || '').slice(0, 80);
}

/**
 * Tagged Amazon SEARCH url (never a /dp/ link to a listing that is gone). A
 * tag that is not one of the site's own becomes the partner tag (ownTrackingId).
 */
export function amazonSearchUrlFor(
  ctx: Pick<SiteContext, 'amazon'>,
  title: string | null | undefined,
  brand: string | null | undefined,
  tag: string = ctx.amazon.partnerTag,
): string {
  const u = new URL(`https://${ctx.amazon.marketplaceHost}/s`);
  u.searchParams.set('k', productSearchTerms(title, brand));
  u.searchParams.set('tag', ownTrackingId(ctx, tag));
  return u.toString();
}
