/**
 * Sitemap index and shards, fed only indexable URLs.
 *
 * A site hands buildSitemapSet() one candidate list per page family ("kind"),
 * each candidate carrying the same PageIndexing decision its page renders
 * with (seo.ts). Only candidates that are indexable, canonical, on the
 * site's origin, not robots-disallowed and not already listed become <url>
 * entries; everything else is reported in `excluded` with a reason. The same
 * filter (indexableUrls) feeds IndexNow, so the sitemaps, IndexNow and the
 * pages' robots meta agree (binding amendment 7: "Sitemaps and IndexNow carry
 * only indexable URLs").
 *
 * Ported from SpecPicks src/routes/sitemap.ts (see
 * unit/render/fixtures/ported-from-render.json): escapeXml, formatDate
 * (sitemapDate), the <urlset> with the image extension and at most 5 images
 * per URL (urlsetXml), the <sitemapindex> (sitemapIndexXml), the 45,000-URL
 * shard target, hash partitioning so a URL's shard is a pure function of the
 * URL (productSitemapPageOf, plus a JS hash for sites that do not compute
 * hashtextextended in SQL), each child's lastmod = the newest lastmod among
 * its own URLs and never the request date (maxLastmod), and the response
 * rules: an empty or failed build is never a cacheable 200 (503, no-store,
 * Retry-After), a shard that no longer exists is a 404, and a good one is
 * public for an hour.
 *
 * What changed: the per-kind SQL builders and SpecPicks' retired sitemap
 * routes stay in SpecPicks; candidates come from the site's route table.
 * Duplicates are removed across kinds (one URL, one listing). An Amazon image
 * is never an <image:loc> (IP License 2(h)). A kind whose source returned no
 * rows is an outage unless it is declared `allowEmpty`; a kind whose rows are
 * all excluded by policy is simply left out of the index.
 *
 * Canonical means the same form headTags() declares: a candidate's loc is
 * normalised exactly as canonicalUrl(loc, { keepParams: 'all' }) (origin,
 * path, parameters sorted, empty ones dropped), so a page's sitemap entry and
 * its rel=canonical agree when the site passes the same value to both. A
 * candidate that knows its page canonicalises elsewhere (a sort or filter
 * view, a variant ASIN) says so with `canonical` and is left out.
 */
import * as crypto from 'crypto';
import type { SiteContext } from '../siteContext';
import type { PageIndexing } from './seo';
import { collapseWhitespace } from './text';
import { canonicalUrl, publicImageUrl, isSiteHost, TRACKING_PARAM_RE } from './urls';

export const SITEMAP_NS = 'http://www.sitemaps.org/schemas/sitemap/0.9';
export const SITEMAP_IMAGE_NS = 'http://www.google.com/schemas/sitemap-image/1.1';
/** The protocol's hard limit of URLs per file. */
export const SITEMAP_PROTOCOL_MAX_URLS = 50_000;
/** URLs per shard the page count is computed from (headroom under the protocol limit). */
export const SITEMAP_SHARD_TARGET = 45_000;
/** <image:image> entries per <url>. */
export const SITEMAP_MAX_IMAGES_PER_URL = 5;
/** Where the sitemap index is served (robots.txt points here). */
export const SITEMAP_INDEX_PATH = '/sitemap.xml';
/** Cache-Control of a good sitemap response. */
export const SITEMAP_CACHE_CONTROL = 'public, max-age=3600';
/** Retry-After (seconds) on a 503. */
export const SITEMAP_RETRY_AFTER_SECONDS = 300;

export type Changefreq = 'always' | 'hourly' | 'daily' | 'weekly' | 'monthly' | 'yearly' | 'never';

export interface SitemapImage {
  loc: string;
  title?: string;
  caption?: string;
}

/** One <url> entry, ready to serialise. */
export interface SitemapUrl {
  loc: string;
  /** YYYY-MM-DD. */
  lastmod?: string;
  changefreq?: Changefreq;
  /** '0.0'..'1.0'. */
  priority?: string;
  images?: SitemapImage[];
}

// ─── XML ───────────────────────────────────────────────────────────────────

