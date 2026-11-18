/**
 * Serving the kit's browser bundle (client/src, built by
 * scripts/build-client.mjs into client/dist/kit-client.js).
 *
 * The bundle is small (the click beacon and the sticky-bar sizing) and
 * changes only with a kit release, so it is served under a content-hashed
 * path with an immutable cache header: /kit/client.<sha256[0:12]>.js. The
 * layout references it through kitClientScriptTag(bundle), so a new build
 * can never be shadowed by a cached old one.
 *
 * The site loads the built file once at boot:
 *
 *   const clientBundle = loadKitClientBundle(path.join(KIT_DIR, KIT_CLIENT_DIST_FILE));
 *   mountKit(app, ctx, { db, clientBundle });
 *   // layout: kitClientScriptTag(clientBundle)
 *
 * The file is a build artifact (gitignored): the site's image build runs
 * `node <kit>/scripts/build-client.mjs` before starting the server.
 */
import * as crypto from 'crypto';
import * as fs from 'fs';
import type { RequestHandler } from 'express';
import { esc } from '../links';
import { KIT_CLIENT_ASSET_PREFIX } from './clientContract';

/** Where scripts/build-client.mjs writes the bundle, relative to the kit root. */
export const KIT_CLIENT_DIST_FILE = 'client/dist/kit-client.js';

export interface KitClientBundle {
  readonly js: string;
  readonly bytes: number;
  /** sha256 of the bundle, hex. */
  readonly sha256: string;
  /** The content-hashed URL path it is served at. */
  readonly path: string;
}

/** A bundle from its source text (tests, or a site that embeds the file differently). */
export function kitClientBundleFromSource(js: string): KitClientBundle {
  if (typeof js !== 'string' || !js.trim()) throw new TypeError('kit client bundle is empty');
  const sha256 = crypto.createHash('sha256').update(js, 'utf8').digest('hex');
  return Object.freeze({
    js,
    bytes: Buffer.byteLength(js, 'utf8'),
    sha256,
    path: `${KIT_CLIENT_ASSET_PREFIX}client.${sha256.slice(0, 12)}.js`,
  });
}

/** Read the built bundle. Throws with the build command when it is missing. */
export function loadKitClientBundle(file: string): KitClientBundle {
  let js: string;
  try {
    js = fs.readFileSync(file, 'utf8');
  } catch (e) {
    throw new Error(`affiliate-kit: client bundle not found at ${file} (run \`node scripts/build-client.mjs\` in the kit): ${e instanceof Error ? e.message : String(e)}`);
  }
  return kitClientBundleFromSource(js);
}

/** GET handler for bundle.path. */
export function kitClientHandler(bundle: KitClientBundle): RequestHandler {
  const etag = `"${bundle.sha256.slice(0, 32)}"`;
  return (req, res) => {
    res.set('Content-Type', 'application/javascript; charset=utf-8');
    res.set('Cache-Control', 'public, max-age=31536000, immutable');
    res.set('ETag', etag);
    res.set('X-Content-Type-Options', 'nosniff');
    if (req.headers['if-none-match'] === etag) {
      res.status(304).end();
      return;
    }
    res.send(bundle.js);
  };
}

/** <script defer src="/kit/client.<hash>.js"></script> for the layout. */
export function kitClientScriptTag(bundle: Pick<KitClientBundle, 'path'>): string {
  return `<script defer src="${esc(bundle.path)}"></script>`;
}

/**
 * The bundle inlined in a <script> element, for a layout that prefers no
 * extra request. `</script` inside the code is escaped so it cannot close
 * the element early.
 */
export function kitClientInlineScript(bundle: Pick<KitClientBundle, 'js'>): string {
  return `<script>${bundle.js.replace(/<\/(script)/gi, '<\\/$1')}</script>`;
}
