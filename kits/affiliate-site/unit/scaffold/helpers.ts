import { spawnSync, type SpawnSyncReturns } from 'child_process';
import * as crypto from 'crypto';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';

/**
 * Shared helpers for the scaffold tests (build item K7): where the scripts and
 * the template are, running them, and throwaway directories.
 *
 * Everything runs offline: the generated sites borrow the kit's own
 * node_modules (a symlink; the site pins exactly the kit's dependencies), and
 * nothing here contacts a registry, a cloud or a database. The full smoke
 * (npm ci from the registry, the image build and the kit gate in docker) is
 * smoke.test.ts, behind KIT_SCAFFOLD_SMOKE=1.
 */

export const KIT = path.resolve(__dirname, '..', '..');
export const REPO = path.resolve(KIT, '..', '..');
export const TEMPLATE = path.join(REPO, '_template', 'affiliate-site');
export const SITE_TEMPLATE = path.join(TEMPLATE, 'site');
export const SCAFFOLD = path.join(TEMPLATE, 'scaffold');
export const CREATE = path.join(REPO, 'install', 'create-affiliate-site.sh');
export const SYNC = path.join(REPO, 'install', 'sync-site-kit.sh');
export const RENDER = path.join(SCAFFOLD, 'render_template.py');
export const VENDOR_TOOL = path.join(SCAFFOLD, 'vendor_kit.py');
export const KIT_VERSION = fs.readFileSync(path.join(KIT, 'VERSION'), 'utf8').trim();
export const KIT_NODE_MODULES = path.join(KIT, 'node_modules');

export interface RunResult {
  status: number;
  stdout: string;
  stderr: string;
}

/**
 * The environment a child process gets: this one's, without jest's worker
 * marker (Playwright refuses to start when it sees JEST_WORKER_ID, and a
 * nested jest sets its own), plus `extra`.
 */
export function childEnv(extra: NodeJS.ProcessEnv = {}): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = { ...process.env, FORCE_COLOR: '0', ...extra };
  delete env.JEST_WORKER_ID;
  return env;
}

/** Run a command; never throws on a non-zero exit (the tests assert on it). */
export function run(cmd: string, args: readonly string[], opts: { cwd?: string; env?: NodeJS.ProcessEnv; timeoutMs?: number; input?: string } = {}): RunResult {
  const r: SpawnSyncReturns<string> = spawnSync(cmd, [...args], {
    cwd: opts.cwd,
    env: childEnv(opts.env),
    encoding: 'utf8',
    timeout: opts.timeoutMs ?? 120_000,
    input: opts.input,
    maxBuffer: 64 * 1024 * 1024,
  });
  if (r.error) throw r.error;
  return { status: r.status ?? (r.signal ? 128 : -1), stdout: r.stdout ?? '', stderr: r.stderr ?? '' };
}

export function describeRun(r: RunResult): string {
  return `exit ${r.status}\n--- stdout ---\n${r.stdout.slice(-4000)}\n--- stderr ---\n${r.stderr.slice(-4000)}`;
}

/** A fresh directory under the OS temp dir; removed by cleanupTmp(). */
export function tmpDir(label: string): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), `k7-${label}-`));
}

export function cleanupTmp(dir: string | undefined): void {
  if (dir && dir.startsWith(os.tmpdir() + path.sep)) fs.rmSync(dir, { recursive: true, force: true });
}

/** Options for create-affiliate-site.sh beyond <slug> <domain> --dir. */
export interface CreateOptions {
  slug?: string;
  domain?: string;
  extra?: string[];
  env?: NodeJS.ProcessEnv;
}

/** The content-gate choice the tests pass when they do not test it (the script requires one). */
export const TEST_WAIVER = 'scaffold test site: it publishes no articles';
export const CONTENT_CHOICE: readonly string[] = ['--content-policy-waived', TEST_WAIVER];

/** `args` plus the content-gate choice, unless they already make one. */
export function withContentChoice(args: readonly string[]): string[] {
  return args.some((a) => a === '--content-rule-sets' || a === '--content-policy-waived') ? [...args] : [...args, ...CONTENT_CHOICE];
}

/**
 * create-affiliate-site.sh into <parent>/<slug>, with a fixed IndexNow key so
 * outputs are stable, and the test waiver as the content gate unless `extra`
 * chooses one.
 */
export function createSite(parent: string, o: CreateOptions = {}): { dir: string; result: RunResult } {
  const slug = o.slug ?? 'testpicks';
  const dir = path.join(parent, slug);
  const result = run('bash', [
    CREATE, slug, o.domain ?? `${slug}.example`, '--dir', dir,
    '--indexnow-key', '0f4c2a9e7b1d4e3a8c6b5d2f1e0a9c7b',
    ...withContentChoice(o.extra ?? []),
  ], { env: o.env });
  return { dir, result };
}

/**
 * The gate projects the scaffold names by default: every enabled site in the
 * reusable-agents site registry (built-in defaults) but `except`, read with
 * the framework module itself.
 */
