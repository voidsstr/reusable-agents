/**
 * ai_traffic_log rows say whether their user-agent can be believed. Ported
 * from SpecPicks tests/unit/aiCrawlerVerify.test.ts (classification, client
 * address, verdicts, Anthropic strictness, vendor JSON), plus the kit's
 * static Anthropic ranges, the injected-fetch refresh and the SiteContext
 * factory. No network: every list is installed or served by a fake fetch.
 */
import * as fs from 'fs';
import * as path from 'path';
import {
  AI_CRAWLER_SOURCES,
  AiCrawlerVerifier,
  CDN_EDGE_RANGES,
  CRAWLER_RANGE_LISTS,
  KIT_DEFAULT_STATIC_RANGES,
  MIN_STATIC_RANGE_PREFIX,
  MIN_TRUSTED_PROXY_PREFIX,
  cidrPolicyIssue,
  classifyAiTraffic,
  clientIp,
  createAiCrawlerVerifier,
  crawlerVerifyUserAgent,
  edgeProxyMatcher,
  isPrivateIp,
  normalizeIp,
  parseAddressList,
  parseCidr,
  rangeListFromJson,
  siteClientIp,
  trustedProxyMatcher,
  type RangeFetch,
} from '../server/src/aiCrawlerVerify';
import { loadSiteContext, parseSiteContext } from '../server/src/siteContext';

const EXAMPLE = path.resolve(__dirname, '../fixtures/site.example.json');
const ctx = loadSiteContext(EXAMPLE, { env: {} });
/** The example site behind Cloudflare (site.json edge.cdn). */
const cfCtx = (() => {
  const j = JSON.parse(fs.readFileSync(EXAMPLE, 'utf8'));
  j.edge = { cdn: 'cloudflare' };
  return parseSiteContext(j, { env: {} });
})();
/** A Cloudflare edge address (172.64.0.0/13) and an IPv6 one (2606:4700::/32). */
const CF_EDGE = '172.70.42.17';
const CF_EDGE_V6 = '2606:4700:10::6816:1';
/** An address inside the OpenAI chatgpt-user list installed below. */
const CHATGPT_IP = '23.98.142.180';
/** A cross-zone Cloudflare Worker subrequest's documented source address (inside 2a06:98c0::/29). */
const CF_WORKER = '2a06:98c0:3600::103';
const HOME = '73.12.1.9';

