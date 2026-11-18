/**
 * The resolved dependencies every kit HTTP handler shares.
 *
 * A site passes KitDeps (its database, optionally a verifier, a redirect
 * lookup, llms.txt content); createKitRuntime() fills in the defaults once,
 * so the handlers agree on the clock, the client-address rule and the write
 * ceiling. mountKit() builds one for you; build one yourself only to mount
 * individual handlers.
 *
 * The client address of every logged row (ai_traffic_log, outbound_clicks)
 * is K1's siteClientIp(): the right-most public X-Forwarded-For entry, and,
 * when site.json `edge` names a proxying CDN (edge.cdn, plus
 * edge.extraTrustedProxies), exactly one hop inside its ranges skipped. That
 * site.json block is the only switch: there is no environment variable or
 * mount option for it, so a site cannot end up with two disagreeing answers.
 */
import type { IncomingHttpHeaders } from 'http';
import { createAiCrawlerVerifier, siteClientIp, type AiCrawlerVerifier } from '../aiCrawlerVerify';
import type { SiteContext } from '../siteContext';
import { BackgroundWriter } from './backgroundWriter';
import type { CrawlerVerifierLike, KitDb, KitLogger, RedirectLookup } from './types';
import type { AiTrafficOptions } from './aiTraffic';
import type { LlmsTxtContent } from './llms';
import type { RobotsTxtOptions } from './robots';
import type { KitClientBundle } from './clientAsset';

export interface KitDeps {
  /** Where click and AI-traffic rows go, and what llms.txt reads (createPgKitDb(pool) in production). */
  db: KitDb;
  /**
   * AI-crawler IP verification. Default: createAiCrawlerVerifier(ctx), whose
   * vendor range lists mountKit() starts loading (and refreshes every 12h)
   * unless startVerifier is false.
   */
  verifier?: CrawlerVerifierLike;
  /** Start the default verifier's range-list refresh at mount (default true). Ignored when `verifier` is given. */
  startVerifier?: boolean;
  /** Catalog lookup for /r/:asin (pgRedirectLookup(pool, sql)). Without it /r/ is not mounted. */
  redirectLookup?: RedirectLookup;
  /** llms.txt content; without it the file carries only what SiteContext knows. */
  llms?: LlmsTxtContent;
  robots?: RobotsTxtOptions;
  aiTraffic?: AiTrafficOptions;
  /** The built browser bundle (loadKitClientBundle); without it the asset route is not mounted. */
  clientBundle?: KitClientBundle;
  /** Fire-and-forget writes allowed in flight before rows are dropped (default 64). */
  maxPendingWrites?: number;
  /**
   * How many of those an AI-traffic row may hold (default 3/4 of
   * maxPendingWrites). AI-log rows are shed first, so a crawler burst during
   * a slow database never crowds out click rows, the verified-human series.
   */
  maxPendingAiWrites?: number;
  log?: KitLogger;
  /** Clock, for tests. */
  now?: () => number;
  /** Environment for the default verifier (AI_TRAFFIC_EXCLUDE_IPS); default process.env. */
  env?: NodeJS.ProcessEnv;
}

export interface KitRuntime {
  readonly ctx: SiteContext;
  readonly deps: Readonly<KitDeps>;
  readonly db: KitDb;
  readonly verifier: CrawlerVerifierLike;
  /** Set when the runtime built the default verifier (mountKit starts and stops it). */
  readonly ownedVerifier: AiCrawlerVerifier | null;
  /** Click writes use the full ceiling; AI-traffic writes are submitted sheddable (maxPendingAiWrites). */
  readonly writer: BackgroundWriter;
  readonly log: KitLogger;
  readonly now: () => number;
  /** The client address of a request: siteClientIp(ctx, X-Forwarded-For, socket peer). */
  clientIp(req: KitRequestLike): string;
}

/** What clientIp() reads from a request (an express Request fits). */
export interface KitRequestLike {
  headers: IncomingHttpHeaders;
  socket?: { remoteAddress?: string | undefined } | null;
}

/* eslint-disable no-console */
export const CONSOLE_LOGGER: KitLogger = Object.freeze({
  info: (m: string) => console.log(m),
  error: (m: string) => console.error(m),
});
/* eslint-enable no-console */

/**
 * A logger that never throws: handlers log from catch blocks and response
 * event listeners, where a throwing log sink would turn a logged problem
 * into a failed page or an uncaught exception.
 */
export function safeLogger(log: KitLogger): KitLogger {
  const guard = (fn: (m: string) => void) => (m: string): void => {
    try {
      fn.call(log, m);
    } catch {
      /* the log sink is down; nothing else to do */
    }
  };
  return Object.freeze({ info: guard(log.info), error: guard(log.error) });
}

export function createKitRuntime(ctx: SiteContext, deps: KitDeps): KitRuntime {
  if (!deps || typeof deps.db?.insertOutboundClick !== 'function' || typeof deps.db?.insertAiTraffic !== 'function') {
    throw new TypeError('affiliate-kit: deps.db must implement insertOutboundClick and insertAiTraffic');
  }
  const env = deps.env ?? process.env;
  const log = safeLogger(deps.log ?? CONSOLE_LOGGER);
  const now = deps.now ?? Date.now;
  const ownedVerifier = deps.verifier ? null : createAiCrawlerVerifier(ctx, { env });
  const verifier: CrawlerVerifierLike = deps.verifier ?? (ownedVerifier as AiCrawlerVerifier);
  const writer = new BackgroundWriter({
    maxPending: deps.maxPendingWrites,
    maxSheddable: deps.maxPendingAiWrites,
    logError: (m) => log.error(m),
    now,
  });
  return Object.freeze({
    ctx,
    deps: Object.freeze({ ...deps }),
    db: deps.db,
    verifier,
    ownedVerifier,
    writer,
    log,
    now,
    clientIp: (req: KitRequestLike) => siteClientIp(ctx, req.headers['x-forwarded-for'], req.socket?.remoteAddress),
  });
}

// ─── small shared helpers ──────────────────────────────────────────────────

/** C0 controls and DEL: Postgres text cannot hold NUL, and none of them belong in a log column. */
const CONTROL_CHARS = /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g;

/**
 * A string clipped to `max` characters (code points, as VARCHAR(n) counts
 * them) with control characters removed; null for a non-string or ''.
 */
export function clipText(v: unknown, max: number): string | null {
  if (typeof v !== 'string') return null;
  const clean = v.replace(CONTROL_CHARS, '');
  if (!clean) return null;
  const cps = Array.from(clean);
  return cps.length > max ? cps.slice(0, max).join('') : clean;
}

/** The first value of a request header, or null. */
export function headerValue(headers: IncomingHttpHeaders, name: string): string | null {
  const v = headers[name.toLowerCase()];
  const s = Array.isArray(v) ? v[0] : v;
  return typeof s === 'string' ? s : null;
}

/** The path of a request URL ('/a/b' from '/a/b?x=1'), without decoding. */
export function urlPath(url: string | undefined): string {
  const u = String(url || '/');
  const q = u.search(/[?#]/);
  return q === -1 ? u : u.slice(0, q);
}
