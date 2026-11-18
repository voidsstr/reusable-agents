/**
 * The kit's public surface and metadata: one version everywhere, exact
 * dependency pins, the scripts other tooling calls, a barrel that exposes the
 * leaf modules, no site names in anything the kit ships, and browser code
 * that does not lean on Node globals.
 */
import * as fs from 'fs';
import * as path from 'path';
import * as ts from 'typescript';
import * as kit from '../server/src';

const KIT = path.resolve(__dirname, '..');
const pkg = JSON.parse(fs.readFileSync(path.join(KIT, 'package.json'), 'utf8')) as {
  version: string;
  private?: boolean;
  scripts: Record<string, string>;
  dependencies: Record<string, string>;
  devDependencies: Record<string, string>;
  engines?: { node?: string };
};

describe('kit version', () => {
  it('is the same in VERSION, package.json and KIT_VERSION, and the changelog has it', () => {
    const version = fs.readFileSync(path.join(KIT, 'VERSION'), 'utf8').trim();
    expect(version).toMatch(/^\d+\.\d+\.\d+$/);
    expect(pkg.version).toBe(version);
    expect(kit.KIT_VERSION).toBe(version);
    expect(fs.readFileSync(path.join(KIT, 'CHANGELOG.md'), 'utf8')).toContain(`## ${version}`);
  });
});

describe('package.json', () => {
  it('pins every dependency to an exact version', () => {
    const all = { ...pkg.dependencies, ...pkg.devDependencies };
    const loose = Object.entries(all).filter(([, v]) => !/^\d+\.\d+\.\d+$/.test(v)).map(([k, v]) => `${k}@${v}`);
    expect(loose).toEqual([]);
  });

  it('pre-declares what the kit items need', () => {
    expect(Object.keys(pkg.dependencies).sort()).toEqual(['express', 'pg']);
    expect(pkg.dependencies.express).toMatch(/^4\./);
    for (const dev of ['typescript', 'jest', 'ts-jest', '@types/jest', '@types/node', '@types/express', '@types/pg',
      'supertest', '@types/supertest', 'esbuild', '@playwright/test']) {
      expect(pkg.devDependencies[dev]).toBeDefined();
    }
  });

  it('exposes test, typecheck, build and build:client, is private and needs node 22', () => {
    expect(pkg.scripts.test).toBe('jest');
    expect(pkg.scripts.typecheck).toContain('tsc -p tsconfig.json --noEmit');
    expect(pkg.scripts.build).toBe('tsc -p tsconfig.build.json');
    expect(pkg.scripts['build:client']).toBe('node scripts/build-client.mjs');
    expect(pkg.private).toBe(true);
    expect(pkg.engines?.node).toBe('>=22');
  });

  it('has a lockfile that matches the pins', () => {
    const lock = JSON.parse(fs.readFileSync(path.join(KIT, 'package-lock.json'), 'utf8'));
    const root = lock.packages[''];
    expect(root.dependencies).toEqual(pkg.dependencies);
    expect(root.devDependencies).toEqual(pkg.devDependencies);
    for (const [name, v] of Object.entries({ ...pkg.dependencies, ...pkg.devDependencies })) {
      expect(lock.packages[`node_modules/${name}`]?.version).toBe(v);
    }
  });
});