/** Escape text for XML element content and attributes; drops characters XML 1.0 forbids. */
export function escapeXml(unsafe: string): string {
  return String(unsafe)
    .replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\ufffe\uffff]/g, '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&apos;');
}

/** A sitemap date (YYYY-MM-DD, UTC), or undefined for a missing or invalid one. */
export function sitemapDate(d: Date | string | number | null | undefined): string | undefined {
  if (d == null || d === '') return undefined;
  const t = d instanceof Date ? d : new Date(d);
  return Number.isFinite(t.getTime()) ? t.toISOString().slice(0, 10) : undefined;
}

/** A <urlset> document (with the image namespace). */
export function urlsetXml(urls: readonly SitemapUrl[]): string {
  const entries = urls.map((u) => {
    const parts: string[] = ['  <url>', `    <loc>${escapeXml(u.loc)}</loc>`];
    if (u.lastmod) parts.push(`    <lastmod>${escapeXml(u.lastmod)}</lastmod>`);
    if (u.changefreq) parts.push(`    <changefreq>${u.changefreq}</changefreq>`);
    if (u.priority) parts.push(`    <priority>${escapeXml(u.priority)}</priority>`);
    for (const img of (u.images ?? []).slice(0, SITEMAP_MAX_IMAGES_PER_URL)) {
      if (!img.loc) continue;
      parts.push('    <image:image>');
      parts.push(`      <image:loc>${escapeXml(img.loc)}</image:loc>`);
      if (img.title) parts.push(`      <image:title>${escapeXml(img.title)}</image:title>`);
      if (img.caption) parts.push(`      <image:caption>${escapeXml(img.caption)}</image:caption>`);
      parts.push('    </image:image>');
    }
    parts.push('  </url>');
    return parts.join('\n');
  });
  return `<?xml version="1.0" encoding="UTF-8"?>\n`
    + `<urlset xmlns="${SITEMAP_NS}"\n        xmlns:image="${SITEMAP_IMAGE_NS}">\n`
    + `${entries.join('\n')}${entries.length ? '\n' : ''}</urlset>\n`;
}

/** A <sitemapindex> document. */
export function sitemapIndexXml(items: ReadonlyArray<{ loc: string; lastmod?: string }>): string {
  const entries = items.map((it) => `  <sitemap>\n    <loc>${escapeXml(it.loc)}</loc>\n`
    + `${it.lastmod ? `    <lastmod>${escapeXml(it.lastmod)}</lastmod>\n` : ''}  </sitemap>`);
  return `<?xml version="1.0" encoding="UTF-8"?>\n<sitemapindex xmlns="${SITEMAP_NS}">\n`
    + `${entries.join('\n')}${entries.length ? '\n' : ''}</sitemapindex>\n`;
}

/** Newest <lastmod> among `urls` (YYYY-MM-DD sorts as text), or undefined. */
export function maxLastmod(urls: ReadonlyArray<{ lastmod?: string }>): string | undefined {
  let max: string | undefined;
  for (const u of urls) if (u.lastmod && (!max || u.lastmod > max)) max = u.lastmod;
  return max;
}

// ─── sharding ──────────────────────────────────────────────────────────────

/**
 * Shard of a row from Postgres' hashtextextended(key, 0)::text:
 * ((h % pages) + pages) % pages, so a negative hash still lands in range.
 * For sites that partition in SQL; the JS-side sitemapShardOf is the default.
 */
export function shardFromPgHash(hashText: string, pages: number): number {
  if (!Number.isInteger(pages) || pages < 1) throw new RangeError(`shardFromPgHash: pages must be a positive integer, got ${pages}`);
  const P = BigInt(pages);
  const h = BigInt(hashText);
  return Number(((h % P) + P) % P);
}

/**
 * Shard of a key (the URL) among `pages`: the first 8 bytes of its SHA-256,
 * modulo pages. A pure function of the key, so a URL lands in the same shard
 * however many requests build the set, and in what order (OFFSET paging put
 * one product in two shards, or none, while the catalog changed).
 */
