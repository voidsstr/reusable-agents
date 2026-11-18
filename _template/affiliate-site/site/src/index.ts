/**
 * The site's server: the kit's routes and middleware first (mountKit: the
 * AI-traffic logger, /health, /robots.txt, /llms.txt, the IndexNow key, the
 * click beacon, the browser bundle), then every page and sitemap route in
 * src/routes/table.ts, then the 404.
 *
 * createApp() takes its database, article store and clock as arguments, so
 * tests exercise it without a server or a database. main() is the process:
 *
 *   1. load site.json (the kit validates it; a bad file stops the boot);
 *   2. connect, then make sure the schema is current:
 *        MIGRATE_ON_BOOT=1  apply the kit's migrations, then db/migrations
 *                           (the deploy gate's fixture app does this);
 *        otherwise          refuse to serve while any migration is pending:
 *                           production applies them in the deploy's migrate
 *                           stage, as the owner role (deploy.env MIGRATE_COMMAND);
 *   3. listen on PORT; on SIGTERM drain the pending click writes and exit.
 *
 * Environment: PORT, DATABASE_URL, PG_POOL_MAX, MIGRATE_ON_BOOT,
 * SITE_HERMETIC (1: never fetch the AI crawlers' published IP ranges; the
 * gate sets it so a gate run stays off the network), SITE_ROOT,
 * PRICE_FRESH_HOURS and AI_TRAFFIC_EXCLUDE_IPS (read by the kit).
 */
import express, { type Express, type NextFunction, type Request, type RequestHandler, type Response } from 'express';
import type { Pool } from 'pg';
import { htmlCacheControl, type SiteContext } from '../vendor/affiliate-kit/server/src';
import { kitMigrationSources, pendingMigrations, runKitMigrations } from '../vendor/affiliate-kit/server/src/db';
import {
  createPgKitDb,
  loadKitClientBundle,
  mountKit,
  type KitClientBundle,
  type KitDb,
  type LlmsTxtContent,
  type MountedKit,
} from '../vendor/affiliate-kit/server/src/http';
import {
  sitemapFileResponse,
  sitemapIndexResponse,
  sitemapUnavailableResponse,
  type SitemapHttpResponse,
  type SitemapSet,
} from '../vendor/affiliate-kit/server/src/render';
import { findSiteRoot, loadLlmsContent, loadSite, sitePaths, type SitePaths } from './config/site';
import { PgArticleStore, type ArticleStore } from './db/articles';
import { createPool } from './db/pool';
import { SiteRenderer } from './render/pages';
import { siteSitemapSet } from './render/sitemaps';
import { ROUTES, type PageHandlerId, type SitemapHandlerId } from './routes/table';

export interface AppDeps {
  ctx: SiteContext;
  /** Where click and AI-traffic rows go (createPgKitDb(pool) in production). */
  kitDb: KitDb;
  articles: ArticleStore;
  llms: LlmsTxtContent;
  /** The built kit browser bundle (click beacon, sticky-bar sizing); null only in tests. */
  clientBundle: KitClientBundle | null;
  /** Start the AI-crawler verifier's range-list refresh (default true). */
  startVerifier?: boolean;
  now?: () => number;
  log?: (line: string) => void;
}

export interface SiteApp {
  app: Express;
  kit: MountedKit;
}

/** How many guides the home page and the index list. */
export const HOME_GUIDES = 6;
export const INDEX_GUIDES = 200;

