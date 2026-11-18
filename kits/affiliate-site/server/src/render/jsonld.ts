/**
 * schema.org JSON-LD builders for affiliate pages.
 *
 * One rule runs through all of them: structured data describes only what the
 * page itself shows and what we can honestly claim as an Amazon Associate.
 *   - Product carries an Offer only when the price is quotable right now
 *     (offer.ts: USD, >= $1, fetched within the site's window, not a
 *     repurposed listing). The Offer's validFrom is the fetch time, so the
 *     dated price the page shows reaches an assistant's structured read too.
 *     No seller, returns or shipping claims: we sell nothing.
 *   - Never an AggregateRating (we have no reviews of our own; Amazon's are
 *     not ours to republish). serializeJsonLd() strips one from any node,
 *     however it got there (compact or full schema.org IRI, at any depth).
 *   - No Amazon-hosted image in Product.image, Article.image or a logo
 *     (IP License 2(h)); serializeJsonLd() also drops one from the image
 *     properties of hand-built nodes.
 *   - An Amazon URL in an Offer or ItemList is an affiliate link: it carries
 *     one of the site's own tags (retagAmazonUrl, when the context has the
 *     site's amazon settings) or it is not published; the product page
 *     stands in. External URLs must be https.
 *   - FAQPage only from the Q&A the page renders (faqSectionHtml() takes the
 *     same rows); an empty FAQ is no FAQPage at all.
 *
 * Ported from SpecPicks (see unit/render/fixtures/ported-from-render.json):
 * the Product node of productHead (name cut to 150 chars without an
 * ellipsis, brand cleaning, placeholder brands dropped, sku = ASIN, @id
 * '<url>#product'), the isSimilarTo node and the alternatives ItemList of the
 * unbuyable PDP, the BreadcrumbList shape, the FAQPage rule, the spec
 * placeholder filter for additionalProperty, productIds.ts (sanitizeMpn,
 * gtinCheckDigitOk) and scrubJsonLdReviews (here stripAggregateRatings).
 */
import { isAllowedTag, isAmazonAsin, productUrl, retagAmazonUrl } from '../links';
import { affiliateOfferLd, productListItemLd, type PriceFields } from '../offer';
import type { SiteContext } from '../siteContext';
import { isAmazonHostedUrl, isAmazonLinkHost, isAmazonOwnedHost } from './amazonHosts';
import { collapseWhitespace, htmlToText, plainText, trimNoEllipsis, truncateAtWord } from './text';
import { publicImageUrl, siteUrl } from './urls';

export const SCHEMA_ORG_CONTEXT = 'https://schema.org';

/** One JSON-LD node (a plain JSON object). */
export type JsonLdNode = { [key: string]: unknown };

/**
 * The context for nodes that may carry offer URLs. `amazon` is optional so a
 * narrow context still works: without it an Amazon URL cannot be re-tagged,
 * and the product page stands in for it.
 */
type LdCtx = Pick<SiteContext, 'site' | 'routes' | 'prices'> & Partial<Pick<SiteContext, 'amazon'>>;

/** Google's limit for Product.name in merchant rich results. */
export const PRODUCT_NAME_MAX = 150;
/** Upper bound for Product.description. */
export const PRODUCT_DESCRIPTION_MAX = 5000;
/** Article headlines longer than this are flagged by Google. */
export const ARTICLE_HEADLINE_MAX = 110;
/** additionalProperty entries per Product (a long spec table must not bloat the head). */
export const MAX_ADDITIONAL_PROPERTIES = 12;
/** Breadcrumb names are cut (without an ellipsis) to this many characters. */
export const BREADCRUMB_NAME_MAX = 70;

// ─── identifiers ───────────────────────────────────────────────────────────

/** The stable @id of a product page's Product node: '<product url>#product'. */
export function productNodeId(ctx: Pick<SiteContext, 'site' | 'routes'>, asin: string): string {
  return `${productUrl(ctx, asin)}#product`;
}

/** The site's Organization @id ('<origin>/#organization'). */
export function organizationNodeId(ctx: Pick<SiteContext, 'site'>): string {
  return `${ctx.site.canonicalOrigin}/#organization`;
}

/** The site's WebSite @id ('<origin>/#website'). */
export function webSiteNodeId(ctx: Pick<SiteContext, 'site'>): string {
  return `${ctx.site.canonicalOrigin}/#website`;
}

