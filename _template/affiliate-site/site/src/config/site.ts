/**
 * Where this site's files are, and loading them.
 *
 * The site root is the nearest directory at or above this module that holds
 * site.json: the repository root when the code runs from src/ (tests), and
 * the image's /app when it runs from dist/src/ (the built server copies
 * site.json to the root). SITE_ROOT in the environment overrides it.
 *
 * site.json is loaded and checked by the kit (loadSiteContext), which throws
 * a SiteContextError listing every problem: a bad file stops the boot.
 */
import * as fs from 'fs';
import * as path from 'path';
import { loadSiteContext, type SiteContext } from '../../vendor/affiliate-kit/server/src';
import type { LlmsTxtContent } from '../../vendor/affiliate-kit/server/src/http';

export const SITE_JSON = 'site.json';

export interface SitePaths {
  readonly root: string;
  readonly siteJson: string;
  /** llms.txt copy (LlmsTxtContent JSON). */
  readonly llmsJson: string;
  /** The site's own migrations; the kit's run first. */
  readonly siteMigrationsDir: string;
  /** The kit browser bundle `npm run build` writes. */
  readonly clientBundle: string;
}

export function findSiteRoot(start: string = __dirname, env: NodeJS.ProcessEnv = process.env): string {
  if (env.SITE_ROOT) {
    const root = path.resolve(env.SITE_ROOT);
    if (!fs.existsSync(path.join(root, SITE_JSON))) throw new Error(`SITE_ROOT=${root} has no ${SITE_JSON}`);
    return root;
  }
  let at = path.resolve(start);
  for (let depth = 0; depth < 8; depth++) {
    if (fs.existsSync(path.join(at, SITE_JSON))) return at;
    const up = path.dirname(at);
    if (up === at) break;
    at = up;
  }
  throw new Error(`no ${SITE_JSON} at or above ${start}; set SITE_ROOT`);
}

export function sitePaths(root: string = findSiteRoot()): SitePaths {
  return Object.freeze({
    root,
    siteJson: path.join(root, SITE_JSON),
    llmsJson: path.join(root, 'content', 'llms.json'),
    siteMigrationsDir: path.join(root, 'db', 'migrations'),
    clientBundle: path.join(root, 'dist', 'client', 'kit-client.js'),
  });
}

/** The SiteContext. PRICE_FRESH_HOURS in `env` may only shorten the price window. */
export function loadSite(paths: SitePaths, env: NodeJS.ProcessEnv = process.env): SiteContext {
  return loadSiteContext(paths.siteJson, { env });
}

/** The llms.txt copy (summary, curated sections, citation guidance). */
export function loadLlmsContent(file: string): LlmsTxtContent {
  let raw: unknown;
  try {
    raw = JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch (e) {
    throw new Error(`cannot read ${file}: ${e instanceof Error ? e.message : String(e)}`);
  }
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) throw new Error(`${file}: expected a JSON object`);
  return raw as LlmsTxtContent;
}
