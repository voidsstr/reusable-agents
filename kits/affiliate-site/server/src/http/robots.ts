/**
 * robots.txt from the site's crawler policy.
 *
 * Ported from SpecPicks src/index.ts (`app.get('/robots.txt', ...)`; see
 * unit/http/fixtures/ported-from-http.json), with the policy taken from
 * SiteContext.crawlers instead of a hard-coded list:
 *
 *   search   (Googlebot, Bingbot, OAI-SearchBot, Claude-SearchBot, PerplexityBot, ...)  allow
 *   agent    (ChatGPT-User, Claude-User, Perplexity-User, ...: answer-time fetches)    allow
 *   training (GPTBot, ClaudeBot, CCBot, Google-Extended, Bytespider, Meta-ExternalAgent, ...) block
 *
 * (the kit defaults; site.json can change a class's policy or token list).
 *
 * Kept from upstream: one group per named crawler, because robots.txt groups
 * do NOT inherit from `*` and conservative parsers fall back to blocking a
 * bot that has no group of its own; every allowed group repeats the site's
 * disallowed paths; Disallow lines come before `Allow: /` so first-match
 * parsers and longest-match parsers (RFC 9309) read the same rules; the
 * llms.txt pointer comment and the Sitemap line.
 *
 * Upstream allowed every AI crawler, training ones included. The kit's
 * default blocks training crawlers (design amendment: Search and Agent
 * allowed, Training blocked).
 */
import type { RequestHandler } from 'express';
import { CRAWLER_CLASSES, type CrawlerClass, type SiteContext } from '../siteContext';

export interface RobotsTxtOptions {
  /** Sitemap paths (site-relative or absolute), default ['/sitemap.xml']. */
  sitemaps?: readonly string[];
  /**
   * Extra Disallow paths for one crawler token, e.g. a crawl trap one bot fell
   * into. A token with no class gets its own allowed group.
   */
  perAgentDisallow?: Readonly<Record<string, readonly string[]>>;
  /** Point crawlers at /llms.txt in a comment (default true). */
  mentionLlmsTxt?: boolean;
}

type RobotsCtx = Pick<SiteContext, 'site' | 'crawlers'>;

const CLASS_NOTES: Readonly<Record<CrawlerClass, string>> = Object.freeze({
  search: 'Search indexes that send readers to the page',
  agent: 'Assistants fetching a page while answering a person',
  training: 'Model-training crawlers',
});

/** A path or URL as written in robots.txt: no line breaks, no comment marks. */
function robotsSafe(s: string): string {
  return String(s).replace(/[\r\n#]/g, '').trim();
}

function absolute(ctx: Pick<SiteContext, 'site'>, pathOrUrl: string): string {
  const s = robotsSafe(pathOrUrl);
  return /^https?:\/\//i.test(s) ? s : `${ctx.site.canonicalOrigin}${s.startsWith('/') ? '' : '/'}${s}`;
}

function allowedGroup(tokens: readonly string[], disallow: readonly string[]): string {
  const lines = tokens.map((t) => `User-agent: ${robotsSafe(t)}`);
  for (const p of new Set(disallow.map(robotsSafe).filter(Boolean))) lines.push(`Disallow: ${p}`);
  lines.push('Allow: /');
  return lines.join('\n');
}

function blockedGroup(token: string): string {
  return `User-agent: ${robotsSafe(token)}\nDisallow: /`;
}

/** The robots.txt text for a site. Deterministic for a given context and options. */
export function renderRobotsTxt(ctx: RobotsCtx, opts: RobotsTxtOptions = {}): string {
  const disallow = ctx.crawlers.disallowPaths;
  const extra = opts.perAgentDisallow ?? {};
  const extraFor = (token: string): readonly string[] => {
    const key = Object.keys(extra).find((k) => k.toLowerCase() === token.toLowerCase());
    return key ? extra[key] ?? [] : [];
  };
  const named = new Set<string>();
  const out: string[] = [];

  const allowed = CRAWLER_CLASSES.filter((c) => ctx.crawlers.policy[c] === 'allow');
  const blocked = CRAWLER_CLASSES.filter((c) => ctx.crawlers.policy[c] === 'block');

  out.push(`# robots.txt for ${robotsSafe(ctx.site.brandName)} (${ctx.site.canonicalOrigin})`);
  out.push(`# Allowed: ${allowed.map((c) => CLASS_NOTES[c].toLowerCase()).join('; ') || 'none'}.`);
  out.push(`# Blocked: ${blocked.map((c) => CLASS_NOTES[c].toLowerCase()).join('; ') || 'none'}.`);
  out.push('');
  out.push(allowedGroup(['*'], [...disallow, ...extraFor('*')]));
  named.add('*');

  for (const cls of CRAWLER_CLASSES) {
    const tokens = ctx.crawlers.userAgents[cls].filter((t) => !named.has(t.toLowerCase()));
    if (!tokens.length) continue;
    out.push('');
    out.push(`# ${CLASS_NOTES[cls]}: ${ctx.crawlers.policy[cls]}`);
    for (const t of tokens) {
      named.add(t.toLowerCase());
      out.push(ctx.crawlers.policy[cls] === 'allow' ? allowedGroup([t], [...disallow, ...extraFor(t)]) : blockedGroup(t));
    }
  }

  const orphans = Object.keys(extra).filter((t) => !named.has(t.toLowerCase()));
  if (orphans.length) {
    out.push('');
    out.push('# Other crawlers with their own rules');
    for (const t of orphans) {
      named.add(t.toLowerCase());
      out.push(allowedGroup([t], [...disallow, ...(extra[t] ?? [])]));
    }
  }

  out.push('');
  if (opts.mentionLlmsTxt ?? true) {
    out.push('# Machine-readable site guide for AI agents (llmstxt.org):');
    out.push(`#   ${ctx.site.canonicalOrigin}/llms.txt`);
  }
  for (const s of opts.sitemaps ?? ['/sitemap.xml']) out.push(`Sitemap: ${absolute(ctx, s)}`);
  return `${out.join('\n')}\n`;
}

/** Cache for robots.txt and llms.txt: no prices in them, so longer than HTML. */
export const DISCOVERY_FILE_CACHE_CONTROL = 'public, max-age=3600';

/** GET /robots.txt. The text is rendered once (the policy is fixed for the process). */
export function robotsTxtHandler(ctx: RobotsCtx, opts: RobotsTxtOptions = {}): RequestHandler {
  const body = renderRobotsTxt(ctx, opts);
  return (_req, res) => {
    res.set('Content-Type', 'text/plain; charset=utf-8');
    res.set('Cache-Control', DISCOVERY_FILE_CACHE_CONTROL);
    res.send(body);
  };
}
