/**
 * Opt-in integration test: the rendered Dockerfile.azure really builds and runs.
 * A minimal dependency-free site (a node http server that answers /health and
 * an SSR page with the kit's Cache-Control) is built with the real docker from
 * the rendered template, run locally, smoke-checked with deploytool, and
 * removed. Nothing is pushed. Skipped unless KIT_DEPLOY_IT_DOCKER=1 (it needs
 * docker and the NODE_IMAGE, pulled if not cached).
 */
import { spawnSync } from 'child_process';
import * as fs from 'fs';
import * as path from 'path';
import { makeSandbox, removeSandbox, runScript, runTool, which, type Sandbox } from './harness';

const run = process.env.KIT_DEPLOY_IT_DOCKER === '1' && which('docker') !== null;
const describeIt = run ? describe : describe.skip;
jest.setTimeout(600_000);

const SERVER = `const http = require('http');
const port = Number(process.env.PORT || 8080);
http.createServer((req, res) => {
  if (req.url === '/health') { res.writeHead(200, { 'content-type': 'application/json' }); return res.end('{"status":"ok"}'); }
  if (req.url === '/') {
    res.writeHead(200, { 'content-type': 'text/html; charset=utf-8', 'cache-control': 'public, max-age=60, s-maxage=600' });
    return res.end('<!doctype html><title>kit</title><p>' + (process.getuid ? process.getuid() : '') + '</p>');
  }
  res.writeHead(404, { 'content-type': 'text/plain' }); res.end('not found');
}).listen(port, () => console.log('listening on ' + port));
`;

function docker(args: string[], input?: string) {
  return spawnSync('docker', args, { encoding: 'utf8', input, timeout: 600_000 });
}

describeIt('Dockerfile.azure builds and serves (opt-in)', () => {
  let sb: Sandbox;
  const tag = `kit-deploy-it:${process.pid}`;
  const name = `kit-deploy-it-${process.pid}`;
  let port = 0;

  beforeAll(() => {
    sb = makeSandbox();
    const site = path.join(sb.fx, 'site');
    fs.writeFileSync(path.join(site, 'package.json'), JSON.stringify({
      name: 'kit-deploy-it-site', version: '0.0.0', private: true,
      scripts: { build: "node -e \"require('fs').mkdirSync('dist/src',{recursive:true});require('fs').copyFileSync('src/server.js','dist/src/index.js')\"" },
    }, null, 2));
    fs.writeFileSync(path.join(site, 'package-lock.json'), JSON.stringify({
      name: 'kit-deploy-it-site', version: '0.0.0', lockfileVersion: 3, requires: true,
      packages: { '': { name: 'kit-deploy-it-site', version: '0.0.0' } },
    }, null, 2));
    fs.writeFileSync(path.join(site, 'tsconfig.json'), '{}\n');
    for (const d of ['src', 'db', 'content', 'vendor/affiliate-kit/migrations']) fs.mkdirSync(path.join(site, d), { recursive: true });
    fs.writeFileSync(path.join(site, 'src/server.js'), SERVER);
    fs.writeFileSync(path.join(site, 'db/.keep'), '');
    fs.writeFileSync(path.join(site, 'content/.keep'), '');
    fs.writeFileSync(path.join(site, 'vendor/affiliate-kit/VERSION'), '0.1.0\n');
    const r = runScript(sb, 'render-dockerfile.sh', ['--env', sb.envFile, '--write']);
    expect(r.status).toBe(0);
    port = 55470 + (process.pid % 20);
  });

  afterAll(() => {
    docker(['rm', '-f', name]);
    docker(['image', 'rm', '-f', tag]);
    if (sb) removeSandbox(sb);
  });

  it('builds, runs as the node user, passes its HEALTHCHECK and the smoke check', async () => {
    const site = path.join(sb.fx, 'site');
    // A checkout no one else may read (a restrictive umask or default ACL, as on
    // the fleet host's deploy worktrees): the image must work anyway.
    const restrict = (p: string): void => {
      const st = fs.lstatSync(p);
      if (st.isSymbolicLink()) return;
      if (st.isDirectory()) {
        for (const e of fs.readdirSync(p)) restrict(path.join(p, e));
        fs.chmodSync(p, 0o750);
      } else {
        fs.chmodSync(p, 0o640);
      }
    };
    restrict(site);
    expect(fs.statSync(path.join(site, 'site.json')).mode & 0o777).toBe(0o640);
    const b = docker(['build', '--platform', 'linux/amd64', '-f', path.join(site, 'Dockerfile.azure'), '-t', tag, site]);
    expect(b.stderr + b.stdout).not.toMatch(/ERROR/);
    expect(b.status).toBe(0);
    const user = docker(['image', 'inspect', tag, '--format', '{{.Config.User}} {{json .Config.Entrypoint}} {{json .Config.Cmd}}']);
    expect(user.stdout.trim()).toBe('node ["dumb-init","--"] ["node","dist/src/index.js"]');
    expect(docker(['run', '-d', '--name', name, '-p', `127.0.0.1:${port}:8080`, tag]).status).toBe(0);
    const smoke = runTool(['smoke', '--url', `http://127.0.0.1:${port}`, '--timeout', '60', '--path', '/', '--path', '/health', '--path', '/nope=404']);
    expect(smoke.stdout).toContain('all 3 path(s) passed');
    let health = '';
    for (let i = 0; i < 40 && health !== 'healthy'; i++) {
      health = docker(['inspect', name, '--format', '{{.State.Health.Status}}']).stdout.trim();
      if (health !== 'healthy') await new Promise((r) => setTimeout(r, 2000));
    }
    expect(health).toBe('healthy');
    const uid = docker(['exec', name, 'id', '-un']).stdout.trim();
    expect(uid).toBe('node');
    const write = docker(['exec', name, 'sh', '-c', 'touch /app/dist/x 2>&1; echo $?']).stdout.trim();
    expect(write.split('\n').pop()).not.toBe('0');
    // Readable as node although the checkout was 0640/0750 (nested files: COPY
    // creates the top-level destination directories 0755 whatever the source).
    for (const cmd of ['cat /app/site.json', 'cat /app/package.json', 'cat /app/db/.keep', 'cat /app/content/.keep',
      'cat /app/vendor/affiliate-kit/VERSION', 'ls /app/vendor/affiliate-kit/migrations', 'cat /app/dist/src/index.js']) {
      const r = docker(['exec', name, 'sh', '-c', `${cmd} >/dev/null`]);
      expect([cmd, r.status, r.stderr]).toEqual([cmd, 0, '']);
    }
    for (const p of ['/app/site.json', '/app/vendor/affiliate-kit/VERSION', '/app/db']) {
      const w = docker(['exec', name, 'sh', '-c', `test -w ${p} && echo writable || echo read-only`]).stdout.trim();
      expect([p, w]).toEqual([p, 'read-only']);
    }
  });
});

if (!run) {
  it('docker integration test skipped (set KIT_DEPLOY_IT_DOCKER=1)', () => {
    expect(run).toBe(false);
  });
}
