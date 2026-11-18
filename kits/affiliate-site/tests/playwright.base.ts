import * as path from 'path';
import { defineConfig, type PlaywrightTestConfig, type PlaywrightTestProject } from '@playwright/test';
import { gateTargetUrl } from './ui/lib/target';

/**
 * The Playwright config of a site's deploy gate, built around the kit's specs.
 *
 * A site's tests/playwright.config.ts is three lines:
 *
 *   import { kitPlaywrightConfig } from '../vendor/affiliate-kit/tests/playwright.base';
 *   export default kitPlaywrightConfig({ dir: __dirname, siteJson: '../site.json',
 *     gateConfig: './ui/gate.config.ts', siteSpecsDir: './ui' });
 *
 * Projects: 'kit' runs the kit's specs (tests/ui: all-pages, kit-compliance,
 * ai-fetcher-access, unbuyable-pdp, pdp-cta-placements) and 'site' the site's
 * own (its niche checks). Both read the site through KIT_GATE_SITE_JSON and
 * KIT_GATE_CONFIG, set here.
 *
 * Ported from SpecPicks tests/playwright.config.ts: the suite targets
 * TEST_URL (run-local-suite.sh sets it to the production image it started),
 * and on a local target the browser cannot resolve any host but the server
 * under test, so a gate run never loads analytics, never sends page views to
 * the production property and never fetches from production. Requests made
 * from Node (request, page.request) are unaffected; a remote TEST_URL (a
 * production smoke) runs as is. Like SpecPicks' config (which starts its own
 * server rather than reuse one), it never falls back to a default port: with
 * no TEST_URL, no runner settings and no webServer override it refuses to run.
 */

/** The kit's spec directory. */
export const KIT_UI_DIR = path.join(__dirname, 'ui');

export interface KitPlaywrightOptions {
  /** Directory relative paths are resolved from (pass __dirname). */
  dir: string;
  /** The site's site.json. */
  siteJson: string;
  /** The site's gate config module (exports `config: GateConfig`). */
  gateConfig: string;
  /** The site's own specs; omit when it has none. */
  siteSpecsDir?: string;
  /** Anything else (webServer for a run without TEST_URL, timeouts); merged last. */
  overrides?: PlaywrightTestConfig;
}

/** The URL of a webServer override (its url, else http://localhost:<port>); '' when it names none. */
export function webServerUrl(ws: PlaywrightTestConfig['webServer']): string {
  const first = Array.isArray(ws) ? ws[0] : ws;
  if (!first) return '';
  if (first.url) {
    try { return new URL(first.url).origin; } catch { return ''; }
  }
  return first.port ? `http://localhost:${first.port}` : '';
}

/** Chromium flags that keep a local run off the network: only the server under test resolves. */
export function offlineLaunchArgs(testUrl: string): string[] {
  const host = new URL(testUrl).hostname;
  const local = /^(localhost|127\.0\.0\.1|\[::1\]|host\.docker\.internal)$/i.test(host);
  return local ? [`--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE ${host}, EXCLUDE localhost, EXCLUDE 127.0.0.1`] : [];
}

export function kitPlaywrightConfig(o: KitPlaywrightOptions): PlaywrightTestConfig {
  const resolve = (p: string): string => (path.isAbsolute(p) ? p : path.resolve(o.dir, p));
  process.env.KIT_GATE_SITE_JSON = resolve(o.siteJson);
  process.env.KIT_GATE_CONFIG = resolve(o.gateConfig);
  // The target: TEST_URL (run-local-suite.sh sets it to the image it started; a
  // production smoke sets the live origin), else the runner's app port, else a
  // webServer the site's overrides start. Never a default port: a run without
  // a target would audit whatever happens to listen there.
  const testUrl = gateTargetUrl(process.env) || webServerUrl(o.overrides?.webServer);
  if (!testUrl) {
    throw new Error('kitPlaywrightConfig: no server to test. Run the gate through tests/run-local-suite.sh (it builds the image, starts it and sets TEST_URL), '
      + 'set TEST_URL to a running server, or pass overrides.webServer to start one.');
  }
  // Every worker (and site.ts in it) reads the same target.
  process.env.TEST_URL = testUrl;
  const args = offlineLaunchArgs(testUrl);
  const projects: PlaywrightTestProject[] = [{ name: 'kit', testDir: KIT_UI_DIR }];
  if (o.siteSpecsDir) projects.push({ name: 'site', testDir: resolve(o.siteSpecsDir) });
  return defineConfig({
    testDir: KIT_UI_DIR,
    // Next to the site's config (the deployer passes --output for its runs).
    outputDir: resolve('test-results'),
    timeout: 30_000,
    retries: 1,
    reporter: [['list']],
    use: {
      baseURL: testUrl,
      screenshot: 'only-on-failure',
      trace: 'retain-on-failure',
      ...(args.length ? { launchOptions: { args } } : {}),
    },
    projects,
    ...o.overrides,
  });
}