describe('barrel', () => {
  it('exports the leaf modules', () => {
    for (const name of [
      'loadSiteContext', 'parseSiteContext', 'SiteContextError', 'SITE_SCHEMA', 'htmlCacheControl', 'themeCss',
      'quotablePrice', 'affiliateOfferLd', 'honestListPrice', 'productListItemLd',
      'cta', 'amazonSearchCta', 'amazonUrl', 'detailLink', 'esc', 'classifySource', 'pickTrackingId', 'tagForPlacement',
      'priceStamp', 'availabilityStamp', 'priceLabel', 'priceHtml', 'PRICE_STAMP_CSS',
      'AMAZON_CONTENT_NOTICE', 'priceAvailabilityDisclaimer', 'amazonContentNoticeHtml', 'earningsDisclosureHtml',
      'classifyClick', 'deviceType', 'countryFromHeaders', 'uaLooksLikeBot',
      'classifyAiTraffic', 'clientIp', 'createAiCrawlerVerifier', 'AiCrawlerVerifier', 'KIT_DEFAULT_STATIC_RANGES',
      'siteClientIp', 'edgeProxyMatcher', 'trustedProxyMatcher', 'CDN_EDGE_RANGES', 'cidrPolicyIssue',
      'MIN_STATIC_RANGE_PREFIX', 'MIN_TRUSTED_PROXY_PREFIX', 'EDGE_CDN_CHOICES',
      'ownTrackingId', 'parsePriceAmount',
      'boundedQuery', 'withBudget', 'SwrCache', 'verifiedClicks30dByAsin',
      'inactiveReason', 'inactivePageRenders', 'amazonSearchUrlFor',
      'SubstituteMemo', 'rememberSubstitute', 'recallSubstitute',
    ]) {
      expect((kit as Record<string, unknown>)[name]).toBeDefined();
    }
  });
});

// ─── no site names in anything the kit ships ───────────────────────────────

const SITE_NAME_RE = /specpicks|aisleprompt|runtimepicks/i;
/**
 * Not scanned: installed or generated trees; fixtures (sample data); unit/
 * (tests may name the upstream they check, e.g. the drift tests); docs and
 * PORTED_FROM.json, whose job is to cite provenance.
 */
const SKIP_DIRS = new Set(['node_modules', 'dist', 'coverage', 'fixtures', 'unit', 'test-results', 'playwright-report', 'blob-report', '.git']);
const SKIP_FILES = new Set(['PORTED_FROM.json', 'package-lock.json']);
const BINARY_EXT = /\.(png|jpe?g|gif|webp|avif|ico|svgz|woff2?|ttf|otf|eot|gz|tgz|zip|br|pdf|wasm)$/i;

type CommentStyle = 'c' | 'css' | 'sql' | 'hash' | 'html' | 'none';

/** How comments are written in a file, by name (a trailing .tmpl is looked through). */
function commentStyle(name: string): CommentStyle {
  const base = name.replace(/\.tmpl$/, '');
  if (/\.(ts|tsx|mts|cts|js|jsx|mjs|cjs)$/.test(base)) return 'c';
  if (/\.css$/.test(base)) return 'css';
  if (/\.sql$/.test(base)) return 'sql';
  if (/\.(sh|bash|ya?ml|toml|ini|conf|env)$/.test(base) || /^Dockerfile/.test(base) || /^\.env/.test(base) || /^\.(git|docker)ignore$/.test(base)) return 'hash';
  if (/\.html?$/.test(base)) return 'html';
  return 'none';
}

/** The text with comments removed (comments may cite provenance; code may not name a site). */
function stripComments(text: string, style: CommentStyle): string {
  switch (style) {
    // A '//' after ':' is a URL scheme, not a comment.
    case 'c': return text.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:\\])\/\/.*$/gm, '$1');
    case 'css': return text.replace(/\/\*[\s\S]*?\*\//g, '');
    case 'sql': return text.replace(/\/\*[\s\S]*?\*\//g, '').replace(/--.*$/gm, '');
    case 'hash': return text.replace(/(^|\s)#.*$/gm, '$1');
    case 'html': return text.replace(/<!--[\s\S]*?-->/g, '');
    default: return text;
  }
}

/** Every file the kit ships, relative to the kit root. */
function shippedFiles(dir = KIT, out: string[] = []): string[] {
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) {
      if (!SKIP_DIRS.has(e.name)) shippedFiles(p, out);
    } else if (e.isFile() && !SKIP_FILES.has(e.name) && !/\.md$/i.test(e.name) && !BINARY_EXT.test(e.name)) {
      out.push(path.relative(KIT, p));
    }
  }
  return out;
}

