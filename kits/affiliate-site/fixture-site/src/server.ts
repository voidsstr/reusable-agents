/**
 * The fixture site's process: load site.json (plus an optional overlay), apply
 * the kit's migrations and the fixture's own, then serve.
 *
 * Environment (the compose test service sets these; none is a secret):
 *   PORT                   listen port (default 8080)
 *   DATABASE_URL           the fixture Postgres
 *   FIXTURE_SITE_JSON      site.json file name inside the fixture dir (default site.json)
 *   FIXTURE_SITE_OVERLAY   optional JSON file laid over it (the violations scenario's robots policy)
 *   FIXTURE_LLMS_JSON      llms.txt content file (default llms.json)
 *   KIT_CLIENT_FILE        the built kit browser bundle (default: the image's build output)
 *   KIT_MIGRATIONS_DIR     the kit's migrations (default: found above this file)
 *   FIXTURE_DIR            the fixture's data files (default: the directory above this file's)
 */
import * as fs from 'fs';
import * as path from 'path';
import { Pool } from 'pg';
import { parseSiteContext, type SiteContext } from '../../server/src';
import { runKitMigrations } from '../../server/src/db';
import { loadKitClientBundle, type KitClientBundle, type LlmsTxtContent } from '../../server/src/http';
import { createFixtureApp } from './app';

/**
 * The fixture directory (site.json, llms.json, db/, public/). Compiled, this
 * file sits at <out>/fixture-site/src/server.js; the image lays the data files
 * next to it, and a local build points FIXTURE_DIR at the source tree.
 */
export const FIXTURE_DIR = process.env.FIXTURE_DIR ? path.resolve(process.env.FIXTURE_DIR) : path.resolve(__dirname, '..');

type Json = Record<string, unknown>;

function readJson(file: string): Json {
  const v: unknown = JSON.parse(fs.readFileSync(file, 'utf8'));
  if (!v || typeof v !== 'object' || Array.isArray(v)) throw new Error(`${file}: expected a JSON object`);
  return v as Json;
}

/** Plain-object deep merge: `over` wins, arrays are replaced, not concatenated. */
export function mergeJson(base: Json, over: Json): Json {
  const out: Json = { ...base };
  for (const [k, v] of Object.entries(over)) {
    const b = out[k];
    out[k] = b && v && typeof b === 'object' && typeof v === 'object' && !Array.isArray(b) && !Array.isArray(v)
      ? mergeJson(b as Json, v as Json)
      : v;
  }
  return out;
}

/** A file name inside the fixture dir; anything with a path separator is refused. */
function fixtureFile(name: string | undefined, dflt: string): string {
  const n = (name || dflt).trim();
  if (!/^[A-Za-z0-9._-]+$/.test(n)) throw new Error(`fixture-site: ${JSON.stringify(n)} is not a file name in ${FIXTURE_DIR}`);
  return path.join(FIXTURE_DIR, n);
}

export function loadFixtureContext(env: NodeJS.ProcessEnv): SiteContext {
  const siteFile = fixtureFile(env.FIXTURE_SITE_JSON, 'site.json');
  let raw = readJson(siteFile);
  if (env.FIXTURE_SITE_OVERLAY) raw = mergeJson(raw, readJson(fixtureFile(env.FIXTURE_SITE_OVERLAY, '')));
  return parseSiteContext(raw, { env, loadedFrom: siteFile });
}

function loadBundle(env: NodeJS.ProcessEnv): KitClientBundle | null {
  const candidates = [
    env.KIT_CLIENT_FILE,
    path.join(FIXTURE_DIR, 'dist', 'client', 'kit-client.js'),
    path.resolve(FIXTURE_DIR, '..', 'client', 'dist', 'kit-client.js'),
  ].filter((f): f is string => !!f);
  for (const f of candidates) if (fs.existsSync(f)) return loadKitClientBundle(f);
  if (env.FIXTURE_ALLOW_NO_CLIENT === '1') return null;
  throw new Error(`fixture-site: kit client bundle not found (tried ${candidates.join(', ')}); build it with scripts/build-client.mjs`);
}

async function main(): Promise<void> {
  const env = process.env;
  const ctx = loadFixtureContext(env);
  const llms = readJson(fixtureFile(env.FIXTURE_LLMS_JSON, 'llms.json')) as LlmsTxtContent;
  const databaseUrl = env.DATABASE_URL;
  if (!databaseUrl) throw new Error('fixture-site: DATABASE_URL is not set');
  const pool = new Pool({ connectionString: databaseUrl, max: 8, statement_timeout: 15_000, connectionTimeoutMillis: 10_000 });
  const summary = await runKitMigrations(pool, { siteMigrationsDir: path.join(FIXTURE_DIR, 'db', 'migrations') });
  console.log(`[fixture-site] migrations: ${JSON.stringify(summary)}`);

  const { app, kit } = createFixtureApp({
    ctx,
    pool,
    llms,
    clientBundle: loadBundle(env),
    assetsDir: path.join(FIXTURE_DIR, 'public'),
  });
  const port = Number(env.PORT || 8080);
  const server = app.listen(port, () => console.log(`[fixture-site] ${ctx.site.brandName} listening on :${port}`));
  const stop = (): void => {
    kit.stop();
    server.close(() => {
      void kit.drain().finally(() => pool.end().finally(() => process.exit(0)));
    });
    setTimeout(() => process.exit(0), 5000).unref();
  };
  process.on('SIGTERM', stop);
  process.on('SIGINT', stop);
}

if (require.main === module) {
  main().catch((e: unknown) => {
    console.error(`[fixture-site] failed to start: ${e instanceof Error ? e.stack ?? e.message : String(e)}`);
    process.exit(1);
  });
}
