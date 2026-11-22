/**
 * mountKit(app, ctx, deps): every HTTP route and middleware the kit owns, in
 * one call. Call it right after creating the express app, before the site's
 * own routes and its 404 handler:
 *
 *   const ctx = loadSiteContext('site.json');
 *   const app = express();
 *   const kit = mountKit(app, ctx, {
 *     db: createPgKitDb(pool),
 *     redirectLookup: pgRedirectLookup(pool, REDIRECT_SQL),
 *     llms: LLMS_CONTENT,
 *     clientBundle: loadKitClientBundle(path.join(KIT_DIR, KIT_CLIENT_DIST_FILE)),
 *   });
 *   // ... site routes ...
 *   process.on('SIGTERM', () => { kit.stop(); void kit.drain(); });
 *
 * Mounted, in order:
 *   use   X-Robots-Tag: noindex on every response (only while site.json
 *         `indexing` is "closed"; first, so nothing escapes it)
 *   use   AI-traffic logger (so it sees every page's final status)
 *   use   301 to the canonical origin for site.json `redirectFromHosts`
 *         (only when it lists hosts; /health, robots.txt, the IndexNow key
 *         file and site-verification files are served on every host)
 *   GET   /health
 *   GET   /robots.txt
 *   GET   /llms.txt
 *   GET   /<indexNow.key>.txt
 *   POST  /api/outbound-click   (click beacon)
 *   GET   /r/:asin              (only with deps.redirectLookup)
 *   GET   /kit/client.<hash>.js (only with deps.clientBundle)
 *
 * Unless deps.verifier is given, the kit's AiCrawlerVerifier is created and
 * its vendor range lists start loading now (refreshed every 12h; stop() ends
 * the timer). Pass startVerifier: false to manage that yourself.
 */
import type { IRouter } from 'express';
import type { SiteContext } from '../siteContext';
import { aiTrafficMiddleware } from './aiTraffic';
import { kitClientHandler } from './clientAsset';
import { BEACON_PATH } from './clientContract';
import { healthHandler } from './health';
import { hostRedirectMiddleware } from './hostRedirect';
import { closedIndexingMiddleware } from './indexing';
import { indexNowKeyHandler, indexNowKeyPath } from './indexNow';
import { llmsTxtHandler } from './llms';
import { outboundClickHandlers } from './outboundClick';
import { REDIRECT_ROUTE, redirectHandler } from './redirect';
import { robotsTxtHandler } from './robots';
import { createKitRuntime, type KitDeps, type KitRuntime } from './runtime';

export const HEALTH_PATH = '/health';
export const ROBOTS_PATH = '/robots.txt';
export const LLMS_PATH = '/llms.txt';

export interface MountedKit {
  readonly runtime: KitRuntime;
  /** What was mounted, e.g. 'GET /robots.txt'. */
  readonly routes: readonly string[];
  /** Resolves when every pending click / AI-traffic write has settled. */
  drain(): Promise<void>;
  /** Stops the range-list refresh of a verifier mountKit created. Idempotent. */
  stop(): void;
}

export function mountKit(app: IRouter, ctx: SiteContext, deps: KitDeps): MountedKit {
  const rt = createKitRuntime(ctx, deps);
  const routes: string[] = [];
  const keyPath = indexNowKeyPath(ctx);

  if (ctx.indexing !== 'open') {
    app.use(closedIndexingMiddleware());
    routes.push('USE closed-indexing');
  }

  app.use(aiTrafficMiddleware(rt));
  routes.push('USE ai-traffic-logger');

  if (ctx.redirectFromHosts.length) {
    app.use(hostRedirectMiddleware(ctx, { keepPaths: [HEALTH_PATH, ROBOTS_PATH, keyPath] }));
    routes.push('USE host-redirect');
  }

  app.get(HEALTH_PATH, healthHandler(ctx, rt.now));
  routes.push(`GET ${HEALTH_PATH}`);

  app.get(ROBOTS_PATH, robotsTxtHandler(ctx, deps.robots));
  routes.push(`GET ${ROBOTS_PATH}`);

  app.get(LLMS_PATH, llmsTxtHandler(rt, deps.llms));
  routes.push(`GET ${LLMS_PATH}`);

  app.get(keyPath, indexNowKeyHandler(ctx));
  routes.push(`GET ${keyPath}`);

  app.post(BEACON_PATH, ...outboundClickHandlers(rt));
  routes.push(`POST ${BEACON_PATH}`);

  if (deps.redirectLookup) {
    app.get(REDIRECT_ROUTE, redirectHandler(rt, deps.redirectLookup));
    routes.push(`GET ${REDIRECT_ROUTE}`);
  }

  if (deps.clientBundle) {
    app.get(deps.clientBundle.path, kitClientHandler(deps.clientBundle));
    routes.push(`GET ${deps.clientBundle.path}`);
  }

  const owned = rt.ownedVerifier;
  if (owned && deps.startVerifier !== false) owned.start((m) => rt.log.info(m));

  return Object.freeze({
    runtime: rt,
    routes: Object.freeze(routes),
    drain: () => rt.writer.drain(),
    stop: () => { owned?.stop(); },
  });
}
