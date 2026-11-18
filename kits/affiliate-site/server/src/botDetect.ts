/**
 * Outbound-click bot classification.
 *
 * Ported from SpecPicks src/services/botDetect.ts (see PORTED_FROM.json); the
 * rules are site-agnostic and unchanged.
 *
 * WHY THIS EXISTS. SpecPicks' /r/:key logged every GET into outbound_clicks
 * with no filtering, so the table counted crawlers as Amazon clicks. On
 * 2026-09-21 its 14-day Amazon "click" count was 10,629 while GA4 -- which
 * needs real JS in a real browser -- recorded 5 amazon_click events over 28
 * days. The inflated number was the whole number:
 *   - `Sogou web spider/4.0` was in the top 15 user agents by itself
 *   - 7,927 distinct ASINs were "clicked" in 14 days, top ASIN only 18 times
 *     (a crawler walking the catalog; humans cluster on a few products)
 *   - user-agent counts were flat across a dozen Chrome versions -- a
 *     UA-rotating scraper pool, not an audience
 *   - top IPs were Cloudflare WARP (104.28.x) and Tencent Cloud (43.x)
 *   - 10,540 of 10,629 rows carried no referer at all
 * Once classified, the honest number was ~130 clicks from ~64 people over 45
 * days.
 *
 * DESIGN: we CLASSIFY, we do not DROP. Every row is still written so the audit
 * trail stays complete and a wrong heuristic can be re-run over history.
 * Consumers filter `WHERE NOT is_bot`; `bot_reason` records WHICH rule fired
 * so a rule can be revisited in isolation.
 */

/** UA substrings that self-identify as automation. Lowercased comparison. */
const BOT_UA = new RegExp(
  [
    'bot', 'spider', 'crawler', 'slurp', 'bingpreview', 'headless', 'phantom',
    'python', 'curl', 'wget', 'scrapy', 'http-client', 'httpclient', 'okhttp',
    'java/', 'go-http', 'libwww', 'lwp::', 'apache-httpclient', 'axios',
    'facebookexternalhit', 'semrush', 'ahrefs', 'mj12', 'dotbot', 'petal',
    'yandex', 'bytespider', 'gptbot', 'claudebot', 'ccbot', 'perplexity',
    'applebot', 'amazonbot', 'dataforseo', 'serpstat', 'screaming frog',
    'preview', 'monitor', 'uptime', 'pingdom', 'lighthouse', 'pagespeed',
    // Headless / automation-framework fingerprints. `headless` above already
    // catches HeadlessChrome; these catch the frameworks that drive a full
    // browser but still leave their name in the UA.
    'puppeteer', 'playwright', 'selenium', 'webdriver', 'phantomjs', 'cypress',
    'electron/', 'node-fetch', 'undici', 'got \\(', 'postman', 'insomnia',
    'httpie', 'aiohttp', 'guzzle', 'scrapingbee', 'zenrows', 'apify',
  ].join('|'),
  'i',
);

/**
 * True when the user agent names itself as automation (the BOT_UA list).
 * Exported so other request gates share one list instead of copying it.
 */
export function uaLooksLikeBot(userAgent: string | null | undefined): boolean {
  return BOT_UA.test(String(userAgent || ''));
}

/**
 * Referer values that are attack-scanner exhaust rather than a page on this
 * site, e.g. `/if(now()=sysdate(),sleep(15),0)` (a timing-based SQLi probe)
 * and `/web/searchList.jsp` (a path from someone else's stack).
 */
