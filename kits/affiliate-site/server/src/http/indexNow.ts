/**
 * The IndexNow key file: GET /<key>.txt answers with the key itself.
 *
 * IndexNow (Bing, Yandex, Seznam, Naver) verifies a submission by fetching
 * https://<host>/<key>.txt and comparing the body with the key it was sent.
 * SpecPicks served it as a static file; the kit serves it from
 * SiteContext.indexNow.key (schema: 8-128 of [A-Za-z0-9-]), so the key lives
 * in one place: the framework's indexnow-submitter reads the same value from
 * the site config.
 */
import type { RequestHandler } from 'express';
import type { SiteContext } from '../siteContext';

/** '/<key>.txt' */
export function indexNowKeyPath(ctx: Pick<SiteContext, 'indexNow'>): string {
  return `/${ctx.indexNow.key}.txt`;
}

/** The absolute key-file URL (IndexNow's keyLocation). */
export function indexNowKeyLocation(ctx: Pick<SiteContext, 'indexNow' | 'site'>): string {
  return `${ctx.site.canonicalOrigin}${indexNowKeyPath(ctx)}`;
}

/** GET handler for indexNowKeyPath(ctx): the bare key, text/plain. */
export function indexNowKeyHandler(ctx: Pick<SiteContext, 'indexNow'>): RequestHandler {
  const key = ctx.indexNow.key;
  return (_req, res) => {
    res.set('Content-Type', 'text/plain; charset=utf-8');
    res.set('Cache-Control', 'public, max-age=86400');
    res.set('X-Robots-Tag', 'noindex');
    res.send(key);
  };
}
