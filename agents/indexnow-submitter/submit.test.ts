/**
 * Tests for submit.ts — pure helpers plus an end-to-end run of the real
 * script against a local mock site and a mock IndexNow endpoint.
 *
 * Run (the pytest wrapper framework/tests/test_indexnow_submitter.py does this):
 *   NODE_PATH=<app>/node_modules npx ts-node --transpile-only \
 *     --compiler-options '{"module":"node16","moduleResolution":"node16","esModuleInterop":true,"skipLibCheck":true}' \
 *     agents/indexnow-submitter/submit.test.ts
 */
import assert from 'assert/strict';
import fs from 'fs';
import http from 'http';
import os from 'os';
import path from 'path';
import { spawn } from 'child_process';
import {
  buildUrl, classifyPage, decideCandidate, diffSnapshots, headerNoindex, isSyntheticLastmod,
  mergeSnapshot, normalizeToken, readLedger, writeLedger, DEFAULT_POLICY, Snapshot,
} from './submit';

const DAY = 86_400_000;
let failures = 0;
async function test(name: string, fn: () => void | Promise<void>) {
  try { await fn(); console.log(`ok   ${name}`); } catch (e: any) { failures += 1; console.log(`FAIL ${name}\n     ${e?.stack || e}`); }
}

async function unit() {
  await test('compose keeps a literal separator like -vs- (was dropped → /compare/ab 404s)', () => {
    const qs = { name: 'hw', bulkSql: '', urlTemplate: 'compose:left_ref|-vs-|right_ref', urlPrefix: '/compare/' };
    assert.equal(buildUrl('https://x.test', qs, { left_ref: 'a', right_ref: 'b' }), 'https://x.test/compare/a-vs-b');
    const qs2 = { ...qs, urlTemplate: 'compose:left_ref|/|right_ref', urlPrefix: '/vs/' };
    assert.equal(buildUrl('https://x.test', qs2, { left_ref: 'A1', right_ref: 'B2' }), 'https://x.test/vs/A1/B2');
    const qs3 = { ...qs, urlTemplate: 'compose:left_ref|lit:-and-|right_ref' };
    assert.equal(buildUrl('https://x.test', qs3, { left_ref: 'a', right_ref: 'b' }), 'https://x.test/compare/a-and-b');
  });
  await test('a column the row lacks, or a null value, skips the URL', () => {
    const qs = { name: 'r', bulkSql: '', urlTemplate: 'slugify:title|-|id', urlPrefix: '/recipes/' };
    assert.equal(buildUrl('https://x.test', qs, { title: 'Chili Mac', id: 12 }), 'https://x.test/recipes/chili-mac-12');
    assert.equal(buildUrl('https://x.test', qs, { title: null, id: 12 }), null);
    assert.equal(buildUrl('https://x.test', qs, { id: 12 }), null);
  });
  await test('normalizeToken makes date-only lastmod and DB timestamps comparable', () => {
    assert.equal(normalizeToken('2026-09-24'), '2026-09-24');
    assert.equal(normalizeToken('2026-09-24T13:05:00.000Z'), '2026-09-24');
    assert.equal(normalizeToken(new Date('2026-09-24T23:59:00Z')), '2026-09-24');
    assert.equal(normalizeToken(null), '');
  });
  await test('ledger gate: new, too-soon, changed, unchanged, stale, rejected', () => {
    const P = DEFAULT_POLICY; const now = Date.parse('2026-09-25T12:00:00Z');
    assert.equal(decideCandidate(undefined, { token: '' }, now, 'incremental', P).reason, 'new');
    const sent = (ageDays: number, token = '2026-09-20') => ({ status: 'sent', at: now - ageDays * DAY, token });
    assert.equal(decideCandidate(sent(0.5), { token: '2026-09-25' }, now, 'incremental', P).reason, 'too-soon');
    assert.equal(decideCandidate(sent(2), { token: '2026-09-25' }, now, 'incremental', P).reason, 'changed');
    // Same token: unchanged however old in incremental mode (the repeated
    // future-dated-article case), re-confirmed only by the slow bulk rotation.
    assert.equal(decideCandidate(sent(10), { token: '2026-09-20' }, now, 'incremental', P).reason, 'unchanged');
    assert.equal(decideCandidate(sent(10), { token: '2026-09-20' }, now, 'bulk', P).reason, 'unchanged');
    assert.equal(decideCandidate(sent(30), { token: '2026-09-20' }, now, 'bulk', P).reason, 'stale');
    // Token-less candidates (force queue): time window.
    assert.equal(decideCandidate(sent(3, ''), { token: '' }, now, 'incremental', P).reason, 'unchanged');
    assert.equal(decideCandidate(sent(8, ''), { token: '' }, now, 'incremental', P).reason, 'stale');
    const rej = (ageDays: number) => ({ status: 'rej:http-404', at: now - ageDays * DAY, token: '' });
    assert.equal(decideCandidate(rej(1), { token: '' }, now, 'incremental', P).reason, 'recently-rejected');
    const d = decideCandidate(rej(4), { token: '' }, now, 'incremental', P);
    assert.equal(d.reason, 'recheck-rejected'); assert.equal(d.needVerify, true);
  });
  await test('ledger round-trips, merges with a concurrent writer, prunes old rows', () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'inx-ledger-'));
    const f = path.join(dir, 'l.tsv'); const now = Date.now();
    writeLedger(f, new Map([['https://x.test/a', { status: 'sent', at: now - 5, token: 't1' }],
      ['https://x.test/old', { status: 'sent', at: now - 400 * DAY, token: '' }]]), 120, now);
    writeLedger(f, new Map([['https://x.test/b', { status: 'rej:http-404', at: now, token: '' }],
      ['https://x.test/a', { status: 'sent', at: now - 10, token: 'older' }]]), 120, now);
    const l = readLedger(f);
    assert.equal(l.get('https://x.test/a')!.token, 't1');          // newer entry kept
    assert.equal(l.get('https://x.test/b')!.status, 'rej:http-404');
    assert.equal(l.has('https://x.test/old'), false);               // pruned
  });
  await test('synthetic lastmod ("today" on every URL) is detected', () => {
    const now = Date.parse('2026-09-25T12:00:00Z');
    const same: Record<string, string> = {}; const real: Record<string, string> = {};
    for (let i = 0; i < 40; i++) { same[`u${i}`] = '2026-09-25'; real[`u${i}`] = `2026-08-${String(1 + (i % 28)).padStart(2, '0')}`; }
    assert.equal(isSyntheticLastmod(same, now), true);
    assert.equal(isSyntheticLastmod(real, now), false);
  });
  await test('sitemap diff: baseline yields nothing; then only new locs and real lastmod changes', () => {
    const child = (entries: Record<string, string>, synthetic = false) => ({ fetchedAt: 'x', synthetic, entries });
    assert.deepEqual(diffSnapshots(null, { s1: child({ a: '2026-09-01' }) }), []);
    const prev: Snapshot = { version: 1, fetchedAt: 'x', children: {
      s1: child({ a: '2026-09-01', b: '2026-09-01' }), s2: child({ h: '2026-09-24' }, true) } };
    const got = diffSnapshots(prev, {
      // b moved to s3 (pagination shift) — not new; c is new; a changed.
      s1: child({ a: '2026-09-20' }), s3: child({ b: '2026-09-01', c: '2026-09-20' }),
      s2: child({ h: '2026-09-25' }, true),                     // synthetic churn ignored
    });
    assert.deepEqual(got.map((d) => `${d.loc}:${d.reason}`).sort(), ['a:sitemap-lastmod', 'c:sitemap-new']);
  });
  await test('ignoreLastmodPrefixes: a trend "last seen" lastmod is not a change; new locs still are', () => {
    const child = (entries: Record<string, string>) => ({ fetchedAt: 'x', synthetic: false, entries });
    const prev: Snapshot = { version: 1, fetchedAt: 'x', children: { s: child({ 'https://x.test/vs/a/b': '2026-09-01', 'https://x.test/p/1': '2026-09-01' }) } };
    const got = diffSnapshots(prev, { s: child({ 'https://x.test/vs/a/b': '2026-09-24', 'https://x.test/vs/c/d': '2026-09-24', 'https://x.test/p/1': '2026-09-24' }) }, ['/vs/']);
    assert.deepEqual(got.map((d) => `${d.loc.slice(14)}:${d.reason}`).sort(), ['/p/1:sitemap-lastmod', '/vs/c/d:sitemap-new']);
  });
  await test('snapshot merge carries failed children, drops unlisted ones', () => {
    const c = (n: string) => ({ fetchedAt: 'x', synthetic: false, entries: { [n]: '' } });
    const prev: Snapshot = { version: 1, fetchedAt: 'x', children: { s1: c('a'), s2: c('b'), gone: c('g') } };
    const merged = mergeSnapshot(prev, { fetched: { s1: c('a2') }, failed: new Set(['s2']),
      discovered: new Set(['idx', 's1', 's2']), rootFailed: false }, 'now');
    assert.deepEqual(Object.keys(merged.children).sort(), ['s1', 's2']);
    assert.ok(merged.children.s1.entries.a2 !== undefined);
    const rootDown = mergeSnapshot(prev, { fetched: {}, failed: new Set(['idx']), discovered: new Set(['idx']), rootFailed: true }, 'now');
    assert.deepEqual(Object.keys(rootDown.children).sort(), ['gone', 's1', 's2']);
  });
  await test('page classification: 200 index / noindex meta / noindex header / redirect / canonical', () => {
    const u = 'https://x.test/p';
    const page = (head: string) => `<html><head>${head}</head><body>hello world</body></html>`;
    assert.deepEqual(classifyPage(u, 200, 'index, follow', page('<meta name="robots" content="index, follow"><link rel="canonical" href="https://x.test/p">')), { ok: true, reason: 'ok' });
    assert.equal(classifyPage(u, 200, null, page('<meta content="noindex, follow" name="robots" />')).reason, 'noindex-meta');
    assert.equal(classifyPage(u, 200, null, page('<meta name="bingbot" content="noindex">')).reason, 'noindex-meta');
    assert.equal(classifyPage(u, 200, 'noindex, follow', page('')).reason, 'noindex-header');
    assert.equal(classifyPage(u, 301, null, '').reason, 'redirect-301');
    assert.equal(classifyPage(u, 404, null, '').reason, 'http-404');
    assert.equal(classifyPage(u, 200, null, page('<link rel="canonical" href="/other">')).reason, 'canonical-elsewhere');
    assert.equal(classifyPage(u, 200, null, page('<link rel="canonical" href="/p/">')).ok, true);
    assert.equal(classifyPage(u, 200, null, page(''), 50).reason, 'thin-body');
  });
  await test('X-Robots-Tag scoping: googlebot-only noindex does not block Bing', () => {
    assert.equal(headerNoindex('googlebot: noindex'), false);
    assert.equal(headerNoindex('bingbot: noindex'), true);
    assert.equal(headerNoindex('max-snippet:-1, noindex'), true);
    assert.equal(headerNoindex('index, follow'), false);
  });
}

