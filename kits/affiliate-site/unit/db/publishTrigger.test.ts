/**
 * The publish-guard template and its renderer (server/src/db/publishTrigger.ts),
 * offline. The guard's behaviour on a real database is in
 * publishTrigger.pg.test.ts.
 */
import fs from 'fs';
import os from 'os';
import path from 'path';
import { splitSqlStatements } from '../../server/src/db/migrate';
import {
  DEFAULT_CONTENT_COLUMNS,
  PUBLISH_GUARD_FUNCTION,
  PUBLISH_GUARD_TRIGGER,
  PUBLISH_RECHECK_FUNCTION,
  PUBLISH_RECHECK_TRIGGER,
  PublishTriggerError,
  publishTriggerCli,
  publishTriggerTemplatePath,
  publishTriggerValues,
  renderPublishTrigger,
  renderSqlTemplate,
  validatePublishTriggerParams,
} from '../../server/src/db/publishTrigger';

const KIT = path.resolve(__dirname, '..', '..');
const TEMPLATE_PATH = path.join(KIT, 'migrations', 'templates', 'publish_trigger.sql.tmpl');
const TEMPLATE = fs.readFileSync(TEMPLATE_PATH, 'utf8');
const VERSION = fs.readFileSync(path.join(KIT, 'VERSION'), 'utf8').trim();

describe('validatePublishTriggerParams', () => {
  it('accepts a plain role with the defaults', () => {
    expect(validatePublishTriggerParams({ publisherRole: 'site_publisher' })).toEqual([]);
    expect(validatePublishTriggerParams({ publisherRole: '_p1', draftStatus: 'pending-review', publishedStatus: 'live', contentColumns: ['body_md'] })).toEqual([]);
  });

  it.each([
    [{ publisherRole: '' }, /publisherRole is required/],
    [{ publisherRole: 'Publisher' }, /lower-case SQL identifier/],
    [{ publisherRole: "p'; DROP TABLE editorial_articles; --" }, /lower-case SQL identifier/],
    [{ publisherRole: 'a'.repeat(64) }, /lower-case SQL identifier/],
    [{ publisherRole: 'public' }, /reserved/],
    [{ publisherRole: 'pg_write_all_data' }, /reserved/],
    [{ publisherRole: 'p', draftStatus: 'published' }, /must differ/],
    [{ publisherRole: 'p', publishedStatus: "pub'lished" }, /publishedStatus/],
    [{ publisherRole: 'p', draftStatus: 'x'.repeat(21) }, /draftStatus/],
    [{ publisherRole: 'p', contentColumns: [] }, /at least one column/],
    [{ publisherRole: 'p', contentColumns: ['body_md', 'body_md'] }, /listed twice/],
    [{ publisherRole: 'p', contentColumns: ['status'] }, /gated column itself/],
    [{ publisherRole: 'p', contentColumns: ['body md'] }, /lower-case SQL identifier/],
  ])('rejects %j', (params, issue) => {
    const issues = validatePublishTriggerParams(params as never);
    expect(issues.join('\n')).toMatch(issue);
    expect(() => publishTriggerValues(params as never)).toThrow(PublishTriggerError);
  });

  it("defaults the content columns to what the publish gate's checks read: the text, and category (the per-bucket policy)", () => {
    expect([...DEFAULT_CONTENT_COLUMNS]).toEqual(['title', 'subtitle', 'excerpt', 'body_md', 'faqs', 'outbound_citations', 'category']);
    expect(Object.isFrozen(DEFAULT_CONTENT_COLUMNS)).toBe(true);
  });
});

describe('renderSqlTemplate', () => {
  it('fills every placeholder, tolerating inner spaces', () => {
    expect(renderSqlTemplate('a {{x}} b {{ y }} {{x}}', { x: '1', y: '2' })).toBe('a 1 b 2 1');
  });

  it('names every placeholder that has no value', () => {
    expect(() => renderSqlTemplate('{{a}} {{b}} {{c}}', { b: '' })).toThrow(/\{\{a\}\} has no value; template placeholder \{\{c\}\} has no value/);
  });

  it('rejects a malformed placeholder instead of passing it through', () => {
    expect(() => renderSqlTemplate('{{ not closed', {})).toThrow(/malformed/);
    expect(() => renderSqlTemplate('{{1bad}}', {})).toThrow(/malformed/);
  });
});

