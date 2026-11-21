/**
 * The contract between the kit's server half (server/src/http) and its
 * browser bundle (client/src, built by scripts/build-client.mjs).
 *
 * The browser bundle imports this file (and, for the link vocabulary it
 * shares with the server, links.ts), so it must stay pure: no imports, no
 * Node APIs, no DOM, no side effects. unit/index.test.ts type-checks this
 * file, links.ts and client/src with DOM typings only and fails on any Node
 * global or Node typing; unit/http/hygiene.test.ts keeps this file free of
 * imports; unit/http/clientBuild.test.ts builds the bundle for the browser
 * platform and scans it for the commonest Node-only names.
 */

/** Where the click beacon POSTs (navigator.sendBeacon, text/plain JSON). */
export const BEACON_PATH = '/api/outbound-click';
/** Body-size cap for the beacon parser (bytes); bigger bodies are ignored. */
export const BEACON_MAX_BYTES = 2048;

/**
 * Field caps: the outbound_clicks column widths (source_page VARCHAR(500),
 * user_agent VARCHAR(500)) and SpecPicks' own clips (placement 40, tag 64,
 * accept_language 100).
 */
export const BEACON_LIMITS = /* @__PURE__ */ Object.freeze({
  page: 500,
  placement: 40,
  tag: 64,
  userAgent: 500,
  acceptLanguage: 100,
});

/** The only merchant the kit tracks. */
export const BEACON_MERCHANT = 'amazon';

/** sessionStorage key counting page views in this tab's session (the beacon's `pv`). */
export const SESSION_PAGEVIEWS_KEY = 'kit_session_pv';
/** window flag that makes installing the click beacon idempotent. */
export const CLICK_BEACON_INSTALL_FLAG = '__kitClickBeacon';
/** One user action is one beacon: the same link within this window is a duplicate (click + auxclick). */
export const CLICK_DEDUPE_MS = 1000;
/** GA4 event the beacon also fires when gtag is on the page (SpecPicks' event name). */
export const GA4_CLICK_EVENT = 'amazon_click';

/** Marks the sticky mobile buy bar; the client publishes its height. */
export const STICKY_BAR_ATTR = 'data-kit-sticky-bar';
export const STICKY_BAR_CLASS = 'kit-sticky-bar';
/** Any element carrying this attribute hides the sticky bar while it is in the page (e.g. an open tray). */
export const STICKY_SUPPRESS_ATTR = 'data-kit-sticky-suppress';
/** CSS custom property on <html> holding the bar's rendered height ('0px' when hidden). */
export const STICKY_HEIGHT_VAR = '--kit-sticky-h';
/**
 * Class the client puts on <html> while the page has a sticky bar. The page
 * padding keys on it, so it works without CSS :has() (Firefox before 121);
 * body:has(.kit-sticky-bar) stays as the no-JS enhancement.
 */
export const STICKY_ACTIVE_CLASS = 'kit-has-sticky';
/** Placement of the bar's button on a buyable page. */
export const STICKY_DEFAULT_PLACEMENT = 'sticky-mobile';

/** URL prefix the built client bundle is served under (content-hashed file name). */
export const KIT_CLIENT_ASSET_PREFIX = '/kit/';
