/**
 * Site-agnostic IndexNow submitter.
 *
 * Pushes NEW and CHANGED URLs for each configured site to IndexNow
 * (api.indexnow.org → Bing, Yandex, Seznam, Naver). IndexNow's contract is
 * "submit URLs that were added, updated or deleted" — re-sending an
 * unchanged catalog wastes the host's crawl budget and the credibility of
 * its future pings. So every candidate URL passes three gates:
 *
 *   1. SOURCE — where the candidate came from:
 *        querySets    DB rows changed since the watermark (incremental) or
 *                     the whole table (--bulk). `bulkOnly` sets run in
 *                     --bulk ONLY.
 *        force queue  <site>.force-submit.txt — URLs queued by
 *                     queue-publish.py (publish-time hook) and by agent.py's
 *                     canonical-coverage gap. Drained here.
 *        sitemaps     incremental: a snapshot DIFF (URLs that are new, or
 *                     whose <lastmod> changed) at most every
 *                     policy.sitemapIntervalHours; --bulk: every <loc>.
 *        staticPaths  --bulk only.
 *   2. LEDGER — <site>.indexnow-ledger.tsv records every URL we submitted
 *      (or rejected) and its change token (lastmod, day granularity). A URL
 *      is re-sent only when its token changed, or the resubmit window has
 *      passed, and never twice inside policy.minResubmitHours.
 *   3. VERIFY — untrusted candidates (force queue, sitemap diffs, static
 *      paths, querySets marked `verify: true`, re-checks of earlier
 *      rejects) are fetched first and dropped unless they return 200 with no
 *      noindex (meta robots/bingbot or X-Robots-Tag) and a self-canonical.
 *      A random sample of trusted DB candidates is checked too, as a drift
 *      canary. Rejections are recorded in the ledger so a dead URL is not
 *      re-fetched every tick. A TRANSIENT failure (timeout, fetch error, 5xx,
 *      429) is not a rejection: the URL is re-queued and retried on the next
 *      incremental ticks, up to policy.transientMaxAttempts times.
 *   Only incremental runs move the watermark; a failed IndexNow POST puts
 *   queue and sitemap-diff URLs back on the queue.
 *
 * All per-site VALUES live in the site's config file
 * (<repo>/agents/seo-config/site-indexnow.json); the logic here is generic.
 *
 * Run manually:
 *   npx ts-node submit.ts               # all sites, incremental
 *   npx ts-node submit.ts --site=<name>
 *   npx ts-node submit.ts --bulk        # full sweep (ledger-filtered, capped)
 *   npx ts-node submit.ts --dry-run     # log candidates; writes NO state
 *   npx ts-node submit.ts --no-sitemap  # skip sitemap intake (DB-only run)
 *
 * Env:
 *   SITE_CONFIG_PATHS        comma-separated config files (overrides discovery)
 *   INDEXNOW_SITE_REPOS_ROOT parent dir scanned for <repo>/agents/seo-config/
 *                            site-indexnow.json (default ~/development)
 *   INDEXNOW_ENDPOINT        IndexNow API endpoint (tests point this at a mock)
 *   INDEXNOW_QUEUE_ROOT      dir holding <site>.force-submit.txt (default: the
 *                            watermark file's dir)
 */

import { Pool } from 'pg';
import fs from 'fs';
import path from 'path';

// ── Types ──────────────────────────────────────────────────────────────────

export type QuerySet = {
  name: string;
  bulkOnly?: boolean;
  /** Skip in --bulk (use when the site's sitemap is the canonical source of
   *  these URLs in bulk, e.g. slugs the DB can't reproduce exactly). */
  incrementalOnly?: boolean;
  bulkSql: string;
  incrementalSql?: string;
  urlTemplate: string;
  urlPrefix: string;
  /** Row column carrying the change token (default `lastmod`). */
  lastmodColumn?: string;
  /** Fetch-verify every URL this set yields (small or untrusted sets). */
  verify?: boolean;
  /** column → JS regex (case-insensitive). Matching rows are skipped. */
  excludeIfMatches?: Record<string, string>;
};

export type Policy = {
  /** Token-less candidates (force queue, static) re-send after this many days. */
  resubmitDays: number;
  /** --bulk re-sends an unchanged URL only after this many days. */
  bulkResubmitDays: number;
  /** Never re-send the same URL inside this window, even if it changed. */
  minResubmitHours: number;
  /** --bulk submits at most this many URLs per run (0 = unlimited). */
  bulkMaxPerRun: number;
  /** Incremental runs refresh the sitemap snapshot at most this often. */
  sitemapIntervalHours: number;
  /** A rejected URL is re-verified only after this many days. */
  rejectRecheckDays: number;
  /** Ledger entries older than this are pruned. */
  ledgerRetentionDays: number;
  /** Per-query statement timeout. */
  queryTimeoutMs: number;
  /** Path prefixes whose sitemap <lastmod> is not a content change (e.g. a
   *  "last seen trending" stamp): only NEW locs under them count. */
  ignoreLastmodPrefixes: string[];
  /** A verification that fails TRANSIENTLY (timeout, fetch error, 5xx, 429)
   *  is retried on the next incremental tick (the URL is re-queued) up to
   *  this many times before it is treated as a normal rejection. */
  transientMaxAttempts: number;
};

export type VerifyCfg = {
  enabled: boolean;
  /** Candidate sources that must pass a live fetch before submission. */
  sources: string[];
  /** Upper bound on live fetches per run; overflow is deferred to the queue. */
  maxPerRun: number;
  concurrency: number;
  timeoutMs: number;
  /** Random trusted (DB) candidates checked per incremental run. */
  sampleTrusted: number;
  /** Random trusted candidates checked per --bulk run. */
  sampleTrustedBulk: number;
  /** Wall-clock budget for all verification fetches in one run. */
  budgetSeconds: number;
  /** Reject 200 pages whose visible text is shorter than this (0 = off). */
  minTextChars: number;
  userAgent: string;
};

