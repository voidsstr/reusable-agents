/**
 * The fixture's page renderers, built only from kit modules: the layout
 * shell, price and availability stamps, CTAs, the sticky bar, Markdown with
 * GFM tables, JSON-LD and the sitemap set. Each renderer is a pure function
 * of its rows and the clock, so a page renders the same for the same data.
 *
 * Notes (fixture_notes) are the one family that trusts stored HTML, the way a
 * site's hand-written pages do; the violations scenario seeds its
 * non-compliant pages there.
 */
import {
  amazonSearchCta,
  amazonSearchUrlFor,
  availabilityIsShowable,
  availabilityKindFromType,
  availabilityStamp,
  cta,
  detailLink,
  esc,
  priceStamp,
  productPath,
  quotablePrice,
  type PriceFields,
  type SiteContext,
} from '../../server/src';
import { STICKY_BAR_CSS, kitClientScriptTag, stickyBuyBarHtml, type KitClientBundle } from '../../server/src/http';
import {
  INDEXABLE_PAGE,
  amazonTextHtml,
  articleLd,
  buildSitemapSet,
  faqPageLd,
  faqSectionHtml,
  isSimilarToLd,
  itemListLd,
  mdToHtml,
  metaDescriptionFrom,
  noindexPage,
  organizationLd,
  pageTitle,
  productLd,
  publicImageUrl,
  renderPage,
  webSiteLd,
  type BreadcrumbItem,
  type JsonLdNode,
  type NavLink,
  type PageIndexing,
  type SitemapCandidate,
  type SitemapSet,
} from '../../server/src/render';
import type { ArticleRow, BestListRow, NoteRow, ProductRow, RecallRow } from './db';

/** Classes and attributes the gate config looks for. */
export const RECALL_NOTICE_ATTR = 'data-recall-notice';
export const ALTERNATIVE_ATTR = 'data-kit-alternative';
export const HERO_IMAGE_ATTR = 'data-hero-image';

const NAV: readonly NavLink[] = Object.freeze([
  { label: 'Guides', href: '/guides' },
  { label: 'Best widgets', href: '/best/best-widgets' },
  { label: 'Recalls', href: '/recalls' },
]);

const FOOTER_LINKS: readonly NavLink[] = Object.freeze([
  { label: 'About', href: '/about' },
  { label: 'How we earn money', href: '/disclosure' },
  { label: 'How this fixture works', href: '/notes/how-this-fixture-works' },
]);

/** Fixture CSS on the kit's theme tokens (every pair here is a validated one). */
const FIXTURE_CSS = [
  STICKY_BAR_CSS,
  '.fx-cards{list-style:none;margin:0;padding:0;display:grid;gap:16px}',
  '.fx-card{background:var(--kit-surface);color:var(--kit-text);border:1px solid var(--kit-border);border-radius:12px;padding:16px}',
  '.fx-card h2{margin:0 0 8px;font-size:1.15rem}',
  '.fx-card p{margin:6px 0}',
  '.fx-buy{background:var(--kit-surface);color:var(--kit-text);border:1px solid var(--kit-border);border-radius:12px;padding:16px;margin:16px 0}',
  '.fx-buy p{margin:6px 0}',
  '.fx-listing,.fx-byline,.fx-subtitle{color:var(--kit-muted)}',
  '.fx-recall{border:2px solid var(--kit-accent);border-radius:12px;padding:16px;margin:16px 0;background:var(--kit-bg);color:var(--kit-text)}',
  '.fx-alt{background:var(--kit-surface);color:var(--kit-text);border:1px solid var(--kit-border);border-radius:12px;padding:16px;margin:16px 0}',
  '.fx-specs{border-collapse:collapse;max-width:100%}',
  '.fx-specs th,.fx-specs td{border:1px solid var(--kit-border);padding:6px 10px;text-align:left}',
  '.fx-hero,.fx-photo{display:block;max-width:100%;height:auto;border-radius:12px}',
  '.fx-buystrip{margin:24px 0}',
].join('');

