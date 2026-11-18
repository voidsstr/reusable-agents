import { test, expect, devices, type BrowserContext, type Page, type Request, type Route } from '@playwright/test';
import { BEACON_PATH, STICKY_BAR_ATTR } from '../../server/src/http/clientContract';
import { loadGateConfig } from './lib/gateConfig';
import { routeRequest } from './lib/pageAudit';
import { BASE, BASE_URL, targetsProduction } from './lib/site';

// Every product-page buy placement must report itself.
//
// Ported from SpecPicks tests/ui/pdp-cta-placements.spec.ts
// (growth-20261016): placement_watch and the verified-click goals can only
// say "this placement broke" if every placement keeps reporting under a known
// name. This clicks each one the way a reader does, on a phone with JS on,
// and checks the placement the kit's click beacon sends.
//
// Nothing leaves for Amazon and no click row is written, against the fixture
// or production alike. Every route is set on the browser CONTEXT: a CTA opens
// a new tab (target=_blank), and a page.route() on the opener never sees that
// tab's first request (SpecPicks' copy, which routes the page, lets the tagged
// Amazon request through on production). The context answers the merchant and
// the /r/ hop itself, records and answers the beacon, gives third-party images
// a placeholder and aborts anything else that is not the server under test;
// the test then asserts that every request to an Amazon host, in any tab, was
// answered by one of those routes.

const cfg = loadGateConfig();
const pc = cfg.placements ?? {};
const HERO = pc.hero ?? ['pdp-hero'];
const STICKY = pc.sticky ?? ['sticky-mobile', 'pdp-sticky-live'];
const ALT = pc.alt ?? ['pdp-alt', 'pdp-substitute', 'pdp-alt-top'];

interface Beacon { placement: string; merchant: string; asin: string; page: string }

