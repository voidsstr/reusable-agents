import { Client } from 'pg';

/**
 * Read-only checks on the fixture database the app under test renders from
 * (KIT_GATE_DB_URL, which run-local-suite.sh builds from suite.env). Only the
 * kit's own tables are read, so every site has them: editorial_articles
 * (kit_0003). Never pointed at production: the runner only knows the fixture.
 */

/** The fixture DSN, or null when the gate runs without one (the DB checks are then annotated as skipped). */
export function gateDbUrl(env: NodeJS.ProcessEnv = process.env): string | null {
  const u = (env.KIT_GATE_DB_URL || '').trim();
  return u || null;
}

export async function withDb<T>(url: string, fn: (c: Client) => Promise<T>): Promise<T> {
  const c = new Client({ connectionString: url, connectionTimeoutMillis: 10_000, statement_timeout: 15_000 });
  await c.connect();
  try {
    await c.query('SET default_transaction_read_only = on');
    return await fn(c);
  } finally {
    await c.end().catch(() => undefined);
  }
}

/** Article heroes stored on an Amazon host (IP License 2(h): never store an Amazon image), any status. */
export const AMAZON_HERO_SQL = `
SELECT slug, status, hero_image_url
  FROM editorial_articles
 WHERE hero_image_url ~* $re$(^|[/.@])(media-amazon\\.com|ssl-images-amazon\\.com|images-amazon\\.com|amazon-adsystem\\.com)([/:?#]|$)$re$
 ORDER BY slug`;

export async function amazonHeroRows(url: string): Promise<Array<{ slug: string; status: string; heroImageUrl: string }>> {
  return withDb(url, async (c) => (await c.query(AMAZON_HERO_SQL)).rows.map((r: Record<string, unknown>) => ({
    slug: String(r.slug),
    status: String(r.status),
    heroImageUrl: String(r.hero_image_url),
  })));
}

/**
 * Article bodies that store a link on an Amazon image CDN, any status (IP
 * License 2(h): an Amazon image link may be used for 24 hours at most, so it
 * is never stored). mdToHtml drops such images when it renders, so only the
 * stored row shows them; the first one found is returned.
 */
export const AMAZON_BODY_IMAGE_SQL = `
SELECT slug, status,
       substring(body_md FROM $re$(?i)(?:https?:)?//[^\\s)"'<>]*(?:media-amazon\\.com|ssl-images-amazon\\.com|images-amazon\\.com|amazon-adsystem\\.com)[^\\s)"'<>]*$re$) AS url
  FROM editorial_articles
 WHERE body_md ~* $re$(?:^|[/.@])(?:media-amazon\\.com|ssl-images-amazon\\.com|images-amazon\\.com|amazon-adsystem\\.com)(?:[/:?#\\s)"'<>]|$)$re$
 ORDER BY slug`;

export async function amazonBodyImageRows(url: string): Promise<Array<{ slug: string; status: string; url: string }>> {
  return withDb(url, async (c) => (await c.query(AMAZON_BODY_IMAGE_SQL)).rows.map((r: Record<string, unknown>) => ({
    slug: String(r.slug),
    status: String(r.status),
    url: r.url == null ? '' : String(r.url),
  })));
}

/**
 * True when the Markdown holds a GFM table the kit's mdToHtml renders: a line
 * with a pipe directly followed by a delimiter row (render/md.ts isTableStart).
 */
export function markdownHasTable(md: string): boolean {
  const lines = String(md ?? '').split(/\r?\n/);
  const delim = /^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*$/;
  const fence = /^\s*(```|~~~)/;
  let inFence = false;
  for (let i = 0; i + 1 < lines.length; i++) {
    const head = lines[i] as string;
    if (fence.test(head)) { inFence = !inFence; continue; }
    if (inFence) continue;
    const next = lines[i + 1] as string;
    if (!head.trim() || !/(^|[^\\])\|/.test(head) || /^\s*(#|>)/.test(head)) continue;
    if (delim.test(next) && next.includes('|')) return true;
  }
  return false;
}

/** Slugs of published articles whose Markdown has a GFM table (their page must render one). */
export async function publishedArticlesWithTables(url: string): Promise<string[]> {
  return withDb(url, async (c) => {
    const r = await c.query("SELECT slug, body_md FROM editorial_articles WHERE status = 'published' ORDER BY slug");
    return r.rows.filter((row: Record<string, unknown>) => markdownHasTable(String(row.body_md ?? ''))).map((row: Record<string, unknown>) => String(row.slug));
  });
}

/** Slugs of every published article (the content-policy scan reads all of them). */
export async function publishedArticleSlugs(url: string): Promise<string[]> {
  return withDb(url, async (c) => (await c.query("SELECT slug FROM editorial_articles WHERE status = 'published' ORDER BY slug")).rows.map((r: Record<string, unknown>) => String(r.slug)));
}
