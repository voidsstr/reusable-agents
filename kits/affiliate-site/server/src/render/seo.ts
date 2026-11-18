/**
 * <head> SEO: the title, meta description, robots, canonical, Open Graph and
 * Twitter tags, and the JSON-LD blocks, built from one HeadFields object.
 *
 * Ported from SpecPicks src/services/ssrHead.ts (see
 * unit/render/fixtures/ported-from-render.json): renderHead (here headTags),
 * clampTitle (60 characters measured after HTML escaping, the " | Brand"
 * suffix kept, no ellipsis before it, no dangling function word, question
 * stub or unclosed parenthesis), compactTitleHead (drop a trailing year, keep
 * the longest leading clause, never a bare sub-30-char lead, a matchup keeps
 * its topic and drops its repeated product line), metaDescriptionFrom (as
 * written when it fits, else end at a sentence or closing clause, no
 * ellipsis unless cut mid-sentence), and the robots rule (noindex pages keep
 * "follow"; indexable pages allow large image previews).
 *
 * What changed:
 *   - Brand, origin and the default social card come from SiteContext and the
 *     caller; SpecPicks' per-slug override tables, hardware vocabulary
 *     (capacity tokens, "RTX"/"Ryzen" families) and Amazon-review scrubbing
 *     stay behind (the kit never has review data).
 *   - og:image is never an Amazon-hosted image (*.media-amazon.com,
 *     *ssl-images-amazon.com, any Amazon host): such a candidate falls back to
 *     the site's default card, or no image at all (IP License 2(h); binding
 *     amendment 2). It must also be https. Image metas passed as extraMeta
 *     obey the same rule.
 *   - Indexing is a value (PageIndexing) the page and its sitemap entry share,
 *     so a page can never be noindex while its URL is submitted; extraMeta
 *     cannot add a second robots meta.
 *   - clampTitle charges escape overhead only for the characters it keeps, so
 *     a quote- or '&'-heavy title keeps its brand suffix and is never empty
 *     (SpecPicks charged the whole title's overhead and could cut to nothing).
 */
import { esc } from '../links';
import type { SiteContext } from '../siteContext';
import { jsonLdScripts, type JsonLdNode } from './jsonld';
import {
  collapseWhitespace,
  dropDanglingModifier,
  dropQuestionStub,
  htmlLength,
  stripDanglingTail,
  truncateAtWord,
  type TruncateOptions,
} from './text';
import { canonicalUrl, publicImageUrl, siteUrl } from './urls';

/** The <title> ceiling, measured on the HTML-escaped text (Google cuts at ~580px). */
export const MAX_TITLE_CHARS = 60;
/** The meta description ceiling. */
export const MAX_META_DESCRIPTION_CHARS = 160;

// ─── titles ────────────────────────────────────────────────────────────────