export type SiteConfig = {
  name: string;
  host: string;
  key: string;
  /** Scheme+host used to build/validate URLs. Default https://<host>. */
  origin?: string;
  databaseUrlEnv: string;
  /** Deprecated: a DSN in a tracked file is a leaked credential. Use databaseUrlEnv. */
  databaseUrlFallback?: string;
  siteIdEnv?: string;
  siteIdFallback?: string;
  siteIds?: string[];
  watermarkFile: string;
  ledgerFile?: string;
  snapshotFile?: string;
  forceSubmitFile?: string;
  staticPaths: string[];
  querySets: QuerySet[];
  sitemapUrls?: string[];
  policy?: Partial<Policy>;
  verify?: Partial<VerifyCfg>;
};

export const DEFAULT_POLICY: Policy = {
  resubmitDays: 7,
  bulkResubmitDays: 28,
  minResubmitHours: 24,
  bulkMaxPerRun: 0,
  sitemapIntervalHours: 6,
  rejectRecheckDays: 3,
  ledgerRetentionDays: 120,
  queryTimeoutMs: 120_000,
  ignoreLastmodPrefixes: [],
  transientMaxAttempts: 6,
};

export const DEFAULT_VERIFY: VerifyCfg = {
  enabled: true,
  sources: ['force', 'sitemap', 'static'],
  maxPerRun: 300,
  concurrency: 4,
  timeoutMs: 20_000,
  sampleTrusted: 5,
  sampleTrustedBulk: 50,
  budgetSeconds: 300,
  minTextChars: 0,
  userAgent: 'Mozilla/5.0 (compatible; IndexNowVerifier/1.0; +https://www.indexnow.org/)',
};

const ARGV = process.argv;
const BULK = ARGV.includes('--bulk');
const DRY = ARGV.includes('--dry-run');
const NO_SITEMAP = ARGV.includes('--no-sitemap');
const ONLY_SITE = (ARGV.find((a) => a.startsWith('--site=')) || '').slice('--site='.length) || null;

const ENDPOINT = process.env.INDEXNOW_ENDPOINT || 'https://api.indexnow.org/indexnow';
const BATCH_SIZE = 10_000;
// sitemap-core.xml has taken 46 s uncached; a 30 s cap dropped it every time.
const SITEMAP_FETCH_TIMEOUT_MS = 90_000;
const MAX_SITEMAP_FETCHES = 60;
const MAX_FORCE_QUEUE = 50_000;
const DAY_MS = 86_400_000;
const HOUR_MS = 3_600_000;

// ── Config discovery ───────────────────────────────────────────────────────

export function loadSites(): SiteConfig[] {
  // Site-specific configs live in each site's repo:
  //   $SITE_CONFIG_PATHS (comma-sep)                  explicit list
  //   <root>/*/agents/seo-config/site-indexnow.json  discovery (no site names in code)
  //   ./sites.json                                    legacy last resort
  const out: SiteConfig[] = [];
  const pushFrom = (p: string) => {
    try {
      out.push(...(JSON.parse(fs.readFileSync(p, 'utf-8')).sites as SiteConfig[]));
    } catch (e) {
      console.error(`[indexnow] could not load site config ${p}: ${e}`);
    }
  };
  const explicit = process.env.SITE_CONFIG_PATHS;
  if (explicit) {
    for (const p of explicit.split(',').map((s) => s.trim()).filter(Boolean)) pushFrom(p);
    if (out.length) return out;
  }
  const root = process.env.INDEXNOW_SITE_REPOS_ROOT
    || (process.env.HOME ? path.join(process.env.HOME, 'development') : '');
  if (root && fs.existsSync(root)) {
    for (const repo of fs.readdirSync(root).sort()) {
      const candidate = path.join(root, repo, 'agents', 'seo-config', 'site-indexnow.json');
      if (fs.existsSync(candidate)) pushFrom(candidate);
    }
  }
  if (out.length) return out;
  const legacy = path.join(__dirname, 'sites.json');
  if (fs.existsSync(legacy)) pushFrom(legacy);
  return out;
}

// ── Small helpers ──────────────────────────────────────────────────────────

function readWatermark(file: string): string {
  try {
    const raw = fs.readFileSync(file, 'utf-8').trim();
    if (raw && !Number.isNaN(Date.parse(raw))) return raw;
  } catch { /* first run */ }
  return new Date(0).toISOString();
}

function writeFileAtomic(file: string, data: string) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const tmp = `${file}.tmp-${process.pid}`;
  fs.writeFileSync(tmp, data, 'utf-8');
  fs.renameSync(tmp, file);
}

function slugify(text: string): string {
  // Strip diacritics first ("Sautéed" → "sauteed", not "saut-ed").
  return String(text || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '')
    .toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').substring(0, 80);
}

/** Change token at day granularity, so a date-only sitemap <lastmod> and a
 *  DB timestamp for the same change compare equal. */
export function normalizeToken(v: unknown): string {
  if (v === null || v === undefined || v === '') return '';
  if (v instanceof Date) return Number.isNaN(v.getTime()) ? '' : v.toISOString().slice(0, 10);
  const s = String(v).trim();
  const ms = Date.parse(s);
  if (!Number.isNaN(ms) && /^\d{4}-\d{2}-\d{2}/.test(s)) return new Date(ms).toISOString().slice(0, 10);
  return s.replace(/[\t\r\n]+/g, ' ').slice(0, 64);
}

export function normalizeUrl(u: string): string {
  try {
    const url = new URL(u);
    url.hash = '';
    url.hostname = url.hostname.toLowerCase();
    if ((url.protocol === 'https:' && url.port === '443') || (url.protocol === 'http:' && url.port === '80')) url.port = '';
    if (url.pathname.length > 1 && url.pathname.endsWith('/')) url.pathname = url.pathname.replace(/\/+$/, '');
    return url.toString();
  } catch {
    return u;
  }
}