const INVISIBLE = /[\u200e\u200f\u202a-\u202e\ufeff]/g;

function cleanId(v: unknown): string {
  return String(v ?? '').replace(INVISIBLE, '').replace(/\s+/g, ' ').trim();
}

/** Mod-10 check digit used by UPC-A / EAN-13 / GTIN-14. */
export function gtinCheckDigitOk(digits: string): boolean {
  if (!/^\d{12,14}$/.test(digits)) return false;
  const body = digits.slice(0, -1);
  let sum = 0;
  for (let i = 0; i < body.length; i++) {
    const d = Number(body[body.length - 1 - i]);
    sum += i % 2 === 0 ? d * 3 : d;
  }
  return (10 - (sum % 10)) % 10 === Number(digits[digits.length - 1]);
}

/**
 * A plausible manufacturer part/model number, or null. Rejects: no digit,
 * under 3 or over 40 chars, more than 3 spaces, a "for ..." compatibility
 * phrase, the ASIN, seller hash noise, a pure barcode (that is a GTIN) and
 * descriptive text (two or more plain words of 4+ letters).
 */
export function sanitizeMpn(raw: unknown, asin?: string | null): string | null {
  const v = cleanId(raw);
  if (v.length < 3 || v.length > 40) return null;
  if (!/\d/.test(v)) return null;
  if ((v.match(/ /g) || []).length > 3) return null;
  if (/^for\s/i.test(v)) return null;
  if (asin && v.toUpperCase().includes(String(asin).toUpperCase())) return null;
  if (/^[a-z0-9]{16,}$/.test(v)) return null;
  if (/^[0-9a-f]{20,}$/i.test(v)) return null;
  if (/^\d{12,14}$/.test(v)) return null;
  const plainWords = v.split(/[\s/,]+/).filter((w) => /^[A-Za-z]{4,}$/.test(w));
  if (plainWords.length >= 2) return null;
  return v;
}

export interface GtinFields {
  gtin12?: string;
  gtin13?: string;
  gtin14?: string;
}

/** The GTIN field for a barcode with a valid check digit ({} otherwise). */
export function gtinFields(raw: unknown): GtinFields {
  const d = cleanId(raw).replace(/[\s-]/g, '');
  if (!gtinCheckDigitOk(d)) return {};
  if (d.length === 12) return { gtin12: d };
  if (d.length === 13) return { gtin13: d };
  return { gtin14: d };
}

const PLACEHOLDER_BRAND_RE = /^(generic|unbranded|unknown|n\/a|none|na|-)$/i;

/**
 * A brand fit for a Brand node, or null: role suffixes ("Jane Doe(Author)")
 * removed, and placeholders ("Generic", "Unbranded") are not a brand.
 */
export function cleanBrand(raw: string | null | undefined): string | null {
  const stripped = String(raw ?? '').trim()
    .replace(/\s*\((?:Author|Publisher|Manufacturer|Editor|Director|Producer|Photographer|Illustrator|Narrator|Translator)\)/gi, '')
    .replace(/\s*,\s*/g, ', ').replace(/\s+/g, ' ').trim()
    .replace(/[,\s\-–—]+$/u, '').trim();
  if (!stripped || PLACEHOLDER_BRAND_RE.test(stripped)) return null;
  return stripped;
}

/**
 * A spec value that says "we don't know" ("Not published by manufacturer",
 * "Verify on Amazon listing"). Fine as a visible table row; as a
 * schema.org PropertyValue it asserts a non-fact, so it is dropped.
 */
export const SPEC_VALUE_PLACEHOLDER = /^(?:not published|not specified|not listed|unknown|unspecified|n\/a|verify (?:on|with)|check (?:the )?(?:amazon|listing|manufacturer))/i;

export interface PropertyValueInput {
  name: string;
  value: string | number;
  /** UN/CEFACT unit code, e.g. 'WHR' (watt hour), 'WTT' (watt). */
  unitCode?: string;
  unitText?: string;
}

