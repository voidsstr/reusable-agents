import { spawnSync } from 'child_process';
import * as crypto from 'crypto';
import * as fs from 'fs';
import * as path from 'path';
import { KIT_VERSION as KIT_VERSION_EXPORT } from '../../vendor/affiliate-kit/server/src';

/**
 * vendor/affiliate-kit is the affiliate-site kit, vendored READ-ONLY by
 * reusable-agents install/sync-site-kit.sh, which writes KIT_VERSION and
 * MANIFEST.sha256 (sha256 of every vendored file). This suite fails on an
 * edit, addition or removal under vendor/affiliate-kit that the manifest
 * does not record, and on a vendored file replaced by a symbolic link. The
 * manifest lives in this repository, so an edit that rewrites it passes
 * that; when the reusable-agents checkout is found, the anchored check below
 * also requires the copy to be byte-identical to the kit version it claims
 * there (install/sync-site-kit.sh --verify-anchored), which a rewritten
 * manifest does not pass. The deploy gate runs both checks before every run
 * (tests/run-local-suite.sh).
 *
 * Neither stops a session that also rewrites this test or the gate shim, or
 * that claims a kit version no commit holds yet (the anchored check only
 * warns then). On the implementer's claude path no path scope applies, so
 * there the kit is protected only by instructions (CLAUDE.md, "The kit
 * boundary").
 *
 * A red here is never fixed by editing this test, the manifest or the
 * vendored file: change the kit in reusable-agents (kits/affiliate-site),
 * release it with a version bump, commit it, and re-sync:
 *   bash <reusable-agents>/install/sync-site-kit.sh <this repo>
 */

const ROOT = path.resolve(__dirname, '..', '..');
const VENDOR = path.join(ROOT, 'vendor', 'affiliate-kit');
const MANIFEST = 'MANIFEST.sha256';
const FIX = 'vendor/affiliate-kit is read-only: re-sync it with reusable-agents install/sync-site-kit.sh (kit changes go to reusable-agents kits/affiliate-site)';

/**
 * The reusable-agents checkout the deploy gate anchors the kit to, resolved as
 * tests/ui/gate.config.ts and tests/run-local-suite.sh do: KIT_GATE_FRAMEWORK_DIR,
 * else REUSABLE_AGENTS_DIR, else the checkout next to GATE_SOURCE_REPO or this repo.
 */
const FRAMEWORK_DIR = process.env.KIT_GATE_FRAMEWORK_DIR
  || process.env.REUSABLE_AGENTS_DIR
  || path.resolve(process.env.GATE_SOURCE_REPO || ROOT, '..', 'reusable-agents');

interface Entry { sha256: string; path: string }

function readManifest(): Entry[] {
  const text = fs.readFileSync(path.join(VENDOR, MANIFEST), 'utf8');
  return text.split('\n').filter((l) => l.length > 0).map((line, i) => {
    const m = /^([0-9a-f]{64}) {2}(\S(?:.*\S)?)$/.exec(line);
    if (!m) throw new Error(`${MANIFEST} line ${i + 1} is not "<sha256>  <path>": ${JSON.stringify(line)}`);
    return { sha256: m[1] as string, path: m[2] as string };
  });
}

function filesUnder(dir: string, base = dir, out: string[] = []): string[] {
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, e.name);
    const rel = path.relative(base, p).split(path.sep).join('/');
    if (e.isSymbolicLink()) out.push(`${rel} (symbolic link)`);
    else if (e.isDirectory()) filesUnder(p, base, out);
    else out.push(rel);
  }
  return out;
}

function sha256(file: string): string {
  return crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
}

function readJson<T>(file: string): T {
  return JSON.parse(fs.readFileSync(file, 'utf8')) as T;
}

/** Fail with the problems and how to fix them (jest's expect takes no message). */
function noProblems(problems: string[], how: string): void {
  if (problems.length) throw new Error(`${how}\n  ${problems.join('\n  ')}`);
}

interface PackageJson { version?: string; dependencies?: Record<string, string>; devDependencies?: Record<string, string> }

