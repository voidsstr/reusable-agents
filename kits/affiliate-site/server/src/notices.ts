/**
 * The notices Amazon requires, verbatim.
 *
 * Wording checked character for character against the live pages on
 * 2026-11-17 (NOTICES_VERIFIED_ON):
 *   - Associates Program Policies, Product Advertising License section 2(i)
 *     and 2(k): https://affiliate-program.amazon.com/help/operating/policies
 *   - Operating Agreement section 5: https://affiliate-program.amazon.com/help/operating/agreement
 *
 * 2(i): when prices/availability are refreshed less often than hourly (the
 * kit's refresh runs every few hours), every price or availability display
 * needs an adjacent date/time stamp AND this disclaimer, "adjacent to the
 * pricing or availability information or provide[d] via a hyperlink, pop-up
 * box, scripted pop-up, or other similar method" -- Amazon's own example puts
 * it behind "Details". price.ts renders every stamp with it.
 *
 * 2(k): wherever Amazon text (e.g. a title) is displayed, this notice must be
 * "in plain view". Note the policy's curly quotes around AS IS (U+2018 and
 * U+2019); the constant keeps them so the rendered text matches exactly.
 *
 * Nothing here is configurable on purpose: a site may choose WHERE the notice
 * renders, never what it says. The Amazon Site name in 2(i) comes from
 * SiteContext (amazon.siteName, 'Amazon.com' for the US store).
 */
import { esc } from './links';
import type { SiteContext } from './siteContext';

export const AMAZON_POLICIES_URL = 'https://affiliate-program.amazon.com/help/operating/policies';
export const AMAZON_OPERATING_AGREEMENT_URL = 'https://affiliate-program.amazon.com/help/operating/agreement';
export const NOTICES_VERIFIED_ON = '2026-11-17';

/** The bracketed slot in the 2(i) sentence that the site fills with its Amazon Site name. */
export const AMAZON_SITE_PLACEHOLDER = '[relevant Amazon Site(s), as applicable]';

/** IP License 2(i) disclaimer exactly as the policy prints it, placeholder included. */
export const PRICE_AVAILABILITY_DISCLAIMER_TEMPLATE =
  'Product prices and availability are accurate as of the date/time indicated and are subject to change. '
  + 'Any price and availability information displayed on [relevant Amazon Site(s), as applicable] at the time '
  + 'of purchase will apply to the purchase of this product.';

/** IP License 2(k) notice exactly as the policy prints it, placeholder included. */
export const AMAZON_CONTENT_NOTICE_TEMPLATE =
  'CERTAIN CONTENT THAT APPEARS [IN THIS APPLICATION or ON THIS SITE, as applicable] COMES FROM AMAZON. '
  + 'THIS CONTENT IS PROVIDED ‘AS IS’ AND IS SUBJECT TO CHANGE OR REMOVAL AT ANY TIME.';

/** IP License 2(k) notice for a website (the "ON THIS SITE" alternative). */
export const AMAZON_CONTENT_NOTICE =
  'CERTAIN CONTENT THAT APPEARS ON THIS SITE COMES FROM AMAZON. '
  + 'THIS CONTENT IS PROVIDED ‘AS IS’ AND IS SUBJECT TO CHANGE OR REMOVAL AT ANY TIME.';

/** Operating Agreement section 5 statement (a site may use a substantially similar one; see SiteContext). */
export const ASSOCIATE_DISCLOSURE = 'As an Amazon Associate I earn from qualifying purchases.';

/** The 2(i) disclaimer naming one Amazon Site, e.g. 'Amazon.com'. */
export function priceAvailabilityDisclaimer(amazonSiteName: string): string {
  const name = String(amazonSiteName || '').trim();
  if (!name) throw new Error('priceAvailabilityDisclaimer: an Amazon Site name is required');
  return PRICE_AVAILABILITY_DISCLAIMER_TEMPLATE.replace(AMAZON_SITE_PLACEHOLDER, name);
}

/** The 2(i) disclaimer for this site's marketplace. */
export function priceDisclaimerFor(ctx: Pick<SiteContext, 'amazon'>): string {
  return priceAvailabilityDisclaimer(ctx.amazon.siteName);
}

/**
 * The 2(k) notice as a plain-view block. Render it on every page that shows
 * Amazon text (titles included). data-amazon-notice lets the compliance spec
 * find it; data-nosnippet keeps it out of search snippets.
 */
export function amazonContentNoticeHtml(className = 'kit-amazon-notice'): string {
  return `<p class="${esc(className)}" data-amazon-notice="2k" data-nosnippet>${esc(AMAZON_CONTENT_NOTICE)}</p>`;
}

/**
 * The site's earnings disclosure (Operating Agreement section 5, and the FTC's
 * clear-and-conspicuous rule), linked to the full disclosure page. Place it
 * before the first affiliate link a reader meets.
 */
export function earningsDisclosureHtml(
  ctx: Pick<SiteContext, 'disclosure' | 'site'>,
  className = 'kit-earnings-disclosure',
): string {
  const href = `${ctx.site.canonicalOrigin}${ctx.disclosure.pagePath}`;
  return `<p class="${esc(className)}" data-earnings-disclosure><a href="${esc(href)}">${esc(ctx.disclosure.earnings)}</a></p>`;
}

/** Collapse whitespace the way rendered text does, for notice comparisons. */
export function normalizeNoticeText(s: string): string {
  return String(s || '').replace(/\s+/g, ' ').trim();
}

/** True when `text` (e.g. a page's textContent) contains `notice` verbatim, modulo whitespace. */
export function containsNotice(text: string, notice: string): boolean {
  return normalizeNoticeText(text).includes(normalizeNoticeText(notice));
}
