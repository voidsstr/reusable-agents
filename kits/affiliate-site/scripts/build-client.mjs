#!/usr/bin/env node
/**
 * Build the kit's browser bundle with esbuild:
 *
 *   client/src/index.ts  ->  client/dist/kit-client.js        (one minified IIFE, ES2018)
 *                            client/dist/kit-client.meta.json (bytes, sha256, SRI hash)
 *
 * The bundle is the click beacon and the sticky-bar sizing (client/README.md).
 * It is a build artifact (gitignored): a site's image build runs this script
 * from its vendored kit, and the server serves the file under a content hash
 * (server/src/http/clientAsset.ts). The output is deterministic: the same
 * sources and esbuild version give the same bytes.
 *
 *   node scripts/build-client.mjs                 write client/dist/
 *   node scripts/build-client.mjs --outdir <dir>  write elsewhere (tests)
 *   node scripts/build-client.mjs --check         build in memory only; report size
 *
 * Exit codes: 0 built, 1 build failed or over the size budget, 2 bad arguments.
 */
import { build, version as esbuildVersion } from 'esbuild';
import { createHash } from 'node:crypto';
import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const KIT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const ENTRY = join(KIT, 'client', 'src', 'index.ts');
const DEFAULT_OUTDIR = join(KIT, 'client', 'dist');
const BUNDLE_NAME = 'kit-client.js';
const META_NAME = 'kit-client.meta.json';
const TARGET = ['es2018'];
/** The bundle rides on every page: keep it small. */
const MAX_BUNDLE_BYTES = 8 * 1024;

function usage(msg) {
  if (msg) process.stderr.write(`build-client: ${msg}\n`);
  process.stderr.write('usage: node scripts/build-client.mjs [--outdir <dir>] [--check]\n');
  process.exit(2);
}

function parseArgs(argv) {
  const out = { outdir: DEFAULT_OUTDIR, check: false };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--check') out.check = true;
    else if (a === '--outdir') {
      const v = argv[++i];
      if (!v) usage('--outdir needs a directory');
      out.outdir = resolve(process.cwd(), v);
    } else if (a === '--help' || a === '-h') usage();
    else usage(`unknown argument ${a}`);
  }
  return out;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  let result;
  try {
    result = await build({
      entryPoints: [ENTRY],
      outfile: join(args.outdir, BUNDLE_NAME),
      bundle: true,
      format: 'iife',
      platform: 'browser',
      target: TARGET,
      minify: true,
      legalComments: 'none',
      charset: 'utf8',
      sourcemap: false,
      write: false,
      logLevel: 'silent',
      absWorkingDir: KIT,
      tsconfig: join(KIT, 'tsconfig.json'),
    });
  } catch (e) {
    const errors = e && Array.isArray(e.errors) ? e.errors : [];
    const lines = errors.length
      ? errors.map((m) => `  ${m.location ? `${m.location.file}:${m.location.line}: ` : ''}${m.text}`)
      : [`  ${e instanceof Error ? e.message : String(e)}`];
    process.stderr.write(`build-client: esbuild failed\n${lines.join('\n')}\n`);
    process.exit(1);
  }

  const out = result.outputFiles.find((f) => f.path.endsWith(BUNDLE_NAME));
  if (!out) {
    process.stderr.write('build-client: esbuild produced no bundle\n');
    process.exit(1);
  }
  const js = out.text;
  const bytes = Buffer.byteLength(js, 'utf8');
  const meta = {
    file: BUNDLE_NAME,
    entry: relative(KIT, ENTRY).split('\\').join('/'),
    bytes,
    sha256: createHash('sha256').update(js, 'utf8').digest('hex'),
    integrity: `sha384-${createHash('sha384').update(js, 'utf8').digest('base64')}`,
    format: 'iife',
    target: TARGET,
    esbuild: esbuildVersion,
  };
  if (bytes > MAX_BUNDLE_BYTES) {
    process.stderr.write(`build-client: bundle is ${bytes} bytes, over the ${MAX_BUNDLE_BYTES}-byte budget\n`);
    process.exit(1);
  }
  if (!args.check) {
    mkdirSync(args.outdir, { recursive: true });
    writeFileSync(join(args.outdir, BUNDLE_NAME), js);
    writeFileSync(join(args.outdir, META_NAME), `${JSON.stringify(meta, null, 2)}\n`);
  }
  process.stdout.write(`build-client: ${args.check ? 'checked' : `wrote ${join(args.outdir, BUNDLE_NAME)}`} (${bytes} bytes, sha256 ${meta.sha256.slice(0, 12)})\n`);
}

main().catch((e) => {
  process.stderr.write(`build-client: ${e instanceof Error ? e.stack || e.message : String(e)}\n`);
  process.exit(1);
});
