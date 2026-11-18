/**
 * GET /r/:asin: the click-logging redirect to Amazon.
 *
 * Ported from SpecPicks src/index.ts (`app.get('/r/:key', ...)`, the ASIN
 * path; see unit/http/fixtures/ported-from-http.json). It gives the site its
 * own attribution for links that go through it, independent of Amazon's
 * 48h-delayed reports: the tracking ID is chosen by the kind of page the
 * click came from (pickTrackingId over classifySource of the Referer), the
 * hit is logged to outbound_clicks (via='redirect') with its bot verdict, and
 * the reader gets a 302. Every hit is recorded, crawlers included: is_bot and
 * bot_reason carry the verdict so a heuristic change can be replayed over
 * history; readers filter `WHERE NOT is_bot`.
 *
 * Where the reader goes (redirectDecision):
 *   - not buyable (anything but buyable === true: an active recall, a hold,
 *     or a lookup that did not say) -> our own product page, which explains
 *     why; nothing is logged as an Amazon click.
 *   - inactive and flagged as no longer sold on Amazon -> a tagged Amazon
 *     SEARCH for it, never its dead /dp/ page (upstream audit F045).
 *   - otherwise -> the fresh Creators detailPageURL for this ASIN, re-tagged
 *     and host-checked, else the plain tagged /dp/ link.
 *
 * Kit changes: Amazon only (no eBay routing, no dashboard short codes); the
 * catalog lookup is the site's (RedirectLookup); a lookup failure sends the
 * reader to our product page instead of a 500; responses are no-store and
 * noindex. Clicks the kit renders itself (cta()) link to Amazon directly and
 * are counted by the beacon; /r/ serves links written into content.
 */
import type { RequestHandler } from 'express';
import { classifyClick, countryFromHeaders, deviceType } from '../botDetect';
import { amazonUrl, classifySource, isAmazonAsin, pickTrackingId, productPath, retagAmazonUrl } from '../links';
import { amazonSearchUrlFor, inactiveReason, productSearchTerms } from '../productAvailability';
import type { SiteContext } from '../siteContext';
import { BEACON_LIMITS } from './clientContract';
import { clipText, headerValue, type KitRuntime } from './runtime';
import type { OutboundClickRow, RedirectLookup, RedirectProduct } from './types';

/** The route, for app.get(). */
export const REDIRECT_ROUTE = '/r/:asin';

export type RedirectDecision =
  | { readonly kind: 'amazon-product'; readonly url: string; readonly reason: 'active' | 'inactive' }
  | { readonly kind: 'amazon-search'; readonly url: string; readonly reason: 'unavailable-on-amazon' }
  | { readonly kind: 'site'; readonly url: string; readonly reason: 'not-buyable' | 'no-search-terms' };

type DecisionCtx = Pick<SiteContext, 'amazon' | 'routes'>;

