/**
 * The browser bundle's logic, in node with small DOM fakes. The real-browser
 * run (sendBeacon over HTTP, layout, ResizeObserver) is clientBrowser.test.ts.
 */
import {
  amazonClickTarget, asinFromAmazonUrl, beaconBody, countPageview, createClickHandler, installClickBeacon,
  pageTypeFor, placementFor, sessionPageviews, type BeaconEnv,
} from '../../client/src/clickBeacon';
import { installStickyBar, stickyBarHeight } from '../../client/src/stickyBar';
import {
  BEACON_PATH, CLICK_BEACON_INSTALL_FLAG, GA4_CLICK_EVENT, SESSION_PAGEVIEWS_KEY, STICKY_ACTIVE_CLASS, STICKY_HEIGHT_VAR,
} from '../../server/src/http/clientContract';
import { parseBeaconPayload } from '../../server/src/http/outboundClick';

// ─── a minimal element tree: tag, attributes, parent, closest() ────────────

class El {
  readonly attrs: Record<string, string>;
  constructor(readonly tag: string, attrs: Record<string, string> = {}, readonly parent: El | null = null, readonly href?: unknown) {
    this.attrs = attrs;
  }

  getAttribute(name: string): string | null {
    return Object.prototype.hasOwnProperty.call(this.attrs, name) ? this.attrs[name] as string : null;
  }

  private matches(sel: string): boolean {
    const attr = sel.match(/^\[([a-z-]+)\]$/);
    if (attr) return Object.prototype.hasOwnProperty.call(this.attrs, attr[1] as string);
    return this.tag === sel;
  }

  closest(sel: string): El | null {
    for (let e: El | null = this; e; e = e.parent) if (e.matches(sel)) return e;
    return null;
  }

  child(tag: string, attrs: Record<string, string> = {}, href?: unknown): El {
    return new El(tag, attrs, this, href);
  }
}

const asEl = (e: El): Element => e as unknown as Element;

function link(href: string, attrs: Record<string, string> = {}, parent: El = new El('main')): { a: El; inner: El } {
  const a = parent.child('a', { href, ...attrs }, href);
  return { a, inner: a.child('span') };
}

interface Sent { url: string; body: string }

function env(over: Partial<BeaconEnv> & { store?: Map<string, string> | 'blocked'; sent?: Sent[]; fetched?: Sent[]; beaconOk?: boolean; gtagCalls?: unknown[][] } = {}): BeaconEnv {
  const store = over.store ?? new Map<string, string>([[SESSION_PAGEVIEWS_KEY, '2']]);
  const sent = over.sent ?? [];
  const fetched = over.fetched ?? [];
  let t = 1000;
  return {
    location: { href: 'https://example.com/product/B0DS2X13PH', origin: 'https://example.com', pathname: '/product/B0DS2X13PH' },
    storage: () => {
      if (store === 'blocked') throw new Error('SecurityError');
      return { getItem: (k: string) => store.get(k) ?? null, setItem: (k: string, v: string) => { store.set(k, v); } };
    },
    navigator: { sendBeacon: (url: string, data?: BodyInit | null) => { sent.push({ url, body: String(data) }); return over.beaconOk ?? true; }, webdriver: false },
    fetch: async (url: string, init?: RequestInit) => { fetched.push({ url, body: String(init?.body) }); return {}; },
    gtag: () => (over.gtagCalls ? (...args: unknown[]) => { over.gtagCalls!.push(args); } : null),
    now: () => (t += 100),
    loadedAt: 0,
    ...over,
  };
}

function click(target: unknown, type = 'click', button = 0): Event {
  return { type, button, target } as unknown as Event;
}

