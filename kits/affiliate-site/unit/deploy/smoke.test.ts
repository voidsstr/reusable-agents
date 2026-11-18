/**
 * The post-deploy smoke check (deploytool smoke) against a local HTTP server,
 * and the DNS check (deploytool dns-check) against a local DNS-over-HTTPS stub.
 * These run for real: they are local network calls, not cloud commands.
 */
import { spawn } from 'child_process';
import * as http from 'http';
import type { AddressInfo } from 'net';
import { TOOL } from './harness';

jest.setTimeout(30_000);

interface Route { status: number; type: string; cc?: string; age?: string; location?: string; extra?: Record<string, string> }

const ROUTES: Record<string, Route> = {
  '/': { status: 200, type: 'text/html; charset=utf-8', cc: 'public, max-age=60, s-maxage=600' },
  '/health': { status: 200, type: 'application/json' },
  '/robots.txt': { status: 200, type: 'text/plain', cc: 'public, max-age=900, s-maxage=3600' },
  '/long-cache': { status: 200, type: 'text/html', cc: 'public, max-age=60, s-maxage=3600' },
  '/no-cache-header': { status: 200, type: 'text/html' },
  '/private': { status: 200, type: 'text/html', cc: 'private, no-store' },
  '/cdn-stale': { status: 200, type: 'text/html', cc: 'public, max-age=60, s-maxage=300', age: '7200' },
  '/moved': { status: 301, type: 'text/html', cc: 'public, max-age=60, s-maxage=600', location: '/' },
  '/broken': { status: 500, type: 'text/html', cc: 'no-store' },
  '/swr': { status: 200, type: 'text/html', cc: 'public, max-age=60, s-maxage=600, stale-while-revalidate=86400' },
  '/cdn-long': { status: 200, type: 'text/html', cc: 'public, max-age=60, s-maxage=600', extra: { 'CDN-Cache-Control': 'max-age=3600' } },
  '/cf-long': { status: 200, type: 'text/html', cc: 'public, max-age=60, s-maxage=600', extra: { 'Cloudflare-CDN-Cache-Control': 'max-age=86400' } },
  '/surrogate-ok': { status: 200, type: 'text/html', cc: 'public, max-age=60, s-maxage=600', extra: { 'Surrogate-Control': 'max-age=300' } },
};

let server: http.Server;
let port = 0;
const hosts: string[] = [];
const agents: string[] = [];
let flaky = 0;

beforeAll(async () => {
  server = http.createServer((req, res) => {
    hosts.push(req.headers.host ?? '');
    agents.push(req.headers['user-agent'] ?? '');
    let route = ROUTES[req.url ?? ''];
    if (req.url === '/flaky') route = ++flaky < 3 ? { status: 503, type: 'text/plain' } : ROUTES['/'];
    if (!route) {
      res.writeHead(404, { 'content-type': 'text/plain' });
      res.end('nope');
      return;
    }
    const headers: Record<string, string> = { 'content-type': route.type };
    if (route.cc) headers['cache-control'] = route.cc;
    if (route.age) headers.age = route.age;
    if (route.location) headers.location = route.location;
    Object.assign(headers, route.extra ?? {});
    res.writeHead(route.status, headers);
    res.end(route.type.startsWith('text/html') ? '<!doctype html><title>t</title>' : 'ok');
  });
  await new Promise<void>((r) => server.listen(0, '127.0.0.1', r));
  port = (server.address() as AddressInfo).port;
});
afterAll(async () => {
  await new Promise<void>((r) => server.close(() => r()));
});

/** Async so the in-process server can answer while the tool runs. */
function tool(args: string[]): Promise<{ status: number; stdout: string; stderr: string }> {
  return new Promise((resolve) => {
    const p = spawn('python3', ['-I', TOOL, ...args], { env: { PATH: process.env.PATH ?? '/usr/bin:/bin', LANG: 'C.UTF-8' } });
    let stdout = '';
    let stderr = '';
    p.stdout.on('data', (d) => { stdout += d; });
    p.stderr.on('data', (d) => { stderr += d; });
    p.on('close', (code) => resolve({ status: code ?? -1, stdout, stderr }));
  });
}

function smoke(paths: string[], extra: string[] = []) {
  return tool(['smoke', '--url', `http://127.0.0.1:${port}`, '--timeout', '1.5', '--request-timeout', '1',
    ...paths.flatMap((p) => ['--path', p]), ...extra]);
}

