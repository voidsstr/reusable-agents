/**
 * The page shell: one complete HTML document around a renderer's main
 * content, carrying what every page of an affiliate site owes its readers and
 * Amazon:
 *   - the IP License 2(k) notice in plain view, in the footer, whenever the
 *     page shows Amazon content. That is the default (`amazonContent` must be
 *     set to false to drop it), and it is forced back on when anything the
 *     page renders (main, bodyEnd, headExtra) holds a kit price or
 *     availability stamp or a data-amazon-content marker, so it can never go
 *     missing by omission (binding amendment 2). Nothing in the content can
 *     switch it off: a copy the content shows itself does not replace it;
 *   - the earnings disclosure (Operating Agreement section 5) in the footer of
 *     every page, and above the content (after its <h1>, before the first
 *     affiliate link) whenever the page holds affiliate links;
 *   - legibility-safe theme tokens: themeCss(ctx.theme) (palettes checked for
 *     WCAG AA when site.json loaded), base.css (which only uses validated
 *     token pairs) and the price stamp CSS, inlined as <style id="ssr-styles">;
 *   - one <main id="main">, a skip link, the header and footer navigation,
 *     the breadcrumb (with its BreadcrumbList twin) and the SEO head.
 *
 * Main content, nav labels and extra CSS/HTML come from the site's own
 * renderers and are trusted; every value the shell itself interpolates is
 * escaped. main and headExtra are sealed (seal.ts) before they are placed,
 * so a malformed fragment (an unclosed comment, script, template or
 * formatting tag) cannot swallow or hide the footer that carries the notices.
 */
import { esc } from '../links';
import { amazonContentNoticeHtml, earningsDisclosureHtml } from '../notices';
import { PRICE_STAMP_CSS, STAMP_ATTR } from '../price';
import { themeCss, type SiteContext } from '../siteContext';
import { isAmazonHostedUrl } from './amazonHosts';
import { BASE_CSS_MIN } from './baseCss';
import { AMAZON_CONTENT_ATTR, breadcrumbNavHtml } from './blocks';
import { breadcrumbListLd, type BreadcrumbItem, type BreadcrumbOptions, type JsonLdNode } from './jsonld';
import { sealHtmlFragment } from './seal';
import { headTags, type HeadFields } from './seo';
import { siteUrl } from './urls';

type LayoutCtx = Pick<SiteContext, 'site' | 'theme' | 'disclosure'>;

export interface NavLink {
  label: string;
  /** Site path or URL on the site; an external https URL is allowed in the footer. */
  href: string;
  current?: boolean;
}

export interface FooterColumn {
  heading: string;
  links: readonly NavLink[];
}

export interface PageOptions {
  head: HeadFields;
  /** The page's content (trusted HTML), holding its one <h1>. */
  main: string;
  /** Breadcrumb trail, rendered above the content and mirrored as BreadcrumbList JSON-LD. */
  breadcrumbs?: readonly BreadcrumbItem[];
  breadcrumbOptions?: BreadcrumbOptions;
  /**
   * Whether the page shows Amazon content (titles, prices, availability).
   * Default true, so the 2(k) notice is never missing by omission. Set false
   * only for pages with none (about, privacy); kit price stamps or
   * data-amazon-content markers anywhere the page renders (main, bodyEnd,
   * headExtra) turn it back on.
   */
  amazonContent?: boolean;
  /**
   * The earnings disclosure above the content: 'auto' (default) when main or
   * bodyEnd holds affiliate links, or always / never. The footer always has it.
   */
  disclosureTop?: boolean | 'auto';
  nav?: readonly NavLink[];
  footerColumns?: readonly FooterColumn[];
  /** Trust links (about, methodology, disclosure, privacy). */
  footerLinks?: readonly NavLink[];
  /** Site CSS appended after the kit's (trusted). */
  css?: string;
  /** Trusted HTML appended inside <head> (preload hints, verification metas); sealed like main. */
  headExtra?: string;
  /**
   * Trusted HTML before </body> (the kit client bundle, a sticky buy bar).
   * Amazon content and affiliate links here count for the 2(k) notice and
   * the top disclosure just as they do in main.
   */
  bodyEnd?: string;
  /** Extra classes on <body> and <main>. */
  bodyClass?: string;
  mainClass?: string;
  /** Clock for the copyright year. */
  now?: number;
}

