/**
 * The site's page renderers, built only from kit modules: the layout shell
 * (theme tokens, earnings disclosure, the 2(k) notice whenever a page shows
 * Amazon content), the SEO head, JSON-LD, and Markdown with GFM tables.
 *
 * Each renderer is a pure function of its data and the clock, so the same
 * rows always render the same page. Copy that names the site comes from
 * site.json (brand, byline, disclosure), never from literals here.
 *
 * Scaffold pages: home, the guides index and article pages, about,
 * disclosure, privacy, and the not-found / error pages. Product pages, hubs
 * and the niche's own renderers are added next to these; every Amazon price,
 * CTA and title they show must come from the kit's helpers (priceStamp, cta,
 * amazonTextHtml), so the licence rules hold by construction.
 */
import { esc, type SiteContext } from '../../vendor/affiliate-kit/server/src';
import { kitClientScriptTag, type KitClientBundle } from '../../vendor/affiliate-kit/server/src/http';
import {
  INDEXABLE_PAGE,
  articleLd,
  faqPageLd,
  faqSectionHtml,
  mdToHtml,
  metaDescriptionFrom,
  noindexPage,
  organizationLd,
  pageTitle,
  publicImageUrl,
  renderPage,
  webSiteLd,
  type BreadcrumbItem,
  type JsonLdNode,
  type NavLink,
  type PageIndexing,
} from '../../vendor/affiliate-kit/server/src/render';
import type { Article, ArticleSummary } from '../db/articles';
import { GUIDES_PATH, guidePath } from '../routes/table';

/** Marks an article's hero image (the gate checks its host). */
export const HERO_IMAGE_ATTR = 'data-hero-image';

const NAV: readonly NavLink[] = Object.freeze([
  { label: 'Guides', href: GUIDES_PATH },
  { label: 'About', href: '/about' },
]);

/** Site CSS on the kit's theme tokens only (every token pair is contrast-checked when site.json loads). */
const SITE_CSS = [
  '.site-lede{font-size:1.1rem;color:var(--kit-text)}',
  '.site-list{list-style:none;margin:0;padding:0;display:grid;gap:12px}',
  '.site-list li{background:var(--kit-surface);color:var(--kit-text);border:1px solid var(--kit-border);border-radius:12px;padding:12px 16px}',
  '.site-list p{margin:6px 0 0;color:var(--kit-muted)}',
  '.site-byline,.site-subtitle{color:var(--kit-muted)}',
  '.site-hero{display:block;max-width:100%;height:auto;border-radius:12px}',
].join('');

export interface ShellInput {
  /** The page's own path, which is its canonical. */
  path: string;
  /** The title before the brand suffix (pageTitle adds it). */
  title: string;
  description: string;
  main: string;
  /**
   * Whether the page shows Amazon content. Default true, as in the kit's
   * layout, so the 2(k) notice is never missing by omission: set false only
   * on a page that by construction shows none (about, disclosure, privacy, the
   * error pages, and lists of this site's own guides). The kit turns the
   * notice back on anyway when it sees a price stamp or a data-amazon-content
   * marker, but it cannot see an Amazon title written into Markdown.
   */
  amazonContent?: boolean;
  indexing?: PageIndexing;
  breadcrumbs?: readonly BreadcrumbItem[];
  jsonLd?: ReadonlyArray<JsonLdNode | null | undefined>;
  ogType?: 'website' | 'article' | 'product';
  ogImage?: string | null;
}

function isoDay(d: Date | null): string {
  return d ? d.toISOString().slice(0, 10) : '';
}

export class SiteRenderer {
  constructor(
    private readonly ctx: SiteContext,
    private readonly bundle: KitClientBundle | null,
    private readonly clock: () => number = Date.now,
  ) {}

