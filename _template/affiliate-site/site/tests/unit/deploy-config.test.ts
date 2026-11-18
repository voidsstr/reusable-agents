import { execFileSync } from 'child_process';
import * as fs from 'fs';
import * as path from 'path';
import { findSiteRoot, loadSite, sitePaths } from '../../src/config/site';
import { ROUTES, pageRouteEntries } from '../../src/routes/table';

/**
 * The deploy files agree with each other and with site.json, so a mistake
 * shows up here instead of halfway through a deploy:
 *   deploy.env          parsed exactly as the kit's deploy scripts parse it;
 *                       every placeholder of the kit's example replaced, and
 *                       a smoke check only of routes this site serves
 *   Dockerfile.azure    rendered from the vendored kit's template (re-render
 *                       after every kit sync)
 *   package.json        the image build makes every runtime path readable
 *   tests/suite.env     the gate's compose files, fixture SQL and ports
 */

const ROOT = path.resolve(__dirname, '..', '..');
const KIT = path.join(ROOT, 'vendor', 'affiliate-kit');
const ctx = loadSite(sitePaths(findSiteRoot(__dirname, {})), {});
const KIT_VERSION = fs.readFileSync(path.join(KIT, 'KIT_VERSION'), 'utf8').trim();

/** A deploy.env-format file as the deploy scripts read it (deploytool env-dump: never sourced, unknown keys are errors). */
function envDump(file: string): Record<string, string> {
  const out = execFileSync('python3', ['-I', path.join(KIT, 'deploy', 'lib', 'deploytool.py'), 'env-dump', '--strict', file]);
  const parts = out.toString('utf8').split('\0');
  const env: Record<string, string> = {};
  for (let i = 0; i + 1 < parts.length; i += 2) env[parts[i] as string] = parts[i + 1] as string;
  return env;
}

const deployEnv = (): Record<string, string> => envDump(path.join(ROOT, 'deploy.env'));

/** An express path pattern ('/guides/:slug') as an anchored regex. */
function routeRegex(pattern: string): RegExp {
  const parts = pattern.split(/(:[A-Za-z_][A-Za-z0-9_]*)/).map((p) => (p.startsWith(':') ? '[^/]+' : p.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
  return new RegExp(`^${parts.join('')}$`);
}

/** tests/suite.env: KEY='value' lines (the runner sources it with bash). */
function suiteEnv(): Record<string, string> {
  const env: Record<string, string> = {};
  for (const line of fs.readFileSync(path.join(ROOT, 'tests', 'suite.env'), 'utf8').split('\n')) {
    const m = /^([A-Z][A-Z0-9_]*)='([^']*)'\s*$/.exec(line);
    if (m) env[m[1] as string] = m[2] as string;
  }
  return env;
}