export function createApp(deps: AppDeps): SiteApp {
  const { ctx, articles } = deps;
  const now = deps.now ?? Date.now;
  const log = deps.log ?? ((line: string) => console.log(line));
  const render = new SiteRenderer(ctx, deps.clientBundle, now);
  const app = express();
  app.disable('x-powered-by');
  // The client address comes from X-Forwarded-For through the kit (siteClientIp, site.json edge), never from express.
  app.set('trust proxy', false);

  const kit = mountKit(app, ctx, {
    db: deps.kitDb,
    llms: deps.llms,
    clientBundle: deps.clientBundle ?? undefined,
    startVerifier: deps.startVerifier,
    now,
    log: { info: (m) => log(m), error: (m) => log(m) },
  });

  const html = (res: Response, status: number, body: string): void => {
    res.status(status);
    res.set('Content-Type', 'text/html; charset=utf-8');
    // 200s carry the kit's HTML cache rule (s-maxage <= 600, which bounds how old a shown price can be).
    res.set('Cache-Control', status === 200 ? htmlCacheControl(ctx) : 'no-store');
    res.send(body);
  };

  /** A page handler whose failure renders the 500 page, never a stack trace. */
  const page = (fn: (req: Request, res: Response) => Promise<void>): RequestHandler => (req, res) => {
    fn(req, res).catch((e: unknown) => {
      log(`[site] ${req.method} ${req.path} failed: ${e instanceof Error ? e.stack ?? e.message : String(e)}`);
      if (!res.headersSent) html(res, 500, render.serverError(req.path));
    });
  };
  const notFound = (req: Request, res: Response): void => html(res, 404, render.notFound(req.path));

  const sendSitemap = (res: Response, r: SitemapHttpResponse): void => {
    res.status(r.status);
    for (const [k, v] of Object.entries(r.headers)) res.set(k, v);
    res.send(r.body);
  };
  const sitemapSet = async (): Promise<SitemapSet> => siteSitemapSet(ctx, { articles: await articles.sitemapEntries() });

  const pages: Record<PageHandlerId, RequestHandler> = {
    home: page(async (_req, res) => html(res, 200, render.home(await articles.latest(HOME_GUIDES)))),
    'guides-index': page(async (_req, res) => html(res, 200, render.guidesIndex(await articles.latest(INDEX_GUIDES)))),
    guide: page(async (req, res) => {
      const a = await articles.bySlug(String(req.params.slug));
      if (!a) return notFound(req, res);
      html(res, 200, render.guide(a));
    }),
    about: page(async (_req, res) => html(res, 200, render.about())),
    disclosure: page(async (_req, res) => html(res, 200, render.disclosure())),
    privacy: page(async (_req, res) => html(res, 200, render.privacy())),
  };
  const sitemaps: Record<SitemapHandlerId, RequestHandler> = {
    'sitemap-index': page(async (_req, res) => {
      try {
        sendSitemap(res, sitemapIndexResponse(await sitemapSet()));
      } catch (e) {
        sendSitemap(res, sitemapUnavailableResponse(e instanceof Error ? e.message : String(e), 'sitemapindex'));
      }
    }),
    'sitemap-file': page(async (req, res) => {
      try {
        sendSitemap(res, sitemapFileResponse(await sitemapSet(), `/sitemap-${String(req.params.name)}.xml`));
      } catch (e) {
        sendSitemap(res, sitemapUnavailableResponse(e instanceof Error ? e.message : String(e)));
      }
    }),
  };

  for (const r of ROUTES) {
    if (r.kind === 'page') app.get(r.pattern, pages[r.handler]);
    else if (r.kind === 'sitemap') app.get(r.pattern, sitemaps[r.handler]);
  }
  app.use(notFound);
  // express's error handler: a malformed request that throws before a handler runs.
  app.use((err: unknown, req: Request, res: Response, _next: NextFunction) => {
    log(`[site] ${req.method} ${req.path} failed: ${err instanceof Error ? err.message : String(err)}`);
    if (!res.headersSent) html(res, 500, render.serverError(req.path));
  });
  return { app, kit };
}

/**
 * Make sure the schema is current before serving: apply the migrations
 * (MIGRATE_ON_BOOT=1), or refuse to start while any is pending.
 */
export async function prepareSchema(pool: Pool, paths: SitePaths, env: NodeJS.ProcessEnv, log: (line: string) => void): Promise<void> {
  if (env.MIGRATE_ON_BOOT === '1') {
    const s = await runKitMigrations(pool, { siteMigrationsDir: paths.siteMigrationsDir, log });
    log(`[site] migrations: ${s.applied.length} applied, ${s.onDisk} on disk`);
    return;
  }
  const s = await pendingMigrations(pool, { sources: kitMigrationSources({ siteMigrationsDir: paths.siteMigrationsDir }) });
  if (s.pending.length) {
    throw new Error(`${s.pending.length} migration(s) pending (${s.pending.map((f) => f.name).join(', ')}): `
      + 'run the deploy migrate stage (deploy.env MIGRATE_COMMAND) as the owner role, or set MIGRATE_ON_BOOT=1 on a fixture database');
  }
}

export async function main(env: NodeJS.ProcessEnv = process.env): Promise<void> {
  const log = (line: string): void => console.log(line);
  const paths = sitePaths(findSiteRoot(__dirname, env));
  const ctx = loadSite(paths, env);
  const llms = loadLlmsContent(paths.llmsJson);
  const clientBundle = loadKitClientBundle(paths.clientBundle);
  const pool = createPool(env, ctx.site.id);
  await prepareSchema(pool, paths, env, log);

  const { app, kit } = createApp({
    ctx,
    kitDb: createPgKitDb(pool),
    articles: new PgArticleStore(pool),
    llms,
    clientBundle,
    startVerifier: env.SITE_HERMETIC !== '1',
    log,
  });
  const port = Number(env.PORT || 8080);
  const server = app.listen(port, () => log(`[site] ${ctx.site.brandName} (${ctx.site.id}) listening on :${port}`));
  let stopping = false;
  const stop = (signal: string): void => {
    if (stopping) return;
    stopping = true;
    log(`[site] ${signal}: draining`);
    kit.stop();
    setTimeout(() => process.exit(0), 10_000).unref();
    server.close(() => {
      void kit.drain().finally(() => pool.end().finally(() => process.exit(0)));
    });
  };
  process.on('SIGTERM', () => stop('SIGTERM'));
  process.on('SIGINT', () => stop('SIGINT'));
}

if (require.main === module) {
  main().catch((e: unknown) => {
    console.error(`[site] failed to start: ${e instanceof Error ? e.stack ?? e.message : String(e)}`);
    process.exit(1);
  });
}
