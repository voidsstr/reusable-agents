import type { BrowserContext, Page } from '@playwright/test';
import type { ComplianceAnchor, ComplianceForm, ComplianceSnapshot, ComplianceStamp } from './compliance';
import { BASE_URL } from './site';

/**
 * Takes a ComplianceSnapshot in the browser: what a reader with JS off sees
 * (the crawler's first pass, and the page an assistant quotes), plus the
 * markup the licence rules are about. compliance.ts judges it: the collector
 * gathers facts (texts, links, stamps), never verdicts, so every rule can be
 * proven in tests/unit without a browser.
 */

export interface CollectComplianceArgs {
  recallSelector: string;
  /**
   * Hosts that mean "this site" (the live hosts and the server under test):
   * a /r/<ASIN> link on any of them is a buy button. The kit's Markdown writes
   * /r/ links absolute on the canonical origin.
   */
  siteHosts: string[];
  /** site.json disclosure.earnings: the collector finds where it first shows. */
  earnings: string;
}

/** What the page itself yields; the response facts (status, headers, time) come from Node. */
export type InPageCompliance = Omit<ComplianceSnapshot, 'path' | 'status' | 'cacheControl' | 'cdnCacheControl' | 'surrogateControl' | 'takenAt'>;

/** Runs in the page; self-contained because Playwright serializes it. */
export function collectCompliance(arg: CollectComplianceArgs): InPageCompliance {
  const SKIP = 'script,style,noscript,template,textarea,pre,code,kbd,samp';
  const collapse = (s: string | null | undefined): string => String(s ?? '').replace(/\s+/g, ' ').trim();
  const isVisible = (el: Element): boolean => {
    const anyEl = el as Element & { checkVisibility?: (o: object) => boolean };
    if (typeof anyEl.checkVisibility === 'function') return anyEl.checkVisibility({ visibilityProperty: true });
    const r = el.getBoundingClientRect();
    return r.width > 0 || r.height > 0;
  };
  // Text behind a closed <details> counts as shown when the <details> shows: one click opens it.
  const isRendered = (el: Element): boolean => {
    const closed = el.closest('details:not([open])');
    if (closed && !el.closest('summary')) return isVisible(closed);
    return isVisible(el);
  };
  const AMAZON_HOST = /(^|\.)amazon\.[a-z.]+$|(^|\.)(amzn\.(to|com|eu|asia)|a\.co)$/i;
  // A GFM delimiter row anywhere in the text, or a whole line that is a row of 3+ pipe-separated cells.
  const RAW_TABLE_DELIM = /\|\s*:?-{3,}:?\s*\|/;
  const RAW_TABLE_ROW = /^\s*\|(?:[^|\n]+\|){2,}\s*$/m;
  const STAMP = '[data-price-stamp]';
  const NOT_A_PRICE = '[data-kit-not-a-price]';
  const siteHosts = new Set(arg.siteHosts.map((h) => h.toLowerCase()));
  siteHosts.add(location.host.toLowerCase());

  // ── head ──
  const metaTexts = (sel: string): string[] => Array.from(document.querySelectorAll(sel))
    .map((m) => (m as HTMLMetaElement).content || '')
    .filter(Boolean);
  const headTexts: Array<{ where: string; text: string }> = [];
  if (document.title) headTexts.push({ where: '<title>', text: document.title });
  for (const [where, sel] of [
    ['meta description', 'meta[name="description"]'],
    ['og:title', 'meta[property="og:title"]'],
    ['og:description', 'meta[property="og:description"]'],
    ['og:image:alt', 'meta[property="og:image:alt"]'],
    ['twitter:title', 'meta[name="twitter:title"],meta[property="twitter:title"]'],
    ['twitter:description', 'meta[name="twitter:description"],meta[property="twitter:description"]'],
    ['twitter:image:alt', 'meta[name="twitter:image:alt"],meta[property="twitter:image:alt"]'],
  ] as const) {
    for (const text of metaTexts(sel)) headTexts.push({ where, text });
  }
  const priceMetas = Array.from(document.querySelectorAll(
    'meta[property^="product:price"],meta[property^="product:sale_price"],meta[property^="product:original_price"],meta[property^="og:price"]',
  )).map((m) => ({ prop: m.getAttribute('property') || '', content: (m as HTMLMetaElement).content || '' }));

  // ── affiliate links ──
  const anchors: ComplianceAnchor[] = [];
  const affiliateEls: HTMLAnchorElement[] = [];
  for (const a of Array.from(document.querySelectorAll<HTMLAnchorElement>('a[href]'))) {
    let u: URL;
    try { u = new URL(a.getAttribute('href') || '', location.href); } catch { continue; }
    const amazon = /^https?:$/.test(u.protocol) && AMAZON_HOST.test(u.hostname);
    const redirect = /^https?:$/.test(u.protocol) && siteHosts.has(u.host.toLowerCase()) && /^\/r\/[A-Za-z0-9]{10}\/?$/.test(u.pathname);
    if (!amazon && !redirect) continue;
    const placed = a.closest('[data-cta-placement]');
    affiliateEls.push(a);
    anchors.push({
      href: a.getAttribute('href') || '',
      abs: u.toString(),
      rel: a.getAttribute('rel') || '',
      target: (a.getAttribute('target') || '').toLowerCase(),
      text: collapse(a.textContent).slice(0, 80),
      visible: isVisible(a),
      placement: placed ? placed.getAttribute('data-cta-placement') || '' : '',
    });
  }

  // ── stamps ──
  const textOfId = (id: string, mustShow: boolean): string => {
    const el = id ? document.getElementById(id) : null;
    if (!el || (mustShow && !isVisible(el))) return '';
    return collapse(el.textContent);
  };
  // A control labelled "Details" (2(i)'s disclosure); the same pattern as compliance.ts DETAILS_LABEL_RE.
  const DETAILS_LABEL = /^[^\p{L}\p{N}]*details[^\p{L}\p{N}]*$/iu;
  const labelOf = (c: Element): string => collapse(c.getAttribute('aria-label') || c.textContent);
  const stamps: ComplianceStamp[] = Array.from(document.querySelectorAll(STAMP)).map((s) => {
    const linked: string[] = [];
    const details: string[] = [];
    // Only a control a reader can see and use reveals anything.
    for (const c of Array.from(s.querySelectorAll('[popovertarget],[aria-controls],[aria-describedby],a[href^="#"]'))) {
      if (!isVisible(c)) continue;
      const revealed: string[] = [];
      for (const attr of ['popovertarget', 'aria-controls']) {
        for (const id of (c.getAttribute(attr) || '').split(/\s+/)) if (id) revealed.push(textOfId(id, false));
      }
      // A description is only read where it shows; it is not a disclosure control.
      for (const id of (c.getAttribute('aria-describedby') || '').split(/\s+/)) if (id) linked.push(textOfId(id, true));
      const href = c.tagName === 'A' ? c.getAttribute('href') || '' : '';
      if (href.length > 1) {
        let id = href.slice(1);
        try { id = decodeURIComponent(id); } catch { /* keep it raw */ }
        revealed.push(textOfId(id, false));
      }
      linked.push(...revealed);
      if (DETAILS_LABEL.test(labelOf(c))) details.push(...revealed);
    }
    for (const d of Array.from(s.querySelectorAll('details'))) {
      const summary = d.querySelector(':scope > summary');
      if (!summary || !isVisible(summary)) continue;
      const body = d.cloneNode(true) as Element;
      body.querySelector(':scope > summary')?.remove();
      linked.push(collapse(body.textContent));
      if (DETAILS_LABEL.test(labelOf(summary))) details.push(collapse(body.textContent));
    }
    return {
      kind: s.getAttribute('data-price-stamp') || '',
      text: collapse(s.textContent),
      visibleText: collapse((s as HTMLElement).innerText ?? s.textContent),
      times: Array.from(s.querySelectorAll('time[datetime]')).map((t) => t.getAttribute('datetime') || ''),
      linkedTexts: linked.filter(Boolean),
      detailsTexts: details.filter(Boolean),
      priceText: collapse(s.querySelector('.ssr-price')?.textContent),
      insideAnchor: !!s.closest('a'),
    };
  });

  // ── what a reader sees outside every stamp, block by block ──
  const displayOf = new Map<Element, string>();
  const isBlock = (el: Element): boolean => {
    let d = displayOf.get(el);
    if (d === undefined) {
      d = getComputedStyle(el).display || 'inline';
      displayOf.set(el, d);
    }
    return !d.startsWith('inline') && d !== 'contents' && d !== 'none';
  };
  const blockOf = (el: Element): Element => {
    for (let cur: Element | null = el; cur && cur !== document.body; cur = cur.parentElement) if (isBlock(cur)) return cur;
    return document.body;
  };
  // Outside every stamp: what a reader sees, what only crawlers and assistants
  // read (in the HTML, not rendered), and what is marked as not a price.
  const blocks = new Map<Element, string[]>();
  const hiddenBlocks = new Map<Element, string[]>();
  const notAPriceBlocks = new Map<Element, string[]>();
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    const t = n.nodeValue || '';
    if (!t.trim()) continue;
    const el = n.parentElement;
    if (!el || el.closest(SKIP) || el.closest(STAMP)) continue;
    const rendered = isRendered(el);
    const into = el.closest(NOT_A_PRICE) ? (rendered ? notAPriceBlocks : null) : rendered ? blocks : hiddenBlocks;
    if (!into) continue;
    const b = blockOf(el);
    const parts = into.get(b);
    if (parts) parts.push(t);
    else if (into.size < 4000) into.set(b, [t]);
  }
  const texts = (m: Map<Element, string[]>): string[] => [...m.values()].map((parts) => collapse(parts.join('')).slice(0, 1000)).filter(Boolean);
  const unstampedText = texts(blocks);
  const hiddenUnstampedText = texts(hiddenBlocks);
  const notAPriceText = texts(notAPriceBlocks);
  // What assistive technology announces (and a hover shows) outside every stamp.
  const attributeTexts: Array<{ where: string; text: string }> = [];
  for (const el of Array.from(document.body.querySelectorAll('[aria-label],[title],img[alt],input[type="image"][alt],area[alt]'))) {
    if (attributeTexts.length >= 2000) break;
    if (el.closest(SKIP) || el.closest(STAMP) || el.closest(NOT_A_PRICE)) continue;
    for (const attr of ['aria-label', 'title', 'alt']) {
      const v = collapse(el.getAttribute(attr));
      if (v) attributeTexts.push({ where: `${attr} of <${el.tagName.toLowerCase()}>`, text: v.slice(0, 300) });
    }
  }
  // The kit's price markup belongs in a stamp, data-kit-not-a-price or not.
  const strayPriceElements = Array.from(document.querySelectorAll('.ssr-price'))
    .filter((p) => !p.closest(STAMP))
    .map((p) => collapse(p.textContent));

  const noticeElements = Array.from(document.querySelectorAll('[data-amazon-notice="2k"]')).map((n) => ({
    text: collapse(n.textContent),
    visible: isVisible(n),
  }));

  const socialImages: Array<{ prop: string; url: string }> = [];
  for (const prop of ['og:image', 'og:image:url', 'og:image:secure_url']) {
    for (const m of Array.from(document.querySelectorAll(`meta[property="${prop}"]`))) socialImages.push({ prop, url: (m as HTMLMetaElement).content });
  }
  for (const m of Array.from(document.querySelectorAll('meta[name="twitter:image"],meta[property="twitter:image"]'))) {
    socialImages.push({ prop: 'twitter:image', url: (m as HTMLMetaElement).content });
  }

  const forms: ComplianceForm[] = Array.from(document.querySelectorAll('form')).map((f) => ({
    action: f.getAttribute('action') || '',
    text: collapse(f.textContent).slice(0, 200),
    fields: Array.from(f.querySelectorAll('input,select,textarea,button')).flatMap((i) =>
      ['name', 'id', 'placeholder', 'aria-label', 'value'].map((k) => i.getAttribute(k) || '').filter(Boolean)),
  }));
  // Controls outside a form (a script-driven alert widget).
  for (const b of Array.from(document.querySelectorAll('button,input[type="email"],input[type="button"]'))) {
    if (b.closest('form') || b.closest(STAMP)) continue;
    forms.push({ action: '', text: collapse(b.textContent), fields: ['name', 'id', 'placeholder', 'aria-label'].map((k) => b.getAttribute(k) || '').filter(Boolean) });
  }

  const mdTables = Array.from(document.querySelectorAll('table.md-table')).map((t) => {
    const head = Array.from(t.querySelectorAll('thead th'));
    const rows = Array.from(t.querySelectorAll('tbody tr'));
    return {
      wrapped: !!t.parentElement?.classList.contains('md-table-wrap'),
      headerCells: head.length,
      scopedHeaderCells: head.filter((h) => h.getAttribute('scope') === 'col').length,
      bodyRows: rows.length,
      rowCells: rows.map((r) => r.querySelectorAll('td,th').length),
    };
  });

  const rawTableText: string[] = [];
  for (const el of Array.from(document.querySelectorAll('p,li,div,span,td,blockquote'))) {
    if (el.closest(SKIP) || !isRendered(el)) continue;
    // Only the element's own lines: a <div> holding a <p> would repeat the <p>'s text.
    const own = Array.from(el.childNodes).filter((c) => c.nodeType === 3).map((c) => c.nodeValue || '').join('');
    if (RAW_TABLE_DELIM.test(own) || RAW_TABLE_ROW.test(own)) rawTableText.push(collapse(own).slice(0, 200));
  }

  // Line breaks kept: they are the block boundaries the phrase rules read sentences by.
  const visibleText = (document.body?.innerText || '').replace(/[ \t ]+/g, ' ').replace(/ *\n[\s]*/g, '\n').trim();

  // ── the earnings disclosure against the first affiliate link a reader sees ──
  // A marker put into that link shows where it falls in the rendered text;
  // last, because it changes the DOM (the page is closed right after).
  let earningsBeforeFirstLink: boolean | null = null;
  const want = collapse(arg.earnings).toLowerCase();
  const first = affiliateEls.find((a) => isVisible(a));
  if (first && want) {
    const MARK = '⁣KITGATELINKMARK⁣';
    const mark = document.createTextNode(MARK);
    first.insertBefore(mark, first.firstChild);
    const rendered = collapse(document.body?.innerText || '').toLowerCase();
    mark.remove();
    const linkAt = rendered.indexOf(MARK.toLowerCase());
    const disclosureAt = rendered.indexOf(want);
    if (linkAt >= 0 && disclosureAt >= 0) earningsBeforeFirstLink = disclosureAt < linkAt;
  }

  return {
    headTexts,
    priceMetas,
    anchors,
    stamps,
    unstampedText,
    hiddenUnstampedText,
    attributeTexts,
    notAPriceText,
    strayPriceElements,
    noticeElements,
    visibleText,
    earningsBeforeFirstLink,
    amazonMarkers: document.querySelectorAll('[data-amazon-content]').length,
    jsonld: Array.from(document.querySelectorAll('script[type="application/ld+json"]')).map((s) => s.textContent || ''),
    socialImages,
    heroImages: Array.from(document.querySelectorAll('img[data-hero-image]')).map((i) => (i as HTMLImageElement).currentSrc || i.getAttribute('src') || ''),
    forms,
    mdTables,
    rawTableText: rawTableText.slice(0, 10),
    recallNotices: arg.recallSelector ? Array.from(document.querySelectorAll(arg.recallSelector)).filter(isVisible).length : 0,
  };
}