describe('deploy.env', () => {
  const env = deployEnv();

  it('names the domain and site.json this site serves', () => {
    expect(env.SITE_DOMAIN).toBe(ctx.site.domain);
    expect((env.SITE_HOSTNAMES ?? '').split(/\s+/)).toContain(ctx.site.domain);
    expect(env.SITE_JSON ?? 'site.json').toBe('site.json');
    expect(new URL(ctx.site.canonicalOrigin).host).toMatch(new RegExp(`^(www\\.)?${ctx.site.domain.replace(/\./g, '\\.')}$`));
  });

  it('keeps the HTML cache within the limit the price window assumes', () => {
    const limit = Number(env.HTML_MAX_S_MAXAGE ?? 600);
    expect(limit).toBeLessThanOrEqual(600);
    expect(ctx.cache.htmlSMaxAgeSeconds).toBeLessThanOrEqual(limit);
    expect(ctx.cache.htmlMaxAgeSeconds).toBeLessThanOrEqual(limit);
  });

  it('every extra image path exists and the image build makes it readable', () => {
    const extra = (env.DOCKER_EXTRA_PATHS ?? '').split(/\s+/).filter(Boolean);
    const pkg = JSON.parse(fs.readFileSync(path.join(ROOT, 'package.json'), 'utf8')) as { scripts?: Record<string, string> };
    const script = env.BUILD_SCRIPT ?? 'build';
    const cmd = pkg.scripts?.[script] ?? '';
    expect(cmd).not.toBe('');
    // The image copies these from the build context with the checkout's file
    // modes; a checkout made under a restrictive umask or default ACL would
    // leave them unreadable to the image's non-root user, so the build makes
    // them world-readable inside the image.
    const chmod = /chmod -R a\+rX ([^&|;]+)/.exec(cmd);
    expect(chmod).not.toBeNull();
    const covered = new Set((chmod?.[1] ?? '').trim().split(/\s+/));
    for (const p of ['package.json', 'site.json', 'dist', 'vendor/affiliate-kit', ...extra]) expect(covered).toContain(p);
    for (const p of extra) expect(fs.existsSync(path.join(ROOT, p))).toBe(true);
  });

  it('MIGRATE_COMMAND runs the vendored migration runner on db/migrations', () => {
    expect(env.MIGRATE_COMMAND ?? '').toMatch(/^node dist\/vendor\/affiliate-kit\/server\/src\/db\/migrate\.js( |$)/);
    expect(fs.existsSync(path.join(ROOT, 'db', 'migrations'))).toBe(true);
  });

  it('SMOKE_PATHS checks only routes this site serves, and at least two of its HTML pages', () => {
    // The post-release smoke expects 200 from each plain path; one this site
    // does not serve fails every release (and rolls it back). A path=<status>
    // entry asserts that status instead (a redirect, a 410) and is left alone.
    const entries = (env.SMOKE_PATHS ?? '').split(/\s+/).filter(Boolean);
    expect(entries.length).toBeGreaterThan(0);
    const plain = entries.filter((e) => !e.includes('=')).map((e) => e.split('?')[0] as string);
    const fixedPaths = new Set(ROUTES.filter((r) => !r.pattern.includes(':')).map((r) => r.pattern));
    const patterns = pageRouteEntries().filter((r) => r.pattern.includes(':')).map((r) => routeRegex(r.pattern));
    expect(plain.filter((p) => !fixedPaths.has(p) && !patterns.some((re) => re.test(p)))).toEqual([]);
    // The HTML cache rule is checked on each HTML page the smoke fetches.
    const pages = new Set(pageRouteEntries().map((r) => r.pattern));
    expect(plain.filter((p) => pages.has(p) || patterns.some((re) => re.test(p))).length).toBeGreaterThanOrEqual(2);
  });

  it('carries none of the kit example\'s placeholder values', () => {
    // Every value deploy.env.example sets to an example (examplepicks,
    // B0EXAMPLE00, example.com) is a site value: left as is, it names another
    // site's resources or a page that does not exist.
    const example = envDump(path.join(KIT, 'deploy', 'deploy.env.example'));
    const leftovers = Object.entries(env)
      .filter(([k, v]) => /example/i.test(example[k] ?? '') && v === example[k])
      .map(([k, v]) => `${k}=${v}`);
    expect(leftovers).toEqual([]);
    expect(Object.entries(env).filter(([, v]) => /examplepicks|B0EXAMPLE|example-best-list/i.test(v)).map(([k]) => k)).toEqual([]);
  });
});

describe('Dockerfile.azure', () => {
  it('was rendered from the vendored kit template (re-render after a kit sync)', () => {
    const first = fs.readFileSync(path.join(ROOT, 'Dockerfile.azure'), 'utf8').split('\n')[0];
    expect(first).toBe(`# rendered-from: affiliate-kit/deploy/Dockerfile.azure.tmpl (kit ${KIT_VERSION})`);
  });
});

describe('tests/suite.env', () => {
  const env = suiteEnv();

  it('names this site\'s gate project, compose files and fixture SQL', () => {
    expect(env.SUITE_PROJECT).toBe(ctx.site.id);
    for (const f of (env.SUITE_COMPOSE_FILES ?? '').split(/\s+/).filter(Boolean)) expect(fs.existsSync(path.join(ROOT, f))).toBe(true);
    for (const f of (env.SUITE_FIXTURE_FRESHEN_SQL ?? '').split(/\s+/).filter(Boolean)) expect(fs.existsSync(path.join(ROOT, f))).toBe(true);
  });

  it('uses two different ports and never overlaps another gate on this host', () => {
    expect(env.SUITE_APP_PORT).toMatch(/^\d+$/);
    expect(env.SUITE_DB_PORT).toMatch(/^\d+$/);
    expect(env.SUITE_APP_PORT).not.toBe(env.SUITE_DB_PORT);
    // The other gates' projects by name (their locks are created when missing,
    // so the exclusion holds after a reboot too), plus '*' for any other.
    const locks = (env.SUITE_HOST_LOCKS ?? '').split(/\s+/).filter(Boolean);
    expect(locks).toContain('*');
    for (const p of locks) expect(p).toMatch(/^(\*|[a-z0-9][a-z0-9_-]{0,62})$/);
    expect(locks).not.toContain(env.SUITE_PROJECT);
  });
});