/** PropertyValue nodes for a spec table: placeholders and empties dropped, at most `max`. */
export function propertyValuesLd(
  props: readonly PropertyValueInput[] | null | undefined,
  max: number = MAX_ADDITIONAL_PROPERTIES,
): JsonLdNode[] {
  const out: JsonLdNode[] = [];
  for (const p of props ?? []) {
    if (out.length >= max) break;
    const name = collapseWhitespace(p?.name);
    const value = typeof p?.value === 'number' ? (Number.isFinite(p.value) ? p.value : null) : collapseWhitespace(p?.value);
    if (!name || value === null || value === '' || (typeof value === 'string' && SPEC_VALUE_PLACEHOLDER.test(value))) continue;
    const node: JsonLdNode = { '@type': 'PropertyValue', name, value };
    if (p.unitCode && /^[A-Z0-9]{2,3}$/.test(p.unitCode)) node.unitCode = p.unitCode;
    if (p.unitText) node.unitText = collapseWhitespace(p.unitText);
    out.push(node);
  }
  return out;
}

// ─── Product, Offer, isSimilarTo, ItemList ─────────────────────────────────

/** Price fields for an Offer (offer.ts PriceFields plus the listing's stock signals). */
export interface ProductOfferInput extends PriceFields {
  /**
   * Offer.url: a site path or URL, an https URL elsewhere, or an Amazon URL
   * on the site's marketplace (re-tagged: it keeps its tag only when that is
   * exactly one of the site's own, else gets the partner tag). Anything else
   * (a short link, another marketplace, plain http) falls back to the
   * product's own page, which is also the default.
   */
  url?: string;
  availabilityText?: string | null;
  availabilityType?: string | null;
  used?: boolean;
}

export interface SimilarProductRef {
  asin: string;
  name: string;
}

export interface ProductLdInput {
  /** The ASIN: sku, and the @id when it is a real ASIN. */
  asin: string;
  name: string;
  /** The product page (path or URL on the site); default productUrl(ctx, asin). */
  url?: string;
  description?: string | null;
  brand?: string | null;
  /** Candidate images; Amazon-hosted and non-https ones are dropped, duplicates removed, at most 10. */
  images?: ReadonlyArray<string | null | undefined>;
  mpn?: string | null;
  gtin?: string | null;
  category?: string | null;
  additionalProperty?: readonly PropertyValueInput[];
  /** The listing's price; becomes an Offer only while quotable. */
  offer?: ProductOfferInput | null;
  /** In-stock alternatives (see isSimilarToLd). */
  isSimilarTo?: readonly SimilarProductRef[];
}

/**
 * The tag an Amazon URL keeps in structured data: its own when it carries
 * exactly one tag and that tag is one of the site's, else the partner tag
 * (the same rule markdown links follow, minus placements: JSON-LD has none).
 */
function structuredDataTag(ctx: Pick<SiteContext, 'amazon'>, url: URL): string {
  const own = Array.from(url.searchParams.entries()).filter(([k]) => k.toLowerCase() === 'tag').map(([, v]) => v);
  return own.length === 1 && isAllowedTag(ctx, own[0]) ? (own[0] as string) : ctx.amazon.partnerTag;
}

/**
 * A URL for structured data: a site path or site URL made absolute on the
 * canonical origin; an Amazon URL re-tagged with one of the site's own tags
 * (null when it cannot be: no amazon settings in the context, a short link,
 * another marketplace, an image host); else an https URL elsewhere (a maker's
 * manual). null for anything else (plain http, javascript:, junk), so the
 * caller's fallback applies.
 */
function ldUrl(ctx: Pick<SiteContext, 'site'> & Partial<Pick<SiteContext, 'amazon'>>, url: string | null | undefined): string | null {
  const s = String(url ?? '').trim();
  if (!s) return null;
  try {
    return siteUrl(ctx, s);
  } catch {
    // not on this site
  }
  let u: URL;
  try {
    u = new URL(s);
  } catch {
    return null;
  }
  if ((u.protocol !== 'https:' && u.protocol !== 'http:') || u.username || u.password) return null;
  if (isAmazonOwnedHost(u.hostname)) {
    const amazon = ctx.amazon;
    if (!amazon || !isAmazonLinkHost(u.hostname)) return null;
    return retagAmazonUrl({ amazon }, s, structuredDataTag({ amazon }, u)) || null;
  }
  return u.protocol === 'https:' ? u.toString() : null;
}

function similarRef(ctx: Pick<SiteContext, 'site' | 'routes'>, alt: SimilarProductRef): JsonLdNode {
  return {
    '@type': 'Product',
    '@id': productNodeId(ctx, alt.asin),
    name: trimNoEllipsis(plainText(alt.name), PRODUCT_NAME_MAX),
    url: productUrl(ctx, alt.asin),
  };
}

