/**
 * Site-agnostic IndexNow submitter.
 *
 * Runs every 15 minutes via the reusable-agents framework, pushing
 * fresh / changed URLs for each configured site (aisleprompt.com,
 * specpicks.com, ...) to Bing, Yandex, Seznam, and Naver via the
 * IndexNow protocol. Indexing latency on the search-engine side is
 * 10-60 min regardless of how fast we push, so a 15-min tick with
 * a DB-query watermark is plenty.
 *
 * URLs come from THREE sources, deduped before submission:
 *   1. staticPaths       — hardcoded SEO landing pages
 *   2. querySets         — DB tables with watermark (incremental) or bulk
 *   3. sitemapUrls       — fetch & parse the site's own sitemap.xml,
 *                          filter <url> entries by <lastmod> >= watermark.
 *                          Catches anything we forgot to model in querySets.
 *
 * Sites are configured in `sites.json`. Each site supplies:
 *   - host, key, key file location (hosted at https://<host>/<key>.txt)
 *   - DB connection string (via env var with fallback)
 *   - query sets that produce URL lists
 *   - sitemap URLs to fetch as a safety net
 *   - watermark file path for incremental submits
 *
 * Run manually:
 *   npx ts-node submit.ts               # all sites, incremental
 *   npx ts-node submit.ts --site=aisleprompt
 *   npx ts-node submit.ts --bulk        # one-time full catalog re-submit
 *   npx ts-node submit.ts --dry-run     # log candidates without calling the API
 *   npx ts-node submit.ts --no-sitemap  # skip sitemap fetch (DB-only run)
 */

import { Pool } from 'pg';
import fs from 'fs';
import path from 'path';

type QuerySet = {
  name: string;
  bulkOnly?: boolean;
  bulkSql: string;
  incrementalSql?: string;
  urlTemplate: string;
  urlPrefix: string;
};

type SiteConfig = {
  name: string;
  host: string;
  key: string;
  databaseUrlEnv: string;
  databaseUrlFallback: string;
  siteIdEnv?: string;
  siteIdFallback?: string;
  siteIds?: string[];
  watermarkFile: string;
  staticPaths: string[];
  querySets: QuerySet[];
  sitemapUrls?: string[];
};

const BULK = process.argv.includes('--bulk');
const DRY = process.argv.includes('--dry-run');
const NO_SITEMAP = process.argv.includes('--no-sitemap');
const ONLY_SITE = (process.argv.find((a) => a.startsWith('--site=')) || '').slice('--site='.length) || null;

const ENDPOINT = 'https://api.indexnow.org/indexnow';
const BATCH_SIZE = 10_000;
const SITEMAP_FETCH_TIMEOUT_MS = 30_000;

function loadSites(): SiteConfig[] {
  // Site-specific configs live in each site's repo:
  //   $SITE_CONFIG_PATHS (comma-sep) — explicit list, used by per-site wrappers
  //   ~/development/<site>/agents/seo-config/site-indexnow.json — fallback discovery
  //   ./sites.json — legacy multi-site config in this dir (deprecated, kept as last-resort fallback)
  // Each file is { "sites": [...] }; we merge.
  const out: SiteConfig[] = [];
  const explicit = process.env.SITE_CONFIG_PATHS;
  if (explicit) {
    for (const p of explicit.split(',').map(s => s.trim()).filter(Boolean)) {
      try {
        const raw = fs.readFileSync(p, 'utf-8');
        out.push(...(JSON.parse(raw).sites as SiteConfig[]));
      } catch (e) {
        console.error(`[indexnow] could not load site config ${p}: ${e}`);
      }
    }
    if (out.length) return out;
  }
  // Auto-discover per-site configs in standard locations
  const homeDev = process.env.HOME ? path.join(process.env.HOME, 'development') : '';
  const siteRepos = ['aisleprompt', 'specpicks'];
  for (const site of siteRepos) {
    const candidate = path.join(homeDev, site, 'agents', 'seo-config', 'site-indexnow.json');
    if (fs.existsSync(candidate)) {
      try {
        const raw = fs.readFileSync(candidate, 'utf-8');
        out.push(...(JSON.parse(raw).sites as SiteConfig[]));
      } catch (e) {
        console.error(`[indexnow] could not load ${candidate}: ${e}`);
      }
    }
  }
  if (out.length) return out;
  // Last-resort fallback to in-repo legacy sites.json
  const legacy = path.join(__dirname, 'sites.json');
  if (fs.existsSync(legacy)) {
    const raw = fs.readFileSync(legacy, 'utf-8');
    return JSON.parse(raw).sites as SiteConfig[];
  }
  return [];
}