export function sitemapShardOf(key: string, pages: number): number {
  if (!Number.isInteger(pages) || pages < 1) throw new RangeError(`sitemapShardOf: pages must be a positive integer, got ${pages}`);
  if (pages === 1) return 0;
  const h = crypto.createHash('sha256').update(key, 'utf8').digest().readBigUInt64BE(0);
  return Number(h % BigInt(pages));
}

// ─── candidates ────────────────────────────────────────────────────────────

/** A page a site offers for its sitemaps, with the indexing decision its page renders with. */
export interface SitemapCandidate {
  /** Site path or URL on the site: the page's canonical URL (what its HeadFields.canonical is). */
  loc: string;
  indexing: PageIndexing;
  /**
   * The page's rel=canonical, when it may differ from `loc` (a filtered or
   * sorted view, a variant ASIN that canonicalises to the model's primary
   * one). A candidate whose canonical form is not its own is excluded as
   * 'non-canonical'. Omitted: `loc` is the canonical.
   */
  canonical?: string | null;
  /** When the page's own content last changed (never "now" by default: lastmod inflation is a negative signal). */
  lastmod?: Date | string | number | null;
  changefreq?: Changefreq;
  /** 0..1. */
  priority?: number;
  /** Image URLs or entries; Amazon-hosted and non-https images are dropped. */
  images?: ReadonlyArray<SitemapImage | string | null | undefined>;
}

export type SitemapExclusionReason =
  | 'noindex'
  | 'bad-url'
  | 'off-site'
  | 'not-https'
  | 'has-fragment'
  | 'tracking-param'
  | 'non-canonical'
  | 'robots-disallowed'
  | 'duplicate';

export interface SitemapExclusion {
  readonly kind: string;
  readonly loc: string;
  readonly reason: SitemapExclusionReason;
  /** The noindex reason, for 'noindex'. */
  readonly detail?: string;
}

type SitemapCtx = Pick<SiteContext, 'site' | 'crawlers'>;

const robotsPatternCache = new Map<string, RegExp>();

/** robots.txt path pattern -> RegExp (Google semantics: prefix match, '*' any run, '$' end anchor). */
function robotsPatternRe(pattern: string): RegExp {
  const hit = robotsPatternCache.get(pattern);
  if (hit) return hit;
  const anchored = pattern.endsWith('$');
  const body = (anchored ? pattern.slice(0, -1) : pattern)
    .split('*')
    .map((part) => part.replace(/[.+?^${}()|[\]\\]/g, '\\$&'))
    .join('.*');
  const re = new RegExp(`^${body}${anchored ? '$' : ''}`);
  if (robotsPatternCache.size >= 256) robotsPatternCache.clear();
  robotsPatternCache.set(pattern, re);
  return re;
}

/** True when a site path (path + query) is disallowed by the site's robots rules. */
export function robotsDisallows(ctx: Pick<SiteContext, 'crawlers'>, pathAndQuery: string): boolean {
  return ctx.crawlers.disallowPaths.some((p) => p && robotsPatternRe(p).test(pathAndQuery));
}

function priorityText(p: number | undefined): string | undefined {
  if (p === undefined || !Number.isFinite(p)) return undefined;
  return Math.min(1, Math.max(0, p)).toFixed(1);
}

/** The canonical form of a URL on the site, as headTags() declares it; null when it is not one. */
function canonicalForm(ctx: SitemapCtx, raw: string): string | null {
  try {
    return canonicalUrl(ctx, raw, { keepParams: 'all' });
  } catch {
    return null;
  }
}

