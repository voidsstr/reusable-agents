/**
 * The built browser bundle in a real Chromium, against the real handlers:
 * mountKit serves the bundle and the beacon endpoint, a page renders kit
 * CTAs and the sticky bar, and clicks travel through navigator.sendBeacon to
 * outbound_clicks rows (an in-memory KitDb).
 *
 * Uses Playwright's cached Chromium through @playwright/test (the declared
 * dev dependency; only its library API, not its runner); skipped when the
 * browser is not installed (or with KIT_SKIP_BROWSER=1). Offline: every
 * request that is not to the local test server is aborted, and clicks never
 * navigate.
 */
import { execFileSync } from 'child_process';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import express from 'express';
import type { Server } from 'http';
import type { AddressInfo } from 'net';
import type { Browser, BrowserContext, Page } from '@playwright/test';
import { kitClientScriptTag, loadKitClientBundle } from '../../server/src/http/clientAsset';
import { STICKY_ACTIVE_CLASS, STICKY_HEIGHT_VAR } from '../../server/src/http/clientContract';
import { mountKit, type MountedKit } from '../../server/src/http/mountKit';
import { STICKY_BAR_CSS, stickyBuyBarHtml } from '../../server/src/http/stickyBar';
import { cta } from '../../server/src/links';
import { PRICE_STAMP_CSS } from '../../server/src/price';
import { captureLogger, CHROME_UA, exampleCtx, FakeKitDb, KIT_DIR, stubVerifier, waitFor } from './helpers';

type Chromium = typeof import('@playwright/test').chromium;

function loadChromium(): Chromium | null {
  if (process.env.KIT_SKIP_BROWSER === '1') return null;
  try {
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    const pw = require('@playwright/test') as typeof import('@playwright/test');
    const exe = pw.chromium.executablePath();
    return exe && fs.existsSync(exe) ? pw.chromium : null;
  } catch {
    return null;
  }
}

const chromium = loadChromium();
const describeBrowser = chromium ? describe : describe.skip;
const ASIN = 'B0DS2X13PH';