function readWatermark(file: string): string {
  try {
    const raw = fs.readFileSync(file, 'utf-8').trim();
    if (raw && !Number.isNaN(Date.parse(raw))) return raw;
  } catch { /* first run */ }
  return new Date(0).toISOString();
}

function writeWatermark(file: string, iso: string) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, iso, 'utf-8');
}

function slugify(text: string): string {
  return String(text || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').substring(0, 80);
}

function buildUrl(origin: string, qs: QuerySet, row: Record<string, any>): string | null {
  // Template forms:
  //   "slug"                     → row.slug
  //   "slugify:title|-|id"       → `${slugify(row.title)}-${row.id}`
  //   "slugify:title|_|id"       → `${slugify(row.title)}_${row.id}`
  //   "slugify:slug"             → `${slugify(row.slug)}` (used for raw category names)
  //   "compose:left_ref|/|right_ref" → `${row.left_ref}/${row.right_ref}` (no slugify)
  const tpl = qs.urlTemplate;
  let pathPart: string;
  if (tpl === 'slug') {
    if (!row.slug) return null;
    pathPart = String(row.slug);
  } else if (tpl.startsWith('slugify:')) {
    const rest = tpl.slice('slugify:'.length);
    const parts = rest.split('|');
    pathPart = parts.map((p) => {
      if (p.length === 0) return '';
      if (p === '-' || p === '_' || /^[-_.\\/]+$/.test(p)) return p;
      return slugify(row[p]);
    }).join('');
    if (!pathPart.replace(/[-_.\\/]/g, '')) return null;
  } else if (tpl.startsWith('compose:')) {
    const rest = tpl.slice('compose:'.length);
    const parts = rest.split('|');
    pathPart = parts.map((p) => {
      if (p.length === 0) return '';
      if (/^[-_.\\/]+$/.test(p)) return p;
      const v = row[p];
      if (v === null || v === undefined || v === '') return '';
      return String(v);
    }).join('');
    if (!pathPart.replace(/[-_.\\/]/g, '')) return null;
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

async function collectUrlsForQuerySet(
  pool: Pool,
  qs: QuerySet,
  origin: string,
  since: string,
  siteId?: string,
  siteIds?: string[]
): Promise<string[]> {
  const useBulk = BULK || qs.bulkOnly;
  if (!useBulk && !qs.incrementalSql) return [];
  const rawSql = useBulk ? qs.bulkSql : qs.incrementalSql!;
  const sql = interpolateSiteIds(rawSql, siteId, siteIds);
  const params = useBulk ? [] : [since];
  try {
    const { rows } = await pool.query(sql, params);
    const urls: string[] = [];
    for (const r of rows) {
      const u = buildUrl(origin, qs, r);
      if (u) urls.push(u);
    }
    return urls;
  } catch (err: any) {
    console.warn(`[indexnow:${qs.name}] query failed: ${err.message}`);
    return [];
  }
}

async function fetchWithTimeout(url: string, timeoutMs: number): Promise<{ ok: boolean; status: number; text: string }> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(url, {
      signal: controller.signal,
      headers: { 'Accept': 'application/xml, text/xml, */*' },
    });
    const text = await res.text();
    return { ok: res.ok, status: res.status, text };
  } catch (err: any) {
    return { ok: false, status: 0, text: `fetch error: ${err.message}` };
  } finally {
    clearTimeout(timer);
  }
}

// Minimal sitemap parser — pulls every <loc> and the matching <lastmod> if
// present. Handles both <urlset> and <sitemapindex> forms. We follow
// sitemap-index entries one level deep so a site can split its sitemap into
// sub-sitemaps without us missing URLs.
type SitemapEntry = { loc: string; lastmod?: string };

function parseSitemap(xml: string): { urls: SitemapEntry[]; subSitemaps: SitemapEntry[] } {
  const urls: SitemapEntry[] = [];
  const subSitemaps: SitemapEntry[] = [];
  const isIndex = /<sitemapindex\b/i.test(xml);
  // Each <url> or <sitemap> block contains one <loc> and an optional <lastmod>.
  const blockRegex = isIndex
    ? /<sitemap\b[^>]*>([\s\S]*?)<\/sitemap>/gi
    : /<url\b[^>]*>([\s\S]*?)<\/url>/gi;
  let m: RegExpExecArray | null;
  while ((m = blockRegex.exec(xml)) !== null) {
    const block = m[1];
    const locMatch = /<loc>\s*([\s\S]*?)\s*<\/loc>/i.exec(block);
    if (!locMatch) continue;
    const loc = locMatch[1].trim();
    const lmMatch = /<lastmod>\s*([\s\S]*?)\s*<\/lastmod>/i.exec(block);
    const lastmod = lmMatch ? lmMatch[1].trim() : undefined;
    if (isIndex) subSitemaps.push({ loc, lastmod });
    else urls.push({ loc, lastmod });
  }
  return { urls, subSitemaps };
}

async function collectUrlsFromSitemaps(
  site: SiteConfig,
  since: string,
  origin: string
): Promise<string[]> {
  if (NO_SITEMAP || !site.sitemapUrls || !site.sitemapUrls.length) return [];
  const sinceMs = Date.parse(since);
  const seen = new Set<string>();
  const out = new Set<string>();
  const queue: string[] = [...site.sitemapUrls];
  let depth = 0;
  const MAX_DEPTH = 2;
  const MAX_FETCHES = 25;
  let fetches = 0;

  while (queue.length && depth <= MAX_DEPTH && fetches < MAX_FETCHES) {
    const next: string[] = [];
    for (const url of queue) {
      if (seen.has(url)) continue;
      seen.add(url);
      fetches += 1;
      if (fetches > MAX_FETCHES) break;
      const res = await fetchWithTimeout(url, SITEMAP_FETCH_TIMEOUT_MS);
      if (!res.ok) {
        console.warn(`[indexnow:${site.name}] sitemap fetch failed ${url} → HTTP ${res.status}`);
        continue;
      }
      const parsed = parseSitemap(res.text);
      for (const sub of parsed.subSitemaps) {
        if (!seen.has(sub.loc)) next.push(sub.loc);
      }
      for (const entry of parsed.urls) {
        // Only emit URLs that belong to our site (some sitemaps include
        // cross-domain links — skip those, IndexNow rejects them).
        if (!entry.loc.startsWith(origin + '/') && entry.loc !== origin) continue;
        if (BULK) {
          out.add(entry.loc);
          continue;
        }
        // Incremental: include only entries newer than the watermark. If
        // <lastmod> is missing, we err on the side of inclusion (the
        // watermark + DB queries already cover the common case, and a
        // missing lastmod usually means a static / always-fresh page).
        if (!entry.lastmod) {
          out.add(entry.loc);
          continue;
        }
        const lmMs = Date.parse(entry.lastmod);
        if (Number.isNaN(lmMs)) {
          out.add(entry.loc);
          continue;
        }
        if (lmMs >= sinceMs) out.add(entry.loc);
      }
    }
    queue.length = 0;
    queue.push(...next);
    depth += 1;
  }
  return Array.from(out);
}

async function submitBatch(site: SiteConfig, urls: string[]): Promise<{ ok: boolean; status: number; body: string }> {
  if (DRY) {
    console.log(`[indexnow:${site.name}] DRY-RUN — would submit ${urls.length} URLs`);
    return { ok: true, status: 0, body: '(dry-run)' };
  }
  const body = {
    host: site.host,
    key: site.key,
    keyLocation: `https://${site.host}/${site.key}.txt`,
    urlList: urls,
  };
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

async function processSite(site: SiteConfig): Promise<{ name: string; submitted: number; failed: number }> {
  const origin = `https://${site.host}`;
  const dbUrl = process.env[site.databaseUrlEnv] || site.databaseUrlFallback;
  const siteId = site.siteIdEnv ? (process.env[site.siteIdEnv] || site.siteIdFallback) : undefined;
  const siteIds = site.siteIds && site.siteIds.length ? site.siteIds : undefined;
  const since = readWatermark(site.watermarkFile);
  const startedAt = new Date().toISOString();

  const idLabel = siteIds ? `site_ids=${siteIds.join(',')}` : siteId ? `site_id=${siteId}` : '';
  console.log(`[indexnow:${site.name}] started ${startedAt} mode=${BULK ? 'bulk' : 'incremental'} since=${since}${idLabel ? ' ' + idLabel : ''}`);

  const pool = new Pool({ connectionString: dbUrl, max: 2 });
  let submitted = 0;
  let failed = 0;
  try {
    const allUrls = new Set<string>();

    const qsResults = await Promise.all(
      site.querySets.map((qs) => collectUrlsForQuerySet(pool, qs, origin, since, siteId, siteIds))
    );
    const counts: Record<string, number> = {};
    site.querySets.forEach((qs, i) => {
      counts[qs.name] = qsResults[i].length;
      qsResults[i].forEach((u) => allUrls.add(u));
    });

    const sitemapUrls = await collectUrlsFromSitemaps(site, since, origin);
    let sitemapNew = 0;
    for (const u of sitemapUrls) {
      if (!allUrls.has(u)) sitemapNew += 1;
      allUrls.add(u);
    }

    const hasDynamicChanges = allUrls.size > 0;
    const shouldSubmitStatic = BULK || hasDynamicChanges;
    if (shouldSubmitStatic) {
      for (const p of site.staticPaths) allUrls.add(`${origin}${p}`);
    }

    const urlList = Array.from(allUrls);
    console.log(`[indexnow:${site.name}] candidates: ${Object.entries(counts).map(([k, v]) => `${k}=${v}`).join(' ')} sitemap=${sitemapUrls.length}(+${sitemapNew} new) static=${shouldSubmitStatic ? site.staticPaths.length : 0} → total=${urlList.length}`);

    if (urlList.length === 0) {
      console.log(`[indexnow:${site.name}] nothing to submit`);
      writeWatermark(site.watermarkFile, startedAt);
      return { name: site.name, submitted: 0, failed: 0 };
    }

    for (let i = 0; i < urlList.length; i += BATCH_SIZE) {
      const batch = urlList.slice(i, i + BATCH_SIZE);
      const result = await submitBatch(site, batch);
      if (result.ok) {
        submitted += batch.length;
        console.log(`[indexnow:${site.name}] batch ${Math.floor(i / BATCH_SIZE) + 1}: submitted ${batch.length} (HTTP ${result.status})`);
      } else {
        failed += batch.length;
        console.error(`[indexnow:${site.name}] batch ${Math.floor(i / BATCH_SIZE) + 1}: FAIL HTTP ${result.status} — ${result.body}`);
      }
    }

    if (failed === 0) writeWatermark(site.watermarkFile, startedAt);
    console.log(`[indexnow:${site.name}] done submitted=${submitted} failed=${failed}`);
  } finally {
    await pool.end();
  }
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

main().catch((err) => {
  console.error('[indexnow] fatal:', err);
  process.exit(1);
});