/** Canonical loc for a candidate, or the reason it cannot be listed. */
function locFor(ctx: SitemapCtx, raw: string, declared: string | null | undefined): { loc: string } | { reason: SitemapExclusionReason } {
  const s = String(raw ?? '').trim();
  if (!s) return { reason: 'bad-url' };
  let u: URL;
  try {
    u = new URL(s, `${ctx.site.canonicalOrigin}/`);
  } catch {
    return { reason: 'bad-url' };
  }
  if (!isSiteHost(ctx, u.hostname) || u.username || u.password) return { reason: 'off-site' };
  if (u.protocol !== 'https:') return { reason: 'not-https' };
  if (u.hash) return { reason: 'has-fragment' };
  for (const k of u.searchParams.keys()) if (TRACKING_PARAM_RE.test(k)) return { reason: 'tracking-param' };
  // Sorted parameters, empty ones dropped, path normalised: the form the page's own head declares.
  const loc = canonicalForm(ctx, u.toString());
  if (loc === null) return { reason: 'bad-url' };
  if (declared != null && String(declared).trim() !== '' && canonicalForm(ctx, String(declared)) !== loc) {
    return { reason: 'non-canonical' };
  }
  if (robotsDisallows(ctx, loc.slice(ctx.site.canonicalOrigin.length))) return { reason: 'robots-disallowed' };
  return { loc };
}

function imagesFor(ctx: SitemapCtx, images: SitemapCandidate['images']): SitemapImage[] {
  const out: SitemapImage[] = [];
  for (const img of images ?? []) {
    if (!img) continue;
    const entry: SitemapImage = typeof img === 'string' ? { loc: img } : img;
    const loc = publicImageUrl(ctx, entry.loc);
    if (!loc || out.some((o) => o.loc === loc)) continue;
    const title = collapseWhitespace(entry.title);
    const caption = collapseWhitespace(entry.caption);
    out.push({ loc, ...(title ? { title } : {}), ...(caption ? { caption } : {}) });
    if (out.length >= SITEMAP_MAX_IMAGES_PER_URL) break;
  }
  return out;
}

export interface SitemapUrlsResult {
  urls: SitemapUrl[];
  excluded: SitemapExclusion[];
}

/**
 * The listable entries among `candidates`: indexable, canonical (in the form
 * headTags declares, and the page's own canonical when the candidate names
 * one), on the site, not robots-disallowed, each URL once (`seen` carries
 * URLs already listed by earlier kinds). Order is kept.
 */
export function sitemapUrlsFrom(
  ctx: SitemapCtx,
  kind: string,
  candidates: readonly SitemapCandidate[],
  seen: Set<string> = new Set(),
): SitemapUrlsResult {
  const urls: SitemapUrl[] = [];
  const excluded: SitemapExclusion[] = [];
  for (const c of candidates) {
    const rawLoc = String(c?.loc ?? '');
    if (!c?.indexing?.index) {
      excluded.push({ kind, loc: rawLoc, reason: 'noindex', detail: c?.indexing?.reason ?? 'no indexing decision' });
      continue;
    }
    const r = locFor(ctx, rawLoc, c.canonical);
    if ('reason' in r) { excluded.push({ kind, loc: rawLoc, reason: r.reason }); continue; }
    if (seen.has(r.loc)) { excluded.push({ kind, loc: r.loc, reason: 'duplicate' }); continue; }
    seen.add(r.loc);
    const entry: SitemapUrl = { loc: r.loc };
    const lastmod = sitemapDate(c.lastmod);
    if (lastmod) entry.lastmod = lastmod;
    if (c.changefreq) entry.changefreq = c.changefreq;
    const priority = priorityText(c.priority);
    if (priority) entry.priority = priority;
    const images = imagesFor(ctx, c.images);
    if (images.length) entry.images = images;
    urls.push(entry);
  }
  return { urls, excluded };
}

/**
 * The URLs to submit to IndexNow (or any ping): exactly the ones the
 * sitemaps would list, in order, de-duplicated.
 */
export function indexableUrls(ctx: SitemapCtx, candidates: readonly SitemapCandidate[]): string[] {
  return sitemapUrlsFrom(ctx, 'indexnow', candidates).urls.map((u) => u.loc);
}

// ─── the set ───────────────────────────────────────────────────────────────

/** Sitemap kinds name files: sitemap-<kind>.xml / sitemap-<kind>-<n>.xml. */
export const SITEMAP_KIND_RE = /^[a-z0-9][a-z0-9-]{0,40}$/;

export interface SitemapKindInput {
  kind: string;
  candidates: readonly SitemapCandidate[];
  /**
   * Always numbered shards (sitemap-<kind>-1.xml, ...), even when one file
   * holds them all, so file names stay stable as the kind grows (products).
   * A kind that is not sharded but outgrows the target is split anyway.
   */
  sharded?: boolean;
  /** No candidates is legitimate (a family that may be empty) rather than an outage. */
  allowEmpty?: boolean;
}

