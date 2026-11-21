/**
 * Who is really behind an "AI crawler" request.
 *
 * Ported from SpecPicks src/services/aiCrawlerVerify.ts (see PORTED_FROM.json).
 * Behaviour kept; what changed:
 *   - Range-list state lives in an AiCrawlerVerifier instance instead of
 *     module globals, so a process (or a test) can hold more than one.
 *   - Static vendor ranges are checked before the fetched lists. The kit ships
 *     Anthropic's published outbound ranges (160.79.104.0/21, 153.61.208.0/20)
 *     as a default; site.json can add more (aiCrawlers.extraStaticRanges),
 *     never broader than MIN_STATIC_RANGE_PREFIX nor in non-public space.
 *   - The fetch User-Agent names the site from SiteContext, not SpecPicks.
 *   - clientIp() can skip ONE trusted CDN hop (site.json edge; siteClientIp),
 *     because a site behind a proxying CDN sees the CDN's edge as the
 *     right-most public X-Forwarded-For entry. See clientIp() for why exactly
 *     one hop.
 *   - Amazon's answer-time and search crawlers (Amzn-User, Amzn-SearchBot)
 *     are classified, as documented at developer.amazon.com/amazonbot.
 *
 * Upstream rationale, kept because it explains every rule below:
 *
 * ai_traffic_log classifies a request by its User-Agent, and a User-Agent is
 * a string anyone can send. On 2026-09-25 SpecPicks' table held 7,662 rows
 * from the fleet host's own audit curls (oai-searchbot 4,753, perplexitybot
 * 1,172, chatgpt-user 1,091), and GCP / Cloudflare-WARP scanners rotate
 * through every AI crawler name while probing /.env.
 *
 * Two questions per logged request:
 *
 *   clientIp()  which address actually connected. Azure Container Apps
 *               APPENDS the peer address to whatever X-Forwarded-For the
 *               client sent (measured: a request carrying
 *               `X-Forwarded-For: 203.0.113.9` was logged as
 *               "203.0.113.9,108.80.179.176"), so the first entry is the
 *               client's claim and the right-most public entry is the address
 *               the edge saw.
 *   verify()    whether that address belongs to the crawler the UA names,
 *               from the vendors' published range lists (loaded at boot,
 *               refreshed every 12h, last good copy kept) and the static
 *               ranges. true = in range; false = a strict list exists and the
 *               address is not in it, or it is one of our own addresses
 *               (AI_TRAFFIC_EXCLUDE_IPS); null = unknown (no usable list for
 *               that crawler, or the list could not be loaded). Rows are only
 *               ever judged at insert time, against the list current then:
 *               vendors rotate ranges, and re-judging old genuine rows against
 *               today's list would call them spoofed.
 *
 * Anthropic publishes https://claude.com/crawling/bots.json: "If a crawler has
 * a source IP address on this list, it indicates that the crawler is coming
 * from Anthropic." Over 7 days SpecPicks saw 4,502 of 4,508 ClaudeBot rows and
 * 216 of 222 Claude-User rows inside it, but 0 of 10 Claude-SearchBot rows. So
 * ClaudeBot is strict, while Claude-User / Claude-SearchBot are non-strict (a
 * miss stays unknown). Anthropic also documents fixed OUTBOUND ranges for its
 * fetches (https://platform.claude.com/docs/en/api/ip-addresses); an address
 * there is Anthropic's whatever the list says, so it verifies as true.
 */
import { BlockList, isIP } from 'net';
import type { CdnName, SiteContext, StaticCrawlerRange } from './siteContext';

export interface RangeListDef {
  readonly id: string;
  readonly url: string;
  readonly sources: readonly string[];
  /** A miss means "not this crawler" (false) rather than "unknown" (null). */
  readonly strict: boolean;
}

/** Freeze plain objects and arrays recursively; RegExps and class instances are left as they are. */
function frozen<T>(v: T): T {
  if (Array.isArray(v) || (v !== null && typeof v === 'object' && Object.getPrototypeOf(v) === Object.prototype)) {
    for (const k of Object.keys(v as object)) frozen((v as Record<string, unknown>)[k]);
    Object.freeze(v);
  }
  return v;
}

const ANTHROPIC_BOTS_URL = 'https://claude.com/crawling/bots.json';