export function registryGateProjects(except: string): string[] {
  const r = run('python3', ['-I', path.join(SCAFFOLD, 'fleet_sites.py'), 'gate-projects', '--repo', REPO, '--except', except]);
  if (r.status !== 0) throw new Error(`reading the site registry failed: ${describeRun(r)}`);
  return r.stdout.trim().split(/\s+/).filter(Boolean);
}

/** A directory holding a fake `docker` that reports the given containers and volumes (for PATH). */
export function fakeDockerBin(parent: string, containers: readonly string[], volumes: readonly string[] = []): string {
  const bin = path.join(parent, 'fake-docker-bin');
  fs.mkdirSync(bin, { recursive: true });
  const script = [
    '#!/usr/bin/env bash',
    'case "$1 $2" in',
    `  "ps -a") printf '%s\\n' ${containers.map((c) => `'${c}'`).join(' ')} ;;`,
    `  "volume ls") printf '%s\\n' ${volumes.map((v) => `'${v}'`).join(' ')} ;;`,
    '  *) exit 1 ;;',
    'esac',
    '',
  ].join('\n');
  fs.writeFileSync(path.join(bin, 'docker'), script, { mode: 0o755 });
  return bin;
}

/** Every regular file under `dir`, relative, sorted; `skip` names directories not to descend into. */
export function filesUnder(dir: string, skip: readonly string[] = ['.git', 'node_modules', 'vendor', 'dist']): string[] {
  const out: string[] = [];
  const walk = (d: string): void => {
    for (const e of fs.readdirSync(d, { withFileTypes: true })) {
      const p = path.join(d, e.name);
      if (e.isDirectory()) {
        if (!skip.includes(e.name)) walk(p);
      } else if (e.isFile()) {
        out.push(path.relative(dir, p).split(path.sep).join('/'));
      }
    }
  };
  walk(dir);
  return out.sort();
}

export function sha256(file: string): string {
  return crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
}

export function readJson<T = Record<string, unknown>>(file: string): T {
  return JSON.parse(fs.readFileSync(file, 'utf8')) as T;
}

/** A path that resolves to a working shellcheck, or null (SHELLCHECK=/path wins over PATH). */
export function shellcheckBin(): string | null {
  const candidates = [process.env.SHELLCHECK, 'shellcheck'].filter((c): c is string => !!c);
  for (const c of candidates) {
    try {
      const r = spawnSync(c, ['--version'], { encoding: 'utf8' });
      if (r.status === 0) return c;
    } catch {
      /* not this one */
    }
  }
  return null;
}

/** Python with PyYAML, for the scope and YAML checks. */
export function pythonHasYaml(): boolean {
  return spawnSync('python3', ['-c', 'import yaml'], { encoding: 'utf8' }).status === 0;
}

/**
 * A copy of the kit's vendored file set in a scratch directory, optionally
 * re-versioned (VERSION, package.json and the KIT_VERSION export together),
 * so the sync's version rules can be tested without touching the real kit.
 */
export function fakeKit(parent: string, version?: string): string {
  const dir = path.join(parent, `kit-${version ?? KIT_VERSION}`);
  const list = run('python3', ['-I', VENDOR_TOOL, 'files', '--kit', KIT]);
  if (list.status !== 0) throw new Error(`listing the kit failed: ${describeRun(list)}`);
  for (const rel of list.stdout.split('\n').filter(Boolean)) {
    const dest = path.join(dir, rel);
    fs.mkdirSync(path.dirname(dest), { recursive: true });
    fs.copyFileSync(path.join(KIT, rel), dest);
    fs.chmodSync(dest, fs.statSync(path.join(KIT, rel)).mode & 0o777);
  }
  for (const f of ['package-lock.json']) fs.copyFileSync(path.join(KIT, f), path.join(dir, f));
  if (version) setKitVersion(dir, version);
  return dir;
}

