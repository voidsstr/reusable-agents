/**
 * The gate engine's pure parts: the gate-config validator (and the fixture
 * site's config passing it), seeded sampling, route patterns, the per-family
 * CTA check, the GFM-table detector behind kit-compliance's "this page must
 * render a table", and the site/host mapping.
 */
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { mdToHtml } from '../../server/src/render/md';
import { parseSiteContext } from '../../server/src/siteContext';
import { config as fixtureConfig } from '../../fixture-site/tests/ui/gate.config';
import { pageRoutes, ROUTES } from '../../fixture-site/src/routes';
import { markdownHasTable } from '../ui/lib/db';
import { GateConfigError, gateConfigProblems, loadGateConfig } from '../ui/lib/gateConfig';
import type { GateConfig, PageFamily } from '../ui/lib/manifestTypes';
import { ctaProblems, routeRegex } from '../ui/lib/routes';
import { gateSite, hashString, resetGateSite, seededSample, sitePath } from '../ui/lib/site';
import { fixed, union } from '../ui/lib/sources';
import { NO_TARGET_URL, gateTargetUrl } from '../ui/lib/target';
import { kitPlaywrightConfig, webServerUrl } from '../playwright.base';

const KIT = path.resolve(__dirname, '..', '..');

function family(over: Partial<PageFamily> = {}): PageFamily {
  return { id: 'fam', routes: ['/x/:id'], sourceLabel: 'fixed', source: fixed('/x/1'), sample: 2, robots: 'index', monetized: false, ...over };
}

/** The compliance sources every config needs (or their waivers). */
const COMPLIANCE_OK = { recalledProducts: async () => [], contentPolicy: { ruleSets: ['kit-fixture'] } };

describe('gateConfigProblems', () => {
  it('accepts the fixture site\'s config', () => {
    expect(gateConfigProblems(fixtureConfig)).toEqual([]);
  });

  it('lists every problem at once', () => {
    const bad = {
      families: [
        family({ id: 'Bad Id' }),
        family({ id: 'dup' }),
        family({ id: 'dup', sample: 0 }),
        family({ id: 'shards', shards: 5 }),
        family({ id: 'robots', robots: 'maybe' as never }),
        family({ id: 'money', monetized: { most: '' } }),
        family({ id: 'exc', exceptions: { 'no-such-check': 'x', h1: ' ' } as never }),
        { ...family({ id: 'nosource' }), source: undefined as never, sourceLabel: '' },
      ],
      routes: { declared: 'nope' },
      compliance: { recalledProducts: async () => [], contentPolicy: { ruleSets: ['ok', 'not ok'] } },
    };
    const out = gateConfigProblems(bad).join('\n');
    for (const want of [
      /families\[0\] \(Bad Id\): id must be kebab-case/,
      /families\[2\] \(dup\): duplicate id/,
      /families\[2\] \(dup\): sample must be a positive integer/,
      /families\[3\] \(shards\): shards must be 1\.\.sample/,
      /families\[4\] \(robots\): robots must be 'index' or 'noindex'/,
      /families\[5\] \(money\): monetized must be a boolean or \{ most: reason \}/,
      /exception for unknown check 'no-such-check'/,
      /exception 'h1' needs a reason/,
      /families\[7\] \(nosource\): source must be a function/,
      /sourceLabel is required/,
      /routes\.declared must be a function/,
      /contentPolicy\.ruleSets must be a list of rule-set names/,
    ]) {
      expect(out).toMatch(want);
    }
    expect(gateConfigProblems(null)).toEqual(['the module exports no config object']);
    expect(gateConfigProblems({ families: [] })).toEqual(['families: at least one page family is required']);
  });

  it('never lets the recall or content gate go unconfigured: a source, or a waiver that says why (review round 1)', () => {
    const base = { families: [family()] };
    expect(gateConfigProblems({ ...base, compliance: COMPLIANCE_OK })).toEqual([]);
    const none = gateConfigProblems(base).join('\n');
    expect(none).toMatch(/compliance\.recalledProducts is required \(the recall gate is never cut\)/);
    expect(none).toMatch(/compliance\.contentPolicy\.ruleSets is required \(the content gate is never cut\)/);
    expect(gateConfigProblems({ ...base, compliance: { recalledProducts: async () => [], contentPolicy: { ruleSets: [] } } }).join('\n'))
      .toMatch(/contentPolicy\.ruleSets is required/);
    const waived = { recallsWaived: 'the scaffold has no product pages yet', contentPolicyWaived: 'no rule set fits this niche yet' };
    expect(gateConfigProblems({ ...base, compliance: waived })).toEqual([]);
    expect(gateConfigProblems({ ...base, compliance: { recallsWaived: ' ', contentPolicyWaived: '' } }).join('\n')).toMatch(/recallsWaived must be the reason, in words[\s\S]*contentPolicyWaived must be the reason/);
    expect(gateConfigProblems({ ...base, compliance: { ...COMPLIANCE_OK, ...waived } }).join('\n')).toMatch(/set recalledProducts or recallsWaived, not both[\s\S]*set contentPolicy or contentPolicyWaived, not both/);
    expect(gateConfigProblems({ ...base, compliance: { ...COMPLIANCE_OK, recalledProducts: ['B0GATE0007'] } }).join('\n')).toMatch(/recalledProducts must be a function/);
  });

  it('loads a config module from KIT_GATE_CONFIG and refuses a broken one with every problem', () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'kit-gatecfg-'));
    const file = path.join(dir, 'gate.config.js');
    fs.writeFileSync(file, "module.exports = { config: { families: [{ id: 'x', routes: [], sourceLabel: 'l', source: () => [], sample: 0, robots: 'index', monetized: true }] } };\n");
    const prev = process.env.KIT_GATE_CONFIG;
    try {
      delete process.env.KIT_GATE_CONFIG;
      expect(() => loadGateConfig()).toThrow(GateConfigError);
      process.env.KIT_GATE_CONFIG = file;
      expect(() => loadGateConfig()).toThrow(/sample must be a positive integer/);
    } finally {
      if (prev === undefined) delete process.env.KIT_GATE_CONFIG; else process.env.KIT_GATE_CONFIG = prev;
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });
});