describeBrowser('kit client bundle in Chromium', () => {
  jest.setTimeout(60_000);

  const ctx = exampleCtx();
  const db = new FakeKitDb();
  let outdir: string;
  let server: Server;
  let base: string;
  let kit: MountedKit;
  let browser: Browser;
  let context: BrowserContext;
  let page: Page;

  beforeAll(async () => {
    outdir = fs.mkdtempSync(path.join(os.tmpdir(), 'kit-client-e2e-'));
    execFileSync(process.execPath, [path.join(KIT_DIR, 'scripts/build-client.mjs'), '--outdir', outdir], { cwd: KIT_DIR });
    const bundle = loadKitClientBundle(path.join(outdir, 'kit-client.js'));

    const app = express();
    kit = mountKit(app, ctx, {
      db, verifier: stubVerifier(), clientBundle: bundle, env: {}, log: captureLogger(),
      redirectLookup: async (asin) => ({ asin, isActive: true, buyable: true }),
    });
    app.get('/product/:asin', (_req, res) => {
      res.type('html').send(`<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{margin:0;font-family:sans-serif}main{min-height:150vh}${PRICE_STAMP_CSS}${STICKY_BAR_CSS}</style>
${kitClientScriptTag(bundle)}
</head><body><main>
<h1>Acme PowerBox 1000</h1>
<p id="hero">${cta(ctx, { asin: ASIN, placement: 'pdp-hero' })}</p>
<article><p>Compare: <a id="search" href="https://www.amazon.com/s?k=acme+powerbox&tag=kitexample-20">search Amazon</a>
and <a id="via-r" data-cta-placement="article-buystrip" href="/r/${ASIN}">buy via our link</a>
or read <a id="internal" href="/guides/winter-kit">the guide</a>.</p></article>
</main>
<footer id="footer"><p>As an Amazon Associate, ExamplePicks earns from qualifying purchases.</p></footer>
${stickyBuyBarHtml(ctx, { asin: ASIN, name: 'Acme PowerBox 1000' })}
<script>document.addEventListener('click', function (e) { e.preventDefault(); }); document.addEventListener('auxclick', function (e) { e.preventDefault(); });</script>
</body></html>`);
    });
    server = await new Promise<Server>((resolve) => {
      const s = app.listen(0, '127.0.0.1', () => resolve(s));
    });
    base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;

    browser = await (chromium as Chromium).launch({ headless: true });
    context = await browser.newContext({ viewport: { width: 390, height: 844 }, userAgent: CHROME_UA, locale: 'en-US' });
    await context.route('**/*', (route) => (route.request().url().startsWith(base) ? route.continue() : route.abort()));
    page = await context.newPage();
    await page.goto(`${base}/product/${ASIN}`);
  });

  afterAll(async () => {
    await context?.close();
    await browser?.close();
    kit?.stop();
    await new Promise<void>((r) => (server ? server.close(() => r()) : r()));
    fs.rmSync(outdir, { recursive: true, force: true });
  });

  async function clickAndWait(selector: string, opts: { button?: 'left' | 'middle' } = {}) {
    const before = db.clicks.length;
    await page.click(selector, opts);
    await waitFor(() => db.clicks.length > before, 5000, `a beacon for ${selector}`);
    await kit.drain();
    return db.clicks[db.clicks.length - 1]!;
  }

  it('publishes the sticky bar height on phones and pads the page by it', async () => {
    await page.waitForFunction((v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim() !== '', STICKY_HEIGHT_VAR);
    const { varH, barH, pad } = await page.evaluate((v) => ({
      varH: getComputedStyle(document.documentElement).getPropertyValue(v).trim(),
      barH: Math.round((document.querySelector('[data-kit-sticky-bar]') as HTMLElement).getBoundingClientRect().height),
      pad: getComputedStyle(document.body).paddingBottom,
    }), STICKY_HEIGHT_VAR);
    expect(barH).toBeGreaterThan(40);
    expect(varH).toBe(`${barH}px`);
    expect(pad).toBe(`${barH}px`);
  });

  it('keeps the page clear of the bar without CSS :has() (html.kit-has-sticky)', async () => {
    // Emulate a browser without :has() (Firefox before 121): drop the :has padding rule.
    const result = await page.evaluate((cls) => {
      let removed = 0;
      for (const sheet of Array.from(document.styleSheets)) {
        const walk = (list: CSSRuleList): void => {
          for (let i = list.length - 1; i >= 0; i--) {
            const r = list[i] as CSSRule & { cssRules?: CSSRuleList; selectorText?: string };
            if (r.selectorText && r.selectorText.includes(':has(.kit-sticky-bar)')) {
              const parent = r.parentRule as CSSGroupingRule | null;
              if (parent) parent.deleteRule(i);
              else sheet.deleteRule(i);
              removed++;
            } else if (r.cssRules) {
              walk(r.cssRules);
            }
          }
        };
        walk(sheet.cssRules);
      }
      const bar = document.querySelector('[data-kit-sticky-bar]') as HTMLElement;
      return {
        removed,
        hasClass: document.documentElement.classList.contains(cls),
        barH: Math.round(bar.getBoundingClientRect().height),
        pad: getComputedStyle(document.body).paddingBottom,
      };
    }, STICKY_ACTIVE_CLASS);
    expect(result.removed).toBe(1);
    expect(result.hasClass).toBe(true);
    expect(result.pad).toBe(`${result.barH}px`);
    // the footer (disclosure, 2(k) notice) scrolls fully above the bar
    await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
    const overlap = await page.evaluate(() => {
      const foot = (document.getElementById('footer') as HTMLElement).getBoundingClientRect();
      const bar = (document.querySelector('[data-kit-sticky-bar]') as HTMLElement).getBoundingClientRect();
      return foot.bottom - bar.top;
    });
    expect(overlap).toBeLessThanOrEqual(0.5);
    await page.evaluate(() => window.scrollTo(0, 0));
  });

  it('sends a beacon for the hero CTA with its placement, tag and page signals', async () => {
    const row = await clickAndWait('#hero a');
    expect(row).toMatchObject({
      via: 'beacon', asin: ASIN, placement: 'pdp-hero', trackingId: 'kitexample-pdp-20', sourcePage: `/product/${ASIN}`,
      sourceKind: 'product', sessionPageviews: 1, ipAddress: '127.0.0.1', acceptLanguage: expect.stringContaining('en-US'),
    });
    expect(row.msOnPage).toBeGreaterThanOrEqual(0);
    // Playwright drives the browser, so navigator.webdriver is true: the click is (rightly) not a human's
    expect([row.isBot, row.botReason]).toEqual([true, 'webdriver']);
  });

  it('attributes the sticky bar button to sticky-mobile with its own tag', async () => {
    const row = await clickAndWait('[data-kit-sticky-bar] a');
    expect(row).toMatchObject({ placement: 'sticky-mobile', trackingId: 'kitexample-sticky-20', asin: ASIN });
  });

  it('counts same-origin /r/ links and untagged-placement Amazon links', async () => {
    expect(await clickAndWait('#via-r')).toMatchObject({ asin: ASIN, placement: 'article-buystrip', trackingId: null });
    expect(await clickAndWait('#search', { button: 'middle' })).toMatchObject({ asin: '', placement: 'in-article', trackingId: 'kitexample-20' });
  });

  it('ignores internal links and right clicks', async () => {
    const before = db.clicks.length;
    await page.click('#internal');
    await page.click('#hero a', { button: 'right' });
    await page.waitForTimeout(400);
    await kit.drain();
    expect(db.clicks.length).toBe(before);
  });

  it('counts page views across loads in the tab session', async () => {
    await page.goto(`${base}/product/${ASIN}`);
    const row = await clickAndWait('#hero a');
    expect(row.sessionPageviews).toBe(2);
  });

  it('hides the bar on desktop widths and publishes 0px', async () => {
    await page.setViewportSize({ width: 1280, height: 800 });
    await page.waitForFunction((v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim() === '0px', STICKY_HEIGHT_VAR);
    expect(await page.isVisible('[data-kit-sticky-bar]')).toBe(false);
    expect(await page.evaluate(() => getComputedStyle(document.body).paddingBottom)).toBe('0px');
  });
});
