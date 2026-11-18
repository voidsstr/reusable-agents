import type { Page } from '@playwright/test';
import * as fs from 'fs';

/**
 * Text a reader cannot read, measured on the rendered page.
 *
 * Ported unchanged in method from SpecPicks tests/ui/lib/legibility.ts (the
 * kit's layout, sticky bar and price-stamp popover are what it measures here).
 *
 * For each viewport of the page, record every visible text run: its colour,
 * the opacity it is painted with, its size and weight, and its line boxes.
 * Then screenshot the viewport with all text made transparent. The pixels
 * under each line box are what a reader sees behind that text: the
 * container's colour, a gradient, an overlay. A run's contrast is the WCAG
 * ratio of its colour (blended at its opacity) over those pixels, taken at the
 * 10th percentile so the light end of a gradient counts. WCAG AA needs 4.5:1,
 * or 3:1 for large text (24px, or 18.66px bold). Input placeholders count as
 * text. Disabled controls are exempt, as WCAG allows.
 *
 * Text over a third-party picture is set aside: the gate answers those images
 * with a grey placeholder (pageAudit.ts), so the pixels behind such text are
 * not what a reader sees. Same-origin images and backgrounds load and count.
 *
 * Why: the homepage PC Builder banner's copy was #334155 on an indigo gradient
 * (about 1.5:1) until 2026-10-19. `.ssr-main p:not([class])` outranked the
 * banner's own paragraph colour, and no check measured colour at all.
 */

export interface Illegible {
  text: string;
  /** Element path of the text, for finding the CSS rule. */
  where: string;
  color: string;
  behind: string;
  ratio: number;
  need: number;
  /** Text runs on the page with the same element path and colour. */
  count: number;
}

// Hide every glyph for the background shot. The selector's three :not(#id) give it
// specificity (3,0,1): a site rule such as .x{color:#fff!important} outranks a bare
// *{...!important}, and its glyphs would then be scored as their own background.
// No ::first-letter: styling it at all makes Chromium build a first-letter box on every
// block, which re-wraps lines (+38px on /articles), so the shot would no longer match the
// measured boxes. The site styles neither ::first-letter nor ::first-line.
const NOT_ID = ':not(#__lg1):not(#__lg2):not(#__lg3)';
const HIDE_TEXT = ['', '::before', '::after', '::placeholder'].map((pe) => `html *${NOT_ID}${pe}`).join(',')
  + '{color:transparent!important;-webkit-text-fill-color:transparent!important;text-shadow:none!important;caret-color:transparent!important;text-decoration-color:transparent!important;}'
  + `html svg${NOT_ID} text,html svg${NOT_ID} tspan{fill:transparent!important;stroke:transparent!important;}`;
// Freeze motion. The site sets html{scroll-behavior:smooth}: a programmatic scroll would
// still be animating when the screenshot is taken, so line boxes measured at one scroll
// position would be scored against pixels from another.
const STILL = `*,*::before,*::after{transition:none!important;animation-duration:0s!important;animation-delay:0s!important;}`
  + `html${NOT_ID},html${NOT_ID} body{scroll-behavior:auto!important;}`;

type Box = { x: number; y: number; w: number; h: number };
interface Run { k: number; rects: Box[]; covers: Box[]; color: number[]; opacity: number; large: boolean; text: string; where: string }

