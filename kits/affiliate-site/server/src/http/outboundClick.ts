/**
 * POST /api/outbound-click: the verified-human affiliate click beacon.
 *
 * Ported from SpecPicks src/index.ts (see unit/http/fixtures/ported-from-http.json).
 * Upstream rationale, kept because it is why the endpoint exists:
 *
 *   Most Amazon CTAs are plain <a href="https://www.amazon.com/dp/...?tag=...">
 *   anchors rendered by SSR, so a click on them never touches this server and
 *   /r/ cannot see it. The delegated click listener (client/src/clickBeacon.ts)
 *   sendBeacon()s here for every one of them, so every click that happened in
 *   a real, JS-executing browser tab lands in outbound_clicks with
 *   via='beacon' plus the automation fingerprints only the page knows
 *   (navigator.webdriver, page views this session, ms on page). Crawlers that
 *   walk /r/ URLs never run the listener, so beacon rows are the "verified
 *   human" series (the amazon_clicks_verified_human_daily view, kit_0004).
 *
 *   Always 204: the response is never read and must not leak the verdict.
 *
 * Kit changes:
 *   - Amazon only (eBay beacons are ignored), site id and source rules from
 *     SiteContext.
 *   - The client address is K1's siteClientIp() (the right-most public
 *     X-Forwarded-For hop, one CDN hop skipped when site.json `edge` names
 *     one), not the first entry, which the client chooses: that let a
 *     scraper dodge the datacenter-IP rule and the per-IP velocity cut by
 *     sending a residential address in the header.
 *   - placement must be a valid placement name (kebab-case, at most 40
 *     chars) or it is stored as NULL; text fields lose control characters;
 *     numbers are clamped to the INT columns' range. Upstream lost the whole
 *     row on an out-of-range number or a NUL byte.
 *   - classified_at is stamped at insert (pgKitDb), since the verdict is
 *     computed here.
 *   - A body the parser rejects (too big, bad encoding) still gets 204.
 */
import express, { type ErrorRequestHandler, type RequestHandler } from 'express';
import type { IncomingHttpHeaders } from 'http';
import { classifyClick, countryFromHeaders, deviceType } from '../botDetect';
import { classifySource, isAmazonAsin, PLACEMENT_RE } from '../links';
import { BEACON_LIMITS, BEACON_MAX_BYTES, BEACON_MERCHANT } from './clientContract';
import { clipText, headerValue, type KitRuntime } from './runtime';
import type { OutboundClickRow } from './types';

/** What the client sends (client/src/clickBeacon.ts), after validation. */
export interface BeaconPayload {
  readonly merchant: 'amazon';
  /** A real ASIN, or null when the link did not name one. */
  readonly asin: string | null;
  /** Path of the page the click happened on ('' when absent). */
  readonly page: string;
  readonly placement: string | null;
  /** The tag on the clicked link (stored as sent: a foreign tag is worth seeing). */
  readonly tag: string | null;
  /** Page views in this tab's session; null when not sent. */
  readonly pv: number | null;
  /** Milliseconds between the beacon script loading and the click; null when not sent. */
  readonly ms: number | null;
  /** navigator.webdriver was true. */
  readonly wd: boolean;
}

const INT_MAX = 2147483647;
const INT_MIN = -2147483648;

function intOrNull(v: unknown): number | null {
  if (typeof v !== 'number' || !Number.isFinite(v)) return null;
  return Math.min(INT_MAX, Math.max(INT_MIN, Math.round(v)));
}

/**
 * Validate a beacon body (JSON text, or an object when a JSON body parser ran
 * first). Null for anything that is not an Amazon click.
 */
export function parseBeaconPayload(body: unknown): BeaconPayload | null {
  let b: unknown = body;
  if (typeof b === 'string') {
    try {
      b = JSON.parse(b || '{}');
    } catch {
      return null;
    }
  } else if (Buffer.isBuffer(b)) {
    try {
      b = JSON.parse(b.toString('utf8') || '{}');
    } catch {
      return null;
    }
  }
  if (!b || typeof b !== 'object' || Array.isArray(b)) return null;
  const o = b as Record<string, unknown>;
  if (o.merchant !== BEACON_MERCHANT) return null;
  const asin = typeof o.asin === 'string' && isAmazonAsin(o.asin) ? o.asin : null;
  const page = typeof o.page === 'string' && o.page.startsWith('/') ? clipText(o.page, BEACON_LIMITS.page) ?? '' : '';
  const placement = typeof o.placement === 'string' && PLACEMENT_RE.test(o.placement) ? o.placement : null;
  return {
    merchant: 'amazon',
    asin,
    page,
    placement,
    tag: clipText(o.tag, BEACON_LIMITS.tag),
    pv: intOrNull(o.pv),
    ms: intOrNull(o.ms),
    wd: o.wd === true,
  };
}

/** The outbound_clicks row for one beacon, with its bot verdict. */
export function beaconClickRow(
  rt: Pick<KitRuntime, 'ctx'>,
  p: BeaconPayload,
  req: { headers: IncomingHttpHeaders },
  ip: string,
): OutboundClickRow {
  const h = req.headers;
  const ua = clipText(headerValue(h, 'user-agent'), BEACON_LIMITS.userAgent);
  const acceptLanguage = headerValue(h, 'accept-language');
  const verdict = classifyClick(ua, p.page, ip, {
    acceptLanguage,
    secFetchSite: headerValue(h, 'sec-fetch-site'),
    secChUa: headerValue(h, 'sec-ch-ua'),
    // Upstream: a beacon that does not report page views counts as zero.
    sessionPageviews: p.pv ?? 0,
    msOnPage: p.ms,
    webdriver: p.wd,
  });
  return {
    siteId: rt.ctx.site.id,
    asin: p.asin || '',
    trackingId: p.tag,
    sourcePage: p.page,
    sourceKind: classifySource(rt.ctx, p.page || undefined),
    target: 'amazon',
    ipAddress: ip || null,
    userAgent: ua,
    isBot: verdict.isBot,
    botReason: verdict.reason,
    country: countryFromHeaders(h as Record<string, string | string[] | undefined>),
    deviceType: deviceType(ua),
    via: 'beacon',
    acceptLanguage: clipText(acceptLanguage, BEACON_LIMITS.acceptLanguage),
    sessionPageviews: p.pv,
    msOnPage: p.ms,
    placement: p.placement,
  };
}

/**
 * The beacon route as [body parser, handler, parser-error handler]; mount
 * with app.post(BEACON_PATH, ...outboundClickHandlers(rt)).
 */
export function outboundClickHandlers(rt: KitRuntime): [RequestHandler, RequestHandler, ErrorRequestHandler] {
  const parse = express.text({ type: '*/*', limit: BEACON_MAX_BYTES });
  const handle: RequestHandler = (req, res) => {
    res.set('Cache-Control', 'no-store');
    res.status(204).end();
    try {
      const p = parseBeaconPayload(req.body);
      if (!p) return;
      const row = beaconClickRow(rt, p, req, rt.clientIp(req));
      rt.writer.submit('outbound_clicks beacon', () => rt.db.insertOutboundClick(row));
    } catch (e) {
      rt.log.error(`[affiliate-kit] outbound-click beacon failed: ${e instanceof Error ? e.message : String(e)}`);
    }
  };
  const onParseError: ErrorRequestHandler = (_err, _req, res, _next) => {
    if (res.headersSent) return;
    res.set('Cache-Control', 'no-store');
    res.status(204).end();
  };
  return [parse, handle, onParseError];
}