/** Vendor range lists fetched at boot and every 12h. */
export const CRAWLER_RANGE_LISTS: readonly RangeListDef[] = frozen([
  { id: 'openai-gptbot', url: 'https://openai.com/gptbot.json', sources: ['gptbot'], strict: true },
  { id: 'openai-searchbot', url: 'https://openai.com/searchbot.json', sources: ['oai-searchbot'], strict: true },
  { id: 'openai-chatgpt-user', url: 'https://openai.com/chatgpt-user.json', sources: ['chatgpt-user'], strict: true },
  { id: 'perplexitybot', url: 'https://www.perplexity.ai/perplexitybot.json', sources: ['perplexitybot'], strict: true },
  { id: 'perplexity-user', url: 'https://www.perplexity.ai/perplexity-user.json', sources: ['perplexity-user'], strict: false },
  { id: 'bingbot', url: 'https://www.bing.com/toolbox/bingbot.json', sources: ['bingbot'], strict: true },
  { id: 'anthropic-claudebot', url: ANTHROPIC_BOTS_URL, sources: ['claudebot'], strict: true },
  { id: 'anthropic-user', url: ANTHROPIC_BOTS_URL, sources: ['claude-user', 'claude-searchbot'], strict: false },
]);

/** Where Anthropic documents its fixed outbound addresses. */
export const ANTHROPIC_IP_DOCS_URL = 'https://platform.claude.com/docs/en/api/ip-addresses';

/**
 * Static ranges every kit site checks (review issue (b): fleet-wide data
 * belongs in the kit, not in each site.json). Anthropic's outbound IPv4
 * ranges, as published at ANTHROPIC_IP_DOCS_URL on 2026-11-17.
 */
export const KIT_DEFAULT_STATIC_RANGES: readonly StaticCrawlerRange[] = frozen([
  {
    id: 'anthropic-outbound',
    sources: ['claudebot', 'claude-user', 'claude-searchbot'],
    cidrs: ['160.79.104.0/21', '153.61.208.0/20'],
    evidenceUrl: ANTHROPIC_IP_DOCS_URL,
  },
]);

export const RANGE_REFRESH_MS = 12 * 60 * 60 * 1000;
export const RANGE_FETCH_TIMEOUT_MS = 10_000;

// ─── classification ────────────────────────────────────────────────────────