function compactTitleHeadOnce(t: string, budget: number): string | null {
  if (!t) return null;
  if (htmlLength(t) <= budget) return t;
  const noYear = t
    .replace(/\s*(?:\((?:in\s+)?20\d\d\)|[,:]?\s+(?:in|for)\s+20\d\d)(\??)$/i, '$1')
    .trim();
  if (noYear !== t && htmlLength(noYear) <= budget) return noYear;
  let best: string | null = null;
  let bestSepEnd = 0;
  const sep = /(?:\?\s|:\s|\s[—–-]\s)/g;
  let m: RegExpExecArray | null;
  while ((m = sep.exec(noYear)) !== null) {
    const head = noYear.slice(0, m.index + (m[0].startsWith('?') ? 1 : 0)).trim();
    if (head.length >= 20 && htmlLength(head) <= budget) { best = head; bestSepEnd = m.index + m[0].length; }
  }
  // A lead under 30 chars is usually the event or codename, with the topic
  // after the separator: keep it plus the detail (or the detail's leading
  // noun phrase), never the bare lead.
  if (best && best.length < 30 && bestSepEnd > 0) {
    const joiner = `${noYear.slice(best.length, bestSepEnd).replace(/\s+$/, '')} `;
    const rest0 = noYear.slice(bestSepEnd).trim();
    const rests = [rest0];
    const noCount = rest0.replace(/^\d+\s+(?=[A-Za-z])/, '');
    if (noCount !== rest0) rests.push(noCount);
    const leads = [best];
    const leadNoYear = best.replace(/(?:\s+(?:in|for))?\s+20\d\d$/i, '').trim();
    if (leadNoYear !== best && leadNoYear.length >= 12) leads.push(leadNoYear);
    let pick: string | null = null;
    let pickDetailLen = 0;
    for (const lead of leads) {
      for (const rest of rests) {
        const cuts = [rest.length];
        if (!/^(?:what|which|why|how|is|are|does|do|can|should|when|where|who)\b/i.test(rest)) {
          const phraseBreak = /\s(?:for|with|on|in|under|by)\s/gi;
          let pm: RegExpExecArray | null;
          while ((pm = phraseBreak.exec(rest)) !== null) cuts.push(pm.index);
        }
        for (const cut of cuts.sort((a, b) => b - a)) {
          const detail = rest.slice(0, cut).trim();
          if (detail.split(' ').length < 3) continue;
          const joined = `${lead}${joiner}${detail}`;
          if (htmlLength(joined) <= budget) {
            if (detail.length > pickDetailLen) { pick = joined; pickDetailLen = detail.length; }
            break;
          }
        }
      }
    }
    return pick;
  }
  return best;
}

/**
 * "Ryzen 5 5600X vs Ryzen 5 5600G" -> "Ryzen 5 5600X vs 5600G": drop the
 * product-line words the right side repeats from the left, only when the
 * right side keeps a model number. ("Mac Mini vs Mac Studio" is left alone.)
 */
export function collapseVsPrefix(title: string, opts: { anyDigitAfter?: boolean } = {}): string {
  const m = /\s(?:vs\.?|versus)\s/i.exec(title);
  if (!m) return title;
  const left = title.slice(0, m.index).split(' ');
  const right = title.slice(m.index + m[0].length).split(' ');
  const keepsModel = (k: number): boolean => (opts.anyDigitAfter ? /\d/.test(right.slice(k).join(' ')) : /\d/.test(right[k] ?? ''));
  let bestK = 0;
  for (let i = 0; i < left.length; i++) {
    let k = 0;
    while (i + k < left.length - 1 && k < right.length - 1
      && (left[i + k] as string).toLowerCase() === (right[k] as string).toLowerCase()) k++;
    if (k > bestK && keepsModel(k)) bestK = k;
  }
  if (!bestK) return title;
  return `${title.slice(0, m.index + m[0].length)}${right.slice(bestK).join(' ')}`;
}

/**
 * "A vs B: Topic Compared" when the lead is a bare matchup: keep the topic
 * from the detail (whole, then without a trailing year or past participle),
 * trying the lead as written and with its repeated product line dropped.
 * null when nothing fits or the lead has a topic of its own.
 */
export function matchupWithTopic(title: string, budget: number): string | null {
  const colon = title.indexOf(': ');
  if (colon < 0) return null;
  const lead = title.slice(0, colon).trim();
  if (!/\s(?:vs\.?|versus)\s/i.test(lead)) return null;
  if (/\s(?:for|on|in|with|at|under|by)\s/i.test(lead)) return null;
  const rest = title.slice(colon + 2).trim();
  const noYear = rest.replace(/\s*(?:\((?:in\s+)?20\d\d\)|,?\s+(?:in|for)?\s*20\d\d)$/i, '').trim();
  const details = [rest, noYear, noYear.replace(/\s+(?:Compared|Tested|Explained|Ranked|Reviewed|Benchmarked)$/i, '')];
  const leads = [lead, collapseVsPrefix(lead), collapseVsPrefix(lead, { anyDigitAfter: true })]
    .filter((l) => !/\s(?:vs\.?|versus)\s+\d+\s/i.test(l));
  for (const l of leads) {
    for (const d of details) {
      if (d.split(' ').length < 2) continue;
      const joined = `${l}: ${d}`;
      if (htmlLength(joined) <= budget) return joined;
    }
  }
  return null;
}