const SCANNER_REFERER = /[()><']|sysdate|sleep\(|union\s+select|\bselect\b.*\bfrom\b|\.jsp|\.php|\.asp|etc\/passwd|\.\.\//i;

/**
 * Datacenter IPv4 prefixes seen driving the fake clicks, plus the major cloud
 * ranges. Deliberately a coarse prefix list, not an ASN lookup: there is no
 * ASN database on the host and a partial-but-honest signal beats a dependency
 * that can fail open. A miss here just means the row falls through to the
 * other rules.
 */
const DATACENTER_PREFIXES = [
  '43.',        // Tencent Cloud (43.156.x, 43.163.x, 43.133.x observed)
  '104.28.',    // Cloudflare WARP
  '172.104.',   // Linode
  '139.162.',   // Linode
  '45.79.',     // Linode
  '159.89.',    // DigitalOcean
  '167.71.',    // DigitalOcean
  '134.209.',   // DigitalOcean
  '165.227.',   // DigitalOcean
  '207.154.',   // DigitalOcean
  '51.15.',     // Scaleway
  '163.172.',   // Scaleway
  '35.',        // GCP
  '34.',        // GCP
  '52.',        // AWS
  '54.',        // AWS
  '18.',        // AWS
  '13.',        // Azure / AWS
  '20.',        // Azure
  '40.',        // Azure
  // Other hosting providers common in scraper pools.
  '5.9.', '88.198.', '78.46.', '95.216.', '116.202.', '135.181.', '65.108.', '65.21.', // Hetzner
  '51.68.', '51.75.', '51.77.', '51.79.', '51.81.', '51.83.', '51.89.', '51.91.',
  '51.161.', '51.178.', '51.195.', '51.210.', '51.222.', '54.36.', '54.37.', '54.38.',
  '147.135.', '149.56.', '158.69.', '192.99.', '198.27.',                             // OVH
  '129.146.', '132.145.', '150.136.', '152.67.', '158.101.', '193.122.',              // Oracle Cloud
  '66.249.',    // Googlebot / Google crawl infrastructure
  '157.55.', '207.46.',                                                               // Bingbot
];

export interface BotVerdict {
  isBot: boolean;
  /** Which rule decided it; null when the row looks human. */
  reason: string | null;
}

/**
 * Extra request signals. All optional so a caller that cannot supply one
 * simply skips that rule rather than failing a human.
 *
 *  - `acceptLanguage`: every real browser sends Accept-Language on a
 *    navigation AND on a sendBeacon POST. HTTP libraries and most headless
 *    scrapers do not. Present-but-empty = bot.
 *  - `secFetchSite` / `secChUa`: Chromium >= 76, Firefox >= 90 and Safari >=
 *    16.4 send Sec-Fetch-* on every request. A UA claiming modern Chrome with
 *    no Sec-Fetch-Site is a spoofed UA string on a non-browser client.
 *    `Sec-CH-UA` naming HeadlessChrome is headless even when the UA was
 *    rewritten to look like desktop Chrome.
 *  - `sessionPageviews` / `msOnPage` / `webdriver`: only the JS beacon knows
 *    these. No page view in this tab's session, a click under 400 ms after
 *    load, or navigator.webdriver=true are all automation fingerprints.
 */
export interface ClickSignals {
  acceptLanguage?: string | null;
  secFetchSite?: string | null;
  secChUa?: string | null;
  sessionPageviews?: number | null;
  msOnPage?: number | null;
  webdriver?: boolean | null;
}

/** Chromium major version from a UA, or 0 when it is not Chromium. */
function chromiumMajor(ua: string): number {
  // iOS Chrome (CriOS) is WebKit and follows Safari's header behaviour.
  if (/CriOS|FxiOS|EdgiOS/.test(ua)) return 0;
  const m = ua.match(/(?:Chrome|Chromium)\/(\d+)/);
  return m ? parseInt(m[1] as string, 10) : 0;
}

/**
 * Classify one outbound-click hit.
 *
 * `refererPath` is the already-normalised pathname of the Referer header (the
 * caller derives it), NOT the raw header.
 *
 * On the no-referer rule: a human reaching the redirect always arrives by
 * clicking a link rendered on one of our pages, and our CTAs use
 * rel="sponsored nofollow noopener" -- noopener does not strip the Referer
 * (only noreferrer would). So a hit with no referer is a direct fetch, which
 * is what a crawler walking discovered URLs does. This rule is listed last so
 * a row that also trips a stronger signal is attributed to that signal.
 */
export function classifyClick(
  userAgent: string | null | undefined,
  refererPath: string | null | undefined,
  ip: string | null | undefined,
  signals: ClickSignals = {},
): BotVerdict {
  const ua = (userAgent || '').trim();
  if (!ua) return { isBot: true, reason: 'no-ua' };
  if (BOT_UA.test(ua)) return { isBot: true, reason: 'ua-bot' };
  if (signals.webdriver) return { isBot: true, reason: 'webdriver' };
  if (signals.secChUa && /headless/i.test(signals.secChUa)) {
    return { isBot: true, reason: 'headless-client-hint' };
  }
  if ('acceptLanguage' in signals && !(signals.acceptLanguage || '').trim()) {
    return { isBot: true, reason: 'no-accept-language' };
  }
  if ('secFetchSite' in signals) {
    const sfs = (signals.secFetchSite || '').trim().toLowerCase();
    if (!sfs && chromiumMajor(ua) >= 80) return { isBot: true, reason: 'spoofed-chrome-ua' };
    // A click on one of our CTAs is a same-origin request. `cross-site` means
    // another site is hot-linking the endpoint; `none` means the URL was
    // typed/pasted or fetched directly -- neither is a click on our page.
    if (sfs === 'cross-site' || sfs === 'none') return { isBot: true, reason: `sec-fetch-${sfs}` };
  }
  if (signals.sessionPageviews != null && signals.sessionPageviews < 1) {
    return { isBot: true, reason: 'no-pageview' };
  }
  if (signals.msOnPage != null && signals.msOnPage < 400) {
    return { isBot: true, reason: 'instant-click' };
  }

  const ref = (refererPath || '').trim();
  if (ref && SCANNER_REFERER.test(ref)) return { isBot: true, reason: 'scanner-referer' };

  const addr = (ip || '').trim();
  if (addr && DATACENTER_PREFIXES.some((p) => addr.startsWith(p))) {
    return { isBot: true, reason: 'datacenter-ip' };
  }

  // A /r/ referer means the "page" they clicked from was itself a redirect --
  // only a crawler follows a redirect chain back into another redirect.
  if (ref.startsWith('/r/')) return { isBot: true, reason: 'referer-is-redirect' };

  if (!ref) return { isBot: true, reason: 'no-referer' };

  return { isBot: false, reason: null };
}

/** Coarse device class from the UA, for the `device_type` column. */
export function deviceType(userAgent: string | null | undefined): string | null {
  const ua = (userAgent || '').toLowerCase();
  if (!ua) return null;
  if (/ipad|tablet|kindle|playbook|silk/.test(ua)) return 'tablet';
  if (/mobi|iphone|ipod|android.*mobile|windows phone/.test(ua)) return 'mobile';
  if (/android/.test(ua)) return 'tablet';
  return 'desktop';
}

/**
 * Two-letter country from whichever edge header is present. Azure Container
 * Apps does not add one itself, so this is populated only when a CDN in front
 * supplies it; null is the honest answer otherwise.
 */
export function countryFromHeaders(
  headers: Record<string, string | string[] | undefined>,
): string | null {
  const candidates = [
    'cf-ipcountry',            // Cloudflare
    'x-vercel-ip-country',     // Vercel
    'x-geo-country',
    'x-appengine-country',     // GAE
    'x-client-geo-country',
  ];
  for (const h of candidates) {
    const v = headers[h];
    const s = Array.isArray(v) ? v[0] : v;
    if (s && typeof s === 'string' && /^[A-Za-z]{2}$/.test(s.trim()) && s.trim().toUpperCase() !== 'XX') {
      return s.trim().toUpperCase();
    }
  }
  return null;
}