// AI-traffic classification: AI-search crawlers (matched by user-agent) and
// human referrals arriving from an AI chat (matched by referer host -- these
// carry a normal browser UA). Only AI-relevant hits are written to
// ai_traffic_log. First match wins. Claude's user-triggered fetchers come
// BEFORE /claudebot/, because Claude-SearchBot and some Claude-User UAs also
// carry "+claudebot@anthropic.com" and were being filed as the training
// crawler; Claude Code's WebFetch ("Claude-User (claude-code/") is developer
// traffic and gets its own name. google-extended and applebot-extended are
// robots.txt tokens, never a real UA -- every row under them was a scanner --
// so they are not here.
export const AI_CRAWLER_UA: ReadonlyArray<readonly [RegExp, string]> = frozen([
  [/gptbot/i, 'gptbot'],
  [/oai-searchbot/i, 'oai-searchbot'],
  [/chatgpt-user/i, 'chatgpt-user'],
  [/claude-user\s*\(\s*claude-code/i, 'claude-code'],
  [/claude-user/i, 'claude-user'],
  [/claude-searchbot/i, 'claude-searchbot'],
  [/claudebot/i, 'claudebot'],
  [/claude-web/i, 'claude-web'],
  [/anthropic-ai/i, 'anthropic-ai'],
  [/perplexitybot/i, 'perplexitybot'],
  [/perplexity-user/i, 'perplexity-user'],
  [/mistralai-user/i, 'mistralai-user'],
  [/duckassistbot/i, 'duckassistbot'],
  [/bytespider/i, 'bytespider'],
  [/\bccbot\b/i, 'ccbot'],
  [/ai2bot/i, 'ai2bot'],
  [/meta-externalagent/i, 'meta-externalagent'],
  // Amazon's crawlers besides Amazonbot (developer.amazon.com/amazonbot,
  // checked 2026-11-17): Amzn-SearchBot indexes for Alexa / Rufus search and
  // "does not crawl content for generative AI model training"; Amzn-User
  // fetches a page to answer one person's question.
  [/amzn-searchbot/i, 'amzn-searchbot'],
  [/amzn-user/i, 'amzn-user'],
  [/amazonbot/i, 'amazonbot'],
  [/bingbot/i, 'bingbot'],
]);

/** Every source name classifyAiTraffic() can return for a crawler. */
export const AI_CRAWLER_SOURCES: readonly string[] = frozen(Array.from(new Set(AI_CRAWLER_UA.map(([, name]) => name))));

export const AI_REFERRAL_HOST: ReadonlyArray<readonly [RegExp, string]> = frozen([
  [/(^|\.)chatgpt\.com$/i, 'chatgpt'],
  [/(^|\.)chat\.openai\.com$/i, 'chatgpt'],
  [/(^|\.)openai\.com$/i, 'chatgpt'],
  [/(^|\.)perplexity\.ai$/i, 'perplexity'],
  [/(^|\.)claude\.ai$/i, 'claude'],
  [/(^|\.)gemini\.google\.com$/i, 'gemini'],
  [/(^|\.)copilot\.microsoft\.com$/i, 'copilot'],
  [/(^|\.)you\.com$/i, 'you'],
  // Assistants GA4 files under Referral: pplx.app is Perplexity's app host;
  // doubao.com, trae.cn and codebuddy.work are ByteDance/Tencent assistants.
  [/(^|\.)pplx\.app$/i, 'perplexity'],
  [/(^|\.)doubao\.com$/i, 'doubao'],
  [/(^|\.)trae\.(cn|ai)$/i, 'trae'],
  [/(^|\.)codebuddy\.work$/i, 'codebuddy'],
  [/(^|\.)chat\.deepseek\.com$/i, 'deepseek'],
  [/(^|\.)grok\.com$/i, 'grok'],
  // kagi.com sends one origin for both Kagi Search and Kagi Assistant, so its
  // own source name keeps it separable. feishu.cn is deliberately absent:
  // those are tenant doc links a person pasted, not an assistant citation.
  [/(^|\.)kagi\.com$/i, 'kagi'],
]);

// Assistant arrivals often carry no Referer; ChatGPT (and several others) tag
// every outbound link `utm_source=chatgpt.com`, so with no AI Referer the
// utm_source value classifies the landing. Matched exactly, never by substring.
export const AI_REFERRAL_UTM: Readonly<Record<string, string>> = Object.freeze({
  'chatgpt.com': 'chatgpt',
  'chatgpt': 'chatgpt',
  'chat.openai.com': 'chatgpt',
  'openai': 'chatgpt',
  'perplexity': 'perplexity',
  'perplexity.ai': 'perplexity',
  'claude.ai': 'claude',
  'claude': 'claude',
  'copilot.microsoft.com': 'copilot',
  'copilot.com': 'copilot',
  'gemini.google.com': 'gemini',
  'kagi.com': 'kagi',
  'doubao.com': 'doubao',
  'grok.com': 'grok',
  'chat.deepseek.com': 'deepseek',
});

export type AiTrafficKind = 'crawler' | 'referral';

export function classifyAiTraffic(
  ua: string | undefined,
  referer: string | undefined,
  utmSource?: string | undefined,
): { kind: AiTrafficKind; source: string } | null {
  if (ua) {
    for (const [re, name] of AI_CRAWLER_UA) if (re.test(ua)) return { kind: 'crawler', source: name };
  }
  if (referer) {
    try {
      const host = new URL(referer).hostname;
      for (const [re, name] of AI_REFERRAL_HOST) if (re.test(host)) return { kind: 'referral', source: name };
    } catch { /* malformed referer -- ignore */ }
  }
  if (utmSource) {
    const key = utmSource.trim().toLowerCase();
    const name = Object.prototype.hasOwnProperty.call(AI_REFERRAL_UTM, key) ? AI_REFERRAL_UTM[key] : undefined;
    if (name) return { kind: 'referral', source: name };
  }
  return null;
}

// ─── addresses ─────────────────────────────────────────────────────────────

export type IpFamily = 'ipv4' | 'ipv6';

/** Strip a port, IPv6 brackets and the IPv4-mapped IPv6 prefix. '' if not an IP. */
export function normalizeIp(raw: string | null | undefined): string {
  let s = String(raw || '').trim();
  if (!s) return '';
  const bracket = s.match(/^\[([^\]]+)\](?::\d+)?$/);
  if (bracket) s = bracket[1] as string;
  else if (/^\d{1,3}(\.\d{1,3}){3}:\d+$/.test(s)) s = s.replace(/:\d+$/, '');
  s = s.replace(/^::ffff:(\d{1,3}(\.\d{1,3}){3})$/i, '$1');
  return isIP(s) ? s.toLowerCase() : '';
}