function imageList(ctx: Pick<SiteContext, 'site'>, images: ReadonlyArray<string | null | undefined> | undefined): string[] {
  const out: string[] = [];
  for (const raw of images ?? []) {
    const u = publicImageUrl(ctx, raw);
    if (u && !out.includes(u)) out.push(u);
    if (out.length >= 10) break;
  }
  return out;
}

/**
 * The Product node of a product page. `offers` is present only when the
 * price is quotable at `now`; there is never an aggregateRating, review,
 * seller, return or shipping claim.
 */
export function productLd(ctx: LdCtx, p: ProductLdInput, now: number = Date.now()): JsonLdNode {
  const realAsin = isAmazonAsin(p.asin);
  const url = p.url ? siteUrl(ctx, p.url) : productUrl(ctx, p.asin);
  const node: JsonLdNode = {
    '@context': SCHEMA_ORG_CONTEXT,
    '@type': 'Product',
    '@id': realAsin ? productNodeId(ctx, p.asin) : `${url}#product`,
    name: trimNoEllipsis(plainText(p.name), PRODUCT_NAME_MAX),
    url,
  };
  if (realAsin) node.sku = p.asin;
  const description = truncateAtWord(htmlToText(p.description), PRODUCT_DESCRIPTION_MAX);
  if (description) node.description = description;
  const brand = cleanBrand(p.brand);
  if (brand) node.brand = { '@type': 'Brand', name: brand };
  const images = imageList(ctx, p.images);
  if (images.length) node.image = images;
  const mpn = sanitizeMpn(p.mpn, p.asin);
  if (mpn) node.mpn = mpn;
  Object.assign(node, gtinFields(p.gtin));
  const category = collapseWhitespace(p.category);
  if (category) node.category = category;
  const props = propertyValuesLd(p.additionalProperty);
  if (props.length) node.additionalProperty = props;
  if (p.offer) {
    const offer = affiliateOfferLd(ctx, { ...p.offer, url: ldUrl(ctx, p.offer.url) ?? url }, now);
    if (offer) node.offers = offer;
  }
  const similar = (p.isSimilarTo ?? []).filter((a) => isAmazonAsin(a.asin) && a.asin !== p.asin && collapseWhitespace(a.name));
  if (similar.length) node.isSimilarTo = similar.map((a) => similarRef(ctx, a));
  return node;
}

/**
 * A node that adds isSimilarTo to the page's Product (same @id), for the
 * "what to buy instead" block a renderer emits in the body: nothing in the
 * structured data otherwise says those rows replace this listing. null when
 * there are no (real-ASIN) alternatives.
 */
export function isSimilarToLd(
  ctx: Pick<SiteContext, 'site' | 'routes'>,
  asin: string,
  alternatives: readonly SimilarProductRef[],
): JsonLdNode | null {
  const alts = alternatives.filter((a) => isAmazonAsin(a.asin) && a.asin !== asin && collapseWhitespace(a.name));
  if (!isAmazonAsin(asin) || !alts.length) return null;
  return {
    '@context': SCHEMA_ORG_CONTEXT,
    '@type': 'Product',
    '@id': productNodeId(ctx, asin),
    isSimilarTo: alts.map((a) => similarRef(ctx, a)),
  };
}

export interface ItemListEntry {
  name: string;
  /**
   * The entry's page: a site path or URL (absolutised), an https URL for a
   * non-product entry, or an Amazon URL (re-tagged, as ProductOfferInput.url).
   * When it cannot be published, an entry with a real ASIN points at its
   * product page instead and any other entry is left out.
   */
  url: string;
  /** A product entry: sku and @id come from it. */
  asin?: string | null;
  brand?: string | null;
  image?: string | null;
  /** The entry's price; the entry is a full Product with an Offer only while it is quotable. */
  offer?: ProductOfferInput | null;
}

export interface ItemListInput {
  name?: string;
  /** The list's own page. */
  url?: string;
  order?: 'ascending' | 'descending' | 'unordered';
  items: readonly ItemListEntry[];
}


/**
 * An ItemList (best lists, ladders, the alternatives on an unbuyable page).
 * Each product entry is a full Product with a current Offer when its price is
 * quotable (offer.ts productListItemLd), else a plain name + url ListItem: a
 * Product with no offers is an invalid list item to Google, and a stale Offer
 * breaks the freshness rule. null for an empty list.
 */