describe('classifyAiTraffic', () => {
  it('files Claude\'s answer-time fetchers before the training crawler', () => {
    expect(classifyAiTraffic('Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; Claude-SearchBot/1.0; +claudebot@anthropic.com)', undefined))
      .toEqual({ kind: 'crawler', source: 'claude-searchbot' });
    expect(classifyAiTraffic('Mozilla/5.0 (compatible; Claude-User/1.0; +Claude-User@anthropic.com)', undefined))
      .toEqual({ kind: 'crawler', source: 'claude-user' });
    expect(classifyAiTraffic('Claude-User (claude-code/2.0.14; +https://support.anthropic.com/)', undefined))
      .toEqual({ kind: 'crawler', source: 'claude-code' });
    expect(classifyAiTraffic('Mozilla/5.0 (compatible; ClaudeBot/1.0; +claudebot@anthropic.com)', undefined))
      .toEqual({ kind: 'crawler', source: 'claudebot' });
  });

  it('keeps OpenAI / Perplexity / Bing and adds DuckAssist + Mistral; robots-only tokens are not UAs', () => {
    expect(classifyAiTraffic('Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; ChatGPT-User/1.0; +https://openai.com/bot', undefined)?.source).toBe('chatgpt-user');
    expect(classifyAiTraffic('Mozilla/5.0; compatible; OAI-SearchBot/1.0; +https://openai.com/searchbot', undefined)?.source).toBe('oai-searchbot');
    expect(classifyAiTraffic('Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)', undefined)?.source).toBe('bingbot');
    expect(classifyAiTraffic('DuckAssistBot/1.2; (+http://duckduckgo.com/duckassistbot.html)', undefined)?.source).toBe('duckassistbot');
    expect(classifyAiTraffic('MistralAI-User/1.0', undefined)?.source).toBe('mistralai-user');
    expect(classifyAiTraffic('Google-Extended', undefined)).toBeNull();
    expect(classifyAiTraffic('Mozilla/5.0 (Windows NT 10.0)', 'https://chatgpt.com/')).toEqual({ kind: 'referral', source: 'chatgpt' });
  });

  it('files a no-Referer landing by its assistant utm_source; a crawler UA still wins', () => {
    expect(classifyAiTraffic('Mozilla/5.0 (iPhone)', undefined, 'chatgpt.com')).toEqual({ kind: 'referral', source: 'chatgpt' });
    expect(classifyAiTraffic('Mozilla/5.0 (iPhone)', undefined, ' Perplexity ')).toEqual({ kind: 'referral', source: 'perplexity' });
    expect(classifyAiTraffic('Mozilla/5.0 (iPhone)', 'https://chatgpt.com/', 'newsletter')?.source).toBe('chatgpt');
    expect(classifyAiTraffic('Mozilla/5.0 (iPhone)', undefined, 'newsletter')).toBeNull();
    expect(classifyAiTraffic('Mozilla/5.0 (iPhone)', undefined, 'notchatgpt.com')).toBeNull();
    expect(classifyAiTraffic('Mozilla/5.0; compatible; ChatGPT-User/1.0', undefined, 'chatgpt.com')?.kind).toBe('crawler');
  });

  it('a utm_source named like an Object.prototype member is not a referral (SpecPicks returned a function)', () => {
    expect(classifyAiTraffic('Mozilla/5.0 (iPhone)', undefined, 'constructor')).toBeNull();
    expect(classifyAiTraffic('Mozilla/5.0 (iPhone)', undefined, '__proto__')).toBeNull();
  });

  it('ignores a malformed referer', () => {
    expect(classifyAiTraffic('Mozilla/5.0', 'not a url')).toBeNull();
  });

  it('lists every crawler source once', () => {
    expect(AI_CRAWLER_SOURCES).toContain('claude-user');
    expect(new Set(AI_CRAWLER_SOURCES).size).toBe(AI_CRAWLER_SOURCES.length);
  });

  it('files Amazon\'s search and answer-time crawlers apart from Amazonbot (developer.amazon.com/amazonbot)', () => {
    const ua = (token: string) => `Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; ${token}) Chrome/119.0.6045.214 Safari/537.36`;
    expect(classifyAiTraffic(ua('Amzn-SearchBot/0.1'), undefined)).toEqual({ kind: 'crawler', source: 'amzn-searchbot' });
    expect(classifyAiTraffic(ua('Amzn-User/0.1'), undefined)).toEqual({ kind: 'crawler', source: 'amzn-user' });
    expect(classifyAiTraffic(ua('Amazonbot/0.1; +https://developer.amazon.com/support/amazonbot'), undefined)).toEqual({ kind: 'crawler', source: 'amazonbot' });
    expect(AI_CRAWLER_SOURCES).toEqual(expect.arrayContaining(['amzn-searchbot', 'amzn-user', 'amazonbot']));
    // Amazon publishes their addresses as HTML pages, not vendor JSON: no list, so unknown.
    expect(new AiCrawlerVerifier().verify('crawler', 'amzn-user', '52.70.240.171')).toEqual({ verified: null, check: 'no-published-list' });
  });
});

describe('clientIp', () => {
  it('ignores what the client put in X-Forwarded-For; uses what our edge appended', () => {
    expect(clientIp('203.0.113.9,108.80.179.176', '100.100.0.5')).toBe('108.80.179.176');
    expect(clientIp('71.131.215.88, 100.100.0.67', '100.100.0.2')).toBe('71.131.215.88');
    expect(clientIp(['203.0.113.9', '108.80.179.176'], null)).toBe('108.80.179.176');
    expect(clientIp('', '::ffff:18.97.9.97')).toBe('18.97.9.97');
    expect(clientIp(undefined, '10.0.0.4')).toBe('10.0.0.4');
    expect(clientIp('10.1.1.1', undefined)).toBe('10.1.1.1');
    expect(normalizeIp('[2a06:98c0:3600::103]:443')).toBe('2a06:98c0:3600::103');
    expect(normalizeIp('203.0.113.9:8080')).toBe('203.0.113.9');
    expect(normalizeIp('not-an-ip')).toBe('');
  });

  it('knows private, CGNAT and loopback space', () => {
    expect(isPrivateIp('10.2.3.4')).toBe(true);
    expect(isPrivateIp('100.64.0.1')).toBe(true);
    expect(isPrivateIp('::1')).toBe(true);
    expect(isPrivateIp('fe80::1')).toBe(true);
    expect(isPrivateIp('8.8.8.8')).toBe(false);
    expect(isPrivateIp('junk')).toBe(true);
  });
});

