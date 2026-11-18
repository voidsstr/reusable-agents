/**
 * Drift between the kit's database layer and the SpecPicks code and schema it
 * was ported from (unit/db/fixtures/ported-from-db.json):
 *
 *   - the migration runner's declarations (src/db/migrationRunner.ts and the
 *     boot path's killStaleTransactions), hashed exactly as PORTED_FROM.json
 *     hashes the K1 ports;
 *   - the upstream migration files that define outbound_clicks, ai_traffic_log,
 *     editorial_articles and the verified-human views: a new one means the
 *     reference schema moved and kit_0001..kit_0004 may need a successor.
 *
 * Reported, not failed, unless KIT_DRIFT_STRICT=1 (the reference site's agents
 * write migrations daily; the kit gate must not go red on their schedule).
 * Knobs as in unit/drift.test.ts: KIT_DRIFT_UPSTREAM=<checkout>,
 * KIT_DRIFT_REF=<commit> (read `git show <commit>:<file>` instead of the
 * working tree), KIT_DRIFT_STRICT=1; KIT_DB_DRIFT_REPORT=<file> writes the
 * computed hashes and schema files (to refresh the fixture after a re-port).
 */
import { execFileSync } from 'child_process';
import * as fs from 'fs';
import * as path from 'path';
import { splitSqlStatements } from '../../server/src/db/migrate';
import { extractSymbolText, hashText } from './helpers/declHash';

interface Entry { upstreamFile: string; symbol: string; kitFile: string; kitSymbol: string; note?: string; sha256: string }
interface Fixture {
  upstream: { repo: string; localPath: string; commit: string; designReferenceCommit: string; headAtPort: string; portedOn: string };
  entries: Entry[];
  schemaSources: { dir: string; tables: string[]; views: string[]; files: string[] };
}

const KIT = path.resolve(__dirname, '..', '..');
const FIX: Fixture = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures', 'ported-from-db.json'), 'utf8'));
const UPSTREAM = process.env.KIT_DRIFT_UPSTREAM || FIX.upstream.localPath;
const REF = process.env.KIT_DRIFT_REF || '';
const STRICT = process.env.KIT_DRIFT_STRICT === '1';
const upstreamPresent = fs.existsSync(path.join(UPSTREAM, '.git')) || fs.existsSync(path.join(UPSTREAM, 'src'));

function git(args: string[]): string | null {
  try {
    return execFileSync('git', ['-C', UPSTREAM, ...args], { encoding: 'utf8', maxBuffer: 64 * 1024 * 1024, stdio: ['ignore', 'pipe', 'ignore'] });
  } catch {
    return null;
  }
}

function upstreamFile(rel: string, ref = REF): string | null {
  if (ref) return git(['show', `${ref}:${rel}`]);
  try {
    return fs.readFileSync(path.join(UPSTREAM, rel), 'utf8');
  } catch {
    return null;
  }
}

/** True when a statement changes the schema of a table or view the kit mirrors. */
function definesKitRelation(code: string, tables: string[], views: string[]): boolean {
  const c = code.replace(/\s+/g, ' ').trim();
  const T = `(?:public\\.)?(?:${tables.join('|')})\\b`;
  const V = `(?:public\\.)?(?:${views.join('|')})\\b`;
  return [
    new RegExp(`^ALTER TABLE (?:IF EXISTS )?(?:ONLY )?${T}`, 'i'),
    new RegExp(`^CREATE (?:UNLOGGED )?TABLE (?:IF NOT EXISTS )?${T}`, 'i'),
    new RegExp(`^CREATE (?:UNIQUE )?INDEX (?:CONCURRENTLY )?(?:IF NOT EXISTS )?\\w* ?ON (?:ONLY )?${T}`, 'i'),
    new RegExp(`^CREATE (?:OR REPLACE )?(?:MATERIALIZED )?VIEW (?:IF NOT EXISTS )?${V}`, 'i'),
    new RegExp(`^(?:CREATE (?:OR REPLACE )?(?:CONSTRAINT )?TRIGGER|DROP TRIGGER) .*? ON ${T}`, 'i'),
    new RegExp(`^DROP (?:TABLE|VIEW|MATERIALIZED VIEW) (?:IF EXISTS )?(?:${T}|${V})`, 'i'),
  ].some((re) => re.test(c));
}

/** Upstream migration files (top level of the dir) with such a statement. */
function schemaFilesUpstream(): string[] {
  const { dir, tables, views } = FIX.schemaSources;
  const mention = new RegExp([...tables, ...views].join('|'), 'i');
  let names: string[];
  if (REF) {
    const listed = git(['ls-tree', '--name-only', REF, `${dir}/`]) ?? '';
    names = listed.split('\n').filter((p) => /\.sql$/.test(p)).map((p) => path.basename(p));
  } else {
    names = fs.readdirSync(path.join(UPSTREAM, dir)).filter((f) => f.endsWith('.sql'));
  }
  const out: string[] = [];
  for (const name of names.sort()) {
    const sql = upstreamFile(`${dir}/${name}`);
    if (!sql || !mention.test(sql)) continue;
    if (splitSqlStatements(sql).some((s) => definesKitRelation(s.code, tables, views))) out.push(name);
  }
  return out;
}