describe('framework-first: no site literals in anything the kit ships', () => {
  it('no shipped file names a specific site outside comments', () => {
    const files = shippedFiles();
    expect(files).toEqual(expect.arrayContaining(['server/src/index.ts', 'schema/site.schema.json', 'test.sh']));
    const offenders = files.filter((f) => {
      const code = stripComments(fs.readFileSync(path.join(KIT, f), 'utf8'), commentStyle(path.basename(f)));
      return SITE_NAME_RE.test(code);
    });
    expect(offenders).toEqual([]);
  });

  it('strips comments per language, so only code (and strings) count', () => {
    const cases: Array<[string, string, boolean]> = [
      ['x.ts', "// ported from SpecPicks\nconst a = 1; /* SpecPicks */", false],
      ['x.ts', "const site = 'specpicks';", true],
      ['x.ts', "const u = 'https://example.com/aisleprompt';", true],
      ['run-local-suite.sh', '# SpecPicks lock path\nLOCK=/tmp/x.lock', false],
      ['run-local-suite.sh', 'PROJECT=specpicks # default', true],
      ['kit_0001.sql', '-- columns as in SpecPicks\nCREATE TABLE t (id int);', false],
      ['publish_trigger.sql.tmpl', "CREATE ROLE runtimepicks_publisher;", true],
      ['Dockerfile.azure.tmpl', '# from SpecPicks\nFROM node:22-slim', false],
      ['base.css', '/* SpecPicks palette */ .a{color:red}', false],
      ['page.html', '<!-- SpecPicks --><p>x</p>', false],
      ['VERSION', 'specpicks', true],
    ];
    for (const [name, text, hit] of cases) {
      expect([name, SITE_NAME_RE.test(stripComments(text, commentStyle(name)))]).toEqual([name, hit]);
    }
  });
});

// ─── browser code ──────────────────────────────────────────────────────────

/** client/src TypeScript files (K2's browser bundle sources); [] while there are none. */
function clientSources(dir = path.join(KIT, 'client', 'src'), out: string[] = []): string[] {
  if (!fs.existsSync(dir)) return out;
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) clientSources(p, out);
    else if (/\.ts$/.test(e.name) && !/\.test\.ts$/.test(e.name)) out.push(p);
  }
  return out;
}

describe('browser code (client/src)', () => {
  const files = clientSources();

  // tsconfig.json type-checks client/src with Node's typings (the unit tests
  // need them), so `process.env`, `Buffer` or `require` would pass there and
  // then throw in the esbuild browser bundle. This re-checks client/src with
  // DOM typings only. Client files import shared constants from server/src,
  // whose type-only imports reach Node-typed modules (fs, net): those files
  // report errors in this program by design, so only diagnostics located in
  // client/src count. (TypeScript's `types` cannot be scoped per file, which
  // is why this is a test rather than a tsconfig of its own.)
  (files.length ? it : it.skip)('type-checks with DOM typings only (no Node globals)', () => {
    const parsed = ts.getParsedCommandLineOfConfigFile(path.join(KIT, 'tsconfig.json'), {}, {
      ...ts.sys,
      onUnRecoverableConfigFileDiagnostic: (d) => { throw new Error(ts.flattenDiagnosticMessageText(d.messageText, '\n')); },
    });
    if (!parsed) throw new Error('tsconfig.json did not parse');
    const options: ts.CompilerOptions = {
      ...parsed.options,
      types: [],
      lib: ['lib.es2022.d.ts', 'lib.dom.d.ts', 'lib.dom.iterable.d.ts'],
      noEmit: true,
    };
    const program = ts.createProgram(files, options);
    const clientDir = path.join(KIT, 'client', 'src') + path.sep;
    const inClient = ts.getPreEmitDiagnostics(program)
      .filter((d) => d.file && path.resolve(d.file.fileName).startsWith(clientDir))
      .map((d) => {
        const { line, character } = (d.file as ts.SourceFile).getLineAndCharacterOfPosition(d.start ?? 0);
        return `${path.relative(KIT, (d.file as ts.SourceFile).fileName)}(${line + 1},${character + 1}): ${ts.flattenDiagnosticMessageText(d.messageText, ' ')}`;
      });
    expect(inClient).toEqual([]);
  }, 60_000);
});