describe('the fixture site\'s families cover its route table', () => {
  it('every page route has a family, and every non-page route a reason', () => {
    const covered = new Set(fixtureConfig.families.flatMap((f) => f.routes));
    expect(pageRoutes().filter((r) => !covered.has(r))).toEqual([]);
    for (const r of ROUTES) if (r.kind !== 'page') expect(r.reason).toBeTruthy();
  });

  it('declares the families the kit gate expects of a site', () => {
    const cfg: GateConfig = fixtureConfig;
    expect(cfg.compliance?.recalledProducts).toBeDefined();
    expect(cfg.compliance?.contentPolicy?.ruleSets).toEqual(['kit-fixture']);
    expect(fs.existsSync(cfg.compliance?.contentPolicy?.configFile as string)).toBe(true);
    expect(cfg.families.some((f) => f.status === 404)).toBe(true);
  });
});

describe('seededSample', () => {
  const items = Array.from({ length: 50 }, (_, i) => `/p/${i}`);

  it('is deterministic for a seed and a key, and differs across keys and seeds', () => {
    expect(seededSample(items, 5, 'k', undefined, 's1')).toEqual(seededSample(items, 5, 'k', undefined, 's1'));
    expect(seededSample(items, 5, 'k', undefined, 's1')).not.toEqual(seededSample(items, 5, 'other', undefined, 's1'));
    expect(seededSample(items, 5, 'k', undefined, 's1')).not.toEqual(seededSample(items, 5, 'k', undefined, 's2'));
  });

  it('holds still when other rows come and go, and dedupes', () => {
    const before = seededSample(items, 5, 'k', undefined, 's1');
    const more = [...items, '/p/new-1', '/p/new-2', ...items.slice(0, 3)];
    const after = seededSample(more, 5, 'k', undefined, 's1');
    expect(after.filter((x) => before.includes(x)).length).toBeGreaterThanOrEqual(3);
    expect(new Set(after).size).toBe(after.length);
    expect(seededSample(items, 0, 'k')).toEqual([]);
    expect(hashString('a')).not.toBe(hashString('b'));
  });

  it('union() merges sources in order without duplicates', async () => {
    expect(await union(fixed('/a', '/b'), fixed('/b', '/c'))({ request: {} as never })).toEqual(['/a', '/b', '/c']);
  });
});

describe('routeRegex', () => {
  it.each([
    ['/product/:asin', '/product/B0GATE0001', true],
    ['/product/:asin', '/product/B0GATE0001/x', false],
    ['/guides', '/guides', true],
    ['/:file.xml', '/sitemap-hub.xml', true],
    ['/:file.xml', '/sitemapxhubxml', false],
    ['/a+b', '/a+b', true],
    ['*', '/anything', false],
  ])('%s matches %s: %s', (route, p, want) => {
    expect(routeRegex(route).test(p)).toBe(want);
  });
});