function family(ip: string): IpFamily {
  return isIP(ip) === 6 ? 'ipv6' : 'ipv4';
}

const PRIVATE = new BlockList();
for (const [net, bits] of [['10.0.0.0', 8], ['172.16.0.0', 12], ['192.168.0.0', 16], ['100.64.0.0', 10],
  ['127.0.0.0', 8], ['169.254.0.0', 16], ['0.0.0.0', 8]] as Array<[string, number]>) PRIVATE.addSubnet(net, bits, 'ipv4');
for (const [net, bits] of [['fc00::', 7], ['fe80::', 10]] as Array<[string, number]>) PRIVATE.addSubnet(net, bits, 'ipv6');
PRIVATE.addAddress('::1', 'ipv6');

export function isPrivateIp(ip: string): boolean {
  const n = normalizeIp(ip);
  return !n || PRIVATE.check(n, family(n));
}

/** Answers "is this address one of our trusted proxies?" (edgeProxyMatcher / trustedProxyMatcher build one). */
export interface TrustedProxyMatcher {
  has(ip: string): boolean;
}

/**
 * The address that connected: the right-most public X-Forwarded-For entry
 * (the one our own edge appended), else the socket peer. Entries to its left
 * were sent by the client and prove nothing.
 *
 * Behind a proxying CDN (site.json edge; use siteClientIp) that right-most
 * public entry is the CDN's edge, not the visitor, so every crawler would be
 * checked with an edge address. Pass the CDN's ranges as `trusted`: when the
 * right-most public entry is inside them, the next public entry to its left,
 * which is the address the CDN itself appended (whoever connected to it), is
 * the client.
 *
 * Exactly ONE trusted hop is skipped, never a run of them. Cloudflare Workers
 * fetch from Cloudflare's own ranges: a cross-zone Worker subrequest arrives
 * as 2a06:98c0:3600::103, inside the published 2a06:98c0::/29. Skipping every
 * trusted entry would walk past that address to whatever X-Forwarded-For the
 * Worker's author wrote, letting anyone with a Worker claim an OpenAI or
 * Anthropic address. CF-Connecting-IP is never read: the header walk gives
 * the same answer for any CDN. Without `trusted` the result is SpecPicks'.
 */
export function clientIp(
  xff: string | string[] | null | undefined,
  remote?: string | null,
  trusted?: TrustedProxyMatcher | null,
): string {
  const header = Array.isArray(xff) ? xff.join(',') : String(xff || '');
  const parts = header.split(',').map(normalizeIp).filter(Boolean);
  for (let i = parts.length - 1; i >= 0; i--) {
    const ip = parts[i] as string;
    if (isPrivateIp(ip)) continue;
    if (!trusted || !trusted.has(ip)) return ip;
    // `ip` is our CDN's edge: the client is what the CDN appended, just to its left.
    for (let j = i - 1; j >= 0; j--) if (!isPrivateIp(parts[j] as string)) return parts[j] as string;
    return ip; // the CDN itself made the request
  }
  const peer = normalizeIp(remote);
  return peer || parts[parts.length - 1] || '';
}

// ─── CDN edges and range policy ────────────────────────────────────────────

export interface CdnEdgeRanges {
  readonly cdn: CdnName;
  readonly cidrs: readonly string[];
  /** Where the CDN publishes them. */
  readonly evidenceUrls: readonly string[];
  /** When the list was last compared with evidenceUrls (YYYY-MM-DD). */
  readonly verifiedOn: string;
}

/**
 * Edge ranges of the CDNs site.json edge.cdn can name (kit data, like
 * KIT_DEFAULT_STATIC_RANGES). Cloudflare's, as published at
 * https://www.cloudflare.com/ips-v4 and /ips-v6 (identical to the
 * api.cloudflare.com/client/v4/ips answer) on 2026-11-17. Cloudflare changes
 * them rarely and announces it; refresh the list and verifiedOn together.
 */
