#!/usr/bin/env node
/**
 * The kit gate proves itself on its fixture site (kit build item K5).
 *
 *   node tests/selftest/gate-selftest.mjs [--out DIR] [--keep]
 *
 * 1. Type-checks the fixture site (fixture-site/tsconfig.json).
 * 2. Runs the full gate through tests/run-local-suite.sh on the CLEAN fixture
 *    (fixture-site/tests/suite.env): it must exit 0 with no failed, flaky or
 *    skipped test.
 * 3. Runs it again on the VIOLATIONS scenario (suite.violations.env: the
 *    kit_fixture_violations database plus the robots/llms overlays): it must
 *    exit 1, and every kit-compliance rule must fail on its own seeded
 *    violation (EXPECTED_FAILURES below, matched in the failure messages),
 *    while the engine's detector self-tests still pass.
 * 4. Leaves the host as it found it: when the fixture Postgres was not
 *    running before, it is removed with its volume (--keep leaves it up).
 *    The removal holds the fixture project's gate lock, so it never pulls the
 *    database from under another run of the fixture gate; when that lock
 *    stays busy, the database is left up and the run says so.
 *
 * Both runs build the fixture's production image (fixture-site/Dockerfile),
 * start it on the fixture's own ports (fixture-site/tests/suite.env: app
 * :10032, Postgres :5434), hold the host's gate locks (SUITE_HOST_LOCKS: the
 * registry sites' gate projects, their lock files created when missing, plus
 * '*' for any other lock present) and run niced with a CPU cap, exactly as a
 * site's deploy gate does. The
 * project, container and compose names come from the runner's --print-config.
 * Needs docker, flock, node 22, python3 and the kit's node_modules (npm ci).
 * Exit: 0 the gate proved itself, 1 it did not, 2 bad arguments or a missing
 * tool.
 */
import { spawnSync, spawn } from 'node:child_process';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const KIT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const FIXTURE = join(KIT, 'fixture-site');
const SHIM = join(FIXTURE, 'tests', 'run-local-suite.sh');
/** How long the teardown waits for another fixture gate run to finish before leaving the database up. */
const TEARDOWN_LOCK_WAIT_S = 900;