/** In-page: the text runs fully inside this viewport that have not been measured yet. */
function collect(trusted: string[]): Run[] {
  const w = window as any;
  const seen: WeakSet<object> = (w.__legibilitySeen ||= new WeakSet());
  // Measured in this step; marked seen only once the step's screenshot is known to match (commit()).
  const pending: object[] = (w.__legibilityPending = []);
  const at: Array<{ pos: number[] | null; el: Element; c: number[] }> = (w.__legibilityPendingAt = []);
  const where = (o: Node | Element): number[] | null => {
    let b: DOMRect | undefined;
    if (o.nodeType === 3) { const rg = document.createRange(); rg.selectNodeContents(o); b = rg.getClientRects()[0]; } else b = (o as Element).getBoundingClientRect();
    return b ? [b.left, b.top] : null;
  };
  w.__legibilityWhere = where;
  const H = innerHeight;
  const runs: Run[] = [];
  const DECOR = /^[\p{Extended_Pictographic}\p{Emoji_Component}\s‍️·•|—–\-:,.()+&/]+$/u;
  const rgba = (s: string): number[] | null => {
    const m = /rgba?\(([^)]+)\)/.exec(s || '');
    if (!m) return null;
    const p = m[1].split(/[ ,/]+/).filter(Boolean).map(Number);
    return [p[0], p[1], p[2], p.length > 3 ? p[3] : 1];
  };
  const pathOf = (el: Element) => {
    const parts: string[] = [];
    for (let e: Element | null = el, i = 0; e && e !== document.body && i < 4; i++, e = e.parentElement) {
      const c = (typeof (e as HTMLElement).className === 'string' ? (e as HTMLElement).className : '').trim().split(/\s+/).filter(Boolean).slice(0, 2).join('.');
      parts.unshift(e.tagName.toLowerCase() + (c ? `.${c}` : ''));
    }
    return parts.join(' > ');
  };
  const opacityOf = (el: Element) => {
    let o = 1;
    for (let e: Element | null = el; e; e = e.parentElement) o *= parseFloat(getComputedStyle(e).opacity);
    return o;
  };
  const thirdParty = (u: string) => {
    if (!u || u.startsWith('data:') || u.startsWith('blob:')) return false;
    try { return !trusted.includes(new URL(u, location.href).host); } catch { return false; }
  };
  // A picture the gate replaced with a placeholder sits behind the text, with no opaque box in between.
  const overPlaceholder = (el: Element, x: number, y: number) => {
    for (const e of document.elementsFromPoint(x, y)) {
      if (e !== el && el.contains(e)) continue;
      const tag = e.tagName.toLowerCase();
      if (tag === 'img' && thirdParty((e as HTMLImageElement).currentSrc || (e as HTMLImageElement).src)) return true;
      if (tag === 'video' || tag === 'canvas' || tag === 'iframe') return true;
      const cs = getComputedStyle(e);
      for (const m of cs.backgroundImage.matchAll(/url\("?([^")]+)"?\)/g)) if (thirdParty(m[1])) return true;
      const bg = rgba(cs.backgroundColor);
      if (bg && bg[3] >= 0.95) return false;
    }
    return false;
  };
  const shown = (el: Element, x: number, y: number) => {
    const t = document.elementFromPoint(x, y);
    return !!t && (t === el || el.contains(t) || t.contains(el));
  };
  w.__legibilityShown = shown;
  const add = (el: Element, rects: Run['rects'], color: number[], text: string) => {
    const cs = getComputedStyle(el);
    const px = parseFloat(cs.fontSize);
    const wt = parseInt(cs.fontWeight, 10) || 400;
    runs.push({ k: runs.length, rects, covers: coversOf(el, rects), color, opacity: opacityOf(el), large: px >= 24 || (px >= 18.66 && wt >= 700), text: text.replace(/\s+/g, ' ').trim().slice(0, 80), where: pathOf(el) });
  };
  // Fixed and sticky boxes (navbar, chat launcher, sticky CTA bar, sticky TOC) paint over
  // whatever scrolls under them. A line can pass the five-point check yet have its edge
  // under one; those pixels are not its background, so score() leaves them out.
  const overlays: Array<{ el: Element; b: Box }> = [];
  // A box paints if it has an opaque-ish background; a transparent fixed wrapper (a
  // full-screen toast layer, a navbar whose white is on an inner div) paints only where
  // its direct children do.
  const paints = (e: Element) => { const c = getComputedStyle(e); const bg = rgba(c.backgroundColor); return (!!bg && bg[3] >= 0.5) || c.backgroundImage !== 'none'; };
  // An overlay's box-shadow paints past its box (a floating launcher's soft shadow); that
  // shading is not the text's background either.
  const shadowOf = (e: Element) => {
    const bs = getComputedStyle(e).boxShadow;
    if (!bs || bs === 'none') return 0;
    let ext = 0;
    for (const sh of bs.split(/,(?![^(]*\))/)) {
      const n = (sh.match(/-?[\d.]+px/g) || []).map(parseFloat);
      if (n.length >= 3) ext = Math.max(ext, Math.max(Math.abs(n[0]), Math.abs(n[1])) + n[2] + (n[3] || 0));
    }
    return Math.min(ext, 40);
  };
  const push = (o: Element, e: Element) => {
    const r = e.getBoundingClientRect(), m = shadowOf(e);
    if (r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < H) overlays.push({ el: o, b: { x: r.left - m, y: r.top - m, w: r.width + 2 * m, h: r.height + 2 * m } });
  };
  for (const e of document.querySelectorAll('body *')) {
    const cs = getComputedStyle(e);
    if ((cs.position !== 'fixed' && cs.position !== 'sticky') || cs.display === 'none' || cs.visibility !== 'visible' || parseFloat(cs.opacity) < 0.1) continue;
    if (paints(e)) push(e, e);
    else for (const c of e.children) if (paints(c)) push(e, c);
  }
  const coversOf = (el: Element, rects: Box[]) => overlays
    .filter((o) => !o.el.contains(el) && rects.some((r) => r.x < o.b.x + o.b.w && o.b.x < r.x + r.w && r.y < o.b.y + o.b.h && o.b.y < r.y + r.h))
    .map((o) => o.b);
  const disabled = (el: Element) => !!el.closest('button:disabled,input:disabled,select:disabled,textarea:disabled,fieldset:disabled,[aria-disabled="true"]');

  const tw = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let n = tw.nextNode(); n; n = tw.nextNode()) {
    if (seen.has(n)) continue;
    const t = n.textContent || '';
    const el = n.parentElement;
    if (!t.trim() || DECOR.test(t) || !el || el.closest('script,style,noscript,template,svg,title')) { seen.add(n); continue; }
    const cs = getComputedStyle(el);
    if (cs.visibility !== 'visible' || disabled(el)) { seen.add(n); continue; }
    const range = document.createRange();
    range.selectNodeContents(n);
    const rects = [...range.getClientRects()].filter((b) => b.width >= 2 && b.height >= 6).map((b) => ({ x: b.left, y: b.top, w: b.width, h: b.height }));
    if (!rects.length) { seen.add(n); continue; }
    if (rects.some((b) => b.y < 0 || b.y + b.h > H)) continue;      // not fully in this viewport: a later one
    // Uncovered at five points inside the line box, not just its centre: a line whose top
    // edge is under the fixed navbar would be scored against the navbar's icons.
    const vis = rects.filter((b) => [[0.5, 0.5], [0.1, 0.25], [0.9, 0.25], [0.1, 0.75], [0.9, 0.75]]
      .every(([fx, fy]) => shown(el, b.x + b.w * fx, b.y + b.h * fy)));
    if (!vis.length) continue;                                     // covered (sticky header) or clipped: retry later
    pending.push(n);
    at.push({ pos: where(n), el, c: [vis[0].x + vis[0].w / 2, vis[0].y + vis[0].h / 2] });
    if (opacityOf(el) < 0.1) continue;
    let color = rgba(cs.color);
    const fill = rgba(cs.webkitTextFillColor);
    if (fill && color && fill.join() !== color.join()) color = fill;
    if (!color || color[3] === 0) continue;                        // gradient text (background-clip: text)
    if (overPlaceholder(el, vis[0].x + vis[0].w / 2, vis[0].y + vis[0].h / 2)) continue;
    add(el, vis, color, t);
  }
  for (const el of document.querySelectorAll<HTMLInputElement | HTMLTextAreaElement>('input[placeholder],textarea[placeholder]')) {
    if (seen.has(el)) continue;
    const b = el.getBoundingClientRect();
    if (b.width < 20 || b.height < 10 || b.top < 0 || b.bottom > H) continue;
    if (el.value || disabled(el)) { seen.add(el); continue; }
    // Hidden right now (the search field inside a closed phone menu): not measured, and not
    // pending either, or the post-screenshot check would reject the step for it every time.
    if (!shown(el, b.left + b.width / 2, b.top + b.height / 2)) continue;
    pending.push(el);
    at.push({ pos: where(el), el, c: [b.left + b.width / 2, b.top + b.height / 2] });
    const color = rgba(getComputedStyle(el, '::placeholder').color);
    if (!color || color[3] === 0) continue;
    const pad = parseFloat(getComputedStyle(el).paddingLeft) || 8;
    add(el, [{ x: b.left + pad, y: b.top + b.height * 0.3, w: Math.max(4, Math.min(b.width - 2 * pad, 220)), h: b.height * 0.4 }], color, `[placeholder] ${el.placeholder}`);
  }
  return runs;
}