// Template forms:
//   "slug"                          → row.slug
//   "slugify:title|-|id"            → `${slugify(row.title)}-${slugify(row.id)}`
//   "slugify:slug"                  → `${slugify(row.slug)}`
//   "compose:left_ref|-vs-|right_ref" → `${row.left_ref}-vs-${row.right_ref}` (no slugify)
// A part that is not a column of the row is a LITERAL (so `-vs-` survives —
// the old separator whitelist read it as a missing column and built
// /compare/<a><b>, a 404). `lit:<text>` forces a literal explicitly.
export function buildUrl(origin: string, qs: QuerySet, row: Record<string, any>): string | null {
  const tpl = qs.urlTemplate;
  let pathPart: string;
  const isSep = (p: string) => /^[-_.\\/]+$/.test(p);
  if (tpl === 'slug') {
    if (!row.slug) return null;
    pathPart = String(row.slug);
  } else if (tpl.startsWith('slugify:') || tpl.startsWith('compose:')) {
    const doSlug = tpl.startsWith('slugify:');
    const parts = tpl.slice(tpl.indexOf(':') + 1).split('|');
    let missing = false;
    pathPart = parts.map((p) => {
      if (p.length === 0) return '';
      if (p.startsWith('lit:')) return p.slice(4);
      if (isSep(p)) return p;
      if (Object.prototype.hasOwnProperty.call(row, p)) {
        const v = row[p];
        if (v === null || v === undefined || v === '') { missing = true; return ''; }
        return doSlug ? slugify(v) : String(v);
      }
      // An identifier the row lacks is a missing column (the SQL doesn't
      // select it) — skip the URL rather than invent one. Anything else
      // (`-vs-`, `.html`) is a literal.
      if (/^[A-Za-z_][A-Za-z0-9_]*$/.test(p)) { missing = true; return ''; }
      return p;
    }).join('');
    if (missing || !pathPart.replace(/[-_.\\/]/g, '')) return null;
  } else {
    return null;
  }
  return `${origin}${qs.urlPrefix}${pathPart}`;
}

function interpolateSiteIds(sql: string, siteId?: string, siteIds?: string[]): string {
  let out = sql;
  if (siteId) out = out.replace(/\$SITE_ID\b/g, `'${siteId.replace(/'/g, "''")}'`);
  if (siteIds && siteIds.length) {
    const list = siteIds.map((s) => `'${s.replace(/'/g, "''")}'`).join(', ');
    out = out.replace(/\$SITE_IDS\b/g, list);
  }
  return out;
}

// ── Ledger ─────────────────────────────────────────────────────────────────
// TSV, one line per URL: url \t status \t atMs \t token
//   status = "sent"          token = change token when sent (day)
//          | "rej:<reason>"  permanent-looking reject (404, noindex, …)
//          | "tmp:<reason>"  transient verify failure; token = attempt count

/** Verify failures that say nothing about the page itself (it may be fine
 *  on the next try): the URL is retried, not rejected for days. */
export function isTransientReason(reason: string): boolean {
  return /^(timeout|fetch-error|http-error|http-5\d\d|http-429|http-408)$/.test(reason);
}

export type LedgerEntry = { status: string; at: number; token: string };
export type Ledger = Map<string, LedgerEntry>;

export function readLedger(file: string): Ledger {
  const out: Ledger = new Map();
  let raw = '';
  try { raw = fs.readFileSync(file, 'utf-8'); } catch { return out; }
  for (const line of raw.split('\n')) {
    if (!line) continue;
    const [url, status, at, token] = line.split('\t');
    const atMs = Number(at);
    if (!url || !status || !Number.isFinite(atMs)) continue;
    out.set(url, { status, at: atMs, token: token || '' });
  }
  return out;
}

/** Merge `updates` into the ledger on disk (re-read first, so a concurrent
 *  bulk/incremental run's writes are kept) and prune old entries. */
export function writeLedger(file: string, updates: Ledger, retentionDays: number, nowMs: number) {
  const merged = readLedger(file);
  for (const [url, e] of updates) {
    const cur = merged.get(url);
    if (!cur || e.at >= cur.at) merged.set(url, e);
  }
  const cutoff = nowMs - retentionDays * DAY_MS;
  const lines: string[] = [];
  for (const [url, e] of merged) {
    if (e.at < cutoff) continue;
    lines.push(`${url}\t${e.status}\t${e.at}\t${e.token || ''}`);
  }
  writeFileAtomic(file, lines.join('\n') + (lines.length ? '\n' : ''));
}

export type Decision = { submit: boolean; reason: string; needVerify?: boolean; priority?: number };

/** Should `cand` be (re-)submitted given its ledger entry? */
export function decideCandidate(
  entry: LedgerEntry | undefined,
  cand: { token?: string },
  nowMs: number,
  mode: 'bulk' | 'incremental',
  policy: Policy,
): Decision {
  if (!entry) return { submit: true, reason: 'new', priority: 0 };
  const ageMs = nowMs - entry.at;
  if (entry.status !== 'sent') {
    // A transient failure (timeout, 5xx) is retried at the next chance, a
    // bounded number of times, before it counts as a rejection.
    if (entry.status.startsWith('tmp:') && (Number(entry.token) || 1) < policy.transientMaxAttempts) {
      return { submit: true, reason: 'retry-transient', needVerify: true, priority: 1 };
    }
    if (ageMs < policy.rejectRecheckDays * DAY_MS) return { submit: false, reason: 'recently-rejected' };
    return { submit: true, reason: 'recheck-rejected', needVerify: true, priority: 1 };
  }
  if (ageMs < policy.minResubmitHours * HOUR_MS) return { submit: false, reason: 'too-soon' };
  if (cand.token && entry.token) {
    // Both sides carry a change token: that is the whole answer, except that
    // --bulk re-confirms long-unchanged URLs on a slow rotation.
    if (cand.token !== entry.token) return { submit: true, reason: 'changed', priority: 0 };
    if (mode === 'bulk' && ageMs >= policy.bulkResubmitDays * DAY_MS) return { submit: true, reason: 'stale', priority: 2 };
    return { submit: false, reason: 'unchanged' };
  }
  if (cand.token && /^\d{4}-\d{2}-\d{2}$/.test(cand.token)) {
    // Sent without a token (force queue, static) and now seen with a change
    // date: changed if that date is after the day we sent it. Without this a
    // page queued at publish time hid every later edit for the whole window.
    if (cand.token > new Date(entry.at).toISOString().slice(0, 10)) return { submit: true, reason: 'changed', priority: 0 };
    if (mode === 'bulk' && ageMs >= policy.bulkResubmitDays * DAY_MS) return { submit: true, reason: 'stale', priority: 2 };
    return { submit: false, reason: 'unchanged' };
  }
  // No comparable token (force queue, static paths, token-less sets): time window.
  const windowDays = mode === 'bulk' ? policy.bulkResubmitDays : policy.resubmitDays;
  if (ageMs >= windowDays * DAY_MS) return { submit: true, reason: 'stale', priority: 2 };
  return { submit: false, reason: 'unchanged' };
}

