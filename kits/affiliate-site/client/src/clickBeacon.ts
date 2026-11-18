/**
 * Delegated affiliate-click beacon (browser).
 *
 * Ported from SpecPicks frontend/src/utils/affiliateClickTracking.ts (see
 * unit/http/fixtures/ported-from-http.json). Upstream rationale, kept:
 *
 *   Every SSR surface emits plain <a> tags; no per-component handler is
 *   attached to them. A single capture-phase listener on `document` fires
 *   for any affiliate anchor regardless of which renderer produced it, keeps
 *   working for CTAs added later, and still reports a CTA whose own handler
 *   calls stopPropagation. It is installed as early as possible: SSR HTML is
 *   clickable before any other script runs.
 *
 *   Each click is sent to POST /api/outbound-click with sendBeacon (it
 *   survives the navigation the click starts), carrying the automation
 *   signals only the page knows (navigator.webdriver, page views this
 *   session, ms since the script loaded). The server classifies it and
 *   writes outbound_clicks (via='beacon'): the verified-human series.
 *
 * Kit changes:
 *   - Amazon only (Amazon storefront and short-link hosts, and same-origin
 *     /r/<ASIN> links); no eBay, no cart links (the kit renders none).
 *   - No framework dependency: vanilla DOM, ES2018 after bundling.
 *   - GA4's `amazon_click` is sent only when the page already has gtag.
 *   - auxclick counts only the middle button: a right-click opens a menu
 *     (copy link, inspect), it does not follow the link.
 *   - Placement falls back to the kit's own markup (the sticky bar, an
 *     <article>) when no data-cta-placement is present.
 */
import { PLACEMENT_ATTR, PLACEMENT_RE, REDIRECT_PATH_PREFIX } from '../../server/src/links';
import {
  BEACON_MERCHANT, BEACON_PATH, CLICK_BEACON_INSTALL_FLAG, CLICK_DEDUPE_MS, GA4_CLICK_EVENT,
  SESSION_PAGEVIEWS_KEY, STICKY_BAR_ATTR, STICKY_DEFAULT_PLACEMENT,
} from '../../server/src/http/clientContract';

/**
 * Amazon storefronts and Amazon's short-link hosts. Upstream matched
 * `amazon\.[a-z.]+`, which also counted look-alike hosts such as
 * amazon.com.example.net; the kit names the real marketplace domains.
 */