describe('URL helpers', () => {
  it.each([
    ['https://www.amazon.com/dp/B0DS2X13PH?tag=x-20', 'B0DS2X13PH'],
    ['https://www.amazon.com/Acme-Station/dp/b0ds2x13ph/ref=sr_1', 'B0DS2X13PH'],
    ['https://www.amazon.com/gp/product/B0DS2X13PH', 'B0DS2X13PH'],
    ['https://www.amazon.com/gp/aw/d/B0DS2X13PH', 'B0DS2X13PH'],
    ['https://www.amazon.com/s?k=power+station&asin=B0DS2X13PH', 'B0DS2X13PH'],
    ['https://www.amazon.com/s?k=power+station', ''],
    ['https://www.amazon.com/dp/B0DS2X13PHX', ''],
  ])('asinFromAmazonUrl(%s) = %j', (u, want) => {
    expect(asinFromAmazonUrl(new URL(u))).toBe(want);
  });

  it('recognises Amazon links, same-origin /r/ links, and nothing else', () => {
    const o = 'https://example.com';
    expect(amazonClickTarget(new URL('https://www.amazon.com/dp/B0DS2X13PH?tag=kitexample-20'), o))
      .toEqual({ asin: 'B0DS2X13PH', viaRedirect: false, tag: 'kitexample-20', tagged: true });
    expect(amazonClickTarget(new URL('https://amzn.to/3xYz'), o)).toEqual({ asin: '', viaRedirect: false, tag: '', tagged: false });
    expect(amazonClickTarget(new URL('https://smile.amazon.co.uk/dp/B0DS2X13PH'), o)?.asin).toBe('B0DS2X13PH');
    expect(amazonClickTarget(new URL('https://example.com/r/B0DS2X13PH'), o)).toEqual({ asin: 'B0DS2X13PH', viaRedirect: true, tag: '', tagged: true });
    expect(amazonClickTarget(new URL('https://example.com/r/B0DS2X13PH/'), o)?.viaRedirect).toBe(true);
    expect(amazonClickTarget(new URL('https://example.com/product/B0DS2X13PH'), o)).toBeNull();
    expect(amazonClickTarget(new URL('https://example.com/r/short'), o)).toBeNull();
    expect(amazonClickTarget(new URL('https://other.example/r/B0DS2X13PH'), o)).toBeNull();
    expect(amazonClickTarget(new URL('https://amazon.com.example.net/dp/B0DS2X13PH'), o)).toBeNull();
    expect(amazonClickTarget(new URL('https://notamazon.com/dp/B0DS2X13PH'), o)).toBeNull();
    expect(amazonClickTarget(new URL('ftp://www.amazon.com/dp/B0DS2X13PH'), o)).toBeNull();
  });

  it('pageTypeFor buckets by first path segment', () => {
    expect(pageTypeFor('/')).toBe('home');
    expect(pageTypeFor('/product/B0DS2X13PH')).toBe('product');
    expect(pageTypeFor('/guides/x/')).toBe('guides');
  });
});

describe('placementFor', () => {
  it('uses the nearest data-cta-placement (the anchor\'s own first)', () => {
    const section = new El('section', { 'data-cta-placement': 'pdp-alt' });
    expect(placementFor(asEl(link('https://www.amazon.com/dp/B0DS2X13PH', { 'data-cta-placement': 'pdp-hero' }, section).a))).toBe('pdp-hero');
    expect(placementFor(asEl(link('https://www.amazon.com/dp/B0DS2X13PH', {}, section).a))).toBe('pdp-alt');
    expect(placementFor(asEl(link('https://www.amazon.com/dp/B0DS2X13PH', { 'data-cta-placement': 'Not Valid' }).a))).toBe('tagged');
  });

  it('falls back to the kit markup around the anchor', () => {
    const bar = new El('div', { 'data-kit-sticky-bar': '' });
    expect(placementFor(asEl(link('https://www.amazon.com/dp/B0DS2X13PH', {}, bar).a))).toBe('sticky-mobile');
    expect(placementFor(asEl(link('https://www.amazon.com/dp/B0DS2X13PH', {}, new El('p', {}, new El('article'))).a))).toBe('in-article');
    expect(placementFor(asEl(link('https://www.amazon.com/dp/B0DS2X13PH').a))).toBe('inline');
  });
});