describe('the template', () => {
  it('lives in migrations/templates and the renderer finds it', () => {
    expect(publishTriggerTemplatePath()).toBe(TEMPLATE_PATH);
    expect(fs.existsSync(TEMPLATE_PATH)).toBe(true);
  });

  it('uses exactly the placeholders the renderer provides', () => {
    const used = new Set([...TEMPLATE.matchAll(/\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}/g)].map((m) => m[1]));
    expect([...used].sort()).toEqual(Object.keys(publishTriggerValues({ publisherRole: 'p' })).sort());
  });

  it('keeps the six placeholders framework/tests/test_article_publish_gate.py renders it with (a new one breaks that test)', () => {
    expect(Object.keys(publishTriggerValues({ publisherRole: 'p' })).sort()).toEqual(
      ['content_columns', 'content_columns_plain', 'draft_status', 'kit_version', 'published_status', 'publisher_role']);
  });

  it('states the role contract the guard depends on', () => {
    // The comment block as prose: no '--' prefixes, one space between words.
    const header = TEMPLATE.slice(0, TEMPLATE.indexOf('SET LOCAL')).replace(/^--\s?/gm, '').replace(/\s+/g, ' ');
    for (const rule of [/holds TRIGGER on editorial_articles/, /GRANT ALL/, /session_replication_role/, /pg_restore --disable-triggers/,
      /dedicated owner role/, /publisher DSN/, /SECURITY DEFINER/, /sorts AFTER the guard/, /dropped or renamed/,
      /NOT DEFERRABLE constraint trigger/, /cannot be swapped by CREATE OR REPLACE TRIGGER/, /refuses every publish while a trigger runs a function/]) {
      expect(header).toMatch(rule);
    }
  });

  it('is not a migration the runner would pick up by itself', () => {
    expect(TEMPLATE_PATH.endsWith('.sql')).toBe(false);
  });
});

