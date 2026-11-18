import { test, expect, type APIRequestContext, type BrowserContext, type Page, type TestInfo } from '@playwright/test';
import { illegibleText, type Illegible } from './lib/legibility';
import type { CheckId, GateConfig, PageFamily } from './lib/manifestTypes';
import { ctaProblems, routeRegex } from './lib/routes';
import {
  DESKTOP, PHONE, canonicalPath, internalPagePath, jsonLdProblems, loadAndSnapshot, newAuditContext, normalizePath, overflowCulprit,
  type Cta, type DomSnapshot, type ImgInfo, type LoadResult,
} from './lib/pageAudit';
import { BASE, BASE_URL, SEED, gateSite, isProdHost, isSiteHost, seededSample, sitemapFiles, sitemapPaths, targetsProduction } from './lib/site';
import { samplesFor } from './lib/sources';

/**
 * Every public page family, the way a reader sees it (the all-pages engine,
 * kit build item K5). Ported from SpecPicks tests/ui/all-pages.spec.ts; the
 * families, routes and fixture exceptions come from the site's gate config.
 *
 * For each sampled URL: JS off at phone width (the crawler's first pass and
 * the reader without JS), JS on at phone width (the reader with the kit's
 * client bundle: beacon and sticky bar), and JS on at desktop width for the
 * first URL of a family. The browser never reaches a third party or
 * production (lib/pageAudit.ts). Kit sites are SSR-only, so SpecPicks' React
 * checks (mount, React-first parity, the React not-found view) are gone; JS
 * must not change a page's H1, canonical, robots, CTAs or JSON-LD.
 *
 * SpecPicks added it on 2026-10-19, after a page showed its <head> CSS as
 * page text to every reader with JS for 3+ weeks while every check passed.
 *
 * Not @prod-safe: it renders a few hundred pages and resolves their links,
 * and its expectations are tuned to the fixture. It skips itself against
 * production.
 */