describe('createClickHandler', () => {
  it('sends one beacon the server accepts, with the page signals', () => {
    const sent: Sent[] = [];
    const h = createClickHandler(env({ sent }));
    h(click(link('https://www.amazon.com/dp/B0DS2X13PH?tag=kitexample-pdp-20&linkCode=ll1', { 'data-cta-placement': 'pdp-hero' }).inner));
    expect(sent).toHaveLength(1);
    expect(sent[0]?.url).toBe(BEACON_PATH);
    expect(JSON.parse(sent[0]!.body)).toEqual({
      merchant: 'amazon', asin: 'B0DS2X13PH', page: '/product/B0DS2X13PH', placement: 'pdp-hero',
      tag: 'kitexample-pdp-20', pv: 2, ms: 1100, wd: false,
    });
    // the server-side parser takes it as-is
    expect(parseBeaconPayload(sent[0]!.body)).toMatchObject({ asin: 'B0DS2X13PH', placement: 'pdp-hero', pv: 2, ms: 1100 });
  });

  it('counts one action once (click + middle-button auxclick), but a later click again', () => {
    const sent: Sent[] = [];
    let t = 0;
    const h = createClickHandler(env({ sent, now: () => t }));
    const { a } = link('https://www.amazon.com/dp/B0DS2X13PH');
    h(click(a));
    t = 10;
    h(click(a, 'auxclick', 1));
    expect(sent).toHaveLength(1);
    t = 2000;
    h(click(a));
    expect(sent).toHaveLength(2);
  });

  it('ignores right-button auxclicks, non-elements, links without href and non-Amazon links', () => {
    const sent: Sent[] = [];
    const h = createClickHandler(env({ sent }));
    h(click(link('https://www.amazon.com/dp/B0DS2X13PH').a, 'auxclick', 2));
    h(click(null));
    h(click({ nodeType: 3 }));
    h(click(new El('main').child('a', {})));
    h(click(link('https://example.com/guides/x').a));
    h(click(new El('div')));
    expect(sent).toEqual([]);
  });

  it('reads href from the attribute when the property is not a string (SVG <a>)', () => {
    const sent: Sent[] = [];
    const h = createClickHandler(env({ sent }));
    const svgA = new El('main').child('a', { href: 'https://www.amazon.com/dp/B0DS2X13PH' }, { baseVal: '' });
    h(click(svgA));
    expect(sent).toHaveLength(1);
  });

  it('fires GA4 amazon_click when the page has gtag, and a throwing gtag never blocks the beacon', () => {
    const gtagCalls: unknown[][] = [];
    const sent: Sent[] = [];
    createClickHandler(env({ sent, gtagCalls }))(click(link('https://example.com/r/B0DS2X13PH').a));
    expect(gtagCalls).toEqual([['event', GA4_CLICK_EVENT, {
      asin: 'B0DS2X13PH', page_slug: '/product/B0DS2X13PH', page_type: 'product', placement: 'inline', merchant: 'amazon', tagged: true,
    }]]);
    const sent2: Sent[] = [];
    createClickHandler(env({ sent: sent2, gtag: () => () => { throw new Error('ga broken'); } }))(click(link('https://www.amazon.com/dp/B0DS2X13PH').a));
    expect(sent2).toHaveLength(1);
  });

  it('falls back to fetch keepalive when sendBeacon is missing or refuses', () => {
    const fetched: Sent[] = [];
    createClickHandler(env({ fetched, beaconOk: false }))(click(link('https://www.amazon.com/dp/B0DS2X13PH').a));
    createClickHandler(env({ fetched, navigator: null }))(click(link('https://www.amazon.com/dp/B0DS2X13PH').a));
    expect(fetched.map((f) => f.url)).toEqual([BEACON_PATH, BEACON_PATH]);
  });

  it('reports navigator.webdriver, and one page view when storage is blocked', () => {
    const sent: Sent[] = [];
    createClickHandler(env({ sent, store: 'blocked', navigator: { sendBeacon: (_u, d) => { sent.push({ url: _u, body: String(d) }); return true; }, webdriver: true } }))(
      click(link('https://www.amazon.com/dp/B0DS2X13PH').a),
    );
    expect(JSON.parse(sent[0]!.body)).toMatchObject({ wd: true, pv: 1 });
  });

  it('never throws, whatever the page does', () => {
    const h = createClickHandler(env({ navigator: { sendBeacon: () => { throw new Error('boom'); } }, fetch: () => { throw new Error('boom'); } }));
    expect(() => h(click(link('https://www.amazon.com/dp/B0DS2X13PH').a))).not.toThrow();
  });
});

describe('session page views', () => {
  it('counts up per page load and survives blocked storage', () => {
    const store = new Map<string, string>();
    const e = env({ store });
    expect(sessionPageviews(e)).toBe(0);
    countPageview(e);
    countPageview(e);
    expect(store.get(SESSION_PAGEVIEWS_KEY)).toBe('2');
    const blocked = env({ store: 'blocked' });
    expect(() => countPageview(blocked)).not.toThrow();
    expect(sessionPageviews(blocked)).toBe(1);
  });

  it('beaconBody is the wire format', () => {
    expect(JSON.parse(beaconBody({ asin: '', page: '/', placement: 'inline', tag: '', pv: 1, ms: 0, wd: false })))
      .toEqual({ merchant: 'amazon', asin: '', page: '/', placement: 'inline', tag: '', pv: 1, ms: 0, wd: false });
  });
});