export const CDN_EDGE_RANGES: Readonly<Record<CdnName, CdnEdgeRanges>> = frozen({
  cloudflare: {
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
  },
});

/** A matcher over CIDRs or bare addresses. Throws on a malformed entry: a typo would silently change every logged address. */
export function trustedProxyMatcher(cidrs: readonly string[]): TrustedProxyMatcher {
  const list = new BlockList();
  for (const raw of cidrs) {
    const c = parseCidr(raw, { allowBare: true });
    if (!c) throw new TypeError(`trusted proxy ${JSON.stringify(raw)} is not an IP address or CIDR`);
    list.addSubnet(c.ip, c.bits, c.family);
  }
  const empty = cidrs.length === 0;
  return Object.freeze({
    has(ip: string): boolean {
      if (empty) return false;
      const n = normalizeIp(ip);
      return !!n && list.check(n, family(n));
    },
  });
}

const EDGE_MATCHERS = new WeakMap<object, TrustedProxyMatcher | null>();

/** The site's trusted CDN hop (ctx.edge.trustedProxyCidrs), built once per SiteContext; null when none. */
export function edgeProxyMatcher(ctx: Pick<SiteContext, 'edge'>): TrustedProxyMatcher | null {
  const key = ctx.edge;
  const hit = EDGE_MATCHERS.get(key);
  if (hit !== undefined) return hit;
  const m = key.trustedProxyCidrs.length ? trustedProxyMatcher(key.trustedProxyCidrs) : null;
  EDGE_MATCHERS.set(key, m);
  return m;
}

/** clientIp() for this site: skips one hop inside the CDN named by site.json edge. */
export function siteClientIp(
  ctx: Pick<SiteContext, 'edge'>,
  xff: string | string[] | null | undefined,
  remote?: string | null,
): string {
  return clientIp(xff, remote, edgeProxyMatcher(ctx));
}

/** The broadest prefix a static crawler range may use: a wider one proves nothing about who connected. */
export const MIN_STATIC_RANGE_PREFIX: Readonly<Record<IpFamily, number>> = frozen({ ipv4: 16, ipv6: 32 });
/** The broadest prefix a trusted proxy range may use (Cloudflare's widest are /13 and /29). */
export const MIN_TRUSTED_PROXY_PREFIX: Readonly<Record<IpFamily, number>> = frozen({ ipv4: 12, ipv6: 24 });

/**
 * Space no crawler vendor or CDN edge connects from: PRIVATE's blocks plus the
 * unspecified address, multicast and IPv4 class E. An allowlisted range
 * there would verify internal hops (or nothing at all).
 */
const NON_PUBLIC_BLOCKS: ReadonlyArray<{ readonly net: string; readonly bits: number; readonly family: IpFamily; readonly list: BlockList }> = ([
  ['0.0.0.0', 8, 'ipv4'], ['10.0.0.0', 8, 'ipv4'], ['100.64.0.0', 10, 'ipv4'], ['127.0.0.0', 8, 'ipv4'],
  ['169.254.0.0', 16, 'ipv4'], ['172.16.0.0', 12, 'ipv4'], ['192.168.0.0', 16, 'ipv4'],
  ['224.0.0.0', 4, 'ipv4'], ['240.0.0.0', 4, 'ipv4'],
  ['::', 128, 'ipv6'], ['::1', 128, 'ipv6'], ['fc00::', 7, 'ipv6'], ['fe80::', 10, 'ipv6'], ['ff00::', 8, 'ipv6'],
] as Array<[string, number, IpFamily]>).map(([net, bits, fam]) => {
  const list = new BlockList();
  list.addSubnet(net, bits, fam);
  return Object.freeze({ net, bits, family: fam, list });
});

/**
 * IPv4-mapped IPv6 space. Node's BlockList matches IPv4 addresses against an
 * IPv6 subnet that overlaps it, so '::ffff:0:0/96' written as a range would be
 * all of IPv4 and '::ffff:a00:0/104' would be 10.0.0.0/8, past both the IPv4
 * prefix floor and the non-public blocks. No other IPv6 block aliases IPv4 in
 * BlockList (NAT64, 6to4 and IPv4-compatible addresses do not).
 */
