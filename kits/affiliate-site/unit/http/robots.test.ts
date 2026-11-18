import express from 'express';
import request from 'supertest';
import { DISCOVERY_FILE_CACHE_CONTROL, renderRobotsTxt, robotsTxtHandler } from '../../server/src/http/robots';
import { KIT_DEFAULT_CRAWLER_USER_AGENTS } from '../../server/src/siteContext';
import { exampleCtx } from './helpers';

interface Group { agents: string[]; rules: Array<{ allow: boolean; path: string }> }

/** robots.txt groups as RFC 9309 reads them (consecutive User-agent lines open one group). */
function parseRobots(text: string): { groups: Group[]; sitemaps: string[] } {
  const groups: Group[] = [];
  const sitemaps: string[] = [];
  let current: Group | null = null;
  let lastWasAgent = false;
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.replace(/#.*/, '').trim();
    if (!line) continue;
    const m = line.match(/^([A-Za-z-]+)\s*:\s*(.*)$/);
    if (!m) continue;
    const key = (m[1] as string).toLowerCase();
    const value = (m[2] as string).trim();
    if (key === 'user-agent') {
      if (!current || !lastWasAgent) {
        current = { agents: [], rules: [] };
        groups.push(current);
      }
      current.agents.push(value.toLowerCase());
      lastWasAgent = true;
      continue;
    }
    lastWasAgent = false;
    if (key === 'sitemap') sitemaps.push(value);
    else if ((key === 'allow' || key === 'disallow') && current) current.rules.push({ allow: key === 'allow', path: value });
  }
  return { groups, sitemaps };
}

/** RFC 9309 matching: the crawler's own group (else '*'), longest rule wins, Allow wins a tie. */
function isAllowed(text: string, token: string, path: string): boolean {
  const { groups } = parseRobots(text);
  const own = groups.filter((g) => g.agents.includes(token.toLowerCase()));
  const chosen = own.length ? own : groups.filter((g) => g.agents.includes('*'));
  const rules = chosen.flatMap((g) => g.rules).filter((r) => r.path && path.startsWith(r.path));
  if (!rules.length) return true;
  rules.sort((a, b) => b.path.length - a.path.length || Number(b.allow) - Number(a.allow));
  return (rules[0] as { allow: boolean }).allow;
}

/** The first rule that matches, as a first-match parser reads a group. */
function firstMatchAllowed(text: string, token: string, path: string): boolean {
  const { groups } = parseRobots(text);
  const g = groups.find((x) => x.agents.includes(token.toLowerCase())) ?? groups.find((x) => x.agents.includes('*'));
  const rule = g?.rules.find((r) => r.path && path.startsWith(r.path));
  return rule ? rule.allow : true;
}

const ctx = exampleCtx();
const text = renderRobotsTxt(ctx);

const ALLOWED = ['OAI-SearchBot', 'ChatGPT-User', 'Claude-User', 'Claude-SearchBot', 'PerplexityBot', 'Perplexity-User', 'Bingbot', 'Googlebot'];
const BLOCKED = ['GPTBot', 'ClaudeBot', 'CCBot', 'Google-Extended', 'Bytespider', 'Meta-ExternalAgent'];
const PAGES = ['/', '/product/B0DS2X13PH', '/guides/best-power-stations', '/llms.txt'];

describe('renderRobotsTxt (kit default policy)', () => {
  it.each(ALLOWED)('lets %s read every page, and keeps it out of /api/ and /r/', (bot) => {
    for (const p of PAGES) {
      expect(isAllowed(text, bot, p)).toBe(true);
      expect(firstMatchAllowed(text, bot, p)).toBe(true);
    }
    for (const p of ['/api/outbound-click', '/r/B0DS2X13PH']) {
      expect(isAllowed(text, bot, p)).toBe(false);
      expect(firstMatchAllowed(text, bot, p)).toBe(false);
    }
  });

  it.each(BLOCKED)('blocks the training crawler %s from the whole site', (bot) => {
    for (const p of PAGES) expect(isAllowed(text, bot, p)).toBe(false);
    const own = parseRobots(text).groups.filter((g) => g.agents.includes(bot.toLowerCase()));
    expect(own).toEqual([{ agents: [bot.toLowerCase()], rules: [{ allow: false, path: '/' }] }]);
  });

  it('gives every configured crawler token exactly one group of its own', () => {
    const { groups } = parseRobots(text);
    const all = [...ctx.crawlers.userAgents.search, ...ctx.crawlers.userAgents.agent, ...ctx.crawlers.userAgents.training];
    expect(all).toEqual([
      ...KIT_DEFAULT_CRAWLER_USER_AGENTS.search, ...KIT_DEFAULT_CRAWLER_USER_AGENTS.agent, ...KIT_DEFAULT_CRAWLER_USER_AGENTS.training,
    ]);
    for (const token of [...all, '*']) {
      expect(groups.filter((g) => g.agents.includes(token.toLowerCase()))).toHaveLength(1);
    }
    for (const g of groups) expect(g.agents).toHaveLength(1);
  });

  it('lets unknown crawlers read pages but not the API or the redirect', () => {
    expect(isAllowed(text, 'SomeNewSearchBot', '/product/B0DS2X13PH')).toBe(true);
    expect(isAllowed(text, 'SomeNewSearchBot', '/api/x')).toBe(false);
    expect(firstMatchAllowed(text, 'SomeNewSearchBot', '/r/B0DS2X13PH')).toBe(false);
  });

  it('points at the sitemap (absolute) and llms.txt', () => {
    expect(parseRobots(text).sitemaps).toEqual(['https://example.com/sitemap.xml']);
    expect(text).toContain('#   https://example.com/llms.txt');
    expect(text.endsWith('\n')).toBe(true);
  });

  it('is deterministic', () => {
    expect(renderRobotsTxt(exampleCtx())).toBe(text);
  });
});