/** Internal links resolved per page, and per family test (deduplicated per worker). */
const LINKS_PER_PAGE = 5;
const LINKS_PER_TEST = 20;
/** Source text in a <title> or meta description: entities, template exhaust. */
const HEAD_EXHAUST = /&(?:amp|lt|gt|quot|apos|nbsp|#\d+|#x[0-9a-f]+|[a-z]+);|\bundefined\b|\bNaN\b|\[object Object\]|\{\{|\}\}|\$\{|__SSR_|<\/?[a-z][^>]*>/i;

// ── Link and image resolution (per worker, deduplicated) ────────────────
interface Resolved {
  /** '' when the link answers 200 (after internal redirects), else why not. */
  why: string;
  /** The path it ended on. */
  final: string;
}
const resolveCache = new Map<string, Promise<Resolved>>();

export function resolvePath(request: APIRequestContext, p: string): Promise<Resolved> {
  if (!resolveCache.has(p)) {
    resolveCache.set(p, (async (): Promise<Resolved> => {
      let current = p;
      for (let hop = 0; hop < 5; hop++) {
        let resp;
        try {
          resp = await request.get(`${BASE_URL}${current}`, { maxRedirects: 0, timeout: 45_000 });
        } catch (e) {
          return { why: `request failed: ${String((e as Error).message || e).slice(0, 120)}`, final: current };
        }
        const st = resp.status();
        if (st === 200) return { why: '', final: current };
        if (st >= 300 && st < 400) {
          const loc = resp.headers()['location'] || '';
          let next: URL;
          try { next = new URL(loc, `${BASE_URL}${current}`); } catch { return { why: `${st} to an invalid Location "${loc}"`, final: current }; }
          if (!isSiteHost(next.host)) return { why: '', final: current };
          current = next.pathname + next.search;
          continue;
        }
        return { why: `HTTP ${st}${current !== p ? ` (after redirects, at ${current})` : ''}`, final: current };
      }
      return { why: 'more than 5 redirects', final: current };
    })());
  }
  return resolveCache.get(p)!;
}

const imageCache = new Map<string, Promise<string>>();
function imageLoads(request: APIRequestContext, p: string): Promise<string> {
  if (!imageCache.has(p)) {
    imageCache.set(p, (async () => {
      try {
        const resp = await request.get(`${BASE_URL}${p}`, { timeout: 30_000 });
        if (resp.status() !== 200) return `HTTP ${resp.status()}`;
        const type = resp.headers()['content-type'] || '';
        return /^image\//.test(type) ? '' : `content-type ${type || 'none'}`;
      } catch (e) {
        return `request failed: ${String((e as Error).message || e).slice(0, 80)}`;
      }
    })());
  }
  return imageCache.get(p)!;
}

// ── Helpers ─────────────────────────────────────────────────────────────

function ctaKey(c: Cta): string {
  try {
    const u = new URL(c.href, BASE_URL);
    const asin = u.pathname.match(/\/(?:dp|gp\/product)\/([A-Z0-9]{10})/i)?.[1];
    if (c.kind === 'amazon') return asin ? `amazon:${asin}` : `amazon:${u.pathname}${u.searchParams.get('k') ? `?k=${u.searchParams.get('k')}` : ''}`;
    return `click:${u.pathname}`;
  } catch {
    return `${c.kind}:${c.href}`;
  }
}

/** Visible CTAs in the page's single <main>. */
function mainCtas(s: DomSnapshot): Cta[] {
  return s.ctas.filter((c) => c.visible && c.inMain);
}

function uniq<T>(xs: T[]): T[] {
  return [...new Set(xs)];
}

function typeCounts(blocks: string[]): Record<string, number> {
  return jsonLdProblems(blocks).types;
}

/**
 * Every <img> has a real src; ours load; third-party URLs are well formed
 * (never fetched). Run on both renders.
 */
async function imageProblems(images: ImgInfo[], request: APIRequestContext): Promise<string[]> {
  const out: string[] = [];
  const sameOrigin = new Set<string>();
  for (const img of images) {
    const raw = (img.src ?? '').trim();
    if (!raw && !img.srcset) { out.push('<img> without src'); continue; }
    if (/^(undefined|null|NaN)$|\/(undefined|null)(\/|$|\?)|\[object Object\]/.test(raw)) { out.push(`<img src="${raw}">`); continue; }
    if (!raw) continue;
    let u: URL;
    try { u = new URL(raw, BASE_URL); } catch { out.push(`unparseable src "${raw.slice(0, 80)}"`); continue; }
    if (u.protocol === 'data:') continue;
    if (u.host === BASE.host || isProdHost(u.host)) sameOrigin.add(u.pathname + u.search);
    else if (u.protocol !== 'https:' || !u.hostname.includes('.') || /\s/.test(raw)) out.push(`malformed third-party src "${raw.slice(0, 100)}"`);
  }
  for (const img of [...sameOrigin].slice(0, 6)) {
    const why = await imageLoads(request, img);
    if (why) out.push(`${img} does not load: ${why}`);
  }
  return out;
}

function phoneOverflow(r: LoadResult): string {
  const o = r.snap.overflow;
  if (o.scrollWidth <= PHONE.width + 1 && o.innerWidth <= PHONE.width + 1) return '';
  const culprit = r.culprit || o.offenders.join('; ');
  return `page is ${Math.max(o.scrollWidth, o.innerWidth)}px wide on a ${PHONE.width}px phone${culprit ? `; widest element: ${culprit}` : ''}`;
}

const diagnosePhone = async (page: Page, r: LoadResult): Promise<void> => {
  if (r.snap.overflow.scrollWidth > PHONE.width + 1 || r.snap.overflow.innerWidth > PHONE.width + 1) {
    r.culprit = await overflowCulprit(page, PHONE.width);
  }
};

// Text below WCAG AA contrast, measured on the rendered page with JS on. See lib/legibility.ts.
const illegibleByResult = new WeakMap<LoadResult, Illegible[]>();
const legibility = async (page: Page, r: LoadResult): Promise<void> => {
  illegibleByResult.set(r, await illegibleText(page, { trustedHosts: [...gateSite().siteHosts] }));
};
/** Illegible text runs reported per page view; GATE_LEGIBILITY_PER_PAGE raises it to list them all. */
const LEGIBILITY_PER_PAGE = Number(process.env.GATE_LEGIBILITY_PER_PAGE || 6);
const illegibleOf = (r: LoadResult): Illegible[] => illegibleByResult.get(r) ?? [];
const legibilityDetail = (t: Illegible): string =>
  `"${t.text}" (${t.where}): ${t.color} on ${t.behind} is ${t.ratio}:1, needs ${t.need}:1${t.count > 1 ? ` (${t.count} runs like it)` : ''}`;

interface Contexts {
  off: BrowserContext;
  on: BrowserContext;
  desk: BrowserContext | null;
}

/**
 * Audit one URL; every problem found is pushed as "<path> [<view>] <check>: <detail>".
 * Returns whether the page showed a visible affiliate CTA without JS.
 */
async function auditOne(
  cfg: GateConfig, fam: PageFamily, p: string, ctx: Contexts, request: APIRequestContext,
  problems: string[], notes: string[], linkBudget: { left: number },
): Promise<boolean> {
  const site = gateSite();
  const brand = site.ctx.site.brandName;
  const allowedTags = site.ctx.amazon.allowedTags;
  const applies = (check: CheckId): boolean => !fam.exceptions?.[check];
  const add = (check: CheckId, view: string, detail: string): void => {
    if (applies(check)) problems.push(`${p} [${view}] ${check}: ${detail}`);
  };
  const expectedStatus = fam.status ?? 200;

  // ── 1. JS off, phone ──────────────────────────────────────────────────
  const off = await loadAndSnapshot(ctx.off, p, { js: false, view: 'phone', diagnose: diagnosePhone });
  if (off.status !== expectedStatus) add('status', 'js-off', `HTTP ${off.status}, expected ${expectedStatus}`);
  if (off.status === 0 || off.status >= 500) return false;
  const s = off.snap;

  const title = s.title.trim();
  if (!title) add('title', 'js-off', 'empty <title>');
  else if (title === brand && !fam.siteNameTitle) add('title', 'js-off', `<title> is just "${brand}"`);
  if (HEAD_EXHAUST.test(title)) add('title', 'js-off', `<title> shows source text: "${title}"`);

  if (s.descriptions.length > 1) add('description', 'js-off', `${s.descriptions.length} meta descriptions`);
  const desc = (s.descriptions[0] || '').trim();
  if (fam.robots === 'index' && !desc) add('description', 'js-off', 'no meta description on an indexable page');
  if (desc && HEAD_EXHAUST.test(desc)) add('description', 'js-off', `meta description shows source text: "${desc.slice(0, 100)}"`);

  const expectedCanonical = normalizePath(fam.canonical ? fam.canonical.expect(p) : p);
  if (s.canonicals.length !== 1) add('canonical', 'js-off', `${s.canonicals.length} canonical links ${JSON.stringify(s.canonicals)}`);
  else {
    const cp = canonicalPath(s.canonicals[0] as string);
    if (!cp) add('canonical', 'js-off', `not an absolute ${site.prodOrigin} URL: ${s.canonicals[0]}`);
    else if (normalizePath(cp) !== expectedCanonical) add('canonical', 'js-off', `points at ${cp}, expected ${expectedCanonical}`);
  }

  const robots = uniq(s.robots.map((r) => r.trim().toLowerCase()));
  const noindex = robots.some((r) => /noindex/.test(r));
  if (robots.length > 1) add('robots', 'js-off', `conflicting robots metas ${JSON.stringify(robots)}`);
  if (fam.robots === 'index' && noindex) add('robots', 'js-off', `"${robots.join(' | ')}" on an indexable family`);
  if (fam.robots === 'noindex' && !noindex) add('robots', 'js-off', `indexable ("${robots.join(' | ') || 'no robots meta'}"), expected noindex`);

  if (s.visibleH1s.length !== 1) add('h1', 'js-off', `${s.visibleH1s.length} visible <h1> ${JSON.stringify(s.visibleH1s.slice(0, 3))}`);
  else if (!s.visibleH1s[0]) add('h1', 'js-off', 'empty <h1>');

  if (s.counts.main !== 1 || s.counts.mainId !== 1) add('structure', 'js-off', `<main>=${s.counts.main} #main=${s.counts.mainId}, expected one of each`);
  if (s.counts.ssrStyles > 1) add('structure', 'js-off', `${s.counts.ssrStyles} style#ssr-styles`);
  if (s.counts.ssrMain > 1) add('structure', 'js-off', `${s.counts.ssrMain} .ssr-main page bodies`);
  if (off.status === 200 && s.notFoundView) add('soft-404', 'js-off', `a 200 page that reads as not found ("${title}")`);

  for (const l of s.leaks.slice(0, 4)) add('visible-code', 'js-off', `${l.kind} in <${l.tag}${l.cls ? `.${l.cls}` : ''}>: "${l.text}"`);
  for (const pr of jsonLdProblems(s.jsonld).problems.slice(0, 4)) add('json-ld', 'js-off', pr);

  const monetized = mainCtas(s).length > 0;
  if (fam.monetized === true && !monetized) add('monetized', 'js-off', 'no visible affiliate CTA in <main>');
  for (const c of s.ctas) for (const m of ctaProblems(c, allowedTags)) add('cta-compliance', 'js-off', m);
  for (const pb of s.priceBugs.slice(0, 3)) add('price-text', 'js-off', `"${pb}"`);
  for (const b of s.badLinks.slice(0, 3)) add('bad-links', 'js-off', `link with href ${b}`);

  for (const d of await imageProblems(s.images, request)) add('images', 'js-off', d);
  const offOverflow = phoneOverflow(off);
  if (offOverflow) add('phone-overflow', 'js-off', offOverflow);

  // ── 2. JS on, phone ───────────────────────────────────────────────────
  const on = await loadAndSnapshot(ctx.on, p, {
    js: true, view: 'phone',
    diagnose: async (page, r) => {
      await diagnosePhone(page, r);
      if (applies('legibility')) await legibility(page, r);
    },
  });
  const n = on.snap;
  for (const e of uniq(on.errors).slice(0, 3)) add('page-errors', 'js-on', e);
  if (on.status !== off.status) add('status', 'js-on', `HTTP ${on.status} with JS, ${off.status} without`);

  const h1Policy = fam.jsOn?.h1 ?? 'same';
  if (n.visibleH1s.length !== 1) add('js-h1', 'js-on', `${n.visibleH1s.length} visible <h1> after JS ${JSON.stringify(n.visibleH1s.slice(0, 3))}`);
  else if (h1Policy === 'same' && s.visibleH1s.length === 1 && n.visibleH1s[0] !== s.visibleH1s[0]) {
    add('js-h1', 'js-on', `H1 changed after JS: "${s.visibleH1s[0]}" -> "${n.visibleH1s[0]}"`);
  }
  if (JSON.stringify(n.canonicals) !== JSON.stringify(s.canonicals)) {
    add('js-canonical', 'js-on', `canonical after JS ${JSON.stringify(n.canonicals)}, before ${JSON.stringify(s.canonicals)}`);
  }
  const robotsOn = uniq(n.robots.map((r) => r.trim().toLowerCase()));
  if (JSON.stringify(robotsOn) !== JSON.stringify(robots)) add('js-robots', 'js-on', `robots after JS ${JSON.stringify(robotsOn)}, before ${JSON.stringify(robots)}`);
  if (n.descriptions.length > Math.max(1, s.descriptions.length)) add('description', 'js-on', `${n.descriptions.length} meta descriptions after JS`);

  const ctaPolicy = fam.jsOn?.ctas ?? 'same';
  const offCtas = mainCtas(s);
  const onCtas = mainCtas(n);
  if (ctaPolicy === 'same' && onCtas.length !== offCtas.length) {
    const before = offCtas.map(ctaKey);
    const after = onCtas.map(ctaKey);
    const lost = before.filter((k) => { const i = after.indexOf(k); if (i >= 0) { after.splice(i, 1); return false; } return true; });
    add('js-ctas', 'js-on', `visible CTAs in <main>: ${offCtas.length} without JS, ${onCtas.length} with${lost.length ? `; lost ${lost.slice(0, 4).join(', ')}` : ''}${after.length ? `; gained ${after.slice(0, 4).join(', ')}` : ''}`);
  } else if (ctaPolicy !== 'same' && offCtas.length > 0 && onCtas.length === 0) {
    add('js-ctas', 'js-on', `${offCtas.length} visible CTAs in <main> without JS, none with JS`);
  }
  for (const c of n.ctas) for (const m of ctaProblems(c, allowedTags)) add('cta-compliance', 'js-on', m);

  if (n.counts.main !== 1 || n.counts.mainId !== 1) add('js-structure', 'js-on', `<main>=${n.counts.main} #main=${n.counts.mainId} after JS, expected one of each`);
  if (n.counts.ssrStyles > 1) add('js-structure', 'js-on', `${n.counts.ssrStyles} style#ssr-styles after JS`);
  if (n.counts.ssrMain > 1) add('js-structure', 'js-on', `${n.counts.ssrMain} .ssr-main page bodies after JS`);

  for (const l of n.leaks.slice(0, 4)) add('visible-code', 'js-on', `${l.kind} in <${l.tag}${l.cls ? `.${l.cls}` : ''}>: "${l.text}"`);
  for (const pr of jsonLdProblems(n.jsonld).problems.slice(0, 4)) add('js-json-ld', 'js-on', pr);
  const tOff = typeCounts(s.jsonld);
  const tOn = typeCounts(n.jsonld);
  for (const [ty, cnt] of Object.entries(tOn)) {
    if (cnt > (tOff[ty] || 0)) add('js-json-ld', 'js-on', `JSON-LD @type ${ty}: ${cnt} after JS, ${tOff[ty] || 0} before (duplicated)`);
  }
  for (const pb of n.priceBugs.slice(0, 3)) add('price-text', 'js-on', `"${pb}"`);
  for (const b of n.badLinks.slice(0, 3)) add('bad-links', 'js-on', `link with href ${b}`);
  for (const d of await imageProblems(n.images, request)) add('images', 'js-on', d);

  const onOverflow = phoneOverflow(on);
  if (onOverflow) add('phone-overflow', 'js-on', onOverflow);
  for (const t of illegibleOf(on).slice(0, LEGIBILITY_PER_PAGE)) add('legibility', 'js-on', legibilityDetail(t));

  // ── 3. JS on, desktop (first URL of the family) ──────────────────────
  if (ctx.desk) {
    const d = await loadAndSnapshot(ctx.desk, p, {
      js: true, view: 'desktop',
      diagnose: async (page, r) => {
        if (r.snap.overflow.scrollWidth > DESKTOP.width + 1) r.culprit = await overflowCulprit(page, DESKTOP.width);
        if (applies('legibility')) await legibility(page, r);
      },
    });
    for (const e of uniq(d.errors).slice(0, 3)) add('page-errors', 'desktop', e);
    for (const l of d.snap.leaks.slice(0, 3)) add('visible-code', 'desktop', `${l.kind} in <${l.tag}>: "${l.text}"`);
    if (d.snap.overflow.scrollWidth > DESKTOP.width + 1) {
      add('desktop-overflow', 'desktop', `page is ${d.snap.overflow.scrollWidth}px wide at ${DESKTOP.width}px${d.culprit ? `; widest element: ${d.culprit}` : ''}`);
    }
    for (const t of illegibleOf(d).slice(0, LEGIBILITY_PER_PAGE)) add('legibility', 'desktop', legibilityDetail(t));
  }

  // ── 4. Internal links resolve on the server under test ───────────────
  // Only links present in both renders, so the sample holds still.
  if (applies('links') && linkBudget.left > 0) {
    const offLinks = new Set(s.links.map((h) => internalPagePath(h, `${BASE_URL}${p}`)).filter((x): x is string => !!x));
    const onLinks = new Set(n.links.map((h) => internalPagePath(h, `${BASE_URL}${p}`)).filter((x): x is string => !!x));
    const stable = [...offLinks].filter((l) => onLinks.has(l) && normalizePath(l) !== normalizePath(p));
    const picks = seededSample(stable, Math.min(LINKS_PER_PAGE, linkBudget.left), `links:${p}`);
    linkBudget.left -= picks.length;
    const results = await Promise.all(picks.map(async (l) => [l, await resolvePath(request, l)] as const));
    const prodOnly = cfg.prodOnlyLinkTargets ?? {};
    for (const [l, r] of results) {
      if (!r.why) continue;
      const why = prodOnly[l] || prodOnly[r.final];
      if (why) notes.push(`${p} links ${l}, ${r.why} locally (production-only: ${why})`);
      else add('links', 'js-off', `internal link ${l} -> ${r.why}`);
    }
  }
  return monetized;
}

/** Declares every all-pages test for a site's gate config. */
export function registerAllPagesSuite(cfg: GateConfig): void {
  test.describe.configure({ mode: 'parallel' });
  test.skip(() => targetsProduction(), 'all-pages audits the production image on its local fixture (run-local-suite.sh); it never runs against production');

  // ── The families ──────────────────────────────────────────────────────
  for (const fam of cfg.families) {
    const shards = fam.shards ?? 1;
    for (let shard = 0; shard < shards; shard++) {
      const name = shards > 1 ? `${fam.id} [${shard + 1}/${shards}]` : fam.id;
      test(`page family ${name}`, async ({ browser, request }, testInfo) => {
        test.setTimeout(60_000 + Math.ceil(fam.sample / shards) * 50_000);
        const all = await samplesFor(fam, request);
        testInfo.annotations.push({ type: 'source', description: `${fam.sourceLabel}; seed ${SEED}; ${all.length} sampled` });
        for (const [check, reason] of Object.entries(fam.exceptions || {})) testInfo.annotations.push({ type: 'exception', description: `${check}: ${reason}` });
        if (fam.jsOn?.h1 && fam.jsOn.h1 !== 'same') testInfo.annotations.push({ type: 'exception', description: `js-h1 may differ: ${fam.jsOn.h1.own}` });
        if (fam.jsOn?.ctas && fam.jsOn.ctas !== 'same') testInfo.annotations.push({ type: 'exception', description: `js-ctas need only stay >= 1: ${fam.jsOn.ctas.atLeastOne}` });
        if (fam.canonical) testInfo.annotations.push({ type: 'exception', description: `canonical: ${fam.canonical.reason}` });
        if (fam.siteNameTitle) testInfo.annotations.push({ type: 'exception', description: `title: ${fam.siteNameTitle}` });
        if (all.length === 0) {
          if (fam.fixtureOptional) {
            testInfo.annotations.push({ type: 'fixture-optional', description: fam.fixtureOptional });
            test.skip(true, `fixture-optional: ${fam.fixtureOptional}`);
            return;
          }
          throw new Error(`${fam.id}: ${fam.sourceLabel} gave no sample URLs on the local server. A family that cannot be sampled is not tested; fix the source, or mark it fixtureOptional with the reason.`);
        }
        const mine = all.filter((_, i) => i % shards === shard);
        if (mine.length === 0) {
          throw new Error(`${fam.id}: ${all.length} sample URL(s) for ${shards} shards leaves shard ${shard + 1} empty; lower shards or fix the source (${fam.sourceLabel}).`);
        }
        if (all.length < fam.sample) testInfo.annotations.push({ type: 'under-sampled', description: `${all.length} of ${fam.sample} wanted: ${fam.sourceLabel} has no more on the local server` });
        for (const u of mine) testInfo.annotations.push({ type: 'url', description: u });

        const problems: string[] = [];
        const notes: string[] = [];
        let monetizedPages = 0;
        const ctx: Contexts = {
          off: await newAuditContext(browser, { js: false, view: 'phone' }),
          on: await newAuditContext(browser, { js: true, view: 'phone' }),
          desk: shard === 0 ? await newAuditContext(browser, { js: true, view: 'desktop' }) : null,
        };
        const linkBudget = { left: LINKS_PER_TEST };
        try {
          for (const [i, u] of mine.entries()) {
            if (await auditOne(cfg, fam, u, { ...ctx, desk: i === 0 ? ctx.desk : null }, request, problems, notes, linkBudget)) monetizedPages++;
          }
        } finally {
          await ctx.off.close();
          await ctx.on.close();
          await ctx.desk?.close();
        }
        for (const n of notes) testInfo.annotations.push({ type: 'prod-only-link', description: n });
        if (typeof fam.monetized === 'object' && !fam.exceptions?.monetized && monetizedPages * 2 < mine.length) {
          problems.push(`[family] monetized: only ${monetizedPages} of ${mine.length} pages show a visible affiliate CTA without JS (${fam.monetized.most})`);
        }
        expect(problems, `${fam.id}: ${problems.length} problem(s) on ${mine.length} URL(s)\n  ${problems.join('\n  ')}\n`).toEqual([]);
      });
    }
  }

  // ── Phone layout sweep: a wider sample of the long-tail families ───────
  for (const sw of cfg.phoneSweep ?? []) {
    const shards = sw.shards ?? 1;
    for (let shard = 0; shard < shards; shard++) {
      test(`phone layout sweep ${sw.id}${shards > 1 ? ` [${shard + 1}/${shards}]` : ''}`, async ({ browser, request }, testInfo) => {
        test.setTimeout(300_000);
        const mine = seededSample(await sw.source({ request }), sw.n, `sweep:${sw.id}`).filter((_, i) => i % shards === shard);
        expect(mine.length, `${sw.id}: ${sw.sourceLabel} sample`).toBeGreaterThan(0);
        testInfo.annotations.push({ type: 'source', description: `${sw.sourceLabel}; ${mine.length} URLs; seed ${SEED}` });
        const ctx = await newAuditContext(browser, { js: false, view: 'phone' });
        const problems: string[] = [];
        try {
          for (const p of mine) {
            const r = await loadAndSnapshot(ctx, p, { js: false, view: 'phone', diagnose: diagnosePhone });
            if (r.status !== 200) { problems.push(`${p}: HTTP ${r.status}`); continue; }
            const o = phoneOverflow(r);
            if (o) problems.push(`${p}: ${o}`);
            for (const l of r.snap.leaks.slice(0, 2)) problems.push(`${p}: visible ${l.kind} in <${l.tag}${l.cls ? `.${l.cls}` : ''}>: "${l.text}"`);
            if (r.snap.visibleH1s.length !== 1) problems.push(`${p}: ${r.snap.visibleH1s.length} visible <h1>`);
          }
        } finally {
          await ctx.close();
        }
        for (const u of mine) testInfo.annotations.push({ type: 'url', description: u });
        expect(problems, `${sw.id}: ${problems.length} problem(s) on ${mine.length} URLs\n  ${problems.join('\n  ')}\n`).toEqual([]);
      });
    }
  }

  // ── Site chrome: every navbar and footer link resolves ─────────────────
  test('site chrome links (nav and footer) resolve', async ({ browser, request }) => {
    test.setTimeout(240_000);
    const page = cfg.chrome?.page ?? '/';
    const problems: string[] = [];
    const notes: string[] = [];
    const chrome = new Set<string>();
    for (const js of [false, true]) {
      const c = await newAuditContext(browser, { js, view: 'desktop' });
      try {
        const r = await loadAndSnapshot(c, page, { js, view: 'desktop' });
        for (const h of r.snap.chromeLinks) {
          const l = internalPagePath(h, `${BASE_URL}${page}`);
          if (l) chrome.add(l);
        }
      } finally {
        await c.close();
      }
    }
    expect(chrome.size, `navbar + footer links found on ${page}`).toBeGreaterThanOrEqual(cfg.chrome?.minLinks ?? 3);
    const prodOnly = cfg.prodOnlyLinkTargets ?? {};
    for (const l of [...chrome].sort()) {
      const r = await resolvePath(request, l);
      if (!r.why) continue;
      const why = prodOnly[l] || prodOnly[r.final];
      if (why) notes.push(`${l}: ${r.why} locally (production-only: ${why})`);
      else problems.push(`${l} -> ${r.why}`);
    }
    for (const n of notes) test.info().annotations.push({ type: 'prod-only-link', description: n });
    test.info().annotations.push({ type: 'checked', description: `${chrome.size} chrome links` });
    expect(problems, `dead navbar/footer links:\n  ${problems.join('\n  ')}\n`).toEqual([]);
  });

  // ── Sitemaps: everything submitted is a real, indexable, self-canonical page ─
  const sm = cfg.sitemaps ?? {};
  const index = sm.index ?? '/sitemap.xml';
  test.describe('sitemap integrity', () => {
    test('the sitemap index lists live sub-sitemaps', async ({ request }) => {
      const resp = await request.get(`${BASE_URL}${index}`);
      expect(resp.status(), index).toBe(200);
      const children = await sitemapFiles(request, index);
      expect(children.length, 'sub-sitemaps in the index').toBeGreaterThan(0);
      const problems: string[] = [];
      for (const c of children) {
        const r = await request.get(`${BASE_URL}/${c}`, { timeout: 90_000 });
        if (r.status() === 200) continue;
        if (r.status() === 503 && sm.emptyOnFixture?.[c]) {
          test.info().annotations.push({ type: 'fixture-optional', description: `${c} answers 503: ${sm.emptyOnFixture[c]}` });
          continue;
        }
        problems.push(`${c}: HTTP ${r.status()}`);
      }
      expect(problems, problems.join('\n')).toEqual([]);
    });

    /** Problems with one sub-sitemap's URLs (this shard of them). */
    const checkFile = async (request: APIRequestContext, file: string, shard: number, shards: number, testInfo: TestInfo): Promise<string[]> => {
      const problems: string[] = [];
      let paths = (await sitemapPaths(request, file)).filter((p) => !/\.xml$/.test(p));
      const total = paths.length;
      if (total === 0) {
        const st = (await request.get(`${BASE_URL}/${file}`, { timeout: 60_000 })).status();
        const why = sm.emptyUrlsetOnFixture?.[file] ?? (st === 503 ? sm.emptyOnFixture?.[file] : undefined);
        if (why) {
          testInfo.annotations.push({ type: 'fixture-optional', description: `${file} has no URLs locally: ${why}` });
          return problems;
        }
        return [`${file}: HTTP ${st} with no URLs on the local server (list it in sitemaps.emptyUrlsetOnFixture with the reason if that is by design)`];
      }
      const cap = sm.sample?.[file];
      if (cap) paths = seededSample(paths, cap, `sitemap:${file}`);
      paths = paths.filter((_, i) => i % shards === shard);
      testInfo.annotations.push({ type: 'source', description: `${file}: ${total} URLs, checking ${paths.length}${cap ? ` (seed ${SEED})` : ''}` });
      for (const p of paths) {
        const r = await request.get(`${BASE_URL}${p}`, { maxRedirects: 0, timeout: 60_000 });
        if (r.status() !== 200) {
          problems.push(`${file} ${p}: HTTP ${r.status()}${r.headers()['location'] ? ` -> ${r.headers()['location']}` : ''}`);
          continue;
        }
        const html = await r.text();
        const robots = [...html.matchAll(/<meta\s+name="robots"\s+content="([^"]*)"/gi)].map((m) => m[1] as string);
        if (robots.some((x) => /noindex/i.test(x))) problems.push(`${file} ${p}: listed but "${robots.join(' | ')}"`);
        const canon = [...html.matchAll(/<link\s+rel="canonical"\s+href="([^"]*)"/gi)].map((m) => m[1] as string);
        if (canon.length !== 1) problems.push(`${file} ${p}: ${canon.length} canonical links`);
        else if (normalizePath(canonicalPath((canon[0] as string).replace(/&amp;/g, '&'))) !== normalizePath(p)) problems.push(`${file} ${p}: canonical is ${canon[0]}`);
      }
      return problems;
    };

    if (sm.files?.length) {
      // Declared files: one test per file (and shard), so workers share a big catalog.
      for (const file of sm.files) {
        const shards = sm.shards?.[file] ?? 1;
        for (let shard = 0; shard < shards; shard++) {
          test(`${file}${shards > 1 ? ` [${shard + 1}/${shards}]` : ''}: every URL is 200, indexable and self-canonical`, async ({ request }, testInfo) => {
            test.setTimeout(300_000);
            const problems = await checkFile(request, file, shard, shards, testInfo);
            expect(problems, `${file}: ${problems.length} listed URL(s) are not indexable pages\n  ${problems.slice(0, 40).join('\n  ')}\n`).toEqual([]);
          });
        }
      }
      test('the sitemap index lists no sub-sitemap the gate config does not declare', async ({ request }) => {
        const listed = await sitemapFiles(request, index);
        const undeclared = listed.filter((f) => !sm.files?.includes(f));
        expect(undeclared, `sub-sitemaps not in sitemaps.files (add them, so their URLs are checked): ${undeclared.join(', ')}`).toEqual([]);
      });
    } else {
      test('every sub-sitemap URL is 200, indexable and self-canonical', async ({ request }, testInfo) => {
        test.setTimeout(600_000);
        const files = await sitemapFiles(request, index);
        expect(files.length, 'sub-sitemaps in the index').toBeGreaterThan(0);
        const problems: string[] = [];
        for (const file of files) problems.push(...(await checkFile(request, file, 0, 1, testInfo)));
        expect(problems, `${problems.length} listed URL(s) are not indexable pages\n  ${problems.slice(0, 40).join('\n  ')}\n`).toEqual([]);
      });
    }
  });

  // ── The HTML cache never serves one URL's render as another's ──────────
  if (cfg.cacheKeyCases) {
    const cases = cfg.cacheKeyCases;
    test('a query-driven render is never served as its hub, and the hub never as it', async ({ request }) => {
      const canonicalOf = async (p: string): Promise<string[]> => {
        const r = await request.get(`${BASE_URL}${p}`);
        expect(r.status(), p).toBe(200);
        return [...(await r.text()).matchAll(/<link\s+rel="canonical"\s+href="([^"]*)"/gi)].map((m) => canonicalPath(m[1] as string));
      };
      for (const c of await cases({ request })) {
        const variant = await canonicalOf(c.variant);
        expect(await canonicalOf(c.hub), `${c.hub} after ${c.variant}`).toEqual([normalizePath(c.hub)]);
        expect(await canonicalOf(c.variant), `${c.variant} after ${c.hub}`).toEqual(variant);
      }
    });
  }

  // ── Legacy URL shapes answer with the redirect the router promises ─────
  if (cfg.redirects) {
    const redirects = cfg.redirects;
    test('legacy URL shapes redirect to the canonical page', async ({ request }) => {
      const cases = await redirects({ request });
      const problems: string[] = [];
      for (const c of cases) {
        const r = await request.get(`${BASE_URL}${c.from}`, { maxRedirects: 0 });
        const want = c.status ?? 301;
        if (r.status() !== want) { problems.push(`${c.from}: HTTP ${r.status()}, expected ${want}`); continue; }
        if (want === 301) {
          const loc = new URL(r.headers()['location'] || '', BASE_URL);
          if (normalizePath(loc.pathname + loc.search) !== normalizePath(c.to)) problems.push(`${c.from}: 301 to ${loc.pathname}${loc.search}, expected ${c.to}`);
        }
      }
      test.info().annotations.push({ type: 'cases', description: cases.map((c) => `${c.from} -> ${c.to || c.status}`).join(', ') });
      expect(problems, problems.join('\n')).toEqual([]);
    });
  }

  // ── The family list covers every route and every sitemap entry ─────────
  if (cfg.routes) {
    const routes = cfg.routes;
    test('every page route in the route table belongs to a page family or is listed as a non-page route', async () => {
      const declared = uniq([...(await routes.declared())]);
      expect(declared.length, 'page routes the route table declares').toBeGreaterThan(0);
      const covered = new Set(cfg.families.flatMap((f) => f.routes));
      const nonPage = routes.nonPage ?? {};
      const missing = declared.filter((r) => !covered.has(r) && !nonPage[r]);
      expect(missing, `routes with no page family in the gate config (add one, or list it in routes.nonPage with the reason): ${missing.join(', ')}`).toEqual([]);
    });
  }

  test('every URL in the local sitemaps belongs to a page family', async ({ request }) => {
    const patterns = cfg.families.flatMap((f) => f.routes).map(routeRegex);
    const unmatched = new Set<string>();
    for (const f of await sitemapFiles(request, index)) {
      for (const p of await sitemapPaths(request, f)) {
        const pathOnly = p.split('?')[0] as string;
        if (/\.xml$/.test(pathOnly)) continue;
        if (!patterns.some((re) => re.test(pathOnly))) unmatched.add(pathOnly.replace(/\/[^/]+$/, '/*'));
      }
    }
    expect([...unmatched], `sitemap URL shapes no page family covers: ${[...unmatched].join(', ')}`).toEqual([]);
  });

  // ── The visible-code detector finds what it must, and nothing else ─────
  test('visible-code detector: catches each leak kind, ignores code blocks and prose', async ({ browser }) => {
    const ctx = await browser.newContext({ javaScriptEnabled: false });
    await ctx.route('**/*', (r) => r.fulfill({
      status: 200, contentType: 'text/html', body: `<!doctype html><html><head><title>t</title></head><body><main id="main">
        <h1>Fixture</h1>
        <p id="css">.ssr-main{max-width:1200px;margin:0 auto;}</p>
        <p id="tag">Intro &lt;div class="ssr-card"&gt; text</p>
        <p id="entity">Tools &amp;amp; Maintenance</p>
        <p id="js">window.dataLayer = window.dataLayer || [];</p>
        <p id="jsonld">{"@context":"https://schema.org","@type":"Product"}</p>
        <p id="template">Price: undefined</p>
        <p id="md">| Model | Capacity |</p><p>|---|---|</p>
        <details><summary>Q</summary><p id="faq">**Bold** answer</p></details>
        <pre>.code{color:red;} const x = 1;</pre>
        <code>{"@type":"Thing"}</code>
        <p>Prose about a $0 affiliate click, a run time &lt;5 ms away, and if a &lt;b and c&gt; d.</p>
        <p class="price">$0.00</p>
        <style>.hidden{display:none}</style>
        <p style="display:none">.x{color:red;}</p>
      </main></body></html>`,
    }));
    const r = await loadAndSnapshot(ctx, '/detector-fixture', { js: false, view: 'desktop' });
    const kinds = new Set(r.snap.leaks.map((l) => l.kind));
    for (const k of ['css', 'html-tag', 'html-entity', 'js', 'json-ld', 'template', 'markdown']) {
      expect(kinds.has(k), `detector missed ${k}; found ${[...kinds].join(', ')}`).toBe(true);
    }
    const texts = r.snap.leaks.map((l) => l.text).join(' | ');
    expect(texts, 'flagged a <pre>/<code> block').not.toContain('.code{color:red;}');
    expect(texts, 'flagged prose').not.toContain('affiliate click');
    expect(texts, 'flagged hidden text').not.toContain('.x{color:red;}');
    expect(r.snap.priceBugs, 'price detector').toEqual(['$0.00']);
    await ctx.close();
  });

  // ── The legibility check flags unreadable text, and nothing else ───────
  test('legibility check: flags low-contrast text, passes readable text, sets aside text over a third-party picture', async ({ browser }) => {
    const ctx = await browser.newContext({ viewport: DESKTOP });
    await ctx.route('**/*', (r) => (r.request().resourceType() === 'image'
      ? r.fulfill({ status: 200, contentType: 'image/svg+xml', body: '<svg xmlns="http://www.w3.org/2000/svg" width="600" height="300"><rect width="600" height="300" fill="#e2e8f0"/></svg>' })
      : r.fulfill({
        status: 200, contentType: 'text/html', body: `<!doctype html><html><head><title>t</title><style>
          body{margin:0;padding:16px;font:16px/1.5 sans-serif;background:#fff}
          .banner{background:linear-gradient(135deg,#1e1b4b 0%,#4f46e5 55%,#7c3aed 100%);padding:24px;margin:0 0 16px}
          .banner h2{color:#fff;margin:0}
          .banner p{color:#334155;margin:8px 0 0}
          .pale{color:#cbd5e1}
          .ok{color:#334155}
          .large-pale{color:#94a3b8;font-size:28px;margin:0}
          .large-ok{color:#7f8ea3;font-size:28px;margin:0}
          .small-same{color:#7f8ea3}
          .photo{position:relative;height:120px;margin:16px 0}
          .photo img{position:absolute;inset:0;width:100%;height:100%}
          .photo span{position:relative;color:#fff}
          .grad{background:linear-gradient(90deg,#f00,#00f);-webkit-background-clip:text;background-clip:text;color:transparent}
          input{background:#f3f4f6;border:0;padding:8px;font-size:16px;width:300px}
          input::placeholder{color:#94a3b8}
        </style></head><body>
          <div class="banner"><h2>Readable heading</h2><p>Tell us your budget</p></div>
          <p class="pale">Pale footnote</p>
          <p class="ok">Readable paragraph</p>
          <p class="large-pale">Large pale heading</p>
          <p class="large-ok">Large heading at 3.3 to 1</p>
          <p class="small-same">Small text at 3.3 to 1</p>
          <div class="photo"><img src="https://m.media-amazon.com/images/I/x.jpg" alt=""><span>Caption on a product photo</span></div>
          <button disabled style="color:#cbd5e1;background:#fff;border:0">Disabled control</button>
          <p class="grad">Gradient text</p>
          <input placeholder="Pale placeholder">
        </body></html>`,
      })));
    const page = await ctx.newPage();
    await page.goto('http://legibility.fixture/');
    const found = await illegibleText(page, { trustedHosts: ['legibility.fixture'] });
    const texts = found.map((t) => t.text);
    for (const want of ['Tell us your budget', 'Pale footnote', 'Large pale heading', 'Small text at 3.3 to 1', '[placeholder] Pale placeholder']) {
      expect(texts, `missed "${want}"; found ${JSON.stringify(found)}`).toContain(want);
    }
    for (const not of ['Readable heading', 'Readable paragraph', 'Large heading at 3.3 to 1', 'Caption on a product photo', 'Disabled control', 'Gradient text']) {
      expect(texts, `flagged "${not}"; found ${JSON.stringify(found)}`).not.toContain(not);
    }
    await ctx.close();
  });
}
