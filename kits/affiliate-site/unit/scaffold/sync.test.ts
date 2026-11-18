import { spawnSync } from 'child_process';
import * as fs from 'fs';
import * as path from 'path';
import {
  KIT, KIT_VERSION, SYNC, VENDOR_TOOL, bareSite, cleanupTmp, commitAll, describeRun, fakeFramework, fakeKit, filesUnder, gitIn, readJson,
  rewriteManifestLine, run, setKitVersion, sha256, tmpDir,
} from './helpers';

/**
 * install/sync-site-kit.sh (and scaffold/vendor_kit.py behind it): vendoring
 * the kit into <site>/vendor/affiliate-kit with KIT_VERSION and
 * MANIFEST.sha256, and checking a vendored copy.
 */

const MANIFEST_LINE = /^([0-9a-f]{64}) {2}(\S(?:.*\S)?)$/;

function sync(site: string, ...args: string[]) {
  return run('bash', [SYNC, site, ...args]);
}

function manifest(site: string): Map<string, string> {
  const text = fs.readFileSync(path.join(site, 'vendor', 'affiliate-kit', 'MANIFEST.sha256'), 'utf8');
  const out = new Map<string, string>();
  for (const line of text.split('\n').filter(Boolean)) {
    const m = MANIFEST_LINE.exec(line);
    if (!m) throw new Error(`bad manifest line ${JSON.stringify(line)}`);
    out.set(m[2] as string, m[1] as string);
  }
  return out;
}

describe('sync-site-kit.sh vendors the kit', () => {
  let tmp = '';
  let site = '';
  beforeAll(() => {
    tmp = tmpDir('sync');
    site = bareSite(tmp);
    const r = sync(site, '--quiet');
    if (r.status !== 0) throw new Error(describeRun(r));
  });
  afterAll(() => cleanupTmp(tmp));

  const vendor = (): string => path.join(site, 'vendor', 'affiliate-kit');

  it('writes KIT_VERSION (one line) and a sha256sum-format manifest over every vendored file', () => {
    expect(fs.readFileSync(path.join(vendor(), 'KIT_VERSION'), 'utf8')).toBe(`${KIT_VERSION}\n`);
    const m = manifest(site);
    const onDisk = filesUnder(vendor(), []).filter((f) => f !== 'MANIFEST.sha256');
    expect([...m.keys()]).toEqual(onDisk.slice().sort((a, b) => Buffer.compare(Buffer.from(a), Buffer.from(b))));
    for (const [rel, digest] of m) expect(sha256(path.join(vendor(), rel))).toBe(digest);
    expect(m.has('KIT_VERSION')).toBe(true);
    expect(m.has('MANIFEST.sha256')).toBe(false);
  });

  it('vendors what a site runs and nothing that only the kit repo needs', () => {
    const files = filesUnder(vendor(), []);
    for (const f of ['VERSION', 'package.json', 'tsconfig.json', 'server/src/index.ts', 'server/src/http/mountKit.ts', 'server/src/render/layout.ts',
      'server/src/db/migrate.ts', 'client/src/index.ts', 'scripts/build-client.mjs', 'migrations/kit_0001_outbound_clicks.sql',
      'migrations/templates/publish_trigger.sql.tmpl', 'schema/site.schema.json', 'tests/run-local-suite.sh', 'tests/playwright.base.ts',
      'tests/ui/kit-compliance.spec.ts', 'deploy/Dockerfile.azure.tmpl', 'deploy/lib/deploytool.py', 'deploy/azure/deploy.sh']) {
      expect(files).toContain(f);
    }
    const leaked = files.filter((f) => /(^|\/)(node_modules|dist|coverage|test-results|__pycache__)\//.test(f)
      || /^(unit|fixture-site|tests\/unit|tests\/selftest)\//.test(f)
      || ['.gitignore', 'jest.config.js', 'tsconfig.jest.json', 'test.sh', 'package-lock.json', 'tests/jest.config.js'].includes(f));
    expect(leaked).toEqual([]);
  });

  it('keeps executable bits as 0755 and everything else 0644', () => {
    const mode = (rel: string): number => fs.statSync(path.join(vendor(), rel)).mode & 0o777;
    expect(mode('tests/run-local-suite.sh')).toBe(fs.statSync(path.join(KIT, 'tests/run-local-suite.sh')).mode & 0o111 ? 0o755 : 0o644);
    expect(mode('server/src/index.ts')).toBe(0o644);
    expect(mode('MANIFEST.sha256')).toBe(0o644);
  });

  it('the manifest checks with sha256sum -c when the host has it', () => {
    const probe = spawnSync('sha256sum', ['--version'], { encoding: 'utf8' });
    if (probe.status !== 0) return;
    const r = run('sha256sum', ['--quiet', '-c', 'MANIFEST.sha256'], { cwd: vendor() });
    expect(describeRun(r)).toMatch(/^exit 0/);
  });

  it('--verify and --check pass on a fresh sync', () => {
    expect(sync(site, '--verify').status).toBe(0);
    const c = sync(site, '--check');
    expect(describeRun(c)).toMatch(/^exit 0/);
    expect(c.stdout).toContain('in sync');
  });

  it('a second sync changes nothing', () => {
    const before = fs.readFileSync(path.join(vendor(), 'MANIFEST.sha256'), 'utf8');
    const r = sync(site, '--json', '--quiet');
    expect(r.status).toBe(0);
    const summary = JSON.parse(r.stdout.trim().split('\n').pop() as string);
    expect(summary).toMatchObject({ version: KIT_VERSION, previous: KIT_VERSION, added: [], removed: [], changed: [] });
    expect(fs.readFileSync(path.join(vendor(), 'MANIFEST.sha256'), 'utf8')).toBe(before);
    // No staging directories left behind.
    expect(fs.readdirSync(path.join(site, 'vendor'))).toEqual(['affiliate-kit']);
  });
});