describe('ported-from-db.json', () => {
  it('names the port commit, the design commit and a hash per entry', () => {
    expect(FIX.upstream.commit).toMatch(/^[0-9a-f]{40}$/);
    expect(FIX.upstream.designReferenceCommit).toMatch(/^[0-9a-f]{40}$/);
    for (const e of FIX.entries) expect(e.sha256).toMatch(/^[0-9a-f]{64}$/);
    const keys = FIX.entries.map((e) => `${e.upstreamFile}#${e.symbol}`);
    expect(new Set(keys).size).toBe(keys.length);
  });

  it('points every entry at a kit file that still holds its kit symbol', () => {
    const missing = FIX.entries.filter((e) => {
      const file = path.join(KIT, e.kitFile);
      return !fs.existsSync(file) || !new RegExp(`\\b${e.kitSymbol}\\b`).test(fs.readFileSync(file, 'utf8'));
    }).map((e) => `${e.kitFile}#${e.kitSymbol}`);
    expect(missing).toEqual([]);
  });

  it('lists the upstream schema sources in apply order', () => {
    expect(FIX.schemaSources.files.length).toBeGreaterThan(5);
    expect([...FIX.schemaSources.files].sort()).toEqual(FIX.schemaSources.files);
  });

  it('recognises schema statements and ignores look-alikes', () => {
    const t = FIX.schemaSources.tables;
    const v = FIX.schemaSources.views;
    expect(definesKitRelation('ALTER TABLE outbound_clicks ADD COLUMN IF NOT EXISTS x int', t, v)).toBe(true);
    expect(definesKitRelation('CREATE INDEX IF NOT EXISTS i ON editorial_articles (a)', t, v)).toBe(true);
    expect(definesKitRelation('create or replace view outbound_clicks_human as select 1', t, v)).toBe(true);
    expect(definesKitRelation('CREATE TRIGGER x BEFORE UPDATE ON editorial_articles FOR EACH ROW EXECUTE FUNCTION f()', t, v)).toBe(true);
    expect(definesKitRelation('CREATE TABLE editorial_articles_backup_20260925 AS SELECT * FROM editorial_articles', t, v)).toBe(false);
    expect(definesKitRelation("UPDATE editorial_articles SET body_md = 'x'", t, v)).toBe(false);
  });
});

const describeUpstream = upstreamPresent ? describe : describe.skip;

describeUpstream(`db drift against the SpecPicks checkout (${UPSTREAM}${REF ? ` @ ${REF}` : ''})`, () => {
  it('hashes exactly as PORTED_FROM.json does (checked on K1 entries at their recorded commit)', () => {
    const k1 = JSON.parse(fs.readFileSync(path.join(KIT, 'PORTED_FROM.json'), 'utf8')) as { upstream: { commit: string }; entries: Entry[] };
    const sample = k1.entries.filter((e) => /dbBudget|affiliateOffer|verifiedClicks/.test(e.upstreamFile)).slice(0, 8);
    expect(sample.length).toBeGreaterThan(0);
    const checked = sample.map((e) => {
      const src = upstreamFile(e.upstreamFile, k1.upstream.commit);
      const text = src == null ? null : extractSymbolText(src, e.symbol, e.upstreamFile);
      return { key: `${e.upstreamFile}#${e.symbol}`, same: text != null && hashText(text) === e.sha256 };
    }).filter((r) => r.same === false);
    expect(checked).toEqual([]);
  });

  const results = FIX.entries.map((e) => {
    const src = upstreamFile(e.upstreamFile);
    if (src == null) return { e, status: 'missing-file', now: null as string | null };
    const text = extractSymbolText(src, e.symbol, e.upstreamFile);
    if (text == null) return { e, status: 'missing-symbol', now: null };
    const now = hashText(text);
    return { e, status: now === e.sha256 ? 'same' : 'changed', now };
  });

  it('reports runner drift (fails only with KIT_DRIFT_STRICT=1)', () => {
    const drifted = results.filter((r) => r.status !== 'same');
    if (drifted.length) {
      // eslint-disable-next-line no-console
      console.warn([
        `affiliate-site kit db: ${drifted.length} of ${results.length} ported runner declarations changed since ${FIX.upstream.commit.slice(0, 8)}.`,
        'Review them and port the fix into server/src/db/migrate.ts, then refresh unit/db/fixtures/ported-from-db.json:',
        ...drifted.map((r) => `  ${r.status.padEnd(14)} ${r.e.upstreamFile}#${r.e.symbol} -> kit ${r.e.kitFile}#${r.e.kitSymbol}`),
      ].join('\n'));
    }
    if (STRICT) expect(drifted.map((r) => `${r.e.upstreamFile}#${r.e.symbol}: ${r.status}`)).toEqual([]);
  });

  it('reports upstream schema migrations the kit has not mirrored (fails only with KIT_DRIFT_STRICT=1)', () => {
    const now = schemaFilesUpstream();
    const added = now.filter((f) => !FIX.schemaSources.files.includes(f));
    const removed = FIX.schemaSources.files.filter((f) => !now.includes(f));
    if (process.env.KIT_DB_DRIFT_REPORT) {
      fs.writeFileSync(process.env.KIT_DB_DRIFT_REPORT, `${JSON.stringify({
        upstream: UPSTREAM,
        ref: REF || 'working-tree',
        entries: results.map((r) => ({ upstreamFile: r.e.upstreamFile, symbol: r.e.symbol, status: r.status, sha256: r.now })),
        schemaFiles: now,
      }, null, 2)}\n`);
    }
    if (added.length || removed.length) {
      // eslint-disable-next-line no-console
      console.warn([
        'affiliate-site kit db: the reference schema for outbound_clicks / ai_traffic_log / editorial_articles / the verified-human views moved.',
        'Mirror it in a new kit migration (never edit an applied one) and refresh unit/db/fixtures/ported-from-db.json and upstream-schema.json:',
        ...added.map((f) => `  new      ${FIX.schemaSources.dir}/${f}`),
        ...removed.map((f) => `  gone     ${FIX.schemaSources.dir}/${f}`),
      ].join('\n'));
    }
    if (STRICT) expect({ added, removed }).toEqual({ added: [], removed: [] });
  });
});