describe('clientIp behind a proxying CDN (one trusted hop)', () => {
  const cf = trustedProxyMatcher(CDN_EDGE_RANGES.cloudflare.cidrs);

  it('client, edge: the client; without the CDN\'s ranges every request looked like the edge', () => {
    // visitor -> Cloudflare (appends the visitor) -> Azure (appends the edge it saw)
    expect(clientIp(`${CHATGPT_IP}, ${CF_EDGE}`, '100.100.0.5', cf)).toBe(CHATGPT_IP);
    expect(clientIp(`${CHATGPT_IP}, ${CF_EDGE}`, '100.100.0.5')).toBe(CF_EDGE);
    expect(clientIp(`${CHATGPT_IP}, ${CF_EDGE}`, '100.100.0.5', null)).toBe(CF_EDGE);
  });

  it('ignores whatever the client prepended, and the private hops our platform appended', () => {
    expect(clientIp(`1.2.3.4, 5.6.7.8, ${CHATGPT_IP}, ${CF_EDGE}`, '10.0.0.1', cf)).toBe(CHATGPT_IP);
    expect(clientIp(`${CHATGPT_IP}, ${CF_EDGE}, 100.100.0.67`, '100.100.0.2', cf)).toBe(CHATGPT_IP);
    expect(clientIp([CHATGPT_IP, CF_EDGE], null, cf)).toBe(CHATGPT_IP);
  });

  it('a request that bypasses the CDN is judged by the address the platform saw, whatever it claims', () => {
    // Direct to the origin with a forged chain (and, say, a forged CF-Connecting-IP, which is never read).
    expect(clientIp(`${CHATGPT_IP}, ${CF_EDGE}, ${HOME}`, '10.0.0.1', cf)).toBe(HOME);
    expect(clientIp(`${CHATGPT_IP}, ${HOME}`, '10.0.0.1', cf)).toBe(HOME);
    expect(clientIp(HOME, '10.0.0.1', cf)).toBe(HOME);
  });

  it('skips exactly one hop: a cross-zone Cloudflare Worker cannot choose its own address', () => {
    // The Worker's forged entry, then the Worker's own (Cloudflare-owned) address, then the edge.
    expect(trustedProxyMatcher(CDN_EDGE_RANGES.cloudflare.cidrs).has(CF_WORKER)).toBe(true);
    expect(clientIp(`${CHATGPT_IP}, ${CF_WORKER}, ${CF_EDGE}`, '100.100.0.5', cf)).toBe(CF_WORKER);
  });

  it('IPv6 edges and visitors; a request the CDN made itself; no header at all', () => {
    expect(clientIp(`2001:db8:1::5, ${CF_EDGE_V6}`, '10.0.0.1', cf)).toBe('2001:db8:1::5');
    expect(clientIp(`[2001:db8:1::5]:443, ${CF_EDGE}`, '10.0.0.1', cf)).toBe('2001:db8:1::5');
    expect(clientIp(CF_EDGE, '100.100.0.7', cf)).toBe(CF_EDGE);
    expect(clientIp(`10.0.0.9, ${CF_EDGE}`, '100.100.0.7', cf)).toBe(CF_EDGE);
    expect(clientIp(undefined, HOME, cf)).toBe(HOME);
    expect(clientIp(undefined, undefined, cf)).toBe('');
  });
});