export interface SitemapFile {
  readonly kind: string;
  /** Site path, e.g. '/sitemap-products-1.xml'. */
  readonly path: string;
  /** Absolute URL. */
  readonly loc: string;
  readonly urls: readonly SitemapUrl[];
  readonly lastmod: string | undefined;
  readonly xml: string;
}

export interface SitemapSet {
  /** The index document (SITEMAP_INDEX_PATH). */
  readonly indexXml: string;
  readonly files: readonly SitemapFile[];
  /** path -> file, for the shard route. */
  readonly byPath: ReadonlyMap<string, SitemapFile>;
  readonly excluded: readonly SitemapExclusion[];
  /** Kinds left out of the index: no candidates (allowEmpty) or none listable. */
  readonly emptyKinds: readonly string[];
  /** Listed URLs per kind. */
  readonly counts: Readonly<Record<string, number>>;
  /** Total listed URLs. */
  readonly total: number;
}

export class SitemapBuildError extends Error {
  readonly kind: string;
  constructor(kind: string, message: string) {
    super(message);
    this.name = 'SitemapBuildError';
    this.kind = kind;
  }
}

export interface BuildSitemapOptions {
  /**
   * URLs per shard used to compute the starting shard count (default
   * 45,000; at most 50,000). Hash buckets vary in size, so when one would
   * pass the protocol's 50,000 the kind is re-split into one more shard
   * until none does: no indexable URL is ever dropped for size.
   */
  shardTarget?: number;
}

/** `urls` split into `pages` hash buckets (a URL's bucket depends only on the URL and `pages`). */
function hashBuckets(urls: readonly SitemapUrl[], pages: number): SitemapUrl[][] {
  const buckets: SitemapUrl[][] = Array.from({ length: pages }, () => []);
  for (const u of urls) (buckets[sitemapShardOf(u.loc, pages)] as SitemapUrl[]).push(u);
  return buckets;
}

/**
 * Build the whole sitemap set from one candidate list per kind (in the order
 * given; a URL listed by an earlier kind is a duplicate in a later one).
 *
 * Throws SitemapBuildError when a kind that is not `allowEmpty` has no
 * candidates at all: a source that returned nothing is treated as an outage
 * (a transient DB fault must never publish an empty, cacheable sitemap, the
 * strongest de-indexing signal there is). The caller keeps serving its last
 * good set, or answers sitemapUnavailableResponse().
 */
