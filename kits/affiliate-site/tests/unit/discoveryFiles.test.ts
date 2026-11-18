/**
 * robots.txt and llms.txt as the gate reads them (tests/ui/lib/robotsTxt.ts,
 * llmsTxt.ts), checked against what the kit itself renders (K2's
 * renderRobotsTxt / renderLlmsTxt): the kit's output must pass, and each way a
 * site can break it must fail.
 */
import * as fs from 'fs';
import * as path from 'path';
import { renderLlmsTxt } from '../../server/src/http/llms';
import { renderRobotsTxt } from '../../server/src/http/robots';
import { parseSiteContext, type SiteContext } from '../../server/src/siteContext';
import { llmsProblems } from '../ui/lib/llmsTxt';
import { KIT_DEFAULT_CRAWLER_USER_AGENTS } from '../../server/src/siteContext';
import { crawlerFloor, isAllowed, parseRobots, robotsGroup, robotsProblems, rulesFor, type RobotsPolicy } from '../ui/lib/robotsTxt';

const KIT = path.resolve(__dirname, '..', '..');
const siteJson = (): Record<string, any> => JSON.parse(fs.readFileSync(path.join(KIT, 'fixture-site', 'site.json'), 'utf8'));
const ctxOf = (edit?: (j: Record<string, any>) => void): SiteContext => {
  const j = siteJson();
  if (edit) edit(j);
  return parseSiteContext(j, { env: {} });
};

function policyFor(ctx: SiteContext): RobotsPolicy {
  return {
    mustAllow: [...ctx.crawlers.userAgents.search, ...ctx.crawlers.userAgents.agent],
    mustBlock: ctx.crawlers.userAgents.training,
    keyPaths: ['/', '/product/B0GATE0001', '/guides/choosing-a-widget'],
    disallowPaths: ctx.crawlers.disallowPaths,
    sitemapUrl: `${ctx.site.canonicalOrigin}/sitemap.xml`,
  };
}

describe('robots.txt parsing (RFC 9309)', () => {
  const txt = [
    'User-agent: *',
    'Disallow: /api/',
    'Allow: /',
    '',
    'User-agent: GPTBot',
    'User-agent: CCBot',
    'Disallow: /',
    '',
    'User-agent: Claude-User',
    'Disallow: /private',
    'Allow: /private/ok$',
    'Disallow: /*.pdf$',
    'Sitemap: https://x.test/sitemap.xml',
  ].join('\n');

  it('groups consecutive user-agent lines and reads sitemaps', () => {
    const p = parseRobots(txt);
    expect(p.groups.map((g) => g.agents)).toEqual([['*'], ['GPTBot', 'CCBot'], ['Claude-User']]);
    expect(p.sitemaps).toEqual(['https://x.test/sitemap.xml']);
  });

  it('picks the own group case-insensitively, else *', () => {
    const p = parseRobots(txt);
    expect(rulesFor(p, 'ccbot').from).toBe('own');
    expect(rulesFor(p, 'Googlebot').from).toBe('*');
    expect(rulesFor(parseRobots('User-agent: X\nDisallow: /'), 'Y').from).toBe('none');
  });

  it('applies longest match, Allow on ties, * and $ wildcards', () => {
    const own = rulesFor(parseRobots(txt), 'Claude-User').rules;
    expect(isAllowed(own, '/guides/a')).toBe(true);
    expect(isAllowed(own, '/private/x')).toBe(false);
    expect(isAllowed(own, '/private/ok')).toBe(true);
    expect(isAllowed(own, '/private/ok/more')).toBe(false);
    expect(isAllowed(own, '/files/manual.pdf')).toBe(false);
    expect(isAllowed(own, '/files/manual.pdf?x=1')).toBe(true);
    expect(isAllowed([{ allow: false, path: '/a' }, { allow: true, path: '/a' }], '/a')).toBe(true);
    expect(robotsGroup(txt, 'gptbot')).toEqual(['/']);
    expect(robotsGroup(txt, 'Bingbot')).toBeNull();
  });
});

