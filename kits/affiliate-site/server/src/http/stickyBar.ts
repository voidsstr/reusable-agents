/**
 * The sticky mobile buy bar: markup (here) and sizing (client/src/stickyBar.ts).
 *
 * From SpecPicks' PDP bar (`.ssr-mobile-sticky-cta` in ssrRender.ts): high-
 * intent readers skim to the price, and a buy button pinned to the bottom of
 * a phone screen is the only CTA visible below the fold. On SpecPicks it is
 * the second-largest verified-click placement after pdp-hero. Desktop hides
 * it with a media query; the client publishes its height as --kit-sticky-h so
 * the page pads its end clear of it (upstream did the same from React).
 *
 * Rules carried over:
 *   - one honest destination or no bar: a real ASIN's /dp/ CTA, or a
 *     ready-made Amazon URL (a tagged search for an unbuyable listing),
 *     re-tagged and host-checked; never a dead link that still bills as a
 *     click.
 *   - every CTA names its placement: 'sticky-mobile' on a buyable page; a
 *     page that sends the bar to a substitute passes its own (upstream:
 *     'pdp-sticky-live'), so click attribution can tell them apart.
 *   - a substitute's bar shows the substitute's name (and status copy saying
 *     so), never this listing's photo beside another product's button.
 *
 * Kit rules on Amazon content:
 *   - a price shows only as the full kit price stamp (time checked plus the
 *     IP License 2(i) disclaimer behind "Details"), only while it is
 *     quotable, and only on a /dp/ bar: a search button is not one product,
 *     so no single price applies to it (links.ts: search CTAs are
 *     price-free). Where the browser has no popover support the stamp is
 *     hidden in the bar rather than unfolding the disclaimer into it.
 *   - the name is marked data-amazon-content (K3's AMAZON_CONTENT_ATTR)
 *     unless the caller says it is the site's own model name, so the layout
 *     keeps the 2(k) notice on any page whose bar shows Amazon's title.
 *   - an Amazon-hosted thumbnail is Product Advertising Content: it is shown
 *     only with the time it was fetched, inside the same window as a price
 *     (IP License 2(h)), and is marked data-amazon-content. Prefer the site's
 *     own images.
 */
import { amazonSearchCta, cta, esc } from '../links';
import { FUTURE_SKEW_MS, quotableMaxAgeMs, type PriceFields } from '../offer';
import { priceStamp } from '../price';
import { isAmazonHostedUrl } from '../render/amazonHosts';
import { AMAZON_CONTENT_ATTR } from '../render/blocks';
import type { SiteContext } from '../siteContext';
import {
  STICKY_ACTIVE_CLASS, STICKY_BAR_ATTR, STICKY_BAR_CLASS, STICKY_DEFAULT_PLACEMENT, STICKY_HEIGHT_VAR, STICKY_SUPPRESS_ATTR,
} from './clientContract';

/** Button text when none is given: short enough for a 360px screen. */
export const STICKY_CTA_LABEL = 'View on Amazon';

export interface StickyBuyBarOptions {
  /** The product's name (our model name, or a fresh cached title). */
  name: string;
  /**
   * True when `name` is the site's own model name. Default false: the name
   * is treated as Amazon's text and marked data-amazon-content, so the page
   * keeps the 2(k) notice.
   */
  nameIsOwn?: boolean;
  /** A real ASIN: the button is the tagged /dp/ CTA. */
  asin?: string | null;
  /** Or a ready-made Amazon URL (e.g. the tagged search for an unbuyable listing). */
  url?: string | null;
  /** data-cta-placement of the button; default 'sticky-mobile'. */
  placement?: string;
  /** Button text; default STICKY_CTA_LABEL. Never a price. */
  label?: string;
  /** The ?tag= the visit arrived under (honoured only when allowlisted). */
  segmentTag?: string | null;
  /**
   * Thumbnail: a site path or an https URL. An Amazon-hosted URL
   * (*.media-amazon.com, *ssl-images-amazon.com, ...) needs imageFetchedAt.
   */
  imageUrl?: string | null;
  /**
   * When an Amazon-hosted imageUrl came back from Amazon (the same fetch as
   * the price). Without it, or outside the price window, an Amazon image is
   * left out. Ignored for the site's own images.
   */
  imageFetchedAt?: string | Date | number | null;
  imageAlt?: string;
  /**
   * A short line above the name, e.g. 'A similar model:'. Never an
   * availability claim ('in stock', 'unavailable', 'ships in 2 days'): the
   * gate's kit-compliance spec (K5) accepts availability only inside a dated
   * stamp (availabilityStamp), and this line is plain text.
   */
  status?: string | null;
  /** Shown as the kit price stamp when quotable, on an `asin` bar only (never beside a search button). */
  price?: PriceFields | null;
  /** Clock for the price and image rules (tests). */
  now?: number;
}

type StickyCtx = Pick<SiteContext, 'amazon' | 'prices'>;