export function itemListLd(ctx: LdCtx, list: ItemListInput, now: number = Date.now()): JsonLdNode | null {
  const entryUrl = (e: ItemListEntry): string | null =>
    ldUrl(ctx, e.url) ?? (e.asin && isAmazonAsin(e.asin) ? productUrl(ctx, e.asin) : null);
  const items = list.items
    .map((e) => ({ e, url: e ? entryUrl(e) : null }))
    .filter((x): x is { e: ItemListEntry; url: string } => x.url !== null && !!plainText(x.e.name));
  if (!items.length) return null;
  const elements = items.map(({ e, url }, k) => {
    const name = trimNoEllipsis(plainText(e.name), PRODUCT_NAME_MAX);
    if (!e.offer) return { '@type': 'ListItem', position: k + 1, name, url };
    const base: JsonLdNode & { name: string; url: string } = { '@type': 'Product', name, url };
    if (e.asin && isAmazonAsin(e.asin)) {
      base['@id'] = productNodeId(ctx, e.asin);
      base.sku = e.asin;
    }
    const brand = cleanBrand(e.brand);
    if (brand) base.brand = { '@type': 'Brand', name: brand };
    const image = publicImageUrl(ctx, e.image);
    if (image) base.image = image;
    return productListItemLd(ctx, k + 1, base, { ...e.offer, url: ldUrl(ctx, e.offer.url) ?? url }, now);
  });
  const node: JsonLdNode = { '@context': SCHEMA_ORG_CONTEXT, '@type': 'ItemList' };
  const name = collapseWhitespace(list.name);
  if (name) node.name = name;
  if (list.url) node.url = siteUrl(ctx, list.url);
  if (list.order) {
    node.itemListOrder = `https://schema.org/ItemListOrder${list.order[0]?.toUpperCase()}${list.order.slice(1)}`;
  }
  node.numberOfItems = elements.length;
  node.itemListElement = elements;
  return node;
}

// ─── BreadcrumbList ────────────────────────────────────────────────────────

export interface BreadcrumbItem {
  name: string;
  /** Site path or URL; omit for the current page when it should not link. */
  href?: string | null;
}

export interface BreadcrumbOptions {
  /** Prepend the home crumb unless the trail already starts there (default true). */
  home?: boolean;
  homeName?: string;
}

export interface ResolvedCrumb {
  readonly name: string;
  readonly url: string | null;
}

/**
 * The normalised trail both the visible breadcrumb (breadcrumbNavHtml) and
 * BreadcrumbList use, so the two never disagree: home first, names as plain
 * text cut to BREADCRUMB_NAME_MAX, urls absolute on the canonical origin.
 * Every crumb but the last links somewhere: BreadcrumbList requires `item` on
 * all but the last ListItem, so a crumb with no href in the middle of the
 * trail is left out of both (the last crumb, the current page, needs none).
 */
export function breadcrumbTrail(
  ctx: Pick<SiteContext, 'site'>,
  items: readonly BreadcrumbItem[],
  opts: BreadcrumbOptions = {},
): ResolvedCrumb[] {
  const homeUrl = `${ctx.site.canonicalOrigin}/`;
  const resolved: ResolvedCrumb[] = [];
  for (const it of items) {
    const name = trimNoEllipsis(plainText(it?.name), BREADCRUMB_NAME_MAX);
    if (!name) continue;
    resolved.push({ name, url: it.href ? siteUrl(ctx, it.href) : null });
  }
  const startsHome = resolved[0]?.url === homeUrl;
  if ((opts.home ?? true) && !startsHome) resolved.unshift({ name: opts.homeName ?? 'Home', url: homeUrl });
  return resolved.filter((c, k) => c.url !== null || k === resolved.length - 1);
}

/** BreadcrumbList for a trail, or null when it holds fewer than two crumbs. */
export function breadcrumbListLd(
  ctx: Pick<SiteContext, 'site'>,
  items: readonly BreadcrumbItem[],
  opts: BreadcrumbOptions = {},
): JsonLdNode | null {
  const trail = breadcrumbTrail(ctx, items, opts);
  if (trail.length < 2) return null;
  return {
    '@context': SCHEMA_ORG_CONTEXT,
    '@type': 'BreadcrumbList',
    itemListElement: trail.map((c, k) => ({
      '@type': 'ListItem',
      position: k + 1,
      name: c.name,
      ...(c.url ? { item: c.url } : {}),
    })),
  };
}

