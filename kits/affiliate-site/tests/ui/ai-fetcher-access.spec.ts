import { test, expect, type APIRequestContext } from '@playwright/test';
import { loadGateConfig } from './lib/gateConfig';
import type { AiFetcher } from './lib/manifestTypes';
import { KIT_DEFAULT_CRAWLER_USER_AGENTS } from '../../server/src/siteContext';
import { crawlerFloor, isAllowed, parseRobots, rulesFor } from './lib/robotsTxt';
import { BASE_URL, gateSite } from './lib/site';
import { samplesFor } from './lib/sources';

// Answer-time AI fetchers must get the same page a browser gets.
//
// Ported from SpecPicks tests/ui/ai-fetcher-access.spec.ts. ChatGPT-User,
// Claude-User and Perplexity-User fetch a page while a person waits for an
// answer, so a block there costs a citation immediately. This fails if a
// fetcher is served a different status than a browser, a challenge page, or
// a robots.txt group that shuts it out of a page family.
//
// Against production (it is @prod-safe: the CDN in front of the live site is
// where a fetcher gets blocked) it only GETs pages, but it is NOT write-free:
// every fetch it makes with an assistant's User-Agent is logged by the kit's
// AI-traffic middleware as a row in ai_traffic_log (a few dozen rows a run).
// The readers that count AI traffic (framework/core/ai_traffic.py) drop rows
// from the addresses in AI_TRAFFIC_EXCLUDE_IPS, so the host that runs the
// production smoke must be listed there, or its runs count as assistant visits.

const cfg = loadGateConfig();

const DEFAULT_FETCHERS: AiFetcher[] = [
  { token: 'ChatGPT-User', ua: 'Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; ChatGPT-User/1.0; +https://openai.com/bot' },
  { token: 'Claude-User', ua: 'Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; Claude-User/1.0; +Claude-User@anthropic.com)' },
  { token: 'Perplexity-User', ua: 'Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; Perplexity-User/1.0; +https://perplexity.ai/perplexity-user)' },
];
const FETCHERS = cfg.aiFetchers?.fetchers ?? DEFAULT_FETCHERS;
const BROWSER_UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36';
const CHALLENGE = /cf-chl|challenge-platform|Just a moment\.\.\.|Attention Required|captcha/i;

/** '/', plus the first sample of every family that answers 200 (or the config's list). */
async function pathsToCheck(request: APIRequestContext): Promise<string[]> {
  if (cfg.aiFetchers?.paths) return cfg.aiFetchers.paths({ request });
  const out = new Set<string>(['/']);
  for (const fam of cfg.families) {
    if ((fam.status ?? 200) !== 200) continue;
    const first = (await samplesFor(fam, request))[0];
    if (first) out.add(first);
  }
  return [...out];
}

test.describe('AI answer-time fetchers @prod-safe', () => {
  test.describe.configure({ mode: 'serial' });

  test('robots.txt lets the assistants\' answer-time fetchers read every page family', async ({ request }) => {
    const r = await request.get(`${BASE_URL}/robots.txt`);
    expect(r.status()).toBe(200);
    const parsed = parseRobots(await r.text());
    const paths = (await pathsToCheck(request)).map((p) => p.split('?')[0] as string);
    // The kit's assistant tokens too, unless the site moved one to another class: a site cannot drop one from the check.
    const floor = crawlerFloor(gateSite().ctx.crawlers.userAgents, KIT_DEFAULT_CRAWLER_USER_AGENTS);
    const agentTokens = new Set([...floor.byClass.agent, ...FETCHERS.map((f) => f.token)]);
    const problems: string[] = [];
    for (const token of agentTokens) {
      const { from, rules } = rulesFor(parsed, token);
      for (const p of paths) if (!isAllowed(rules, p)) problems.push(`${token} (${from === 'own' ? 'its own group' : 'the * group'}) is disallowed ${p}`);
    }
    expect(problems, problems.join('\n')).toEqual([]);
  });

  for (const { token, ua } of FETCHERS) {
    test(`${token} gets the same status as a browser on every page family`, async ({ playwright, request }) => {
      test.setTimeout(120_000);
      const paths = await pathsToCheck(request);
      const bot = await playwright.request.newContext({ extraHTTPHeaders: { 'User-Agent': ua } });
      const human = await playwright.request.newContext({ extraHTTPHeaders: { 'User-Agent': BROWSER_UA } });
      try {
        for (const path of paths) {
          const [b, h] = await Promise.all([
            bot.get(`${BASE_URL}${path}`, { maxRedirects: 0, timeout: 20_000 }),
            human.get(`${BASE_URL}${path}`, { maxRedirects: 0, timeout: 20_000 }),
          ]);
          expect(b.status(), `${token} ${path}: ${b.status()} vs browser ${h.status()}`).toBe(h.status());
          if (path === '/') expect(b.status(), `${token} homepage`).toBe(200);
          if (b.status() === 200) {
            const body = await b.text();
            expect(CHALLENGE.test(body.slice(0, 4000)), `${token} ${path} served a challenge page`).toBe(false);
          }
        }
      } finally {
        await bot.dispose();
        await human.dispose();
      }
    });
  }
});