  /** One complete document in the kit's layout. */
  page(i: ShellInput): string {
    return renderPage(this.ctx, {
      head: {
        title: pageTitle(this.ctx, i.title),
        description: metaDescriptionFrom(i.description),
        canonical: i.path,
        indexing: i.indexing ?? INDEXABLE_PAGE,
        ogType: i.ogType ?? 'website',
        ogImage: i.ogImage ?? null,
        jsonLd: i.jsonLd ?? [],
      },
      main: i.main,
      breadcrumbs: i.breadcrumbs,
      amazonContent: i.amazonContent ?? true,
      nav: NAV,
      footerLinks: [
        { label: 'About', href: '/about' },
        { label: 'How we earn money', href: this.ctx.disclosure.pagePath },
        { label: 'Privacy', href: '/privacy' },
      ],
      css: SITE_CSS,
      bodyEnd: this.bundle ? kitClientScriptTag(this.bundle) : '',
      now: this.clock(),
    });
  }

  private guideList(items: readonly ArticleSummary[]): string {
    return `<ul class="site-list">${items.map((a) => `<li><a href="${esc(guidePath(a.slug))}">${esc(a.title)}</a>`
      + `${a.excerpt ? `<p>${esc(a.excerpt)}</p>` : ''}</li>`).join('')}</ul>`;
  }

  home(latest: readonly ArticleSummary[]): string {
    const { brandName } = this.ctx.site;
    const guides = latest.length ? this.guideList(latest) : '<p>The first guides are on their way.</p>';
    const main = `<h1>${esc(brandName)}</h1>`
      + `<p class="site-lede">Buying guides and product pages. Specifications come from the makers' published data; `
      + `prices and availability come from ${esc(this.ctx.amazon.siteName)} and show the time they were checked.</p>`
      + `<h2>Latest guides</h2>${guides}`
      + `<p><a href="${esc(GUIDES_PATH)}">All guides</a></p>`;
    return this.page({
      path: '/',
      title: 'Buying Guides and Product Pages',
      description: `${brandName} publishes buying guides and product pages, with specifications from the makers and prices checked on ${this.ctx.amazon.siteName}.`,
      main,
      // This site's guide titles and excerpts only. Drop this line when product cards land here.
      amazonContent: false,
      jsonLd: [organizationLd(this.ctx), webSiteLd(this.ctx)],
    });
  }

  guidesIndex(articles: readonly ArticleSummary[]): string {
    const { brandName } = this.ctx.site;
    const body = articles.length ? this.guideList(articles) : '<p>The first guides are on their way.</p>';
    return this.page({
      path: GUIDES_PATH,
      title: 'Buying Guides',
      description: `Every buying guide on ${brandName}: what to look for, how the options compare, and which to buy.`,
      main: `<h1>Buying Guides</h1>${body}`,
      // This site's guide titles and excerpts only. Drop this line if products are ever listed here.
      amazonContent: false,
      breadcrumbs: [{ name: 'Guides' }],
    });
  }

  guide(a: Article): string {
    const path = guidePath(a.slug);
    const hero = publicImageUrl(this.ctx, a.heroImageUrl);
    const main = '<article>'
      + `<h1>${esc(a.title)}</h1>`
      + (a.subtitle ? `<p class="site-subtitle">${esc(a.subtitle)}</p>` : '')
      + `<p class="site-byline">By ${esc(this.ctx.byline.name)}`
      + (a.publishedAt ? `, <time datetime="${esc(a.publishedAt.toISOString())}">${esc(isoDay(a.publishedAt))}</time>` : '')
      + '</p>'
      + (hero ? `<img class="site-hero" ${HERO_IMAGE_ATTR} src="${esc(hero)}" alt="" width="1200" height="630">` : '')
      + mdToHtml(this.ctx, a.bodyMd, { amazonPlacement: 'in-article' })
      + faqSectionHtml(a.faqs)
      + '</article>';
    return this.page({
      path,
      title: a.title,
      description: a.excerpt ?? a.subtitle ?? a.title,
      main,
      // amazonContent stays at its default (true): an article may quote an Amazon
      // product title in its Markdown, which the kit cannot tell from our own text,
      // so every guide carries the 2(k) notice.
      ogType: 'article',
      ogImage: hero,
      breadcrumbs: [{ name: 'Guides', href: GUIDES_PATH }, { name: a.title }],
      jsonLd: [
        articleLd(this.ctx, {
          headline: a.title,
          url: path,
          description: a.excerpt,
          datePublished: a.publishedAt,
          dateModified: a.updatedAt,
          image: hero,
          section: a.category || null,
        }),
        faqPageLd(a.faqs),
      ],
    });
  }

