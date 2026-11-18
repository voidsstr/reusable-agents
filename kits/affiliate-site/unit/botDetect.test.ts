/**
 * Human-vs-bot click rules. Ported from SpecPicks
 * tests/unit/affiliateOfferAndClicks.test.ts (classifyClick) and
 * productNotFoundRecovery.test.ts (uaLooksLikeBot), plus the rules and
 * helpers those files leave uncovered.
 */
import { classifyClick, countryFromHeaders, deviceType, uaLooksLikeBot } from '../server/src/botDetect';

const CHROME = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36';
const IOS_CHROME = 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/140.0 Mobile/15E148 Safari/604.1';
const HUMAN = { acceptLanguage: 'en-US,en;q=0.9', secFetchSite: 'same-origin', secChUa: '"Chromium";v="140", "Google Chrome";v="140"' };

describe('classifyClick', () => {
  it('passes a real same-origin browser click from a product page', () => {
    expect(classifyClick(CHROME, '/product/B0DS2X13PH', '73.12.1.9', HUMAN)).toEqual({ isBot: false, reason: null });
  });

  it('passes a beacon with page views and dwell time', () => {
    const v = classifyClick(CHROME, '/product/B0DS2X13PH', '73.12.1.9', { ...HUMAN, sessionPageviews: 1, msOnPage: 5200, webdriver: false });
    expect(v.isBot).toBe(false);
  });

  it.each([
    ['headless UA', 'Mozilla/5.0 HeadlessChrome/140.0.0.0', HUMAN, 'ua-bot'],
    ['playwright UA', `${CHROME} Playwright/1.62`, HUMAN, 'ua-bot'],
    ['headless client hint', CHROME, { ...HUMAN, secChUa: '"HeadlessChrome";v="140"' }, 'headless-client-hint'],
    ['missing Accept-Language', CHROME, { ...HUMAN, acceptLanguage: null }, 'no-accept-language'],
    ['Chrome UA without Sec-Fetch', CHROME, { ...HUMAN, secFetchSite: null }, 'spoofed-chrome-ua'],
    ['cross-site hit', CHROME, { ...HUMAN, secFetchSite: 'cross-site' }, 'sec-fetch-cross-site'],
    ['typed or fetched directly', CHROME, { ...HUMAN, secFetchSite: 'none' }, 'sec-fetch-none'],
    ['navigator.webdriver', CHROME, { ...HUMAN, webdriver: true }, 'webdriver'],
    ['no page view this session', CHROME, { ...HUMAN, sessionPageviews: 0 }, 'no-pageview'],
    ['instant click', CHROME, { ...HUMAN, sessionPageviews: 1, msOnPage: 50 }, 'instant-click'],
  ])('flags %s', (_label, ua, signals, reason) => {
    expect(classifyClick(ua as string, '/product/B0DS2X13PH', '73.12.1.9', signals as any)).toEqual({ isBot: true, reason });
  });

  it('flags datacenter IPs (Hetzner, Tencent, WARP)', () => {
    expect(classifyClick(CHROME, '/product/X', '88.198.4.4', HUMAN).reason).toBe('datacenter-ip');
    expect(classifyClick(CHROME, '/product/X', '43.156.1.1', HUMAN).reason).toBe('datacenter-ip');
    expect(classifyClick(CHROME, '/product/X', '104.28.9.9', HUMAN).reason).toBe('datacenter-ip');
  });

  it('flags scanner exhaust and redirect chains in the referer', () => {
    expect(classifyClick(CHROME, '/if(now()=sysdate(),sleep(15),0)', '73.12.1.9', HUMAN).reason).toBe('scanner-referer');
    expect(classifyClick(CHROME, '/web/searchList.jsp', '73.12.1.9', HUMAN).reason).toBe('scanner-referer');
    expect(classifyClick(CHROME, '/r/B0DS2X13PH', '73.12.1.9', HUMAN).reason).toBe('referer-is-redirect');
  });

  it('an empty UA is a bot; iOS Chrome (WebKit) is not a spoofed Chromium', () => {
    expect(classifyClick('', '/product/X', '73.12.1.9')).toEqual({ isBot: true, reason: 'no-ua' });
    expect(classifyClick('   ', '/product/X', '73.12.1.9').reason).toBe('no-ua');
    expect(classifyClick(IOS_CHROME, '/product/X', '73.12.1.9', { ...HUMAN, secFetchSite: null }).isBot).toBe(false);
  });

  it('keeps the legacy 3-arg behaviour for callers without signals', () => {
    expect(classifyClick(CHROME, '/product/X', '73.12.1.9')).toEqual({ isBot: false, reason: null });
    expect(classifyClick(CHROME, '', '73.12.1.9').reason).toBe('no-referer');
  });

  it('attributes a row to the strongest signal first', () => {
    // bot UA + datacenter + no referer -> the UA rule wins
    expect(classifyClick('curl/8.5.0', '', '35.1.2.3').reason).toBe('ua-bot');
  });
});

describe('uaLooksLikeBot', () => {
  it('shares the BOT_UA list with other request gates', () => {
    expect(uaLooksLikeBot('Mozilla/5.0 (compatible; AhrefsBot/7.0)')).toBe(true);
    expect(uaLooksLikeBot('python-requests/2.32')).toBe(true);
    expect(uaLooksLikeBot('got (https://github.com/sindresorhus/got)')).toBe(true);
    expect(uaLooksLikeBot(CHROME)).toBe(false);
    expect(uaLooksLikeBot(null)).toBe(false);
  });
});

describe('deviceType', () => {
  it.each([
    [CHROME, 'desktop'],
    [IOS_CHROME, 'mobile'],
    ['Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 Chrome/140 Mobile Safari/537.36', 'mobile'],
    ['Mozilla/5.0 (Linux; Android 14; SM-X710) AppleWebKit/537.36 Chrome/140 Safari/537.36', 'tablet'],
    ['Mozilla/5.0 (iPad; CPU OS 18_0 like Mac OS X)', 'tablet'],
    ['', null],
    [null, null],
  ])('%j -> %j', (ua, want) => {
    expect(deviceType(ua as string | null)).toBe(want);
  });
});

describe('countryFromHeaders', () => {
  it('reads the first edge header with a real two-letter code', () => {
    expect(countryFromHeaders({ 'cf-ipcountry': 'us' })).toBe('US');
    expect(countryFromHeaders({ 'cf-ipcountry': 'XX', 'x-geo-country': 'CA' })).toBe('CA');
    expect(countryFromHeaders({ 'x-vercel-ip-country': ['DE', 'FR'] })).toBe('DE');
    expect(countryFromHeaders({ 'cf-ipcountry': 'USA' })).toBeNull();
    expect(countryFromHeaders({})).toBeNull();
  });
});