const DEFAULT_OG = Object.freeze({ url: '/assets/og-card.png', width: 1200, height: 630, alt: 'GateFixture' });

export interface ShellInput {
  /** The page's own path: its canonical. */
  path: string;
  title: string;
  description: string;
  main: string;
  amazonContent: boolean;
  indexing?: PageIndexing;
  breadcrumbs?: readonly BreadcrumbItem[];
  jsonLd?: ReadonlyArray<JsonLdNode | null | undefined>;
  ogType?: 'website' | 'article' | 'product';
  ogImage?: string | null;
  headExtra?: string;
}

/** Whether the product's Amazon data may be shown right now (title, image, availability). */
function amazonDataShowable(ctx: SiteContext, p: ProductRow, now: number): boolean {
  return availabilityIsShowable(ctx, p.fetchedAt, now);
}

function priceOf(p: ProductRow): PriceFields {
  return { price: p.price, currency: p.currency, fetchedAt: p.fetchedAt, qualityFlags: p.qualityFlags };
}

/** A product that may carry a buy button: live, active, not under an active recall. */
export function buyable(p: ProductRow): boolean {
  return p.isActive && p.lifecycle === 'live' && !p.recallId;
}

function day(d: Date | null | undefined): string {
  return d ? d.toISOString().slice(0, 10) : '';
}

export class FixtureRenderer {
  constructor(
    private readonly ctx: SiteContext,
    private readonly bundle: KitClientBundle | null,
    private readonly clock: () => number = Date.now,
  ) {}

  /** One complete document in the kit layout. */
  page(i: ShellInput): string {
    return renderPage(this.ctx, {
      head: {
        title: pageTitle(this.ctx, i.title),
        description: metaDescriptionFrom(i.description),
        canonical: i.path,
        indexing: i.indexing ?? INDEXABLE_PAGE,
        ogType: i.ogType ?? 'website',
        ogImage: i.ogImage ?? null,
        defaultOgImage: DEFAULT_OG,
        jsonLd: i.jsonLd ?? [],
      },
      main: i.main,
      breadcrumbs: i.breadcrumbs,
      amazonContent: i.amazonContent,
      nav: NAV,
      footerLinks: FOOTER_LINKS,
      css: FIXTURE_CSS,
      headExtra: i.headExtra,
      bodyEnd: this.bundle ? kitClientScriptTag(this.bundle) : '',
      now: this.clock(),
    });
  }

  /** A product card: our name and summary, the stamp and the CTA while buyable. */
  private card(p: ProductRow, placement: string, now: number): string {
    const stamp = buyable(p) ? priceStamp(this.ctx, priceOf(p), { now, key: `${p.asin}|${placement}` }) : '';
    const buy = buyable(p) ? cta(this.ctx, { asin: p.asin, placement }) : '';
    return `<li class="fx-card"><h2><a href="${esc(productPath(this.ctx, p.asin))}">${esc(p.modelName)}</a></h2>`
      + `<p>${esc(p.summary)}</p>`
      + (stamp ? `<p class="fx-price">${stamp}</p>` : '')
      + (buy ? `<p>${buy}</p>` : '')
      + `<p>${detailLink(this.ctx, p.asin, `${p.modelName} details`)}</p></li>`;
  }