// ─── FAQPage ───────────────────────────────────────────────────────────────

export interface FaqEntry {
  question: string;
  answer: string;
}

/** The FAQ rows a page shows: plain text, whitespace collapsed, empty rows dropped. */
export function normalizeFaq(rows: readonly FaqEntry[] | null | undefined): FaqEntry[] {
  return (rows ?? [])
    .map((r) => ({ question: htmlToText(r?.question), answer: htmlToText(r?.answer) }))
    .filter((r) => r.question && r.answer);
}

/** FAQPage mirroring exactly the Q&A the page renders; null when there is none. */
export function faqPageLd(rows: readonly FaqEntry[] | null | undefined): JsonLdNode | null {
  const qs = normalizeFaq(rows);
  if (!qs.length) return null;
  return {
    '@context': SCHEMA_ORG_CONTEXT,
    '@type': 'FAQPage',
    mainEntity: qs.map((r) => ({
      '@type': 'Question',
      name: r.question,
      acceptedAnswer: { '@type': 'Answer', text: r.answer },
    })),
  };
}

// ─── Article, Organization, WebSite ────────────────────────────────────────

function isoDate(d: Date | string | number | null | undefined): string | null {
  if (d == null || d === '') return null;
  const t = d instanceof Date ? d : new Date(d);
  return Number.isFinite(t.getTime()) ? t.toISOString() : null;
}

export interface ArticleLdInput {
  headline: string;
  /** The article's page (path or URL on the site). */
  url: string;
  description?: string | null;
  datePublished?: Date | string | null;
  dateModified?: Date | string | null;
  /** Hero image; dropped when it is Amazon-hosted or not https. */
  image?: string | null;
  section?: string | null;
  wordCount?: number | null;
  type?: 'Article' | 'BlogPosting' | 'NewsArticle';
  /** Default: Person when site.json's byline has a jobTitle, else Organization. */
  authorType?: 'Person' | 'Organization';
}

/**
 * Article JSON-LD. The author is site.json's byline (never an invented
 * person) and the publisher the site's Organization. dateModified is never
 * earlier than datePublished.
 */
export function articleLd(ctx: Pick<SiteContext, 'site' | 'byline'>, a: ArticleLdInput): JsonLdNode {
  const url = siteUrl(ctx, a.url);
  const authorType = a.authorType ?? (ctx.byline.jobTitle ? 'Person' : 'Organization');
  const author: JsonLdNode = { '@type': authorType, name: ctx.byline.name };
  if (ctx.byline.profilePath) author.url = siteUrl(ctx, ctx.byline.profilePath);
  if (authorType === 'Person' && ctx.byline.jobTitle) author.jobTitle = ctx.byline.jobTitle;
  const node: JsonLdNode = {
    '@context': SCHEMA_ORG_CONTEXT,
    '@type': a.type ?? 'Article',
    headline: trimNoEllipsis(plainText(a.headline), ARTICLE_HEADLINE_MAX),
    url,
    mainEntityOfPage: { '@type': 'WebPage', '@id': url },
    author,
    publisher: { '@type': 'Organization', '@id': organizationNodeId(ctx), name: ctx.site.brandName, url: `${ctx.site.canonicalOrigin}/` },
    inLanguage: ctx.site.language,
  };
  const description = htmlToText(a.description);
  if (description) node.description = description;
  const published = isoDate(a.datePublished);
  const modified = isoDate(a.dateModified);
  if (published) node.datePublished = published;
  if (modified && (!published || modified >= published)) node.dateModified = modified;
  else if (published) node.dateModified = published;
  const image = publicImageUrl(ctx, a.image);
  if (image) node.image = image;
  const section = collapseWhitespace(a.section);
  if (section) node.articleSection = section;
  if (a.wordCount && Number.isInteger(a.wordCount) && a.wordCount > 0) node.wordCount = a.wordCount;
  return node;
}

export interface OrganizationLdOptions {
  /** Logo (site path or URL); dropped when Amazon-hosted or not https. */
  logo?: { url: string; width?: number; height?: number } | null;
  description?: string | null;
  /** Profiles of the publisher elsewhere (https URLs). */
  sameAs?: readonly string[];
  /** Where the editorial standards live (publishingPrinciples / ethicsPolicy). */
  publishingPrinciplesPath?: string | null;
  /** Topics the publisher covers. */
  knowsAbout?: readonly string[];
}

