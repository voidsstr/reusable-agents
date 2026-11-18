import type { Browser, BrowserContext, Page, Route } from '@playwright/test';
import { BASE, BASE_URL, gateSite, isProdHost, isSiteHost } from './site';

/**
 * Loads a page the way a reader gets it and records what they would see.
 *
 * Ported from SpecPicks tests/ui/lib/pageAudit.ts. The kit's sites are
 * server-rendered only (no SPA), so the React checks are gone; what stays is
 * the reader's view with JS off and with the kit's small client bundle on.
 *
 * Network policy (the gate must never depend on, or send anything to, a third
 * party or production):
 *   - requests to the server under test go through untouched;
 *   - an asset the page asks production for (https://<site domain>/...) is
 *     served from the server under test instead, so the page renders the same
 *     and production sees nothing;
 *   - a third-party image (Amazon's CDN) is answered locally with a 600x600
 *     placeholder, so layout matches a real image without fetching it;
 *   - every other third-party request (analytics, fonts, Amazon itself) is aborted.
 */

export const PHONE = { width: 390, height: 844 };
export const DESKTOP = { width: 1366, height: 900 };

/** User agents the gate's browser sends: plain Chrome, so no AI-traffic row is ever logged for it. */
export const GATE_UA = {
  phone: 'Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Mobile Safari/537.36 KitGate/1.0',
  desktop: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36 KitGate/1.0',
} as const;

export const PLACEHOLDER_SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="600" height="600"><rect width="600" height="600" fill="#e2e8f0"/></svg>';

export type View = 'phone' | 'desktop';

export interface ContextOptions {
  js: boolean;
  view: View;
}

export async function newAuditContext(browser: Browser, opts: ContextOptions): Promise<BrowserContext> {
  const ctx = await browser.newContext({
    javaScriptEnabled: opts.js,
    viewport: opts.view === 'phone' ? PHONE : DESKTOP,
    isMobile: opts.view === 'phone',
    hasTouch: opts.view === 'phone',
    serviceWorkers: 'block',
    userAgent: opts.view === 'phone' ? GATE_UA.phone : GATE_UA.desktop,
  });
  await ctx.route('**/*', (route) => routeRequest(route));
  return ctx;
}

export async function routeRequest(route: Route): Promise<void> {
  const req = route.request();
  let u: URL;
  try {
    u = new URL(req.url());
  } catch {
    return route.abort();
  }
  if (u.protocol === 'data:' || u.protocol === 'blob:') return route.continue();
  if (u.host === BASE.host) return route.continue();
  if (isProdHost(u.host)) {
    // Serve it from the server under test, never from production.
    try {
      const resp = await route.fetch({ url: `${BASE_URL}${u.pathname}${u.search}`, maxRedirects: 0 });
      return route.fulfill({ response: resp });
    } catch {
      return route.abort();
    }
  }
  if (req.resourceType() === 'image') {
    return route.fulfill({ status: 200, contentType: 'image/svg+xml', body: PLACEHOLDER_SVG });
  }
  return route.abort();
}

export interface Cta {
  href: string;
  kind: 'amazon' | 'click';
  rel: string;
  target: string;
  visible: boolean;
  inMain: boolean;
  placement: string;
  text: string;
}

export interface Leak {
  kind: string;
  text: string;
  tag: string;
  cls: string;
}

export interface ImgInfo {
  src: string | null;
  srcset: string | null;
  resolved: string;
}

export interface DomSnapshot {
  href: string;
  title: string;
  descriptions: string[];
  canonicals: string[];
  robots: string[];
  h1s: string[];
  visibleH1s: string[];
  counts: { main: number; mainId: number; ssrMain: number; ssrStyles: number };
  jsonld: string[];
  leaks: Leak[];
  ctas: Cta[];
  priceBugs: string[];
  images: ImgInfo[];
  links: string[];
  /** hrefs inside nav, header and footer: the site chrome. */
  chromeLinks: string[];
  badLinks: string[];
  overflow: { scrollWidth: number; innerWidth: number; offenders: string[] };
  /** The page reads as a not-found page (title or h1). */
  notFoundView: boolean;
  /** The kit client bundle installed its click beacon. */
  kitClient: boolean;
  textLength: number;
}