const AFFILIATE_LINK_RE = /<a\b[^>]*\b(?:rel\s*=\s*["'][^"']*\bsponsored\b|href\s*=\s*["']?(?:https?:)?\/\/(?:[a-z0-9-]+\.)*(?:amazon\.[a-z.]+|amzn\.(?:to|com|eu|asia)|a\.co)[/"'?]|href\s*=\s*["'][^"']*\/r\/[A-Z0-9]{10}\b)/i;
const AMAZON_CONTENT_RE = new RegExp(`\\b(?:${AMAZON_CONTENT_ATTR}|${STAMP_ATTR})\\b`);

/** True when `html` holds an affiliate link (rel="sponsored", an Amazon href, or a /r/<ASIN> redirect). */
export function htmlHasAffiliateLinks(html: string): boolean {
  return AFFILIATE_LINK_RE.test(html);
}

/** True when `html` holds Amazon content the kit can see: a price or availability stamp, or a data-amazon-content marker. */
export function htmlShowsAmazonContent(html: string): boolean {
  return AMAZON_CONTENT_RE.test(html);
}

function classList(...parts: Array<string | undefined>): string {
  return parts.join(' ').split(/\s+/).filter((c) => /^[A-Za-z0-9_-]+$/.test(c)).join(' ');
}

/**
 * href for a nav link: absolute on the site, or (footer only) an external
 * https URL. Never an Amazon URL: a link to Amazon is an affiliate link and
 * comes from the kit's CTA helpers (cta, amazonSearchCta) with a tag, rel and
 * placement, not from the site chrome.
 */
