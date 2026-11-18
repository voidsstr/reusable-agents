/**
 * Opt-in integration test: the real database phase (azure/provision.sh database,
 * psql or psql-in-docker) against a THROWAWAY Postgres, as a non-superuser
 * CREATEROLE CREATEDB admin like Azure's flexible-server admin, then teardown
 * --with-database. Skipped unless both are set:
 *
 *   KIT_DEPLOY_IT_PG_PORT       port of a disposable server on 127.0.0.1
 *   KIT_DEPLOY_IT_PG_PASSWORD   its `postgres` superuser password
 *   KIT_DEPLOY_IT_PSQL_IMAGE    optional psql image when psql is not installed (default postgres:16-alpine)
 *
 *   docker run -d --rm --name kit-deploy-it-pg -e POSTGRES_PASSWORD=... -p 55432:5432 postgres:16-alpine
 *   KIT_DEPLOY_IT_PG_PORT=55432 KIT_DEPLOY_IT_PG_PASSWORD=... npx jest unit/deploy/postgres.it.test.ts
 *
 * It creates and drops the databases kitit, kitit_victimdb, kitit3 and the
 * roles kitit*_* / kit_it_admin only.
 */
import * as fs from 'fs';
import * as path from 'path';
import { Client } from 'pg';
import { makeSandbox, removeSandbox, runScript, setResponses, type Sandbox } from './harness';

const PORT = Number(process.env.KIT_DEPLOY_IT_PG_PORT || 0);
const SUPER_PW = process.env.KIT_DEPLOY_IT_PG_PASSWORD || '';
const run = PORT > 0 && SUPER_PW !== '';
const describeIt = run ? describe : describe.skip;

jest.setTimeout(120_000);

const HOST = '127.0.0.1';
const DB = 'kitit';
const ADMIN = { user: 'kit_it_admin', password: 'It-Admin-Password-0123456789' };
const ROLES = { owner: 'kitit_owner', app: 'kitit_app', agents: 'kitit_agents', publisher: 'kitit_publisher' } as const;
type Kind = keyof typeof ROLES;
const PW: Record<Kind, string> = {
  owner: 'It-Owner-Password-0123456789', app: 'It-App-Password-0123456789',
  agents: 'It-Agents-Password-0123456789', publisher: 'It-Publisher-Password-012345',
};
const dsn = (k: Kind) => `postgresql://${ROLES[k]}:${PW[k]}@${HOST}:${PORT}/${DB}?sslmode=disable`;
/** Objects that stand in for another site's on the shared server. */
const VICTIM = { role: 'kitit_victim', password: 'Victim-Password-0123456789', db: 'kitit_victimdb' };
const EXTRA_ROLES = [VICTIM.role, 'kitit2_owner', 'kitit2_app', 'kitit2_agents', 'kitit2_publisher',
  'kitit3_owner', 'kitit3_app', 'kitit3_agents', 'kitit3_publisher'];
const EXTRA_DBS = [VICTIM.db, 'kitit3'];

async function withClient<T>(user: string, password: string, database: string, fn: (c: Client) => Promise<T>): Promise<T> {
  const c = new Client({ host: HOST, port: PORT, user, password, database });
  await c.connect();
  try {
    return await fn(c);
  } finally {
    await c.end();
  }
}

const su = <T>(fn: (c: Client) => Promise<T>, database = 'postgres') => withClient('postgres', SUPER_PW, database, fn);
const as = <T>(k: Kind, fn: (c: Client) => Promise<T>) => withClient(ROLES[k], PW[k], DB, fn);

async function allowed(k: Kind, sql: string): Promise<boolean> {
  return as(k, async (c) => {
    try {
      await c.query(sql);
      return true;
    } catch (e) {
      if (/permission denied|must be owner/.test(String((e as Error).message))) return false;
      throw e;
    }
  });
}

