/**
 * The kit's migration files, offline: names and order, the idempotent forms
 * every statement must use, no site names, and the column contract against
 * the reference deployment's live schema (unit/db/fixtures/upstream-schema.json)
 * read from the SQL text. migrate.pg.test.ts checks the same contract on a
 * real database through information_schema.
 */
import fs from 'fs';
import path from 'path';
import { splitSqlStatements } from '../../server/src/db/migrate';

const KIT = path.resolve(__dirname, '..', '..');
const DIR = path.join(KIT, 'migrations');
const FILES = fs.readdirSync(DIR).filter((f) => f.endsWith('.sql')).sort();
const sqlOf = (f: string) => fs.readFileSync(path.join(DIR, f), 'utf8');
const UPSTREAM = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures', 'upstream-schema.json'), 'utf8')) as {
  tables: Record<string, { columns: Array<{ name: string; type: string; nullable: boolean; default: string | null; kitDefault?: string }>; indexes: string[] }>;
  views: Record<string, Array<{ name: string; type: string }>>;
};
const SITE_NAMES = /specpicks|aisleprompt|runtimepicks/i;

/** SQL without `--` comments (string and dollar-quoted text kept: it reaches the database). */
function withoutLineComments(sql: string): string {
  return splitSqlStatements(sql).map((s) => {
    // splitSqlStatements keeps comments in .text; drop whole-line and trailing ones outside quotes.
    return s.text.split('\n').map((line) => {
      let quote: string | null = null;
      for (let i = 0; i < line.length; i++) {
        const c = line[i];
        if (quote) {
          if (line.startsWith(quote, i)) { i += quote.length - 1; quote = null; }
        } else if (c === "'") quote = "'";
        else if (c === '$') {
          const m = /^\$[A-Za-z_]*\$/.exec(line.slice(i));
          if (m) { quote = m[0]; i += m[0].length - 1; }
        } else if (c === '-' && line[i + 1] === '-') return line.slice(0, i);
      }
      return line;
    }).join('\n');
  }).join(';\n');
}

describe('kit migration files', () => {
  it('are kit_NNNN_<name>.sql, numbered from 0001 without gaps', () => {
    expect(FILES.length).toBeGreaterThanOrEqual(4);
    FILES.forEach((f, i) => expect(f).toMatch(new RegExp(`^kit_${String(i + 1).padStart(4, '0')}_[a-z0-9_]+\\.sql$`)));
    expect(FILES.slice(0, 4)).toEqual(['kit_0001_outbound_clicks.sql', 'kit_0002_ai_traffic_log.sql', 'kit_0003_editorial_articles.sql', 'kit_0004_views.sql']);
  });

  it.each(FILES)('%s sets a lock timeout and a statement timeout first and uses only idempotent, transactional statements', (f) => {
    const stmts = splitSqlStatements(sqlOf(f)).filter((s) => s.code);
    // .code blanks literals; .text keeps them (and the comments before the first statement).
    expect(stmts[0].code).toMatch(/^SET LOCAL lock_timeout =\s*$/);
    expect(stmts[0].text).toMatch(/\nSET LOCAL lock_timeout = '5s'$/);
    // The role's own statement_timeout (binding amendment 9) must not cancel a migration at boot.
    expect(stmts[1].text).toBe("SET LOCAL statement_timeout = '15min'");
    const bad: string[] = [];
    for (const s of stmts) {
      const code = s.code.replace(/\s+/g, ' ').trim();
      const ok =
        /^SET LOCAL /i.test(code)
        || /^CREATE TABLE IF NOT EXISTS /i.test(code)
        || /^CREATE (UNIQUE )?INDEX IF NOT EXISTS /i.test(code)
        || /^CREATE OR REPLACE (FUNCTION|VIEW|TRIGGER) /i.test(code)
        || /^COMMENT ON /i.test(code)
        || /^DO\b/i.test(code);
      if (!ok) bad.push(code.slice(0, 80));
      // One implicit transaction per file: no CONCURRENTLY, no BEGIN/COMMIT of its own.
      if (/\bconcurrently\b|^(BEGIN|COMMIT|ROLLBACK|START TRANSACTION)\b/i.test(code)) bad.push(`non-transactional: ${code.slice(0, 60)}`);
    }
    expect(bad).toEqual([]);
  });

  it.each(FILES)('%s names no site outside comments (comments may cite provenance)', (f) => {
    expect(withoutLineComments(sqlOf(f))).not.toMatch(SITE_NAMES);
  });

  it('the publish-guard template names no site outside comments either', () => {
    expect(withoutLineComments(fs.readFileSync(path.join(DIR, 'templates', 'publish_trigger.sql.tmpl'), 'utf8'))).not.toMatch(SITE_NAMES);
  });

  it('server/src/db sources name no site in code', () => {
    const dir = path.join(KIT, 'server', 'src', 'db');
    const offenders = fs.readdirSync(dir).filter((n) => n.endsWith('.ts')).filter((n) => {
      const code = fs.readFileSync(path.join(dir, n), 'utf8')
        .replace(/\/\*[\s\S]*?\*\//g, '')
        .replace(/(^|[^:])\/\/.*$/gm, '$1');
      return SITE_NAMES.test(code);
    });
    expect(offenders).toEqual([]);
  });
});

// ─── The column contract, read from the SQL text ────────────────────────────

const TYPE_MAP: Array<[RegExp, (m: RegExpMatchArray) => string]> = [
  [/^BIGSERIAL\b/i, () => 'bigint'],
  [/^SERIAL\b/i, () => 'integer'],
  [/^VARCHAR\((\d+)\)/i, (m) => `character varying(${m[1]})`],
  [/^TEXT\[\]/i, () => 'text[]'],
  [/^TEXT\b/i, () => 'text'],
  [/^(INTEGER|INT)\b/i, () => 'integer'],
  [/^INET\b/i, () => 'inet'],
  [/^BOOLEAN\b/i, () => 'boolean'],
  [/^(TIMESTAMPTZ|TIMESTAMP WITH TIME ZONE)\b/i, () => 'timestamp with time zone'],
  [/^JSONB\b/i, () => 'jsonb'],
  [/^NUMERIC\b/i, () => 'numeric'],
];

interface ParsedColumn { name: string; type: string; nullable: boolean; default: string | null }

/** The column lines of `CREATE TABLE IF NOT EXISTS <table> (...)` in the kit SQL. */
function parseTable(table: string): ParsedColumn[] {
  const file = FILES.find((f) => sqlOf(f).includes(`CREATE TABLE IF NOT EXISTS ${table} (`));
  if (!file) throw new Error(`no CREATE TABLE for ${table}`);
  const head = `CREATE TABLE IF NOT EXISTS ${table} (`;
  const stmt = splitSqlStatements(sqlOf(file)).find((s) => s.text.includes(head))!;
  const body = stmt.text.slice(stmt.text.indexOf(head) + head.length);
  const cols: ParsedColumn[] = [];
  for (const raw of body.split('\n')) {
    const line = raw.replace(/\s--.*$/, '').trim().replace(/,$/, '');
    if (!line || line.startsWith('--')) continue;
    // Table constraints follow the columns; the body ends with ')'.
    if (/^(CONSTRAINT|CHECK|PRIMARY KEY|UNIQUE)\b|^\)/i.test(line)) break;
    const m = /^([a-z_][a-z0-9_]*)\s+(.+)$/.exec(line);
    if (!m) throw new Error(`cannot parse column line ${JSON.stringify(line)}`);
    const [, name, rest] = m;
    const tm = TYPE_MAP.map(([re, f]) => [re.exec(rest), f] as const).find(([mm]) => mm);
    if (!tm) throw new Error(`unknown type in ${JSON.stringify(line)}`);
    const type = tm[1](tm[0] as RegExpMatchArray);
    const serial = /^(BIG)?SERIAL\b/i.test(rest);
    const def = /\bDEFAULT\s+(.+?)$/i.exec(rest)?.[1].trim() ?? null;
    let normDefault: string | null = serial ? `nextval('${table}_id_seq'::regclass)` : null;
    if (def !== null) {
      if (/^NOW\(\)$/i.test(def)) normDefault = 'now()';
      else if (/^(TRUE|FALSE)$/i.test(def)) normDefault = def.toLowerCase();
      else if (/^\d+$/.test(def)) normDefault = def;
      else if (/^'.*'$/.test(def)) normDefault = `${def}::${type.replace(/\(\d+\)$/, '')}`;
      else throw new Error(`unhandled default ${def}`);
    }
    cols.push({ name, type, nullable: !(serial || /\bNOT NULL\b|\bPRIMARY KEY\b/i.test(rest)), default: normDefault });
  }
  return cols;
}