const IPV4_MAPPED_NET = '::ffff:0:0';
const IPV4_MAPPED = new BlockList();
IPV4_MAPPED.addSubnet(IPV4_MAPPED_NET, 96, 'ipv6');

/**
 * Why `spec` cannot be an allowlist range (a static crawler range or a
 * trusted proxy), or null when it can: it must be a valid CIDR, no broader
 * than `minPrefix` for its family, must not overlap non-public space, and an
 * IPv6 range must not overlap IPv4-mapped space (IPv4 is written as IPv4).
 */
export function cidrPolicyIssue(spec: string, minPrefix: Readonly<Record<IpFamily, number>>): string | null {
  const c = parseCidr(spec);
  if (!c) return 'is not a valid CIDR';
  const floor = minPrefix[c.family];
  if (c.bits < floor) return `is broader than /${floor}, the widest ${c.family === 'ipv4' ? 'IPv4' : 'IPv6'} range allowed`;
  const mine = new BlockList();
  mine.addSubnet(c.ip, c.bits, c.family);
  for (const b of NON_PUBLIC_BLOCKS) {
    if (b.family !== c.family) continue;
    // CIDR blocks are nested or disjoint: they overlap iff one holds the other's network address.
    if (b.list.check(c.ip, c.family) || mine.check(b.net, b.family)) return `overlaps non-public address space ${b.net}/${b.bits}`;
  }
  if (c.family === 'ipv6' && (IPV4_MAPPED.check(c.ip, 'ipv6') || mine.check(IPV4_MAPPED_NET, 'ipv6'))) {
    return `overlaps IPv4-mapped IPv6 space ${IPV4_MAPPED_NET}/96; write it as an IPv4 range`;
  }
  return null;
}

/** One CIDR (or bare address, when allowBare) parsed; null when malformed. */
export function parseCidr(
  spec: string,
  opts: { allowBare?: boolean } = {},
): { ip: string; bits: number; family: IpFamily } | null {
  const parts = String(spec || '').trim().split('/');
  if (parts.length > 2) return null;
  const ip = normalizeIp(parts[0]);
  if (!ip) return null;
  const fam = family(ip);
  const max = fam === 'ipv4' ? 32 : 128;
  if (parts.length === 1) return opts.allowBare ? { ip, bits: max, family: fam } : null;
  const bitsRaw = parts[1] as string;
  if (!/^\d{1,3}$/.test(bitsRaw)) return null;
  const bits = Number(bitsRaw);
  if (bits > max) return null;
  return { ip, bits, family: fam };
}

interface AddressList {
  list: BlockList;
  size: number;
}

/** Comma/space-separated IPs or CIDRs; malformed tokens are skipped. */
export function parseAddressList(spec: string | undefined): AddressList {
  const list = new BlockList();
  let size = 0;
  for (const tok of String(spec || '').split(/[\s,]+/).filter(Boolean)) {
    const c = parseCidr(tok, { allowBare: true });
    if (!c) continue;
    if (c.bits === (c.family === 'ipv4' ? 32 : 128) && !tok.includes('/')) list.addAddress(c.ip, c.family);
    else list.addSubnet(c.ip, c.bits, c.family);
    size++;
  }
  return { list, size };
}

// ─── published crawler ranges ──────────────────────────────────────────────

export interface LoadedRangeList {
  readonly list: BlockList;
  readonly prefixes: number;
  readonly loadedAt: number;
}

/** Build a BlockList from a vendor JSON ({prefixes:[{ipv4Prefix|ipv6Prefix}]}); null when it holds none. */
export function rangeListFromJson(json: unknown, now: number = Date.now()): LoadedRangeList | null {
  const raw = (json && typeof json === 'object') ? (json as { prefixes?: unknown }).prefixes : undefined;
  const prefixes: unknown[] = Array.isArray(raw) ? raw : [];
  const list = new BlockList();
  let n = 0;
  for (const p of prefixes) {
    const entry = (p && typeof p === 'object') ? p as { ipv4Prefix?: unknown; ipv6Prefix?: unknown } : {};
    const c = parseCidr(String(entry.ipv4Prefix || entry.ipv6Prefix || ''));
    if (!c) continue;
    try {
      list.addSubnet(c.ip, c.bits, c.family);
      n++;
    } catch { /* malformed prefix: skip it */ }
  }
  return n ? { list, prefixes: n, loadedAt: now } : null;
}