describe('CDN edge ranges and site.json edge', () => {
  it('ships Cloudflare\'s published edge ranges (www.cloudflare.com/ips-v4 and /ips-v6, 2026-11-17)', () => {
    expect(CDN_EDGE_RANGES.cloudflare).toEqual({
      cdn: 'cloudflare',
      cidrs: [
        '173.245.48.0/20', '103.21.244.0/22', '103.22.200.0/22', '103.31.4.0/22', '141.101.64.0/18',
        '108.162.192.0/18', '190.93.240.0/20', '188.114.96.0/20', '197.234.240.0/22', '198.41.128.0/17',
        '162.158.0.0/15', '104.16.0.0/13', '104.24.0.0/14', '172.64.0.0/13', '131.0.72.0/22',
        '2400:cb00::/32', '2606:4700::/32', '2803:f800::/32', '2405:b500::/32', '2405:8100::/32',
        '2a06:98c0::/29', '2c0f:f248::/32',
      ],
      evidenceUrls: ['https://www.cloudflare.com/ips-v4', 'https://www.cloudflare.com/ips-v6'],
      verifiedOn: '2026-11-17',
    });
    expect(Object.isFrozen(CDN_EDGE_RANGES.cloudflare.cidrs)).toBe(true);
    for (const c of CDN_EDGE_RANGES.cloudflare.cidrs) expect([c, cidrPolicyIssue(c, MIN_TRUSTED_PROXY_PREFIX)]).toEqual([c, null]);
  });

  it('trustedProxyMatcher takes CIDRs and bare addresses, and throws on a typo', () => {
    const m = trustedProxyMatcher(['198.51.100.1', '2001:db8::/32']);
    expect(m.has('198.51.100.1')).toBe(true);
    expect(m.has('198.51.100.2')).toBe(false);
    expect(m.has('::ffff:198.51.100.1')).toBe(true);
    expect(m.has('2001:db8:ffff::1')).toBe(true);
    expect(m.has('not-an-ip')).toBe(false);
    expect(trustedProxyMatcher([]).has(CF_EDGE)).toBe(false);
    expect(() => trustedProxyMatcher(['cloudflare'])).toThrow(/not an IP address or CIDR/);
    expect(() => trustedProxyMatcher(['10.0.0.0/33'])).toThrow(TypeError);
  });

  it('edgeProxyMatcher follows site.json, built once per SiteContext', () => {
    expect(ctx.edge).toEqual({ cdn: 'none', trustedProxyCidrs: [] });
    expect(edgeProxyMatcher(ctx)).toBeNull();
    const m = edgeProxyMatcher(cfCtx);
    expect(m?.has(CF_EDGE)).toBe(true);
    expect(m?.has(HOME)).toBe(false);
    expect(edgeProxyMatcher(cfCtx)).toBe(m);
  });

  it('siteClientIp: the edge for a site with no CDN, the visitor for a site behind Cloudflare', () => {
    expect(siteClientIp(ctx, `${CHATGPT_IP}, ${CF_EDGE}`, '100.100.0.5')).toBe(CF_EDGE);
    expect(siteClientIp(cfCtx, `${CHATGPT_IP}, ${CF_EDGE}`, '100.100.0.5')).toBe(CHATGPT_IP);
    expect(siteClientIp(cfCtx, `203.0.113.9, ${HOME}`, '100.100.0.5')).toBe(HOME);
  });

  it('through Cloudflare, a crawler is verified with its own address and our host stays excluded', () => {
    const v = createAiCrawlerVerifier(cfCtx, { env: { AI_TRAFFIC_EXCLUDE_IPS: '203.0.113.7' }, fetchImpl: async () => { throw new Error('offline'); } });
    v.setRangeList('openai-chatgpt-user', { prefixes: [{ ipv4Prefix: '23.98.142.176/28' }] });
    const viaCf = siteClientIp(cfCtx, `${CHATGPT_IP}, ${CF_EDGE}`, '100.100.0.5');
    expect(v.verify('crawler', 'chatgpt-user', viaCf)).toEqual({ verified: true, check: 'range:openai-chatgpt-user' });
    // What the unpatched rule logged: the edge, which is not OpenAI's.
    expect(v.verify('crawler', 'chatgpt-user', clientIp(`${CHATGPT_IP}, ${CF_EDGE}`, '100.100.0.5')).verified).toBe(false);
    // The fleet host's audit through Cloudflare is still recognised as ours.
    const audit = siteClientIp(cfCtx, `203.0.113.7, ${CF_EDGE}`, '100.100.0.5');
    expect(v.verify('crawler', 'oai-searchbot', audit)).toEqual({ verified: false, check: 'excluded-ip' });
  });
});