describe('renderPublishTrigger', () => {
  const sql = renderPublishTrigger({ publisherRole: 'site_publisher' });

  it('leaves no placeholder behind and states its parameters and kit version in the header', () => {
    expect(sql).not.toMatch(/\{\{|\}\}/);
    expect(sql).toContain(`affiliate-site kit ${VERSION}`);
    expect(sql).toMatch(/publisher role\s+site_publisher/);
    expect(sql).toMatch(/content columns\s+'title', 'subtitle', 'excerpt', 'body_md', 'faqs', 'outbound_citations', 'category'/);
  });

  it('is: both timeouts, install checks, the guard and the recheck with their comments, the guard trigger, the recheck as a constraint trigger, ENABLE ALWAYS', () => {
    // .code blanks quoted literals and comments: the shape of each statement.
    const stmts = splitSqlStatements(sql).filter((s) => s.code);
    expect(stmts.map((s) => s.code.replace(/\s+/g, ' ').trim())).toEqual([
      'SET LOCAL lock_timeout =',
      'SET LOCAL statement_timeout =',
      expect.stringMatching(/^DO\b/),
      expect.stringMatching(new RegExp(`^CREATE OR REPLACE FUNCTION ${PUBLISH_GUARD_FUNCTION}\\(\\)`)),
      expect.stringMatching(new RegExp(`^COMMENT ON FUNCTION ${PUBLISH_GUARD_FUNCTION}\\(\\)`)),
      expect.stringMatching(new RegExp(`^CREATE OR REPLACE FUNCTION ${PUBLISH_RECHECK_FUNCTION}\\(\\)`)),
      expect.stringMatching(new RegExp(`^COMMENT ON FUNCTION ${PUBLISH_RECHECK_FUNCTION}\\(\\)`)),
      expect.stringMatching(new RegExp(`^CREATE OR REPLACE TRIGGER ${PUBLISH_GUARD_TRIGGER} BEFORE INSERT OR UPDATE`)),
      // Constraint triggers take no OR REPLACE.
      `DROP TRIGGER IF EXISTS ${PUBLISH_RECHECK_TRIGGER} ON editorial_articles`,
      expect.stringMatching(new RegExp(`^CREATE CONSTRAINT TRIGGER ${PUBLISH_RECHECK_TRIGGER} AFTER INSERT OR UPDATE ON editorial_articles NOT DEFERRABLE FOR EACH ROW`)),
      `ALTER TABLE editorial_articles ENABLE ALWAYS TRIGGER ${PUBLISH_GUARD_TRIGGER}, ENABLE ALWAYS TRIGGER ${PUBLISH_RECHECK_TRIGGER}`,
    ]);
    expect(sql).toContain("SET LOCAL lock_timeout = '5s';\nSET LOCAL statement_timeout = '15min';\n");
  });

  it('compares current_user with the role and lists the content columns as a text[]', () => {
    expect(sql).toContain("publisher       CONSTANT name   := 'site_publisher';");
    expect(sql).toContain("content_columns CONSTANT text[] := ARRAY['title', 'subtitle', 'excerpt', 'body_md', 'faqs', 'outbound_citations', 'category']::text[];");
    expect(sql).toContain('IF current_user = publisher THEN');
    expect(sql).toMatch(/BEFORE INSERT OR UPDATE ON editorial_articles\s+FOR EACH ROW EXECUTE FUNCTION editorial_articles_publish_guard\(\)/);
    expect(sql).toMatch(/CREATE CONSTRAINT TRIGGER editorial_articles_zzz_publish_recheck_trg\s+AFTER INSERT OR UPDATE ON editorial_articles\s+NOT DEFERRABLE\s+FOR EACH ROW\s+WHEN \(NEW\.status = 'published'\)\s+EXECUTE FUNCTION editorial_articles_publish_recheck\(\)/);
    expect(sql).not.toMatch(/\bDEFERRABLE\b(?<!NOT DEFERRABLE)|INITIALLY DEFERRED/);
  });

  it('compares the content columns by name on each call, never by serializing the whole row', () => {
    const guard = sql.slice(sql.indexOf(`CREATE OR REPLACE FUNCTION ${PUBLISH_GUARD_FUNCTION}`), sql.indexOf(`COMMENT ON FUNCTION ${PUBLISH_GUARD_FUNCTION}`));
    expect(guard).not.toMatch(/to_jsonb|row_to_json/);
    const compare = "EXECUTE (SELECT 'SELECT ' || string_agg(format('($1).%1$I IS DISTINCT FROM ($2).%1$I', c), ' OR ')";
    expect(guard).toContain(compare);
    const recheck = sql.slice(sql.indexOf(`CREATE OR REPLACE FUNCTION ${PUBLISH_RECHECK_FUNCTION}`), sql.indexOf(`COMMENT ON FUNCTION ${PUBLISH_RECHECK_FUNCTION}`));
    expect(recheck).toContain(compare);
    expect(recheck).toContain("RAISE EXCEPTION 'publish guard:");
  });

  it("refuses the publisher's write while a trigger runs a function that neither the table owner (or a member) nor a superuser owns", () => {
    const recheck = sql.slice(sql.indexOf(`CREATE OR REPLACE FUNCTION ${PUBLISH_RECHECK_FUNCTION}`), sql.indexOf(`COMMENT ON FUNCTION ${PUBLISH_RECHECK_FUNCTION}`));
    expect(recheck).toContain('WHERE t.tgrelid = TG_RELID');
    expect(recheck).toContain('AND NOT t.tgisinternal');
    expect(recheck).toContain('AND NOT o.rolsuper');
    expect(recheck).toContain("AND NOT pg_has_role(p.proowner, c.relowner, 'MEMBER')");
    expect(recheck).toContain("broken := format('published by %s while trigger(s) %s run a function that neither the table owner (%s) nor a superuser owns'");
    // Catalog names resolve in pg_catalog first, never to a temporary object.
    expect(recheck).toContain('SET search_path = pg_catalog, pg_temp');
  });

  it('keeps the statuses quoted in the guard function, where the publish gate looks for them (guard_status)', () => {
    const guard = sql.slice(sql.indexOf(`CREATE OR REPLACE FUNCTION ${PUBLISH_GUARD_FUNCTION}`), sql.indexOf(`COMMENT ON FUNCTION ${PUBLISH_GUARD_FUNCTION}`));
    const body = guard.slice(guard.indexOf('$fn$') + 4, guard.lastIndexOf('$fn$'));
    expect(body).toContain("'draft'");
    expect(body).toContain("'published'");
  });

  it('runs last among BEFORE triggers (name order), after the kit and reference triggers', () => {
    const names = ['editorial_articles_updated_at_content_trg', 'editorial_articles_vertical_trg', 'editorial_articles_zz_category_alias_trg', PUBLISH_GUARD_TRIGGER];
    // Postgres orders a table's triggers by strcmp() of the name; for ASCII
    // names that is JavaScript's default sort.
    expect([...names].sort()).toEqual(names);
    // ...which is why the install warns about a later-sorting BEFORE trigger.
    expect(['set_body_trg', PUBLISH_GUARD_TRIGGER].sort()).toEqual([PUBLISH_GUARD_TRIGGER, 'set_body_trg']);
    expect(sql).toContain(`t.tgname::text COLLATE "C" > '${PUBLISH_GUARD_TRIGGER}'`);
  });

  it('is deterministic and follows custom statuses and columns', () => {
    expect(renderPublishTrigger({ publisherRole: 'site_publisher' })).toBe(sql);
    const custom = renderPublishTrigger({ publisherRole: 'p', draftStatus: 'pending', publishedStatus: 'live', contentColumns: ['body_md'] }, { kitVersion: '9.9.9' });
    expect(custom).toContain("draft_status    CONSTANT text   := 'pending';");
    expect(custom).toContain("public_status   CONSTANT text   := 'live';");
    expect(custom).toContain("ARRAY['body_md']::text[]");
    expect(custom).toContain("WHEN (NEW.status = 'live')");
    expect(custom).toContain('affiliate-site kit 9.9.9');
  });

  it('takes a template text, and a bad kit version never reaches the SQL', () => {
    expect(renderPublishTrigger({ publisherRole: 'p' }, { template: '-- {{kit_version}} {{publisher_role}}', kitVersion: "1'; --" }))
      .toBe('-- unversioned p');
  });
});