function navHref(ctx: Pick<SiteContext, 'site'>, href: string, allowExternal: boolean): { href: string; external: boolean } {
  try {
    return { href: siteUrl(ctx, href), external: false };
  } catch (e) {
    const h = href.trim();
    if (isAmazonHostedUrl(h)) {
      throw new TypeError(`nav link ${JSON.stringify(href)} points at Amazon; build Amazon links with the kit's CTA helpers`);
    }
    if (allowExternal && /^https:\/\/[^\s"'<>]+$/i.test(h)) return { href: h, external: true };
    throw e;
  }
}

function linkHtml(ctx: Pick<SiteContext, 'site'>, l: NavLink, allowExternal: boolean): string {
  const { href, external } = navHref(ctx, l.href, allowExternal);
  const cur = l.current ? ' aria-current="page"' : '';
  const rel = external ? ' rel="noopener"' : '';
  return `<a href="${esc(href)}"${cur}${rel}>${esc(l.label)}</a>`;
}

/** The site header: brand link home, then the main navigation. */
export function siteHeaderHtml(ctx: Pick<SiteContext, 'site'>, nav: readonly NavLink[] = []): string {
  const items = nav.map((l) => `<li>${linkHtml(ctx, l, false)}</li>`).join('');
  const navHtml = items ? `<nav class="kit-nav" aria-label="Main navigation"><ul>${items}</ul></nav>` : '';
  return `<header class="kit-header"><div class="kit-wrap kit-header-inner">`
    + `<a class="kit-brand" href="${esc(`${ctx.site.canonicalOrigin}/`)}">${esc(ctx.site.brandName)}</a>${navHtml}</div></header>`;
}

export interface FooterOptions {
  /** Render the 2(k) notice (the layout decides; see PageOptions.amazonContent). */
  amazonNotice: boolean;
  columns?: readonly FooterColumn[];
  links?: readonly NavLink[];
  now?: number;
}

/**
 * The site footer: the 2(k) notice (when asked), the earnings disclosure,
 * the section columns, the trust links and the copyright line.
 */
export function siteFooterHtml(ctx: Pick<SiteContext, 'site' | 'disclosure'>, o: FooterOptions): string {
  const parts: string[] = [];
  if (o.amazonNotice) parts.push(amazonContentNoticeHtml());
  parts.push(earningsDisclosureHtml(ctx));
  const cols = (o.columns ?? []).filter((c) => c.links.length).map((c) =>
    `<div class="kit-footer-col"><p class="kit-footer-heading">${esc(c.heading)}</p>`
    + `<ul>${c.links.map((l) => `<li>${linkHtml(ctx, l, true)}</li>`).join('')}</ul></div>`);
  if (cols.length) parts.push(`<nav class="kit-footer-cols" aria-label="Site sections">${cols.join('')}</nav>`);
  const links = (o.links ?? []).map((l) => `<li>${linkHtml(ctx, l, true)}</li>`).join('');
  if (links) parts.push(`<nav class="kit-footer-links" aria-label="About this site"><ul>${links}</ul></nav>`);
  const year = new Date(o.now ?? Date.now()).getUTCFullYear();
  parts.push(`<p class="kit-copyright">&copy; ${year} ${esc(ctx.site.brandName)}</p>`);
  return `<footer class="kit-footer"><div class="kit-wrap">${parts.join('')}</div></footer>`;
}

/**
 * Put the top earnings disclosure right after the content's first </h1>
 * when that comes before the first affiliate link, else at the very top.
 */
function withTopDisclosure(main: string, disclosure: string): string {
  const aff = main.search(AFFILIATE_LINK_RE);
  const h1 = /<\/h1\s*>/i.exec(main);
  if (h1 && (aff < 0 || h1.index < aff)) {
    const at = h1.index + h1[0].length;
    return `${main.slice(0, at)}${disclosure}${main.slice(at)}`;
  }
  return `${disclosure}${main}`;
}

/** CSS safe to inline in <style>: no way to close the element early. */
function inlineCss(css: string): string {
  return css.replace(/<\/(style)/gi, '<\\/$1');
}

/**
 * The 2(k) decision for a page, as renderPage() makes it: true unless
 * amazonContent is false and nothing the page renders (main, bodyEnd,
 * headExtra) holds a kit price or availability stamp or an Amazon-content
 * marker.
 */
export function needsAmazonNotice(opts: Pick<PageOptions, 'amazonContent' | 'main' | 'bodyEnd' | 'headExtra'>): boolean {
  if (opts.amazonContent !== false) return true;
  return [opts.main, opts.bodyEnd, opts.headExtra].some((html) => !!html && htmlShowsAmazonContent(html));
}

/** A complete HTML document for one page. */
export function renderPage(ctx: LayoutCtx, opts: PageOptions): string {
  const main = sealHtmlFragment(opts.main);
  const headExtra = sealHtmlFragment(opts.headExtra);
  const bodyEnd = String(opts.bodyEnd ?? '');
  const trail = opts.breadcrumbs ?? [];
  const crumbsHtml = trail.length ? breadcrumbNavHtml(ctx, trail, opts.breadcrumbOptions) : '';

  // The breadcrumb's JSON-LD twin, unless the head already carries one.
  const jsonLd: Array<JsonLdNode | null | undefined | false> = [...(opts.head.jsonLd ?? [])];
  if (crumbsHtml && !jsonLd.some((n) => n && n['@type'] === 'BreadcrumbList')) {
    jsonLd.push(breadcrumbListLd(ctx, trail, opts.breadcrumbOptions));
  }

  // 2(k): on by default, forced on by Amazon content anywhere on the page. A
  // notice-like block inside the content never stands in for this one (its
  // text or visibility cannot be checked here); a second copy does no harm.
  const amazonNotice = needsAmazonNotice({ amazonContent: opts.amazonContent, main, bodyEnd, headExtra });

  const top = opts.disclosureTop ?? 'auto';
  const wantTop = top === true || (top === 'auto' && (htmlHasAffiliateLinks(main) || htmlHasAffiliateLinks(bodyEnd)));
  const body = wantTop
    ? withTopDisclosure(main, earningsDisclosureHtml(ctx, 'kit-earnings-disclosure kit-earnings-disclosure--top'))
    : main;

  const css = `${themeCss(ctx.theme)}\n${BASE_CSS_MIN}\n${PRICE_STAMP_CSS}${opts.css ? `\n${inlineCss(opts.css)}` : ''}`;
  const head = [
    '<meta charset="utf-8">',
    '<meta name="viewport" content="width=device-width, initial-scale=1">',
    '<meta name="color-scheme" content="light dark">',
    `<meta name="theme-color" content="${esc(ctx.theme.light.bg)}" media="(prefers-color-scheme: light)">`,
    `<meta name="theme-color" content="${esc(ctx.theme.dark.bg)}" media="(prefers-color-scheme: dark)">`,
    headTags(ctx, { ...opts.head, jsonLd }),
    `<style id="ssr-styles">${css}</style>`,
    headExtra,
  ].filter(Boolean).join('\n');

  const bodyClass = classList('kit-page', opts.bodyClass);
  const mainClass = classList('ssr-main', 'kit-main', 'kit-wrap', opts.mainClass);
  return [
    '<!doctype html>',
    `<html lang="${esc(ctx.site.language)}">`,
    '<head>',
    head,
    '</head>',
    `<body class="${bodyClass}">`,
    '<a class="kit-skip-link" href="#main">Skip to content</a>',
    siteHeaderHtml(ctx, opts.nav),
    `<main id="main" class="${mainClass}">`,
    crumbsHtml,
    body,
    '</main>',
    siteFooterHtml(ctx, { amazonNotice, columns: opts.footerColumns, links: opts.footerLinks, now: opts.now }),
    bodyEnd,
    '</body>',
    '</html>',
    '',
  ].filter((l, k, all) => l !== '' || k === all.length - 1).join('\n');
}