/** Amazon storefront and short-link hosts, and the site's /r/<ASIN> click redirect: answered by the test, never fetched. */
const MERCHANT_RE = /^https?:\/\/(?:[^/]+\.)?(?:amazon\.[a-z.]+|amzn\.(?:to|com|eu|asia)|a\.co)(?:[:/?#]|$)|\/r\/[A-Za-z0-9]{10}\/?(?:[?#]|$)/i;
/** Every Amazon host: storefronts, short links and the image CDNs. */
const AMAZON_HOST_RE = /(^|\.)(?:amazon\.[a-z.]+|amzn\.(?:to|com|eu|asia)|a\.co|media-amazon\.com|ssl-images-amazon\.com|images-amazon\.com|amazon-adsystem\.com)$/i;

const hostOf = (url: string): string => {
  try { return new URL(url).hostname; } catch { return ''; }
};

interface Opened {
  beacons: Beacon[];
  status: number;
  /** Requests to an Amazon host, from any tab, that no route of this test answered (must stay empty). */
  leaked: () => string[];
}

/** This spec's own context listeners, so a second openPdp() replaces them (and never touches the runner's). */
const listeners = new WeakMap<BrowserContext, { onRequest: (r: Request) => void; onPage: (p: Page) => void }>();

async function openPdp(page: Page, path: string): Promise<Opened> {
  const context = page.context();
  const beacons: Beacon[] = [];
  const seen: string[] = [];
  const answered = new Set<string>();
  await context.unrouteAll({ behavior: 'ignoreErrors' });
  const prev = listeners.get(context);
  if (prev) {
    context.off('request', prev.onRequest);
    context.off('page', prev.onPage);
  }
  const onRequest = (r: Request): void => { if (AMAZON_HOST_RE.test(hostOf(r.url()))) seen.push(r.url()); };
  // A CTA's new tab gets its (intercepted) answer, then closes.
  const onPage = (p: Page): void => {
    if (p === page) return;
    void p.waitForLoadState('domcontentloaded', { timeout: 5_000 }).catch(() => undefined).then(() => p.close().catch(() => undefined));
  };
  context.on('request', onRequest);
  context.on('page', onPage);
  listeners.set(context, { onRequest, onPage });
  // Routes run newest first: the catch-all goes in first, so the two after it win where they match.
  // 1. Only the server under test is reached: a production asset is served from it, a third-party
  //    image gets a placeholder, anything else is aborted (lib/pageAudit.ts routeRequest).
  await context.route('**/*', (route: Route) => {
    answered.add(route.request().url());
    return routeRequest(route);
  });
  // 2. The merchant navigation and the /r/ hop, in any tab: a real click would log a click row or reach Amazon tagged.
  await context.route(MERCHANT_RE, (route) => {
    answered.add(route.request().url());
    return route.fulfill({ status: 200, contentType: 'text/plain', body: 'intercepted' });
  });
  // 3. The click beacon: recorded, answered, never stored.
  await context.route(`**${BEACON_PATH}`, async (route) => {
    try { beacons.push(JSON.parse(route.request().postData() || '{}') as Beacon); } catch { /* not JSON */ }
    await route.fulfill({ status: 204 });
  });
  const resp = await page.goto(`${BASE_URL}${path}`, { waitUntil: 'load', timeout: 60_000 });
  await page.waitForFunction(() => (window as unknown as Record<string, unknown>).__kitClickBeacon === true, null, { timeout: 30_000 });
  const leaked = (): string[] => [...new Set(seen.filter((u) => !answered.has(u) && hostOf(u) !== BASE.hostname))];
  return { beacons, status: resp?.status() ?? 0, leaked };
}

async function clickAndRead(page: Page, beacons: Beacon[], selector: string): Promise<Beacon | undefined> {
  // The beacon drops a second click on the same href within 1 s as a duplicate,
  // and the hero and the sticky bar usually share one Amazon URL.
  await page.waitForTimeout(1_200);
  const before = beacons.length;
  const link = page.locator(selector).first();
  await link.scrollIntoViewIfNeeded();
  await link.click({ noWaitAfter: true });
  await expect.poll(() => beacons.length, { timeout: 5_000 }).toBeGreaterThan(before);
  return beacons[beacons.length - 1];
}

const MERCHANT_A = 'a[href*="amazon.com/"], a[href^="/r/"]';
const placed = (names: string[]): string => names
  .flatMap((n) => [`a[data-cta-placement="${n}"]`, ...MERCHANT_A.split(', ').map((a) => `[data-cta-placement="${n}"] ${a}`)])
  .join(', ');
const STICKY_SEL = `[${STICKY_BAR_ATTR}] a`;

test.describe('Product-page buy placements report the right placement on click @prod-safe', () => {
  test.describe.configure({ mode: 'serial' });
  test.use({
    viewport: devices['iPhone 13'].viewport,
    userAgent: devices['iPhone 13'].userAgent,
    deviceScaleFactor: devices['iPhone 13'].deviceScaleFactor,
    isMobile: true,
    hasTouch: true,
    // A service worker's own fetches would bypass the routes above.
    serviceWorkers: 'block',
  });

  test('in-stock product page: hero and sticky bar each report their own placement', async ({ page, request }) => {
    test.skip(!pc.inStock, 'the gate config names no in-stock product page (placements.inStock)');
    const path = (await pc.inStock!({ request }))[0];
    expect(path, 'an in-stock product page (placements.inStock)').toBeTruthy();
    const { beacons, status, leaked } = await openPdp(page, path as string);
    expect(status).toBe(200);

    const hero = await clickAndRead(page, beacons, placed(HERO));
    expect(HERO, `hero beacon placement was "${hero?.placement}"`).toContain(hero?.placement);

    const sticky = page.locator(STICKY_SEL).first();
    await expect(sticky, 'sticky buy button visible on a phone viewport').toBeVisible();
    const s = await clickAndRead(page, beacons, STICKY_SEL);
    expect(STICKY, `sticky beacon placement was "${s?.placement}"`).toContain(s?.placement);
    await page.waitForTimeout(500);
    expect(leaked(), 'requests to Amazon no route of this test answered').toEqual([]);
  });

  test('unbuyable product page: the alternative and the sticky bar report their placements', async ({ page, request }) => {
    test.skip(!pc.unbuyable, 'the gate config names no unbuyable product page (placements.unbuyable)');
    let tested = false;
    for (const path of await pc.unbuyable!({ request })) {
      const { beacons, status, leaked } = await openPdp(page, path);
      if (status !== 200) continue;
      if ((await page.locator(placed(ALT)).count()) === 0) continue;
      tested = true;
      const alt = await clickAndRead(page, beacons, placed(ALT));
      expect(ALT, `${path}: alternative beacon placement was "${alt?.placement}"`).toContain(alt?.placement);
      const s = await clickAndRead(page, beacons, STICKY_SEL);
      expect(STICKY, `${path}: sticky beacon placement was "${s?.placement}"`).toContain(s?.placement);
      await page.waitForTimeout(500);
      expect(leaked(), `${path}: requests to Amazon no route of this test answered`).toEqual([]);
      break;
    }
    // Production's stock moves every refresh; the fixture's does not, so there a missing alternative is a failure.
    if (!tested && targetsProduction()) test.skip(true, 'no unbuyable product page shows an in-stock alternative right now');
    expect(tested, 'an unbuyable product page that shows an in-stock alternative (placements.unbuyable)').toBe(true);
  });
});
