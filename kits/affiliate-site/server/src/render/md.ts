/**
 * Markdown to HTML for stored editorial bodies (guides, best-list intros,
 * recall notes), with GFM tables rendered as <table class="md-table">.
 *
 * Ported from SpecPicks (see unit/render/fixtures/ported-from-render.json): mdToHtml,
 * underscoreEmphasis and headingId from src/services/ssrRender.ts, and the
 * link patterns and resolveNestedLink from src/services/markdownLink.ts. The
 * block grammar is SpecPicks': headings (an in-body '#' is demoted to <h2>,
 * because the page chrome owns the one <h1>) with stable de-duplicated ids,
 * the authored "## Quick Answer" section wrapper that speakable JSON-LD
 * points at, lists, blockquotes, rules, fenced code, raw HTML blocks passed
 * through, and paragraphs. Inline: code, images, links whose text may hold one
 * level of brackets ("FLUX.2 [klein]"), bold and italic with * and _, and
 * intraword underscores left alone (snake_case, model_numbers).
 *
 * What changed, deliberately:
 *   - Tables follow GFM: optional outer pipes, alignment from the delimiter
 *     row (md-align-left/center/right), escaped pipes (\|) inside cells, and
 *     every row padded or cut to the header's width. They render as
 *     <div class="md-table-wrap"><table class="md-table"> (the wrapper scrolls
 *     on a phone instead of widening the page).
 *   - Amazon links are affiliate links and obey the kit's link rules: an
 *     Amazon link (markdown or a raw-HTML <a>) carries one of the site's own
 *     tags (its own when it already has exactly one of them, else the
 *     placement's), rel="sponsored nofollow noopener", target="_blank" and
 *     data-cta-placement, or, when it cannot be tagged (another marketplace,
 *     amzn.to, a.co, an image CDN), is dropped and its text kept. SpecPicks
 *     passed raw anchors and short links through with whatever tag they
 *     carried.
 *   - Images on Amazon's CDNs are dropped (IP License 2(h): a stored body may
 *     not keep an Amazon image link), in markdown and in raw HTML.
 *   - Code spans, links and images are set aside before emphasis runs, so
 *     "*.media-amazon.com" in a code span or a URL holding '*' or '_' is never
 *     turned into <em>. Code is escaped exactly (no entity decoding).
 *   - Backslash escapes (\*, \_, \$, ...) and <https://...> autolinks work.
 *   - '#fragment' links stay in-page (SpecPicks sent them to the home page);
 *     links to this site are made absolute on the canonical origin and get no
 *     rel/target; mailto: works; any other scheme (javascript:, data:) is
 *     dropped. A site-specific dead-route rule is a `rewriteLink` hook, not
 *     code here (SpecPicks hard-coded its /vs/ routes).
 *   - Links to the kit's click redirect (/r/<ASIN>, REDIRECT_PATH_PREFIX) are
 *     affiliate links too: they get the same rel, target and placement as an
 *     Amazon link, in markdown and in raw HTML.
 *   - Ordered lists keep their start number, a wrapped list item keeps its
 *     continuation lines, a "1." line, a rule or a block-level HTML tag ends a
 *     paragraph (no <div> inside a <p>), and HTML comments outside code are
 *     removed (agent notes never reach the page source). A comment opener
 *     that never closes (a truncated note) is shown as text instead of being
 *     left to swallow the rest of the page.
 *   - A table needs as many delimiter cells as header cells (GFM); otherwise
 *     the lines are a paragraph.
 *   - The body cannot pose as a kit notice: data-amazon-notice and
 *     data-earnings-disclosure attributes are removed from its markup, and
 *     the output is sealed (seal.ts), so no unclosed comment, raw-text
 *     element or formatting tag in raw HTML reaches past the body.
 *   - The code block's inline styles and inline-script copy button are gone;
 *     base.css styles .ssr-code-block.
 *
 * Input is trusted, agent-authored text (raw HTML passes through, as in
 * SpecPicks); this is not a sanitizer for user input.
 */
import {
  AFFILIATE_REL,
  PLACEMENT_ATTR,
  PLACEMENT_RE,
  REDIRECT_PATH_PREFIX,
  assertPlacement,
  esc,
  isAllowedTag,
  retagAmazonUrl,
  tagForPlacement,
} from '../links';
import type { SiteContext } from '../siteContext';
import { isAmazonHostedUrl, isAmazonImageHost, isAmazonLinkHost, isAmazonOwnedHost, urlHostname } from './amazonHosts';
import { sealHtmlFragment } from './seal';
import { htmlToText } from './text';
import { isSiteHost } from './urls';

export type MdContext = Pick<SiteContext, 'site' | 'routes' | 'amazon'>;

/** Placement of Amazon links inside a markdown body unless the caller names another. */
export const DEFAULT_MD_PLACEMENT = 'in-article';

