import type { APIRequestContext } from '@playwright/test';
import type { PageFamily, Source, SourceContext } from './manifestTypes';
import { BASE_URL, SEED, apiJson, seededSample, sitemapPaths, sitePath } from './site';

/**
 * Sample sources a site's gate config builds its families from. Samples come
 * from the LOCAL server only (its sitemaps, its own pages' links, its API),
 * never from production. Ported from SpecPicks tests/ui/pages.manifest.ts.
 */

/** Fixed paths. */
export function fixed(...paths: string[]): Source {
  return async () => paths;
}

/** Every <loc> of the given local sitemap files, optionally filtered. */
export function fromSitemaps(files: string[], re?: RegExp): Source {
  return async ({ request }) => {
    const out: string[] = [];
    for (const f of files) out.push(...(await sitemapPaths(request, f)));
    return out.filter((p) => !re || re.test(p));
  };
}

/** Links the local server's own page carries (what a reader can click), matching `re`. */
export function linksOn(page: string, re: RegExp): Source {
  return async ({ request }) => {
    const resp = await request.get(`${BASE_URL}${page}`, { timeout: 60_000 });
    if (resp.status() !== 200) return [];
    const html = await resp.text();
    const out = new Set<string>();
    for (const m of html.matchAll(/href="([^"]+)"/g)) {
      const p = sitePath((m[1] as string).replace(/&amp;/g, '&'));
      if (p && re.test(p)) out.add(p.split('#')[0] as string);
    }
    return [...out];
  };
}

/** Links matching `re` on any of the local pages. */
export function linksOnAny(pages: string[], re: RegExp): Source {
  return async (ctx) => {
    const out = new Set<string>();
    for (const page of pages) for (const l of await linksOn(page, re)(ctx)) out.add(l);
    return [...out];
  };
}

/** Paths picked out of a local JSON endpoint. */
export function fromApi(path: string, pick: (json: unknown) => string[]): Source {
  return async ({ request }) => pick(await apiJson(request, path));
}

/** The union of several sources (deduplicated, in order). */
export function union(...sources: Source[]): Source {
  return async (ctx) => {
    const out = new Set<string>();
    for (const s of sources) for (const p of await s(ctx)) out.add(p);
    return [...out];
  };
}

const sampleCache = new Map<string, Promise<string[]>>();

/** A family's seeded sample, once per worker. */
export function samplesFor(fam: PageFamily, request: APIRequestContext): Promise<string[]> {
  if (!sampleCache.has(fam.id)) {
    sampleCache.set(fam.id, (async () => seededSample(await fam.source({ request }), fam.sample, fam.id))());
  }
  return sampleCache.get(fam.id)!;
}

/** A source's output, once per worker (keyed by the caller). */
const sourceCache = new Map<string, Promise<string[]>>();
export function cachedSource(key: string, source: Source, ctx: SourceContext): Promise<string[]> {
  if (!sourceCache.has(key)) sourceCache.set(key, source(ctx));
  return sourceCache.get(key)!;
}

/** The seed in use, for annotations. */
export function seedLabel(): string {
  return `seed ${SEED}`;
}
