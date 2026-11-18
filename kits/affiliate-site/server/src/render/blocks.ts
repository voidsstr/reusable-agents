/**
 * Visible blocks that have a structured-data twin, built from the same input
 * so the page and its JSON-LD can never disagree:
 *   - breadcrumbNavHtml  <->  breadcrumbListLd  (one trail: breadcrumbTrail)
 *   - faqSectionHtml     <->  faqPageLd         (one row set: normalizeFaq)
 * plus amazonTextHtml, the marker for Amazon-sourced text that tells the
 * layout (and the compliance spec) the page shows Amazon content.
 *
 * breadcrumbNavHtml follows SpecPicks' breadcrumbHtml (ssrRender.ts: the last
 * crumb is the current page, aria-current, not a link) as an ordered list
 * with CSS separators instead of inline SVG.
 */
import { esc } from '../links';
import type { SiteContext } from '../siteContext';
import { breadcrumbTrail, normalizeFaq, type BreadcrumbItem, type BreadcrumbOptions, type FaqEntry } from './jsonld';

/**
 * Marks an element whose text came from Amazon (a cached title, feature or
 * description). The layout shows the 2(k) notice on any page that holds one,
 * and the compliance spec checks that pairing.
 */
export const AMAZON_CONTENT_ATTR = 'data-amazon-content';

/** The visible breadcrumb for a trail ('' when it has fewer than two crumbs). */
export function breadcrumbNavHtml(
  ctx: Pick<SiteContext, 'site'>,
  items: readonly BreadcrumbItem[],
  opts: BreadcrumbOptions = {},
): string {
  const trail = breadcrumbTrail(ctx, items, opts);
  if (trail.length < 2) return '';
  const lis = trail.map((c, k) => {
    if (k === trail.length - 1) return `<li><span class="kit-bc-current" aria-current="page">${esc(c.name)}</span></li>`;
    return c.url ? `<li><a href="${esc(c.url)}">${esc(c.name)}</a></li>` : `<li><span>${esc(c.name)}</span></li>`;
  });
  return `<nav class="kit-breadcrumb" aria-label="Breadcrumb"><ol>${lis.join('')}</ol></nav>`;
}

export interface FaqSectionOptions {
  /** Visible heading; null renders none (the section is then labelled by aria-label). */
  heading?: string | null;
  headingLevel?: 2 | 3;
  /** Section id (default 'faq'); the heading gets '<id>-h'. */
  id?: string;
}

/**
 * The visible FAQ: one <details> per question, answers as text. Feed
 * faqPageLd() the same rows. '' when there are no complete rows.
 */
export function faqSectionHtml(rows: readonly FaqEntry[] | null | undefined, opts: FaqSectionOptions = {}): string {
  const qs = normalizeFaq(rows);
  if (!qs.length) return '';
  const id = /^[A-Za-z][A-Za-z0-9_-]{0,40}$/.test(opts.id ?? '') ? (opts.id as string) : 'faq';
  const heading = opts.heading === undefined ? 'Frequently asked questions' : opts.heading;
  const level = opts.headingLevel ?? 2;
  const head = heading ? `<h${level} id="${id}-h">${esc(heading)}</h${level}>` : '';
  const label = heading ? ` aria-labelledby="${id}-h"` : ' aria-label="Frequently asked questions"';
  const items = qs.map((r) => `<details class="kit-faq-item"><summary>${esc(r.question)}</summary><p>${esc(r.answer)}</p></details>`);
  return `<section class="kit-faq" id="${id}"${label}>${head}${items.join('')}</section>`;
}

export type AmazonTextTag = 'span' | 'p' | 'div' | 'strong' | 'cite' | 'h1' | 'h2' | 'h3' | 'h4';

/**
 * Amazon-sourced text (e.g. a cached product title used as the H1 when we
 * have no model name of our own), escaped and marked with
 * data-amazon-content so the page carries the 2(k) notice.
 */
export function amazonTextHtml(text: string | null | undefined, opts: { tag?: AmazonTextTag; className?: string } = {}): string {
  const tag = opts.tag ?? 'span';
  const cls = String(opts.className ?? '').split(/\s+/).filter((c) => /^[A-Za-z0-9_-]+$/.test(c)).join(' ');
  return `<${tag}${cls ? ` class="${cls}"` : ''} ${AMAZON_CONTENT_ATTR}>${esc(text ?? '')}</${tag}>`;
}