/** Each kit-compliance rule, and what its failure must say on the violations scenario. */
const EXPECTED_FAILURES = [
  ['kit-compliance: affiliate links carry', ['/notes/foreign-tag-note', 'tag "someoneelse-20" is not one of the site\'s tracking IDs', 'lacks sponsored, nofollow, noopener']],
  ['kit-compliance: pages with affiliate links show the earnings disclosure', ['/notes/hidden-disclosure-note: 1 visible affiliate link(s) but the earnings disclosure']],
  ['kit-compliance: prices and availability show only in a dated stamp', [
    '/notes/stale-stamp-note', 'older than 24h', 'without the exact 2(i) disclaimer', '/notes/unstamped-price-note: price outside a dated stamp',
    '/notes/whole-dollar-price-note: price outside a dated stamp: "The Northwind PW-500 sells for $349 right now',
    '/notes/whole-dollar-price-note: price in og:title', '/notes/whole-dollar-price-note: price meta tag product:price:amount',
    '/notes/availability-claim-note: availability outside a dated stamp',
    '/notes/hidden-disclaimer-note price stamp 1', '(it is in the stamp but hidden)',
    '/notes/edge-stamp-note price stamp 1', 'quotable window',
  ]],
  ['kit-compliance: pages that show Amazon content carry the exact 2(k) notice', [
    '/notes/amazon-title-note: shows Amazon content (the Amazon title',
    '/notes/truncated-title-note: shows Amazon content (the Amazon title "Contoso Volt 1000 Widget with Four AC..." (truncated))',
  ]],
  ['kit-compliance: every Offer has validFrom within 24h', ['/notes/stale-offer-note', 'h old (older than 24h)', 'no visible dated price', '/notes/raw-jsonld-note: JSON-LD carries an AggregateRating']],
  ['kit-compliance: no price alerts, price tracking or "lowest ever"', [
    '/notes/price-alert-note', 'lowest ever copy', 'price-tracking UI (form action /api/price-alert',
    '/notes/deal-idiom-note: price alert copy: "Never miss a deal', '/notes/deal-idiom-note: lowest ever copy: "Don\'t miss out',
    '/notes/deal-idiom-note: all-time low copy: "No other store', '/notes/deal-idiom-note: price-drop notification copy: "Get notified',
    '/notes/deal-idiom-note: price tracking copy: "Track this price',
  ]],
  ['kit-compliance: GFM tables render as table.md-table', ['/guides/broken-table-guide: unrendered Markdown table text']],
  ['kit-compliance: no Amazon CDN image', [
    '/notes/amazon-og-note: og:image on an Amazon host', 'editorial_articles amazon-hero-guide (published): hero_image_url on an Amazon host',
    '/notes/raw-jsonld-note: Product JSON-LD image on an Amazon host', 'editorial_articles amazon-body-image-guide (published): body_md stores an Amazon image link',
  ]],
  ['kit-compliance: a recalled SKU has no buy button anywhere', [
    '/guides/recalled-pick-guide: buy button for recalled B0GATE0007',
    '/guides/recalled-redirect-guide: buy button for recalled B0GATE0007: https://gate-fixture.test/r/B0GATE0007',
    '/notes/recall-search-note: Amazon search for recalled B0GATE0007 ("Tailspin Spark 800")',
    '/notes/recall-search-note: buy button for recalled B0GATE0007: https://www.amazon.com/gp/aws/cart/add.html',
  ]],
  ['kit-compliance: robots.txt lets search engines and assistants', ['Claude-User (its own group) is blocked from /', 'training crawler GPTBot is not blocked']],
  ['kit-compliance: llms.txt is present', ['quotes a price']],
  ['kit-compliance: rendered pages pass the content policy scan', ['/guides/unsafe-advice-guide: [kit-fixture.fixture-backfeed]']],
];

/** Tests that judge no fixture data: they must pass in both scenarios. */
const MUST_PASS = [
  'kit-compliance detectors:',
  'visible-code detector:',
  'legibility check:',
  'dead-end detector:',
  'every page route in the route table belongs to a page family',
];

function parseArgs(argv) {
  const out = { out: '', keep: false };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--keep') out.keep = true;
    else if (a === '--out') out.out = argv[++i] ?? '';
    else if (a.startsWith('--out=')) out.out = a.slice(6);
    else if (a === '-h' || a === '--help') { process.stdout.write(readFileSync(fileURLToPath(import.meta.url), 'utf8').split('*/')[0]); process.exit(0); }
    else { process.stderr.write(`gate-selftest: unknown argument ${a}\n`); process.exit(2); }
  }
  return out;
}

function log(msg) {
  process.stdout.write(`[selftest] ${msg}\n`);
}

function need(cmd, args) {
  const r = spawnSync(cmd, args, { encoding: 'utf8' });
  if (r.status !== 0) {
    process.stderr.write(`gate-selftest: ${cmd} is not usable (${(r.stderr || r.error || '').toString().trim().split('\n')[0]})\n`);
    process.exit(2);
  }
}