  home(products: readonly ProductRow[], articles: readonly ArticleRow[]): string {
    const now = this.clock();
    const shown = products.filter(buyable).slice(0, 6);
    const guides = articles.slice(0, 5).map((a) => `<li><a href="/guides/${esc(a.slug)}">${esc(a.title)}</a></li>`).join('');
    const main = '<h1>Widgets Compared</h1>'
      + '<p>Specs, current Amazon prices and buying guides for portable widgets.</p>'
      + `<ul class="fx-cards">${shown.map((p) => this.card(p, 'home-card', now)).join('')}</ul>`
      + `<h2>Buying guides</h2><ul class="ssr-list">${guides}</ul>`
      + '<p><a href="/best/best-widgets">See the ranked list of the best widgets</a></p>';
    const list = itemListLd(this.ctx, {
      name: 'Widgets compared',
      url: '/',
      items: shown.map((p) => ({ name: p.modelName, url: productPath(this.ctx, p.asin), asin: p.asin, brand: p.brand, offer: priceOf(p) })),
    }, now);
    return this.page({
      path: '/',
      title: 'Widgets Compared: Specs, Prices and Guides',
      description: 'Compare portable widgets by capacity and weight, with current Amazon prices, buying guides and recall notices.',
      main,
      amazonContent: true,
      jsonLd: [organizationLd(this.ctx), webSiteLd(this.ctx), list],
    });
  }

  product(p: ProductRow, others: ReadonlyMap<string, ProductRow>, recall: RecallRow | null): string {
    const now = this.clock();
    const path = productPath(this.ctx, p.asin);
    const fresh = amazonDataShowable(this.ctx, p, now);
    const parts: string[] = [`<h1>${esc(p.modelName)}</h1>`];
    const alt = p.alternativeAsin ? others.get(p.alternativeAsin) ?? null : null;
    const altUsable = !!alt && buyable(alt) && quotablePrice(this.ctx, priceOf(alt), now) != null;
    let sticky = '';

    if (recall) {
      parts.push(`<section class="fx-recall" ${RECALL_NOTICE_ATTR} role="alert">`
        + `<h2>Recall: ${recall.status === 'stop-use' ? 'stop using this product' : 'do not buy or use this product until it is repaired'}</h2>`
        + `<p>${esc(recall.title)}. ${esc(recall.hazard)} ${esc(recall.remedy)}</p>`
        + `<p><a href="/recalls/${esc(recall.id)}">Read the recall notice</a> (announced ${esc(recall.announcedOn)}).</p></section>`);
    } else if (buyable(p)) {
      if (fresh && p.title) parts.push(`<p class="fx-listing">Listed on Amazon as ${amazonTextHtml(p.title, { tag: 'span' })}</p>`);
      const photo = fresh ? String(p.imageUrl ?? '') : '';
      if (/^https:\/\//.test(photo)) {
        parts.push(`<img class="fx-photo" src="${esc(photo)}" alt="${esc(p.modelName)}" width="300" height="300" loading="lazy" decoding="async">`);
      }
      const stamp = priceStamp(this.ctx, priceOf(p), { now, key: `${p.asin}|pdp-hero` });
      const kind = availabilityKindFromType(p.availabilityType);
      const avail = fresh && kind && kind !== 'in-stock' ? availabilityStamp(this.ctx, { kind, checkedAt: p.fetchedAt }, { now, key: `${p.asin}|pdp-hero` }) : '';
      parts.push('<div class="fx-buy">'
        + (stamp ? `<p class="fx-price">${stamp}</p>` : '')
        + (avail ? `<p class="fx-availability">${avail}</p>` : '')
        + `<p>${cta(this.ctx, { asin: p.asin, placement: 'pdp-hero' })}</p></div>`);
      sticky = stickyBuyBarHtml(this.ctx, { name: p.modelName, asin: p.asin, price: priceOf(p), now });
    } else {
      // Unavailable on Amazon: say so with a dated stamp, then hand over something buyable.
      parts.push(`<p class="fx-status">${availabilityStamp(this.ctx, { kind: 'unavailable', checkedAt: p.fetchedAt }, { now, key: `${p.asin}|pdp-status` })}</p>`);
    }

    if (!buyable(p)) {
      if (alt && altUsable) {
        const placement = recall ? 'pdp-substitute' : 'pdp-alt';
        // Availability is said only by the dated stamps (2(i)): the heading and the bar's status never claim stock.
        parts.push(`<section class="fx-alt" ${ALTERNATIVE_ATTR}><h2>${recall ? 'A similar widget without a recall' : 'A similar widget you can buy'}</h2>`
          + `<p><a href="${esc(productPath(this.ctx, alt.asin))}">${esc(alt.modelName)}</a>: ${esc(alt.summary)}</p>`
          + `<p class="fx-price">${priceStamp(this.ctx, priceOf(alt), { now, key: `${alt.asin}|${placement}` })}</p>`
          + `<p>${cta(this.ctx, { asin: alt.asin, placement })}</p></section>`);
        sticky = stickyBuyBarHtml(this.ctx, {
          name: alt.modelName,
          asin: alt.asin,
          placement: 'pdp-sticky-live',
          status: recall ? 'Recalled. A similar widget:' : 'A similar widget:',
          price: priceOf(alt),
          now,
        });
      } else if (!recall) {
        const url = amazonSearchUrlFor(this.ctx, p.modelName, p.brand);
        parts.push(`<p>No in-stock alternative is on file. ${amazonSearchCta(this.ctx, { url, placement: 'pdp-search' })}</p>`);
      }
    }

    parts.push(`<p>${esc(p.summary)}</p>`);
    const specs: string[] = [];
    if (p.capacityWh != null) specs.push(`<tr><th scope="row">Capacity</th><td>${esc(p.capacityWh.toLocaleString('en-US'))} Wh</td></tr>`);
    if (p.weightKg != null) specs.push(`<tr><th scope="row">Weight</th><td>${esc(p.weightKg)} kg</td></tr>`);
    specs.push(`<tr><th scope="row">Brand</th><td>${esc(p.brand)}</td></tr>`);
    parts.push(`<h2>Specifications</h2><table class="fx-specs"><tbody>${specs.join('')}</tbody></table>`);
    if (sticky) parts.push(sticky);

    const offer = buyable(p) ? { ...priceOf(p), availabilityType: p.availabilityType } : null;
    const similar = alt && altUsable ? isSimilarToLd(this.ctx, p.asin, [{ asin: alt.asin, name: alt.modelName }]) : null;
    return this.page({
      path,
      title: p.modelName,
      description: `${p.modelName}: ${p.summary}`,
      main: `<article class="fx-pdp">${parts.join('')}</article>`,
      amazonContent: true,
      ogType: 'product',
      ogImage: p.imageUrl,
      breadcrumbs: [{ name: 'Widgets', href: '/best/best-widgets' }, { name: p.modelName }],
      jsonLd: [productLd(this.ctx, { asin: p.asin, name: p.modelName, brand: p.brand, description: p.summary, offer }, now), similar],
    });
  }

