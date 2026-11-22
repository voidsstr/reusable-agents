/**
 * site.json `"indexing": "closed"`: X-Robots-Tag: noindex on EVERY response.
 *
 * mountKit installs closedIndexingMiddleware() ahead of every other handler
 * when the site is closed, so the header reaches pages, sitemaps (XML),
 * robots.txt and llms.txt (text), /health and the beacon (JSON), redirects
 * (/r/, the host redirect, trailing-slash fixes), 404s and error pages alike.
 *
 * Setting the header once at the start is not enough on its own: a later
 * handler may overwrite it (res.set('X-Robots-Tag', ...)), remove it, or pass
 * its own headers straight to res.writeHead(), which Node lets win over
 * setHeader(). So the middleware also wraps this response's writeHead(), the
 * one door every response goes through (res.end() and res.write() call it via
 * _implicitHeader), and makes sure that the headers which actually go out
 * still say noindex for every crawler. A value scoped to one crawler
 * ("googlebot: noindex") does not count: a plain "noindex" line is added.
 *
 * Nothing else changes: robots.txt keeps allowing crawls (a Disallow would
 * hide the noindex: see ../indexing.ts), pages keep their own robots meta.
 */
import type { RequestHandler } from 'express';
import type { OutgoingHttpHeader, OutgoingHttpHeaders, ServerResponse } from 'http';

export const ROBOTS_TAG_HEADER = 'X-Robots-Tag';
/** The value a closed site adds. */
export const CLOSED_INDEXING_ROBOTS_TAG = 'noindex';

/**
 * Robots directives that may lead an X-Robots-Tag value. Any other leading
 * "<token>:" is a user agent the rest of the value is scoped to
 * ("googlebot: noindex" binds Googlebot only).
 */
const ROBOTS_DIRECTIVES: ReadonlySet<string> = new Set([
  'all', 'none', 'index', 'noindex', 'follow', 'nofollow', 'noarchive', 'nocache', 'nosnippet', 'notranslate',
  'noimageindex', 'indexifembedded', 'unavailable_after', 'max-snippet', 'max-image-preview', 'max-video-preview',
  'noodp', 'noydir',
]);

/** True when one X-Robots-Tag value applies noindex (or none) to every crawler. */
export function robotsTagNoindexForAll(value: string): boolean {
  const v = String(value ?? '').trim().toLowerCase();
  const lead = /^([a-z0-9._-]+)\s*:/.exec(v);
  if (lead && !ROBOTS_DIRECTIVES.has(lead[1] as string)) return false;
  return v.split(',').some((part) => {
    const directive = (part.split(':')[0] ?? '').trim();
    return directive === 'noindex' || directive === 'none';
  });
}

function asValues(v: number | string | readonly string[] | undefined | null): string[] {
  if (v === undefined || v === null) return [];
  return Array.isArray(v) ? v.map(String) : [String(v)];
}

/** Adds a plain "noindex" X-Robots-Tag line to `res` unless a value already says noindex for every crawler. */
export function ensureNoindexHeader(res: Pick<ServerResponse, 'getHeader' | 'setHeader'>): void {
  const values = asValues(res.getHeader(ROBOTS_TAG_HEADER) as number | string | string[] | undefined);
  if (values.some(robotsTagNoindexForAll)) return;
  res.setHeader(ROBOTS_TAG_HEADER, values.length ? [...values, CLOSED_INDEXING_ROBOTS_TAG] : CLOSED_INDEXING_ROBOTS_TAG);
}

/**
 * Headers passed to writeHead() replace what setHeader() set under the same
 * name. When they carry an X-Robots-Tag that does not say noindex for every
 * crawler, return a copy that adds one; otherwise return them as given.
 */
function withNoindex(headers: OutgoingHttpHeaders | OutgoingHttpHeader[]): OutgoingHttpHeaders | OutgoingHttpHeader[] {
  const isTag = (k: unknown): boolean => String(k).toLowerCase() === 'x-robots-tag';
  if (Array.isArray(headers)) {
    // Node's flat form: [name, value, name, value, ...].
    const values: string[] = [];
    for (let i = 0; i + 1 < headers.length; i += 2) if (isTag(headers[i])) values.push(...asValues(headers[i + 1] as string | string[]));
    if (!values.length || values.some(robotsTagNoindexForAll)) return headers;
    return [...headers, ROBOTS_TAG_HEADER, CLOSED_INDEXING_ROBOTS_TAG];
  }
  const keys = Object.keys(headers).filter(isTag);
  if (!keys.length) return headers;
  const values = keys.flatMap((k) => asValues(headers[k] as string | string[] | number | undefined));
  if (values.some(robotsTagNoindexForAll)) return headers;
  const copy: OutgoingHttpHeaders = { ...headers };
  for (const k of keys) delete copy[k];
  copy[ROBOTS_TAG_HEADER] = [...values, CLOSED_INDEXING_ROBOTS_TAG];
  return copy;
}

type WriteHead = (statusCode: number, ...rest: unknown[]) => ServerResponse;

/**
 * Middleware for a closed site: X-Robots-Tag: noindex now (later handlers can
 * read it), and again in writeHead() if a handler took it away.
 */
export function closedIndexingMiddleware(): RequestHandler {
  return (_req, res, next) => {
    ensureNoindexHeader(res);
    const original = res.writeHead as unknown as WriteHead;
    const patched = function writeHead(this: ServerResponse, statusCode: number, reason?: unknown, headers?: unknown): ServerResponse {
      ensureNoindexHeader(this);
      const fix = (h: unknown): unknown => (h && typeof h === 'object' ? withNoindex(h as OutgoingHttpHeaders | OutgoingHttpHeader[]) : h);
      if (typeof reason === 'string') return headers === undefined ? original.call(this, statusCode, reason) : original.call(this, statusCode, reason, fix(headers));
      if (reason !== undefined && reason !== null) return original.call(this, statusCode, fix(reason));
      return headers === undefined ? original.call(this, statusCode) : original.call(this, statusCode, reason, fix(headers));
    };
    res.writeHead = patched as unknown as typeof res.writeHead;
    next();
  };
}
