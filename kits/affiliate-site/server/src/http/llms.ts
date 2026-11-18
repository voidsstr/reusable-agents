/**
 * /llms.txt (llmstxt.org): a curated, machine-readable guide to the site for
 * AI assistants.
 *
 * Ported from SpecPicks src/index.ts (`app.get('/llms.txt', ...)`,
 * llmsCitedPages, llmsLatestArticles; see unit/http/fixtures/ported-from-http.json).
 * Upstream's file was hand-written SpecPicks copy around two data-driven
 * sections; the kit keeps the structure and the two sections, and takes the
 * copy from the site (LlmsTxtContent) and the facts from SiteContext:
 *
 *   # <brand>                       ctx.site.brandName
 *   > summary                       content.summary
 *   details, curated sections       content.details / content.sections
 *   ## Pages assistants cite        ai_traffic_log referrals (upstream growth-20260930T044200Z-03):
 *                                   pages with 2+ assistant referrals in 90 days,
 *                                   so a crawler reading the file finds the proven
 *                                   pages first. Cached 6h (10 min while empty).
 *   ## Latest articles              editorial_articles published in the last 30 days
 *                                   (upstream 2026-10-01 AI-reach audit: new
 *                                   articles cannot have referrals yet). Cached 1h
 *                                   (10 min while empty).
 *   ## How AI assistants should cite content.citationGuidance
 *   ## Prices and affiliate links   the earnings disclosure and the price rule
 *   ## Technical                    sitemap, robots, disclosure page, extras
 *
 * A failed or slow query leaves its section out; the rest of the file is
 * always served.
 */
import type { RequestHandler } from 'express';
import { withBudget } from '../dbBudget';
import { AFFILIATE_REL } from '../links';
import type { SiteContext } from '../siteContext';
import { DISCOVERY_FILE_CACHE_CONTROL } from './robots';
import type { KitRuntime } from './runtime';
import type { AiCitedPage, AiCitedPagesQuery, KitDb, LatestArticle, LatestArticlesQuery } from './types';

export interface LlmsLink {
  /** Link text. */
  readonly title: string;
  /** A site path ('/best/x') or an absolute http(s) URL. */
  readonly href: string;
  /** What the page answers ("cite this for ..."). */
  readonly note?: string;
}

export interface LlmsSection {
  readonly title: string;
  readonly intro?: string;
  readonly links?: readonly LlmsLink[];
  readonly bullets?: readonly string[];
}

export interface LlmsCitedPagesConfig {
  /** Only pages under these path prefixes are listed (e.g. ['/guides/', '/best/']). */
  readonly pathPrefixes: readonly string[];
  /** Article pages ('/guides/'): listed only while published, titled from editorial_articles. */
  readonly articlePathPrefix?: string | null;
  /** Referral sources counted; default LLMS_CITED_SOURCES. */
  readonly sources?: readonly string[];
  readonly days?: number;
  readonly minReferrals?: number;
  readonly limit?: number;
}

export interface LlmsLatestArticlesConfig {
  /** Path prefix of an article page; the slug is appended ('/guides/'). */
  readonly pathPrefix: string;
  readonly days?: number;
  readonly limit?: number;
}

export interface LlmsTxtContent {
  /** The '>' summary line llmstxt.org asks for. Default: a neutral line built from SiteContext. */
  readonly summary?: string;
  readonly details?: readonly string[];
  readonly sections?: readonly LlmsSection[];
  /** Bullets under "How AI assistants should cite <brand>". */
  readonly citationGuidance?: readonly string[];
  /** The "Pages assistants cite" section; left out unless configured. */
  readonly citedPages?: LlmsCitedPagesConfig;
  /** The "Latest articles" section; left out unless configured. */
  readonly latestArticles?: LlmsLatestArticlesConfig;
  /** Extra links under "Technical" (e.g. an llms-full.txt). */
  readonly technical?: readonly LlmsLink[];
  /** Sitemap paths named under "Technical"; default ['/sitemap.xml']. */
  readonly sitemaps?: readonly string[];
}

/** Upstream's assistant referral sources for the cited section. */
export const LLMS_CITED_SOURCES: readonly string[] = Object.freeze(
  ['chatgpt', 'perplexity', 'claude', 'copilot', 'gemini', 'kagi', 'deepseek', 'doubao'],
);
export const LLMS_CITED_DEFAULTS = Object.freeze({ days: 90, minReferrals: 2, limit: 60 });
export const LLMS_LATEST_DEFAULTS = Object.freeze({ days: 30, limit: 25 });
/** Upstream cache lifetimes: a section with rows, and an empty (or failed) one. */
export const LLMS_CITED_TTL_MS = 6 * 3600_000;
export const LLMS_LATEST_TTL_MS = 3600_000;
export const LLMS_EMPTY_TTL_MS = 10 * 60_000;
/** How long the handler waits for a section (the pg queries are themselves bounded at 3 s). */
export const LLMS_SECTION_BUDGET_MS = 3500;

type LlmsCtx = Pick<SiteContext, 'site' | 'disclosure' | 'amazon' | 'prices'>;