/**
 * A complete-phrase head for a long title that fits `budget` (HTML-escaped
 * length), or null when the title has none; the caller then word-cuts.
 * Tries: a matchup keeping its topic; the title without a trailing year or as
 * its longest leading clause; the same without a year anywhere; the same with
 * a repeated matchup product line collapsed.
 */
export function compactTitleHead(title: string, budget: number): string | null {
  const t = collapseWhitespace(title);
  if (htmlLength(t) > budget) {
    const matchup = matchupWithTopic(t, budget);
    if (matchup !== null) return matchup;
  }
  const once = compactTitleHeadOnce(t, budget);
  if (once !== null) return once;
  const yearless = t.replace(/\s+(?:in\s+|for\s+)?202\d(?=[\s:?]|$)/i, '').trim();
  const fromYearless = yearless !== t ? compactTitleHeadOnce(yearless, budget) : null;
  if (fromYearless !== null) return fromYearless;
  const collapsed = collapseVsPrefix(t);
  if (collapsed !== t) {
    const fromCollapsed = compactTitleHeadOnce(collapsed, budget);
    if (fromCollapsed !== null) return fromCollapsed;
    const collapsedYearless = collapsed.replace(/\s+(?:in\s+|for\s+)?202\d(?=[\s:?]|$)/i, '').trim();
    const fromCollapsedYearless = collapsedYearless !== collapsed ? compactTitleHeadOnce(collapsedYearless, budget) : null;
    if (fromCollapsedYearless !== null) return fromCollapsedYearless;
  }
  return null;
}

/**
 * The longest word cut of `text` (no ellipsis, no dangling connector,
 * question stub or unclosed parenthesis) whose HTML-escaped length fits
 * `room`. The character budget starts at `room` and steps down one character
 * at a time, so only the characters the cut keeps are charged for escaping
 * ('&' and quotes that fall past the cut cost nothing). At most `room`
 * (<= 60) attempts on a title-sized string. '' when no cut fits.
 */
function fitEscaped(text: string, room: number, tidy: (s: string) => string): string {
  for (let budget = room; budget >= 1; budget--) {
    const cut = dropQuestionStub(stripDanglingTail(tidy(truncateAtWord(text, budget))), text);
    if (htmlLength(cut) <= room) return cut;
  }
  return '';
}

/** The longest prefix of `text` whose HTML-escaped length fits `room`, trimmed (a last resort that is never empty for non-empty text). */
function hardCutEscaped(text: string, room: number): string {
  let out = '';
  for (const ch of text) {
    if (htmlLength(out + ch) > room) break;
    out += ch;
  }
  return out.trim();
}

/**
 * Guarantee a <title> of at most MAX_TITLE_CHARS once HTML-escaped, and never
 * an empty one for non-empty input. A " | Brand" suffix survives whenever the
 * lead can keep at least 20 escaped characters beside it: only the lead is
 * shortened, with no ellipsis before the suffix and no dangling connector,
 * question stub or unclosed parenthesis. Escape overhead is charged only for
 * the characters the cut keeps.
 */