describe('renderRobotsTxt follows site.json', () => {
  it('a class policy flip changes every token in it', () => {
    const open = exampleCtx((j) => { j.crawlers.policy = { search: 'allow', agent: 'block', training: 'allow' }; });
    const t = renderRobotsTxt(open);
    expect(isAllowed(t, 'GPTBot', '/guides/x')).toBe(true);
    expect(isAllowed(t, 'GPTBot', '/api/x')).toBe(false);
    expect(isAllowed(t, 'Claude-User', '/guides/x')).toBe(false);
    expect(isAllowed(t, 'Googlebot', '/guides/x')).toBe(true);
  });

  it('uses the site\'s own token lists and disallowed paths', () => {
    const c = exampleCtx((j) => {
      j.crawlers.userAgents = { training: ['GPTBot'] };
      j.crawlers.disallowPaths = ['/api/', '/r/', '/search?'];
    });
    const t = renderRobotsTxt(c);
    expect(isAllowed(t, 'Googlebot', '/search?q=x')).toBe(false);
    expect(isAllowed(t, 'Googlebot', '/search')).toBe(true);
    expect(parseRobots(t).groups.some((g) => g.agents.includes('ccbot'))).toBe(false);
    expect(parseRobots(t).groups.some((g) => g.agents.includes('googlebot'))).toBe(true);
  });

  it('adds per-crawler traps without touching other crawlers', () => {
    const t = renderRobotsTxt(ctx, { perAgentDisallow: { perplexitybot: ['/compare/'], '*': ['/tmp/'], NewsBot: ['/drafts/'] } });
    expect(isAllowed(t, 'PerplexityBot', '/compare/a-vs-b')).toBe(false);
    expect(isAllowed(t, 'Googlebot', '/compare/a-vs-b')).toBe(true);
    expect(isAllowed(t, 'UnknownBot', '/tmp/x')).toBe(false);
    expect(isAllowed(t, 'NewsBot', '/drafts/x')).toBe(false);
    expect(isAllowed(t, 'NewsBot', '/guides/x')).toBe(true);
    expect(parseRobots(t).groups.filter((g) => g.agents.includes('*'))).toHaveLength(1);
  });

  it('cannot be broken by a line break in an option', () => {
    const t = renderRobotsTxt(ctx, { perAgentDisallow: { Googlebot: ['/x\nUser-agent: *\nDisallow: /'] }, sitemaps: ['/a.xml\nAllow: /'] });
    expect(isAllowed(t, 'Googlebot', '/guides/y')).toBe(true);
    expect(isAllowed(t, 'UnknownBot', '/guides/y')).toBe(true);
    expect(parseRobots(t).sitemaps).toEqual(['https://example.com/a.xmlAllow: /']);
  });

  it('takes several sitemaps and can drop the llms.txt comment', () => {
    const t = renderRobotsTxt(ctx, { sitemaps: ['/sitemap.xml', 'https://cdn.example.com/s2.xml'], mentionLlmsTxt: false });
    expect(parseRobots(t).sitemaps).toEqual(['https://example.com/sitemap.xml', 'https://cdn.example.com/s2.xml']);
    expect(t).not.toContain('llms.txt');
  });
});

describe('robotsTxtHandler', () => {
  it('serves the text as UTF-8 plain text with a modest cache', async () => {
    const app = express();
    app.get('/robots.txt', robotsTxtHandler(ctx));
    const res = await request(app).get('/robots.txt').expect(200);
    expect(res.headers['content-type']).toBe('text/plain; charset=utf-8');
    expect(res.headers['cache-control']).toBe(DISCOVERY_FILE_CACHE_CONTROL);
    expect(res.text).toBe(text);
  });
});