/** Class of the wrapper that keeps a wide table scrollable inside the page. */
export const MD_TABLE_WRAP_CLASS = 'md-table-wrap';
/** Class of every table mdToHtml renders. */
export const MD_TABLE_CLASS = 'md-table';
/** Class of the <span> that replaces an Amazon link that could not be tagged. */
export const MD_UNLINKED_CLASS = 'md-unlinked';

// ─── link patterns (ported from markdownLink.ts) ───────────────────────────

/**
 * Link text: any character but a bracket, or ONE level of balanced brackets
 * ("FLUX.2 [klein] 4B model card"). A bracket pair not followed by "(url)"
 * never matches, so footnotes "[1]" and checkboxes "[x]" stay text.
 */
export const MD_LINK_TEXT = String.raw`(?:[^\[\]]|\[[^\[\]]*\])`;

/** `[text](url)`: group 1 is the text (non-empty), group 2 the url. */
export function mdLinkRe(url: string = String.raw`[^)]+`): RegExp {
  return new RegExp(String.raw`\[(${MD_LINK_TEXT}+)\]\((${url})\)`, 'g');
}

/** `![alt](url)`: group 1 is the alt text (may be empty), group 2 the url. */
export function mdImageRe(): RegExp {
  return new RegExp(String.raw`!\[(${MD_LINK_TEXT}*)\]\(([^)]+)\)`, 'g');
}

/** An image OR a link, `!?[text](url)`, text possibly empty; group 1 is the text. */
export function mdLinkOrImageRe(): RegExp {
  return new RegExp(String.raw`!?\[(${MD_LINK_TEXT}*)\]\([^)]*\)`, 'g');
}

/** Link text with any link nested inside it reduced to that link's own text. */
export function unwrapNestedLinks(text: string): string {
  return String(text).replace(/\[([^\[\]]*)\]\([^)]*\)/g, '$1');
}

function escRe(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

const productHrefCache = new Map<string, RegExp>();

/**
 * A link to this site's product page, relative or absolute, built from
 * ctx.routes.product (SpecPicks hard-coded "/product/<id>" on its own domain).
 */
export function productHrefRe(ctx: Pick<SiteContext, 'site' | 'routes'>): RegExp {
  const key = `${ctx.site.domain}|${ctx.routes.product}`;
  const hit = productHrefCache.get(key);
  if (hit) return hit;
  const [pre, post] = ctx.routes.product.split('{asin}') as [string, string?];
  const re = new RegExp(
    `^(?:https?://(?:www\\.)?${escRe(ctx.site.domain)})?${escRe(pre)}[A-Za-z0-9_-]+${escRe(post ?? '')}(?:[/?#]\\S*)?$`,
    'i',
  );
  if (productHrefCache.size >= 32) productHrefCache.clear();
  productHrefCache.set(key, re);
  return re;
}

/**
 * The text and url of a markdown link whose text holds another link. Links
 * cannot nest in HTML, so the text keeps the inner link's words; the url is
 * the inner product-page link when the outer one goes to Amazon (an Amazon
 * url typed into prose is never checked, while our product page is resolved
 * against the catalog and carries the tagged buy button). Any other outer url
 * (a citation) stays.
 */
export function resolveNestedLink(
  ctx: Pick<SiteContext, 'site' | 'routes'>,
  text: string,
  url: string,
): { text: string; url: string } {
  const t = String(text);
  if (isAmazonLinkHost(urlHostname(String(url).trim()))) {
    for (const m of t.matchAll(/\[[^\[\]]*\]\(([^)]*)\)/g)) {
      const inner = String(m[1]).trim();
      if (productHrefRe(ctx).test(inner)) return { text: unwrapNestedLinks(t), url: inner };
    }
  }
  return { text: unwrapNestedLinks(t), url };
}

// ─── emphasis and heading ids (ported from ssrRender.ts) ───────────────────

/**
 * `__x__` / `_x_` emphasis. Both forms need a non-word character (or the
 * string start) on the left and refuse a word character on the right, so
 * snake_case identifiers and model_numbers survive (CommonMark's intraword
 * rule). Apply to text nodes only, never to a string still holding tags.
 */
export function underscoreEmphasis(s: string): string {
  return s
    .replace(/(^|[^A-Za-z0-9_])__([^_\n]+)__(?![A-Za-z0-9_])/g, '$1<strong>$2</strong>')
    .replace(/(^|[^A-Za-z0-9_])_([^_\n]+)_(?![A-Za-z0-9_])/g, '$1<em>$2</em>');
}

/**
 * A stable, URL-safe fragment id for one heading: inline markdown stripped,
 * alphanumerics and spaces kept, hyphenated, at most 80 chars. `seen` counts
 * occurrences per document, so a second "Pros" becomes "pros-1".
 */