describe('ctaProblems (the per-family CTA check)', () => {
  const tags = ['site-20'];
  it('passes a compliant Amazon CTA and a /r/ link', () => {
    expect(ctaProblems({ kind: 'amazon', href: 'https://www.amazon.com/dp/B0GATE0001?tag=site-20', rel: 'sponsored nofollow noopener', target: '_blank' }, tags)).toEqual([]);
    expect(ctaProblems({ kind: 'click', href: '/r/B0GATE0001', rel: 'sponsored nofollow', target: '' }, tags)).toEqual([]);
  });
  it('flags rel, tag and target', () => {
    const out = ctaProblems({ kind: 'amazon', href: 'https://www.amazon.com/dp/B0GATE0001?tag=x-20', rel: 'nofollow', target: '' }, tags).join('\n');
    expect(out).toMatch(/lacks sponsored, noopener/);
    expect(out).toMatch(/affiliate tag "x-20" is not one of the site's/);
    expect(out).toMatch(/target=""/);
  });
});

describe('markdownHasTable mirrors the kit\'s GFM table rule', () => {
  const site = parseSiteContext(JSON.parse(fs.readFileSync(path.join(KIT, 'fixture-site', 'site.json'), 'utf8')), { env: {} });
  it.each([
    ['| a | b |\n| --- | --- |\n| 1 | 2 |', true],
    ['a | b\n--- | ---\n1 | 2', true],
    ['| a | b |\n\n| --- | --- |\n| 1 | 2 |', false],
    ['```\n| a | b |\n| --- | --- |\n```', false],
    ['Just | a pipe', false],
    ['| a | b |\n|===|===|', false],
  ])('%j -> %s, as mdToHtml renders it', (md, want) => {
    expect(markdownHasTable(md)).toBe(want);
    // Fenced code keeps its pipes as code; everywhere else the detector and the renderer agree.
    if (!md.startsWith('```')) expect(mdToHtml(site, md).includes('class="md-table"')).toBe(want);
  });
});

describe('the gate target (no default port)', () => {
  const keys = ['TEST_URL', 'SUITE_APP_PORT', 'SUITE_APP_HOST', 'KIT_GATE_SITE_JSON', 'KIT_GATE_CONFIG'] as const;
  const saved: Record<string, string | undefined> = {};
  beforeEach(() => { for (const k of keys) { saved[k] = process.env[k]; delete process.env[k]; } });
  afterEach(() => { for (const k of keys) { if (saved[k] === undefined) delete process.env[k]; else process.env[k] = saved[k]; } });

  it('is TEST_URL, else the runner\'s app port, else nothing', () => {
    expect(gateTargetUrl({ TEST_URL: 'https://gate-fixture.test/' })).toBe('https://gate-fixture.test');
    expect(gateTargetUrl({ SUITE_APP_PORT: '10032', SUITE_APP_HOST: '127.0.0.1' })).toBe('http://127.0.0.1:10032');
    expect(gateTargetUrl({ SUITE_APP_PORT: '10032' })).toBe('http://localhost:10032');
    expect(gateTargetUrl({ TEST_PORT: '10011' })).toBe('');
    expect(gateTargetUrl({})).toBe('');
    expect(new URL(NO_TARGET_URL).hostname.endsWith('.invalid')).toBe(true);
  });

  it('kitPlaywrightConfig refuses to run with no target, and otherwise hands every worker the same TEST_URL', () => {
    const opts = { dir: path.join(KIT, 'fixture-site', 'tests'), siteJson: '../site.json', gateConfig: './ui/gate.config.ts' };
    expect(() => kitPlaywrightConfig(opts)).toThrow(/no server to test\. Run the gate through tests\/run-local-suite\.sh/);
    process.env.SUITE_APP_PORT = '10032';
    process.env.SUITE_APP_HOST = '127.0.0.1';
    const cfg = kitPlaywrightConfig(opts);
    expect(process.env.TEST_URL).toBe('http://127.0.0.1:10032');
    expect(cfg.use?.baseURL).toBe('http://127.0.0.1:10032');
    delete process.env.TEST_URL;
    delete process.env.SUITE_APP_PORT;
    const viaServer = kitPlaywrightConfig({ ...opts, overrides: { webServer: { command: 'node server.js', url: 'http://localhost:4100/health' } } });
    expect(viaServer.use?.baseURL).toBe('http://localhost:4100');
    expect(webServerUrl({ command: 'x', port: 4200 })).toBe('http://localhost:4200');
    expect(webServerUrl(undefined)).toBe('');
  });
});

describe('gateSite', () => {
  const prev = { json: process.env.KIT_GATE_SITE_JSON, extra: process.env.KIT_GATE_EXTRA_SITE_HOSTS };
  afterEach(() => {
    resetGateSite();
    if (prev.json === undefined) delete process.env.KIT_GATE_SITE_JSON; else process.env.KIT_GATE_SITE_JSON = prev.json;
    if (prev.extra === undefined) delete process.env.KIT_GATE_EXTRA_SITE_HOSTS; else process.env.KIT_GATE_EXTRA_SITE_HOSTS = prev.extra;
  });

  it('maps the live hosts and the server under test onto site paths, and nothing else', () => {
    process.env.KIT_GATE_SITE_JSON = path.join(KIT, 'fixture-site', 'site.json');
    process.env.KIT_GATE_EXTRA_SITE_HOSTS = 'localhost:8080';
    resetGateSite();
    const s = gateSite();
    expect([...s.prodHosts].sort()).toEqual(['gate-fixture.test', 'www.gate-fixture.test']);
    expect(s.prodOrigin).toBe('https://gate-fixture.test');
    expect(sitePath('https://gate-fixture.test/guides?x=1')).toBe('/guides?x=1');
    expect(sitePath('https://www.gate-fixture.test/a')).toBe('/a');
    expect(sitePath('http://localhost:8080/b')).toBe('/b');
    expect(sitePath('/relative')).toBe('/relative');
    expect(sitePath('https://www.amazon.com/dp/B0GATE0001')).toBeNull();
  });

  it('refuses to run without a site.json', () => {
    delete process.env.KIT_GATE_SITE_JSON;
    resetGateSite();
    expect(() => gateSite()).toThrow(/KIT_GATE_SITE_JSON is not set/);
  });
});