// ── Sitemaps ───────────────────────────────────────────────────────────────

export type SitemapEntry = { loc: string; lastmod?: string };

export function parseSitemap(xml: string): { urls: SitemapEntry[]; subSitemaps: SitemapEntry[] } {
  const urls: SitemapEntry[] = [];
  const subSitemaps: SitemapEntry[] = [];
  const isIndex = /<sitemapindex\b/i.test(xml);
  const blockRegex = isIndex
    ? /<sitemap\b[^>]*>([\s\S]*?)<\/sitemap>/gi
    : /<url\b[^>]*>([\s\S]*?)<\/url>/gi;
  let m: RegExpExecArray | null;
  while ((m = blockRegex.exec(xml)) !== null) {
    const block = m[1];
    const locMatch = /<loc>\s*([\s\S]*?)\s*<\/loc>/i.exec(block);
    if (!locMatch) continue;
    const loc = locMatch[1].trim().replace(/&amp;/g, '&');
    const lmMatch = /<lastmod>\s*([\s\S]*?)\s*<\/lastmod>/i.exec(block);
    const lastmod = lmMatch ? lmMatch[1].trim() : undefined;
    if (isIndex) subSitemaps.push({ loc, lastmod });
    else urls.push({ loc, lastmod });
  }
  return { urls, subSitemaps };
}

export type SnapshotChild = { fetchedAt: string; synthetic: boolean; entries: Record<string, string> };
export type Snapshot = { version: 1; fetchedAt: string; children: Record<string, SnapshotChild> };

/** A child sitemap whose <lastmod> is the same "today" stamp on (nearly)
 *  every URL is not carrying change information — treat only NEW locs in it
 *  as changes, never its lastmod churn. */
export function isSyntheticLastmod(entries: Record<string, string>, nowMs: number): boolean {
  const vals = Object.values(entries);
  if (vals.length < 20) return false;
  const counts = new Map<string, number>();
  for (const v of vals) { const k = normalizeToken(v); counts.set(k, (counts.get(k) || 0) + 1); }
  let modal = ''; let n = 0;
  for (const [k, c] of counts) if (c > n) { modal = k; n = c; }
  if (!modal || n / vals.length < 0.9) return false;
  const modalMs = Date.parse(modal);
  return !Number.isNaN(modalMs) && modalMs >= nowMs - 2 * DAY_MS;
}

export type SitemapCandidate = { loc: string; token: string; reason: string };

/** Diff a freshly fetched snapshot against the previous one. Children that
 *  failed this time are carried over (never read as "all URLs deleted" or,
 *  when they recover, "all URLs new"). With no previous snapshot the run is a
 *  baseline and yields no diffs. */
export function lastmodIgnored(loc: string, prefixes: string[]): boolean {
  if (!prefixes.length) return false;
  let p = loc;
  try { p = new URL(loc).pathname; } catch { /* keep raw */ }
  return prefixes.some((x) => p.startsWith(x));
}

export function diffSnapshots(prev: Snapshot | null, fetched: Record<string, SnapshotChild>,
                              ignoreLastmodPrefixes: string[] = []): SitemapCandidate[] {
  if (!prev || !Object.keys(prev.children).length) return [];
  const prevAll = new Map<string, { lastmod: string; synthetic: boolean }>();
  for (const child of Object.values(prev.children)) {
    for (const [loc, lastmod] of Object.entries(child.entries)) prevAll.set(loc, { lastmod, synthetic: child.synthetic });
  }
  const out: SitemapCandidate[] = [];
  for (const child of Object.values(fetched)) {
    for (const [loc, lastmod] of Object.entries(child.entries)) {
      const p = prevAll.get(loc);
      const ignored = child.synthetic || lastmodIgnored(loc, ignoreLastmodPrefixes);
      const token = ignored ? '' : normalizeToken(lastmod);
      if (!p) { out.push({ loc, token, reason: 'sitemap-new' }); continue; }
      if (!ignored && !p.synthetic && token && token !== normalizeToken(p.lastmod)) {
        out.push({ loc, token, reason: 'sitemap-lastmod' });
      }
    }
  }
  return out;
}

async function fetchWithTimeout(url: string, timeoutMs: number, ua: string): Promise<{ ok: boolean; status: number; text: string }> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(url, {
      signal: controller.signal,
      headers: { 'Accept': 'application/xml, text/xml, */*', 'User-Agent': ua },
    });
    const text = await res.text();
    return { ok: res.ok, status: res.status, text };
  } catch (err: any) {
    return { ok: false, status: 0, text: `fetch error: ${err.message}` };
  } finally {
    clearTimeout(timer);
  }
}

/** Fetch the sitemap tree; return the per-child snapshot for children that
 *  fetched cleanly, plus the list of children that failed. */
export type SitemapTree = {
  /** Children fetched cleanly this run. */
  fetched: Record<string, SnapshotChild>;
  /** Children that errored, timed out or collapsed. */
  failed: Set<string>;
  /** Every sitemap URL the configured roots list (fetched or not). */
  discovered: Set<string>;
  /** A configured root failed, so `discovered` is incomplete. */
  rootFailed: boolean;
};

/** Next snapshot = fresh children + the previous copy of every child that
 *  failed or was not reached. A previous child is dropped only when the
 *  roots were read cleanly and no longer list it. */
export function mergeSnapshot(prev: Snapshot | null, tree: SitemapTree, fetchedAt: string): Snapshot {
  const children: Record<string, SnapshotChild> = {};
  for (const [u, c] of Object.entries(prev?.children || {})) {
    const stillListed = tree.rootFailed || tree.discovered.has(u);
    if (stillListed && !tree.fetched[u]) children[u] = c;
  }
  Object.assign(children, tree.fetched);
  return { version: 1, fetchedAt, children };
}