  guidesIndex(articles: readonly ArticleRow[]): string {
    const items = articles.map((a) => `<li><a href="/guides/${esc(a.slug)}">${esc(a.title)}</a>${a.excerpt ? ` <span class="fx-subtitle">${esc(a.excerpt)}</span>` : ''}</li>`);
    return this.page({
      path: '/guides',
      title: 'Widget Buying Guides',
      description: 'Buying guides for portable widgets: how to size one, how to care for it, and how to use it safely.',
      main: `<h1>Widget Buying Guides</h1><ul class="ssr-list">${items.join('')}</ul>`,
      amazonContent: false,
      breadcrumbs: [{ name: 'Guides' }],
    });
  }

  guide(a: ArticleRow, related: ReadonlyMap<string, ProductRow>): string {
    const now = this.clock();
    const path = `/guides/${a.slug}`;
    const hero = publicImageUrl(this.ctx, a.heroImageUrl);
    const strip = a.relatedProductAsins
      .map((asin) => related.get(asin))
      .filter((p): p is ProductRow => !!p && buyable(p))
      .map((p) => this.card(p, 'article-buystrip', now));
    const main = '<article class="fx-article">'
      + `<h1>${esc(a.title)}</h1>`
      + (a.subtitle ? `<p class="fx-subtitle">${esc(a.subtitle)}</p>` : '')
      + `<p class="fx-byline">By ${esc(this.ctx.byline.name)}${a.publishedAt ? `, <time datetime="${esc(a.publishedAt.toISOString())}">${esc(day(a.publishedAt))}</time>` : ''}</p>`
      + (hero ? `<img class="fx-hero" ${HERO_IMAGE_ATTR} src="${esc(hero)}" alt="" width="1200" height="630">` : '')
      + mdToHtml(this.ctx, a.bodyMd, { amazonPlacement: 'in-article' })
      + (strip.length ? `<aside class="fx-buystrip" aria-label="Products in this guide"><h2>Products in this guide</h2><ul class="fx-cards">${strip.join('')}</ul></aside>` : '')
      + faqSectionHtml(a.faqs)
      + '</article>';
    return this.page({
      path,
      title: a.title,
      description: a.excerpt ?? a.title,
      main,
      amazonContent: strip.length > 0,
      ogType: 'article',
      ogImage: hero,
      breadcrumbs: [{ name: 'Guides', href: '/guides' }, { name: a.title }],
      jsonLd: [
        articleLd(this.ctx, {
          headline: a.title,
          url: path,
          description: a.excerpt,
          datePublished: a.publishedAt,
          dateModified: a.updatedAt,
          image: hero,
          section: a.category,
        }),
        faqPageLd(a.faqs),
      ],
    });
  }