// ── End to end: the real script against a mock site + mock IndexNow ────────

type Page = { status: number; headers?: Record<string, string>; body?: string };

function startServer(routes: () => Record<string, Page>, posts: any[]): Promise<{ port: number; close: () => void }> {
  return new Promise((resolve) => {
    const srv = http.createServer((req, res) => {
      if (req.method === 'POST' && req.url === '/indexnow') {
        let b = ''; req.on('data', (c) => (b += c)); req.on('end', () => { posts.push(JSON.parse(b)); res.writeHead(200); res.end('ok'); });
        return;
      }
      const p = routes()[req.url || ''];
      if (!p) { res.writeHead(404); res.end('nope'); return; }
      res.writeHead(p.status, p.headers || {}); res.end(p.body || '');
    });
    srv.listen(0, '127.0.0.1', () => resolve({ port: (srv.address() as any).port, close: () => srv.close() }));
  });
}

// Async spawn: the mock site lives in THIS process, so a synchronous spawn
// would block it and every fetch the child makes would hang.
function runScript(args: string[], env: Record<string, string>): Promise<{ rc: number; out: string }> {
  return new Promise((resolve) => {
    const child = spawn('npx', ['--no-install', 'ts-node', '--transpile-only', '--compiler-options',
      '{"module":"node16","moduleResolution":"node16","esModuleInterop":true,"skipLibCheck":true}',
      path.join(__dirname, 'submit.ts'), ...args], { env: { ...process.env, ...env } });
    let out = '';
    child.stdout.on('data', (d) => (out += d));
    child.stderr.on('data', (d) => (out += d));
    const timer = setTimeout(() => child.kill('SIGKILL'), 120_000);
    child.on('close', (code) => { clearTimeout(timer); resolve({ rc: code ?? -1, out }); });
  });
}