const stripAnsi = (s) => String(s ?? '').replace(/\x1b\[[0-9;]*m/g, '');

/** Every test of a Playwright JSON report: title, project, final status, all error text. */
function testsOf(report) {
  const out = [];
  const walk = (suite) => {
    for (const spec of suite.specs ?? []) {
      for (const t of spec.tests ?? []) {
        const results = t.results ?? [];
        const last = results[results.length - 1];
        out.push({
          title: spec.title,
          project: t.projectName,
          status: t.status, // expected | unexpected | flaky | skipped
          final: last?.status ?? 'none',
          errors: results.flatMap((r) => [r.error?.message, ...(r.errors ?? []).map((e) => e.message)]).map(stripAnsi).join('\n'),
        });
      }
    }
    for (const child of suite.suites ?? []) walk(child);
  };
  for (const s of report.suites ?? []) walk(s);
  return out;
}

/** The runner's resolved settings for a suite.env (--print-config): project, site root, compose files, containers, lock dir. */
function suiteConfig(suiteEnv) {
  const r = spawnSync('bash', [SHIM, '--print-config'], {
    encoding: 'utf8',
    cwd: join(FIXTURE, 'tests'),
    env: { ...process.env, ...(suiteEnv ? { SUITE_ENV: suiteEnv } : {}) },
  });
  if (r.status !== 0) throw new Error(`run-local-suite.sh --print-config exited ${r.status}: ${(r.stderr || r.stdout || '').trim()}`);
  const cfg = {};
  for (const line of r.stdout.split('\n')) {
    const at = line.indexOf('=');
    if (at > 0) cfg[line.slice(0, at)] = line.slice(at + 1);
  }
  for (const k of ['project', 'site_root', 'compose_files', 'db_container', 'lock_dir']) {
    if (!cfg[k]) throw new Error(`run-local-suite.sh --print-config printed no ${k}`);
  }
  return cfg;
}

/** Run `cmd args` holding the project's gate lock (flock -w); null when the lock stayed busy. */
function underGateLock(cfg, waitS, cmd, args, opts = {}) {
  const lock = join(cfg.lock_dir, `local-suite-${cfg.project}.lock`);
  mkdirSync(cfg.lock_dir, { recursive: true });
  // flock exits 1 when -w runs out; -E gives that case its own code so a command's own 1 is not mistaken for it.
  const r = spawnSync('flock', ['-E', '75', '-w', String(waitS), lock, cmd, ...args], { encoding: 'utf8', ...opts });
  return r.status === 75 ? null : r;
}

function runGate(name, outDir, suiteEnv) {
  const art = join(outDir, name);
  mkdirSync(art, { recursive: true });
  const env = {
    ...process.env,
    GATE_ARTIFACT_DIR: art,
    PLAYWRIGHT_JSON_OUTPUT_FILE: join(art, 'report.json'),
    FORCE_COLOR: '0',
    ...(suiteEnv ? { SUITE_ENV: suiteEnv } : {}),
  };
  log(`${name}: running the gate (output in ${art}/output.log)`);
  return new Promise((resolveRun) => {
    const child = spawn('bash', [SHIM, '--reporter=line,json', '--output', join(art, 'test-results')], { env, cwd: join(FIXTURE, 'tests') });
    let tail = '';
    const keep = (d) => { tail = (tail + d.toString()).slice(-200_000); };
    child.stdout.on('data', keep);
    child.stderr.on('data', keep);
    child.on('close', (code) => {
      writeFileSync(join(art, 'output.log'), tail);
      const reportFile = join(art, 'report.json');
      const report = existsSync(reportFile) ? JSON.parse(readFileSync(reportFile, 'utf8')) : null;
      resolveRun({ code, report, tail });
    });
  });
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  need('docker', ['version', '--format', '{{.Server.Version}}']);
  need('python3', ['--version']);
  need('flock', ['--version']);
  if (!existsSync(join(KIT, 'node_modules', '.bin', 'playwright'))) {
    process.stderr.write(`gate-selftest: run 'npm ci' in ${KIT} first\n`);
    process.exit(2);
  }
  const outDir = args.out ? resolve(args.out) : mkdtempSync(join(tmpdir(), 'kit-gate-selftest-'));
  mkdirSync(outDir, { recursive: true });
  const problems = [];

  log('type-checking the fixture site');
  const tsc = spawnSync(join(KIT, 'node_modules', '.bin', 'tsc'), ['-p', join(FIXTURE, 'tsconfig.json')], { encoding: 'utf8', cwd: KIT });
  if (tsc.status !== 0) problems.push(`fixture-site does not type-check:\n${tsc.stdout}${tsc.stderr}`);

  const cfg = suiteConfig();
  // Probed under the gate lock: a fixture gate run in progress has its database up, and it is not ours to remove.
  const probe = underGateLock(cfg, TEARDOWN_LOCK_WAIT_S, 'docker', ['inspect', '-f', '{{.State.Running}}', cfg.db_container]);
  const dbWasRunning = probe === null || probe.stdout.trim() === 'true';

  try {
    // ── clean: green, nothing flaky, nothing skipped ──
    const clean = await runGate('clean', outDir);
    if (clean.code !== 0) problems.push(`clean fixture: the gate exited ${clean.code}, expected 0\n${clean.tail.slice(-3000)}`);
    if (!clean.report) problems.push('clean fixture: no JSON report');
    else {
      const tests = testsOf(clean.report);
      for (const t of tests.filter((x) => x.status !== 'expected')) {
        problems.push(`clean fixture: [${t.project}] ${t.title} was ${t.status}${t.errors ? `\n    ${t.errors.slice(0, 1500).replace(/\n/g, '\n    ')}` : ''}`);
      }
      log(`clean: ${tests.length} tests, ${tests.filter((x) => x.status === 'expected').length} passed`);
    }

    // ── violations: red, for the right reasons ──
    const viol = await runGate('violations', outDir, join(FIXTURE, 'tests', 'suite.violations.env'));
    if (viol.code !== 1) problems.push(`violations: the gate exited ${viol.code}, expected 1 (tests failed)\n${viol.tail.slice(-3000)}`);
    if (!viol.report) problems.push('violations: no JSON report');
    else {
      const tests = testsOf(viol.report);
      for (const [prefix, needles] of EXPECTED_FAILURES) {
        const mine = tests.filter((t) => t.title.startsWith(prefix));
        if (!mine.length) { problems.push(`violations: no test titled "${prefix}..." ran`); continue; }
        const failed = mine.filter((t) => t.status === 'unexpected');
        if (!failed.length) { problems.push(`violations: "${prefix}..." passed, but its seeded violation must fail it`); continue; }
        const text = failed.map((t) => t.errors).join('\n');
        for (const n of needles) if (!text.includes(n)) problems.push(`violations: "${prefix}..." failed without naming "${n}"`);
        log(`violations: ${prefix}... failed as expected (${failed.length}/${mine.length} shard(s))`);
      }
      for (const prefix of MUST_PASS) {
        for (const t of tests.filter((x) => x.title.startsWith(prefix))) {
          if (t.status !== 'expected') problems.push(`violations: the data-independent test "${t.title}" was ${t.status}`);
        }
      }
    }
  } finally {
    if (!dbWasRunning && !args.keep) {
      log('removing the fixture Postgres this run started (and its volume), holding the fixture gate lock');
      const first = cfg.compose_files.split(/\s+/).filter(Boolean)[0] ?? 'docker-compose.yml';
      const down = underGateLock(cfg, TEARDOWN_LOCK_WAIT_S, 'docker', [
        'compose', '-p', cfg.project, '--project-directory', cfg.site_root, '-f', join(cfg.site_root, first), 'down', '-v',
      ], { stdio: 'ignore' });
      if (down === null) log(`another fixture gate run held local-suite-${cfg.project}.lock for ${TEARDOWN_LOCK_WAIT_S}s: leaving ${cfg.db_container} up`);
    }
  }

  if (problems.length) {
    process.stdout.write(`\n[selftest] FAILED: the gate did not prove itself (${problems.length} problem(s)):\n  ${problems.join('\n  ')}\n`);
    process.exit(1);
  }
  log(`PASSED: green on the clean fixture, red on every seeded violation (reports in ${outDir})`);
}

main().catch((e) => {
  process.stderr.write(`gate-selftest: ${e?.stack ?? e}\n`);
  process.exit(1);
});