describe('smoke', () => {
  it('passes when every path answers as expected and HTML keeps s-maxage <= 600', async () => {
    const r = await smoke(['/', '/health', '/robots.txt', '/private', '/moved=301']);
    expect(r.stdout).toContain('all 5 path(s) passed');
    expect(r.status).toBe(0);
    expect(r.stdout).toContain('ok    / (200 text/html cache-control: public, max-age=60, s-maxage=600)');
  });

  it.each([
    ['/long-cache', 's-maxage=3600 > 600'],
    ['/no-cache-header', 'no Cache-Control header'],
    ['/cdn-stale', 'Age 7200s > 600s'],
    ['/broken', 'HTTP 500, expected 200'],
    ['/missing', 'HTTP 404, expected 200'],
    ['/moved', 'HTTP 301, expected 200'],
    ['/swr', 'stale-while-revalidate=86400'],
    ['/cdn-long', 'cdn-cache-control: max-age=3600 > 600'],
    ['/cf-long', 'cloudflare-cdn-cache-control: max-age=86400 > 600'],
  ])('fails %s', async (p, why) => {
    const r = await smoke([p]);
    expect(r.status).toBe(1);
    expect(r.stdout).toContain(`FAIL  ${p}`);
    expect(r.stdout).toContain(why);
  });

  it('accepts a Surrogate-Control within the limit', async () => {
    const r = await smoke(['/surrogate-ok', '/']);
    expect(r.status).toBe(0);
  });

  it('warns when fewer than two HTML pages were checked (the price pages are what the rule protects)', async () => {
    const one = await smoke(['/', '/health']);
    expect(one.status).toBe(0);
    expect(one.stdout).toContain('WARN  ');
    expect(one.stdout).toContain('the HTML cache rule was checked on 1 HTML page(s) (/); add a product page and a best-of list to SMOKE_PATHS');
    const two = await smoke(['/', '/surrogate-ok']);
    expect(two.stdout).not.toContain('WARN');
  });

  it('a lower limit applies to the same header', async () => {
    const r = await smoke(['/'], ['--max-shared-age', '300']);
    expect(r.status).toBe(1);
    expect(r.stdout).toContain('s-maxage=600 > 300');
  });

  it('retries until the deadline (a revision that is still starting)', async () => {
    flaky = 0;
    const r = await tool(['smoke', '--url', `http://127.0.0.1:${port}`, '--timeout', '20', '--path', '/flaky']);
    expect(r.status).toBe(0);
    expect(flaky).toBe(3);
  });

  it('--connect-to sends the custom domain as Host to another address; the user agent is honest', async () => {
    hosts.length = 0;
    agents.length = 0;
    const r = await tool(['smoke', '--url', 'http://kit-example.invalid', '--connect-to', `127.0.0.1:${port}`,
      '--timeout', '2', '--path', '/', '--user-agent', 'affiliate-kit-deploy-smoke/0.1.0 (+https://kit-example.invalid)']);
    expect(r.status).toBe(0);
    expect(hosts).toEqual(['kit-example.invalid']);
    expect(agents[0]).toMatch(/^affiliate-kit-deploy-smoke\//);
  });

  it('--optional skips a domain that does not resolve yet', async () => {
    const r = await tool(['smoke', '--url', 'https://kit-example.invalid', '--optional', '--timeout', '2', '--path', '/']);
    expect(r.status).toBe(0);
    expect(r.stdout).toContain('skip  ');
  });
});

describe('dns-check (DNS-over-HTTPS)', () => {
  let doh: http.Server;
  let dohPort = 0;
  const zone: Record<string, { type: number; data: string }[]> = {
    'A example.test': [{ type: 1, data: '51.8.15.167' }],
    'A proxied.test': [{ type: 1, data: '104.21.1.1' }, { type: 1, data: '172.67.1.1' }],
    'CNAME www.example.test': [{ type: 5, data: 'site.happysky-1.eastus.azurecontainerapps.io.' }],
    'TXT asuid.example.test': [{ type: 16, data: '"ABC123"' }],
    'TXT asuid.www.example.test': [{ type: 16, data: '"ABC123"' }],
  };

  beforeAll(async () => {
    doh = http.createServer((req, res) => {
      const u = new URL(req.url ?? '/', 'http://x');
      const key = `${u.searchParams.get('type')} ${u.searchParams.get('name')}`;
      res.writeHead(200, { 'content-type': 'application/dns-json' });
      res.end(JSON.stringify({ Status: 0, Answer: (zone[key] ?? []).map((a) => ({ name: u.searchParams.get('name'), ...a })) }));
    });
    await new Promise<void>((r) => doh.listen(0, '127.0.0.1', r));
    dohPort = (doh.address() as AddressInfo).port;
  });
  afterAll(async () => {
    await new Promise<void>((r) => doh.close(() => r()));
  });

  const check = (args: string[]) => tool(['dns-check', '--doh', `http://127.0.0.1:${dohPort}/dns-query`, '--timeout', '3', ...args]);

  it('passes when the apex A, the www CNAME and both asuid TXT records are right', async () => {
    const a = await check(['--host', 'example.test', '--expect-a', '51.8.15.167', '--expect-txt', 'asuid.example.test=ABC123']);
    expect(a.status).toBe(0);
    const w = await check(['--host', 'www.example.test', '--expect-cname', 'site.happysky-1.eastus.azurecontainerapps.io',
      '--expect-txt', 'asuid.www.example.test=ABC123']);
    expect(w.status).toBe(0);
    expect(w.stdout).toContain('ok    CNAME  www.example.test -> site.happysky-1.eastus.azurecontainerapps.io');
  });

  it('flags a proxied (CDN) record and a missing CNAME or TXT with exit 3', async () => {
    const p = await check(['--host', 'proxied.test', '--expect-a', '51.8.15.167']);
    expect(p.status).toBe(3);
    expect(p.stdout).toContain('managed certificates need DNS-only');
    const c = await check(['--host', 'proxied.test', '--expect-cname', 'x.azurecontainerapps.io']);
    expect(c.status).toBe(3);
    expect(c.stdout).toContain('no CNAME record');
    const t = await check(['--host', 'example.test', '--expect-txt', 'asuid.example.test=OTHER']);
    expect(t.status).toBe(3);
  });

  it('exits 4 when no resolver answers', async () => {
    const r = await tool(['dns-check', '--doh', 'http://127.0.0.1:9/dns-query', '--timeout', '2', '--host', 'example.test', '--expect-a', '1.2.3.4']);
    expect(r.status).toBe(4);
  });
});
