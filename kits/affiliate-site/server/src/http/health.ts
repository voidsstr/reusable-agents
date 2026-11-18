/**
 * GET /health: liveness for the container platform and the deploy gate.
 *
 * Ported from SpecPicks src/index.ts: `{status, siteId, timestamp}`, plus the
 * kit version. Deliberately touches no database: the platform restarts a
 * replica whose probe fails, and a database blip must not restart every
 * replica at once (the page handlers already degrade on their own).
 */
import type { RequestHandler } from 'express';
import type { SiteContext } from '../siteContext';

export interface HealthBody {
  readonly status: 'ok';
  readonly siteId: string;
  readonly kitVersion: string;
  readonly timestamp: string;
}

/**
 * KIT_VERSION, read when first needed rather than imported at load time: the
 * kit barrel (server/src/index.ts) is meant to re-export this layer, and a
 * load-time import of the barrel from here would be a require cycle that
 * leaves the barrel without the http exports when http is loaded first.
 */
let cachedVersion: string | null = null;
function kitVersion(): string {
  if (cachedVersion == null) {
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    cachedVersion = (require('../index') as typeof import('../index')).KIT_VERSION;
  }
  return cachedVersion;
}

export function healthBody(ctx: Pick<SiteContext, 'site'>, now: number = Date.now()): HealthBody {
  return { status: 'ok', siteId: ctx.site.id, kitVersion: kitVersion(), timestamp: new Date(now).toISOString() };
}

export function healthHandler(ctx: Pick<SiteContext, 'site'>, now: () => number = Date.now): RequestHandler {
  return (_req, res) => {
    res.set('Cache-Control', 'no-store');
    res.json(healthBody(ctx, now()));
  };
}