export function headingId(raw: string, seen: Map<string, number>): string {
  const base = String(raw || '')
    .replace(/`([^`]+)`/g, '$1')
    .replace(mdLinkOrImageRe(), '$1')
    .replace(/[*_]+/g, '')
    .replace(/<[^>]+>/g, '')
    .toLowerCase()
    .replace(/[^a-z0-9\s-]/g, '')
    .trim()
    .replace(/\s+/g, '-')
    .replace(/-+/g, '-')
    .slice(0, 80)
    .replace(/^-|-$/g, '');
  if (!base) return '';
  const n = seen.get(base) || 0;
  seen.set(base, n + 1);
  return n ? `${base}-${n}` : base;
}

// ─── escaping and attributes ───────────────────────────────────────────────

/** Exact HTML escaping (no entity decoding): for code, which must show what was written. */
function escapeExact(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function decodeEntities(s: string): string {
  return s.replace(/&(#x[0-9a-f]{1,6}|#\d{1,7}|amp|lt|gt|quot|apos);/gi, (m, e: string) => {
    const k = e.toLowerCase();
    if (k === 'amp') return '&';
    if (k === 'lt') return '<';
    if (k === 'gt') return '>';
    if (k === 'quot') return '"';
    if (k === 'apos') return "'";
    const code = k.startsWith('#x') ? parseInt(k.slice(2), 16) : parseInt(k.slice(1), 10);
    return Number.isFinite(code) && code > 0 && code <= 0x10ffff ? String.fromCodePoint(code) : m;
  });
}

type Attr = readonly [name: string, value: string | null];

const ATTR_RE = /([^\s"'<>/=]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?/g;

function parseAttributes(s: string): Attr[] {
  const out: Attr[] = [];
  for (const m of s.matchAll(ATTR_RE)) out.push([String(m[1]), m[2] ?? m[3] ?? m[4] ?? null]);
  return out;
}

function attrValue(attrs: readonly Attr[], name: string): string | null {
  const n = name.toLowerCase();
  for (const [k, v] of attrs) if (k.toLowerCase() === n) return v ?? '';
  return null;
}

const CONTROLLED_ATTRS = new Set(['href', 'rel', 'target', PLACEMENT_ATTR]);

/**
 * The opening tag of an affiliate anchor: `kept` attributes (class, title,
 * data-*, ...) first, then the controlled ones. Inline event handlers on an
 * affiliate anchor are dropped.
 */
function affiliateOpenTag(href: string, placement: string, kept: readonly Attr[] = []): string {
  const extra = kept
    .filter(([n]) => !CONTROLLED_ATTRS.has(n.toLowerCase()) && !/^on/i.test(n))
    .map(([n, v]) => (v == null ? ` ${n}` : ` ${n}="${esc(v)}"`))
    .join('');
  return `<a${extra} href="${esc(href)}" rel="${AFFILIATE_REL}" target="_blank" ${PLACEMENT_ATTR}="${placement}">`;
}

/**
 * The tag an Amazon link gets: its own when it carries exactly one tag and
 * that tag is one of the site's (an upstream choice such as a segment tag is
 * kept), else the placement's (links.ts tagForPlacement).
 */
function affiliateTagFor(ctx: MdContext, url: string, placement: string, segmentTag: string | null | undefined): string {
  try {
    const own = Array.from(new URL(url).searchParams.entries()).filter(([k]) => k.toLowerCase() === 'tag').map(([, v]) => v);
    if (own.length === 1 && isAllowedTag(ctx, own[0])) return own[0] as string;
  } catch {
    // not a URL: the placement decides
  }
  return tagForPlacement(ctx, placement, segmentTag);
}

type LinkTarget =
  | { readonly kind: 'drop' }
  | { readonly kind: 'fragment' | 'mailto' | 'internal' | 'redirect' | 'external' | 'amazon'; readonly href: string };

/**
 * The path of the kit's click redirect for one ASIN: the same rule as the
 * client beacon (client/src/clickBeacon.ts) and the /r/:asin route (10
 * uppercase alphanumerics, optional trailing slash).
 */
const REDIRECT_PATH_RE = new RegExp(`^${escRe(REDIRECT_PATH_PREFIX)}[A-Z0-9]{10}/?$`);

/** Classify and absolutise one link target. */
function resolveLinkTarget(ctx: MdContext, raw: string): LinkTarget {
  const u = String(raw ?? '').trim().replace(/^<(.*)>$/, '$1').trim();
  if (!u) return { kind: 'drop' };
  if (u.startsWith('#')) return /^#[^\s"'<>]*$/.test(u) ? { kind: 'fragment', href: u } : { kind: 'drop' };
  if (/^mailto:[^\s"'<>]+$/i.test(u)) return { kind: 'mailto', href: u };
  if (/^[a-z][a-z0-9+.-]*:/i.test(u) && !/^https?:/i.test(u)) return { kind: 'drop' };
  const abs = /^https?:\/\//i.test(u)
    ? u
    : u.startsWith('//')
      ? `https:${u}`
      : `${ctx.site.canonicalOrigin}${u.startsWith('/') ? u : `/${u}`}`;
  let parsed: URL;
  try {
    parsed = new URL(abs);
  } catch {
    return { kind: 'drop' };
  }
  const host = parsed.hostname.toLowerCase();
  if (isSiteHost(ctx, host)) {
    const href = `${ctx.site.canonicalOrigin}${parsed.pathname}${parsed.search}${parsed.hash}`;
    // /r/<ASIN> sends the reader to tagged Amazon: an affiliate link, not a plain internal one.
    return { kind: REDIRECT_PATH_RE.test(parsed.pathname) ? 'redirect' : 'internal', href };
  }
  if (isAmazonLinkHost(host)) return { kind: 'amazon', href: parsed.toString() };
  if (isAmazonImageHost(host)) return { kind: 'drop' };
  return { kind: 'external', href: abs };
}

/** An image src made absolute; '' for an Amazon-hosted, non-http(s) or unparseable source. */
function resolveImageSrc(ctx: MdContext, raw: string): string {
  const u = String(raw ?? '').trim().replace(/^<(.*)>$/, '$1').trim();
  if (!u) return '';
  if (/^[a-z][a-z0-9+.-]*:/i.test(u) && !/^https?:/i.test(u)) return '';
  const abs = /^https?:\/\//i.test(u)
    ? u
    : u.startsWith('//')
      ? `https:${u}`
      : `${ctx.site.canonicalOrigin}${u.startsWith('/') ? u : `/${u}`}`;
  const host = urlHostname(abs);
  if (!host || isAmazonOwnedHost(host)) return '';
  return abs;
}

/** `url "title"` / `url 'title'`: markdown's optional link title is not part of the url. */
function stripLinkTitle(u: string): string {
  return u.replace(/\s+(?:"[^"]*"|'[^']*')\s*$/, '');
}

// ─── inline ────────────────────────────────────────────────────────────────

const STASH = '\u0000';
const STASH_RE = /\u0000(\d+)\u0000/g;

/** Bold, italic (asterisks, on the whole string as SpecPicks did), then underscore emphasis on text nodes. */
function emphasis(s: string): string {
  return s
    .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>')
    .replace(/(<[^>]*>)|([^<]+)/g, (_m, tag: string | undefined, text: string | undefined) =>
      tag ?? underscoreEmphasis(text ?? ''));
}

interface InlineEnv {
  ctx: MdContext;
  placement: string;
  segmentTag: string | null | undefined;
  rewriteLink?: MdOptions['rewriteLink'];
}

function renderInline(env: InlineEnv, src: string): string {
  const stash: string[] = [];
  const put = (html: string): string => `${STASH}${stash.push(html) - 1}${STASH}`;
  const restore = (s: string): string => {
    let cur = s;
    for (let guard = 0; guard < 8 && cur.includes(STASH); guard++) {
      cur = cur.replace(STASH_RE, (_m, n: string) => stash[Number(n)] ?? '');
    }
    return cur;
  };
  /** Link text: raw text escaped, markdown emphasis applied, set-aside spans restored. */
  const label = (t: string): string => restore(emphasis(esc(t)));

  /** One link; `literal` keeps the text as written (an autolink shows its url, not markdown). */
  const linkHtml = (rawText: string, rawUrl: string, literal = false): string => {
    const { text, url } = resolveNestedLink(env.ctx, rawText, stripLinkTitle(rawUrl));
    const inner = literal ? esc(text) : label(text);
    // A link whose content rendered to nothing (an Amazon image that was dropped) is no link.
    if (!inner.trim()) return '';
    let targetUrl = url;
    if (env.rewriteLink) {
      const r = env.rewriteLink(url, htmlToText(restore(text)));
      if (r === null) return inner;
      if (typeof r === 'string') targetUrl = r;
    }
    const t = resolveLinkTarget(env.ctx, targetUrl);
    switch (t.kind) {
      case 'drop':
        return inner;
      case 'amazon': {
        const tagged = retagAmazonUrl(env.ctx, t.href, affiliateTagFor(env.ctx, t.href, env.placement, env.segmentTag));
        return tagged ? `${affiliateOpenTag(tagged, env.placement)}${inner}</a>` : inner;
      }
      case 'redirect':
        return `${affiliateOpenTag(t.href, env.placement)}${inner}</a>`;
      case 'external':
        return `<a href="${esc(t.href)}" rel="noopener noreferrer" target="_blank">${inner}</a>`;
      default:
        return `<a href="${esc(t.href)}">${inner}</a>`;
    }
  };

  const imageHtml = (alt: string, rawUrl: string): string => {
    const src = resolveImageSrc(env.ctx, stripLinkTitle(rawUrl));
    if (!src) return '';
    return `<img src="${esc(src)}" alt="${esc(htmlToText(restore(alt)))}" loading="lazy" />`;
  };

  let s = src
    // code spans first: nothing inside them is markdown
    .replace(/`([^`]+)`/g, (_m, c: string) => put(`<code>${escapeExact(c)}</code>`))
    // backslash escapes of ASCII punctuation
    .replace(/\\([!-/:-@[-`{-~])/g, (_m, ch: string) => put(escapeExact(ch)))
    // <https://...> autolinks
    .replace(/<(https?:\/\/[^\s<>"]+)>/gi, (_m, u: string) => put(linkHtml(u, u, true)))
    // images before links, or "!" is left dangling and the image becomes a link
    .replace(mdImageRe(), (_m, alt: string, u: string) => put(imageHtml(alt, u)))
    .replace(mdLinkRe(), (_m, t: string, u: string) => put(linkHtml(t, u)));
  s = emphasis(s);
  return restore(s);
}

// ─── blocks ────────────────────────────────────────────────────────────────

const FENCE_RE = /^```/;
const HEADING_RE = /^(#{1,6})\s+(.*)$/;
const BULLET_RE = /^(\s*)[-*+]\s+(.*)$/;
const ORDERED_RE = /^(\s*)(\d{1,9})\.\s+(.*)$/;
const QUOTE_RE = /^>\s?/;
const RULE_RE = /^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/;
const RAW_HTML_RE = /^\s*<(?!\/?(?:em|strong|code)\b)(?!https?:)[a-zA-Z]/;
const BLANK_RE = /^\s*$/;
/** An HTML block that may interrupt a paragraph (CommonMark's block-level tags). */
const BLOCK_HTML_RE = /^\s*<\/?(?:address|article|aside|blockquote|details|dialog|div|dl|fieldset|figcaption|figure|footer|form|h[1-6]|header|hr|li|main|nav|ol|p|picture|pre|section|summary|table|ul)\b/i;
const DELIMITER_ROW_RE = /^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*$/;

function hasUnescapedPipe(line: string): boolean {
  return /(^|[^\\])\|/.test(line);
}

/**
 * A GFM table starts here: a header row with a pipe, then a delimiter row
 * with a pipe and exactly as many cells (GFM: otherwise it is no table).
 */
function isTableStart(lines: readonly string[], i: number): boolean {
  const head = lines[i];
  const delim = lines[i + 1];
  if (head === undefined || delim === undefined) return false;
  if (BLANK_RE.test(head) || !hasUnescapedPipe(head)) return false;
  if (FENCE_RE.test(head) || HEADING_RE.test(head) || QUOTE_RE.test(head)) return false;
  if (!DELIMITER_ROW_RE.test(delim) || !delim.includes('|')) return false;
  return splitTableRow(head).length === splitTableRow(delim).length;
}

/** A body row: non-blank, holds a pipe, and starts no other block. */
function isTableBodyRow(line: string | undefined): boolean {
  return line !== undefined && !BLANK_RE.test(line) && hasUnescapedPipe(line)
    && !FENCE_RE.test(line) && !HEADING_RE.test(line) && !QUOTE_RE.test(line);
}

/** Cells of one table row: outer pipes optional, `\|` is a literal pipe. */
function splitTableRow(row: string): string[] {
  let s = row.trim();
  if (s.startsWith('|')) s = s.slice(1);
  if (s.endsWith('|') && !s.endsWith('\\|')) s = s.slice(0, -1);
  const cells: string[] = [];
  let cur = '';
  for (let k = 0; k < s.length; k++) {
    const ch = s[k];
    if (ch === '\\' && s[k + 1] === '|') { cur += '|'; k++; continue; }
    if (ch === '|') { cells.push(cur.trim()); cur = ''; continue; }
    cur += ch;
  }
  cells.push(cur.trim());
  return cells;
}

type Align = '' | 'left' | 'center' | 'right';

function alignOf(cell: string): Align {
  const c = cell.trim();
  const left = c.startsWith(':');
  const right = c.endsWith(':');
  if (left && right) return 'center';
  if (right) return 'right';
  if (left) return 'left';
  return '';
}

function tableHtml(header: string[], aligns: Align[], rows: string[][], inline: (s: string) => string): string {
  const width = header.length;
  const cls = (k: number): string => (aligns[k] ? ` class="md-align-${aligns[k]}"` : '');
  const thead = `<thead><tr>${header.map((h, k) => `<th scope="col"${cls(k)}>${inline(h)}</th>`).join('')}</tr></thead>`;
  const body = rows.map((r) => {
    const cells: string[] = [];
    for (let k = 0; k < width; k++) cells.push(`<td${cls(k)}>${inline(r[k] ?? '')}</td>`);
    return `<tr>${cells.join('')}</tr>`;
  }).join('');
  return `<div class="${MD_TABLE_WRAP_CLASS}"><table class="${MD_TABLE_CLASS}">${thead}<tbody>${body}</tbody></table></div>`;
}

function codeBlockHtml(code: string, info: string): string {
  const first = info.trim().split(/\s+/)[0] ?? '';
  const lang = /^[A-Za-z0-9_+#.-]{1,24}$/.test(first) ? first : '';
  const badge = lang ? `<span class="ssr-code-lang">${escapeExact(lang)}</span>` : '';
  const langAttr = lang ? ` data-lang="${escapeExact(lang)}"` : '';
  return `<div class="ssr-code-block">${badge}<pre class="ssr-code"><code${langAttr}>${escapeExact(code)}</code></pre></div>`;
}

/** A line that ends a paragraph. */
function isParagraphBreak(lines: readonly string[], j: number): boolean {
  const l = lines[j] as string;
  return BLANK_RE.test(l) || HEADING_RE.test(l) || /^\s*[-*+]\s+/.test(l) || /^\s*\|/.test(l)
    || FENCE_RE.test(l) || QUOTE_RE.test(l) || /^\s*1\.\s+/.test(l) || RULE_RE.test(l) || BLOCK_HTML_RE.test(l)
    || isTableStart(lines, j);
}

/** A line that continues the list item above it: text that starts no block of its own. */
function isListContinuation(lines: readonly string[], j: number): boolean {
  const l = lines[j] as string;
  return !BLANK_RE.test(l) && !BULLET_RE.test(l) && !ORDERED_RE.test(l) && !RAW_HTML_RE.test(l) && !isParagraphBreak(lines, j);
}

/**
 * Outside fenced code and code spans: an HTML comment, or a code span (kept as
 * written), or a comment opener that never closes. A comment ends at its first
 * "-->" or "--!>"; the abrupt empty forms ("<!-->", "<!--->") end at once.
 */
const COMMENT_OR_CODE_RE = /(`+)[^\n]*?\1|<!--(?:-?>|[\s\S]*?--!?>)|<!--/g;

/**
 * Remove <!-- comments --> outside fenced code and code spans (agent notes
 * never ship). An opener with no end (a truncated note) would hide the rest
 * of the page, footer notices included, so it is escaped and shown as text.
 */
function stripHtmlComments(lines: string[]): string[] {
  const out: string[] = [];
  let chunk: string[] = [];
  const flush = (): void => {
    if (!chunk.length) return;
    const text = chunk.join('\n').replace(COMMENT_OR_CODE_RE, (m: string, ticks: string | undefined) => {
      if (ticks) return m;
      // Only the bare opener is 4 characters long: every complete comment is longer.
      return m.length === 4 ? `&lt;${m.slice(1)}` : '';
    });
    out.push(...text.split('\n'));
    chunk = [];
  };
  let inFence = false;
  for (const line of lines) {
    if (FENCE_RE.test(line)) {
      if (!inFence) flush();
      inFence = !inFence;
      out.push(line);
      continue;
    }
    if (inFence) out.push(line);
    else chunk.push(line);
  }
  flush();
  return out;
}

export interface MdOptions {
  /**
   * data-cta-placement for Amazon links in the body: authored markdown links,
   * and raw-HTML anchors that do not name a valid placement of their own. Its
   * segment tag (amazon.placementTags) applies. Default 'in-article'.
   */
  amazonPlacement?: string;
  /** The ?tag= the visit arrived under; honoured only when it is one of the site's own tags. */
  segmentTag?: string | null;
  /**
   * Site hook for dead or moved routes. Called with each link's url (after
   * nested-link resolution) and its plain text; return a replacement url,
   * null to drop the link and keep its text, or undefined to keep it.
   */
  rewriteLink?: (url: string, text: string) => string | null | undefined;
}

/**
 * Render a stored markdown body. '' for empty input. Output is a sequence of
 * block elements joined by newlines; Amazon links and images follow the
 * kit's rules (see the module comment) via enforceAffiliateLinks().
 */
export function mdToHtml(ctx: MdContext, md: string | null | undefined, opts: MdOptions = {}): string {
  if (!md) return '';
  const placement = assertPlacement(opts.amazonPlacement ?? DEFAULT_MD_PLACEMENT);
  const env: InlineEnv = { ctx, placement, segmentTag: opts.segmentTag, rewriteLink: opts.rewriteLink };
  const inline = (s: string): string => renderInline(env, s);
  const lines = stripHtmlComments(String(md).replace(/\u0000/g, '').replace(/\r\n?/g, '\n').split('\n'));
  const out: string[] = [];
  const headingIds = new Map<string, number>();
  const listStack: string[] = [];
  const closeList = (): void => {
    while (listStack.length) out.push(`</${listStack.pop()}>`);
  };
  /** Inside the authored "## Quick Answer" wrapper. */
  let qaOpen = false;
  let i = 0;
  /**
   * A list item's text plus its continuation lines (a wrapped item, indented
   * or not), which SpecPicks split off into a stray paragraph. Advances `i`.
   */
  const listItemText = (first: string): string => {
    const parts = [first];
    while (i < lines.length && isListContinuation(lines, i)) { parts.push((lines[i] as string).trim()); i++; }
    return parts.join(' ');
  };

  while (i < lines.length) {
    const line = lines[i] as string;

    // Fenced code block.
    if (FENCE_RE.test(line)) {
      closeList();
      const info = line.replace(FENCE_RE, '');
      const buf: string[] = [];
      i++;
      while (i < lines.length && !FENCE_RE.test(lines[i] as string)) { buf.push(lines[i] as string); i++; }
      i++; // closing fence
      out.push(codeBlockHtml(buf.join('\n'), info));
      continue;
    }

    // Heading: '#' demoted to h2 (the page owns the h1), stable ids, Quick Answer wrapper.
    const hm = line.match(HEADING_RE);
    if (hm) {
      closeList();
      const level = (hm[1] as string).length === 1 ? 2 : (hm[1] as string).length;
      const text = (hm[2] as string).trim().replace(/\s+#+\s*$/, '');
      i++;
      if (!text) continue;
      const id = headingId(text, headingIds);
      // The wrapper closes at the next h1/h2, so it is exactly as wide as the author's section.
      if (qaOpen && level <= 2) { out.push('</section>'); qaOpen = false; }
      if (!qaOpen && id === 'quick-answer' && level <= 3) {
        // The wrapper carries the id; repeating it on the heading would make the fragment ambiguous.
        out.push('<section class="ssr-quick-answer-authored" id="quick-answer">');
        qaOpen = true;
        out.push(`<h${level} class="ssr-h${level}">${inline(text)}</h${level}>`);
        continue;
      }
      out.push(`<h${level} class="ssr-h${level}"${id ? ` id="${id}"` : ''}>${inline(text)}</h${level}>`);
      continue;
    }

    // GFM table.
    if (isTableStart(lines, i)) {
      closeList();
      const header = splitTableRow(line);
      const aligns = splitTableRow(lines[i + 1] as string).map(alignOf);
      i += 2;
      const rows: string[][] = [];
      while (isTableBodyRow(lines[i])) { rows.push(splitTableRow(lines[i] as string)); i++; }
      out.push(tableHtml(header, aligns, rows, inline));
      continue;
    }

    // Unordered list.
    const um = line.match(BULLET_RE);
    if (um) {
      if (listStack[listStack.length - 1] !== 'ul') { closeList(); out.push('<ul class="ssr-list">'); listStack.push('ul'); }
      i++;
      out.push(`<li>${inline(listItemText((um[2] as string).trim()))}</li>`);
      continue;
    }

    // Ordered list (keeps its start number).
    const om = line.match(ORDERED_RE);
    if (om) {
      if (listStack[listStack.length - 1] !== 'ol') {
        closeList();
        const start = Number(om[2]);
        out.push(`<ol class="ssr-list"${start !== 1 ? ` start="${start}"` : ''}>`);
        listStack.push('ol');
      }
      i++;
      out.push(`<li>${inline(listItemText((om[3] as string).trim()))}</li>`);
      continue;
    }

    // Blockquote.
    if (QUOTE_RE.test(line)) {
      closeList();
      const buf: string[] = [];
      while (i < lines.length && QUOTE_RE.test(lines[i] as string)) { buf.push((lines[i] as string).replace(QUOTE_RE, '')); i++; }
      out.push(`<blockquote class="ssr-quote">${inline(buf.join(' ').trim())}</blockquote>`);
      continue;
    }

    // Horizontal rule.
    if (RULE_RE.test(line)) { closeList(); out.push('<hr />'); i++; continue; }

    // Raw HTML block: passed through until a blank line, heading or table row.
    if (RAW_HTML_RE.test(line)) {
      closeList();
      const buf: string[] = [line];
      i++;
      while (i < lines.length && !BLANK_RE.test(lines[i] as string) && !/^#{1,6}\s+/.test(lines[i] as string) && !/^\s*\|/.test(lines[i] as string)) {
        buf.push(lines[i] as string);
        i++;
      }
      out.push(buf.join('\n'));
      continue;
    }

    // Blank line: ends a list.
    if (BLANK_RE.test(line)) { closeList(); i++; continue; }

    // Paragraph: consecutive lines until something else starts.
    closeList();
    const buf: string[] = [line];
    i++;
    while (i < lines.length && !isParagraphBreak(lines, i)) { buf.push(lines[i] as string); i++; }
    // A paragraph that rendered to nothing (only a dropped Amazon image) is no paragraph.
    const para = inline(buf.join(' ').trim()).trim();
    if (para) out.push(`<p>${para}</p>`);
  }
  closeList();
  // A Quick Answer that runs to the end of the body never meets a closing heading.
  if (qaOpen) out.push('</section>');
  const linked = enforceAffiliateLinks(ctx, out.join('\n'), { placement, segmentTag: opts.segmentTag });
  return sealHtmlFragment(stripKitMarkers(linked));
}

/**
 * Attributes that mark the kit's own compliance blocks (notices.ts). The page
 * shell and the compliance gate look for them, so a body must never carry
 * one: a raw-HTML "<p data-amazon-notice=2k>" with other words would claim to
 * be the 2(k) notice.
 */
const KIT_MARKER_ATTRS: ReadonlySet<string> = new Set(['data-amazon-notice', 'data-earnings-disclosure']);
const START_TAG_RE = /<([A-Za-z][A-Za-z0-9-]*)((?:[^>"']|"[^"]*"|'[^']*')*)>/g;

/** `html` with the kit's marker attributes removed from every start tag; other attributes stay as written. */
function stripKitMarkers(html: string): string {
  if (!/data-(?:amazon-notice|earnings-disclosure)/i.test(html)) return html;
  return html.replace(START_TAG_RE, (whole: string, name: string, raw: string) => {
    let kept = '';
    let last = 0;
    let changed = false;
    for (const m of raw.matchAll(ATTR_RE)) {
      if (!KIT_MARKER_ATTRS.has(String(m[1]).toLowerCase())) continue;
      // Drop the attribute with the whitespace in front of it.
      let start = m.index as number;
      while (start > last && /\s/.test(raw.charAt(start - 1))) start--;
      kept += raw.slice(last, start);
      last = (m.index as number) + m[0].length;
      changed = true;
    }
    return changed ? `<${name}${kept}${raw.slice(last)}>` : whole;
  });
}

// ─── affiliate-link enforcement over finished HTML ────────────────────────

export interface AffiliateLinkOptions {
  /** Placement for Amazon anchors that do not carry a valid data-cta-placement. Default 'in-article'. */
  placement?: string;
  segmentTag?: string | null;
}

const TAG_TOKEN_RE = /<(a|img|source)\b((?:[^>"']|"[^"]*"|'[^']*')*)>|<\/a\s*>/gi;

function srcsetHasAmazon(srcset: string | null): boolean {
  if (!srcset) return false;
  return decodeEntities(srcset).split(',').some((c) => isAmazonHostedUrl(c.trim().split(/\s+/)[0] ?? ''));
}

/**
 * Enforce the kit's Amazon rules on finished HTML (a rendered body, or any
 * fragment that holds hand-written anchors):
 *   - every <a> to an Amazon storefront host is rebuilt with one of the
 *     site's own tags (the one it carries when that is exactly one of them,
 *     else the tag for its placement: the anchor's own data-cta-placement
 *     when valid, else `placement`), rel="sponsored nofollow noopener",
 *     target="_blank" and data-cta-placement; other attributes (class,
 *     title, data-*) stay;
 *   - an <a> that cannot be tagged (another marketplace, a short link, an
 *     Amazon image) becomes <span class="md-unlinked"> with the same content;
 *   - an <a> to the site's own /r/<ASIN> click redirect is an affiliate link
 *     as well: rebuilt, absolute on the canonical origin, with the same
 *     rel, target and placement;
 *   - an <img> or <source> on an Amazon host is removed.
 * Text inside code (escaped) is never touched. Idempotent.
 */
export function enforceAffiliateLinks(ctx: MdContext, html: string, opts: AffiliateLinkOptions = {}): string {
  const placement = assertPlacement(opts.placement ?? DEFAULT_MD_PLACEMENT);
  let unlinkedOpen = false;
  return String(html ?? '').replace(TAG_TOKEN_RE, (whole: string, tag: string | undefined, rawAttrs: string | undefined) => {
    if (!tag) {
      if (!unlinkedOpen) return whole;
      unlinkedOpen = false;
      return '</span>';
    }
    const attrs = parseAttributes(rawAttrs ?? '');
    if (tag.toLowerCase() !== 'a') {
      const src = attrValue(attrs, 'src');
      return (src != null && isAmazonHostedUrl(decodeEntities(src))) || srcsetHasAmazon(attrValue(attrs, 'srcset')) ? '' : whole;
    }
    unlinkedOpen = false;
    const href = attrValue(attrs, 'href');
    if (href == null) return whole;
    const decoded = decodeEntities(href).trim();
    const host = urlHostname(decoded, ctx.site.canonicalOrigin);
    const own = attrValue(attrs, PLACEMENT_ATTR);
    const pl = own && PLACEMENT_RE.test(own) ? own : placement;
    if (host && isSiteHost(ctx, host)) {
      const u = new URL(decoded, ctx.site.canonicalOrigin);
      if (!REDIRECT_PATH_RE.test(u.pathname)) return whole;
      return affiliateOpenTag(`${ctx.site.canonicalOrigin}${u.pathname}${u.search}${u.hash}`, pl, attrs);
    }
    if (!host || !isAmazonOwnedHost(host)) return whole;
    if (isAmazonLinkHost(host)) {
      const abs = new URL(decoded, ctx.site.canonicalOrigin).toString();
      const tagged = retagAmazonUrl(ctx, abs, affiliateTagFor(ctx, abs, pl, opts.segmentTag));
      if (tagged) return affiliateOpenTag(tagged, pl, attrs);
    }
    unlinkedOpen = true;
    return `<span class="${MD_UNLINKED_CLASS}">`;
  });
}