describe('sync-site-kit.sh catches and repairs edits to the vendored copy', () => {
  let tmp = '';
  let site = '';
  beforeEach(() => {
    tmp = tmpDir('sync-edit');
    site = bareSite(tmp);
    expect(sync(site, '--quiet').status).toBe(0);
  });
  afterEach(() => cleanupTmp(tmp));

  it('--verify and --check fail on a modified, an added and a removed file, naming each', () => {
    const v = path.join(site, 'vendor', 'affiliate-kit');
    fs.appendFileSync(path.join(v, 'server', 'src', 'links.ts'), '\n// local hotfix\n');
    fs.writeFileSync(path.join(v, 'server', 'src', 'extra.ts'), 'export const x = 1;\n');
    fs.rmSync(path.join(v, 'server', 'src', 'price.ts'));
    for (const mode of ['--verify', '--check']) {
      const r = sync(site, mode);
      expect(r.status).toBe(1);
      expect(r.stderr).toContain('modified: server/src/links.ts');
      expect(r.stderr).toContain('not in the manifest: server/src/extra.ts');
      expect(r.stderr).toContain('missing: server/src/price.ts');
    }
    // A sync replaces the edited copy (and says so).
    const r = sync(site);
    expect(r.status).toBe(0);
    expect(r.stderr).toContain('local edits are discarded');
    expect(sync(site, '--verify').status).toBe(0);
    expect(fs.existsSync(path.join(v, 'server', 'src', 'extra.ts'))).toBe(false);
    expect(fs.readFileSync(path.join(v, 'server', 'src', 'links.ts'), 'utf8')).toBe(fs.readFileSync(path.join(KIT, 'server', 'src', 'links.ts'), 'utf8'));
  });

  it('--verify and --check treat a vendored file replaced by a symbolic link as an edit, even to identical bytes', () => {
    const v = path.join(site, 'vendor', 'affiliate-kit');
    const file = path.join(v, 'server', 'src', 'offer.ts');
    const copy = path.join(tmp, 'offer-copy.ts');
    fs.copyFileSync(file, copy);
    fs.rmSync(file);
    fs.symlinkSync(copy, file);
    fs.symlinkSync(tmp, path.join(v, 'linked-dir'));
    for (const mode of ['--verify', '--check']) {
      const r = sync(site, mode);
      expect(r.status).toBe(1);
      expect(r.stderr).toContain('modified: server/src/offer.ts (symbolic link)');
      expect(r.stderr).toContain('not in the manifest: linked-dir (symbolic link)');
    }
    // A sync replaces the link with the kit's file.
    expect(sync(site, '--quiet').status).toBe(0);
    expect(fs.lstatSync(file).isFile()).toBe(true);
    expect(fs.existsSync(path.join(v, 'linked-dir'))).toBe(false);
    expect(sync(site, '--verify').status).toBe(0);
  });

  it('--dry-run reports what would change and writes nothing', () => {
    const v = path.join(site, 'vendor', 'affiliate-kit');
    fs.rmSync(path.join(v, 'server', 'src', 'price.ts'));
    const before = filesUnder(v, []);
    const r = sync(site, '--dry-run', '--json');
    expect(r.status).toBe(0);
    expect(r.stdout).toContain('dry run: would vendor kit');
    expect(JSON.parse(r.stdout.trim().split('\n').pop() as string).dry_run).toBe(true);
    expect(filesUnder(v, [])).toEqual(before);
  });

  it('--check reports dependency pins the site does not follow, and a Dockerfile rendered from another kit', () => {
    const pkgPath = path.join(site, 'package.json');
    const pkg = readJson<{ dependencies: Record<string, string> }>(pkgPath);
    pkg.dependencies.express = '4.0.0';
    fs.writeFileSync(pkgPath, JSON.stringify(pkg, null, 2));
    fs.writeFileSync(path.join(site, 'Dockerfile.azure'), '# rendered-from: affiliate-kit/deploy/Dockerfile.azure.tmpl (kit 0.0.9)\nFROM x\n');
    const r = sync(site, '--check');
    expect(r.status).toBe(1);
    expect(r.stderr).toContain('dependencies.express: kit pins');
    expect(r.stderr).toContain(`Dockerfile.azure was rendered from another kit version (kit 0.0.9, vendored kit ${KIT_VERSION})`);
    // A sync warns about both but does not touch them.
    const s = sync(site, '--quiet');
    expect(s.status).toBe(0);
    expect(s.stderr).toContain('package.json: dependencies.express');
    expect(readJson<{ dependencies: Record<string, string> }>(pkgPath).dependencies.express).toBe('4.0.0');
  });
});

