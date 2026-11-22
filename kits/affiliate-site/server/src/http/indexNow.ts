/**
 * IndexNow: the key file, and the submit path.
 *
 * GET /<key>.txt answers with the key itself. IndexNow (Bing, Yandex, Seznam,
 * Naver) verifies a submission by fetching https://<host>/<key>.txt and
 * comparing the body with the key it was sent. SpecPicks served it as a static
 * file; the kit serves it from SiteContext.indexNow.key (schema: 8-128 of
 * [A-Za-z0-9-]), so the key lives in one place: the framework's
 * indexnow-submitter reads the same value from the site config. The key file
 * is served whatever site.json `indexing` says (it submits nothing by itself).
 *
 * The submit path, indexNowSubmission() (the JSON body of one POST) and
 * submitIndexNow() (the POSTs, in batches), refuses before it builds or sends
 * anything while site.json `indexing` is "closed" (IndexingClosedError): a
 * site that is not open to search engines asks none of them to crawl it. Feed
 * it indexableUrls() (render/sitemap.ts), the sitemaps' own set, which
 * refuses the same way.
 */
import type { RequestHandler } from 'express';
import { assertIndexingOpen } from '../indexing';
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

// ─── the submit path ───────────────────────────────────────────────────────

/** The shared IndexNow endpoint (it forwards to every participating engine). */
export const INDEXNOW_ENDPOINT = 'https://api.indexnow.org/indexnow';
/** The protocol's limit of URLs in one POST. */
export const INDEXNOW_MAX_URLS = 10_000;
/** How long one POST may take before it is abandoned. */
export const INDEXNOW_TIMEOUT_MS = 30_000;

type IndexNowCtx = Pick<SiteContext, 'site' | 'indexNow' | 'indexing'>;

/** The JSON body of one IndexNow POST. */
export interface IndexNowSubmission {
  readonly host: string;
  readonly key: string;
  readonly keyLocation: string;
  readonly urlList: readonly string[];
}

/**
 * `urls`, de-duplicated in order, each an absolute URL on the canonical
 * origin (IndexNow refuses a batch with another host's URL: 422). Throws a
 * TypeError naming the first few that are not.
 */
function siteUrls(ctx: IndexNowCtx, urls: readonly string[]): string[] {
  const origin = ctx.site.canonicalOrigin;
  const out: string[] = [];
  const seen = new Set<string>();
  const bad: string[] = [];
  for (const raw of urls) {
    let u: URL | null = null;
    try { u = new URL(String(raw)); } catch { u = null; }
    if (!u || u.origin !== origin || u.hash) {
      bad.push(String(raw));
      continue;
    }
    const s = u.toString();
    if (!seen.has(s)) {
      seen.add(s);
      out.push(s);
    }
  }
  if (bad.length) throw new TypeError(`IndexNow: ${bad.length} URL(s) not on ${origin}: ${bad.slice(0, 3).join(', ')}`);
  return out;
}

/**
 * The body of one IndexNow POST for `urls` (absolute, on the canonical origin,
 * at most INDEXNOW_MAX_URLS once de-duplicated). Refuses (IndexingClosedError)
 * while site.json `indexing` is "closed".
 */
export function indexNowSubmission(ctx: IndexNowCtx, urls: readonly string[]): IndexNowSubmission {
  assertIndexingOpen(ctx, 'IndexNow submission');
  const list = siteUrls(ctx, urls);
  if (list.length > INDEXNOW_MAX_URLS) throw new RangeError(`IndexNow: ${list.length} URLs in one submission (at most ${INDEXNOW_MAX_URLS}; submitIndexNow batches)`);
  return Object.freeze({
    host: new URL(ctx.site.canonicalOrigin).host,
    key: ctx.indexNow.key,
    keyLocation: indexNowKeyLocation(ctx),
    urlList: Object.freeze(list),
  });
}

/** The fetch submitIndexNow uses (the global fetch fits). */
export type IndexNowFetch = (
  url: string,
  init: { method: 'POST'; headers: Record<string, string>; body: string; signal: AbortSignal },
) => Promise<{ status: number }>;

export interface SubmitIndexNowOptions {
  /** Default: the global fetch. */
  fetch?: IndexNowFetch;
  /** Default INDEXNOW_ENDPOINT. */
  endpoint?: string;
  /** Per POST; default INDEXNOW_TIMEOUT_MS. */
  timeoutMs?: number;
  /** Default `<site id>-indexnow/1.0 (+<canonical origin>/)`. */
  userAgent?: string;
}

export interface IndexNowBatchResult {
  /** URLs in the batch. */
  readonly urls: number;
  /** The HTTP status (0 when the request failed). */
  readonly status: number;
  /** 200 (submitted) or 202 (accepted, key check pending). */
  readonly ok: boolean;
  readonly error: string | null;
}

/**
 * POST `urls` to IndexNow in batches of INDEXNOW_MAX_URLS. Refuses
 * (IndexingClosedError, nothing sent) while site.json `indexing` is "closed",
 * and refuses URLs that are not on the canonical origin (TypeError, nothing
 * sent). One result per batch; a failed batch does not stop the next.
 */
export async function submitIndexNow(ctx: IndexNowCtx, urls: readonly string[], opts: SubmitIndexNowOptions = {}): Promise<IndexNowBatchResult[]> {
  assertIndexingOpen(ctx, 'IndexNow submission');
  const list = siteUrls(ctx, urls);
  const send = opts.fetch ?? (globalThis.fetch as unknown as IndexNowFetch | undefined);
  if (!list.length) return [];
  if (typeof send !== 'function') throw new TypeError('IndexNow: no fetch available (pass opts.fetch)');
  const endpoint = opts.endpoint ?? INDEXNOW_ENDPOINT;
  const timeoutMs = opts.timeoutMs ?? INDEXNOW_TIMEOUT_MS;
  const userAgent = opts.userAgent ?? `${ctx.site.id}-indexnow/1.0 (+${ctx.site.canonicalOrigin}/)`;
  const results: IndexNowBatchResult[] = [];
  for (let i = 0; i < list.length; i += INDEXNOW_MAX_URLS) {
    const batch = list.slice(i, i + INDEXNOW_MAX_URLS);
    const body = JSON.stringify(indexNowSubmission(ctx, batch));
    const abort = new AbortController();
    const timer = setTimeout(() => abort.abort(), timeoutMs);
    try {
      const r = await send(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json; charset=utf-8', 'User-Agent': userAgent },
        body,
        signal: abort.signal,
      });
      results.push(Object.freeze({ urls: batch.length, status: r.status, ok: r.status === 200 || r.status === 202, error: null }));
    } catch (e) {
      results.push(Object.freeze({ urls: batch.length, status: 0, ok: false, error: e instanceof Error ? e.message : String(e) }));
    } finally {
      clearTimeout(timer);
    }
  }
  return results;
}