  bestList(b: BestListRow, products: ReadonlyMap<string, ProductRow>): string {
    const now = this.clock();
    const ranked = b.asins.map((asin) => products.get(asin)).filter((p): p is ProductRow => !!p && buyable(p));
    const path = `/best/${b.slug}`;
    const main = `<h1>${esc(b.title)}</h1>`
      + mdToHtml(this.ctx, b.introMd, { amazonPlacement: 'best-list' })
      + `<ol class="fx-cards">${ranked.map((p) => this.card(p, 'best-list', now)).join('')}</ol>`;
    return this.page({
      path,
      title: b.title,
      description: `${b.title}: the widgets ranked by usable capacity per kilogram, with current Amazon prices.`,
      main,
      amazonContent: true,
      breadcrumbs: [{ name: b.title }],
      jsonLd: [itemListLd(this.ctx, {
        name: b.title,
        url: path,
        order: 'ascending',
        items: ranked.map((p) => ({ name: p.modelName, url: productPath(this.ctx, p.asin), asin: p.asin, brand: p.brand, offer: priceOf(p) })),
      }, now)],
    });
  }

  recallsIndex(recalls: readonly RecallRow[]): string {
    const items = recalls.map((r) => `<li><a href="/recalls/${esc(r.id)}">${esc(r.title)}</a> (${esc(r.announcedOn)}): `
      + `<a href="${esc(productPath(this.ctx, r.asin))}">${esc(r.modelName)}</a></li>`);
    return this.page({
      path: '/recalls',
      title: 'Widget Recall Notices',
      description: 'Widget recalls from public safety notices. A recalled product keeps its page here, but never a buy button.',
      main: '<h1>Widget Recall Notices</h1><p>Products under an active recall have no buy button on this site.</p>'
        + `<ul class="ssr-list">${items.join('')}</ul>`,
      amazonContent: false,
      breadcrumbs: [{ name: 'Recalls' }],
    });
  }

  recall(r: RecallRow): string {
    return this.page({
      path: `/recalls/${r.id}`,
      title: r.title,
      description: `${r.title}. ${r.hazard} ${r.remedy}`,
      main: `<h1>${esc(r.title)}</h1>`
        + `<p>${esc(r.hazard)}</p><p><strong>What to do:</strong> ${esc(r.remedy)}</p>`
        + `<p>Product: <a href="${esc(productPath(this.ctx, r.asin))}">${esc(r.modelName)}</a>. Announced ${esc(r.announcedOn)}. `
        + `<a href="${esc(r.sourceUrl)}" rel="noopener">Source notice</a>.</p>`,
      amazonContent: false,
      breadcrumbs: [{ name: 'Recalls', href: '/recalls' }, { name: r.title }],
    });
  }

