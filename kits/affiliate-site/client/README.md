# affiliate-site kit: browser bundle

One small script (about 4 KB minified, budget 8 KB) that every kit page loads:

- **Click beacon** (`src/clickBeacon.ts`, ported from SpecPicks'
  `frontend/src/utils/affiliateClickTracking.ts`). A capture-phase listener on
  `document` sees every click on an Amazon link (Amazon storefront and
  short-link hosts, or a same-origin `/r/<ASIN>` link) and sends it to
  `POST /api/outbound-click` with `navigator.sendBeacon` (falling back to
  `fetch` with `keepalive`). The body carries the automation signals only the
  page knows: `navigator.webdriver`, page views in this tab's session, and
  milliseconds since the script loaded. The server classifies the click
  (`botDetect.classifyClick`) and writes an `outbound_clicks` row with
  `via='beacon'`: the verified-human click series. When the page has GA4's
  `gtag`, the same click is also sent as `amazon_click`.
- **Sticky-bar sizing** (`src/stickyBar.ts`). Publishes the sticky mobile
  buy bar's rendered height as `--kit-sticky-h` on `<html>` and marks
  `<html>` with `kit-has-sticky`, so the page pads its end clear of the bar
  and the footer notices stay readable (also in browsers without CSS
  `:has()`).

`src/index.ts` installs both. Nothing else runs: no framework, no cookies, no
third-party requests.

## Build

```bash
node scripts/build-client.mjs                 # client/dist/kit-client.js + kit-client.meta.json
node scripts/build-client.mjs --check         # build in memory, report the size
node scripts/build-client.mjs --outdir <dir>  # write elsewhere
```

`client/dist/` is a build artifact (gitignored). A site's image build runs the
script from its vendored kit (`vendor/affiliate-kit`) before starting the
server; esbuild must be installed at that stage. The output is deterministic.

## Serving it

```ts
import * as path from 'path';
import { KIT_CLIENT_DIST_FILE, kitClientScriptTag, loadKitClientBundle, mountKit } from '../vendor/affiliate-kit/server/src/http';

const clientBundle = loadKitClientBundle(path.join(KIT_DIR, KIT_CLIENT_DIST_FILE));
mountKit(app, ctx, { db, clientBundle });   // GET /kit/client.<sha256:12>.js, immutable
// in <head>: kitClientScriptTag(clientBundle)  ->  <script defer src="/kit/client.<hash>.js"></script>
```

`kitClientInlineScript(bundle)` inlines it instead (no extra request).

## Markup contract

| What | Rule |
| --- | --- |
| Every affiliate CTA | `data-cta-placement="<kebab-case, at most 40 chars>"` on the anchor or an ancestor (`cta()` and `amazonSearchCta()` in `links.ts` do this). Without one the beacon reports `sticky-mobile` inside the sticky bar, `in-article` inside an `<article>`, else `inline`. |
| Sticky bar | `stickyBuyBarHtml(ctx, {...})` from `server/src/http/stickyBar.ts`, once per page at the end of `<main>`; include `STICKY_BAR_CSS` (and `PRICE_STAMP_CSS`) in the page CSS. Pass `nameIsOwn: true` only for the site's own model name (otherwise the name is marked `data-amazon-content`, which keeps the 2(k) notice on the page); an Amazon-hosted `imageUrl` needs `imageFetchedAt` inside the price window; a price shows only on an `asin` bar, never beside a search button. |
| Hide the bar | Any element with `data-kit-sticky-suppress` in the page hides it (e.g. an open tray). |
| Never | `rel="noreferrer"` on CTAs (the server uses the Referer), or a second click listener that stops the event in the capture phase on `document`. |

## Wire format

`POST /api/outbound-click`, `text/plain` JSON, answered `204` with no body:

```json
{"merchant":"amazon","asin":"B0DS2X13PH","page":"/product/B0DS2X13PH","placement":"pdp-hero","tag":"example-20","pv":2,"ms":5200,"wd":false}
```

`asin` is `''` when the link names no product (a search link); `tag` is the
link's `tag` parameter (`''` for `/r/` links, which the server tags).

## Deliberate differences from SpecPicks

- Amazon only (no eBay, no cart links), and only real Amazon hosts: upstream's
  `amazon\.[a-z.]+` also matched look-alikes such as `amazon.com.example.net`.
- `auxclick` counts the middle button only; a right-click opens a menu, it
  does not follow the link.
- No React or GA4 dependency: GA4 is used only when the page already has it.