describe('sync-site-kit.sh version rules', () => {
  let tmp = '';
  beforeAll(() => { tmp = tmpDir('sync-versions'); });
  afterAll(() => cleanupTmp(tmp));

  it('upgrades to a newer kit, refuses a downgrade unless asked, and --check says out of date', () => {
    const site = bareSite(tmp, 'versions');
    const newer = fakeKit(tmp, '0.2.0');
    expect(sync(site, '--quiet').status).toBe(0);
    let c = sync(site, '--check', '--kit', newer);
    expect(c.status).toBe(1);
    expect(c.stderr).toContain(`out of date: vendored ${KIT_VERSION}, kit 0.2.0`);
    const up = sync(site, '--kit', newer);
    expect(describeRun(up)).toMatch(/^exit 0/);
    expect(up.stdout).toContain(`vendored kit 0.2.0 (was ${KIT_VERSION})`);
    expect(fs.readFileSync(path.join(site, 'vendor', 'affiliate-kit', 'KIT_VERSION'), 'utf8')).toBe('0.2.0\n');
    const down = sync(site, '--quiet');
    expect(down.status).toBe(2);
    expect(down.stderr).toContain('a downgrade');
    expect(sync(site, '--quiet', '--allow-downgrade').status).toBe(0);
    c = sync(site, '--check');
    expect(c.status).toBe(0);
  });

  it('refuses a half-bumped kit (VERSION, package.json and KIT_VERSION export disagree)', () => {
    const site = bareSite(tmp, 'halfbumped');
    const kit = fakeKit(tmp, '0.3.0');
    fs.writeFileSync(path.join(kit, 'VERSION'), '0.3.1\n');
    const r = sync(site, '--kit', kit);
    expect(r.status).toBe(2);
    expect(r.stderr).toContain('half-bumped');
    expect(fs.existsSync(path.join(site, 'vendor'))).toBe(false);
  });

  it('refuses a vendored copy as its source, and a directory that is not a site', () => {
    const site = bareSite(tmp, 'source');
    expect(sync(site, '--quiet').status).toBe(0);
    const other = bareSite(tmp, 'other');
    const r = sync(other, '--kit', path.join(site, 'vendor', 'affiliate-kit'));
    expect(r.status).not.toBe(0);
    expect(r.stderr).toContain('only a sync writes');
    const notSite = path.join(tmp, 'not-a-site');
    fs.mkdirSync(notSite);
    const n = sync(notSite);
    expect(n.status).toBe(2);
    expect(n.stderr).toContain('no site.json');
  });

  it('usage errors exit 2', () => {
    expect(run('bash', [SYNC]).status).toBe(2);
    expect(run('bash', [SYNC, tmp, '--frobnicate']).status).toBe(2);
    expect(run('bash', [SYNC, tmp, '--check', '--dry-run']).status).toBe(2);
    expect(run('bash', [SYNC, tmp, '--check', '--verify-anchored']).status).toBe(2);
    expect(run('bash', [SYNC, tmp, '--verify', '--verify-anchored']).status).toBe(2);
    expect(run('bash', [SYNC, tmp, '--verify-anchored', '--dry-run']).status).toBe(2);
    expect(run('bash', [SYNC, '--help']).status).toBe(0);
    expect(run('python3', ['-I', VENDOR_TOOL]).status).toBe(2);
  });
});

