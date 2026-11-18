/**
 * Declaration hashing for drift checks, the algorithm PORTED_FROM.json
 * documents: the top-level declaration(s) of a symbol as the TypeScript parser
 * prints them (Node.getText(): modifiers, signature and body; leading comments
 * excluded; overloads joined with a newline; a const also takes the following
 * top-level statements that only configure it), CRLF -> LF, trailing
 * whitespace stripped per line, sha256 of the UTF-8 text.
 *
 * The same functions exist inside unit/drift.test.ts, but importing a test
 * module would register its suites a second time, so the db drift test uses
 * this copy; unit/db/drift.test.ts checks the two agree on shared fixtures.
 */
import * as crypto from 'crypto';
import * as ts from 'typescript';

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

/** The source text of `symbol`'s top-level declaration(s); null when absent. */
export function extractSymbolText(source: string, symbol: string, fileName = 'upstream.ts'): string | null {
  let sf = parsed.get(source);
  if (!sf) {
    sf = ts.createSourceFile(fileName, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
    parsed.set(source, sf);
  }
  const stmts = sf.statements;
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

export function normalizeForHash(text: string): string {
  return text.replace(/\r\n?/g, '\n').split('\n').map((l) => l.replace(/\s+$/, '')).join('\n');
}

export function hashText(text: string): string {
  return crypto.createHash('sha256').update(normalizeForHash(text), 'utf8').digest('hex');
}
