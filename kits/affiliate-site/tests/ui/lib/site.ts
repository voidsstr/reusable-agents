import type { APIRequestContext } from '@playwright/test';
import { loadSiteContext, type SiteContext } from '../../../server/src/siteContext';
import { NO_TARGET_URL, gateTargetUrl } from './target';

export { NO_TARGET_URL, gateTargetUrl };

/**
 * Where the suite points, and how the site's production URLs map onto it.
 *
 * Ported from SpecPicks tests/ui/lib/site.ts, with the site taken from its
 * site.json (KIT_GATE_SITE_JSON, set by tests/playwright.base.ts) instead of
 * literals. The gate runs against the production image on a local port. That
 * server still writes production URLs into its pages (canonicals, links,
 * og:url and sitemap <loc>s are absolute on site.json's canonical origin), so
 * every site URL is rewritten to BASE_URL before anything is fetched, and the
 * gate never sends a request to production.
 */
export const BASE_URL = gateTargetUrl() || NO_TARGET_URL;
export const BASE = new URL(BASE_URL);

export interface GateSite {
  readonly ctx: SiteContext;
  /** Hosts of the live site: domain, www.domain, the canonical origin's host. */
  readonly prodHosts: ReadonlySet<string>;
  /** Hosts that mean "this site" in a page or a sitemap: the live hosts plus the server under test. */
  readonly siteHosts: ReadonlySet<string>;
  /** https://<canonical host>, no trailing slash. */
  readonly prodOrigin: string;
}

let cached: GateSite | null = null;

/** The site under test, from its site.json. Throws with how to fix it when the gate runs without one. */
export function gateSite(): GateSite {
  if (cached) return cached;
  const file = process.env.KIT_GATE_SITE_JSON;
  if (!file) {
    throw new Error('KIT_GATE_SITE_JSON is not set: run the gate through a playwright.config.ts built by kitPlaywrightConfig() (tests/playwright.base.ts)');
  }
  // The gate judges the rules, not this process's ops overrides: no env.
  const ctx = loadSiteContext(file, { env: {} });
  const canonical = new URL(ctx.site.canonicalOrigin);
  const prodHosts = new Set([ctx.site.domain, `www.${ctx.site.domain}`, canonical.host].map((h) => h.toLowerCase()));
  const extra = String(process.env.KIT_GATE_EXTRA_SITE_HOSTS || '').split(',').map((h) => h.trim().toLowerCase()).filter(Boolean);
  const siteHosts = new Set([...prodHosts, BASE.host.toLowerCase(), ...extra]);
  cached = Object.freeze({ ctx, prodHosts, siteHosts, prodOrigin: canonical.origin });
  return cached;
}

/** Forget the cached site (unit tests). */
export function resetGateSite(): void {
  cached = null;
}

export function isSiteHost(host: string): boolean {
  return gateSite().siteHosts.has(host.toLowerCase());
}

export function isProdHost(host: string): boolean {
  return gateSite().prodHosts.has(host.toLowerCase());
}

/** True when the suite is pointed at production itself (heavy, fixture-tuned specs skip then). */
export function targetsProduction(): boolean {
  return isProdHost(BASE.host);
}

/** Path + query of a site URL (absolute on any site host, or relative). null for other hosts. */
export function sitePath(href: string): string | null {
  try {
    const u = new URL(href, BASE_URL);
    if (!isSiteHost(u.host)) return null;
    return u.pathname + u.search;
  } catch {
    return null;
  }
}

export function localUrl(pathOrUrl: string): string {
  const p = sitePath(pathOrUrl);
  if (p === null) throw new Error(`not a site URL: ${pathOrUrl}`);
  return BASE_URL + p;
}

// ── Deterministic sampling ──────────────────────────────────────────────
// One fixed seed, so a red run names the same URLs every time it is re-run
// against the same fixture. PAGES_SEED overrides it to look at other samples.
export const SEED = process.env.PAGES_SEED || '20261117';

