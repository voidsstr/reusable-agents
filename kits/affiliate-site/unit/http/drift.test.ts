/**
 * Drift between the kit's HTTP layer / browser bundle and the SpecPicks code
 * they were ported from (K2).
 *
 * Same contract as unit/drift.test.ts (K1), for the code K1's PORTED_FROM.json
 * does not cover: the route handlers in SpecPicks src/index.ts (anonymous
 * `app.get(...)` statements, selected by a unique substring), the helpers
 * next to them, the browser click tracker and the sticky-bar height effect.
 * unit/http/fixtures/ported-from-http.json records a sha256 per upstream
 * unit at the port commit; when a SpecPicks checkout is present this test
 * re-hashes them and reports what changed. It fails only with
 * KIT_DRIFT_STRICT=1.
 *
 * Knobs (shared with the K1 test): KIT_DRIFT_UPSTREAM, KIT_DRIFT_REF,
 * KIT_DRIFT_STRICT, and KIT_DRIFT_HTTP_REPORT=<file> to write the computed
 * hashes (refresh the fixture after re-porting).
 */
import { execFileSync } from 'child_process';
import * as crypto from 'crypto';
import * as fs from 'fs';
import * as path from 'path';
import * as ts from 'typescript';

interface Selector {
  /** A top-level declaration (function, class, const, ...), as K1's extractor takes it. */
  symbol?: string;
  /** The one top-level statement whose text contains this string. */
  statementContaining?: string;
}

interface Entry {
  upstreamFile: string;
  select: Selector;
  label: string;
  kitFile: string;
  kitSymbol: string;
  note?: string;
  sha256: string;
}

interface Fixture {
  upstream: { repo: string; localPath: string; commit: string; committedAt: string; portedOn: string };
  hash: { algorithm: string; unit: string; normalization: string; tool: string };
  entries: Entry[];
}

type DriftStatus = 'same' | 'changed' | 'missing-unit' | 'missing-file';

const KIT = path.resolve(__dirname, '../..');
const FIXTURE: Fixture = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures/ported-from-http.json'), 'utf8'));

// ─── extraction (K1's rules, plus statement selection) ─────────────────────

function declaredNames(stmt: ts.Statement): string[] {
  if (ts.isFunctionDeclaration(stmt) || ts.isClassDeclaration(stmt) || ts.isInterfaceDeclaration(stmt)
    || ts.isTypeAliasDeclaration(stmt) || ts.isEnumDeclaration(stmt)) {
    return stmt.name ? [stmt.name.text] : [];
  }
  if (ts.isVariableStatement(stmt)) {
    return stmt.declarationList.declarations.flatMap((d) => (ts.isIdentifier(d.name) ? [d.name.text] : []));
  }
  return [];
}

function isDeclarationLike(stmt: ts.Statement): boolean {
  return declaredNames(stmt).length > 0 || ts.isImportDeclaration(stmt) || ts.isImportEqualsDeclaration(stmt)
    || ts.isExportDeclaration(stmt) || ts.isExportAssignment(stmt) || ts.isModuleDeclaration(stmt);
}

const parsed = new Map<string, ts.SourceFile>();
function sourceFile(source: string, fileName: string): ts.SourceFile {
  const key = `${fileName}\u0000${source.length}\u0000${crypto.createHash('sha1').update(source).digest('hex')}`;
  let sf = parsed.get(key);
  if (!sf) {
    const kind = fileName.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS;
    sf = ts.createSourceFile(fileName, source, ts.ScriptTarget.Latest, true, kind);
    parsed.set(key, sf);
  }
  return sf;
}