async function fetchSitemapTree(site: SiteConfig, origin: string, prev: Snapshot | null, ua: string, nowMs: number): Promise<SitemapTree> {
  const fetched: Record<string, SnapshotChild> = {};
  const failed = new Set<string>();
  const discovered = new Set<string>(site.sitemapUrls || []);
  const seen = new Set<string>();
  let queue: string[] = [...(site.sitemapUrls || [])];
  let depth = 0;
  let fetches = 0;
  let rootFailed = false;
  while (queue.length && depth <= 2) {
    const next: string[] = [];
    for (const url of queue) {
      if (seen.has(url)) continue;
      seen.add(url);
      if (++fetches > MAX_SITEMAP_FETCHES) break;
      const res = await fetchWithTimeout(url, SITEMAP_FETCH_TIMEOUT_MS, ua);
      if (!res.ok) {
        console.warn(`[indexnow:${site.name}] sitemap fetch failed ${url} → HTTP ${res.status}`);
        failed.add(url);
        if (depth === 0) rootFailed = true;
        continue;
      }
      const parsed = parseSitemap(res.text);
      for (const sub of parsed.subSitemaps) {
        discovered.add(sub.loc);
        if (!seen.has(sub.loc)) next.push(sub.loc);
      }
      if (parsed.subSitemaps.length) continue;       // an index, not a urlset
      const entries: Record<string, string> = {};
      for (const e of parsed.urls) {
        // Same-origin only: IndexNow rejects a batch containing foreign hosts.
        if (!e.loc.startsWith(origin + '/') && e.loc !== origin) continue;
        entries[e.loc] = e.lastmod || '';
      }
      // Collapse guard: a child that had URLs and now returns (almost) none
      // is an upstream failure served as 200, not a mass deletion.
      const prevN = prev?.children[url] ? Object.keys(prev.children[url].entries).length : 0;
      const n = Object.keys(entries).length;
      if (prevN >= 50 && n < prevN * 0.1) {
        console.warn(`[indexnow:${site.name}] sitemap ${url} collapsed ${prevN} → ${n} URLs; keeping previous snapshot`);
        failed.add(url);
        continue;
      }
      fetched[url] = { fetchedAt: new Date(nowMs).toISOString(), synthetic: isSyntheticLastmod(entries, nowMs), entries };
    }
    queue = next;
    depth += 1;
  }
  return { fetched, failed, discovered, rootFailed };
}

function readSnapshot(file: string): Snapshot | null {
  try {
    const s = JSON.parse(fs.readFileSync(file, 'utf-8'));
    if (s && s.version === 1 && s.children) return s as Snapshot;
  } catch { /* none yet */ }
  return null;
}

// ── Page verification ──────────────────────────────────────────────────────

function parseAttrs(tag: string): Record<string, string> {
  const out: Record<string, string> = {};
  const re = /([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*("([^"]*)"|'([^']*)'|([^\s"'>]+))/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(tag)) !== null) out[m[1].toLowerCase()] = (m[3] ?? m[4] ?? m[5] ?? '').trim();
  return out;
}

function robotsNoindex(directives: string): boolean {
  return /(^|[\s,])(noindex|none)([\s,]|$)/i.test(directives);
}

/** X-Robots-Tag: "noindex, follow" or "googlebot: noindex" (scoped). Only an
 *  unscoped directive, or one scoped to bingbot / robots / *, counts here. */
export function headerNoindex(xRobotsTag: string | null | undefined): boolean {
  if (!xRobotsTag) return false;
  const known = new Set(['unavailable_after', 'max-snippet', 'max-image-preview', 'max-video-preview']);
  let scope: string | null = null;
  for (const raw of xRobotsTag.toLowerCase().split(',')) {
    let tok = raw.trim();
    const m = /^([a-z0-9_*-]+)\s*:\s*(.*)$/.exec(tok);
    if (m && !known.has(m[1])) { scope = m[1]; tok = m[2]; }
    if (robotsNoindex(tok) && (scope === null || scope === 'bingbot' || scope === 'robots' || scope === '*')) return true;
  }
  return false;
}

export type PageVerdict = { ok: boolean; reason: string };

export function classifyPage(url: string, status: number, xRobotsTag: string | null, html: string, minTextChars = 0): PageVerdict {
  if (status >= 300 && status < 400) return { ok: false, reason: `redirect-${status}` };
  if (status !== 200) return { ok: false, reason: `http-${status || 'error'}` };
  if (headerNoindex(xRobotsTag)) return { ok: false, reason: 'noindex-header' };
  const head = html.slice(0, 400_000);
  for (const m of head.matchAll(/<meta\b[^>]*>/gi)) {
    const a = parseAttrs(m[0]);
    const name = (a.name || '').toLowerCase();
    if ((name === 'robots' || name === 'bingbot') && robotsNoindex(a.content || '')) {
      return { ok: false, reason: 'noindex-meta' };
    }
  }
  for (const m of head.matchAll(/<link\b[^>]*>/gi)) {
    const a = parseAttrs(m[0]);
    if (!/(^|\s)canonical(\s|$)/i.test(a.rel || '') || !a.href) continue;
    let canon = a.href;
    try { canon = new URL(a.href, url).toString(); } catch { /* keep raw */ }
    if (normalizeUrl(canon) !== normalizeUrl(url)) return { ok: false, reason: 'canonical-elsewhere' };
    break;
  }
  if (minTextChars > 0) {
    const text = html.replace(/<script\b[\s\S]*?<\/script>/gi, ' ')
      .replace(/<style\b[\s\S]*?<\/style>/gi, ' ')
      .replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();
    if (text.length < minTextChars) return { ok: false, reason: 'thin-body' };
  }
  return { ok: true, reason: 'ok' };
}

async function verifyUrl(url: string, cfg: VerifyCfg): Promise<PageVerdict> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), cfg.timeoutMs);
  try {
    const res = await fetch(url, {
      signal: controller.signal,
      redirect: 'manual',
      headers: { 'User-Agent': cfg.userAgent, 'Accept': 'text/html,application/xhtml+xml,*/*;q=0.8' },
    });
    const body = res.status === 200 ? await res.text() : '';
    if (res.status !== 200) { try { await res.body?.cancel(); } catch { /* ignore */ } }
    return classifyPage(url, res.status, res.headers.get('x-robots-tag'), body, cfg.minTextChars);
  } catch (err: any) {
    return { ok: false, reason: err?.name === 'AbortError' ? 'timeout' : 'fetch-error' };
  } finally {
    clearTimeout(timer);
  }
}