describe('publishTriggerCli', () => {
  const dirs: string[] = [];
  afterAll(() => { for (const d of dirs) fs.rmSync(d, { recursive: true, force: true }); });

  function run(argv: string[], cwd?: string) {
    const out: string[] = [];
    const err: string[] = [];
    const code = publishTriggerCli(argv, { cwd, stdout: (t) => out.push(t), stderr: (l) => err.push(l) });
    return { code, out: out.join(''), err: err.join('\n') };
  }

  it('prints help', () => {
    const r = run(['--help']);
    expect(r.code).toBe(0);
    expect(r.out).toMatch(/--publisher-role <role>/);
  });

  it('renders to stdout', () => {
    const r = run(['--publisher-role', 'site_publisher', '--content-columns', 'title, body_md']);
    expect(r.code).toBe(0);
    expect(r.out).toContain("ARRAY['title', 'body_md']::text[]");
  });

  it('exits 2 on a missing role, an invalid value, an unknown flag or a flag without a value', () => {
    expect(run([]).code).toBe(2);
    expect(run(['--publisher-role', 'Bad Role']).err).toMatch(/lower-case SQL identifier/);
    expect(run(['--publisher-role', 'p', '--frobnicate', 'x']).code).toBe(2);
    expect(run(['--publisher-role']).code).toBe(2);
    expect(run(['postgres://u:secret@h/db']).err).not.toContain('secret');
  });

  it('--out writes a new file and refuses to overwrite one', () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'kit-publish-guard-'));
    dirs.push(dir);
    const first = run(['--publisher-role', 'p', '--out', 'db/0009_guard.sql'], dir);
    expect(first.code).toBe(2); // the db/ directory does not exist
    fs.mkdirSync(path.join(dir, 'db'));
    expect(run(['--publisher-role', 'p', '--out', 'db/0009_guard.sql'], dir).code).toBe(0);
    expect(fs.readFileSync(path.join(dir, 'db', '0009_guard.sql'), 'utf8')).toContain("CONSTANT name   := 'p';");
    const again = run(['--publisher-role', 'q', '--out', 'db/0009_guard.sql'], dir);
    expect(again.code).toBe(2);
    expect(again.err).toMatch(/already exists/);
    expect(fs.readFileSync(path.join(dir, 'db', '0009_guard.sql'), 'utf8')).toContain("CONSTANT name   := 'p';");
  });
});
