/**
 * Where the gate points, resolved from the environment. No side effects and
 * no imports, so tests/playwright.base.ts can use it before it sets TEST_URL
 * (lib/site.ts computes BASE_URL once, when it is first imported).
 */

/**
 * TEST_URL, else the app the runner started (SUITE_APP_HOST:SUITE_APP_PORT,
 * which run-local-suite.sh exports); '' when neither is set. There is no
 * default port: a gate that audits whatever happens to listen on one is not a
 * gate.
 */
export function gateTargetUrl(env: NodeJS.ProcessEnv = process.env): string {
  const explicit = String(env.TEST_URL ?? '').trim();
  if (explicit) return explicit.replace(/\/+$/, '');
  const port = String(env.SUITE_APP_PORT ?? '').trim();
  if (/^\d{1,5}$/.test(port)) return `http://${String(env.SUITE_APP_HOST ?? '').trim() || 'localhost'}:${port}`;
  return '';
}

/** The target when none is set (unit tests): it resolves nowhere, so a stray request fails at once instead of reaching a server. */
export const NO_TARGET_URL = 'http://gate-target-not-set.invalid';
