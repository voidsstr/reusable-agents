/**
 * Drift between the kit and the SpecPicks functions it was ported from.
 *
 * PORTED_FROM.json records the SpecPicks commit the port was taken from and a
 * sha256 of each ported upstream declaration. When a SpecPicks checkout is
 * present (PORTED_FROM.json upstream.localPath, or KIT_DRIFT_UPSTREAM), this
 * test re-hashes those declarations and reports every one that changed, so a
 * fix made in SpecPicks (price rule, CTA, Offer, click rules) is not silently
 * missing from the kit, and vice versa.
 *
 * Drift is reported, not failed, unless KIT_DRIFT_STRICT=1 (SpecPicks agents
 * edit those files daily; the kit gate must not go red on their schedule).
 *
 * Maintainer knobs:
 *   KIT_DRIFT_UPSTREAM=<dir>   upstream checkout (default: upstream.localPath)
 *   KIT_DRIFT_REF=<commit>     hash `git show <commit>:<file>` instead of the working tree
 *   KIT_DRIFT_REPORT=<file>    write the computed hashes as JSON (use it to refresh PORTED_FROM.json after a re-port)
 *   KIT_DRIFT_STRICT=1         fail on any drift
 */
import { execFileSync } from 'child_process';
import * as crypto from 'crypto';
import * as fs from 'fs';
import * as path from 'path';
import * as ts from 'typescript';

interface PortedEntry {
  upstreamFile: string;
  symbol: string;
  kitFile: string;
  kitSymbol: string;
  note?: string;
  sha256: string;
}

interface PortedFrom {
  upstream: { repo: string; localPath: string; commit: string; committedAt: string; designReferenceCommit: string; portedOn: string };
  hash: { algorithm: string; unit: string; normalization: string; tool: string };
  entries: PortedEntry[];
}

type DriftStatus = 'same' | 'changed' | 'missing-symbol' | 'missing-file';

const KIT = path.resolve(__dirname, '..');
const PORTED: PortedFrom = JSON.parse(fs.readFileSync(path.join(KIT, 'PORTED_FROM.json'), 'utf8'));

// ─── extraction and hashing ────────────────────────────────────────────────

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

// ssrRender.ts is ~49k lines and a dozen symbols come from it: parse each text once.
const parsed = new Map<string, ts.SourceFile>();
function sourceFile(source: string, fileName: string): ts.SourceFile {
  let sf = parsed.get(source);
  if (!sf) {
    sf = ts.createSourceFile(fileName, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
    parsed.set(source, sf);
  }
  return sf;
}

/** The source text of `symbol`'s top-level declaration(s); null when absent. */
export function extractSymbolText(source: string, symbol: string, fileName = 'upstream.ts'): string | null {
  const sf = sourceFile(source, fileName);
  const stmts = sf.statements;
  const mentions = new RegExp(`\\b${symbol.replace(/[$]/g, '\\$')}\\b`);
  const parts: string[] = [];
  for (let i = 0; i < stmts.length; i++) {
    const s = stmts[i] as ts.Statement;
    if (!declaredNames(s).includes(symbol)) continue;
    parts.push(s.getText(sf));
    if (ts.isVariableStatement(s)) {
      // The statements right after a const that only configure it (e.g. loops filling a BlockList).
      for (let j = i + 1; j < stmts.length; j++) {
        const t = stmts[j] as ts.Statement;
        if (isDeclarationLike(t) || !mentions.test(t.getText(sf))) break;
        parts.push(t.getText(sf));
      }
    }
  }
  return parts.length ? parts.join('\n') : null;
}

export function normalizeForHash(text: string): string {
  return text.replace(/\r\n?/g, '\n').split('\n').map((l) => l.replace(/\s+$/, '')).join('\n');
}

export function hashText(text: string): string {
  return crypto.createHash('sha256').update(normalizeForHash(text), 'utf8').digest('hex');
}

// ─── upstream access ───────────────────────────────────────────────────────

const UPSTREAM = process.env.KIT_DRIFT_UPSTREAM || PORTED.upstream.localPath;
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

function upstreamHead(): string {
  try {
    return execFileSync('git', ['-C', UPSTREAM, 'rev-parse', 'HEAD'], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }).trim();
  } catch {
    return 'unknown';
  }
}

// ─── tests ─────────────────────────────────────────────────────────────────