/**
 * The site's Organization node: who publishes the page, how it reaches a
 * verdict and what it earns (the disclosure page), the provenance assistants
 * weigh before citing a commercial page.
 */
export function organizationLd(ctx: Pick<SiteContext, 'site' | 'disclosure'>, o: OrganizationLdOptions = {}): JsonLdNode {
  const node: JsonLdNode = {
    '@context': SCHEMA_ORG_CONTEXT,
    '@type': 'Organization',
    '@id': organizationNodeId(ctx),
    name: ctx.site.brandName,
    url: `${ctx.site.canonicalOrigin}/`,
  };
  const logo = o.logo ? publicImageUrl(ctx, o.logo.url) : null;
  if (logo && o.logo) {
    node.logo = {
      '@type': 'ImageObject',
      url: logo,
      ...(o.logo.width ? { width: o.logo.width } : {}),
      ...(o.logo.height ? { height: o.logo.height } : {}),
    };
  }
  const description = collapseWhitespace(o.description);
  if (description) node.description = description;
  const sameAs = (o.sameAs ?? []).filter((u) => /^https:\/\/[^\s]+$/.test(u));
  if (sameAs.length) node.sameAs = sameAs;
  const topics = (o.knowsAbout ?? []).map((t) => collapseWhitespace(t)).filter(Boolean);
  if (topics.length) node.knowsAbout = topics;
  if (o.publishingPrinciplesPath) {
    const principles = siteUrl(ctx, o.publishingPrinciplesPath);
    node.publishingPrinciples = principles;
    node.ethicsPolicy = principles;
  }
  node.subjectOf = { '@type': 'WebPage', name: 'Affiliate disclosure', url: siteUrl(ctx, ctx.disclosure.pagePath) };
  return node;
}

export interface WebSiteLdOptions {
  /** Site search path holding '{search_term_string}', e.g. '/search?q={search_term_string}'. */
  searchPath?: string | null;
}

/** The WebSite node, published by the site's Organization. */
export function webSiteLd(ctx: Pick<SiteContext, 'site'>, o: WebSiteLdOptions = {}): JsonLdNode {
  const node: JsonLdNode = {
    '@context': SCHEMA_ORG_CONTEXT,
    '@type': 'WebSite',
    '@id': webSiteNodeId(ctx),
    name: ctx.site.brandName,
    url: `${ctx.site.canonicalOrigin}/`,
    inLanguage: ctx.site.language,
    publisher: { '@id': organizationNodeId(ctx) },
  };
  if (o.searchPath && o.searchPath.includes('{search_term_string}') && o.searchPath.startsWith('/')) {
    node.potentialAction = {
      '@type': 'SearchAction',
      target: { '@type': 'EntryPoint', urlTemplate: `${ctx.site.canonicalOrigin}${o.searchPath}` },
      'query-input': 'required name=search_term_string',
    };
  }
  return node;
}

// ─── serialization ─────────────────────────────────────────────────────────

/** A non-array object (what a JSON-LD node is). scrubLd() turns any object into its JSON form first. */
function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

/** A schema.org term without its vocabulary prefix ("schema:", "http(s)://(www.)schema.org/"), lower-cased. */
function schemaTerm(s: string): string {
  return s.replace(/^(?:https?:\/\/(?:www\.)?schema\.org\/|schema:)/i, '').toLowerCase();
}

function isAggregateRating(v: unknown): boolean {
  if (!isPlainObject(v)) return false;
  const t = v['@type'];
  return (Array.isArray(t) ? t : [t]).some((x) => typeof x === 'string' && schemaTerm(x) === 'aggregaterating');
}

/** Nesting deeper than this is not structured data a page needs; such a subtree is dropped unchecked rather than published. */
const MAX_LD_DEPTH = 32;

/**
 * Properties that hold an image. On a page they must never point at an
 * Amazon host (IP License 2(h)); the builders already refuse one, and
 * serializeJsonLd() also drops one from a hand-built node.
 */
const IMAGE_PROPERTIES: ReadonlySet<string> = new Set(['image', 'logo', 'thumbnail', 'thumbnailurl', 'photo', 'primaryimageofpage']);