/** The upstream text a selector names; null when absent (or, for statementContaining, not unique). */
export function selectUnitText(source: string, sel: Selector, fileName = 'upstream.ts'): string | null {
  const sf = sourceFile(source, fileName);
  const stmts = sf.statements;
  if (sel.statementContaining) {
    const hits = stmts.filter((s) => s.getText(sf).includes(sel.statementContaining as string));
    return hits.length === 1 ? (hits[0] as ts.Statement).getText(sf) : null;
  }
  const symbol = sel.symbol;
  if (!symbol) return null;
  const mentions = new RegExp(`\\b${symbol.replace(/[$]/g, '\\$')}\\b`);
  const parts: string[] = [];
  for (let i = 0; i < stmts.length; i++) {
    const s = stmts[i] as ts.Statement;
    if (!declaredNames(s).includes(symbol)) continue;
    parts.push(s.getText(sf));
    if (ts.isVariableStatement(s)) {
      for (let j = i + 1; j < stmts.length; j++) {
        const t = stmts[j] as ts.Statement;
        if (isDeclarationLike(t) || !mentions.test(t.getText(sf))) break;
        parts.push(t.getText(sf));
      }
    }
  }
  return parts.length ? parts.join('\n') : null;
}

function normalizeForHash(text: string): string {
  return text.replace(/\r\n?/g, '\n').split('\n').map((l) => l.replace(/\s+$/, '')).join('\n');
}

function hashText(text: string): string {
  return crypto.createHash('sha256').update(normalizeForHash(text), 'utf8').digest('hex');
}

function selectorKey(e: Entry): string {
  return `${e.upstreamFile}#${e.select.symbol ?? `{${e.select.statementContaining}}`}`;
}

// ─── upstream ──────────────────────────────────────────────────────────────

const UPSTREAM = process.env.KIT_DRIFT_UPSTREAM || FIXTURE.upstream.localPath;
const REF = process.env.KIT_DRIFT_REF || '';
const upstreamPresent = fs.existsSync(path.join(UPSTREAM, 'src'));

const fileCache = new Map<string, string | null>();
function upstreamSource(rel: string): string | null {
  if (fileCache.has(rel)) return fileCache.get(rel) as string | null;
  let text: string | null = null;
  try {
    text = REF
      ? execFileSync('git', ['-C', UPSTREAM, 'show', `${REF}:${rel}`], { encoding: 'utf8', maxBuffer: 64 * 1024 * 1024, stdio: ['ignore', 'pipe', 'ignore'] })
      : fs.readFileSync(path.join(UPSTREAM, rel), 'utf8');
  } catch {
    text = null;
  }
  fileCache.set(rel, text);
  return text;
}

// ─── tests ─────────────────────────────────────────────────────────────────

describe('the unit selector', () => {
  const src = [
    "import express from 'express';",
    'const app = express();',
    "app.get('/a', (_req, res) => { res.send('a'); });",
    "app.get('/b', (_req, res) => { res.send('b'); });",
    "app.use((req, _res, next) => { classify(req); next(); });",
    'function helper(x: number): number { return x * 2; }',
    'const KEY = "k";',
  ].join('\n');

  it('takes the one statement containing a needle', () => {
    expect(selectUnitText(src, { statementContaining: "app.get('/b'" })).toBe("app.get('/b', (_req, res) => { res.send('b'); });");
    expect(selectUnitText(src, { statementContaining: 'classify(req)' })).toContain('app.use(');
  });

  it('returns null for an ambiguous or absent needle', () => {
    expect(selectUnitText(src, { statementContaining: 'app.get(' })).toBeNull();
    expect(selectUnitText(src, { statementContaining: "app.get('/zzz'" })).toBeNull();
  });

  it('takes declarations like K1 does', () => {
    expect(selectUnitText(src, { symbol: 'helper' })).toBe('function helper(x: number): number { return x * 2; }');
    expect(selectUnitText(src, { symbol: 'KEY' })).toBe('const KEY = "k";');
    expect(selectUnitText(src, { symbol: 'missing' })).toBeNull();
  });

  it('parses .tsx', () => {
    const tsx = 'export function C() { return <div className="x" />; }';
    expect(selectUnitText(tsx, { symbol: 'C' }, 'c.tsx')).toContain('<div');
  });

  it('hashes stably under CRLF and trailing whitespace', () => {
    expect(hashText('a();  \r\nb();')).toBe(hashText('a();\nb();'));
    expect(hashText('a();')).not.toBe(hashText('b();'));
  });
});