export interface IpVerdict {
  verified: boolean | null;
  /**
   * Why, for the log row: 'static-range:<id>', 'range:<list>',
   * 'not-in-range:<list>', 'excluded-ip', 'no-published-list',
   * 'list-unavailable', 'no-ip'; null for a non-crawler that is not excluded.
   */
  check: string | null;
}

/** Minimal fetch contract, so tests (and non-global-fetch runtimes) can inject one. */
export type RangeFetch = (
  url: string,
  init: { signal: AbortSignal; headers: Record<string, string> },
) => Promise<{ ok: boolean; status: number; json(): Promise<unknown> }>;

export interface AiCrawlerVerifierOptions {
  lists?: readonly RangeListDef[];
  staticRanges?: readonly StaticCrawlerRange[];
  /** AI_TRAFFIC_EXCLUDE_IPS: our own addresses (fleet host audits), never AI traffic. */
  excludeIps?: string;
  /** User-Agent for the range-list fetches. */
  userAgent?: string;
  fetchImpl?: RangeFetch;
  refreshMs?: number;
  fetchTimeoutMs?: number;
  now?: () => number;
}

interface CompiledStatic {
  readonly def: StaticCrawlerRange;
  readonly list: BlockList;
}

/**
 * A static range verifies before (and regardless of) the strict vendor lists,
 * so one that is too broad or covers internal hops would mark spoofed rows
 * verified: refuse it here too, not only when site.json is loaded.
 */
function compileStatic(def: StaticCrawlerRange): CompiledStatic {
  const list = new BlockList();
  for (const cidr of def.cidrs) {
    const c = parseCidr(cidr);
    if (!c) throw new Error(`aiCrawlerVerify: static range ${def.id} has a malformed CIDR: ${cidr}`);
    const issue = cidrPolicyIssue(cidr, MIN_STATIC_RANGE_PREFIX);
    if (issue) throw new Error(`aiCrawlerVerify: static range ${def.id}: ${cidr} ${issue}`);
    list.addSubnet(c.ip, c.bits, c.family);
  }
  return { def, list };
}

/**
 * Range lists, static ranges and exclusions for one site process. Build it
 * with createAiCrawlerVerifier(ctx); call start() once at boot.
 */
export class AiCrawlerVerifier {
  private readonly lists: readonly RangeListDef[];
  private readonly statics: readonly CompiledStatic[];
  private readonly loaded = new Map<string, LoadedRangeList>();
  private excluded: AddressList;
  private readonly userAgent: string;
  private readonly fetchImpl: RangeFetch | null;
  private readonly refreshMs: number;
  private readonly fetchTimeoutMs: number;
  private readonly now: () => number;
  private timer: NodeJS.Timeout | null = null;

  constructor(opts: AiCrawlerVerifierOptions = {}) {
    this.lists = opts.lists ?? CRAWLER_RANGE_LISTS;
    this.statics = (opts.staticRanges ?? KIT_DEFAULT_STATIC_RANGES).map(compileStatic);
    this.excluded = parseAddressList(opts.excludeIps);
    this.userAgent = opts.userAgent ?? 'affiliate-site-kit-crawler-verify/1.0';
    const globalFetch = typeof fetch === 'function' ? (fetch as unknown as RangeFetch) : null;
    this.fetchImpl = opts.fetchImpl ?? globalFetch;
    this.refreshMs = opts.refreshMs ?? RANGE_REFRESH_MS;
    this.fetchTimeoutMs = opts.fetchTimeoutMs ?? RANGE_FETCH_TIMEOUT_MS;
    this.now = opts.now ?? Date.now;
  }

  /** Test/ops seam: install (or, with an empty/garbled body, remove) a list without the network. */
  setRangeList(id: string, json: unknown): void {
    const l = rangeListFromJson(json, this.now());
    if (l) this.loaded.set(id, l);
    else this.loaded.delete(id);
  }

  /** Replace the exclusion list ('' clears it). */
  setExcludedIps(spec: string): void {
    this.excluded = parseAddressList(spec);
  }

  isExcludedIp(ip: string): boolean {
    const n = normalizeIp(ip);
    return !!n && this.excluded.size > 0 && this.excluded.list.check(n, family(n));
  }

  /** Ids of the lists currently held (fetched or installed). */
  loadedListIds(): string[] {
    return Array.from(this.loaded.keys());
  }

