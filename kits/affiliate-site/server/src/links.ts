/**
 * Affiliate links and CTAs, the tracking ID each one carries, and the page
 * kind a click came from.
 *
 * Ported from SpecPicks (see PORTED_FROM.json): esc, isRealAmazonAsin,
 * amazonUrl, retagAmazonUrl, cta, amazonSearchCta, detailLink and
 * resolveSegmentTag from src/services/ssrRender.ts; pickTrackingId and
 * classifySource from src/index.ts. What changed:
 *   - Tags, labels, the marketplace host, the product route and the
 *     referer-path rules come from SiteContext, not env vars or literals.
 *   - Every CTA names its placement (data-cta-placement on the anchor), and
 *     its tag can be a per-placement segment tag. placement_watch and
 *     human_clicks attribute clicks by that value from day one.
 *   - Only Amazon: no eBay or retro routing, no pseudo-ASIN paths.
 *
 * Every affiliate anchor carries rel="sponsored nofollow noopener" and
 * target="_blank", and its tag is always one of the site's own tracking IDs
 * (ctx.amazon.allowedTags): every URL builder here, and amazonSearchUrlFor,
 * passes its tag through ownTrackingId(). noopener keeps the Referer, which
 * the click classifier relies on; never add noreferrer.
 */
import type { SiteContext } from './siteContext';

/** rel on every affiliate anchor. */
export const AFFILIATE_REL = 'sponsored nofollow noopener';
/** Class of the primary Amazon button (kept from SpecPicks so its CSS and gate specs carry over). */
export const CTA_CLASS = 'ssr-amz-cta';
export const DETAIL_LINK_CLASS = 'ssr-detail-link';
/** The attribute the click beacon reads to attribute a click to a CTA position. */
export const PLACEMENT_ATTR = 'data-cta-placement';
/** Placement names: kebab-case, at most 40 chars (the beacon stores 40). */
export const PLACEMENT_RE = /^[a-z0-9][a-z0-9-]{0,39}$/;
/** A real Amazon ASIN: exactly 10 uppercase alphanumerics. */
export const AMAZON_ASIN_RE = /^[A-Z0-9]{10}$/;
/** The kit's click-logging redirect route (implemented by the HTTP middleware). */
export const REDIRECT_PATH_PREFIX = '/r/';

type LinkCtx = Pick<SiteContext, 'amazon'>;

const own = (o: object, k: string): boolean => Object.prototype.hasOwnProperty.call(o, k);

/**
 * HTML-escape text for an element body or a quoted attribute.
 *
 * Common entities that leak into DB text (&amp; &lt; &gt; &quot; &#39;) are
 * decoded first, so "Strix Halo &amp; Radeon" and "Strix Halo & Radeon" both
 * render as "Strix Halo &amp; Radeon" instead of "&amp;amp;". Any other '&'
 * passes through the decode and is encoded normally.
 */