/** Re-version a kit copy: VERSION, package.json and the KIT_VERSION export together (a release bump). */
export function setKitVersion(kitDir: string, version: string): void {
  fs.writeFileSync(path.join(kitDir, 'VERSION'), `${version}\n`);
  const pkg = readJson<Record<string, unknown>>(path.join(kitDir, 'package.json'));
  pkg.version = version;
  fs.writeFileSync(path.join(kitDir, 'package.json'), `${JSON.stringify(pkg, null, 2)}\n`);
  const index = path.join(kitDir, 'server', 'src', 'index.ts');
  fs.writeFileSync(index, fs.readFileSync(index, 'utf8').replace(/export const KIT_VERSION = '[^']+'/, `export const KIT_VERSION = '${version}'`));
}

/**
 * git in a throwaway repository: none of this process's GIT_* variables (a
 * GIT_DIR would aim it at another repository), no system config, a fixed
 * identity, no signing and no hooks.
 */
export function gitIn(repo: string, ...args: string[]): RunResult {
  const env = childEnv({ GIT_CONFIG_NOSYSTEM: '1' });
  for (const k of Object.keys(env)) if (k.startsWith('GIT_') && k !== 'GIT_CONFIG_NOSYSTEM') delete env[k];
  const r = spawnSync('git', ['-C', repo, '-c', 'user.name=K7 test', '-c', 'user.email=k7-test@example.invalid',
    '-c', 'commit.gpgsign=false', '-c', 'core.hooksPath=/dev/null', ...args], { env, encoding: 'utf8', timeout: 60_000 });
  if (r.error) throw r.error;
  return { status: r.status ?? -1, stdout: r.stdout ?? '', stderr: r.stderr ?? '' };
}

/** Commit everything in a throwaway repository; returns the commit id. */
export function commitAll(repo: string, message: string): string {
  for (const args of [['add', '-A'], ['commit', '-q', '--allow-empty', '-m', message]]) {
    const r = gitIn(repo, ...args);
    if (r.status !== 0) throw new Error(`git ${args[0]} failed: ${describeRun(r)}`);
  }
  return gitIn(repo, 'rev-parse', 'HEAD').stdout.trim();
}

/**
 * Blank (that is, unset: the gate shim reads them with ${VAR:-}, the site's
 * tests with ||) every variable that picks the reusable-agents checkout a
 * site's gate anchors its kit to, or makes the anchor mandatory, so a test
 * sees only the checkout it sets up.
 */
export const ANCHOR_ENV_CLEARED: Readonly<NodeJS.ProcessEnv> = Object.freeze({
  KIT_GATE_FRAMEWORK_DIR: '', REUSABLE_AGENTS_DIR: '', GATE_SOURCE_REPO: '', KIT_ANCHOR_REQUIRED: '',
});

/**
 * A stand-in reusable-agents checkout at <parent>/reusable-agents: this
 * checkout's install/sync-site-kit.sh and scaffold/vendor_kit.py, and `kitFrom`
 * (a kit copy, or a site's vendored copy: its KIT_VERSION and MANIFEST.sha256
 * are left out) as kits/affiliate-site. A git repository on main with that
 * committed, unless `git` or `commit` is false.
 */
export function fakeFramework(parent: string, kitFrom: string, opts: { git?: boolean; commit?: boolean } = {}): string {
  const ra = path.join(parent, 'reusable-agents');
  const kit = path.join(ra, 'kits', 'affiliate-site');
  fs.mkdirSync(path.join(ra, 'install'), { recursive: true });
  fs.mkdirSync(path.join(ra, '_template', 'affiliate-site', 'scaffold'), { recursive: true });
  fs.copyFileSync(SYNC, path.join(ra, 'install', 'sync-site-kit.sh'));
  fs.copyFileSync(VENDOR_TOOL, path.join(ra, '_template', 'affiliate-site', 'scaffold', 'vendor_kit.py'));
  fs.cpSync(kitFrom, kit, {
    recursive: true,
    filter: (src) => !['KIT_VERSION', 'MANIFEST.sha256'].includes(path.relative(kitFrom, src)),
  });
  if (opts.git !== false) {
    const init = gitIn(ra, 'init', '-q', '-b', 'main');
    if (init.status !== 0) throw new Error(`git init failed: ${describeRun(init)}`);
    if (opts.commit !== false) commitAll(ra, 'kits/affiliate-site');
  }
  return ra;
}

/**
 * What an edit that hides itself does: point the vendored MANIFEST.sha256 line
 * for `rel` at the file's current digest (adding the line when `rel` is new).
 */
export function rewriteManifestLine(site: string, rel: string): void {
  const vendor = path.join(site, 'vendor', 'affiliate-kit');
  const file = path.join(vendor, 'MANIFEST.sha256');
  const digest = sha256(path.join(vendor, rel));
  const lines = fs.readFileSync(file, 'utf8').split('\n').filter(Boolean).filter((l) => !l.endsWith(`  ${rel}`));
  lines.push(`${digest}  ${rel}`);
  lines.sort((a, b) => Buffer.compare(Buffer.from(a.slice(66)), Buffer.from(b.slice(66))));
  fs.writeFileSync(file, `${lines.join('\n')}\n`);
}

/** A minimal site directory the sync accepts: site.json and a package.json with the kit's pins. */
export function bareSite(parent: string, name = 'site'): string {
  const dir = path.join(parent, name);
  fs.mkdirSync(dir, { recursive: true });
  fs.copyFileSync(path.join(KIT, 'fixtures', 'site.example.json'), path.join(dir, 'site.json'));
  const kitPkg = readJson<{ dependencies: Record<string, string>; devDependencies: Record<string, string> }>(path.join(KIT, 'package.json'));
  fs.writeFileSync(path.join(dir, 'package.json'), `${JSON.stringify({
    name: name, version: '0.0.1', private: true, dependencies: kitPkg.dependencies, devDependencies: kitPkg.devDependencies,
  }, null, 2)}\n`);
  return dir;
}
