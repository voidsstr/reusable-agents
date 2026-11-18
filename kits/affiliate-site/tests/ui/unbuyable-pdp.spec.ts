import { test, expect } from '@playwright/test';
import { loadGateConfig } from './lib/gateConfig';
import { GATE_UA } from './lib/pageAudit';
import { inspectPdp, unbuyableProblems } from './lib/pdpInspect';
import { BASE_URL, gateSite, targetsProduction } from './lib/site';

// Unbuyable product pages must never be a dead end.
//
// Ported from SpecPicks tests/ui/unbuyable-pdp.spec.ts (growth-20261003): the
// product pages assistants open most are often ones Amazon cannot sell right
// now. When the page says so (the kit's dated "Unavailable on Amazon.com"
// stamp), it must offer something buyable at once: an in-stock alternative
// with a dated price and a tagged CTA, or, when the catalog holds none, a
// tagged Amazon search. A page that says "unavailable" and stops there loses
// the click. In-stock pages still need a tagged CTA.
//
// On the local fixture it reads each page as ChatGPT-User, the way the
// assistant does (what a site serves an assistant must be buyable too). Against
// production (@prod-safe) it reads them with the gate's plain-browser
// User-Agent instead: an assistant's would write a spoofed row into the live
// ai_traffic_log for every page, and ai-fetcher-access.spec.ts already proves
// that assistants get the same page a browser gets. So a production run writes
// nothing.

const cfg = loadGateConfig();
const CHATGPT_USER_UA = 'Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; ChatGPT-User/1.0; +https://openai.com/bot';
const ALT_PLACEMENTS = cfg.placements?.alt ?? ['pdp-alt', 'pdp-substitute', 'pdp-alt-top'];
const UNAVAILABLE_RE = cfg.unbuyable?.unavailableRe ?? /Unavailable on Amazon(?:\.com)? as of/;

test.describe('Unbuyable product pages offer a buyable answer @prod-safe', () => {
  test('every unbuyable product page offers an alternative or a tagged search', async ({ browser, request }, testInfo) => {
    test.setTimeout(180_000);
    if (!cfg.unbuyable) {
      testInfo.annotations.push({ type: 'not-configured', description: 'unbuyable.products is not set in the gate config' });
      test.skip(true, 'the gate config names no unbuyable product pages (unbuyable.products)');
      return;
    }
    const pages = await cfg.unbuyable.products({ request });
    expect(pages.length, 'unbuyable product pages on the fixture (unbuyable.products)').toBeGreaterThan(0);
    const allowed = gateSite().ctx.amazon.allowedTags;
    const ctx = await browser.newContext({ javaScriptEnabled: false, userAgent: targetsProduction() ? GATE_UA.phone : CHATGPT_USER_UA });
    await ctx.route('**/*', async (route) => {
      // Only the server under test; nothing else is fetched (the page is read, not rendered).
      const u = new URL(route.request().url());
      if (u.origin === new URL(BASE_URL).origin) return route.continue();
      return route.abort();
    });
    const problems: string[] = [];
    try {
      for (const p of pages) {
        const page = await ctx.newPage();
        try {
          const resp = await page.goto(`${BASE_URL}${p}`, { waitUntil: 'domcontentloaded' });
          if (resp?.status() !== 200) { problems.push(`${p}: HTTP ${resp?.status()}`); continue; }
          const inspection = await page.evaluate(inspectPdp, { unavailableRe: UNAVAILABLE_RE.source });
          if (!inspection.unbuyable) testInfo.annotations.push({ type: 'in-stock-now', description: `${p} is buyable right now; checked for a tagged CTA` });
          problems.push(...unbuyableProblems(p, inspection, allowed, ALT_PLACEMENTS));
        } finally {
          await page.close();
        }
      }
    } finally {
      await ctx.close();
    }
    for (const p of pages) testInfo.annotations.push({ type: 'url', description: p });
    expect(problems, problems.join('\n')).toEqual([]);
  });

  // The kit-side twin of the rule, on fixed markup, so the check cannot pass by matching nothing.
  test('dead-end detector: flags an unavailable page with nothing to buy, passes one with an alternative', async ({ browser }) => {
    const tag = gateSite().ctx.amazon.partnerTag;
    const stamp = '<span data-price-stamp="availability"><span class="kit-availability kit-availability--unavailable">Unavailable on Amazon.com</span> as of <time datetime="2026-11-17T12:00:00Z">2026-11-17 12:00 UTC</time></span>';
    const alt = `<section><p><span data-price-stamp="price"><span class="ssr-price">$10.00</span> on Amazon.com as of <time datetime="2026-11-17T12:00:00Z">2026-11-17 12:00 UTC</time></span></p>`
      + `<p><a href="https://www.amazon.com/dp/B0ALTERNAT?tag=${tag}" rel="sponsored nofollow noopener" target="_blank" data-cta-placement="${ALT_PLACEMENTS[0]}">View</a></p></section>`;
    const ctx = await browser.newContext({ javaScriptEnabled: false });
    await ctx.route('**/*', (r) => r.fulfill({
      status: 200,
      contentType: 'text/html',
      body: `<!doctype html><html><body><main><h1>X</h1>${stamp}${new URL(r.request().url()).pathname === '/alt' ? alt : ''}</main></body></html>`,
    }));
    const check = async (p: string): Promise<string[]> => {
      const page = await ctx.newPage();
      await page.goto(`http://pdp.fixture${p}`);
      const out = unbuyableProblems(p, await page.evaluate(inspectPdp, { unavailableRe: UNAVAILABLE_RE.source }), gateSite().ctx.amazon.allowedTags, ALT_PLACEMENTS);
      await page.close();
      return out;
    };
    expect(await check('/alt')).toEqual([]);
    expect((await check('/dead-end')).join('\n')).toMatch(/dead end/);
    await ctx.close();
  });
});