export function esc(s: string | number | null | undefined): string {
  if (s == null) return '';
  return String(s)
    .replace(/&(amp|lt|gt|quot|#39);/g, (_m, e: string) =>
      e === 'amp' ? '&' :
      e === 'lt' ? '<' :
      e === 'gt' ? '>' :
      e === 'quot' ? '"' : "'")
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/**
 * True only for a real Amazon ASIN. An allowlist on purpose: SpecPicks fixed a
 * pseudo-ASIN denylist twice ('EBAY_' then 'PCB_' walked through it); an
 * allowlist excludes the next pseudo-ID by construction.
 */
export function isAmazonAsin(asin: string | null | undefined): boolean {
  return AMAZON_ASIN_RE.test(String(asin || ''));
}

/** Throws on a malformed placement: a typo would silently split click attribution. */
export function assertPlacement(placement: string): string {
  if (!PLACEMENT_RE.test(String(placement || ''))) {
    throw new TypeError(`invalid CTA placement ${JSON.stringify(placement)}: use kebab-case, at most 40 chars`);
  }
  return placement;
}

/** The site's own tracking IDs: partner tag plus every placement and source tag. */
export function allowedTags(ctx: LinkCtx): readonly string[] {
  return ctx.amazon.allowedTags;
}

export function isAllowedTag(ctx: LinkCtx, tag: string | null | undefined): boolean {
  return !!tag && ctx.amazon.allowedTags.includes(tag);
}

/**
 * `tag` when it is one of the site's own tracking IDs, else the partner tag.
 * Every URL builder runs its tag through this, so no link the kit builds can
 * carry someone else's tracking ID, whatever a caller passes (a stray or
 * request-supplied tag would otherwise divert the commission). It never
 * throws: a bad tag costs segment attribution, not the page.
 */
export function ownTrackingId(ctx: LinkCtx, tag: string | null | undefined): string {
  return tag && isAllowedTag(ctx, tag) ? tag : ctx.amazon.partnerTag;
}

/**
 * Resolve a caller-supplied ?tag= (an internal link's segment) to a tracking
 * ID we own, else the partner tag. The allowlist matters: an open passthrough
 * would let any URL rewrite our associate tag to a third party's and divert
 * commission. A site that does not opt in (acceptSegmentTagParam) always gets
 * the partner tag.
 */
export function resolveSegmentTag(ctx: LinkCtx, raw: string | null | undefined): string {
  const t = (raw || '').trim();
  return ctx.amazon.acceptSegmentTagParam && t && isAllowedTag(ctx, t) ? t : ctx.amazon.partnerTag;
}

/**
 * The tag a CTA at `placement` carries: the placement's segment tag when one is
 * configured, else the (allowlisted) segment the visit arrived under, else the
 * partner tag.
 */
export function tagForPlacement(ctx: LinkCtx, placement: string, segmentTag?: string | null): string {
  const tags = ctx.amazon.placementTags;
  if (own(tags, placement)) return tags[placement] as string;
  return resolveSegmentTag(ctx, segmentTag);
}

/** Tagged /dp/ URL for a real ASIN; a tag that is not the site's own becomes the partner tag. */
export function amazonUrl(ctx: LinkCtx, asin: string, tag: string = ctx.amazon.partnerTag): string {
  return `https://${ctx.amazon.marketplaceHost}/dp/${encodeURIComponent(asin)}?tag=${encodeURIComponent(ownTrackingId(ctx, tag))}&linkCode=ll1`;
}

/**
 * Re-tag a STORED Amazon URL and validate it in one step; '' for anything that
 * is not an https URL on this site's Amazon marketplace. Validates the PARSED
 * hostname, never the raw string: a prefix regex like
 * /^https?:\/\/(www\.)?amazon\.[a-z.]+\// also matched amazon.com.evil.io,
 * i.e. an open redirect wearing our tag and rel="sponsored". The tag is SET,
 * not defaulted, so a stored URL carrying someone else's tag reports under
 * ours; a `tag` argument that is not the site's own becomes the partner tag.
 */
export function retagAmazonUrl(ctx: LinkCtx, url: string, tag: string = ctx.amazon.partnerTag): string {
  const raw = String(url || '').trim();
  let u: URL;
  try {
    u = new URL(raw);
  } catch {
    return '';
  }
  if (u.protocol !== 'https:' && u.protocol !== 'http:') return '';
  if (u.username || u.password) return '';
  const want = ctx.amazon.marketplaceHost.toLowerCase().replace(/^www\./, '');
  const host = u.hostname.toLowerCase().replace(/^www\./, '');
  if (host !== want) return '';
  u.protocol = 'https:';
  u.port = '';
  // Drop every case variant first: a stored `Tag=` would otherwise ride along next to ours.
  for (const k of Array.from(u.searchParams.keys())) if (k.toLowerCase() === 'tag') u.searchParams.delete(k);
  u.searchParams.set('tag', ownTrackingId(ctx, tag));
  return u.toString();
}

function classAttr(base: string, extra?: string): string {
  const more = String(extra || '').split(/\s+/).filter((c) => /^[A-Za-z0-9_-]+$/.test(c));
  return [base, ...more].join(' ');
}

export interface CtaOptions {
  asin: string;
  /** Where the CTA sits (pdp-hero, sticky-mobile, ...). Required: every click is attributed. */
  placement: string;
  /** Button text; default ctx.amazon.ctaLabel. Never put a price here. */
  label?: string;
  /** The ?tag= the visit arrived under (honoured only when allowlisted). */
  segmentTag?: string | null;
  /** Extra CSS classes (letters, digits, '-' and '_' only). */
  className?: string;
}

/**
 * The Amazon /dp/ button for one ASIN, or '' when the ASIN is not a real one:
 * a dead /dp/ link that still counts as an affiliate click is worse than no
 * button. Labels are escaped; the href's '&' becomes '&amp;'.
 */
export function cta(ctx: LinkCtx, o: CtaOptions): string {
  if (!isAmazonAsin(o.asin)) return '';
  const placement = assertPlacement(o.placement);
  const tag = tagForPlacement(ctx, placement, o.segmentTag);
  return `<a class="${classAttr(CTA_CLASS, o.className)}" target="_blank" rel="${AFFILIATE_REL}" ${PLACEMENT_ATTR}="${placement}" href="${esc(amazonUrl(ctx, o.asin, tag))}">${esc(o.label ?? ctx.amazon.ctaLabel)}</a>`;
}

export interface SearchCtaOptions {
  /** A tagged or untagged Amazon URL (typically a SEARCH url); re-tagged and validated here. */
  url: string;
  placement: string;
  /** Default ctx.amazon.searchCtaLabel. Price-free by contract: a search page is not a product page. */
  label?: string;
  segmentTag?: string | null;
  className?: string;
}

/**
 * The same button as cta(), for a ready-made Amazon URL (e.g. the tagged
 * SEARCH url for a listing Amazon no longer sells). The URL is re-tagged with
 * the placement's tag and must be on this site's marketplace, else ''.
 */
export function amazonSearchCta(ctx: LinkCtx, o: SearchCtaOptions): string {
  const placement = assertPlacement(o.placement);
  const href = retagAmazonUrl(ctx, o.url, tagForPlacement(ctx, placement, o.segmentTag));
  if (!href) return '';
  return `<a class="${classAttr(CTA_CLASS, o.className)}" target="_blank" rel="${AFFILIATE_REL}" ${PLACEMENT_ATTR}="${placement}" href="${esc(href)}">${esc(o.label ?? ctx.amazon.searchCtaLabel)}</a>`;
}

/** Site path of a product page, from ctx.routes.product. */
export function productPath(ctx: Pick<SiteContext, 'routes'>, asin: string): string {
  return ctx.routes.product.replace('{asin}', encodeURIComponent(asin));
}

/** Absolute canonical URL of a product page. */
export function productUrl(ctx: Pick<SiteContext, 'routes' | 'site'>, asin: string): string {
  return `${ctx.site.canonicalOrigin}${productPath(ctx, asin)}`;
}

/** Internal link to our own product page ('' for a non-ASIN). Not an affiliate link. */
export function detailLink(ctx: Pick<SiteContext, 'routes' | 'site'>, asin: string, label = 'See Full Details →'): string {
  if (!isAmazonAsin(asin)) return '';
  return `<a class="${DETAIL_LINK_CLASS}" href="${esc(productUrl(ctx, asin))}">${esc(label)}</a>`;
}

/**
 * The page kind a click came from, by referer path, using ctx.sources (first
 * matching rule wins: exact paths, then prefixes). No path is 'direct', '/' is
 * 'home', anything unmatched is 'other'. Stored in outbound_clicks.source_kind.
 */
export function classifySource(ctx: Pick<SiteContext, 'sources'>, refererPath: string | undefined | null): string {
  if (!refererPath) return 'direct';
  if (refererPath === '/') return 'home';
  for (const rule of ctx.sources) {
    if (rule.exact.includes(refererPath)) return rule.kind;
    if (rule.prefixes.some((p) => refererPath.startsWith(p))) return rule.kind;
  }
  return 'other';
}

/**
 * The tracking ID for a click from `sourceKind` (the /r/ redirect's choice):
 * the kind's tag from site.json, else the partner tag. Associates allows extra
 * tracking IDs under one account, so each content type's sales can be told
 * apart.
 */
export function pickTrackingId(ctx: LinkCtx, sourceKind: string | undefined | null): string {
  const tags = ctx.amazon.sourceTags;
  return (sourceKind && own(tags, sourceKind) ? tags[sourceKind] : undefined) || ctx.amazon.partnerTag;
}