export interface CollectArgs {
  deviceWidth: number;
  /** Hosts that are this site (for /r/<ASIN> click links written absolute). */
  siteHosts: string[];
}

/**
 * Runs in the page. Everything it needs is defined inside it, because
 * Playwright serializes the function source.
 */
function collect(arg: CollectArgs): DomSnapshot {
  const SKIP = 'script,style,noscript,template,textarea,pre,code,kbd,samp';
  // Text a reader can see that is really source: CSS rules, markup, script,
  // structured data, template and renderer exhaust, unrendered markdown.
  const PATTERNS: Record<string, RegExp> = {
    css: /(^|[\s};])[.#]?[a-zA-Z][\w-]*(?:[\s>+~,:.#[\]()="'\w-]*)?\s*\{\s*[a-z-]+\s*:\s*[^{};]+;[^{}]*\}/,
    'html-tag': /<\/?(?:a|article|aside|b|blockquote|body|br|button|dd|details|div|dl|dt|em|figcaption|figure|footer|form|h[1-6]|head|header|hr|html|i|iframe|img|input|li|link|main|meta|nav|ol|p|script|section|small|span|strong|style|summary|sup|sub|svg|table|tbody|td|th|thead|tr|u|ul)(?:\s*\/?>|\s+[a-z-]+=["'][^<>]*>)/i,
    'html-entity': /&(?:amp|lt|gt|quot|apos|nbsp|rsquo|lsquo|rdquo|ldquo|mdash|ndash|hellip|#\d{2,6}|#x[0-9a-f]{2,5});/i,
    js: /function\s*\([^)]*\)\s*\{|=>\s*\{|\bdocument\.(?:getElementById|querySelector(?:All)?|createElement|write)\s*\(|\bwindow\.[A-Za-z_$][\w$]*\s*(?:=[^=]|\()|\b(?:const|let|var)\s+[A-Za-z_$][\w$]*\s*=\s*[^;\n]{1,80};/,
    'json-ld': /"@(?:context|type|graph)"\s*:/,
    template: /\{\{|\}\}|\$\{|__SSR_[A-Z_]*__|<\?claude|\[object Object\]|\bundefined\b|\bNaN\b/,
    'null-value': /^\s*null\s*$|(?:\$|:\s*)null\b|\bnull\s*(?:%|GB\b|MB\b|W\b|Wh\b|kWh\b|fps\b|ms\b)/i,
    markdown: /\*\*[^*\s][^*]{0,80}\*\*|(?:^|\n)\s{0,3}#{2,6}\s+\S|\|\s*:?-{3,}:?\s*\||```|\[[^\]\n]{1,120}\]\((?:https?:\/\/|\/)[^)\s]*\)/,
  };
  const AMAZON_HOST = /(^|\.)amazon\.[a-z.]+$|(^|\.)(amzn\.(to|com|eu|asia)|a\.co)$/i;
  const siteHosts = new Set(arg.siteHosts.map((h) => h.toLowerCase()));
  const isVisible = (el: Element): boolean => {
    const anyEl = el as Element & { checkVisibility?: (o: object) => boolean };
    if (typeof anyEl.checkVisibility === 'function') return anyEl.checkVisibility({ visibilityProperty: true });
    const r = el.getBoundingClientRect();
    return r.width > 0 || r.height > 0;
  };
  // Text inside a closed <details> is rendered as soon as the reader opens it
  // (FAQ answers live there), so judge the <details> itself.
  const isRendered = (el: Element): boolean => {
    const closed = el.closest('details:not([open])');
    if (closed && !el.closest('summary')) return isVisible(closed);
    return isVisible(el);
  };
  const text = (el: Element | null): string => (el?.textContent || '').replace(/\s+/g, ' ').trim();

  const leaks: Leak[] = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    const t = n.nodeValue || '';
    if (t.trim().length < 3) continue;
    const el = n.parentElement;
    if (!el || el.closest(SKIP)) continue;
    const kinds = Object.keys(PATTERNS).filter((k) => (PATTERNS[k] as RegExp).test(t));
    if (!kinds.length || !isRendered(el)) continue;
    for (const kind of kinds) {
      leaks.push({ kind, text: t.replace(/\s+/g, ' ').trim().slice(0, 160), tag: el.tagName.toLowerCase(), cls: String(el.className || '').slice(0, 60) });
    }
  }

  const mains = Array.from(document.querySelectorAll('main'));
  const main = mains.length === 1 ? mains[0] : null;
  const ctas: Cta[] = [];
  for (const a of Array.from(document.querySelectorAll<HTMLAnchorElement>('a[href]'))) {
    let u: URL;
    try { u = new URL(a.href); } catch { continue; }
    let kind: Cta['kind'] | null = null;
    if (/^https?:$/.test(u.protocol) && AMAZON_HOST.test(u.hostname)) kind = 'amazon';
    else if ((u.origin === location.origin || siteHosts.has(u.host.toLowerCase())) && /^\/r\/[A-Za-z0-9_]+\/?$/.test(u.pathname)) kind = 'click';
    if (!kind) continue;
    const placed = a.closest('[data-cta-placement]');
    ctas.push({
      href: a.getAttribute('href') || '',
      kind,
      rel: (a.getAttribute('rel') || '').toLowerCase(),
      target: (a.getAttribute('target') || '').toLowerCase(),
      visible: isVisible(a),
      inMain: !!main && main.contains(a),
      placement: placed ? placed.getAttribute('data-cta-placement') || '' : '',
      text: text(a).slice(0, 60),
    });
  }

  // Price exhaust a reader can see. "$NaN", "$undefined", "$null" are never
  // copy. "$0" / "$0.00" is exhaust where a price is shown -- a short price
  // label or an element in a price block -- and legitimate in prose.
  const priceBugs: string[] = [];
  const PRICE_EXHAUST = /(?<![\w.,])\$\s?(?:NaN|undefined|null)\b/;
  const ZERO_PRICE = /(?<![\w.,])\$\s?0(?:\.0{1,2})?(?![\d.,]*\d)/;
  const priceWalker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const seenPriceEls = new Set<Element>();
  for (let n = priceWalker.nextNode(); n; n = priceWalker.nextNode()) {
    const t = n.nodeValue || '';
    if (!t.includes('$')) continue;
    const el = n.parentElement;
    if (!el || seenPriceEls.has(el) || el.closest(SKIP) || !isRendered(el)) continue;
    seenPriceEls.add(el);
    const whole = text(el);
    const priceBlock = whole.length <= 40 || !!el.closest('[class*="price"]');
    if (PRICE_EXHAUST.test(whole) || (priceBlock && ZERO_PRICE.test(whole))) priceBugs.push(whole.slice(0, 120));
  }

  const images: ImgInfo[] = Array.from(document.querySelectorAll('img')).map((img) => ({
    src: img.getAttribute('src'),
    srcset: img.getAttribute('srcset'),
    resolved: (img as HTMLImageElement).src,
  }));

  const links: string[] = [];
  const chromeLinks: string[] = [];
  const badLinks: string[] = [];
  for (const a of Array.from(document.querySelectorAll('a'))) {
    const raw = a.getAttribute('href');
    if (raw === null) continue;
    const trimmed = raw.trim();
    if (!trimmed || /(^|\/)(undefined|null|NaN)(\/|$|\?|#)|\[object Object\]/.test(trimmed)) {
      badLinks.push(`${JSON.stringify(raw)} "${text(a).slice(0, 50)}"`);
      continue;
    }
    links.push(trimmed);
    if (a.closest('nav, header, footer')) chromeLinks.push(trimmed);
  }

  // A phone's layout viewport grows to fit content that is too wide (the page
  // is then zoomed out), so innerWidth is no yardstick: compare with the
  // device width the context was created with.
  const vw = arg.deviceWidth;
  const offenders: string[] = [];
  if (document.documentElement.scrollWidth > vw + 1 || window.innerWidth > vw + 1) {
    for (const el of Array.from(document.querySelectorAll('body *'))) {
      const r = el.getBoundingClientRect();
      if (r.right > vw + 1 && r.width > 0 && isVisible(el)) {
        const parentOver = el.parentElement && el.parentElement.getBoundingClientRect().right > vw + 1;
        if (!parentOver) offenders.push(`${el.tagName.toLowerCase()}.${String(el.className || '').split(/\s+/).slice(0, 2).join('.')} right=${Math.round(r.right)}`);
        if (offenders.length >= 5) break;
      }
    }
  }

  const title = document.title || '';
  const bodyText = document.body?.innerText || '';
  const h1s = Array.from(document.querySelectorAll('h1'));
  return {
    href: location.href,
    title,
    descriptions: Array.from(document.querySelectorAll('meta[name="description"]')).map((m) => (m as HTMLMetaElement).content),
    canonicals: Array.from(document.querySelectorAll('link[rel="canonical"]')).map((l) => l.getAttribute('href') || ''),
    robots: Array.from(document.querySelectorAll('meta[name="robots"]')).map((m) => (m as HTMLMetaElement).content),
    h1s: h1s.map((h) => text(h)),
    visibleH1s: h1s.filter((h) => isVisible(h)).map((h) => text(h)),
    counts: {
      main: mains.length,
      mainId: document.querySelectorAll('[id="main"]').length,
      // Page bodies: the kit layout's <main class="ssr-main">.
      ssrMain: document.querySelectorAll('main.ssr-main, div.ssr-main').length,
      ssrStyles: document.querySelectorAll('style#ssr-styles').length,
    },
    jsonld: Array.from(document.querySelectorAll('script[type="application/ld+json"]')).map((s) => s.textContent || ''),
    leaks: leaks.slice(0, 20),
    ctas,
    priceBugs: priceBugs.slice(0, 10),
    images,
    links,
    chromeLinks,
    badLinks: badLinks.slice(0, 10),
    overflow: { scrollWidth: document.documentElement.scrollWidth, innerWidth: window.innerWidth, offenders },
    notFoundView: /^(404\b|page not found\b|not found\b)/i.test(title.trim()) || h1s.some((h) => /^\s*(404\b|page not found\b)/i.test(text(h))),
    kitClient: !!(window as unknown as Record<string, unknown>).__kitClickBeacon,
    textLength: bodyText.length,
  };
}

/**
 * The element that makes the page wider than the device: hide children one
 * at a time from <body> down and keep the one whose hiding removes the
 * overflow. Mutates styles while it runs and restores them; call it last.
 */
export async function overflowCulprit(page: Page, deviceWidth: number): Promise<string> {
  return page.evaluate((limit) => {
    const over = (): boolean => document.documentElement.scrollWidth > limit + 1;
    const desc = (el: Element): string => {
      const cls = typeof (el as HTMLElement).className === 'string' ? (el as HTMLElement).className.trim().split(/\s+/).filter(Boolean).slice(0, 3) : [];
      return `${el.tagName.toLowerCase()}${el.id ? `#${el.id}` : ''}${cls.length ? `.${cls.join('.')}` : ''}`;
    };
    if (!over()) return '';
    let node: Element = document.body;
    const path: string[] = [];
    for (let depth = 0; depth < 40; depth++) {
      let found: Element | null = null;
      for (const child of Array.from(node.children)) {
        const el = child as HTMLElement;
        const prev = el.style.display;
        el.style.display = 'none';
        const fixed = !over();
        el.style.display = prev;
        if (fixed) { found = child; break; }
      }
      if (!found) break;
      path.push(desc(found));
      node = found;
    }
    const sample = (node.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 90);
    return `${path.slice(-4).join(' > ') || desc(node)}${sample ? ` ("${sample}")` : ''}`;
  }, deviceWidth);
}

export interface LoadResult {
  status: number;
  finalUrl: string;
  errors: string[];
  snap: DomSnapshot;
  /** Set by a diagnose hook: the element that widens the page. */
  culprit?: string;
}

/**
 * Load `path` in `ctx` and snapshot it. With JS on, wait for the load event,
 * the kit client bundle (when the page carries it) and a bounded network idle.
 */
export async function loadAndSnapshot(
  ctx: BrowserContext,
  path: string,
  opts: { js: boolean; view: View; diagnose?: (page: Page, result: LoadResult) => Promise<void> },
): Promise<LoadResult> {
  const page: Page = await ctx.newPage();
  const errors: string[] = [];
  page.on('pageerror', (e) => errors.push(String(e.message || e).slice(0, 300)));
  try {
    const resp = await page.goto(`${BASE_URL}${path}`, { waitUntil: opts.js ? 'load' : 'domcontentloaded', timeout: 45_000 });
    if (opts.js) {
      const hasClient = await page.evaluate(() => !!document.querySelector('script[src*="/kit/client."]'));
      if (hasClient) {
        await page.waitForFunction(() => !!(window as unknown as Record<string, unknown>).__kitClickBeacon, undefined, { timeout: 15_000 }).catch(() => undefined);
      }
      // Bounded: a page that polls must not hang the gate.
      await page.waitForLoadState('networkidle', { timeout: 8_000 }).catch(() => undefined);
    }
    const snap = await page.evaluate(collect, {
      deviceWidth: opts.view === 'phone' ? PHONE.width : DESKTOP.width,
      siteHosts: [...gateSite().siteHosts],
    });
    const result: LoadResult = { status: resp?.status() ?? 0, finalUrl: page.url(), errors, snap };
    if (opts.diagnose) await opts.diagnose(page, result);
    return result;
  } finally {
    await page.close();
  }
}

/** Every JSON-LD block parses and carries @context and @type (directly or in @graph). */
export function jsonLdProblems(blocks: string[]): { problems: string[]; types: Record<string, number> } {
  const problems: string[] = [];
  const types: Record<string, number> = {};
  const count = (node: unknown): void => {
    if (!node || typeof node !== 'object') return;
    const t = (node as Record<string, unknown>)['@type'];
    for (const ty of Array.isArray(t) ? t : t ? [t] : []) types[String(ty)] = (types[String(ty)] || 0) + 1;
    for (const v of Object.values(node as Record<string, unknown>)) {
      if (Array.isArray(v)) v.forEach(count);
      else if (v && typeof v === 'object') count(v);
    }
  };
  blocks.forEach((raw, i) => {
    let data: unknown;
    try {
      data = JSON.parse(raw);
    } catch (e) {
      problems.push(`block ${i + 1} does not parse: ${(e as Error).message.slice(0, 80)} :: ${raw.trim().slice(0, 80)}`);
      return;
    }
    const roots = Array.isArray(data) ? data : [data];
    for (const r of roots) {
      if (!r || typeof r !== 'object') { problems.push(`block ${i + 1}: not an object`); continue; }
      const o = r as Record<string, unknown>;
      if (!o['@context']) problems.push(`block ${i + 1}: no @context`);
      const graph = o['@graph'];
      const typed = o['@type'] || (Array.isArray(graph) && graph.length > 0 && graph.every((g) => g && typeof g === 'object' && (g as Record<string, unknown>)['@type']));
      if (!typed) problems.push(`block ${i + 1}: no @type`);
      count(r);
    }
  });
  return { problems, types };
}

/** Path of a canonical href, '' when it is not an absolute URL on the site's canonical origin. */
export function canonicalPath(href: string): string {
  try {
    const u = new URL(href);
    if (u.origin !== gateSite().prodOrigin) return '';
    return (u.pathname.replace(/\/+$/, '') || '/') + u.search;
  } catch {
    return '';
  }
}

export function normalizePath(p: string): string {
  const [path, query] = p.split('?');
  return ((path as string).replace(/\/+$/, '') || '/') + (query ? `?${query}` : '');
}

/** Internal links worth resolving: site pages, not the click logger, APIs, the kit bundle or files. */
export function internalPagePath(href: string, pageUrl: string): string | null {
  let u: URL;
  try { u = new URL(href, pageUrl); } catch { return null; }
  if (!/^https?:$/.test(u.protocol) || !isSiteHost(u.host)) return null;
  const p = u.pathname;
  if (/^\/(r|api|cdn-cgi|assets|images|kit)\//.test(p)) return null;
  if (/\.(xml|csv|rss|txt|json|png|jpe?g|webp|gif|svg|ico|pdf|js|css|woff2?)$/i.test(p)) return null;
  return p + u.search;
}