describeIt('database phase against a real Postgres (opt-in)', () => {
  let sb: Sandbox;
  let envFile: string;
  const env = () => ({
    KIT_IT_ADMIN_PW: ADMIN.password,
    KIT_IT_DSN_OWNER: dsn('owner'), KIT_IT_DSN_APP: dsn('app'), KIT_IT_DSN_AGENTS: dsn('agents'), KIT_IT_DSN_PUBLISHER: dsn('publisher'),
    KIT_DEPLOY_PSQL_IMAGE: process.env.KIT_DEPLOY_IT_PSQL_IMAGE || 'postgres:16-alpine',
  });
  const provision = () => runScript(sb, 'azure/provision.sh', ['--env', envFile, 'database'], env());

  /** A deploy.env for another set of names (same APP_NAME), with DSN variables for them. */
  function variantEnv(name: string, set: Record<string, string>): string {
    let text = fs.readFileSync(envFile, 'utf8');
    for (const [k, v] of Object.entries(set)) {
      const re = new RegExp(`^${k}=.*$`, 'm');
      text = re.test(text) ? text.replace(re, `${k}=${v}`) : `${text}${k}=${v}\n`;
    }
    const f = path.join(sb.fx, name);
    fs.writeFileSync(f, text);
    return f;
  }

  async function cleanup() {
    await su(async (c) => {
      for (const d of [DB, ...EXTRA_DBS]) await c.query(`DROP DATABASE IF EXISTS ${d} WITH (FORCE)`);
      for (const r of [...Object.values(ROLES), ...EXTRA_ROLES, 'kitit_stranger', ADMIN.user]) await c.query(`DROP ROLE IF EXISTS ${r}`);
    });
  }

  beforeAll(async () => {
    await cleanup();
    await su(async (c) => {
      await c.query(`CREATE ROLE ${ADMIN.user} LOGIN CREATEROLE CREATEDB PASSWORD '${ADMIN.password}'`);
      await c.query(`CREATE ROLE kitit_stranger LOGIN PASSWORD 'stranger-password-0123456789'`);
    });
    sb = makeSandbox({ real: ['docker', 'psql'] });
    let text = fs.readFileSync(sb.envFile, 'utf8');
    const set = (k: string, v: string) => {
      const re = new RegExp(`^${k}=.*$`, 'm');
      text = re.test(text) ? text.replace(re, `${k}=${v}`) : `${text}${k}=${v}\n`;
    };
    set('DB_NAME', DB);
    set('DB_PORT', String(PORT));
    set('DB_SSLMODE', 'disable');
    set('AZ_DB_HOST', HOST);
    set('AZ_DB_ADMIN_USER', ADMIN.user);
    set('AZ_DB_ADMIN_PASSWORD', 'env:KIT_IT_ADMIN_PW');
    for (const k of Object.keys(ROLES) as Kind[]) {
      set(`DB_${k.toUpperCase()}_ROLE`, ROLES[k]);
      set(`DB_${k.toUpperCase()}_DSN`, `env:KIT_IT_DSN_${k.toUpperCase()}`);
    }
    set('APP_SECRETS', '"database-url=env:KIT_IT_DSN_APP"');
    envFile = path.join(sb.fx, 'it.env');
    fs.writeFileSync(envFile, text);
  });

  afterAll(async () => {
    if (sb) removeSandbox(sb);
    await cleanup();
  });

  it('creates the roles and the database as a non-superuser admin', async () => {
    const r = provision();
    expect(r.stderr).not.toMatch(/ERROR/);
    expect(r.status).toBe(0);
    for (const secret of [...Object.values(PW), ADMIN.password]) expect(r.all).not.toContain(secret);
    expect(r.all).not.toContain('SCRAM-SHA-256$');
    const roles = await su((c) => c.query(
      `SELECT rolname, rolconnlimit, rolcanlogin, rolsuper OR rolcreaterole OR rolcreatedb OR rolreplication OR rolbypassrls AS powerful
         FROM pg_roles WHERE rolname LIKE 'kitit\\_%' AND rolname <> 'kitit_stranger' ORDER BY rolname`));
    expect(roles.rows).toEqual([
      { rolname: 'kitit_agents', rolconnlimit: 10, rolcanlogin: true, powerful: false },
      { rolname: 'kitit_app', rolconnlimit: 20, rolcanlogin: true, powerful: false },
      { rolname: 'kitit_owner', rolconnlimit: 3, rolcanlogin: true, powerful: false },
      { rolname: 'kitit_publisher', rolconnlimit: 2, rolcanlogin: true, powerful: false },
    ]);
    const settings = await su((c) => c.query(
      `SELECT r.rolname, s.setconfig FROM pg_db_role_setting s JOIN pg_roles r ON r.oid = s.setrole
        WHERE r.rolname LIKE 'kitit\\_%' AND s.setdatabase = 0 ORDER BY r.rolname`));
    const byRole = Object.fromEntries(settings.rows.map((row: { rolname: string; setconfig: string[] }) => [row.rolname, row.setconfig.sort()]));
    expect(byRole).toEqual({
      kitit_agents: ['idle_in_transaction_session_timeout=60s', 'statement_timeout=5min'],
      kitit_app: ['idle_in_transaction_session_timeout=60s', 'statement_timeout=15s'],
      kitit_owner: ['idle_in_transaction_session_timeout=60s', 'statement_timeout=15min'],
      kitit_publisher: ['idle_in_transaction_session_timeout=60s', 'statement_timeout=30s'],
    });
    const owner = await su((c) => c.query(`SELECT pg_get_userbyid(datdba) AS owner FROM pg_database WHERE datname = $1`, [DB]));
    expect(owner.rows[0].owner).toBe('kitit_owner');
    const stranger = await su((c) => c.query(`SELECT has_database_privilege('kitit_stranger', $1, 'CONNECT') AS ok`, [DB]));
    expect(stranger.rows[0].ok).toBe(false);
  });

  it('every role logs in with the password from its DSN (the SCRAM verifiers are right) and gets its timeout', async () => {
    for (const k of Object.keys(ROLES) as Kind[]) {
      const t = await as(k, (c) => c.query('SHOW statement_timeout'));
      expect(t.rows[0].statement_timeout).toBe({ owner: '15min', app: '15s', agents: '5min', publisher: '30s' }[k]);
    }
    await expect(withClient(ROLES.app, `${PW.app}x`, DB, async () => 0)).rejects.toThrow(/password authentication failed/);
  });

  it('default privileges cover tables the owner creates later; named grants wait for a re-run', async () => {
    await as('owner', async (c) => {
      await c.query(`CREATE TABLE outbound_clicks (id bigserial PRIMARY KEY, asin text, clicked_at timestamptz DEFAULT now())`);
      await c.query(`CREATE TABLE ai_traffic_log (id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY, path text)`);
      await c.query(`CREATE TABLE editorial_articles (id serial PRIMARY KEY, slug text UNIQUE, status text DEFAULT 'draft', published_at timestamptz, body_md text)`);
      await c.query(`CREATE TABLE products (id serial PRIMARY KEY, asin text)`);
      await c.query(`INSERT INTO products (asin) VALUES ('B000000001')`);
      await c.query(`INSERT INTO editorial_articles (slug) VALUES ('first')`);
    });
    expect(await allowed('app', 'SELECT * FROM products')).toBe(true);
    expect(await allowed('agents', `INSERT INTO products (asin) VALUES ('B000000002')`)).toBe(true);
    expect(await allowed('app', `INSERT INTO outbound_clicks (asin) VALUES ('B000000001')`)).toBe(false);
  });

  it('after a re-run every role has exactly its grants', async () => {
    const r = provision();
    expect(r.status).toBe(0);
    expect(r.stderr).not.toContain('does not exist yet');
    // app: read everything, insert clicks and AI log only
    expect(await allowed('app', 'SELECT * FROM products')).toBe(true);
    expect(await allowed('app', 'SELECT * FROM editorial_articles')).toBe(true);
    expect(await allowed('app', `INSERT INTO outbound_clicks (asin) VALUES ('B000000001')`)).toBe(true);
    expect(await allowed('app', `INSERT INTO ai_traffic_log (path) VALUES ('/')`)).toBe(true);
    expect(await allowed('app', `UPDATE outbound_clicks SET asin = 'x'`)).toBe(false);
    expect(await allowed('app', 'DELETE FROM outbound_clicks')).toBe(false);
    expect(await allowed('app', `INSERT INTO products (asin) VALUES ('B000000003')`)).toBe(false);
    expect(await allowed('app', 'CREATE TABLE nope (x int)')).toBe(false);
    // agents: DML everywhere, no DDL
    expect(await allowed('agents', `UPDATE products SET asin = asin`)).toBe(true);
    expect(await allowed('agents', `DELETE FROM products WHERE asin = 'B000000002'`)).toBe(true);
    expect(await allowed('agents', `INSERT INTO outbound_clicks (asin) VALUES ('B000000004')`)).toBe(true);
    expect(await allowed('agents', 'CREATE TABLE nope (x int)')).toBe(false);
    // publisher: editorial_articles only, and of it only the promotion columns
    expect(await allowed('publisher', `UPDATE editorial_articles SET status = 'published', published_at = now() WHERE slug = 'first'`)).toBe(true);
    expect(await allowed('publisher', `UPDATE editorial_articles SET body_md = 'rewritten' WHERE slug = 'first'`)).toBe(false);
    expect(await allowed('publisher', `UPDATE editorial_articles SET slug = 'renamed'`)).toBe(false);
    expect(await allowed('publisher', 'SELECT * FROM products')).toBe(false);
    expect(await allowed('publisher', `INSERT INTO editorial_articles (slug) VALUES ('x')`)).toBe(false);
  });

  it('marks every role and the database as this site\'s', async () => {
    const marks = await su((c) => c.query(
      `SELECT r.rolname AS name, shobj_description(r.oid, 'pg_authid') AS mark FROM pg_roles r WHERE r.rolname = ANY($1)
       UNION ALL SELECT d.datname, shobj_description(d.oid, 'pg_database') FROM pg_database d WHERE d.datname = $2 ORDER BY 1`,
      [Object.values(ROLES), DB]));
    expect(marks.rows).toEqual([DB, ...Object.values(ROLES)].sort().map((name) => ({ name, mark: 'affiliate-kit app=kitexample' })));
  });

  it('a database an interrupted first run left unmarked counts as this site\'s when its (marked) owner role owns it', async () => {
    await su((c) => c.query(`COMMENT ON DATABASE ${DB} IS NULL`));
    const r = provision();
    expect(r.status).toBe(0);
    const m = await su((c) => c.query(`SELECT shobj_description(oid, 'pg_database') AS mark FROM pg_database WHERE datname = $1`, [DB]));
    expect(m.rows[0].mark).toBe('affiliate-kit app=kitexample');
  });

  it('refuses to take over another site\'s role: nothing changes, the role keeps its password and rights', async () => {
    await su((c) => c.query(`CREATE ROLE ${VICTIM.role} LOGIN CREATEDB CONNECTION LIMIT 7 PASSWORD '${VICTIM.password}'`));
    const f = variantEnv('victim-role.env', { DB_APP_ROLE: VICTIM.role, DB_APP_DSN: 'env:KIT_IT_DSN_VICTIM' });
    const r = runScript(sb, 'azure/provision.sh', ['--env', f, 'database'],
      { ...env(), KIT_IT_DSN_VICTIM: `postgresql://${VICTIM.role}:Attacker-Password-0123456789@${HOST}:${PORT}/${DB}?sslmode=disable` });
    expect(r.status).toBe(1);
    expect(r.stderr).toContain(`not created by the kit for this site, so nothing was changed: role ${VICTIM.role} (no affiliate-kit marker)`);
    expect(r.all).not.toContain('Attacker-Password-0123456789');
    const v = await su((c) => c.query(`SELECT rolcreatedb, rolconnlimit FROM pg_roles WHERE rolname = $1`, [VICTIM.role]));
    expect(v.rows[0]).toEqual({ rolcreatedb: true, rolconnlimit: 7 });
    await withClient(VICTIM.role, VICTIM.password, 'postgres', async () => 0);
    // the kit's own roles were not touched either (the roles transaction never ran)
    await as('owner', (c) => c.query('SELECT 1'));
  });

  it('refuses another site\'s database: no role is created, nothing on it changes, and teardown will not drop it', async () => {
    await su((c) => c.query(`CREATE DATABASE ${VICTIM.db} OWNER kitit_stranger`));
    const names = { DB_NAME: VICTIM.db, DB_OWNER_ROLE: 'kitit2_owner', DB_APP_ROLE: 'kitit2_app', DB_AGENTS_ROLE: 'kitit2_agents',
      DB_PUBLISHER_ROLE: 'kitit2_publisher', DB_OWNER_DSN: '', DB_APP_DSN: '', DB_AGENTS_DSN: '', DB_PUBLISHER_DSN: '' };
    const f = variantEnv('victim-db.env', names);
    const r = runScript(sb, 'azure/provision.sh', ['--env', f, 'database'], env());
    expect(r.status).toBe(1);
    expect(r.stderr).toContain(`database ${VICTIM.db} (owner kitit_stranger, no affiliate-kit marker)`);
    const after = await su((c) => c.query(
      `SELECT pg_get_userbyid(datdba) AS owner, has_database_privilege('kitit_stranger', datname, 'CONNECT') AS stranger_connect,
              has_database_privilege('public', datname, 'CONNECT') AS public_connect,
              (SELECT count(*) FROM pg_roles WHERE rolname LIKE 'kitit2\\_%')::int AS kit_roles
         FROM pg_database WHERE datname = $1`, [VICTIM.db]));
    expect(after.rows[0]).toEqual({ owner: 'kitit_stranger', stranger_connect: true, public_connect: true, kit_roles: 0 });

    setResponses(sb, [{ match: 'az account show', stdout: '00000000-0000-4000-8000-000000000000\n' }]);
    const t = runScript(sb, 'azure/teardown.sh', ['--env', f, '--confirm', 'kitexample', '--with-database', '--confirm-database', VICTIM.db], env());
    expect(t.status).toBe(1);
    expect(t.stderr).toContain(`(teardown, app kitexample): not created by the kit for this site`);
    const still = await su((c) => c.query(`SELECT count(*)::int AS n FROM pg_database WHERE datname = $1`, [VICTIM.db]));
    expect(still.rows[0].n).toBe(1);
  });

  it('--adopt-existing takes over objects without a kit marker, never another kit site\'s', async () => {
    // created by the admin before the kit ran (so the admin may administer it)
    await withClient(ADMIN.user, ADMIN.password, 'postgres', (c) => c.query(`CREATE ROLE kitit3_app LOGIN CONNECTION LIMIT 1`));
    const kit3 = (k: string) => `postgresql://kitit3_${k}:It3-${k}-Password-0123456789@${HOST}:${PORT}/kitit3?sslmode=disable`;
    const f = variantEnv('adopt.env', { DB_NAME: 'kitit3', DB_OWNER_ROLE: 'kitit3_owner', DB_APP_ROLE: 'kitit3_app', DB_AGENTS_ROLE: 'kitit3_agents',
      DB_PUBLISHER_ROLE: 'kitit3_publisher', DB_OWNER_DSN: 'env:KIT3_OWNER', DB_APP_DSN: 'env:KIT3_APP', DB_AGENTS_DSN: 'env:KIT3_AGENTS',
      DB_PUBLISHER_DSN: 'env:KIT3_PUBLISHER' });
    const env3 = { ...env(), KIT3_OWNER: kit3('owner'), KIT3_APP: kit3('app'), KIT3_AGENTS: kit3('agents'), KIT3_PUBLISHER: kit3('publisher') };
    const refused = runScript(sb, 'azure/provision.sh', ['--env', f, 'database'], env3);
    expect(refused.status).toBe(1);
    expect(refused.stderr).toContain('role kitit3_app (no affiliate-kit marker)');
    const adopted = runScript(sb, 'azure/provision.sh', ['--env', f, 'database', '--adopt-existing'], env3);
    expect(adopted.stderr).not.toMatch(/ERROR/);
    expect(adopted.status).toBe(0);
    const m = await su((c) => c.query(`SELECT shobj_description(oid, 'pg_authid') AS mark, rolconnlimit FROM pg_roles WHERE rolname = 'kitit3_app'`));
    expect(m.rows[0]).toEqual({ mark: 'affiliate-kit app=kitexample', rolconnlimit: 20 });
    await withClient('kitit3_app', 'It3-app-Password-0123456789', 'kitit3', async () => 0);
    // another kit site's marker is never adopted
    await withClient(ADMIN.user, ADMIN.password, 'postgres', (c) => c.query(`COMMENT ON ROLE kitit3_app IS 'affiliate-kit app=othersite'`));
    const other = runScript(sb, 'azure/provision.sh', ['--env', f, 'database', '--adopt-existing'], env3);
    expect(other.status).toBe(1);
    expect(other.stderr).toContain('role kitit3_app (affiliate-kit app=othersite)');
  });

  it('grants converge: a privilege removed from deploy.env is revoked', async () => {
    fs.writeFileSync(envFile, fs.readFileSync(envFile, 'utf8').replace(/^DB_PUBLISHER_GRANTS=.*$/m, '') + 'DB_PUBLISHER_GRANTS=SELECT:editorial_articles\n');
    const r = provision();
    expect(r.status).toBe(0);
    expect(await allowed('publisher', `UPDATE editorial_articles SET status = 'draft'`)).toBe(false);
    expect(await allowed('publisher', 'SELECT * FROM editorial_articles')).toBe(true);
  });

  it('teardown --with-database drops the database and the four roles', async () => {
    setResponses(sb, [{ match: 'az account show', stdout: '00000000-0000-4000-8000-000000000000\n' }]);
    const r = runScript(sb, 'azure/teardown.sh', ['--env', envFile, '--confirm', 'kitexample', '--with-database', '--confirm-database', DB], env());
    expect(r.status).toBe(0);
    const left = await su((c) => c.query(
      `SELECT (SELECT count(*) FROM pg_database WHERE datname = $1)::int AS dbs,
              (SELECT count(*) FROM pg_roles WHERE rolname = ANY($2))::int AS roles`, [DB, Object.values(ROLES)]));
    expect(left.rows[0]).toEqual({ dbs: 0, roles: 0 });
  });
});

if (!run) {
  it('postgres integration test skipped (set KIT_DEPLOY_IT_PG_PORT and KIT_DEPLOY_IT_PG_PASSWORD)', () => {
    expect(run).toBe(false);
  });
}