/** An Amazon-hosted image value: a URL, or an ImageObject whose url, contentUrl or thumbnailUrl is one. */
function isAmazonImageValue(v: unknown): boolean {
  if (typeof v === 'string') return isAmazonHostedUrl(v);
  if (!isPlainObject(v)) return false;
  return ['url', 'contentUrl', 'thumbnailUrl'].some((k) => typeof v[k] === 'string' && isAmazonHostedUrl(v[k] as string));
}

/**
 * The value JSON.stringify would serialise for `v` at `key`, with every
 * AggregateRating removed (as a property, compact or IRI, and as a typed
 * node), Amazon-hosted images removed from image properties, and subtrees
 * nested past MAX_LD_DEPTH dropped. undefined means "omit".
 */
function scrubLd(v: unknown, key: string, depth: number, viaToJSON = false): unknown {
  if (depth > MAX_LD_DEPTH) return undefined;
  if (typeof v !== 'object' || v === null) return v;
  // As JSON.stringify does: call toJSON once (a Date becomes its ISO string, a URL its href), unwrap boxed primitives.
  const toJSON = (v as { toJSON?: unknown }).toJSON;
  if (!viaToJSON && typeof toJSON === 'function') {
    return scrubLd((toJSON as (k: string) => unknown).call(v, key), key, depth, true);
  }
  if (v instanceof Number || v instanceof String || v instanceof Boolean) return v.valueOf();
  // Every check below runs on the JSON form, so a toJSON cannot smuggle a rating or an Amazon image past it.
  if (Array.isArray(v)) {
    return v.map((x, k) => scrubLd(x, String(k), depth + 1)).filter((x) => x !== undefined && !isAggregateRating(x));
  }
  const out: Record<string, unknown> = {};
  for (const [k, raw] of Object.entries(v as Record<string, unknown>)) {
    const term = schemaTerm(k);
    if (term === 'aggregaterating') continue;
    let clean = scrubLd(raw, k, depth + 1);
    if (clean === undefined || isAggregateRating(clean)) continue;
    if (IMAGE_PROPERTIES.has(term)) {
      if (Array.isArray(clean)) {
        clean = clean.filter((x) => !isAmazonImageValue(x));
        if (!(clean as unknown[]).length) continue;
      } else if (isAmazonImageValue(clean)) {
        continue;
      }
    }
    out[k] = clean;
  }
  return out;
}

/**
 * A deep copy of `node`, as JSON would carry it, with every aggregateRating
 * property and every AggregateRating-typed node removed (compact names and
 * full schema.org IRIs alike), Amazon-hosted images removed from image
 * properties, and anything nested deeper than 32 levels dropped. The kit
 * never emits a rating; this makes sure no caller-supplied fragment smuggles
 * one in. Values with a toJSON (Date, URL) become what JSON.stringify makes of
 * them. The input is not mutated. `depth` is how deep `node` already sits.
 */
export function stripAggregateRatings<T>(node: T, depth = 0): T {
  return scrubLd(node, '', depth) as T;
}

/** True when a root node can be published: a plain object with @type or a @graph. */
export function isPublishableNode(node: unknown): node is JsonLdNode {
  return isPlainObject(node) && (node['@type'] != null || Array.isArray(node['@graph']));
}

/**
 * JSON for a <script type="application/ld+json"> block: @context added when
 * missing, ratings and Amazon-hosted images stripped (stripAggregateRatings),
 * and '<', '>', '&', U+2028 and U+2029 escaped, so no string value can close
 * the script element or open a comment.
 */
export function serializeJsonLd(node: JsonLdNode): string {
  const scrubbed: unknown = stripAggregateRatings(node);
  const root: JsonLdNode = isPlainObject(scrubbed) ? scrubbed : {};
  const withContext = root['@context'] ? root : { '@context': SCHEMA_ORG_CONTEXT, ...root };
  return JSON.stringify(withContext)
    .replace(/</g, '\\u003c')
    .replace(/>/g, '\\u003e')
    .replace(/&/g, '\\u0026')
    .replace(/\u2028/g, '\\u2028')
    .replace(/\u2029/g, '\\u2029');
}

/**
 * <script type="application/ld+json"> blocks for the given nodes, one per
 * node, joined by newlines. null/false entries and nodes without @type (or
 * @graph) are skipped.
 */
export function jsonLdScripts(nodes: ReadonlyArray<JsonLdNode | null | undefined | false>): string {
  return nodes
    .filter(isPublishableNode)
    .map((n) => `<script type="application/ld+json">${serializeJsonLd(n)}</script>`)
    .join('\n');
}
