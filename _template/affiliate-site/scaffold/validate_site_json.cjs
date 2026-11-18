#!/usr/bin/env node
/**
 * Validate a site.json with the kit's own loader, exactly as the server does at boot.
 *
 *   node validate_site_json.cjs --kit <kit dir> [--resolve-from <dir>]... <site.json>
 *
 * Bundles <kit>/server/src/siteContext.ts (the schema, the defaults and every
 * cross-field rule: origin host, source rules, WCAG contrast of the theme,
 * the Operating Agreement disclosure, the price window left after caching)
 * with esbuild into a temporary CommonJS file, loads it and calls
 * loadSiteContext(file, { env: {} }). Environment overrides are ignored on
 * purpose: this judges the file.
 *
 * esbuild comes from the kit's node_modules (npm ci in the kit), or from any
 * --resolve-from directory (a site that has run npm ci).
 *
 * Prints a one-line JSON summary of the resolved context on success.
 * Exit: 0 valid; 1 invalid (each issue on stderr); 2 usage; 3 esbuild missing.
 */
'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');

function usage(msg) {
  if (msg) process.stderr.write(`validate_site_json: ${msg}\n`);
  process.stderr.write('usage: node validate_site_json.cjs --kit <kit dir> [--resolve-from <dir>]... <site.json>\n');
  process.exit(2);
}

function parseArgs(argv) {
  const out = { kit: '', from: [], file: '' };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--kit') out.kit = argv[++i] || usage('--kit needs a directory');
    else if (a === '--resolve-from') out.from.push(argv[++i] || usage('--resolve-from needs a directory'));
    else if (a === '-h' || a === '--help') usage();
    else if (a.startsWith('-')) usage(`unknown option ${a}`);
    else if (out.file) usage('one site.json at a time');
    else out.file = a;
  }
  if (!out.kit || !out.file) usage('--kit and a site.json are required');
  return out;
}

function loadEsbuild(dirs) {
  for (const dir of dirs) {
    try {
      return require(require.resolve('esbuild', { paths: [path.resolve(dir)] }));
    } catch {
      /* try the next one */
    }
  }
  return null;
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  const kit = path.resolve(args.kit);
  const entry = path.join(kit, 'server', 'src', 'siteContext.ts');
  if (!fs.existsSync(entry)) usage(`${entry} not found: --kit must be the affiliate-site kit`);
  const esbuild = loadEsbuild([kit, ...args.from]);
  if (!esbuild) {
    process.stderr.write(`validate_site_json: esbuild is not installed; run \`npm ci\` in ${kit}\n`);
    process.exit(3);
  }
  const built = esbuild.buildSync({
    entryPoints: [entry],
    bundle: true,
    platform: 'node',
    format: 'cjs',
    target: 'node22',
    write: false,
    logLevel: 'silent',
    absWorkingDir: kit,
  });
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'affiliate-kit-sitecontext-'));
  const bundle = path.join(dir, 'siteContext.cjs');
  let mod;
  try {
    fs.writeFileSync(bundle, built.outputFiles[0].text, { mode: 0o600 });
    mod = require(bundle);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
  const file = path.resolve(args.file);
  try {
    const ctx = mod.loadSiteContext(file, { env: {} });
    process.stdout.write(`${JSON.stringify({
      file,
      siteId: ctx.site.id,
      brandName: ctx.site.brandName,
      canonicalOrigin: ctx.site.canonicalOrigin,
      partnerTag: ctx.amazon.partnerTag,
      allowedTags: ctx.amazon.allowedTags,
      quotableMaxAgeSeconds: ctx.prices.quotableMaxAgeSeconds,
    })}\n`);
  } catch (e) {
    const issues = e && Array.isArray(e.issues) ? e.issues : [e instanceof Error ? e.message : String(e)];
    process.stderr.write(`validate_site_json: ${file} is not a valid site.json:\n`);
    for (const issue of issues) process.stderr.write(`  - ${issue}\n`);
    process.exit(1);
  }
}

main();