describe('ported-from-http.json', () => {
  it('names the upstream commit and hashes every entry', () => {
    expect(FIXTURE.upstream.commit).toMatch(/^[0-9a-f]{40}$/);
    expect(FIXTURE.hash.algorithm).toBe('sha256');
    expect(FIXTURE.entries.length).toBeGreaterThanOrEqual(15);
    for (const e of FIXTURE.entries) {
      expect(e.sha256).toMatch(/^[0-9a-f]{64}$/);
      expect(!!e.select.symbol !== !!e.select.statementContaining).toBe(true);
    }
  });

  it('has one entry per upstream unit', () => {
    const keys = FIXTURE.entries.map(selectorKey);
    expect(new Set(keys).size).toBe(keys.length);
  });

  it('points every entry at a kit file that still holds its kit symbol', () => {
    const missing = FIXTURE.entries.filter((e) => {
      const file = path.join(KIT, e.kitFile);
      if (!fs.existsSync(file)) return true;
      return !new RegExp(`\\b${e.kitSymbol}\\b`).test(fs.readFileSync(file, 'utf8'));
    }).map((e) => `${e.kitFile}#${e.kitSymbol}`);
    expect(missing).toEqual([]);
  });

  it('uses the same upstream commit as K1 (PORTED_FROM.json)', () => {
    const k1 = JSON.parse(fs.readFileSync(path.join(KIT, 'PORTED_FROM.json'), 'utf8')) as { upstream: { commit: string } };
    expect(FIXTURE.upstream.commit).toBe(k1.upstream.commit);
  });
});

const describeUpstream = upstreamPresent ? describe : describe.skip;

describeUpstream(`K2 drift against the SpecPicks checkout (${UPSTREAM}${REF ? ` @ ${REF}` : ''})`, () => {
  const results = FIXTURE.entries.map((e) => {
    const src = upstreamSource(e.upstreamFile);
    if (src == null) return { e, status: 'missing-file' as DriftStatus, now: null as string | null };
    const text = selectUnitText(src, e.select, e.upstreamFile);
    if (text == null) return { e, status: 'missing-unit' as DriftStatus, now: null };
    const now = hashText(text);
    return { e, status: (now === e.sha256 ? 'same' : 'changed') as DriftStatus, now };
  });
  const drifted = results.filter((r) => r.status !== 'same');

  it('reports drift (fails only with KIT_DRIFT_STRICT=1)', () => {
    if (process.env.KIT_DRIFT_HTTP_REPORT) {
      fs.writeFileSync(process.env.KIT_DRIFT_HTTP_REPORT, `${JSON.stringify({
        upstream: UPSTREAM,
        ref: REF || 'working-tree',
        entries: results.map((r) => ({ key: selectorKey(r.e), status: r.status, sha256: r.now })),
      }, null, 2)}\n`);
    }
    if (drifted.length) {
      // eslint-disable-next-line no-console
      console.warn([
        `affiliate-site kit (HTTP/client): ${drifted.length} of ${results.length} ported SpecPicks units changed since ${FIXTURE.upstream.commit.slice(0, 8)}.`,
        'Review them and port the fix, or record the new hash in unit/http/fixtures/ported-from-http.json:',
        ...drifted.map((r) => `  ${r.status.padEnd(13)} ${selectorKey(r.e)} -> kit ${r.e.kitFile}#${r.e.kitSymbol}`),
      ].join('\n'));
    }
    if (process.env.KIT_DRIFT_STRICT === '1') {
      expect(drifted.map((r) => `${selectorKey(r.e)}: ${r.status}`)).toEqual([]);
    }
  });
});