  note(n: NoteRow): string {
    return this.page({
      path: `/notes/${n.slug}`,
      title: n.title,
      description: n.description,
      main: `<h1>${esc(n.title)}</h1>${n.bodyHtml}`,
      amazonContent: n.amazonContent,
      headExtra: n.headHtml ?? undefined,
      jsonLd: n.jsonld ? [n.jsonld as JsonLdNode] : [],
    });
  }

  about(): string {
    return this.page({
      path: '/about',
      title: 'About GateFixture',
      description: 'GateFixture is the gate fixture of the affiliate-site kit: a small site rendered only by kit modules.',
      main: '<h1>About GateFixture</h1>'
        + `<p>${esc(this.ctx.byline.name)} writes the guides on this site. The product data comes from each maker's published specs; prices and availability come from Amazon and carry the time they were checked.</p>`,
      amazonContent: false,
      breadcrumbs: [{ name: 'About' }],
    });
  }

  disclosure(): string {
    return this.page({
      path: '/disclosure',
      title: 'How GateFixture Earns Money',
      description: 'How this site earns money: Amazon Associates commissions on qualifying purchases, and what that does not change.',
      main: '<h1>How GateFixture Earns Money</h1>'
        + `<p>${esc(this.ctx.disclosure.earnings)} That never changes which product a guide recommends.</p>`
        + '<p>This site does not track prices over time and does not send price alerts.</p>',
      amazonContent: false,
      breadcrumbs: [{ name: 'Disclosure' }],
    });
  }

  notFound(path: string): string {
    return this.page({
      path,
      title: 'Page Not Found',
      description: 'The page you asked for does not exist on this site.',
      main: '<h1>Page Not Found</h1><p>Start again from the <a href="/">home page</a>.</p>',
      amazonContent: false,
      indexing: noindexPage('not found'),
    });
  }

  serverError(path: string): string {
    return this.page({
      path,
      title: 'Something Went Wrong',
      description: 'This page could not be rendered.',
      main: '<h1>Something Went Wrong</h1><p>Try again in a minute.</p>',
      amazonContent: false,
      indexing: noindexPage('server error'),
    });
  }
}

/** Every page the sitemaps list, each with the indexing decision its page renders with. */
export function fixtureSitemapSet(
  ctx: SiteContext,
  data: { products: readonly ProductRow[]; articles: readonly ArticleRow[]; bestLists: readonly BestListRow[]; notes: readonly NoteRow[]; recalls: readonly RecallRow[] },
): SitemapSet {
  const at = (d: Date | null | undefined): string | undefined => (d ? d.toISOString() : undefined);
  const hub: SitemapCandidate[] = ['/', '/guides', '/recalls', '/about', '/disclosure'].map((loc) => ({ loc, indexing: INDEXABLE_PAGE }));
  return buildSitemapSet(ctx, [
    { kind: 'hub', candidates: hub },
    { kind: 'products', sharded: true, candidates: data.products.map((p) => ({ loc: productPath(ctx, p.asin), indexing: INDEXABLE_PAGE })) },
    { kind: 'guides', candidates: data.articles.map((a) => ({ loc: `/guides/${a.slug}`, indexing: INDEXABLE_PAGE, lastmod: at(a.updatedAt) })) },
    { kind: 'best', candidates: data.bestLists.map((b) => ({ loc: `/best/${b.slug}`, indexing: INDEXABLE_PAGE, lastmod: at(b.updatedAt) })) },
    { kind: 'notes', candidates: data.notes.map((n) => ({ loc: `/notes/${n.slug}`, indexing: INDEXABLE_PAGE, lastmod: at(n.updatedAt) })) },
    { kind: 'recalls', allowEmpty: true, candidates: data.recalls.map((r) => ({ loc: `/recalls/${r.id}`, indexing: INDEXABLE_PAGE })) },
  ]);
}