  about(): string {
    const { brandName } = this.ctx.site;
    const disclosure = this.ctx.disclosure.pagePath;
    return this.page({
      path: '/about',
      title: 'About',
      description: `Who writes ${brandName}, where its product data comes from, and how the site earns money.`,
      main: `<h1>About ${esc(brandName)}</h1>`
        + `<p>${esc(brandName)} publishes buying guides and product pages. ${esc(this.ctx.byline.name)} writes and maintains them.</p>`
        + '<h2>Where the data comes from</h2>'
        + `<p>Specifications come from the makers' spec sheets and manuals. Prices and availability come from ${esc(this.ctx.amazon.siteName)}; `
        + 'each one shows the time it was checked, and they can change after that.</p>'
        + '<h2>How the site earns money</h2>'
        + `<p>${esc(this.ctx.disclosure.earnings)} <a href="${esc(disclosure)}">How we earn money</a> has the details.</p>`,
      amazonContent: false,
      breadcrumbs: [{ name: 'About' }],
    });
  }

  disclosure(): string {
    const { brandName } = this.ctx.site;
    return this.page({
      path: this.ctx.disclosure.pagePath,
      title: 'How We Earn Money',
      description: `How ${brandName} earns money: commissions from Amazon on qualifying purchases, and what that does and does not change.`,
      main: `<h1>How ${esc(brandName)} Earns Money</h1>`
        + `<p>${esc(this.ctx.disclosure.earnings)}</p>`
        + `<p>When you follow a link from this site to ${esc(this.ctx.amazon.siteName)} and buy, the site may earn a commission. `
        + 'The commission does not change the price you pay.</p>'
        + '<p>This site does not track prices over time and does not send price alerts.</p>',
      amazonContent: false,
      breadcrumbs: [{ name: 'How we earn money' }],
    });
  }

  privacy(): string {
    const { brandName } = this.ctx.site;
    return this.page({
      path: '/privacy',
      title: 'Privacy',
      description: `What ${brandName} records when you visit, and why: clicks through to Amazon and visits from AI assistants, with no accounts and no cookies.`,
      main: '<h1>Privacy</h1>'
        + `<p>${esc(brandName)} has no accounts and sets no cookies.</p>`
        + '<h2>Clicks to Amazon</h2>'
        + `<p>When you click a link to ${esc(this.ctx.amazon.siteName)}, the site records the click: the page and the button, `
        + 'the tracking tag, your IP address, browser and device type, language and country. It uses them to count the visits '
        + 'that reach Amazon and to tell people from bots. A small script keeps a count of the pages you viewed in this '
        + 'browser tab (session storage, cleared when the tab closes).</p>'
        + '<h2>AI assistants and crawlers</h2>'
        + '<p>Visits from AI assistants and search crawlers are recorded with their user agent, IP address and the page fetched.</p>'
        + '<h2>On Amazon</h2>'
        + `<p>Once you are on ${esc(this.ctx.amazon.siteName)}, Amazon's privacy notice applies.</p>`,
      amazonContent: false,
      breadcrumbs: [{ name: 'Privacy' }],
    });
  }

  notFound(path: string): string {
    return this.page({
      path,
      title: 'Page Not Found',
      description: 'The page you asked for does not exist on this site.',
      main: '<h1>Page Not Found</h1><p>Start again from the <a href="/">home page</a> or the <a href="/guides">guides</a>.</p>',
      amazonContent: false,
      indexing: noindexPage('not found'),
    });
  }

  serverError(path: string): string {
    return this.page({
      path,
      title: 'Something Went Wrong',
      description: 'This page could not be shown right now.',
      main: '<h1>Something Went Wrong</h1><p>Try again in a minute.</p>',
      amazonContent: false,
      indexing: noindexPage('server error'),
    });
  }
}