/** FNV-1a, finished with murmur3's fmix32 so near-identical ids spread out. */
export function hashString(s: string): number {
  let h = 2166136261 >>> 0;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619) >>> 0;
  }
  h ^= h >>> 16;
  h = Math.imul(h, 0x85ebca6b) >>> 0;
  h ^= h >>> 13;
  h = Math.imul(h, 0xc2b2ae35) >>> 0;
  h ^= h >>> 16;
  return h >>> 0;
}

/**
 * n items chosen by seeded rank: each item's id is hashed with the seed and the
 * key, and the n lowest hashes win. The pick depends only on which items
 * exist, and stays put when the fixture gains or loses OTHER rows: an item
 * leaves the sample only if a newcomer outranks it.
 */
export function seededSample<T>(items: readonly T[], n: number, key: string, id: (t: T) => string = (t) => String(t), seed: string = SEED): T[] {
  const byId = new Map<string, T>();
  for (const it of items) if (!byId.has(id(it))) byId.set(id(it), it);
  return [...byId.entries()]
    .map(([k, v]) => ({ v, h: hashString(`${seed}:${key}:${k}`), k }))
    .sort((a, b) => a.h - b.h || (a.k < b.k ? -1 : 1))
    .slice(0, Math.max(0, n))
    .map((x) => x.v);
}

// ── Local sitemaps ──────────────────────────────────────────────────────
const sitemapCache = new Map<string, Promise<string[]>>();

/** The <loc> entries of one local sitemap file, as site paths. A non-200 sitemap yields []. */
export function sitemapPaths(request: APIRequestContext, file: string): Promise<string[]> {
  if (!sitemapCache.has(file)) {
    sitemapCache.set(file, (async () => {
      const resp = await request.get(`${BASE_URL}/${file.replace(/^\//, '')}`, { timeout: 60_000 });
      if (resp.status() !== 200) return [];
      const out: string[] = [];
      for (const loc of xmlLocs(await resp.text())) {
        const p = sitePath(loc);
        if (p) out.push(p);
      }
      return out;
    })());
  }
  return sitemapCache.get(file)!;
}

/** The <loc> values of a sitemap or sitemap index, entity-decoded. */
export function xmlLocs(xml: string): string[] {
  return [...xml.matchAll(/<loc>\s*([^<\s]+)\s*<\/loc>/g)].map((m) => (m[1] as string).replace(/&amp;/g, '&'));
}

/** The child sitemaps the local sitemap index lists, as file paths ('sitemap-x.xml'). */
export async function sitemapFiles(request: APIRequestContext, index = '/sitemap.xml'): Promise<string[]> {
  const resp = await request.get(`${BASE_URL}${index}`, { timeout: 60_000 });
  if (resp.status() !== 200) return [];
  return xmlLocs(await resp.text()).map((loc) => (sitePath(loc) ?? loc).replace(/^\//, '').split('?')[0] as string);
}

/** Every page path in the local sitemaps (deduplicated, in sitemap order). */
export async function allSitemapPaths(request: APIRequestContext, index = '/sitemap.xml'): Promise<string[]> {
  const out = new Set<string>();
  for (const f of await sitemapFiles(request, index)) {
    for (const p of await sitemapPaths(request, f)) if (!/\.xml$/.test(p.split('?')[0] as string)) out.add(p);
  }
  return [...out];
}

const jsonCache = new Map<string, Promise<unknown>>();

/** GET a local JSON endpoint once per worker. Throws on a non-2xx answer. */
export function apiJson(request: APIRequestContext, path: string): Promise<unknown> {
  if (!jsonCache.has(path)) {
    jsonCache.set(path, (async () => {
      const resp = await request.get(`${BASE_URL}${path}`, { timeout: 60_000 });
      if (!resp.ok()) throw new Error(`GET ${path} -> ${resp.status()}`);
      return resp.json();
    })());
  }
  return jsonCache.get(path)!;
}