/** In-page: did the page scroll, or any measured box move or get covered, since collect()? */
function moved(y0: number): boolean {
  const w = window as any;
  if (window.scrollY !== y0) return true;
  return (w.__legibilityPending as Node[]).some((n, i) => {
    const was = w.__legibilityPendingAt[i], now = w.__legibilityWhere(n);
    if (!was.pos || !now || Math.abs(was.pos[0] - now[0]) > 1 || Math.abs(was.pos[1] - now[1]) > 1) return true;
    // An overlay that slid in after the scroll (a sticky bar, a header that reappears).
    return !w.__legibilityShown(was.el, was.c[0], was.c[1]);
  });
}

/** In-page: score each run against the text-free screenshot of the same viewport. */
async function score(arg: { png: string; runs: Run[] }): Promise<Array<{ k: number; ratio: number; behind: number[] }>> {
  const img = new Image();
  img.src = `data:image/png;base64,${arg.png}`;
  await img.decode();
  const c = document.createElement('canvas');
  c.width = img.width;
  c.height = img.height;
  const g = c.getContext('2d', { willReadFrequently: true })!;
  g.drawImage(img, 0, 0);
  const dpr = img.width / innerWidth;
  const lin = (v: number) => { v /= 255; return v <= 0.04045 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
  const lum = (p: number[]) => 0.2126 * lin(p[0]) + 0.7152 * lin(p[1]) + 0.0722 * lin(p[2]);
  const ratio = (a: number[], b: number[]) => { const x = lum(a), y = lum(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); };
  const out: Array<{ k: number; ratio: number; behind: number[] }> = [];
  for (const run of arg.runs) {
    const rs: number[] = [];
    const sum = [0, 0, 0];
    let planned = 0;
    for (const r of run.rects) {
      const x0 = Math.max(0, Math.floor(r.x * dpr)), y0 = Math.max(0, Math.floor(r.y * dpr));
      const x1 = Math.min(c.width, Math.ceil((r.x + r.w) * dpr)), y1 = Math.min(c.height, Math.ceil((r.y + r.h) * dpr));
      const w = x1 - x0, h = y1 - y0;
      if (w < 2 || h < 2) continue;
      const d = g.getImageData(x0, y0, w, h).data;
      const sx = Math.max(1, Math.floor(w / 48)), sy = Math.max(1, Math.floor(h / 6));
      // The middle band of the line box: its edges touch neighbouring boxes.
      for (let y = Math.floor(h * 0.15); y < Math.ceil(h * 0.85); y += sy) {
        for (let x = 0; x < w; x += sx) {
          planned++;
          const px = (x0 + x) / dpr, py = (y0 + y) / dpr;
          if (run.covers.some((o) => px >= o.x && px < o.x + o.w && py >= o.y && py < o.y + o.h)) continue;
          const i = (y * w + x) * 4;
          const bg = [d[i], d[i + 1], d[i + 2]];
          const a = run.color[3] * run.opacity;
          rs.push(ratio([0, 1, 2].map((j) => run.color[j] * a + bg[j] * (1 - a)), bg));
          for (let j = 0; j < 3; j++) sum[j] += bg[j];
        }
      }
    }
    if (!rs.length || rs.length < planned * 0.5) continue;      // mostly under an overlay: not measurable here
    rs.sort((p, q) => p - q);
    out.push({ k: run.k, ratio: rs[Math.floor(rs.length * 0.1)], behind: sum.map((s) => Math.round(s / rs.length)) });
  }
  return out;
}

/**
 * Every text run on the page below WCAG AA contrast, worst first, one entry per
 * element path and colour. Scrolls the page viewport by viewport (at most
 * `maxSteps`), so lazy content below the fold is measured as a reader meets it.
 */
export async function illegibleText(page: Page, opts: { trustedHosts: string[]; maxSteps?: number }): Promise<Illegible[]> {
  const found = new Map<string, Illegible>();
  await page.emulateMedia({ reducedMotion: 'reduce' }).catch(() => undefined);
  const still = await page.addStyleTag({ content: STILL });
  const vh = page.viewportSize()?.height ?? 900;
  const stride = vh - Math.max(160, Math.round(vh / 4));
  try {
    let retries = 0;
    for (let step = 0, y = 0; step < (opts.maxSteps ?? 30); step++) {
      const at = await page.evaluate(async (yy) => {
        window.scrollTo({ top: yy, behavior: 'instant' as ScrollBehavior });
        // Lazy images entering the viewport load now; until they do, content below them moves.
        const loading = [...document.images].filter((i) => { const b = i.getBoundingClientRect(); return !i.complete && b.bottom > -300 && b.top < innerHeight + 300; });
        await Promise.race([
          Promise.all(loading.map((i) => new Promise((r) => { i.addEventListener('load', r, { once: true }); i.addEventListener('error', r, { once: true }); }))),
          new Promise((r) => setTimeout(r, 2000)),
        ]);
        await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(() => r(null))));
        return window.scrollY;
      }, y);
      const runs = await page.evaluate(collect, opts.trustedHosts);
      if (runs.length) {
        const hide = await page.addStyleTag({ content: HIDE_TEXT });
        // Hiding the text must not move anything; if it does, the shot cannot be trusted.
        const shifted = await page.evaluate(moved, at);
        const png = (await page.screenshot({ type: 'png', caret: 'hide' })).toString('base64');
        await hide.evaluate((s) => (s as Element).remove());
        // A box that moved, or was covered, between measuring and the screenshot (a late
        // image, an async rail, a sticky bar sliding in) would be scored against someone
        // else's pixels: measure the step again.
        if (shifted || await page.evaluate(moved, at)) {
          if (++retries <= 3) { step--; continue; }
          retries = 0;                                                   // still moving (an animation): leave this step unmeasured
          y = at + stride;
          continue;
        }
        retries = 0;
        await page.evaluate(() => {
          const w = window as any;
          for (const n of w.__legibilityPending || []) w.__legibilitySeen.add(n);
          w.__legibilityPending = [];
        });
        for (const s of await page.evaluate(score, { png, runs })) {
          const run = runs[s.k];
          const need = run.large ? 3 : 4.5;
          if (s.ratio >= need) continue;
          // GATE_LEGIBILITY_DEBUG_DIR: keep each failing run's text-free screenshot and boxes.
          const dbg = process.env.GATE_LEGIBILITY_DEBUG_DIR;
          if (dbg) {
            fs.mkdirSync(dbg, { recursive: true });
            const base = `${dbg}/${Date.now()}-${s.k}`;
            fs.writeFileSync(`${base}.png`, Buffer.from(png, 'base64'));
            fs.writeFileSync(`${base}.json`, JSON.stringify({ url: page.url(), viewport: page.viewportSize(), scrollY: at, run, ratio: s.ratio, behind: s.behind }, null, 1));
          }
          const color = `rgba(${run.color.join(',')})`;
          const key = `${run.where}|${color}`;
          const prev = found.get(key);
          if (prev) { prev.count++; if (s.ratio < prev.ratio) Object.assign(prev, { ratio: +s.ratio.toFixed(2), text: run.text, behind: `rgb(${s.behind.join(',')})` }); continue; }
          found.set(key, { text: run.text, where: run.where, color, behind: `rgb(${s.behind.join(',')})`, ratio: +s.ratio.toFixed(2), need, count: 1 });
        }
      }
      const height = await page.evaluate(() => (document.scrollingElement || document.documentElement).scrollHeight);
      if (at + vh >= height || (step > 0 && at < y)) break;              // bottom reached, or the page will not scroll further
      y = at + stride;                                                   // overlap: a line cut by the fold, or under a bottom bar, is whole in the next step
    }
  } finally {
    await still.evaluate((s) => (s as Element).remove()).catch(() => undefined);
  }
  return [...found.values()].sort((a, b) => a.ratio - b.ratio);
}