// ─── install functions against a fake window ──────────────────────────────

function fakeWindow(bars: Array<{ height: number }> = []) {
  const listeners: Array<{ type: string; capture: unknown }> = [];
  const winListeners: string[] = [];
  const props = new Map<string, string>();
  const classes = new Set<string>();
  const observed: unknown[] = [];
  const store = new Map<string, string>();
  const w = {
    document: {
      addEventListener: (type: string, _fn: unknown, capture: unknown) => { listeners.push({ type, capture }); },
      querySelectorAll: () => bars.map((b) => ({ getBoundingClientRect: () => ({ height: b.height }) })),
      documentElement: {
        style: { setProperty: (k: string, v: string) => { props.set(k, v); }, removeProperty: (k: string) => { props.delete(k); } },
        classList: { add: (c: string) => { classes.add(c); }, remove: (c: string) => { classes.delete(c); } },
      },
    },
    location: { href: 'https://example.com/', origin: 'https://example.com', pathname: '/' },
    navigator: {},
    sessionStorage: { getItem: (k: string) => store.get(k) ?? null, setItem: (k: string, v: string) => { store.set(k, v); } },
    performance: { now: () => 5 },
    addEventListener: (type: string) => { winListeners.push(type); },
    removeEventListener: (type: string) => { winListeners.splice(winListeners.indexOf(type), 1); },
    ResizeObserver: class { constructor(readonly cb: () => void) {} observe(el: unknown) { observed.push(el); } disconnect() { observed.length = 0; } },
  };
  return { w: w as unknown as Window, listeners, winListeners, props, classes, observed, store, raw: w as unknown as Record<string, unknown> };
}

describe('installClickBeacon', () => {
  it('installs capture-phase click and auxclick listeners once, and counts the page view', () => {
    const f = fakeWindow();
    expect(installClickBeacon(f.w)).toBe(true);
    expect(f.listeners).toEqual([{ type: 'click', capture: true }, { type: 'auxclick', capture: true }]);
    expect(f.store.get(SESSION_PAGEVIEWS_KEY)).toBe('1');
    expect(f.raw[CLICK_BEACON_INSTALL_FLAG]).toBe(true);
    expect(installClickBeacon(f.w)).toBe(false);
    expect(f.listeners).toHaveLength(2);
  });

  it('is a no-op outside a browser', () => {
    expect(installClickBeacon()).toBe(false);
  });
});

describe('installStickyBar', () => {
  it('publishes the tallest bar\'s height and keeps it current', () => {
    const f = fakeWindow([{ height: 71.6 }, { height: 0 }]);
    const off = installStickyBar(f.w);
    expect(f.props.get(STICKY_HEIGHT_VAR)).toBe('72px');
    expect(f.observed).toHaveLength(2);
    expect(f.winListeners.sort()).toEqual(['orientationchange', 'resize']);
    expect(installStickyBar(f.w)).not.toBe(off); // second install is a no-op
    off();
    expect(f.props.has(STICKY_HEIGHT_VAR)).toBe(false);
    expect(f.winListeners).toEqual([]);
  });

  it('marks <html> while the page has a bar, so the padding works without CSS :has()', () => {
    const f = fakeWindow([{ height: 64 }]);
    const off = installStickyBar(f.w);
    expect([...f.classes]).toEqual([STICKY_ACTIVE_CLASS]);
    off();
    expect(f.classes.size).toBe(0);
  });

  it('does nothing on a page without a bar', () => {
    const f = fakeWindow([]);
    installStickyBar(f.w)();
    expect(f.props.size).toBe(0);
    expect(f.classes.size).toBe(0);
    expect(f.observed).toEqual([]);
  });

  it('stickyBarHeight is the rounded max', () => {
    expect(stickyBarHeight([])).toBe(0);
    expect(stickyBarHeight([{ getBoundingClientRect: () => ({ height: 10.4 }) as DOMRect }])).toBe(10);
  });
});