/** The response facts a snapshot carries beside what the page yields. */
export interface ResponseFacts {
  path: string;
  status: number;
  /** Response headers (lower-cased names, as Playwright gives them). */
  headers: Record<string, string>;
  /** When the page was read. */
  takenAt: number;
}

/** A snapshot from what the page yielded and its response. */
export function snapshotOf(inPage: InPageCompliance, r: ResponseFacts): ComplianceSnapshot {
  return {
    path: r.path,
    status: r.status,
    takenAt: r.takenAt,
    cacheControl: r.headers['cache-control'] ?? '',
    cdnCacheControl: r.headers['cdn-cache-control'] ?? '',
    surrogateControl: r.headers['surrogate-control'] ?? '',
    ...inPage,
  };
}

/** Take the snapshot of a page that is already loaded. */
export async function collectFrom(page: Page, args: CollectComplianceArgs, r: Omit<ResponseFacts, 'takenAt'>): Promise<ComplianceSnapshot> {
  const takenAt = Date.now();
  return snapshotOf(await page.evaluate(collectCompliance, args), { ...r, takenAt });
}

/** Load `path` with JS off in `ctx` and take its compliance snapshot. */
export async function complianceSnapshot(ctx: BrowserContext, path: string, args: CollectComplianceArgs): Promise<ComplianceSnapshot> {
  const page = await ctx.newPage();
  try {
    const resp = await page.goto(`${BASE_URL}${path}`, { waitUntil: 'domcontentloaded', timeout: 45_000 });
    return await collectFrom(page, args, { path, status: resp?.status() ?? 0, headers: resp?.headers() ?? {} });
  } finally {
    await page.close();
  }
}