describe('vendor/affiliate-kit matches its MANIFEST.sha256', () => {
  const entries = readManifest();

  it('lists the kit, sorted by path, once each', () => {
    expect(entries.length).toBeGreaterThan(50);
    const paths = entries.map((e) => e.path);
    expect(new Set(paths).size).toBe(paths.length);
    expect(paths).toEqual([...paths].sort((a, b) => Buffer.compare(Buffer.from(a), Buffer.from(b))));
    expect(paths).toEqual(expect.arrayContaining(['KIT_VERSION', 'VERSION', 'server/src/index.ts', 'tests/run-local-suite.sh']));
  });

  it('every vendored file is unchanged', () => {
    const problems: string[] = [];
    for (const e of entries) {
      const file = path.join(VENDOR, e.path);
      let st: fs.Stats;
      try {
        st = fs.lstatSync(file);
      } catch {
        problems.push(`missing: ${e.path}`);
        continue;
      }
      // A link to an identical file is still an edit: the image builds from wherever it points.
      if (!st.isFile()) problems.push(`modified: ${e.path} (${st.isSymbolicLink() ? 'symbolic link' : 'not a regular file'})`);
      else if (sha256(file) !== e.sha256) problems.push(`modified: ${e.path}`);
    }
    noProblems(problems, FIX);
  });

  it('nothing was added to the vendored kit', () => {
    const listed = new Set([...entries.map((e) => e.path), MANIFEST]);
    const extra = filesUnder(VENDOR).filter((f) => !listed.has(f));
    noProblems(extra.map((f) => `not in the manifest: ${f}`), FIX);
  });
});

describe('vendor/affiliate-kit is the kit release it claims (reusable-agents)', () => {
  it('is byte-identical to that kit version in the reusable-agents checkout', () => {
    const tool = path.join(FRAMEWORK_DIR, 'install', 'sync-site-kit.sh');
    const required = process.env.KIT_ANCHOR_REQUIRED === '1';
    let unanchored: string;
    if (!fs.existsSync(tool)) {
      unanchored = `no reusable-agents checkout at ${FRAMEWORK_DIR} (set KIT_GATE_FRAMEWORK_DIR to one)`;
    } else {
      const r = spawnSync('bash', [tool, ROOT, '--verify-anchored', '--quiet'], { encoding: 'utf8', timeout: 120_000 });
      const said = `${r.stdout ?? ''}${r.stderr ?? ''}`.trim();
      if (r.status === 0) return;
      if (r.status === 1) noProblems([said], FIX);
      unanchored = `${tool} --verify-anchored exited ${r.status ?? r.signal}: ${said}`;
    }
    if (required) throw new Error(`KIT_ANCHOR_REQUIRED=1 and the vendored kit could not be anchored: ${unanchored}`);
    // eslint-disable-next-line no-console
    console.warn(`vendor/affiliate-kit was NOT anchored to reusable-agents (checked against its own manifest only): ${unanchored}`);
  }, 150_000);
});

describe('the vendored kit version', () => {
  it('KIT_VERSION, VERSION, package.json and the KIT_VERSION export agree', () => {
    const kitVersion = fs.readFileSync(path.join(VENDOR, 'KIT_VERSION'), 'utf8').trim();
    expect(kitVersion).toMatch(/^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$/);
    expect(fs.readFileSync(path.join(VENDOR, 'VERSION'), 'utf8').trim()).toBe(kitVersion);
    expect(readJson<PackageJson>(path.join(VENDOR, 'package.json')).version).toBe(kitVersion);
    expect(KIT_VERSION_EXPORT).toBe(kitVersion);
  });
});

describe('the site pins the kit\'s dependencies exactly', () => {
  const kit = readJson<PackageJson>(path.join(VENDOR, 'package.json'));
  const site = readJson<PackageJson>(path.join(ROOT, 'package.json'));

  it('runtime dependencies (the server imports them in the image)', () => {
    const drift = Object.entries(kit.dependencies ?? {})
      .filter(([name, pin]) => site.dependencies?.[name] !== pin)
      .map(([name, pin]) => `${name}: kit ${pin}, site ${site.dependencies?.[name] ?? 'missing'}`);
    noProblems(drift, 'align package.json with the vendored kit (npm install <pkg>@<pin> --save-exact):');
  });

  it('development dependencies (the build, the unit tests and the gate)', () => {
    const drift = Object.entries(kit.devDependencies ?? {})
      .filter(([name, pin]) => (site.devDependencies?.[name] ?? site.dependencies?.[name]) !== pin)
      .map(([name, pin]) => `${name}: kit ${pin}, site ${site.devDependencies?.[name] ?? site.dependencies?.[name] ?? 'missing'}`);
    noProblems(drift, 'align package.json with the vendored kit (npm install -D <pkg>@<pin> --save-exact):');
  });
});
