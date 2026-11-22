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
      'INDEXING_MODES', 'IndexingClosedError', 'indexingClosed', 'assertIndexingOpen',
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
  const CLIENT_DIR = path.join(KIT, 'client', 'src') + path.sep;
  /**
   * The server files the bundle compiles in: the only imports outside
   * client/src that unit/http/hygiene.test.ts allows it. links.ts's type-only
   * import reaches siteContext.ts and aiCrawlerVerify.ts, which the bundle
   * drops.
   */
  const BUNDLED_SERVER_FILES = new Set(['server/src/links.ts', 'server/src/http/clientContract.ts'].map((f) => path.join(KIT, f)));
  const NODE_TYPINGS_RE = /[\\/]node_modules[\\/]@types[\\/]node[\\/]/;

  /**
   * client/src as the browser bundle sees it: DOM typings, the standard
   * library of the bundle's ES2018 target (scripts/build-client.mjs), no
   * @types. `overrides` replaces file texts (absolute path -> text), so the
   * tests below can show what the check catches.
   */
  function browserProgram(overrides: Readonly<Record<string, string>> = {}): ts.Program {
    const parsed = ts.getParsedCommandLineOfConfigFile(path.join(KIT, 'tsconfig.json'), {}, {
      ...ts.sys,
      onUnRecoverableConfigFileDiagnostic: (d) => { throw new Error(ts.flattenDiagnosticMessageText(d.messageText, '\n')); },
    });
    if (!parsed) throw new Error('tsconfig.json did not parse');
    const options: ts.CompilerOptions = {
      ...parsed.options,
      types: [],
      lib: ['lib.es2018.d.ts', 'lib.dom.d.ts', 'lib.dom.iterable.d.ts'],
      noEmit: true,
    };
    const host = ts.createCompilerHost(options, true);
    const readFile = host.readFile.bind(host);
    host.readFile = (f) => overrides[path.resolve(f)] ?? readFile(f);
    return ts.createProgram({ rootNames: files, options, host });
  }

  /**
   * What would break in the bundle: any file of Node's typings in the
   * program (they declare process, Buffer and require for every file), and
   * every error located in client/src, in a bundled server file, or in no
   * file at all. siteContext.ts and aiCrawlerVerify.ts use Node modules (fs,
   * net), so their errors here are expected and do not count.
   */
  function browserIssues(program: ts.Program): { nodeTypings: string[]; errors: string[] } {
    const nodeTypings = program.getSourceFiles().map((sf) => sf.fileName).filter((f) => NODE_TYPINGS_RE.test(f));
    const errors = ts.getPreEmitDiagnostics(program)
      .filter((d) => {
        if (!d.file) return true;
        const f = path.resolve(d.file.fileName);
        return f.startsWith(CLIENT_DIR) || BUNDLED_SERVER_FILES.has(f);
      })
      .map((d) => {
        const text = ts.flattenDiagnosticMessageText(d.messageText, ' ');
        if (!d.file) return text;
        const { line, character } = d.file.getLineAndCharacterOfPosition(d.start ?? 0);
        return `${path.relative(KIT, d.file.fileName)}(${line + 1},${character + 1}): ${text}`;
      });
    return { nodeTypings, errors };
  }

  // tsconfig.json type-checks client/src with Node's typings (the unit tests
  // need them), so `process.env`, `Buffer` or `require` would pass there and
  // then throw in the esbuild browser bundle (or, in a fallback path a real
  // browser rarely takes, be swallowed). This re-checks what the bundle
  // compiles, client/src plus links.ts and clientContract.ts, with DOM
  // typings only. Node's typings must not reach the program at all: one type
  // import from express or pg in any module links.ts reaches would bring
  // Node's globals back for every file, silently. (TypeScript's `types`
  // cannot be scoped per file, and links.ts's type import reaches Node-typed
  // modules, so this is a test rather than a tsconfig of its own.)
  (files.length ? it : it.skip)('type-checks with DOM typings only (no Node globals)', () => {
    expect(browserIssues(browserProgram())).toEqual({ nodeTypings: [], errors: [] });
  }, 60_000);

  (files.length ? it : it.skip)('sees Node globals in the server files the bundle compiles in', () => {
    const contract = path.join(KIT, 'server', 'src', 'http', 'clientContract.ts');
    const links = path.join(KIT, 'server', 'src', 'links.ts');
    const { nodeTypings, errors } = browserIssues(browserProgram({
      [contract]: `${fs.readFileSync(contract, 'utf8')}\nexport const PROBE_GLOBAL = typeof global;\n`,
      [links]: `${fs.readFileSync(links, 'utf8')}\nexport const PROBE_ENV = process.env.PROBE;\n`,
    }));
    expect(nodeTypings).toEqual([]);
    expect(errors).toHaveLength(2);
    expect(errors).toEqual(expect.arrayContaining([
      expect.stringMatching(/^server\/src\/http\/clientContract\.ts\(\d+,\d+\): Cannot find name 'global'/),
      expect.stringMatching(/^server\/src\/links\.ts\(\d+,\d+\): Cannot find name 'process'/),
    ]));
  }, 60_000);

  (files.length ? it : it.skip)('sees Node typings that a type-only import brings into the program', () => {
    // An ordinary edit: a type from express in a module links.ts reaches. It
    // silences every Node-global error, so only the typings check notices.
    const verify = path.join(KIT, 'server', 'src', 'aiCrawlerVerify.ts');
    const beacon = path.join(KIT, 'client', 'src', 'clickBeacon.ts');
    const { nodeTypings, errors } = browserIssues(browserProgram({
      [verify]: `import type { Request } from 'express';\nexport type ProbeRequest = Request;\n${fs.readFileSync(verify, 'utf8')}`,
      [beacon]: `${fs.readFileSync(beacon, 'utf8')}\nexport const probeLater = (f: () => void): unknown => setImmediate(f);\n`,
    }));
    expect(errors).toEqual([]);
    expect(nodeTypings.length).toBeGreaterThan(0);
  }, 60_000);
});