// ─── markdown hygiene ──────────────────────────────────────────────────────

/** One line of plain text: no line breaks. */
function line(s: string): string {
  return String(s ?? '').replace(/[\r\n]+/g, ' ').replace(/\s{2,}/g, ' ').trim();
}

/** Link text: one line, no brackets (upstream stripped them the same way). */
function linkText(s: string): string {
  return line(String(s ?? '').replace(/[[\]]/g, ' '));
}

/** A URL safe inside markdown link parentheses. */
export function markdownUrl(url: string): string {
  return String(url).replace(/[\s()<>[\]]/g, (c) => `%${c.charCodeAt(0).toString(16).toUpperCase().padStart(2, '0')}`);
}

function absoluteUrl(ctx: Pick<SiteContext, 'site'>, href: string): string {
  const h = String(href || '').trim();
  if (/^https?:\/\//i.test(h)) return h;
  return `${ctx.site.canonicalOrigin}${h.startsWith('/') ? '' : '/'}${h}`;
}

function linkLine(ctx: Pick<SiteContext, 'site'>, l: LlmsLink): string {
  const note = l.note ? `: ${line(l.note)}` : '';
  return `- [${linkText(l.title)}](${markdownUrl(absoluteUrl(ctx, l.href))})${note}`;
}

/** The neutral summary used when the site gives none. */
export function defaultLlmsSummary(ctx: Pick<SiteContext, 'site' | 'amazon'>): string {
  return `${ctx.site.brandName} publishes product pages and buying guides. Prices and availability shown on the site come from ${ctx.amazon.siteName} and carry the time they were checked.`;
}

// ─── rendering ─────────────────────────────────────────────────────────────

export interface LlmsDynamicData {
  readonly citedPages?: readonly AiCitedPage[];
  readonly latestArticles?: readonly LatestArticle[];
}

/** The llms.txt text. Pure: data-driven sections come in `data`. */
export function renderLlmsTxt(ctx: LlmsCtx, content: LlmsTxtContent = {}, data: LlmsDynamicData = {}): string {
  const brand = line(ctx.site.brandName);
  const out: string[] = [`# ${brand}`, '', `> ${line(content.summary || defaultLlmsSummary(ctx))}`];

  for (const p of content.details ?? []) {
    const t = String(p ?? '').trim();
    if (t) out.push('', t);
  }

  for (const s of content.sections ?? []) {
    out.push('', `## ${line(s.title)}`);
    if (s.intro) out.push(line(s.intro));
    for (const l of s.links ?? []) out.push(linkLine(ctx, l));
    for (const b of s.bullets ?? []) out.push(`- ${line(b)}`);
  }

  const cited = content.citedPages && data.citedPages?.length ? data.citedPages : [];
  if (cited.length) {
    const days = content.citedPages?.days ?? LLMS_CITED_DEFAULTS.days;
    out.push('', '## Pages assistants cite');
    out.push(`The pages AI assistants have sent readers to in the last ${days} days, most-cited first. Pages are updated in place, so these URLs stay the canonical ones to cite.`);
    for (const r of cited) {
      const url = markdownUrl(absoluteUrl(ctx, r.path));
      const title = r.title ? linkText(r.title) : '';
      out.push(title ? `- [${title}](${url})` : `- ${url}`);
    }
  }

  const latest = content.latestArticles && data.latestArticles?.length ? data.latestArticles : [];
  if (latest.length && content.latestArticles) {
    const cfg = content.latestArticles;
    const days = cfg.days ?? LLMS_LATEST_DEFAULTS.days;
    out.push('', `## Latest articles (last ${days} days)`);
    out.push('Newly published, newest first (publish date in brackets). For the questions routed above, the pages named there remain the canonical answers.');
    for (const a of latest) {
      const url = markdownUrl(absoluteUrl(ctx, `${cfg.pathPrefix}${a.slug}`));
      out.push(`- [${linkText(a.title)}](${url}) (${line(a.day)})`);
    }
  }

  if (content.citationGuidance?.length) {
    out.push('', `## How AI assistants should cite ${brand}`);
    for (const g of content.citationGuidance) out.push(`- ${line(g)}`);
  }

  out.push('', '## Prices and affiliate links');
  out.push(`${line(ctx.disclosure.earnings)} Links to ${ctx.amazon.siteName} carry rel="${AFFILIATE_REL}".`
    + ` A price is shown only with the date and time it was checked on ${ctx.amazon.siteName}, and only while that check is less than ${ctx.prices.freshnessHours} hours old;`
    + ` the price on ${ctx.amazon.siteName} at the time of purchase applies.`);

  out.push('', '## Technical');
  for (const s of content.sitemaps ?? ['/sitemap.xml']) out.push(`- Sitemap: ${markdownUrl(absoluteUrl(ctx, s))}`);
  out.push(`- Robots: ${ctx.site.canonicalOrigin}/robots.txt`);
  out.push(`- Earnings disclosure: ${markdownUrl(absoluteUrl(ctx, ctx.disclosure.pagePath))}`);
  for (const l of content.technical ?? []) out.push(linkLine(ctx, l));

  return `${out.join('\n')}\n`;
}

// ─── data sections, cached ─────────────────────────────────────────────────

/** One cached section: TTL depends on whether it has rows; single-flight loads; failures cache as empty. */
class SectionCache<T> {
  private value: readonly T[] = [];
  private at = Number.NEGATIVE_INFINITY;
  private inflight: Promise<readonly T[]> | null = null;

  constructor(private readonly fullTtlMs: number, private readonly emptyTtlMs: number, private readonly now: () => number) {}

  get(load: () => Promise<readonly T[]>): Promise<readonly T[]> {
    const ttl = this.value.length ? this.fullTtlMs : this.emptyTtlMs;
    if (this.now() - this.at < ttl) return Promise.resolve(this.value);
    if (this.inflight) return this.inflight;
    this.inflight = load()
      .catch(() => [] as readonly T[])
      .then((v) => {
        this.value = Array.isArray(v) ? v : [];
        this.at = this.now();
        return this.value;
      })
      .finally(() => { this.inflight = null; });
    return this.inflight;
  }
}

/** The resolved query for the cited section, or null when the site did not configure it. */
export function citedPagesQuery(ctx: Pick<SiteContext, 'site'>, content: LlmsTxtContent): AiCitedPagesQuery | null {
  const c = content.citedPages;
  if (!c || !c.pathPrefixes?.length) return null;
  return {
    sources: [...(c.sources ?? LLMS_CITED_SOURCES)],
    days: c.days ?? LLMS_CITED_DEFAULTS.days,
    pathPrefixes: [...c.pathPrefixes],
    minReferrals: c.minReferrals ?? LLMS_CITED_DEFAULTS.minReferrals,
    limit: c.limit ?? LLMS_CITED_DEFAULTS.limit,
    articlePathPrefix: c.articlePathPrefix ?? content.latestArticles?.pathPrefix ?? null,
    siteId: ctx.site.id,
  };
}

export function latestArticlesQuery(content: LlmsTxtContent): LatestArticlesQuery | null {
  const c = content.latestArticles;
  if (!c || !c.pathPrefix) return null;
  return { days: c.days ?? LLMS_LATEST_DEFAULTS.days, limit: c.limit ?? LLMS_LATEST_DEFAULTS.limit };
}

/** Loads (and caches) the data sections for one site process. */
export class LlmsDataSource {
  private readonly cited: SectionCache<AiCitedPage>;
  private readonly latest: SectionCache<LatestArticle>;

  constructor(
    private readonly db: Pick<KitDb, 'aiCitedPages' | 'latestArticles'>,
    private readonly ctx: Pick<SiteContext, 'site'>,
    private readonly content: LlmsTxtContent,
    now: () => number = Date.now,
    private readonly budgetMs: number = LLMS_SECTION_BUDGET_MS,
  ) {
    this.cited = new SectionCache<AiCitedPage>(LLMS_CITED_TTL_MS, LLMS_EMPTY_TTL_MS, now);
    this.latest = new SectionCache<LatestArticle>(LLMS_LATEST_TTL_MS, LLMS_EMPTY_TTL_MS, now);
  }

  async load(): Promise<LlmsDynamicData> {
    const cq = citedPagesQuery(this.ctx, this.content);
    const lq = latestArticlesQuery(this.content);
    const db = this.db;
    const empty: readonly never[] = [];
    const [citedPages, latestArticles] = await Promise.all([
      cq && db.aiCitedPages
        ? this.cited.get(() => withBudget(Promise.resolve().then(() => db.aiCitedPages!(cq)), this.budgetMs, empty))
        : Promise.resolve(empty),
      lq && db.latestArticles
        ? this.latest.get(() => withBudget(Promise.resolve().then(() => db.latestArticles!(lq)), this.budgetMs, empty))
        : Promise.resolve(empty),
    ]);
    return { citedPages, latestArticles };
  }
}

/** GET /llms.txt. */
export function llmsTxtHandler(rt: Pick<KitRuntime, 'ctx' | 'db' | 'now' | 'log'>, content: LlmsTxtContent = {}): RequestHandler {
  const source = new LlmsDataSource(rt.db, rt.ctx, content, rt.now);
  return async (_req, res) => {
    let data: LlmsDynamicData = {};
    try {
      data = await source.load();
    } catch (e) {
      rt.log.error(`[affiliate-kit] llms.txt sections failed: ${e instanceof Error ? e.message : String(e)}`);
    }
    try {
      const body = renderLlmsTxt(rt.ctx, content, data);
      res.set('Content-Type', 'text/plain; charset=utf-8');
      res.set('Cache-Control', DISCOVERY_FILE_CACHE_CONTROL);
      res.send(body);
    } catch (e) {
      rt.log.error(`[affiliate-kit] llms.txt render failed: ${e instanceof Error ? e.message : String(e)}`);
      if (!res.headersSent) res.status(500).type('text/plain').send('llms.txt unavailable');
    }
  };
}