export function clampTitle(raw: string): string {
  if (!raw) return raw;
  const clean = raw.replace(/\s+/g, ' ').trim();
  if (htmlLength(clean) <= MAX_TITLE_CHARS) return clean;
  const sepIdx = clean.lastIndexOf(' | ');
  const suffix = sepIdx > 0 ? clean.slice(sepIdx) : '';
  if (suffix) {
    const head = clean.slice(0, sepIdx);
    const room = MAX_TITLE_CHARS - htmlLength(suffix);
    if (room >= 20) {
      const lead = fitEscaped(head, room, (s) => s.replace(/[\s'’"”…]+$/u, ''));
      if (lead) return lead + suffix;
    }
  }
  const whole = fitEscaped(clean, MAX_TITLE_CHARS, (s) => s.replace(/…$/u, ''));
  if (whole) return whole;
  // Nothing word-cuts into the budget (all punctuation, say): the brand, else a plain cut.
  const brand = suffix.slice(3).trim();
  if (brand && htmlLength(brand) <= MAX_TITLE_CHARS) return brand;
  return hardCutEscaped(clean, MAX_TITLE_CHARS) || clean.slice(0, 1);
}

/**
 * "<head> | <Brand>", clamped: the head becomes a complete phrase that fits
 * (compactTitleHead) or is word-cut without a dangling modifier or question
 * stub. A head that already ends with the brand is not suffixed twice.
 */
export function pageTitle(ctx: Pick<SiteContext, 'site'>, head: string, opts: TruncateOptions = {}): string {
  const brand = ctx.site.brandName;
  const clean = collapseWhitespace(head);
  if (!clean || clean === brand) return clampTitle(brand);
  const suffix = ` | ${brand}`;
  if (clean.endsWith(suffix)) return clampTitle(clean);
  const room = MAX_TITLE_CHARS - htmlLength(suffix);
  if (room < 20) return clampTitle(`${clean}${suffix}`);
  const segment = compactTitleHead(clean, room)
    ?? dropQuestionStub(dropDanglingModifier(truncateAtWord(clean, Math.max(20, room), opts).replace(/…$/u, ''), clean), clean);
  return clampTitle(`${segment}${suffix}`);
}

/**
 * A meta description from authored prose: as written when it fits `max`;
 * otherwise end at the last sentence boundary that keeps at least `minKeep`
 * chars (no ellipsis), else at a trailing ", plus/and/but/..." clause, else a
 * word cut (which keeps its '…', since the text really is cut).
 */
export function metaDescriptionFrom(s: string | null | undefined, max = MAX_META_DESCRIPTION_CHARS, minKeep = 100): string {
  const clean = collapseWhitespace(s);
  if (clean.length <= max) return clean;
  const ABBREV = /(?:^|\s)(?:vs|e\.g|i\.e|etc|approx|incl|no|mr|dr|st|jr|sr|inc|ltd)\.$/i;
  let cut = -1;
  const re = /[.!?](?=\s+["“(\[]?[A-Z0-9])/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(clean)) !== null) {
    const end = m.index + 1;
    if (end > max) break;
    if (ABBREV.test(clean.slice(0, end))) continue;
    cut = end;
  }
  if (cut >= minKeep) return clean.slice(0, cut);
  const clause = /,\s+(?:plus|and|but|with|while|so|which|though)\s/gi;
  let clauseCut = -1;
  while ((m = clause.exec(clean)) !== null) {
    if (m.index + 1 > max) break;
    clauseCut = m.index;
  }
  if (clauseCut >= minKeep) return `${clean.slice(0, clauseCut).replace(/[\s,;:–—-]+$/, '')}.`;
  return truncateAtWord(clean, Math.min(max, 158));
}

// ─── indexing ──────────────────────────────────────────────────────────────

/**
 * Whether a page may be indexed. The page's robots meta and its sitemap /
 * IndexNow membership read the same value, so they cannot disagree.
 */
export interface PageIndexing {
  readonly index: boolean;
  /** Follow the page's links (default true: noindex pages still pass link equity). */
  readonly follow: boolean;
  /** Why the page is noindex (reported by the sitemap builder); null when indexable. */
  readonly reason: string | null;
}

export const INDEXABLE_PAGE: PageIndexing = Object.freeze({ index: true, follow: true, reason: null });

/** A noindex decision. The reason is required: every noindex should be explainable. */
export function noindexPage(reason: string, opts: { follow?: boolean } = {}): PageIndexing {
  const r = collapseWhitespace(reason);
  if (!r) throw new TypeError('noindexPage: a reason is required');
  return Object.freeze({ index: false, follow: opts.follow ?? true, reason: r });
}

/** The robots meta content for a decision. */
export function robotsMetaContent(i: PageIndexing): string {
  const follow = i.follow === false ? 'nofollow' : 'follow';
  return i.index ? `index, ${follow}, max-image-preview:large` : `noindex, ${follow}`;
}

// ─── Open Graph image ──────────────────────────────────────────────────────

export interface OgImage {
  /** Site path or https URL. */
  url: string;
  width?: number;
  height?: number;
  alt?: string;
}

/** True when `url` may be an og:image: https, and not on any Amazon host. */
export function isAllowedOgImage(ctx: Pick<SiteContext, 'site'>, url: string | null | undefined): boolean {
  return publicImageUrl(ctx, url) !== null;
}

/**
 * The og:image to publish: the candidate when allowed, else the fallback
 * when allowed, else null. URLs come back absolute.
 */
export function resolveOgImage(
  ctx: Pick<SiteContext, 'site'>,
  candidate: OgImage | string | null | undefined,
  fallback?: OgImage | null,
): OgImage | null {
  for (const c of [typeof candidate === 'string' ? { url: candidate } : candidate, fallback]) {
    if (!c) continue;
    const url = publicImageUrl(ctx, c.url);
    if (url) return { ...c, url };
  }
  return null;
}

// ─── <head> ────────────────────────────────────────────────────────────────

/**
 * An extra <meta>. Not for what headTags writes itself from HeadFields
 * (description, robots and crawler-specific robots metas, og:type, og:title,
 * og:description, og:url, og:site_name, og:locale, twitter:card,
 * twitter:title, twitter:description): naming one throws a TypeError, since
 * a second copy could contradict the first (a robots meta in particular must
 * follow the page's PageIndexing). An image meta (og:image, og:image:url,
 * og:image:secure_url, twitter:image, twitter:image:src) goes through the
 * og:image gate: made absolute, or dropped when it is Amazon-hosted or not https.
 */
export interface MetaTag {
  name?: string;
  property?: string;
  content: string;
}

/** Metas headTags owns. */
const HEAD_OWNED_META: ReadonlySet<string> = new Set([
  'description', 'og:type', 'og:title', 'og:description', 'og:url', 'og:site_name', 'og:locale',
  'twitter:card', 'twitter:title', 'twitter:description',
]);
/** Robots metas, general and crawler-specific: indexing is PageIndexing's alone. */
const ROBOTS_META_RE = /^(?:robots|googlebot(?:-[a-z]+)?|bingbot|msnbot|slurp|yandex(?:bot)?|baiduspider|duckduckbot|applebot)$/;
/** Image metas, held to the og:image rule. */
const IMAGE_META_RE = /^(?:og:image(?::url|:secure_url)?|twitter:image(?::src)?)$/;

/** The extra meta lines for headTags: owned metas refused, image metas gated. */
function extraMetaLines(ctx: Pick<SiteContext, 'site'>, metas: readonly MetaTag[]): string[] {
  const lines: string[] = [];
  for (const m of metas) {
    const keys = [m.name, m.property].map((k) => String(k ?? '').trim().toLowerCase()).filter(Boolean);
    for (const k of keys) {
      if (HEAD_OWNED_META.has(k) || ROBOTS_META_RE.test(k)) {
        throw new TypeError(`headTags: extraMeta may not set ${JSON.stringify(k)}; it comes from HeadFields (indexing, description, ogType, canonical, ogImage)`);
      }
    }
    let content = m.content;
    if (keys.some((k) => IMAGE_META_RE.test(k))) {
      const url = publicImageUrl(ctx, m.content);
      if (!url) continue;
      content = url;
    }
    if (m.name) lines.push(`<meta name="${esc(m.name)}" content="${esc(content)}">`);
    if (m.property) lines.push(`<meta property="${esc(m.property)}" content="${esc(content)}">`);
  }
  return lines;
}

export interface HeadFields {
  /** The full title; clamped to MAX_TITLE_CHARS here. pageTitle() adds the brand. */
  title: string;
  description?: string | null;
  /** The page's canonical URL (site path or URL on the site); its query is kept as given. */
  canonical: string;
  indexing: PageIndexing;
  ogType?: 'website' | 'article' | 'product';
  /** The page's own social image; refused when Amazon-hosted or not https. */
  ogImage?: OgImage | string | null;
  /** The site's default card, used when ogImage is missing or refused. */
  defaultOgImage?: OgImage | null;
  jsonLd?: ReadonlyArray<JsonLdNode | null | undefined | false>;
  /** Further metas (article:published_time, verification tags); see MetaTag for what they may not set. */
  extraMeta?: readonly MetaTag[];
  linkPrev?: string | null;
  linkNext?: string | null;
  /** <link rel="alternate"> entries (feeds). */
  alternates?: ReadonlyArray<{ type: string; href: string; title?: string }>;
}

/** og:locale from a BCP 47 tag: 'en-US' -> 'en_US'. */
function ogLocale(language: string): string {
  return language.replace('-', '_');
}

/**
 * The SEO part of <head> as HTML, one tag per line: title, description,
 * robots, canonical, Open Graph, Twitter, extra meta, alternates, prev/next
 * and the JSON-LD blocks. Every value is escaped. Throws a TypeError when
 * extraMeta names a meta this function owns (see MetaTag).
 */
export function headTags(ctx: Pick<SiteContext, 'site'>, h: HeadFields): string {
  const title = esc(clampTitle(collapseWhitespace(h.title)));
  const description = collapseWhitespace(h.description);
  const desc = esc(description);
  const canonical = esc(canonicalUrl(ctx, h.canonical, { keepParams: 'all' }));
  const og = resolveOgImage(ctx, h.ogImage, h.defaultOgImage);
  const extra = extraMetaLines(ctx, h.extraMeta ?? []);
  const lines: string[] = [];

  lines.push(`<title>${title}</title>`);
  if (description) lines.push(`<meta name="description" content="${desc}">`);
  lines.push(`<meta name="robots" content="${robotsMetaContent(h.indexing)}">`);
  lines.push(`<link rel="canonical" href="${canonical}">`);

  lines.push(`<meta property="og:type" content="${esc(h.ogType ?? 'website')}">`);
  lines.push(`<meta property="og:title" content="${title}">`);
  if (description) lines.push(`<meta property="og:description" content="${desc}">`);
  lines.push(`<meta property="og:url" content="${canonical}">`);
  lines.push(`<meta property="og:site_name" content="${esc(ctx.site.brandName)}">`);
  lines.push(`<meta property="og:locale" content="${esc(ogLocale(ctx.site.language))}">`);
  if (og) {
    lines.push(`<meta property="og:image" content="${esc(og.url)}">`);
    if (og.width && og.height) {
      lines.push(`<meta property="og:image:width" content="${Math.round(og.width)}">`);
      lines.push(`<meta property="og:image:height" content="${Math.round(og.height)}">`);
    }
    if (og.alt) lines.push(`<meta property="og:image:alt" content="${esc(collapseWhitespace(og.alt))}">`);
  }

  lines.push(`<meta name="twitter:card" content="${og ? 'summary_large_image' : 'summary'}">`);
  lines.push(`<meta name="twitter:title" content="${title}">`);
  if (description) lines.push(`<meta name="twitter:description" content="${desc}">`);
  if (og) {
    lines.push(`<meta name="twitter:image" content="${esc(og.url)}">`);
    if (og.alt) lines.push(`<meta name="twitter:image:alt" content="${esc(collapseWhitespace(og.alt))}">`);
  }

  lines.push(...extra);
  for (const a of h.alternates ?? []) {
    lines.push(`<link rel="alternate" type="${esc(a.type)}"${a.title ? ` title="${esc(a.title)}"` : ''} href="${esc(siteUrl(ctx, a.href))}">`);
  }
  if (h.linkPrev) lines.push(`<link rel="prev" href="${esc(siteUrl(ctx, h.linkPrev))}">`);
  if (h.linkNext) lines.push(`<link rel="next" href="${esc(siteUrl(ctx, h.linkNext))}">`);

  const ld = jsonLdScripts(h.jsonLd ?? []);
  if (ld) lines.push(ld);
  return lines.join('\n');
}
