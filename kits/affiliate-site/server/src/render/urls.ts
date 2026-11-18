/**
 * URLs on the site's own origin, and the public image URLs the render helpers
 * may emit.
 *
 * Every URL a page puts in its head, structured data or sitemaps is absolute
 * on ctx.site.canonicalOrigin; a site path or a www/apex variant of the
 * domain is normalised to it. A URL on any other host is a programming error
 * for the canonical helpers (they throw), and simply "not ours" for the
 * predicates.
 *
 * publicImageUrl() is the one gate for an image a page publishes outside its
 * <img> tags (og:image, Product.image, Article.image, sitemap <image:loc>,
 * an Organization logo): https only, and never an Amazon-owned host (IP
 * License 2(h); binding amendment 2).
 */
import type { SiteContext } from '../siteContext';
import { isAmazonOwnedHost } from './amazonHosts';

type SiteCtx = Pick<SiteContext, 'site'>;

/** Query parameters that never belong in a canonical URL (tracking, affiliate tags). */
export const TRACKING_PARAM_RE = /^(?:utm_[a-z0-9_]*|tag|fbclid|gclid|gbraid|wbraid|dclid|msclkid|mc_[a-z]+|_ga|_gl|ref|ref_|igshid|yclid)$/i;

/** True when `host` is the site's domain, its www variant, or the canonical origin's host. */
export function isSiteHost(ctx: SiteCtx, host: string | null | undefined): boolean {
  const h = String(host ?? '').toLowerCase();
  if (!h) return false;
  const d = ctx.site.domain.toLowerCase();
  return h === d || h === `www.${d}` || h === new URL(ctx.site.canonicalOrigin).hostname;
}

function parseOnSite(ctx: SiteCtx, pathOrUrl: string, fn: string): URL {
  const raw = String(pathOrUrl ?? '').trim();
  let u: URL;
  try {
    u = new URL(raw || '/', `${ctx.site.canonicalOrigin}/`);
  } catch {
    throw new TypeError(`${fn}: not a URL or site path: ${JSON.stringify(pathOrUrl)}`);
  }
  if ((u.protocol !== 'https:' && u.protocol !== 'http:') || !isSiteHost(ctx, u.hostname) || u.username || u.password) {
    throw new TypeError(`${fn}: ${JSON.stringify(pathOrUrl)} is not on ${ctx.site.canonicalOrigin}`);
  }
  return u;
}

function normalizePath(p: string): string {
  const collapsed = p.replace(/\/{2,}/g, '/');
  return collapsed.length > 1 ? collapsed.replace(/\/+$/, '') || '/' : collapsed || '/';
}

/**
 * Absolute URL on the canonical origin for a site path or a URL on the site's
 * host, query kept, fragment dropped, repeated and trailing slashes removed.
 * Throws a TypeError for another host or a non-http(s) URL.
 */
export function siteUrl(ctx: SiteCtx, pathOrUrl: string): string {
  const u = parseOnSite(ctx, pathOrUrl, 'siteUrl');
  return `${ctx.site.canonicalOrigin}${normalizePath(u.pathname)}${u.search}`;
}

export interface CanonicalOptions {
  /**
   * Query parameters a canonical URL keeps (e.g. ['page'] for paginated
   * archives); 'all' keeps every parameter except tracking ones. Default:
   * none, so filter and sort variants canonicalise to the base page.
   */
  keepParams?: readonly string[] | 'all';
}

/**
 * The canonical URL of a page: absolute on the canonical origin, no fragment,
 * no tracking parameters, only the kept parameters (sorted, empty values
 * dropped), no trailing slash except the root. Throws a TypeError for a URL
 * on another host.
 */
export function canonicalUrl(ctx: SiteCtx, pathOrUrl: string, opts: CanonicalOptions = {}): string {
  const u = parseOnSite(ctx, pathOrUrl, 'canonicalUrl');
  const keep = opts.keepParams ?? [];
  const keepSet = keep === 'all' ? null : new Set(keep.map((k) => k.toLowerCase()));
  const params = Array.from(u.searchParams.entries())
    .filter(([k, v]) => v !== '' && !TRACKING_PARAM_RE.test(k) && (keepSet === null || keepSet.has(k.toLowerCase())))
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
  const query = params.length ? `?${new URLSearchParams(params).toString()}` : '';
  return `${ctx.site.canonicalOrigin}${normalizePath(u.pathname)}${query}`;
}

/**
 * An image URL a page may publish (og:image, structured data, sitemaps):
 * resolved against the canonical origin, https only, no credentials, never an
 * Amazon-owned host. null otherwise.
 */
export function publicImageUrl(ctx: SiteCtx, url: string | null | undefined): string | null {
  const raw = String(url ?? '').trim();
  if (!raw) return null;
  let u: URL;
  try {
    u = new URL(raw.startsWith('//') ? `https:${raw}` : raw, `${ctx.site.canonicalOrigin}/`);
  } catch {
    return null;
  }
  if (u.protocol !== 'https:' || u.username || u.password) return null;
  if (isAmazonOwnedHost(u.hostname)) return null;
  return u.toString();
}