describe('cidrPolicyIssue (static crawler ranges and trusted proxies)', () => {
  it.each<[string, 'static' | 'proxy', RegExp | null]>([
    ['160.79.104.0/21', 'static', null],
    ['2607:6bc0::/48', 'static', null],
    ['192.0.2.0/24', 'static', null],
    ['0.0.0.0/0', 'static', /broader than \/16/],
    ['::/0', 'static', /broader than \/32/],
    ['52.0.0.0/8', 'static', /broader than \/16/],
    ['2600::/16', 'static', /broader than \/32/],
    ['10.20.0.0/16', 'static', /non-public address space 10\.0\.0\.0\/8/],
    ['192.168.4.0/24', 'static', /non-public/],
    ['100.64.0.0/16', 'static', /non-public/],
    ['224.1.0.0/16', 'static', /non-public/],
    ['fd00:1::/48', 'static', /non-public/],
    ['1.2.3.4/33', 'static', /^is not a valid CIDR$/],
    ['1.2.3.4', 'static', /^is not a valid CIDR$/],
    ['104.16.0.0/13', 'proxy', null],
    ['2a06:98c0::/29', 'proxy', null],
    ['104.0.0.0/11', 'proxy', /broader than \/12/],
    // A /12 that holds 169.254.0.0/16: the overlap check looks both ways.
    ['169.240.0.0/12', 'proxy', /non-public address space 169\.254\.0\.0\/16/],
    ['172.16.0.0/12', 'proxy', /non-public/],
  ])('%s as a %s range', (cidr, kind, want) => {
    const issue = cidrPolicyIssue(cidr, kind === 'static' ? MIN_STATIC_RANGE_PREFIX : MIN_TRUSTED_PROXY_PREFIX);
    if (want) expect(issue).toMatch(want);
    else expect(issue).toBeNull();
  });

  it('the floors are what site.json documents', () => {
    expect(MIN_STATIC_RANGE_PREFIX).toEqual({ ipv4: 16, ipv6: 32 });
    expect(MIN_TRUSTED_PROXY_PREFIX).toEqual({ ipv4: 12, ipv6: 24 });
  });
});

describe('CIDR and address-list parsing', () => {
  it('parseCidr is strict', () => {
    expect(parseCidr('160.79.104.0/21')).toEqual({ ip: '160.79.104.0', bits: 21, family: 'ipv4' });
    expect(parseCidr('2607:6bc0::/48')).toEqual({ ip: '2607:6bc0::', bits: 48, family: 'ipv6' });
    expect(parseCidr('1.2.3.4/33')).toBeNull();
    expect(parseCidr('1.2.3.4/')).toBeNull();
    expect(parseCidr('1.2.3.4/24/1')).toBeNull();
    expect(parseCidr('1.2.3.4')).toBeNull();
    expect(parseCidr('1.2.3.4', { allowBare: true })).toEqual({ ip: '1.2.3.4', bits: 32, family: 'ipv4' });
  });

  it('parseAddressList skips malformed tokens', () => {
    const l = parseAddressList('108.80.179.176, 192.0.2.0/24 nope 1.2.3.4/ 2001:db8::/32');
    expect(l.size).toBe(3);
    expect(l.list.check('192.0.2.77', 'ipv4')).toBe(true);
    expect(l.list.check('1.2.3.4', 'ipv4')).toBe(false);
    expect(parseAddressList(undefined).size).toBe(0);
  });
});