/** Verify `urls` with bounded concurrency inside a wall-clock budget.
 *  URLs not reached before the budget runs out come back as `undone`. */
async function verifyAll(urls: string[], cfg: VerifyCfg): Promise<{ results: Map<string, PageVerdict>; undone: string[] }> {
  const results = new Map<string, PageVerdict>();
  const deadline = Date.now() + cfg.budgetSeconds * 1000;
  let i = 0;
  const worker = async () => {
    while (i < urls.length && Date.now() < deadline) {
      const u = urls[i++];
      results.set(u, await verifyUrl(u, cfg));
    }
  };
  await Promise.all(Array.from({ length: Math.max(1, cfg.concurrency) }, worker));
  return { results, undone: urls.filter((u) => !results.has(u)) };
}

// ── Force-submit queue ─────────────────────────────────────────────────────

/** One URL per line. A writer that appended to a file lacking its trailing
 *  newline splices two URLs into one line ("…/a-buildhttps://…/b" — seen in
 *  production); split those back apart rather than submit a 404. */
export function parseQueue(raw: string): string[] {
  const out = new Set<string>();
  for (const line of raw.split('\n')) {
    for (const tok of line.trim().split(/\s+/)) {
      for (const u of tok.split(/(?<!^)(?<![=?&%])(?=https?:\/\/)/)) {
        if (u.trim()) out.add(u.trim());
      }
    }
  }
  return Array.from(out);
}

function readQueue(file: string): string[] {
  try {
    return parseQueue(fs.readFileSync(file, 'utf-8'));
  } catch { return []; }
}

/** Rewrite the queue with `keep`, preserving lines appended by other writers
 *  (queue-publish.py, agent.py) since `initial` was read. */
function rewriteQueue(file: string, initial: string[], keep: string[]) {
  const initialSet = new Set(initial);
  const appended = readQueue(file).filter((u) => !initialSet.has(u));
  const out = Array.from(new Set([...keep, ...appended])).slice(-MAX_FORCE_QUEUE);
  if (!out.length && !fs.existsSync(file)) return;
  writeFileAtomic(file, out.length ? out.join('\n') + '\n' : '');
}

// ── IndexNow POST ──────────────────────────────────────────────────────────

async function submitBatch(site: SiteConfig, urls: string[]): Promise<{ ok: boolean; status: number; body: string }> {
  if (DRY) {
    console.log(`[indexnow:${site.name}] DRY-RUN — would submit ${urls.length} URLs`);
    return { ok: true, status: 0, body: '(dry-run)' };
  }
  const body = { host: site.host, key: site.key, keyLocation: `https://${site.host}/${site.key}.txt`, urlList: urls };
  try {
    const res = await fetch(ENDPOINT, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json; charset=utf-8' },
      body: JSON.stringify(body),
    });
    const text = await res.text();
    return { ok: res.ok, status: res.status, body: text.slice(0, 500) };
  } catch (err: any) {
    return { ok: false, status: 0, body: `fetch error: ${err.message}` };
  }
}

// ── Per-site run ───────────────────────────────────────────────────────────

// verify   = must pass a live fetch before submission.
// explicit = the requirement came from a querySet marked `verify: true`; a
//            trusted source for the same URL cannot waive it (an untrusted
//            source's requirement — force queue, sitemap diff — can be).
type Candidate = { url: string; source: string; token: string; verify: boolean; explicit: boolean };

function shuffle<T>(a: T[]): T[] {
  for (let i = a.length - 1; i > 0; i--) { const j = Math.floor(Math.random() * (i + 1)); [a[i], a[j]] = [a[j], a[i]]; }
  return a;
}