async function e2e() {
  const posts: any[] = [];
  let origin = '';
  const html = (loc: string, extraHead = '') =>
    `<html><head><title>t</title><link rel="canonical" href="${origin}${loc}">${extraHead}</head><body>content</body></html>`;
  let sitemapUrls = ['/a', '/b'];
  const routes = (): Record<string, Page> => ({
    '/': { status: 200, body: html('/') },
    '/ok': { status: 200, body: html('/ok') },
    '/new': { status: 200, body: html('/new') },
    '/a': { status: 200, body: html('/a') }, '/b': { status: 200, body: html('/b') }, '/c': { status: 200, body: html('/c') },
    '/noindex-meta': { status: 200, body: html('/noindex-meta', '<meta name="robots" content="noindex, follow">') },
    '/noindex-hdr': { status: 200, headers: { 'X-Robots-Tag': 'noindex' }, body: html('/noindex-hdr') },
    '/moved': { status: 301, headers: { Location: '/ok' } },
    '/canon-else': { status: 200, body: html('/ok') },
    '/sitemap.xml': { status: 200, body: `<sitemapindex><sitemap><loc>${origin}/sitemap-1.xml</loc></sitemap></sitemapindex>` },
    '/sitemap-1.xml': { status: 200, body: `<urlset>${sitemapUrls.map((u) => `<url><loc>${origin}${u}</loc><lastmod>2026-09-01</lastmod></url>`).join('')}</urlset>` },
  });
  const srv = await startServer(routes, posts);
  origin = `http://127.0.0.1:${srv.port}`;
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'inx-e2e-'));
  const cfgFile = path.join(dir, 'site-indexnow.json');
  fs.writeFileSync(cfgFile, JSON.stringify({ sites: [{
    name: 'mock', host: '127.0.0.1', origin, key: 'k', databaseUrlEnv: 'INX_TEST_NO_DB',
    watermarkFile: path.join(dir, 'mock-watermark.txt'), staticPaths: ['/'], querySets: [],
    sitemapUrls: [`${origin}/sitemap.xml`], policy: { sitemapIntervalHours: 0 },
    verify: { concurrency: 2, timeoutMs: 5000, budgetSeconds: 30 },
  }] }));
  const env = { SITE_CONFIG_PATHS: cfgFile, INDEXNOW_ENDPOINT: `${origin}/indexnow` };
  const queue = path.join(dir, 'mock.force-submit.txt');
  const paths = (i: number) => (posts[i]?.urlList || []).map((u: string) => u.slice(origin.length)).sort();

  try {
    await test('e2e: dry run writes no state', async () => {
      fs.writeFileSync(queue, `${origin}/ok\n`);
      const r = await runScript(['--site=mock', '--dry-run'], env);
      assert.equal(r.rc, 0, r.out);
      assert.equal(posts.length, 0);
      assert.equal(fs.existsSync(path.join(dir, 'mock.indexnow-ledger.tsv')), false);
      assert.equal(fs.existsSync(path.join(dir, 'mock-watermark.txt')), false);
      assert.equal(fs.readFileSync(queue, 'utf-8'), `${origin}/ok\n`);
    });

    await test('e2e run 1: only the 200/indexable/self-canonical queue URL is submitted; sitemap is a baseline', async () => {
      fs.writeFileSync(queue, ['/ok', '/noindex-meta', '/noindex-hdr', '/gone', '/moved', '/canon-else']
        .map((p) => origin + p).concat(['https://elsewhere.test/x']).join('\n') + '\n');
      const r = await runScript(['--site=mock'], env);
      assert.equal(r.rc, 0, r.out);
      assert.equal(posts.length, 1, r.out);
      assert.deepEqual(paths(0), ['/ok']);
      const l = readLedger(path.join(dir, 'mock.indexnow-ledger.tsv'));
      assert.equal(l.get(`${origin}/ok`)!.status, 'sent');
      assert.equal(l.get(`${origin}/gone`)!.status, 'rej:http-404');
      assert.equal(l.get(`${origin}/moved`)!.status, 'rej:redirect-301');
      assert.equal(l.get(`${origin}/noindex-meta`)!.status, 'rej:noindex-meta');
      assert.equal(l.get(`${origin}/noindex-hdr`)!.status, 'rej:noindex-header');
      assert.equal(l.get(`${origin}/canon-else`)!.status, 'rej:canonical-elsewhere');
      assert.equal(fs.readFileSync(queue, 'utf-8').trim(), '');               // drained
      assert.ok(fs.existsSync(path.join(dir, 'mock.sitemap-snapshot.json')));
      assert.ok(fs.existsSync(path.join(dir, 'mock-watermark.txt')));
    });

    await test('e2e run 2: repeats and recent rejects are skipped; new queue + new sitemap URLs go out', async () => {
      fs.writeFileSync(queue, [`${origin}/ok`, `${origin}/gone`, `${origin}/new`].join('\n') + '\n');
      sitemapUrls = ['/a', '/b', '/c'];
      const r = await runScript(['--site=mock'], env);
      assert.equal(r.rc, 0, r.out);
      assert.equal(posts.length, 2, r.out);
      assert.deepEqual(paths(1), ['/c', '/new']);
    });

    await test('e2e run 3 (--bulk): unsent sitemap URLs + verified static path; nothing re-sent', async () => {
      const r = await runScript(['--site=mock', '--bulk'], env);
      assert.equal(r.rc, 0, r.out);
      assert.equal(posts.length, 3, r.out);
      assert.deepEqual(paths(2), ['/', '/a', '/b']);
    });

    await test('e2e run 4: nothing changed → no POST at all', async () => {
      const r = await runScript(['--site=mock'], env);
      assert.equal(r.rc, 0, r.out);
      assert.equal(posts.length, 3, r.out);
      assert.match(r.out, /done submitted=0 failed=0/);
    });
  } finally {
    srv.close();
  }
}

(async () => {
  await unit();
  if (!process.env.INDEXNOW_SKIP_E2E) await e2e();
  console.log(failures ? `${failures} FAILED` : 'all passed');
  process.exit(failures ? 1 : 0);
})();
