/**
 * site.json `redirectFromHosts`: the site's other names answer with a
 * permanent redirect to the canonical origin.
 *
 * A request whose Host header names one of those hosts gets a 301 to
 * canonicalOrigin + the request's own path and query, byte for byte (what the
 * client sent, not a decoded or normalised form), so every old URL lands on
 * its new address and search engines move each page's signals with it. That
 * is how a site leaves a temporary platform address (or how www folds into
 * the apex) without losing a link.
 *
 * Served as is on every host (never redirected):
 *   - the paths mountKit passes in `keepPaths`: /health (the platform's
 *     probe), /robots.txt and the IndexNow key file;
 *   - site-verification files: /google*.html (Search Console),
 *     /BingSiteAuth.xml (Bing Webmaster Tools) and anything under
 *     /.well-known/ (ACME certificate challenges, security.txt, ...).
 * The verification files and the key file have to answer on the old host
 * itself: ownership checks and IndexNow notices about the old URLs are made
 * per host. robots.txt is served too, not redirected: RFC 9309 asks crawlers
 * to follow up to five robots.txt redirects, but one that does not falls
 * back to "no robots.txt" (crawl everything, training crawlers included),
 * and the same policy served on both hosts keeps every crawler free to fetch
 * the old URLs and see their 301s (blocking them would freeze the old URLs in
 * the index instead). On a redirected host these files also carry
 * X-Robots-Tag: noindex: nothing on that host is meant to be indexed.
 *
 * Only GET and HEAD are redirected. A 301 tells a browser to repeat a POST as
 * a GET, which would silently lose a click beacon sent from a page still open
 * on the old host, so other methods are served as is (and every page a
 * reader could still be on is already on the canonical host).
 *
 * The redirect is cacheable for a day (HOST_REDIRECT_CACHE_CONTROL), not
 * forever: a 301 without a lifetime is kept by browsers indefinitely, which
 * would make a host's redirect impossible to take back. Mount the middleware
 * after the AI-traffic logger (mountKit does), so assistants that still
 * fetch old URLs are logged with the 301 they got.
 */
import type { RequestHandler } from 'express';
import type { SiteContext } from '../siteContext';
import { ensureNoindexHeader } from './indexing';
import { urlPath } from './runtime';

/** Paths every host serves itself: site-verification files and RFC 8615 well-known URIs. */
export const HOST_REDIRECT_VERIFICATION_PATHS: readonly RegExp[] = Object.freeze([
  /^\/google[A-Za-z0-9_.-]*\.html$/,
  /^\/BingSiteAuth\.xml$/i,
  /^\/\.well-known\//,
]);

/** Cache-Control of a host redirect: a day, so the redirect can be taken back. */
export const HOST_REDIRECT_CACHE_CONTROL = 'public, max-age=86400';

export interface HostRedirectOptions {
  /** Exact paths every host serves as is (mountKit: /health, /robots.txt, the IndexNow key file). */
  keepPaths: readonly string[];
}

/** The request facts the redirect decision reads (an express Request fits). */
export interface HostRedirectRequest {
  method: string;
  url: string;
  originalUrl?: string;
  headers: { host?: string | undefined };
}

/**
 * The host name a Host header names: lower case, without the port or a
 * trailing dot; null for an IP literal in brackets, an empty or a malformed one.
 */
export function requestHostname(hostHeader: string | undefined | null): string | null {
  const raw = String(hostHeader ?? '').trim().toLowerCase();
  if (!raw || raw.startsWith('[')) return null;
  const m = /^([a-z0-9.-]+?)\.?(?::\d{1,5})?$/.exec(raw);
  return m ? (m[1] as string) : null;
}

/** The path and query of a request target ('/a?b=1'), also from the absolute form ('http://h/a?b=1'). */
function pathAndQuery(target: string): string {
  if (target.startsWith('/')) return target;
  try {
    const u = new URL(target);
    return `${u.pathname}${u.search}`;
  } catch {
    return '/';
  }
}

/** True when `path` is served as is on every host. */
export function servedOnEveryHost(path: string, keepPaths: readonly string[]): boolean {
  return keepPaths.includes(path) || HOST_REDIRECT_VERIFICATION_PATHS.some((re) => re.test(path));
}

/**
 * Where a request must be redirected to (canonicalOrigin + its path and
 * query), or null when it is served as is: a host not in redirectFromHosts, a
 * method other than GET and HEAD, or a path every host serves.
 */
export function hostRedirectLocation(
  ctx: Pick<SiteContext, 'site' | 'redirectFromHosts'>,
  req: HostRedirectRequest,
  keepPaths: readonly string[],
): string | null {
  if (!ctx.redirectFromHosts.length) return null;
  const method = String(req.method || '').toUpperCase();
  if (method !== 'GET' && method !== 'HEAD') return null;
  const host = requestHostname(req.headers.host);
  if (!host || !ctx.redirectFromHosts.includes(host)) return null;
  const target = req.originalUrl || req.url || '/';
  if (servedOnEveryHost(urlPath(pathAndQuery(target)), keepPaths)) return null;
  return `${ctx.site.canonicalOrigin}${pathAndQuery(target)}`;
}

/** The middleware: a 301 to the canonical origin for redirectFromHosts (see the file comment). */
export function hostRedirectMiddleware(ctx: Pick<SiteContext, 'site' | 'redirectFromHosts'>, opts: HostRedirectOptions): RequestHandler {
  const keepPaths = [...opts.keepPaths];
  return (req, res, next) => {
    const location = hostRedirectLocation(ctx, req, keepPaths);
    if (location) {
      res.set('Cache-Control', HOST_REDIRECT_CACHE_CONTROL);
      res.redirect(301, location);
      return;
    }
    const host = requestHostname(req.headers.host);
    if (host && ctx.redirectFromHosts.includes(host)) ensureNoindexHeader(res);
    next();
  };
}