export function buildSitemapSet(ctx: SitemapCtx, kinds: readonly SitemapKindInput[], opts: BuildSitemapOptions = {}): SitemapSet {
  const target = opts.shardTarget ?? SITEMAP_SHARD_TARGET;
  if (!Number.isInteger(target) || target < 1 || target > SITEMAP_PROTOCOL_MAX_URLS) {
    throw new RangeError(`buildSitemapSet: shardTarget must be 1..${SITEMAP_PROTOCOL_MAX_URLS}, got ${target}`);
  }
  const names = new Set<string>();
  for (const k of kinds) {
    if (!SITEMAP_KIND_RE.test(k.kind)) throw new SitemapBuildError(k.kind, `invalid sitemap kind ${JSON.stringify(k.kind)}`);
    if (names.has(k.kind)) throw new SitemapBuildError(k.kind, `sitemap kind ${k.kind} is listed twice`);
    names.add(k.kind);
    if (!k.candidates.length && !k.allowEmpty) {
      throw new SitemapBuildError(k.kind, `sitemap kind ${k.kind} has no candidates (treated as an outage; declare allowEmpty if it may be empty)`);
    }
  }

  const seen = new Set<string>();
  const files: SitemapFile[] = [];
  const excluded: SitemapExclusion[] = [];
  const emptyKinds: string[] = [];
  const counts: Record<string, number> = {};
  let total = 0;
  const origin = ctx.site.canonicalOrigin;
  const indexLoc = `${origin}${SITEMAP_INDEX_PATH}`;

  for (const k of kinds) {
    const r = sitemapUrlsFrom(ctx, k.kind, k.candidates, seen);
    excluded.push(...r.excluded);
    // The index itself is never a <url> of a child.
    const urls = r.urls.filter((u) => u.loc !== indexLoc);
    if (!urls.length) { emptyKinds.push(k.kind); counts[k.kind] = 0; continue; }
    let pages = Math.max(1, Math.ceil(urls.length / target));
    let buckets = hashBuckets(urls, pages);
    while (buckets.some((b) => b.length > SITEMAP_PROTOCOL_MAX_URLS)) {
      pages += 1;
      buckets = hashBuckets(urls, pages);
    }
    const numbered = k.sharded || pages > 1;
    let listed = 0;
    buckets.forEach((bucket, n) => {
      const path = numbered ? `/sitemap-${k.kind}-${n + 1}.xml` : `/sitemap-${k.kind}.xml`;
      listed += bucket.length;
      files.push({ kind: k.kind, path, loc: `${origin}${path}`, urls: bucket, lastmod: maxLastmod(bucket), xml: urlsetXml(bucket) });
    });
    counts[k.kind] = listed;
    total += listed;
  }

  // A shard can come out empty only when hashing leaves a bucket unused; it is
  // still listed (a valid, empty <urlset>) so shard numbering stays contiguous.
  const indexXml = sitemapIndexXml(files.map((f) => ({ loc: f.loc, lastmod: f.lastmod })));
  return {
    indexXml,
    files,
    byPath: new Map(files.map((f) => [f.path, f])),
    excluded,
    emptyKinds,
    counts,
    total,
  };
}

// ─── HTTP responses (framework-agnostic) ───────────────────────────────────

export interface SitemapHttpResponse {
  readonly status: 200 | 404 | 503;
  readonly headers: Readonly<Record<string, string>>;
  readonly body: string;
}

const XML_TYPE = 'application/xml; charset=utf-8';

/**
 * 503 for a sitemap that cannot be built right now: no-store, Retry-After,
 * an empty document with the reason in a comment. Never a cacheable 200.
 */
export function sitemapUnavailableResponse(why: string, root: 'urlset' | 'sitemapindex' = 'urlset'): SitemapHttpResponse {
  const reason = escapeXml(collapseWhitespace(why)).replace(/--/g, '- -');
  return {
    status: 503,
    headers: { 'Content-Type': XML_TYPE, 'Cache-Control': 'no-store', 'Retry-After': String(SITEMAP_RETRY_AFTER_SECONDS) },
    body: `<?xml version="1.0" encoding="UTF-8"?>\n<!-- sitemap temporarily unavailable: ${reason} -->\n<${root} xmlns="${SITEMAP_NS}"></${root}>\n`,
  };
}

/** The index response: 200 with the index, or 503 when the set is missing or lists nothing. */
export function sitemapIndexResponse(set: SitemapSet | null | undefined): SitemapHttpResponse {
  if (!set || !set.files.length) return sitemapUnavailableResponse('no sitemap set available', 'sitemapindex');
  return { status: 200, headers: { 'Content-Type': XML_TYPE, 'Cache-Control': SITEMAP_CACHE_CONTROL }, body: set.indexXml };
}

/**
 * A child sitemap by site path: 200 with the shard, 404 for a path the set
 * does not hold (a shard that no longer exists is gone, not "temporarily
 * unavailable"), 503 when there is no set yet.
 */
export function sitemapFileResponse(set: SitemapSet | null | undefined, path: string): SitemapHttpResponse {
  if (!set) return sitemapUnavailableResponse('no sitemap set available');
  const file = set.byPath.get(path);
  if (!file) {
    return {
      status: 404,
      headers: { 'Content-Type': XML_TYPE, 'Cache-Control': 'no-store' },
      body: '<?xml version="1.0" encoding="UTF-8"?>\n<error>no such sitemap</error>\n',
    };
  }
  return { status: 200, headers: { 'Content-Type': XML_TYPE, 'Cache-Control': SITEMAP_CACHE_CONTROL }, body: file.xml };
}
