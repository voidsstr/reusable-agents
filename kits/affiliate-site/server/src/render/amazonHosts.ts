/**
 * Which hosts belong to Amazon, for the render helpers' licence rules.
 *
 * Two families matter:
 *   - Amazon's image CDNs (m.media-amazon.com, images-na.ssl-images-amazon.com,
 *     the older images-amazon.com, the SiteStripe image host on
 *     amazon-adsystem.com). IP License 2(h) forbids storing Amazon images, and
 *     an image link may be used for 24 hours at most, so none of them may ever
 *     be an og:image, a stored hero, a sitemap <image:loc>, a Product.image, or
 *     an image baked into a stored article body (binding amendment 2).
 *   - Amazon's storefront and short-link hosts (amazon.<tld>, its subdomains,
 *     amzn.to / amzn.com / amzn.eu / amzn.asia, a.co). A link to one of those
 *     is an affiliate link: it must carry one of the site's own tags, or no
 *     link at all.
 *
 * The storefront test is deliberately broad (amazon.com.evil.io counts): it
 * only ever leads to a link being re-tagged through retagAmazonUrl(), which
 * accepts nothing but the site's own marketplace host, or to the link being
 * dropped. Over-matching can therefore only remove a link, never add one.
 */

/** Amazon image CDN host suffixes (a host equal to one, or ending in '.' + one). */
export const AMAZON_IMAGE_HOST_SUFFIXES: readonly string[] = Object.freeze([
  'media-amazon.com',
  'ssl-images-amazon.com',
  'images-amazon.com',
  'amazon-adsystem.com',
]);

const IMAGE_HOST_RE = new RegExp(
  `(^|\\.)(?:${AMAZON_IMAGE_HOST_SUFFIXES.map((s) => s.replace(/[.]/g, '\\.')).join('|')})$`,
  'i',
);
const STOREFRONT_HOST_RE = /(^|\.)amazon\.[a-z.]+$/i;
const SHORTLINK_HOST_RE = /(^|\.)(?:amzn\.(?:to|com|eu|asia)|a\.co)$/i;

function normHost(host: string | null | undefined): string {
  return String(host ?? '').trim().toLowerCase().replace(/\.$/, '');
}

/** True for an Amazon image CDN host (m.media-amazon.com, images-na.ssl-images-amazon.com, ...). */
export function isAmazonImageHost(host: string | null | undefined): boolean {
  const h = normHost(host);
  return !!h && IMAGE_HOST_RE.test(h);
}

/** True for an Amazon storefront or short-link host: links there are affiliate links. */
export function isAmazonLinkHost(host: string | null | undefined): boolean {
  const h = normHost(host);
  return !!h && (STOREFRONT_HOST_RE.test(h) || SHORTLINK_HOST_RE.test(h));
}

/** True for any Amazon-owned host the kit knows (storefront, short link or image CDN). */
export function isAmazonOwnedHost(host: string | null | undefined): boolean {
  return isAmazonImageHost(host) || isAmazonLinkHost(host);
}

/**
 * The lower-cased hostname of `url` (resolved against `base` when relative),
 * or '' when it does not parse as an http(s) URL.
 */
export function urlHostname(url: string | null | undefined, base?: string): string {
  const raw = String(url ?? '').trim();
  if (!raw) return '';
  try {
    const u = base ? new URL(raw, base) : new URL(raw);
    if (u.protocol !== 'https:' && u.protocol !== 'http:') return '';
    return u.hostname.toLowerCase();
  } catch {
    return '';
  }
}

/**
 * True when `url` points at any Amazon-owned host. Protocol-relative URLs
 * ("//m.media-amazon.com/...") count too. An unparseable URL is not Amazon's
 * (callers reject those on their own grounds).
 */
export function isAmazonHostedUrl(url: string | null | undefined): boolean {
  return isAmazonOwnedHost(urlHostname(url, 'https://kit.invalid/'));
}