describe('the hash extractor', () => {
  const src = [
    "import { x } from './x';",
    '/** doc comments are not hashed */',
    'export function f(a: number): number {',
    '  return a + 1;',
    '}',
    'function g(a: string): string;',
    'function g(a: number): number;',
    'function g(a: any): any { return a; }',
    'const LIST = new Set<string>();',
    "for (const s of ['a', 'b']) LIST.add(s);",
    "LIST.add('c');",
    "console.log('unrelated');",
    'export class C { m(): void {} }',
    'export interface I { a: string }',
    'type T = I | null;',
    'const a = 1, b = 2;',
  ].join('\n');

  it('takes a declaration with its modifiers and body, without leading comments', () => {
    expect(extractSymbolText(src, 'f')).toBe('export function f(a: number): number {\n  return a + 1;\n}');
  });
  it('joins overloads, and keeps the statements that configure a const', () => {
    expect(extractSymbolText(src, 'g')?.split('\n')).toHaveLength(3);
    expect(extractSymbolText(src, 'LIST')).toBe("const LIST = new Set<string>();\nfor (const s of ['a', 'b']) LIST.add(s);\nLIST.add('c');");
  });
  it('finds classes, interfaces, type aliases and multi-declarator consts; null when absent', () => {
    expect(extractSymbolText(src, 'C')).toContain('class C');
    expect(extractSymbolText(src, 'I')).toContain('interface I');
    expect(extractSymbolText(src, 'T')).toBe('type T = I | null;');
    expect(extractSymbolText(src, 'b')).toBe('const a = 1, b = 2;');
    expect(extractSymbolText(src, 'missing')).toBeNull();
    expect(extractSymbolText(src, 'x')).toBeNull(); // imports are not declarations of the module
  });
  it('hashes are stable under CRLF and trailing whitespace, and change with code', () => {
    const a = hashText('function f() {\n  return 1;\n}');
    expect(hashText('function f() {  \r\n  return 1;\r\n}')).toBe(a);
    expect(hashText('function f() {\n  return 2;\n}')).not.toBe(a);
    expect(a).toMatch(/^[0-9a-f]{64}$/);
  });
});

describe('PORTED_FROM.json', () => {
  it('names the upstream commit and hashes every entry', () => {
    expect(PORTED.upstream.commit).toMatch(/^[0-9a-f]{40}$/);
    expect(PORTED.hash.algorithm).toBe('sha256');
    expect(PORTED.entries.length).toBeGreaterThan(50);
    for (const e of PORTED.entries) expect(e.sha256).toMatch(/^[0-9a-f]{64}$/);
  });

  it('has one entry per upstream symbol', () => {
    const keys = PORTED.entries.map((e) => `${e.upstreamFile}#${e.symbol}`);
    expect(new Set(keys).size).toBe(keys.length);
  });

  it('points every entry at a kit file that still holds its kit symbol', () => {
    const missing = PORTED.entries.filter((e) => {
      const file = path.join(KIT, e.kitFile);
      if (!fs.existsSync(file)) return true;
      return !new RegExp(`\\b${e.kitSymbol}\\b`).test(fs.readFileSync(file, 'utf8'));
    }).map((e) => `${e.kitFile}#${e.kitSymbol}`);
    expect(missing).toEqual([]);
  });
});

const describeUpstream = upstreamPresent ? describe : describe.skip;

describeUpstream(`drift against the SpecPicks checkout (${UPSTREAM}${REF ? ` @ ${REF}` : ''})`, () => {
  const results = PORTED.entries.map((e) => {
    const src = upstreamSource(e.upstreamFile);
    if (src == null) return { e, status: 'missing-file' as DriftStatus, now: null as string | null };
    const text = extractSymbolText(src, e.symbol, e.upstreamFile);
    if (text == null) return { e, status: 'missing-symbol' as DriftStatus, now: null };
    const now = hashText(text);
    return { e, status: (now === e.sha256 ? 'same' : 'changed') as DriftStatus, now };
  });
  const drifted = results.filter((r) => r.status !== 'same');

  it('reports drift (fails only with KIT_DRIFT_STRICT=1)', () => {
    if (process.env.KIT_DRIFT_REPORT) {
      fs.writeFileSync(process.env.KIT_DRIFT_REPORT, `${JSON.stringify({
        upstream: UPSTREAM,
        ref: REF || 'working-tree',
        head: upstreamHead(),
        entries: results.map((r) => ({ upstreamFile: r.e.upstreamFile, symbol: r.e.symbol, status: r.status, sha256: r.now })),
      }, null, 2)}\n`);
    }
    if (drifted.length) {
      const lines = drifted.map((r) => `  ${r.status.padEnd(14)} ${r.e.upstreamFile}#${r.e.symbol} -> kit ${r.e.kitFile}#${r.e.kitSymbol}`);
      // eslint-disable-next-line no-console
      console.warn([
        `affiliate-site kit: ${drifted.length} of ${results.length} ported SpecPicks declarations changed since ${PORTED.upstream.commit.slice(0, 8)}`,
        `(upstream HEAD ${upstreamHead().slice(0, 8)}). Review them and port the fix, or record the new hash in PORTED_FROM.json:`,
        ...lines,
      ].join('\n'));
    }
    if (process.env.KIT_DRIFT_STRICT === '1') {
      expect(drifted.map((r) => `${r.e.upstreamFile}#${r.e.symbol}: ${r.status}`)).toEqual([]);
    }
  });

  it('finds every ported file upstream (a rename is drift too)', () => {
    const files = Array.from(new Set(PORTED.entries.map((e) => e.upstreamFile)));
    const gone = files.filter((f) => upstreamSource(f) == null);
    if (gone.length) {
      // eslint-disable-next-line no-console
      console.warn(`affiliate-site kit: upstream files missing: ${gone.join(', ')}`);
    }
    if (process.env.KIT_DRIFT_STRICT === '1') expect(gone).toEqual([]);
  });
});