describe('verify', () => {
  const v = new AiCrawlerVerifier({ staticRanges: [], excludeIps: '108.80.179.176, 192.0.2.0/24' });
  beforeAll(() => {
    v.setRangeList('openai-searchbot', { prefixes: [{ ipv4Prefix: '104.210.140.128/28' }] });
    v.setRangeList('perplexity-user', { prefixes: [{ ipv4Prefix: '44.208.221.197/32' }] });
  });

  it('true in range, false out of range for a complete list', () => {
    expect(v.verify('crawler', 'oai-searchbot', '104.210.140.130')).toEqual({ verified: true, check: 'range:openai-searchbot' });
    expect(v.verify('crawler', 'oai-searchbot', '35.240.242.202')).toEqual({ verified: false, check: 'not-in-range:openai-searchbot' });
  });

  it('unknown when the list is thin, missing or not loaded', () => {
    expect(v.verify('crawler', 'perplexity-user', '35.229.224.83').verified).toBeNull();
    expect(v.verify('crawler', 'perplexity-user', '44.208.221.197').verified).toBe(true);
    expect(v.verify('crawler', 'meta-externalagent', '57.141.0.9')).toEqual({ verified: null, check: 'no-published-list' });
    expect(v.verify('crawler', 'gptbot', '132.196.86.5')).toEqual({ verified: null, check: 'list-unavailable' });
    expect(v.verify('crawler', 'gptbot', '')).toEqual({ verified: null, check: 'no-ip' });
  });

  it('our own host is never AI traffic, whatever it claims to be', () => {
    expect(v.verify('crawler', 'oai-searchbot', '108.80.179.176')).toEqual({ verified: false, check: 'excluded-ip' });
    expect(v.verify('crawler', 'claude-user', '192.0.2.44')).toEqual({ verified: false, check: 'excluded-ip' });
    expect(v.verify('referral', 'chatgpt', '108.80.179.176')).toEqual({ verified: false, check: 'excluded-ip' });
    expect(v.verify('referral', 'chatgpt', '73.1.2.3')).toEqual({ verified: null, check: null });
    v.setExcludedIps('');
    expect(v.verify('crawler', 'oai-searchbot', '108.80.179.176').check).toBe('not-in-range:openai-searchbot');
  });

  it('Anthropic bots.json: strict for ClaudeBot, a miss is unknown for Claude-User / Claude-SearchBot', () => {
    const a = new AiCrawlerVerifier({ staticRanges: [] });
    const anthropic = { prefixes: [{ ipv4Prefix: '216.73.216.0/22' }, { ipv4Prefix: '34.162.191.81/32' }] };
    a.setRangeList('anthropic-claudebot', anthropic);
    a.setRangeList('anthropic-user', anthropic);
    expect(a.verify('crawler', 'claudebot', '216.73.217.5')).toEqual({ verified: true, check: 'range:anthropic-claudebot' });
    expect(a.verify('crawler', 'claudebot', '35.229.175.21')).toEqual({ verified: false, check: 'not-in-range:anthropic-claudebot' });
    expect(a.verify('crawler', 'claude-user', '34.162.191.81')).toEqual({ verified: true, check: 'range:anthropic-user' });
    expect(a.verify('crawler', 'claude-user', '35.189.157.25')).toEqual({ verified: null, check: 'not-in-range:anthropic-user' });
    expect(a.verify('crawler', 'claude-searchbot', '35.229.175.21')).toEqual({ verified: null, check: 'not-in-range:anthropic-user' });
    expect(a.verify('crawler', 'claude-searchbot', '216.73.216.200').verified).toBe(true);
    const lists = CRAWLER_RANGE_LISTS.filter((r) => r.url === 'https://claude.com/crawling/bots.json');
    expect(lists.map((r) => [r.id, r.strict])).toEqual([['anthropic-claudebot', true], ['anthropic-user', false]]);
  });

  it('reads the vendor JSON shape, and never turns "x/" into a /0 that matches everything', () => {
    expect(rangeListFromJson({ prefixes: [{ ipv4Prefix: '157.55.39.0/24' }, { ipv6Prefix: '2620:1ec:c::/48' }, { bogus: 1 }] })?.prefixes).toBe(2);
    expect(rangeListFromJson({})).toBeNull();
    expect(rangeListFromJson(null)).toBeNull();
    expect(rangeListFromJson({ prefixes: [{ ipv4Prefix: '1.2.3.4/' }] })).toBeNull();
  });
});