const AMAZON_HOST_RE = /(^|\.)(amazon\.(com|ca|cn|de|es|fr|in|it|nl|pl|se|sg|ae|sa|eg|co\.uk|co\.jp|com\.au|com\.br|com\.mx|com\.tr|com\.be)|amzn\.to|amzn\.com|a\.co)$/i;
const PRODUCT_PATH_RE = /\/(?:dp|gp\/product|gp\/aw\/d)\/([A-Z0-9]{10})(?=[/?#]|$)/i;
const ASIN_TEXT_RE = /^[A-Z0-9]{10}$/i;
const REDIRECT_PREFIX_SOURCE = REDIRECT_PATH_PREFIX.replace(/[.*+?^$()|[\]{}\\/]/g, '\\$&');
/** Same-origin /r/<ASIN> (the kit's redirect route; the server's ASIN rule: 10 uppercase alphanumerics). */
const REDIRECT_LINK_RE = new RegExp(`^${REDIRECT_PREFIX_SOURCE}([A-Z0-9]{10})/?$`);

/** The ASIN an Amazon URL names (/dp/, /gp/product/, /gp/aw/d/, ?asin=), or ''. */
export function asinFromAmazonUrl(url: URL): string {
  const m = url.pathname.match(PRODUCT_PATH_RE);
  if (m) return (m[1] as string).toUpperCase();
  const q = url.searchParams.get('asin');
  return q && ASIN_TEXT_RE.test(q) ? q.toUpperCase() : '';
}

export interface AmazonClickTarget {
  /** '' when the link does not name a product (e.g. a search). */
  readonly asin: string;
  /** A same-origin /r/<ASIN> link: the server adds the tag before redirecting. */
  readonly viaRedirect: boolean;
  /** The tag on the link ('' for /r/ links and untagged links). */
  readonly tag: string;
  /** Whether the click earns anything: /r/ always tags, a direct link only with ?tag=. */
  readonly tagged: boolean;
}

/** The Amazon click a link URL represents, or null for any other link. */
export function amazonClickTarget(url: URL, pageOrigin: string): AmazonClickTarget | null {
  if (url.origin === pageOrigin) {
    const m = url.pathname.match(REDIRECT_LINK_RE);
    return m ? { asin: m[1] as string, viaRedirect: true, tag: '', tagged: true } : null;
  }
  if (url.protocol !== 'https:' && url.protocol !== 'http:') return null;
  if (!AMAZON_HOST_RE.test(url.hostname)) return null;
  const tag = url.searchParams.get('tag') || '';
  return { asin: asinFromAmazonUrl(url), viaRedirect: false, tag, tagged: tag !== '' };
}

/** Where on the page the CTA sat: its data-cta-placement, else the kit markup around it. */
export function placementFor(anchor: Element): string {
  const tagged = anchor.closest(`[${PLACEMENT_ATTR}]`);
  if (tagged) {
    const v = tagged.getAttribute(PLACEMENT_ATTR) || '';
    return PLACEMENT_RE.test(v) ? v : 'tagged';
  }
  if (anchor.closest(`[${STICKY_BAR_ATTR}]`)) return STICKY_DEFAULT_PLACEMENT;
  if (anchor.closest('article')) return 'in-article';
  return 'inline';
}

/** Coarse page bucket for GA4: the first path segment, 'home' for '/'. */
export function pageTypeFor(pathname: string): string {
  const first = (pathname.replace(/\/+$/, '') || '/').split('/')[1] || '';
  return first || 'home';
}

export interface BeaconFields {
  readonly asin: string;
  readonly page: string;
  readonly placement: string;
  readonly tag: string;
  readonly pv: number;
  readonly ms: number;
  readonly wd: boolean;
}

/** The JSON body POST /api/outbound-click expects. */
export function beaconBody(f: BeaconFields): string {
  return JSON.stringify({
    merchant: BEACON_MERCHANT,
    asin: f.asin,
    page: f.page,
    placement: f.placement,
    tag: f.tag,
    pv: f.pv,
    ms: f.ms,
    wd: f.wd,
  });
}

type Gtag = (...args: unknown[]) => void;

/** What the click handler touches; installClickBeacon() builds it from `window`. */
export interface BeaconEnv {
  readonly location: { readonly href: string; readonly origin: string; readonly pathname: string };
  /** sessionStorage (may throw when storage is blocked). */
  storage(): Pick<Storage, 'getItem' | 'setItem'> | null;
  readonly navigator?: { sendBeacon?: (url: string, data?: BodyInit | null) => boolean; readonly webdriver?: boolean } | null;
  /** A bound fetch, used when sendBeacon is missing or refuses the body. */
  readonly fetch?: ((input: string, init?: RequestInit) => Promise<unknown>) | null;
  /** The page's gtag, read at click time (GA may load after this script). */
  gtag(): Gtag | null;
  now(): number;
  /** now() when the script loaded: ms on page is measured from here, as upstream. */
  readonly loadedAt: number;
}

/** Page views in this tab's session; 1 when storage is blocked (the page is loaded, so at least one). */
export function sessionPageviews(env: Pick<BeaconEnv, 'storage'>): number {
  try {
    const s = env.storage();
    return s ? parseInt(s.getItem(SESSION_PAGEVIEWS_KEY) || '0', 10) || 0 : 1;
  } catch {
    return 1;
  }
}

/** Count one page view for this tab's session (once per page load). */
export function countPageview(env: Pick<BeaconEnv, 'storage'>): void {
  try {
    const s = env.storage();
    if (s) s.setItem(SESSION_PAGEVIEWS_KEY, String(sessionPageviews(env) + 1));
  } catch {
    /* storage blocked: nothing to count into */
  }
}

/** POST the body; never throws (measurement must never break the click). */
export function sendBeaconBody(env: Pick<BeaconEnv, 'navigator' | 'fetch'>, body: string): boolean {
  try {
    const nav = env.navigator;
    if (nav && typeof nav.sendBeacon === 'function' && nav.sendBeacon(BEACON_PATH, body)) return true;
    if (typeof env.fetch === 'function') {
      void env.fetch(BEACON_PATH, {
        method: 'POST',
        body,
        keepalive: true,
        credentials: 'same-origin',
        headers: { 'Content-Type': 'text/plain;charset=UTF-8' },
      }).catch(() => undefined);
      return true;
    }
  } catch {
    /* ignore */
  }
  return false;
}

function isElement(v: unknown): v is Element {
  return !!v && typeof (v as Element).closest === 'function' && typeof (v as Element).getAttribute === 'function';
}

/** The listener for `click` and `auxclick` (capture phase on document). */
export function createClickHandler(env: BeaconEnv): (ev: Event) => void {
  let lastKey = '';
  let lastAt = Number.NEGATIVE_INFINITY;
  return (ev: Event) => {
    try {
      if (ev.type === 'auxclick' && (ev as MouseEvent).button !== 1) return;
      const target = ev.target;
      if (!isElement(target)) return;
      const anchor = target.closest('a');
      if (!anchor) return;
      const href = (anchor as HTMLAnchorElement).href;
      const raw = typeof href === 'string' && href ? href : anchor.getAttribute('href');
      if (!raw) return;
      let url: URL;
      try {
        url = new URL(raw, env.location.href);
      } catch {
        return;
      }
      const hit = amazonClickTarget(url, env.location.origin);
      if (!hit) return;

      const key = `${BEACON_MERCHANT}|${url.href}`;
      const now = env.now();
      if (key === lastKey && now - lastAt < CLICK_DEDUPE_MS) return;
      lastKey = key;
      lastAt = now;

      const placement = placementFor(anchor);
      const page = env.location.pathname;
      const gtag = env.gtag();
      if (gtag) {
        try {
          gtag('event', GA4_CLICK_EVENT, {
            asin: hit.asin,
            page_slug: page,
            page_type: pageTypeFor(page),
            placement,
            merchant: BEACON_MERCHANT,
            tagged: hit.tagged,
          });
        } catch {
          /* analytics must never break the click */
        }
      }
      sendBeaconBody(env, beaconBody({
        asin: hit.asin,
        page,
        placement,
        tag: hit.tag,
        pv: sessionPageviews(env),
        ms: Math.round(now - env.loadedAt),
        wd: env.navigator?.webdriver === true,
      }));
    } catch {
      /* measurement must never break the click */
    }
  };
}

/** The BeaconEnv of a real window. */
export function browserBeaconEnv(w: Window): BeaconEnv {
  const perf = w.performance;
  const now = (): number => (perf && typeof perf.now === 'function' ? perf.now() : Date.now());
  return {
    location: w.location,
    storage: () => w.sessionStorage,
    navigator: w.navigator,
    fetch: typeof w.fetch === 'function' ? w.fetch.bind(w) : null,
    gtag: () => {
      const g = (w as unknown as { gtag?: unknown }).gtag;
      return typeof g === 'function' ? (g as Gtag) : null;
    },
    now,
    loadedAt: now(),
  };
}

/**
 * Install once per window, as early as possible: counts this page view and
 * attaches the capture-phase listeners. Returns false when already installed
 * (or there is no document).
 */
export function installClickBeacon(win?: Window): boolean {
  const w = win ?? (typeof window !== 'undefined' ? window : undefined);
  if (!w || !w.document) return false;
  const flags = w as unknown as Record<string, unknown>;
  if (flags[CLICK_BEACON_INSTALL_FLAG]) return false;
  flags[CLICK_BEACON_INSTALL_FLAG] = true;
  const env = browserBeaconEnv(w);
  countPageview(env);
  const handler = createClickHandler(env);
  w.document.addEventListener('click', handler, true);
  w.document.addEventListener('auxclick', handler, true);
  return true;
}