export async function processSite(site: SiteConfig): Promise<{ name: string; submitted: number; failed: number }> {
  const origin = (site.origin || `https://${site.host}`).replace(/\/+$/, '');
  const mode: 'bulk' | 'incremental' = BULK ? 'bulk' : 'incremental';
  const policy: Policy = { ...DEFAULT_POLICY, ...(site.policy || {}) };
  const vcfg: VerifyCfg = { ...DEFAULT_VERIFY, ...(site.verify || {}) };
  const stateDir = path.dirname(site.watermarkFile);
  const queueRoot = process.env.INDEXNOW_QUEUE_ROOT || stateDir;
  const ledgerFile = site.ledgerFile || path.join(stateDir, `${site.name}.indexnow-ledger.tsv`);
  const snapshotFile = site.snapshotFile || path.join(stateDir, `${site.name}.sitemap-snapshot.json`);
  const forceFile = site.forceSubmitFile || path.join(queueRoot, `${site.name}.force-submit.txt`);
  const since = readWatermark(site.watermarkFile);
  const nowMs = Date.now();
  const startedAt = new Date(nowMs).toISOString();
  const siteId = site.siteIdEnv ? (process.env[site.siteIdEnv] || site.siteIdFallback) : undefined;
  const siteIds = site.siteIds && site.siteIds.length ? site.siteIds : undefined;
  const verifySource = (src: string) => vcfg.enabled && vcfg.sources.includes(src);

  console.log(`[indexnow:${site.name}] started ${startedAt} mode=${mode} since=${since}${DRY ? ' DRY-RUN' : ''}`);

  const stats: Record<string, any> = {
    mode, candidates: 0, by_source: {} as Record<string, number>, skipped: {} as Record<string, number>,
    rejected: {} as Record<string, number>, verified_ok: 0, deferred: 0, retry_later: 0, sample_checked: 0, sample_bad: 0,
    query_failures: 0, sitemap: 'skipped', queue_in: 0, queue_left: 0, submitted: 0, failed: 0,
  };
  const bump = (o: Record<string, number>, k: string, n = 1) => { o[k] = (o[k] || 0) + n; };

  const cands = new Map<string, Candidate>();
  const add = (rawUrl: string, source: string, token: string, verify: boolean, explicit = false) => {
    const url = rawUrl.trim();
    if (!url.startsWith(origin + '/') && url !== origin) return;
    bump(stats.by_source, source.split(':')[0]);
    const cur = cands.get(url);
    if (!cur) { cands.set(url, { url, source, token, verify: verify || explicit, explicit }); return; }
    // A trusted source outranks an untrusted one for the same URL (a force-
    // queued product that is also a DB row needs no fetch) — unless a
    // querySet explicitly demands verification. A real token beats none.
    cur.explicit = cur.explicit || explicit;
    cur.verify = cur.explicit || (cur.verify && verify);
    if (!cur.token && token) cur.token = token;
    if (cur.source === 'force' || cur.source === 'sitemap') cur.source = source;
  };

  // 1. Query sets
  const dbUrl = process.env[site.databaseUrlEnv] || site.databaseUrlFallback;
  if (!process.env[site.databaseUrlEnv] && site.databaseUrlFallback) {
    console.warn(`[indexnow:${site.name}] ${site.databaseUrlEnv} unset — using databaseUrlFallback from the config file (a tracked credential; set the env var instead)`);
  }
  const activeSets = site.querySets.filter((qs) => (BULK ? !qs.incrementalOnly : !qs.bulkOnly && !!qs.incrementalSql));
  if (activeSets.length && !dbUrl) {
    console.error(`[indexnow:${site.name}] no DB URL: set ${site.databaseUrlEnv}`);
    stats.query_failures += activeSets.length;
  } else if (activeSets.length) {
    const pool = new Pool({ connectionString: dbUrl, max: 2, statement_timeout: policy.queryTimeoutMs } as any);
    try {
      for (const qs of activeSets) {
        const sql = interpolateSiteIds(BULK ? qs.bulkSql : qs.incrementalSql!, siteId, siteIds);
        const excl = Object.entries(qs.excludeIfMatches || {}).map(([col, re]) => [col, new RegExp(re, 'i')] as const);
        const lastmodCol = qs.lastmodColumn || 'lastmod';
        try {
          const { rows } = await pool.query(sql, BULK ? [] : [since]);
          let n = 0;
          for (const r of rows) {
            if (excl.some(([col, re]) => re.test(String(r[col] ?? '')))) { bump(stats.skipped, 'excluded-by-config'); continue; }
            const u = buildUrl(origin, qs, r);
            if (!u) continue;
            add(u, `query:${qs.name}`, normalizeToken(r[lastmodCol]), false, qs.verify === true);
            n += 1;
          }
          console.log(`[indexnow:${site.name}] query ${qs.name}: ${n} URLs`);
        } catch (err: any) {
          stats.query_failures += 1;
          console.warn(`[indexnow:${site.name}] query ${qs.name} failed: ${err.message}`);
        }
      }
    } finally {
      await pool.end();
    }
  }

  // 2. Force-submit queue (incremental only — bulk must not race the drain)
  const queueInitial = BULK ? [] : readQueue(forceFile);
  stats.queue_in = queueInitial.length;
  for (const u of queueInitial) add(u, 'force', '', verifySource('force'));

  // 3. Sitemaps
  let newSnapshot: Snapshot | null = null;
  const prevSnapshot = readSnapshot(snapshotFile);
  const snapshotAgeH = prevSnapshot ? (nowMs - Date.parse(prevSnapshot.fetchedAt)) / HOUR_MS : Infinity;
  if (!NO_SITEMAP && site.sitemapUrls && site.sitemapUrls.length && (BULK || snapshotAgeH >= policy.sitemapIntervalHours)) {
    const tree = await fetchSitemapTree(site, origin, prevSnapshot, vcfg.userAgent, nowMs);
    newSnapshot = mergeSnapshot(prevSnapshot, tree, startedAt);
    if (BULK) {
      for (const c of Object.values(tree.fetched)) {
        for (const [loc, lm] of Object.entries(c.entries)) {
          const ignored = c.synthetic || lastmodIgnored(loc, policy.ignoreLastmodPrefixes);
          add(loc, 'sitemap', ignored ? '' : normalizeToken(lm), false);
        }
      }
    } else {
      for (const d of diffSnapshots(prevSnapshot, tree.fetched, policy.ignoreLastmodPrefixes)) {
        add(d.loc, 'sitemap', d.token, verifySource('sitemap'));
      }
    }
    const nUrls = Object.values(tree.fetched).reduce((a, c) => a + Object.keys(c.entries).length, 0);
    stats.sitemap = `${Object.keys(tree.fetched).length} ok / ${tree.failed.size} failed / ${nUrls} urls${prevSnapshot ? '' : ' (baseline)'}`;
  }

  // 4. Static paths (bulk only — they are not "changed" on every tick)
  if (BULK) for (const p of site.staticPaths || []) add(`${origin}${p}`, 'static', '', verifySource('static'));

  stats.candidates = cands.size;

  // 5. Ledger gate
  const ledger = readLedger(ledgerFile);
  const ledgerUpdates: Ledger = new Map();
  type Keep = Candidate & { priority: number; lastAt: number };
  let keep: Keep[] = [];
  for (const c of cands.values()) {
    const entry = ledger.get(c.url);
    const d = decideCandidate(entry, c, nowMs, mode, policy);
    if (!d.submit) { bump(stats.skipped, d.reason); continue; }
    keep.push({ ...c, verify: c.verify || !!d.needVerify, priority: d.priority ?? 0, lastAt: entry?.at ?? 0 });
  }
  if (BULK && policy.bulkMaxPerRun > 0 && keep.length > policy.bulkMaxPerRun) {
    // New/changed first; within a tier the small verified sets (static
    // paths, hubs) before the big catalog sets; then least recently sent.
    keep.sort((a, b) => a.priority - b.priority || Number(b.verify) - Number(a.verify) || a.lastAt - b.lastAt);
    bump(stats.skipped, 'bulk-cap', keep.length - policy.bulkMaxPerRun);
    keep = keep.slice(0, policy.bulkMaxPerRun);
  }

  // 6. Verification
  const deferredForce: string[] = [];
  if (vcfg.enabled) {
    const must = keep.filter((c) => c.verify);
    const trusted = keep.filter((c) => !c.verify);
    const sampleN = Math.min(trusted.length, vcfg.maxPerRun, BULK ? vcfg.sampleTrustedBulk : vcfg.sampleTrusted);
    const sample = shuffle(trusted.slice()).slice(0, sampleN).map((c) => c.url);
    // Force-queue URLs first (FIFO), then the rest.
    must.sort((a, b) => (a.source === 'force' ? 0 : 1) - (b.source === 'force' ? 0 : 1));
    const toCheck = must.slice(0, Math.max(0, vcfg.maxPerRun - sample.length)).map((c) => c.url);
    const overflow = must.slice(toCheck.length).map((c) => c.url);
    const { results, undone } = await verifyAll([...toCheck, ...sample], vcfg);
    const drop = new Set<string>();
    for (const u of [...overflow, ...undone]) {
      drop.add(u);
      stats.deferred += 1;
      if (!BULK) deferredForce.push(u);      // retried on the next incremental tick
    }
    const sampleSet = new Set(sample);
    const examples: string[] = [];
    for (const [u, v] of results) {
      const transient = !v.ok && isTransientReason(v.reason);
      if (sampleSet.has(u)) { stats.sample_checked += 1; if (!v.ok && !transient) stats.sample_bad += 1; }
      if (v.ok) { if (!sampleSet.has(u)) stats.verified_ok += 1; continue; }
      drop.add(u);
      bump(stats.rejected, v.reason);
      if (examples.length < 12) examples.push(`${v.reason}${sampleSet.has(u) ? '(sample)' : ''} ${u}`);
      if (transient) {
        // Says nothing about the page: retry (bounded) instead of parking a
        // new article for rejectRecheckDays because one fetch timed out.
        const prev = ledger.get(u);
        const attempts = (prev && prev.status.startsWith('tmp:') ? (Number(prev.token) || 1) : 0) + 1;
        ledgerUpdates.set(u, { status: `tmp:${v.reason}`, at: nowMs, token: String(attempts) });
        if (!BULK && attempts < policy.transientMaxAttempts) { deferredForce.push(u); stats.retry_later += 1; }
        continue;
      }
      ledgerUpdates.set(u, { status: `rej:${v.reason}`, at: nowMs, token: '' });
    }
    if (examples.length) console.log(`[indexnow:${site.name}] rejected e.g.:\n  ${examples.join('\n  ')}`);
    if (stats.sample_bad) {
      console.warn(`[indexnow:${site.name}] drift canary: ${stats.sample_bad}/${stats.sample_checked} sampled DB URLs failed verification — check the querySet SQL against the site's noindex/404 rules`);
    }
    keep = keep.filter((c) => !drop.has(c.url));
  }

  // 7. Submit
  const urlList = keep.map((c) => c.url);
  const tokenOf = new Map(keep.map((c) => [c.url, c.token] as const));
  const sourceOf = new Map(keep.map((c) => [c.url, c.source] as const));
  console.log(`[indexnow:${site.name}] candidates=${stats.candidates} → submit=${urlList.length} ` +
    `skipped=${JSON.stringify(stats.skipped)} rejected=${JSON.stringify(stats.rejected)} deferred=${stats.deferred} sitemap=${stats.sitemap}`);
  let submitted = 0;
  let failed = 0;
  const failedForce: string[] = [];
  const queueSet = new Set(queueInitial);
  for (let i = 0; i < urlList.length; i += BATCH_SIZE) {
    const batch = urlList.slice(i, i + BATCH_SIZE);
    const result = await submitBatch(site, batch);
    if (result.ok) {
      submitted += batch.length;
      for (const u of batch) ledgerUpdates.set(u, { status: 'sent', at: nowMs, token: tokenOf.get(u) || '' });
      console.log(`[indexnow:${site.name}] batch ${Math.floor(i / BATCH_SIZE) + 1}: submitted ${batch.length} (HTTP ${result.status})`);
    } else {
      failed += batch.length;
      // DB rows come back via the un-advanced watermark; queue URLs and
      // sitemap diffs would be lost (the snapshot below still advances), so
      // they go back on the queue.
      for (const u of batch) {
        const src = sourceOf.get(u) || '';
        if (queueSet.has(u) || src === 'sitemap' || src === 'force') failedForce.push(u);
      }
      console.error(`[indexnow:${site.name}] batch ${Math.floor(i / BATCH_SIZE) + 1}: FAIL HTTP ${result.status} — ${result.body}`);
    }
  }
  stats.submitted = submitted;
  stats.failed = failed;

  // 8. Persist state (never on a dry run)
  if (!DRY) {
    if (ledgerUpdates.size) writeLedger(ledgerFile, ledgerUpdates, policy.ledgerRetentionDays, nowMs);
    if (newSnapshot) writeFileAtomic(snapshotFile, JSON.stringify(newSnapshot));
    if (!BULK) {
      const left = Array.from(new Set([...deferredForce.filter((u) => u), ...failedForce]));
      rewriteQueue(forceFile, queueInitial, left);
      stats.queue_left = readQueue(forceFile).length;
    }
    // The watermark is the INCREMENTAL cursor. --bulk skips incrementalOnly
    // sets and caps its output, so it must not move it (it used to, and could
    // jump past rows an earlier failed incremental tick never read).
    if (!BULK && failed === 0 && stats.query_failures === 0) writeFileAtomic(site.watermarkFile, startedAt);
  }

  console.log(`[indexnow:${site.name}] stats ${JSON.stringify(stats)}`);
  console.log(`[indexnow:${site.name}] done submitted=${submitted} failed=${failed}`);
  return { name: site.name, submitted, failed };
}

async function main() {
  const sites = loadSites().filter((s) => !ONLY_SITE || s.name === ONLY_SITE);
  if (sites.length === 0) {
    console.error(`[indexnow] no sites matched --site=${ONLY_SITE}`);
    process.exit(1);
  }
  const summaries: Array<{ name: string; submitted: number; failed: number }> = [];
  for (const site of sites) {
    try {
      summaries.push(await processSite(site));
    } catch (err: any) {
      console.error(`[indexnow:${site.name}] fatal:`, err.message);
      summaries.push({ name: site.name, submitted: 0, failed: -1 });
    }
  }
  console.log('[indexnow] summary ' + summaries.map((s) => `${s.name}:${s.submitted}/${s.failed}`).join(' '));
  process.exit(summaries.some((s) => s.failed < 0) ? 1 : 0);
}

if (require.main === module) {
  main().catch((err) => {
    console.error('[indexnow] fatal:', err);
    process.exit(1);
  });
}