const DETAIL_PATH_ASIN_RE = /\/(?:dp|gp\/product|gp\/aw\/d)\/([A-Z0-9]{10})(?=[/?#]|$)/i;

/**
 * A stored detail-page URL re-tagged for this click, or '' when it is not an
 * https URL on our marketplace or names a different ASIN than the one asked
 * for (a mismatched URL would send the reader to the wrong product).
 */
export function detailPageUrlFor(ctx: Pick<SiteContext, 'amazon'>, url: string | null | undefined, asin: string, tag: string): string {
  if (!url) return '';
  const tagged = retagAmazonUrl(ctx, url, tag);
  if (!tagged) return '';
  const m = new URL(tagged).pathname.match(DETAIL_PATH_ASIN_RE);
  return m && (m[1] as string).toUpperCase() === asin ? tagged : '';
}

/** Where /r/<asin> sends a reader, for a product the catalog lookup found. */
export function redirectDecision(ctx: DecisionCtx, product: RedirectProduct, trackingId: string): RedirectDecision {
  const asin = product.asin;
  // Fail closed: only an explicit true lets a reader through to Amazon.
  if (product.buyable !== true) return { kind: 'site', url: productPath(ctx, asin), reason: 'not-buyable' };
  if (!product.isActive && inactiveReason(product.qualityFlags) === 'unavailable-on-amazon') {
    if (!productSearchTerms(product.title, product.brand)) {
      return { kind: 'site', url: productPath(ctx, asin), reason: 'no-search-terms' };
    }
    return {
      kind: 'amazon-search',
      url: amazonSearchUrlFor(ctx, product.title, product.brand, trackingId),
      reason: 'unavailable-on-amazon',
    };
  }
  const url = detailPageUrlFor(ctx, product.detailPageUrl, asin, trackingId) || amazonUrl(ctx, asin, trackingId);
  return { kind: 'amazon-product', url, reason: product.isActive ? 'active' : 'inactive' };
}

/**
 * The path of the page a click came from. Relative Referers resolve against
 * the canonical origin (Node's URL() needs a base). A foreign Referer keeps
 * its own path, as upstream: a real browser arriving from another site sends
 * Sec-Fetch-Site: cross-site, which classifyClick already judges first.
 */
export function refererPath(referer: string | null | undefined, ctx: Pick<SiteContext, 'site'>): string | undefined {
  if (!referer) return undefined;
  try {
    return new URL(referer, `${ctx.site.canonicalOrigin}/`).pathname;
  } catch {
    return undefined;
  }
}

/** The handler; mount with app.get(REDIRECT_ROUTE, redirectHandler(rt, lookup)). */
export function redirectHandler(rt: KitRuntime, lookup: RedirectLookup): RequestHandler {
  const ctx = rt.ctx;
  const handle = async (req: Parameters<RequestHandler>[0], res: Parameters<RequestHandler>[1]): Promise<void> => {
    res.set('Cache-Control', 'private, no-store');
    res.set('X-Robots-Tag', 'noindex, nofollow');
    const asin = String(req.params.asin || '');
    if (!isAmazonAsin(asin)) {
      res.status(404).type('text/plain').send('Product not found');
      return;
    }
    let product: RedirectProduct | null;
    try {
      product = await lookup(asin);
    } catch (e) {
      // Fail closed but useful: never guess an Amazon destination the lookup
      // could not vouch for (it carries the recall gate), send the reader to
      // our page about the product instead.
      rt.log.error(`[affiliate-kit] /r/${asin} lookup failed: ${e instanceof Error ? e.message : String(e)}`);
      res.redirect(302, productPath(ctx, asin));
      return;
    }
    if (!product) {
      res.status(404).type('text/plain').send('Product not found');
      return;
    }
    try {
      const refPath = refererPath(headerValue(req.headers, 'referer'), ctx);
      const sourceKind = classifySource(ctx, refPath);
      const trackingId = pickTrackingId(ctx, sourceKind);
      const decision = redirectDecision(ctx, { ...product, asin }, trackingId);
      if (decision.kind !== 'site') {
        const ua = clipText(headerValue(req.headers, 'user-agent'), BEACON_LIMITS.userAgent);
        const ip = rt.clientIp(req);
        const acceptLanguage = headerValue(req.headers, 'accept-language');
        const verdict = classifyClick(ua, refPath, ip, {
          acceptLanguage,
          secFetchSite: headerValue(req.headers, 'sec-fetch-site'),
          secChUa: headerValue(req.headers, 'sec-ch-ua'),
        });
        const row: OutboundClickRow = {
          siteId: ctx.site.id,
          asin,
          trackingId,
          sourcePage: clipText(refPath, BEACON_LIMITS.page) ?? '',
          sourceKind,
          target: 'amazon',
          ipAddress: ip || null,
          userAgent: ua,
          isBot: verdict.isBot,
          botReason: verdict.reason,
          country: countryFromHeaders(req.headers as Record<string, string | string[] | undefined>),
          deviceType: deviceType(ua),
          via: 'redirect',
          acceptLanguage: clipText(acceptLanguage, BEACON_LIMITS.acceptLanguage),
          sessionPageviews: null,
          msOnPage: null,
          placement: null,
        };
        rt.writer.submit('outbound_clicks redirect', () => rt.db.insertOutboundClick(row));
      }
      res.redirect(302, decision.url);
    } catch (e) {
      rt.log.error(`[affiliate-kit] /r/${asin} failed: ${e instanceof Error ? e.message : String(e)}`);
      if (!res.headersSent) res.redirect(302, productPath(ctx, asin));
    }
  };
  // Express 4 does not await handlers: hand anything unexpected to the error chain.
  return (req, res, next) => {
    handle(req, res).catch((e: unknown) => {
      rt.log.error(`[affiliate-kit] /r/ handler error: ${e instanceof Error ? e.message : String(e)}`);
      if (!res.headersSent) next(e);
    });
  };
}
