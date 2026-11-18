/**
 * Affiliate-site kit: HTTP middleware, routes and their browser half (K2).
 *
 * Import from here (vendor/affiliate-kit/server/src/http in a site), or from
 * the kit barrel once it re-exports this module. mountKit() mounts
 * everything; the individual handlers are exported for a site that needs a
 * different order or path.
 *
 *   mountKit.ts       mountKit(app, ctx, deps)
 *   runtime.ts        KitDeps, createKitRuntime (shared clock, writer, verifier, client address via siteClientIp)
 *   aiTraffic.ts      AI-traffic logger -> ai_traffic_log (IP-verified)
 *   outboundClick.ts  POST /api/outbound-click beacon -> outbound_clicks (via='beacon')
 *   redirect.ts       GET /r/:asin -> Amazon, logged (via='redirect')
 *   robots.ts         robots.txt from SiteContext.crawlers (search + agent allowed, training blocked)
 *   llms.ts           llms.txt (site copy + assistant-cited pages + latest articles)
 *   indexNow.ts       GET /<key>.txt
 *   health.ts         GET /health
 *   clientAsset.ts    serving the built browser bundle under a content hash
 *   stickyBar.ts      sticky mobile buy bar markup + CSS
 *   clientContract.ts constants shared with client/src
 *   pgKitDb.ts        KitDb on node-postgres; pgRedirectLookup for the site's catalog SQL
 *   backgroundWriter.ts  capped fire-and-forget writes (AI-log rows shed before click rows)
 */
export * from './types';
export * from './clientContract';
export * from './backgroundWriter';
export * from './runtime';
export * from './aiTraffic';
export * from './outboundClick';
export * from './redirect';
export * from './robots';
export * from './llms';
export * from './indexNow';
export * from './health';
export * from './clientAsset';
export * from './stickyBar';
export * from './pgKitDb';
export * from './mountKit';