describe('static ranges (kit default: Anthropic outbound)', () => {
  it('ships 160.79.104.0/21 and 153.61.208.0/20 for ClaudeBot, Claude-User and Claude-SearchBot', () => {
    expect(KIT_DEFAULT_STATIC_RANGES).toEqual([{
      id: 'anthropic-outbound',
      sources: ['claudebot', 'claude-user', 'claude-searchbot'],
      cidrs: ['160.79.104.0/21', '153.61.208.0/20'],
      evidenceUrl: 'https://platform.claude.com/docs/en/api/ip-addresses',
    }]);
  });

  it('verifies an address in the static ranges even before any list loads', () => {
    const v = new AiCrawlerVerifier();
    expect(v.verify('crawler', 'claude-user', '160.79.104.10')).toEqual({ verified: true, check: 'static-range:anthropic-outbound' });
    expect(v.verify('crawler', 'claude-user', '160.79.111.255').verified).toBe(true);
    expect(v.verify('crawler', 'claude-user', '160.79.112.0')).toEqual({ verified: null, check: 'list-unavailable' });
    expect(v.verify('crawler', 'claude-searchbot', '153.61.223.254').verified).toBe(true);
    expect(v.verify('crawler', 'claudebot', '153.61.208.1').check).toBe('static-range:anthropic-outbound');
  });

  it('a strict list miss does not override a static hit; exclusion overrides both', () => {
    const v = new AiCrawlerVerifier({ excludeIps: '160.79.104.99' });
    v.setRangeList('anthropic-claudebot', { prefixes: [{ ipv4Prefix: '216.73.216.0/22' }] });
    expect(v.verify('crawler', 'claudebot', '160.79.104.10').verified).toBe(true);
    expect(v.verify('crawler', 'claudebot', '35.1.2.3').verified).toBe(false);
    expect(v.verify('crawler', 'claudebot', '160.79.104.99')).toEqual({ verified: false, check: 'excluded-ip' });
  });

  it('only proves the crawlers it names', () => {
    const v = new AiCrawlerVerifier();
    expect(v.verify('crawler', 'claude-code', '160.79.104.10').check).toBe('no-published-list');
    expect(v.verify('crawler', 'gptbot', '160.79.104.10').check).toBe('list-unavailable');
    expect(v.verify('referral', 'claude', '160.79.104.10')).toEqual({ verified: null, check: null });
  });

  it('refuses a malformed static CIDR at construction', () => {
    expect(() => new AiCrawlerVerifier({ staticRanges: [{ id: 'bad', sources: ['claudebot'], cidrs: ['1.2.3.4/40'], evidenceUrl: 'https://x' }] }))
      .toThrow(/malformed CIDR/);
  });

  it('refuses a static range so broad (or so internal) that it would verify anyone', () => {
    const range = (cidrs: string[]) => ({ id: 'all', sources: ['gptbot', 'claudebot', 'chatgpt-user'], cidrs, evidenceUrl: 'https://x.example/' });
    expect(() => new AiCrawlerVerifier({ staticRanges: [range(['0.0.0.0/0'])] })).toThrow(/static range all: 0\.0\.0\.0\/0 is broader than \/16/);
    expect(() => new AiCrawlerVerifier({ staticRanges: [range(['10.1.0.0/16'])] })).toThrow(/non-public/);
    expect(() => new AiCrawlerVerifier({ staticRanges: [range(['::/0'])] })).toThrow(/broader than \/32/);
  });
});

