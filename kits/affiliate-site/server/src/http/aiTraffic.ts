/**
 * AI-traffic logging middleware.
 *
 * Ported from SpecPicks src/index.ts ("AI-traffic Logging Middleware"; see
 * unit/http/fixtures/ported-from-http.json). It captures the acquisition
 * channel GA4 misses: AI crawlers and answer-time fetchers (matched by
 * User-Agent) and people arriving from an AI chat (matched by Referer host,
 * or by utm_source when the assistant strips the Referer). Only AI-relevant
 * page hits are written, to ai_traffic_log, after the response finishes:
 * fire-and-forget, never blocking or failing the page.
 *
 * Each row says whether its User-Agent can be believed: ip_address is the
 * address that connected (rt.clientIp: K1's siteClientIp, which skips one
 * hop of the CDN site.json `edge` names), ip_verified / ip_check are that
 * address checked against the vendor's published ranges and the kit's static
 * ranges (AiCrawlerVerifier), false for our own fleet host
 * (AI_TRAFFIC_EXCLUDE_IPS), null when unknowable. duration_ms is the server
 * time for the page, so a slow page an assistant gave up on shows up next to
 * its status code.
 *
 * Kit changes:
 *   - The site id comes from SiteContext; the client address skips one
 *     trusted CDN hop when site.json `edge` names one (siteClientIp).
 *   - Rows are submitted sheddable: under database contention they are
 *     dropped before click rows (BackgroundWriter, maxPendingAiWrites).
 *   - Skip prefixes match whole path segments ('/api' skips '/api/x', not
 *     '/apiary') and are configurable.
 *   - A fetch the client abandons before the response finishes (an assistant
 *     timing out on a cold or slow page) is logged with status 499 instead of
 *     vanishing: upstream logged only on 'finish', so exactly the failures
 *     worth seeing left no row. Readers that count landings already require
 *     status 200 (framework/core/ai_traffic.py), so these rows add evidence
 *     without changing their numbers.
 */
import type { RequestHandler } from 'express';
import { classifyAiTraffic } from '../aiCrawlerVerify';
import type { KitRuntime } from './runtime';
import { clipText, urlPath } from './runtime';
import type { AiTrafficRow } from './types';

export interface AiTrafficOptions {
  /** Path prefixes never logged, matched on whole segments. Default AI_TRAFFIC_SKIP_PREFIXES. */
  skipPrefixes?: readonly string[];
  /** Exact paths never logged. Default ['/health']. */
  skipPaths?: readonly string[];
  /** Log fetches the client abandoned before the response finished, as status 499 (default true). */
  logAborted?: boolean;
}

/** Upstream's API/asset prefixes plus the kit's own asset prefix. */
export const AI_TRAFFIC_SKIP_PREFIXES: readonly string[] = Object.freeze(['/api', '/assets', '/images', '/static', '/kit']);
export const AI_TRAFFIC_SKIP_PATHS: readonly string[] = Object.freeze(['/health']);
/** Static files are never a page an assistant read (upstream's list, plus avif). */
export const AI_TRAFFIC_STATIC_EXT_RE = /\.(js|css|png|jpe?g|gif|svg|webp|avif|ico|woff2?|map)$/i;
/** nginx's "client closed request": the status logged for an abandoned fetch. */
export const AI_TRAFFIC_ABORTED_STATUS = 499;

/** Upstream column clips. */
const MAX_PATH = 300;
const MAX_UA = 500;
const MAX_REFERER = 500;
const MAX_IP = 100;
const MAX_FORWARDED_FOR = 300;

/** True when a request for `path` is a page hit worth classifying. */
export function shouldLogAiTraffic(path: string, opts: AiTrafficOptions = {}): boolean {
  const skipPaths = opts.skipPaths ?? AI_TRAFFIC_SKIP_PATHS;
  if (skipPaths.includes(path)) return false;
  for (const raw of opts.skipPrefixes ?? AI_TRAFFIC_SKIP_PREFIXES) {
    const pfx = raw.endsWith('/') ? raw.slice(0, -1) : raw;
    if (path === pfx || path.startsWith(`${pfx}/`)) return false;
  }
  return !AI_TRAFFIC_STATIC_EXT_RE.test(path);
}

/**
 * The middleware. Mount it before every route (mountKit does), so it sees
 * the final status of whatever handler serves the page.
 */
export function aiTrafficMiddleware(rt: KitRuntime, opts: AiTrafficOptions = rt.deps.aiTraffic ?? {}): RequestHandler {
  const logAborted = opts.logAborted ?? true;
  return (req, res, next) => {
    let path = '';
    try {
      path = urlPath(req.originalUrl || req.url);
      if (shouldLogAiTraffic(path, opts)) {
        const ua = typeof req.headers['user-agent'] === 'string' ? req.headers['user-agent'] : undefined;
        const referer = typeof req.headers.referer === 'string' ? req.headers.referer : undefined;
        const utm = typeof req.query?.utm_source === 'string' ? req.query.utm_source : undefined;
        const hit = classifyAiTraffic(ua, referer, utm);
        if (hit) {
          const started = rt.now();
          const xff = typeof req.headers['x-forwarded-for'] === 'string' ? req.headers['x-forwarded-for'] : undefined;
          const ip = rt.clientIp(req);
          const verdict = rt.verifier.verify(hit.kind, hit.source, ip);
          const base = {
            kind: hit.kind,
            source: hit.source,
            path: clipText(path, MAX_PATH) ?? '',
            userAgent: clipText(ua, MAX_UA),
            referer: clipText(referer, MAX_REFERER),
            ipAddress: clipText(ip, MAX_IP),
            siteId: rt.ctx.site.id,
            ipVerified: verdict.verified,
            ipCheck: verdict.check,
            forwardedFor: xff && xff.trim() !== ip ? clipText(xff, MAX_FORWARDED_FOR) : null,
          };
          let logged = false;
          // Runs inside a response event listener: a throw here would be an
          // uncaught exception, so nothing may escape.
          const write = (statusCode: number): void => {
            if (logged) return;
            logged = true;
            try {
              const row: AiTrafficRow = { ...base, statusCode, durationMs: Math.max(0, rt.now() - started) };
              rt.writer.submit('ai_traffic_log', () => rt.db.insertAiTraffic(row), { sheddable: true });
            } catch (e) {
              rt.log.error(`[affiliate-kit] ai-traffic log failed for ${path}: ${e instanceof Error ? e.message : String(e)}`);
            }
          };
          res.on('finish', () => write(res.statusCode));
          res.on('close', () => {
            if (!res.writableFinished && logAborted) write(AI_TRAFFIC_ABORTED_STATUS);
          });
        }
      }
    } catch (e) {
      // Classification must never cost the page.
      rt.log.error(`[affiliate-kit] ai-traffic classification failed for ${path}: ${e instanceof Error ? e.message : String(e)}`);
    }
    next();
  };
}