function safeImageUrl(u: string | null | undefined): string {
  const s = String(u || '').trim();
  if (/^https:\/\/[^\s"'<>]+$/i.test(s)) return s;
  if (/^\/(?!\/)[^\s"'<>]*$/.test(s)) return s;
  return '';
}

/** True when an Amazon response fetched at `at` may still be shown: the price window and clock-skew rule (offer.ts). */
function withinAmazonWindow(ctx: Pick<SiteContext, 'prices'>, at: string | Date | number | null | undefined, now: number): boolean {
  if (at == null || at === '') return false;
  const t = new Date(at).getTime();
  if (!Number.isFinite(t)) return false;
  const age = now - t;
  return age >= -FUTURE_SKEW_MS && age <= quotableMaxAgeMs(ctx);
}

/**
 * The bar's markup, or '' when there is no honest destination. Place it once,
 * at the end of <main>; include STICKY_BAR_CSS in the page CSS and the kit
 * client bundle for the height variable.
 */
export function stickyBuyBarHtml(ctx: StickyCtx, o: StickyBuyBarOptions): string {
  const placement = o.placement ?? STICKY_DEFAULT_PLACEMENT;
  const label = o.label ?? STICKY_CTA_LABEL;
  const className = `${STICKY_BAR_CLASS}__btn`;
  const productButton = o.asin ? cta(ctx, { asin: o.asin, placement, label, segmentTag: o.segmentTag, className }) : '';
  const button = productButton
    || (!o.asin && o.url ? amazonSearchCta(ctx, { url: o.url, placement, label, segmentTag: o.segmentTag, className }) : '');
  if (!button) return '';

  const now = o.now ?? Date.now();
  let img = safeImageUrl(o.imageUrl);
  const amazonImg = !!img && isAmazonHostedUrl(img);
  if (amazonImg && !withinAmazonWindow(ctx, o.imageFetchedAt, now)) img = '';
  // A price belongs to one product: never beside a search button.
  const stamp = productButton && o.price ? priceStamp(ctx, o.price, { now, key: `${o.asin}|${placement}` }) : '';
  const amazonMark = ` ${AMAZON_CONTENT_ATTR}`;
  return `<div class="${STICKY_BAR_CLASS}" ${STICKY_BAR_ATTR}>`
    + `<div class="${STICKY_BAR_CLASS}__inner">`
    + (img ? `<img class="${STICKY_BAR_CLASS}__img" src="${esc(img)}" alt="${esc(o.imageAlt ?? o.name)}" width="48" height="48" loading="lazy" decoding="async"${amazonImg ? amazonMark : ''}>` : '')
    + `<div class="${STICKY_BAR_CLASS}__meta">`
    + (o.status ? `<div class="${STICKY_BAR_CLASS}__status">${esc(o.status)}</div>` : '')
    + `<div class="${STICKY_BAR_CLASS}__name"${o.nameIsOwn ? '' : amazonMark}>${esc(o.name)}</div>`
    + (stamp ? `<div class="${STICKY_BAR_CLASS}__price">${stamp}</div>` : '')
    + '</div>'
    + button
    + '</div></div>';
}

const B = `.${STICKY_BAR_CLASS}`;

/**
 * CSS for the bar, on the kit theme tokens (themeCss). Shown under 768px,
 * hidden in print and while any [data-kit-sticky-suppress] element is in the
 * page (that rule needs :has()). The page end is padded by the bar's
 * measured height (96px until the client has measured it), keyed on the
 * class the client puts on <html> (html.kit-has-sticky), so the footer's
 * disclosure and 2(k) notice stay clear of the bar in browsers without
 * :has(); body:has(.kit-sticky-bar) pads it without JS where :has() works.
 * The two are separate rules: an unsupported selector would void a list.
 */
export const STICKY_BAR_CSS = [
  `${B}{display:none;position:fixed;left:0;right:0;bottom:0;z-index:100;background:var(--kit-surface,#fff);color:var(--kit-text,#0f172a);border-top:1px solid var(--kit-border,#e2e8f0);padding:10px 16px calc(10px + env(safe-area-inset-bottom,0px));box-shadow:0 -4px 20px rgba(15,23,42,.12)}`,
  `${B}__inner{display:flex;align-items:center;gap:10px;max-width:600px;margin:0 auto}`,
  `${B}__img{width:48px;height:48px;object-fit:contain;flex-shrink:0;border-radius:8px;background:var(--kit-bg,#fff)}`,
  `${B}__meta{flex:1;min-width:0}`,
  `${B}__status{font-size:.74rem;line-height:1.2;color:var(--kit-muted,#475569)}`,
  `${B}__name{font-size:.8rem;font-weight:600;line-height:1.25;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}`,
  `${B}__price{font-size:.72rem;line-height:1.3;color:var(--kit-muted,#475569)}`,
  `${B}__price .ssr-price{font-size:1rem;font-weight:800;color:var(--kit-text,#0f172a)}`,
  `${B} ${B}__btn{flex-shrink:0;white-space:nowrap;padding:10px 14px;border-radius:8px;font-weight:700;text-decoration:none;background:var(--kit-cta-bg,#f59e0b);color:var(--kit-cta-text,#111827)}`,
  `${B} ${B}__btn:focus-visible{outline:3px solid var(--kit-focus,#2563eb);outline-offset:2px}`,
  `@supports not selector(:popover-open){${B} .kit-price-stamp{display:none}}`,
  `@media (max-width:768px){${B}{display:block}html.${STICKY_ACTIVE_CLASS} body{padding-bottom:var(${STICKY_HEIGHT_VAR},96px)}body:has(${B}){padding-bottom:var(${STICKY_HEIGHT_VAR},96px)}}`,
  `body:has([${STICKY_SUPPRESS_ATTR}]) ${B}{display:none!important}`,
  `@media print{${B}{display:none!important}}`,
].join('');