  /** Fetch one URL and install it under every list id that reads it (one fetch for bots.json, not two). */
  private async fetchList(url: string, ids: readonly string[]): Promise<void> {
    if (!this.fetchImpl) throw new Error('no fetch implementation');
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), this.fetchTimeoutMs);
    try {
      const res = await this.fetchImpl(url, {
        signal: ctl.signal,
        headers: { 'User-Agent': this.userAgent, Accept: 'application/json' },
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const l = rangeListFromJson(await res.json(), this.now());
      if (l) for (const id of ids) this.loaded.set(id, l); // keep the last good copy on an empty/garbled answer
    } finally {
      clearTimeout(timer);
    }
  }

  async refresh(): Promise<{ loaded: string[]; failed: string[] }> {
    const failed: string[] = [];
    const byUrl = new Map<string, string[]>();
    for (const r of this.lists) byUrl.set(r.url, [...(byUrl.get(r.url) || []), r.id]);
    await Promise.all(Array.from(byUrl, ([url, ids]) => this.fetchList(url, ids).catch((e: unknown) => {
      failed.push(`${ids.join('+')}: ${e instanceof Error ? e.message : String(e)}`);
    })));
    return { loaded: this.loadedListIds(), failed };
  }

  /** Load the lists now and every refreshMs. Never throws; logging only. Idempotent. */
  start(log: (msg: string) => void = () => undefined): void {
    if (this.timer) return;
    const run = (): void => {
      this.refresh()
        .then((r) => log(`[ai-traffic] crawler ranges loaded=${r.loaded.length}${r.failed.length ? ` failed=${r.failed.join('; ')}` : ''}`))
        .catch(() => undefined);
    };
    run();
    this.timer = setInterval(run, this.refreshMs);
    this.timer.unref?.();
  }

  stop(): void {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
  }

  /** The verdict for one logged request (see the module doc). */
  verify(kind: string, source: string, ip: string): IpVerdict {
    const n = normalizeIp(ip);
    if (n && this.isExcludedIp(n)) return { verified: false, check: 'excluded-ip' };
    if (kind !== 'crawler') return { verified: null, check: null };
    if (!n) return { verified: null, check: 'no-ip' };
    const fam = family(n);
    for (const s of this.statics) {
      if (s.def.sources.includes(source) && s.list.check(n, fam)) return { verified: true, check: `static-range:${s.def.id}` };
    }
    const def = this.lists.find((r) => r.sources.includes(source));
    if (!def) return { verified: null, check: 'no-published-list' };
    const l = this.loaded.get(def.id);
    if (!l) return { verified: null, check: 'list-unavailable' };
    if (l.list.check(n, fam)) return { verified: true, check: `range:${def.id}` };
    return def.strict
      ? { verified: false, check: `not-in-range:${def.id}` }
      : { verified: null, check: `not-in-range:${def.id}` };
  }
}

/** The range-list fetch User-Agent for a site: "<Brand>-crawler-verify/1.0 (+<origin>/)". */
export function crawlerVerifyUserAgent(ctx: Pick<SiteContext, 'site'>): string {
  const token = ctx.site.brandName.replace(/[^A-Za-z0-9.-]+/g, '') || ctx.site.id;
  return `${token}-crawler-verify/1.0 (+${ctx.site.canonicalOrigin}/)`;
}

/**
 * The verifier a site process uses: kit lists, kit + site static ranges
 * (ctx.aiCrawlers.staticRanges), exclusions from AI_TRAFFIC_EXCLUDE_IPS (set
 * by the deploy script to the deploying host's egress address).
 */
export function createAiCrawlerVerifier(
  ctx: Pick<SiteContext, 'site' | 'aiCrawlers'>,
  opts: Omit<AiCrawlerVerifierOptions, 'staticRanges' | 'userAgent'> & { env?: NodeJS.ProcessEnv } = {},
): AiCrawlerVerifier {
  const { env = process.env, ...rest } = opts;
  return new AiCrawlerVerifier({
    ...rest,
    excludeIps: rest.excludeIps ?? env.AI_TRAFFIC_EXCLUDE_IPS,
    staticRanges: ctx.aiCrawlers.staticRanges,
    userAgent: crawlerVerifyUserAgent(ctx),
  });
}
