/**
 * The fixture site's express app: the kit's routes first (mountKit), then the
 * page routes from routes.ts, then the 404. createFixtureApp takes its
 * database and clock as arguments, so it can be exercised without a server.
 */
import * as fs from 'fs';
import * as path from 'path';
import express, { type Express, type Request, type Response } from 'express';
import type { Pool } from 'pg';
import { htmlCacheControl, isAmazonAsin, type SiteContext } from '../../server/src';
import {
  createPgKitDb,
  mountKit,
  pgRedirectLookup,
  type KitClientBundle,
  type LlmsTxtContent,
  type MountedKit,
} from '../../server/src/http';
import { sitemapFileResponse, sitemapIndexResponse, sitemapUnavailableResponse, type SitemapHttpResponse } from '../../server/src/render';
import { FixtureDb, REDIRECT_LOOKUP_SQL } from './db';
import { FixtureRenderer, fixtureSitemapSet } from './render';
import { ROUTES } from './routes';

export interface FixtureAppDeps {
  ctx: SiteContext;
  pool: Pool;
  llms: LlmsTxtContent;
  clientBundle: KitClientBundle | null;
  /** Directory of the static images (/assets/*). */
  assetsDir: string;
  now?: () => number;
  log?: (line: string) => void;
}

export interface FixtureApp {
  app: Express;
  kit: MountedKit;
}

const ASSET_TYPES: Readonly<Record<string, string>> = Object.freeze({ '.svg': 'image/svg+xml', '.png': 'image/png' });

export function createFixtureApp(deps: FixtureAppDeps): FixtureApp {
  const { ctx, pool } = deps;
  const now = deps.now ?? Date.now;
  const log = deps.log ?? ((line: string) => console.log(line));
  const db = new FixtureDb(pool);
  const render = new FixtureRenderer(ctx, deps.clientBundle, now);
  const app = express();
  app.disable('x-powered-by');
  app.set('trust proxy', false);

  const kit = mountKit(app, ctx, {
    db: createPgKitDb(pool),
    redirectLookup: pgRedirectLookup(pool, REDIRECT_LOOKUP_SQL),
    llms: deps.llms,
    clientBundle: deps.clientBundle ?? undefined,
    // Hermetic: the gate fixture never fetches the vendors' crawler range lists.
    startVerifier: false,
    now,
  });

  const html = (res: Response, status: number, body: string): void => {
    res.status(status);
    res.set('Content-Type', 'text/html; charset=utf-8');
    res.set('Cache-Control', status === 200 ? htmlCacheControl(ctx) : 'no-store');
    res.send(body);
  };

  /** Wrap a page handler: a thrown error renders the 500 page, never a stack trace. */
  const page = (fn: (req: Request, res: Response) => Promise<void>) => (req: Request, res: Response): void => {
    fn(req, res).catch((e: unknown) => {
      log(`[fixture-site] ${req.method} ${req.path} failed: ${e instanceof Error ? e.stack ?? e.message : String(e)}`);
      if (!res.headersSent) html(res, 500, render.serverError(req.path));
    });
  };
  const notFound = (req: Request, res: Response): void => html(res, 404, render.notFound(req.path));

  const sendSitemap = (res: Response, r: SitemapHttpResponse): void => {
    res.status(r.status);
    for (const [k, v] of Object.entries(r.headers)) res.set(k, v);
    res.send(r.body);
  };
  const sitemapSet = async () => fixtureSitemapSet(ctx, {
    products: await db.products(),
    articles: await db.articles(),
    bestLists: await db.bestLists(),
    notes: await db.notes(),
    recalls: (await db.recalls()).filter((r) => r.status !== 'closed'),
  });

  const handlers: Record<string, (req: Request, res: Response) => void> = {
    '/': page(async (_req, res) => html(res, 200, render.home(await db.products(), await db.articles()))),
    '/product/:asin': page(async (req, res) => {
      const asin = String(req.params.asin);
      if (!isAmazonAsin(asin)) return notFound(req, res);
      const p = await db.product(asin);
      if (!p || !p.isActive) return notFound(req, res);
      const others = await db.productsByAsin(p.alternativeAsin ? [p.alternativeAsin] : []);
      const recall = p.recallId ? (await db.recalls()).find((r) => r.id === p.recallId) ?? null : null;
      html(res, 200, render.product(p, others, recall));
    }),
    '/guides': page(async (_req, res) => html(res, 200, render.guidesIndex(await db.articles()))),
    '/guides/:slug': page(async (req, res) => {
      const a = await db.article(String(req.params.slug));
      if (!a) return notFound(req, res);
      html(res, 200, render.guide(a, await db.productsByAsin(a.relatedProductAsins)));
    }),
    '/best/:slug': page(async (req, res) => {
      const b = await db.bestList(String(req.params.slug));
      if (!b) return notFound(req, res);
      html(res, 200, render.bestList(b, await db.productsByAsin(b.asins)));
    }),
    '/recalls': page(async (_req, res) => html(res, 200, render.recallsIndex((await db.recalls()).filter((r) => r.status !== 'closed')))),
    '/recalls/:id': page(async (req, res) => {
      const r = (await db.recalls()).find((x) => x.id === String(req.params.id));
      if (!r) return notFound(req, res);
      html(res, 200, render.recall(r));
    }),
    '/notes/:slug': page(async (req, res) => {
      const n = await db.note(String(req.params.slug));
      if (!n) return notFound(req, res);
      html(res, 200, render.note(n));
    }),
    '/about': page(async (_req, res) => html(res, 200, render.about())),
    '/disclosure': page(async (_req, res) => html(res, 200, render.disclosure())),
    '/sitemap.xml': page(async (_req, res) => {
      try {
        sendSitemap(res, sitemapIndexResponse(await sitemapSet()));
      } catch (e) {
        sendSitemap(res, sitemapUnavailableResponse(e instanceof Error ? e.message : String(e), 'sitemapindex'));
      }
    }),
    '/:file.xml': page(async (req, res) => {
      try {
        sendSitemap(res, sitemapFileResponse(await sitemapSet(), `/${String(req.params.file)}.xml`));
      } catch (e) {
        sendSitemap(res, sitemapUnavailableResponse(e instanceof Error ? e.message : String(e)));
      }
    }),
    '/assets/:file': (req, res) => {
      const name = String(req.params.file);
      const type = ASSET_TYPES[path.extname(name)];
      const file = path.join(deps.assetsDir, name);
      if (!type || !/^[a-z0-9-]+\.[a-z]+$/.test(name) || !fs.existsSync(file)) return notFound(req, res);
      res.set('Content-Type', type);
      res.set('Cache-Control', 'public, max-age=86400');
      res.send(fs.readFileSync(file));
    },
  };

  // Mount every page, sitemap and asset route the table declares; the kit's own are mounted already.
  for (const r of ROUTES) {
    if (r.kind === 'kit') continue;
    const h = handlers[r.pattern];
    if (!h) throw new Error(`fixture-site: route ${r.pattern} has no handler`);
    app.get(r.pattern, h);
  }
  app.use(notFound);
  return { app, kit };
}