/**
 * --verify-anchored: the check a site cannot satisfy by rewriting its own
 * MANIFEST.sha256. Each test builds a stand-in reusable-agents repository
 * (this checkout's sync script and engine, a copy of the kit) and syncs sites
 * from it.
 */
describe('sync-site-kit.sh --verify-anchored anchors a vendored copy to the kit as reusable-agents holds it', () => {
  let tmp = '';
  beforeEach(() => { tmp = tmpDir('anchor'); });
  afterEach(() => cleanupTmp(tmp));

  /** The stand-in checkout's own sync script, as a site's gate runs it. */
  const anchored = (ra: string, site: string, ...extra: string[]) => run('bash', [path.join(ra, 'install', 'sync-site-kit.sh'), site, '--verify-anchored', ...extra]);
  const syncFrom = (ra: string, site: string): void => {
    const r = run('bash', [path.join(ra, 'install', 'sync-site-kit.sh'), site, '--quiet']);
    if (r.status !== 0) throw new Error(describeRun(r));
  };
  const links = (site: string): string => path.join(site, 'vendor', 'affiliate-kit', 'server', 'src', 'links.ts');

  it('passes a copy synced from a committed kit, naming the commit; an edit with its manifest line rewritten passes --verify but not this', () => {
    const ra = fakeFramework(tmp, fakeKit(tmp));
    const head = gitIn(ra, 'rev-parse', 'HEAD').stdout.trim();
    const site = bareSite(tmp);
    syncFrom(ra, site);
    const ok = anchored(ra, site);
    expect(describeRun(ok)).toMatch(/^exit 0/);
    expect(ok.stdout).toContain(`kit ${KIT_VERSION}, `);
    expect(ok.stdout).toContain(`identical to kits/affiliate-site at commit ${head.slice(0, 12)}`);

    // The edit the gate must refuse: the CTA rel weakened, the manifest line rewritten to match.
    fs.writeFileSync(links(site), fs.readFileSync(links(site), 'utf8').replace(/sponsored nofollow noopener/g, 'noopener'));
    rewriteManifestLine(site, 'server/src/links.ts');
    expect(run('bash', [SYNC, site, '--verify']).status).toBe(0);
    const bad = anchored(ra, site);
    expect(bad.status).toBe(1);
    expect(bad.stderr).toContain(`matches no copy of kit ${KIT_VERSION}`);
    expect(bad.stderr).toContain(`closest: commit ${head.slice(0, 12)}`);
    expect(bad.stderr).toContain('modified: server/src/links.ts');
    expect(bad.stderr).toContain('re-sync it from the committed kit');

    // An added file, listed in the manifest, is refused the same way.
    syncFrom(ra, site);
    fs.writeFileSync(path.join(site, 'vendor', 'affiliate-kit', 'server', 'src', 'extra.ts'), 'export const x = 1;\n');
    rewriteManifestLine(site, 'server/src/extra.ts');
    expect(run('bash', [SYNC, site, '--verify']).status).toBe(0);
    const added = anchored(ra, site);
    expect(added.status).toBe(1);
    expect(added.stderr).toContain('not in the kit: server/src/extra.ts');
  });

  it('accepts any commit of the claimed version, and the working tree with a warning', () => {
    const ra = fakeFramework(tmp, fakeKit(tmp));
    const first = gitIn(ra, 'rev-parse', 'HEAD').stdout.trim();
    const site = bareSite(tmp);
    syncFrom(ra, site);
    const kitLinks = path.join(ra, 'kits', 'affiliate-site', 'server', 'src', 'links.ts');
    fs.appendFileSync(kitLinks, '\n// a later change, same version\n');

    // The kit moved on (uncommitted): the site still matches its commit.
    expect(anchored(ra, site).stdout).toContain(`at commit ${first.slice(0, 12)}`);

    // Synced from the uncommitted kit: anchored to the working tree, with a warning.
    syncFrom(ra, site);
    const wt = anchored(ra, site);
    expect(describeRun(wt)).toMatch(/^exit 0/);
    expect(wt.stdout).toContain('identical to the kit working tree');
    expect(wt.stderr).toContain('is in no commit');

    // Committed: anchored to the new commit; the copy of the first commit still passes too.
    const second = commitAll(ra, 'same version, a change');
    expect(anchored(ra, site).stdout).toContain(`at commit ${second.slice(0, 12)}`);
    // A site synced from the first commit's tree (links.ts is the only file the commits differ in).
    const older = bareSite(tmp, 'older');
    fs.writeFileSync(kitLinks, gitIn(ra, 'show', `${first}:kits/affiliate-site/server/src/links.ts`).stdout);
    syncFrom(ra, older);
    fs.writeFileSync(kitLinks, gitIn(ra, 'show', `${second}:kits/affiliate-site/server/src/links.ts`).stdout);
    expect(anchored(ra, older).stdout).toContain(`at commit ${first.slice(0, 12)}`);

    // Once the working tree moves on again, a copy of a commit-less state matches nothing: exit 1 (the version has commits).
    const uncommitted = bareSite(tmp, 'uncommitted');
    fs.appendFileSync(kitLinks, '\n// never committed\n');
    syncFrom(ra, uncommitted);
    fs.appendFileSync(kitLinks, '\n// and moved on\n');
    const gone = anchored(ra, uncommitted);
    expect(gone.status).toBe(1);
    expect(gone.stderr).toContain('nor the working tree');
  });

  it('version rules: older than the newest commit and never committed fails; newer than every commit cannot be anchored (exit 4); a non-version fails', () => {
    const kit = fakeKit(tmp);
    const ra = fakeFramework(tmp, kit);
    const site = bareSite(tmp);
    syncFrom(ra, site);
    const raKit = path.join(ra, 'kits', 'affiliate-site');
    setKitVersion(raKit, '0.2.0');
    commitAll(ra, 'kit 0.2.0');
    // Still the older release, still committed: passes.
    expect(describeRun(anchored(ra, site))).toMatch(/^exit 0/);

    const claim = (version: string) => {
      fs.writeFileSync(path.join(site, 'vendor', 'affiliate-kit', 'KIT_VERSION'), `${version}\n`);
      rewriteManifestLine(site, 'KIT_VERSION');
      return anchored(ra, site);
    };
    const never = claim('0.1.5');
    expect(never.status).toBe(1);
    expect(never.stderr).toContain('kit 0.1.5, which no commit');
    expect(never.stderr).toContain(`${KIT_VERSION}, 0.2.0`);

    const newer = claim('9.0.0');
    expect(newer.status).toBe(4);
    expect(newer.stderr).toContain('cannot anchor');
    expect(newer.stderr).toContain('no commit there holds kit 9.0.0 yet (the newest committed kit is 0.2.0)');
    expect(newer.stderr).toContain('its working tree is kit 0.2.0');

    const junk = claim('latest');
    expect(junk.status).toBe(1);
    expect(junk.stderr).toContain("KIT_VERSION 'latest' is not a kit version");
  });

  it('with no history to read only the working tree anchors, and a difference is exit 4, never a pass', () => {
    for (const [label, opts] of [['no-git', { git: false }], ['no-commit', { commit: false }]] as const) {
      const dir = path.join(tmp, label);
      fs.mkdirSync(dir);
      const ra = fakeFramework(dir, fakeKit(dir), opts);
      const site = bareSite(dir);
      syncFrom(ra, site);
      const wt = anchored(ra, site);
      expect(`${label}: ${describeRun(wt)}`).toMatch(/^[a-z-]+: exit 0/);
      expect(wt.stdout).toContain('identical to the kit working tree');
      fs.writeFileSync(links(site), fs.readFileSync(links(site), 'utf8').replace(/sponsored nofollow noopener/g, 'noopener'));
      rewriteManifestLine(site, 'server/src/links.ts');
      const r = anchored(ra, site);
      expect(`${label}: exit ${r.status}`).toBe(`${label}: exit 4`);
      expect(r.stderr).toContain('modified: server/src/links.ts');
      expect(r.stderr).toContain('checked only against its own MANIFEST.sha256');
    }
  });

  it('fails closed (exit 1) on a copy that does not match its own manifest, a missing copy, and a kit inside the site; ignores GIT_DIR', () => {
    const ra = fakeFramework(tmp, fakeKit(tmp));
    const site = bareSite(tmp);
    const missing = anchored(ra, site);
    expect(missing.status).toBe(1);
    expect(missing.stderr).toContain('is missing');
    syncFrom(ra, site);
    fs.appendFileSync(links(site), '\n// unrecorded\n');
    const raw = anchored(ra, site);
    expect(raw.status).toBe(1);
    expect(raw.stderr).toContain('does not match its MANIFEST.sha256');
    syncFrom(ra, site);
    const inside = anchored(ra, site, '--kit', path.join(site, 'vendor', 'affiliate-kit'));
    expect(inside.status).toBe(1);
    expect(inside.stderr).toContain('may not be inside the site');
    // A GIT_DIR in the caller's environment never redirects the history read.
    const env = run('bash', [path.join(ra, 'install', 'sync-site-kit.sh'), site, '--verify-anchored'], { env: { GIT_DIR: path.join(tmp, 'nowhere') } });
    expect(describeRun(env)).toMatch(/^exit 0/);
    expect(env.stdout).toContain('at commit');
    const notSite = path.join(tmp, 'not-a-site');
    fs.mkdirSync(notSite);
    expect(anchored(ra, notSite).status).toBe(1);
  });
});