describe('the column contract (SQL text vs the reference live schema)', () => {
  it.each(Object.keys(UPSTREAM.tables))('%s: same columns, order, types, nullability and defaults', (table) => {
    const want = UPSTREAM.tables[table].columns.map((c) => ({ name: c.name, type: c.type, nullable: c.nullable, default: c.kitDefault ?? c.default }));
    expect(parseTable(table)).toEqual(want);
  });

  it.each(Object.keys(UPSTREAM.tables))('%s: every reference index is created under its name', (table) => {
    const all = FILES.map(sqlOf).join('\n');
    const missing = UPSTREAM.tables[table].indexes
      .filter((n) => !/_pkey$|_key$/.test(n))
      .filter((n) => !new RegExp(`CREATE INDEX IF NOT EXISTS ${n}\\s+ON ${table}\\b`).test(all.replace(/\s+/g, ' ')));
    expect(missing).toEqual([]);
  });

  it('the only default that differs from the reference is the author placeholder (no site name)', () => {
    const diffs = Object.values(UPSTREAM.tables).flatMap((t) => t.columns.filter((c) => c.kitDefault !== undefined));
    expect(diffs.map((c) => [c.name, c.kitDefault])).toEqual([['author', "'Editorial'::character varying"]]);
  });

  it.each(Object.keys(UPSTREAM.views))('view %s selects the reference column names', (view) => {
    // The latest kit migration that (re)defines the view is the one in force.
    const defines = new RegExp(`^CREATE OR REPLACE VIEW ${view} AS`);
    const stmt = FILES.flatMap((f) => splitSqlStatements(sqlOf(f))).filter((s) => defines.test(s.code)).pop()!;
    expect(stmt).toBeDefined();
    const select = stmt.text.slice(stmt.text.indexOf('SELECT') + 6, stmt.text.search(/\n\s*FROM /));
    const names = select.split(/,(?![^(]*\))/).map((part) => {
      const alias = /\bAS\s+([a-z_][a-z0-9_]*)\s*$/i.exec(part.trim());
      return alias ? alias[1] : part.trim().replace(/^.*\./, '');
    });
    expect(names).toEqual(UPSTREAM.views[view].map((c) => c.name));
  });
});