describe('robotsProblems', () => {
  it('passes the kit\'s robots.txt for the fixture site', () => {
    const ctx = ctxOf();
    expect(robotsProblems(renderRobotsTxt(ctx), policyFor(ctx))).toEqual([]);
  });

  it('fails when assistants are blocked and training crawlers let in (the violations overlay)', () => {
    const good = ctxOf();
    const bad = ctxOf((j) => { j.crawlers.policy = { search: 'allow', agent: 'block', training: 'allow' }; });
    const out = robotsProblems(renderRobotsTxt(bad), policyFor(good)).join('\n');
    expect(out).toMatch(/Claude-User \(its own group\) is blocked from \/, \/product\/B0GATE0001/);
    expect(out).toMatch(/training crawler GPTBot is not blocked \(its own group allows \/\)/);
  });

  it('fails when a training crawler has no group of its own, the private paths are open, or the sitemap is missing', () => {
    const ctx = ctxOf();
    const txt = 'User-agent: *\nAllow: /\n';
    const out = robotsProblems(txt, policyFor(ctx)).join('\n');
    expect(out).toMatch(/training crawler GPTBot is not blocked \(no group of its own; the \* group lets it in\)/);
    expect(out).toMatch(/\/api\/ is not disallowed for User-agent: \*/);
    expect(out).toMatch(/no "Sitemap: https:\/\/gate-fixture\.test\/sitemap\.xml" line/);
    expect(robotsProblems('User-agent: Googlebot\nAllow: /\n', policyFor(ctx)).join('\n')).toMatch(/no User-agent: \* group/);
  });

  it('fails when an allowed crawler\'s own group does not repeat the private paths (groups do not inherit from *)', () => {
    const ctx = ctxOf();
    const txt = renderRobotsTxt(ctx).replace(/(User-agent: Googlebot[\s\S]*?)Disallow: \/r\/\n/, '$1');
    expect(txt).not.toBe(renderRobotsTxt(ctx));
    expect(robotsProblems(txt, policyFor(ctx)).join('\n')).toMatch(/\/r\/ not disallowed for Googlebot \(its own group does not inherit the \* group's Disallow lines\)/);
    // A crawler that falls back to the * group is judged by it, once.
    expect(robotsProblems('User-agent: *\nDisallow: /api/\nDisallow: /r/\nAllow: /\nSitemap: https://gate-fixture.test/sitemap.xml\n', { ...policyFor(ctx), mustBlock: [] })).toEqual([]);
  });

  it('a site that lets training crawlers in (with a reason) is not held to blocking them', () => {
    const ctx = ctxOf((j) => { j.crawlers.policy = { search: 'allow', agent: 'allow', training: 'allow' }; });
    expect(robotsProblems(renderRobotsTxt(ctx), { ...policyFor(ctx), mustBlock: [] })).toEqual([]);
  });
});

describe('crawlerFloor (the kit\'s crawler lists are the floor)', () => {
  it('is the kit lists when site.json names none', () => {
    const f = crawlerFloor(ctxOf().crawlers.userAgents, KIT_DEFAULT_CRAWLER_USER_AGENTS);
    expect(f.byClass.training.sort()).toEqual([...KIT_DEFAULT_CRAWLER_USER_AGENTS.training].sort());
    expect(f.reclassified).toEqual([]);
  });

  it('keeps a kit token the site leaves out, so dropping GPTBot cannot take it out of the check (review round 1)', () => {
    const ctx = ctxOf((j) => { j.crawlers.userAgents = { training: ['CCBot'] }; });
    expect(ctx.crawlers.userAgents.training).toEqual(['CCBot']);
    const f = crawlerFloor(ctx.crawlers.userAgents, KIT_DEFAULT_CRAWLER_USER_AGENTS);
    expect(f.byClass.training).toContain('GPTBot');
    // The site's robots.txt (rendered from its own short list) leaves GPTBot on the * group: the floor catches it.
    const out = robotsProblems(renderRobotsTxt(ctx), { ...policyFor(ctx), mustAllow: [...f.byClass.search, ...f.byClass.agent], mustBlock: f.byClass.training }).join('\n');
    expect(out).toMatch(/training crawler GPTBot is not blocked \(no group of its own; the \* group lets it in\)/);
  });

  it('follows a token the site moves to another class, and reports the move; adds the site\'s own tokens', () => {
    const ctx = ctxOf((j) => {
      j.crawlers.userAgents = {
        search: [...KIT_DEFAULT_CRAWLER_USER_AGENTS.search, 'Amazonbot', 'NewSearchBot'],
        training: KIT_DEFAULT_CRAWLER_USER_AGENTS.training.filter((t) => t !== 'Amazonbot'),
      };
    });
    const f = crawlerFloor(ctx.crawlers.userAgents, KIT_DEFAULT_CRAWLER_USER_AGENTS);
    expect(f.byClass.search).toEqual(expect.arrayContaining(['Amazonbot', 'NewSearchBot']));
    expect(f.byClass.training).not.toContain('Amazonbot');
    expect(f.reclassified).toEqual(['Amazonbot: training -> search']);
    expect(robotsProblems(renderRobotsTxt(ctx), { ...policyFor(ctx), mustAllow: [...f.byClass.search, ...f.byClass.agent], mustBlock: f.byClass.training })).toEqual([]);
  });
});

describe('llmsProblems', () => {
  const ctx = ctxOf();
  const exp = { brandName: ctx.site.brandName, earnings: ctx.disclosure.earnings, canonicalOrigin: ctx.site.canonicalOrigin };
  const content = JSON.parse(fs.readFileSync(path.join(KIT, 'fixture-site', 'llms.json'), 'utf8'));

  it('passes the kit\'s llms.txt and lists its on-site links for resolving', () => {
    const txt = renderLlmsTxt(ctx, content, { latestArticles: [{ slug: 'choosing-a-widget', title: 'How to Choose', day: '2026-11-01' }] });
    const r = llmsProblems(txt, exp);
    expect(r.problems).toEqual([]);
    expect(r.siteLinks).toEqual(expect.arrayContaining([
      'https://gate-fixture.test/guides',
      'https://gate-fixture.test/guides/choosing-a-widget',
      'https://gate-fixture.test/sitemap.xml',
    ]));
  });

  it('fails a price, a wrong title, no summary, no disclosure and relative links', () => {
    const viol = JSON.parse(fs.readFileSync(path.join(KIT, 'fixture-site', 'llms.violations.json'), 'utf8'));
    expect(llmsProblems(renderLlmsTxt(ctx, viol), exp).problems.join('\n')).toMatch(/line 3 quotes a price/);
    const out = llmsProblems('# Other\n\nNo summary here.\n\n## Links\n- [x](/relative)\n', exp).problems.join('\n');
    expect(out).toMatch(/expected "# GateFixture"/);
    expect(out).toMatch(/no "> summary" line/);
    expect(out).toMatch(/does not carry the earnings disclosure/);
    expect(out).toMatch(/"\/relative" is not an absolute URL/);
    expect(llmsProblems('', exp).problems).toEqual(['llms.txt is empty']);
  });
});