describe('refresh with an injected fetch (no network)', () => {
  function fakeFetch(answers: Record<string, unknown>): { fetchImpl: RangeFetch; calls: string[]; uas: string[] } {
    const calls: string[] = [];
    const uas: string[] = [];
    const fetchImpl: RangeFetch = async (url, init) => {
      calls.push(url);
      uas.push(init.headers['User-Agent'] as string);
      const a = answers[url];
      if (a instanceof Error) throw a;
      if (typeof a === 'number') return { ok: false, status: a, json: async () => ({}) };
      return { ok: true, status: 200, json: async () => a };
    };
    return { fetchImpl, calls, uas };
  }

  it('fetches bots.json once for both Anthropic lists, and records failures', async () => {
    const f = fakeFetch({
      'https://claude.com/crawling/bots.json': { prefixes: [{ ipv4Prefix: '216.73.216.0/22' }] },
      'https://openai.com/gptbot.json': { prefixes: [{ ipv4Prefix: '20.15.240.64/28' }] },
      'https://openai.com/searchbot.json': 503,
      'https://openai.com/chatgpt-user.json': new Error('socket hang up'),
      'https://www.perplexity.ai/perplexitybot.json': {},
      'https://www.perplexity.ai/perplexity-user.json': { prefixes: [{ ipv4Prefix: '44.208.221.197/32' }] },
      'https://www.bing.com/toolbox/bingbot.json': { prefixes: [{ ipv4Prefix: '157.55.39.0/24' }] },
    });
    const v = createAiCrawlerVerifier(ctx, { fetchImpl: f.fetchImpl, env: {} });
    const r = await v.refresh();
    expect(f.calls.filter((u) => u === 'https://claude.com/crawling/bots.json')).toHaveLength(1);
    expect(f.calls).toHaveLength(7);
    expect(new Set(f.uas)).toEqual(new Set(['ExamplePicks-crawler-verify/1.0 (+https://example.com/)']));
    expect(r.loaded.sort()).toEqual(['anthropic-claudebot', 'anthropic-user', 'bingbot', 'openai-gptbot', 'perplexity-user']);
    expect(r.failed.sort()).toEqual(['openai-chatgpt-user: socket hang up', 'openai-searchbot: HTTP 503']);
    expect(v.verify('crawler', 'claudebot', '216.73.217.1').check).toBe('range:anthropic-claudebot');
  });

  it('keeps the last good copy when a later answer is empty or fails', async () => {
    let round = 0;
    const fetchImpl: RangeFetch = async (url) => {
      if (url !== 'https://openai.com/gptbot.json') return { ok: false, status: 404, json: async () => ({}) };
      round++;
      if (round === 1) return { ok: true, status: 200, json: async () => ({ prefixes: [{ ipv4Prefix: '20.15.240.64/28' }] }) };
      if (round === 2) return { ok: true, status: 200, json: async () => ({ prefixes: [] }) };
      throw new Error('timeout');
    };
    const v = new AiCrawlerVerifier({ fetchImpl, staticRanges: [] });
    await v.refresh();
    await v.refresh();
    await v.refresh();
    expect(v.verify('crawler', 'gptbot', '20.15.240.70')).toEqual({ verified: true, check: 'range:openai-gptbot' });
  });

  it('start() loads at once, is idempotent, and stop() clears the timer', async () => {
    const f = fakeFetch({});
    const v = new AiCrawlerVerifier({ fetchImpl: f.fetchImpl, refreshMs: 60_000 });
    const logs: string[] = [];
    v.start((m) => logs.push(m));
    v.start((m) => logs.push(m));
    await new Promise((r) => setImmediate(r));
    await new Promise((r) => setImmediate(r));
    v.stop();
    expect(f.calls.length).toBe(7); // one refresh: 7 distinct URLs
    expect(logs).toHaveLength(1);
    // Empty answers are not failures (the last good copy, if any, is kept).
    expect(logs[0]).toBe('[ai-traffic] crawler ranges loaded=0');
  });

  it('a verifier with no fetch at all reports every list as failed', async () => {
    const v = new AiCrawlerVerifier({ fetchImpl: undefined, staticRanges: [] });
    (v as unknown as { fetchImpl: null }).fetchImpl = null;
    const r = await v.refresh();
    expect(r.loaded).toEqual([]);
    expect(r.failed.every((m) => m.endsWith('no fetch implementation'))).toBe(true);
  });
});

describe('createAiCrawlerVerifier', () => {
  it('takes the static ranges from SiteContext and exclusions from AI_TRAFFIC_EXCLUDE_IPS', () => {
    const v = createAiCrawlerVerifier(ctx, { env: { AI_TRAFFIC_EXCLUDE_IPS: '203.0.113.5' } });
    expect(v.verify('crawler', 'claude-user', '203.0.113.5')).toEqual({ verified: false, check: 'excluded-ip' });
    expect(v.verify('crawler', 'claude-user', '160.79.104.10').verified).toBe(true);
    expect(v.isExcludedIp('203.0.113.6')).toBe(false);
  });

  it('builds a polite, site-named User-Agent', () => {
    expect(crawlerVerifyUserAgent(ctx)).toBe('ExamplePicks-crawler-verify/1.0 (+https://example.com/)');
    expect(crawlerVerifyUserAgent({ site: { ...ctx.site, brandName: '!!!' } })).toBe('kit-example-crawler-verify/1.0 (+https://example.com/)');
  });
});
