/**
 * azure/{provision,deploy,teardown}.sh under --dry-run against the sample
 * deploy.env: the exact az / docker / psql command lines, the SQL, and the
 * guards. The sandbox's az, docker, psql, curl and git are recording stubs;
 * a dry run must not call any of them.
 */
import * as fs from 'fs';
import * as path from 'path';
import {
  cloudCalls, dryCommands, expectNoSecrets, makeSandbox, oneCommand, removeSandbox, runScript, setResponses, sqlBlocks, type RunResult,
  type Sandbox,
} from './harness';

/** The kit release the scripts label images and tag apps with (its VERSION file). */
const KIT_VERSION = fs.readFileSync(path.join(__dirname, '..', '..', 'VERSION'), 'utf8').trim();

let sb: Sandbox;
beforeAll(() => {
  sb = makeSandbox();
});
afterAll(() => removeSandbox(sb));

function dry(script: string, args: string[], env: Record<string, string> = {}): RunResult {
  const r = runScript(sb, script, ['--env', sb.envFile, '--dry-run', ...args], env);
  expect(cloudCalls(r.calls)).toEqual([]);
  expectNoSecrets(r.all);
  return r;
}

function editEnv(edit: (text: string) => string): string {
  const f = path.join(sb.fx, `deploy-${Math.random().toString(36).slice(2)}.env`);
  fs.writeFileSync(f, edit(fs.readFileSync(sb.envFile, 'utf8')));
  return f;
}

describe('azure/provision.sh --dry-run all (fresh: nothing exists)', () => {
  let r: RunResult;
  beforeAll(() => {
    r = runScript(sb, 'azure/provision.sh', ['--env', sb.envFile, '--dry-run', 'all']);
  });

  it('runs check, database, app, secrets, domain, calls nothing and leaks nothing', () => {
    expect(r.status).toBe(0);
    expect(cloudCalls(r.calls)).toEqual([]);
    expectNoSecrets(r.all);
    for (const p of ['check', 'database', 'app', 'secrets', 'domain']) expect(r.stdout).toContain(`== ${p} ==`);
  });

  it('creates the Container App at 0.5 vCPU / 1 GiB, 1..3 replicas, secrets as @file, env by secretref', () => {
    const create = oneCommand(r.stdout, 'az containerapp create ');
    for (const part of [
      '--name kitexample', '--resource-group kit-test-rg-does-not-exist', '--environment kit-test-env-does-not-exist',
      '--image kittestacrdoesnotexist.azurecr.io/kitexample:latest', '--registry-server kittestacrdoesnotexist.azurecr.io',
      '--ingress external', '--target-port 8080', '--transport auto', '--revisions-mode single',
      '--cpu 0.5', '--memory 1Gi', '--min-replicas 1', '--max-replicas 3',
      "--secrets 'database-url=@<secret-dir>/database-url'",
      '--env-vars PORT=8080 NODE_ENV=production PG_POOL_MAX=5 SITE_ID=kit-example AI_TRAFFIC_EXCLUDE_IPS=198.51.100.7 DATABASE_URL=secretref:database-url',
      '-o none',
    ]) expect(create).toContain(part);
    expect(create).not.toMatch(/--registry-password|--registry-username/);
    expect(r.stdout).toContain('the app was created with its secrets in this run');
  });

  it('creates four roles, each with a CONNECTION LIMIT and a statement_timeout, as SCRAM verifiers', () => {
    const [cluster, db] = sqlBlocks(r.stdout);
    for (const [role, limit, timeout] of [
      ['kitexample_owner', 3, '15min'], ['kitexample_app', 20, '15s'],
      ['kitexample_agents', 10, '5min'], ['kitexample_publisher', 2, '30s'],
    ] as const) {
      expect(cluster).toContain(`SELECT 'CREATE ROLE "${role}" WITH LOGIN NOCREATEDB NOCREATEROLE CONNECTION LIMIT ${limit}'`);
      expect(cluster).toContain(`ALTER ROLE "${role}" WITH LOGIN NOCREATEDB NOCREATEROLE CONNECTION LIMIT ${limit};`);
      expect(cluster).toContain(`ALTER ROLE "${role}" SET statement_timeout = '${timeout}';`);
      expect(cluster).toContain(`ALTER ROLE "${role}" SET idle_in_transaction_session_timeout = '60s';`);
      expect(cluster).toMatch(new RegExp(`ALTER ROLE "${role}" WITH PASSWORD '<scram-sha-256 verifier of the password in env:KIT_TEST_DATABASE_URL[A-Z_]*>';`));
    }
    expect(cluster).not.toMatch(/SUPERUSER|REPLICATION|BYPASSRLS|SCRAM-SHA-256\$/);
    expect(cluster).toContain(`CREATE DATABASE "kitexample" WITH OWNER "kitexample_owner"`);
    expect(cluster).toContain('REVOKE ALL ON DATABASE "kitexample" FROM PUBLIC;');
    expect(cluster).toContain('GRANT CONNECT ON DATABASE "kitexample" TO "kitexample_app", "kitexample_agents", "kitexample_publisher";');
    expect(cluster).toContain("RAISE EXCEPTION 'affiliate-kit: roles without a statement_timeout: %', bad;");
    expect(r.stdout).toContain("psql 'host=<db-host> port=5432 dbname=postgres user=kitadmin sslmode=require");
    expect(r.stdout).toContain('PGPASSWORD=<env:KIT_TEST_PG_ADMIN_PASSWORD>');
    // DB_CONNECTION_LIMIT unset: the database's own limit is left as the server has it.
    expect(cluster).not.toMatch(/ALTER DATABASE "kitexample" WITH CONNECTION LIMIT/);
    expect(cluster).not.toContain('datconnlimit');
    expect(r.stdout).toContain('connection limits: roles owner 3, app 20, agents 10, publisher 2 (sum 35); database left as the server has it (DB_CONNECTION_LIMIT unset)');

    expect(db).toMatch(/^-- affiliate-kit: privileges inside database "kitexample"/);
    // Schema public goes to the owner role first when another role owns it (Azure: azure_pg_admin),
    // never when the owner or pg_database_owner (stock PostgreSQL 15+) already does; then the owner converges it.
    expect(db).toContain(`IF cur IS NOT NULL AND cur <> '"kitexample_owner"'::regrole`);
    expect(db).toContain("AND (to_regrole('pg_database_owner') IS NULL OR cur <> to_regrole('pg_database_owner')) THEN");
    expect(db).toContain('    ALTER SCHEMA public OWNER TO "kitexample_owner";');
    expect(db.indexOf('ALTER SCHEMA public OWNER TO')).toBeGreaterThan(db.indexOf('BEGIN;'));
    expect(db.indexOf('ALTER SCHEMA public OWNER TO')).toBeLessThan(db.indexOf('SET LOCAL ROLE "kitexample_owner";'));
    expect(db).toContain('$kit$;\nSET LOCAL ROLE "kitexample_owner";\nREVOKE ALL ON SCHEMA public FROM PUBLIC;');
    expect(db).toContain('GRANT SELECT ON ALL TABLES IN SCHEMA public TO "kitexample_app";');
    expect(db).toContain("EXECUTE format('GRANT INSERT ON public.%I TO %I', 'outbound_clicks', 'kitexample_app');");
    expect(db).toContain("EXECUTE format('GRANT INSERT ON public.%I TO %I', 'ai_traffic_log', 'kitexample_app');");
    expect(db).toContain('GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO "kitexample_agents";');
    expect(db).toContain('ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO "kitexample_agents";');
    // The publisher promotes drafts: SELECT, and UPDATE of status and published_at only.
    expect(db).toContain(`EXECUTE format('GRANT SELECT, UPDATE ("status", "published_at") ON public.%I TO %I', 'editorial_articles', 'kitexample_publisher');`);
    // The publisher gets editorial_articles only; the app no write on every table.
    expect(db).not.toMatch(/^(GRANT|ALTER DEFAULT PRIVILEGES).*TO "kitexample_publisher";$/m);
    expect(db).not.toMatch(/^GRANT [A-Z, ]*(INSERT|UPDATE|DELETE)[A-Z, ]* ON ALL TABLES IN SCHEMA public TO "kitexample_app";$/m);
    expect(db.trimEnd().endsWith('COMMIT;')).toBe(true);
  });

  it('refuses another site\'s roles or database before changing anything, and marks what it creates', () => {
    const [cluster] = sqlBlocks(r.stdout);
    // The ownership check is the first statement after the session setting.
    const check = cluster.indexOf("marker constant text := 'affiliate-kit app=kitexample';");
    expect(check).toBeGreaterThan(0);
    for (const later of ['CREATE ROLE', 'ALTER ROLE', 'COMMENT ON', 'GRANT ', 'ALTER DATABASE', 'REVOKE ']) {
      expect(cluster.indexOf(later)).toBeGreaterThan(check);
    }
    expect(cluster).toContain('adopt constant boolean := false;');
    expect(cluster).toContain("RAISE EXCEPTION 'affiliate-kit (%, app kitexample): not created by the kit for this site, so nothing was changed: %', 'provision', strangers");
    expect(cluster).toContain("WHERE r.rolname IN ('kitexample_owner', 'kitexample_app', 'kitexample_agents', 'kitexample_publisher')");
    // Roles are created and marked in one transaction; the database is marked right after CREATE DATABASE.
    const begin = cluster.indexOf('\nBEGIN;\n');
    const commit = cluster.indexOf('\nCOMMIT;\n');
    expect(begin).toBeGreaterThan(check);
    for (const role of ['kitexample_owner', 'kitexample_app', 'kitexample_agents', 'kitexample_publisher']) {
      const at = cluster.indexOf(`COMMENT ON ROLE "${role}" IS 'affiliate-kit app=kitexample';`);
      expect(at).toBeGreaterThan(begin);
      expect(at).toBeLessThan(commit);
    }
    const createDb = cluster.indexOf('CREATE DATABASE "kitexample"');
    expect(createDb).toBeGreaterThan(commit);
    expect(cluster.indexOf(`COMMENT ON DATABASE "kitexample" IS 'affiliate-kit app=kitexample';`)).toBeGreaterThan(createDb);
    expect(cluster).toContain("RAISE EXCEPTION 'affiliate-kit: database kitexample lacks the marker %'");
  });

  it('prints the DNS records and binds managed certificates (HTTP for the apex, CNAME for www)', () => {
    expect(r.stdout).toMatch(/A +kit-example\.test +<static-ip> +\(or a CNAME to <fqdn>, flattened at the apex\)/);
    expect(r.stdout).toMatch(/TXT +asuid\.kit-example\.test +<verification-id>/);
    expect(r.stdout).toMatch(/CNAME +www\.kit-example\.test +<fqdn>/);
    expect(r.stdout).toMatch(/TXT +asuid\.www\.kit-example\.test +<verification-id>/);
    expect(r.stdout).toContain('CAA kit-example.test 0 issue "digicert.com"');
    expect(r.stdout).toContain('"DNS only"');
    expect(oneCommand(r.stdout, 'az containerapp hostname add --hostname kit-example.test ')).toContain('--name kitexample');
    expect(oneCommand(r.stdout, 'az containerapp hostname bind --hostname kit-example.test '))
      .toContain('--environment kit-test-env-does-not-exist --validation-method HTTP -o none');
    expect(oneCommand(r.stdout, 'az containerapp hostname bind --hostname www.kit-example.test '))
      .toContain('--validation-method CNAME');
    const checks = dryCommands(r.stdout).filter((l) => l.includes('deploytool.py dns-check'));
    expect(checks).toHaveLength(2);
    expect(checks[0]).toContain("--expect-txt 'asuid.kit-example.test=<verification-id>' --expect-a '<static-ip>'");
    expect(checks[1]).toContain("--expect-cname '<fqdn>'");
  });
});

describe('azure/provision.sh database: DB_CONNECTION_LIMIT', () => {
  it('sets the database\'s own CONNECTION LIMIT once it exists and is marked, and checks it like the roles\'', () => {
    const f = editEnv((t) => `${t}DB_CONNECTION_LIMIT=40\n`);
    const r = dry('azure/provision.sh', ['--env', f, 'database']);
    expect(r.status).toBe(0);
    const [cluster] = sqlBlocks(r.stdout);
    const alter = cluster.indexOf('ALTER DATABASE "kitexample" WITH CONNECTION LIMIT 40;');
    expect(alter).toBeGreaterThan(cluster.indexOf(`COMMENT ON DATABASE "kitexample" IS 'affiliate-kit app=kitexample';`));
    expect(alter).toBeGreaterThan(cluster.indexOf('ALTER DATABASE "kitexample" OWNER TO "kitexample_owner";'));
    expect(alter).toBeLessThan(cluster.indexOf('GRANT CONNECT ON DATABASE "kitexample"'));
    expect(cluster).toContain("IF NOT EXISTS (SELECT FROM pg_catalog.pg_database WHERE datname = 'kitexample' AND datconnlimit = 40) THEN");
    expect(cluster).toContain("RAISE EXCEPTION 'affiliate-kit: database kitexample does not have CONNECTION LIMIT 40';");
    expect(cluster).toContain("IF (SELECT sum(rolconnlimit) FROM pg_catalog.pg_roles WHERE rolname IN ('kitexample_owner', 'kitexample_app', 'kitexample_agents', 'kitexample_publisher')) > 40 THEN");
    expect(r.stdout).toContain('connection limits: roles owner 3, app 20, agents 10, publisher 2 (sum 35); database 40');
    expect(r.stderr).not.toContain('equals the four role limits');
  });

  it('warns when it leaves no room beyond the roles\' sum, and normalises the number', () => {
    const f = editEnv((t) => `${t}DB_CONNECTION_LIMIT=035\n`);
    const r = dry('azure/provision.sh', ['--env', f, 'database']);
    expect(r.status).toBe(0);
    expect(sqlBlocks(r.stdout)[0]).toContain('ALTER DATABASE "kitexample" WITH CONNECTION LIMIT 35;');
    expect(r.stderr).toContain("WARN: DB_CONNECTION_LIMIT=35 equals the four role limits' sum");
  });

  it('refuses a limit below the roles\' sum, zero, or not a number, before anything runs', () => {
    for (const [v, why] of [
      ['34', 'DB_CONNECTION_LIMIT=34 is below the sum of the four role limits (35)'],
      ['0', "DB_CONNECTION_LIMIT='0' must be an integer from 1 to 5000"],
      ['-1', "DB_CONNECTION_LIMIT='-1' must be an integer from 1 to 5000"],
      ['unlimited', "DB_CONNECTION_LIMIT='unlimited' must be an integer from 1 to 5000"],
    ] as const) {
      const f = editEnv((t) => `${t}DB_CONNECTION_LIMIT=${v}\n`);
      const r = runScript(sb, 'azure/provision.sh', ['--env', f, '--dry-run', 'database']);
      expect([v, r.status]).toEqual([v, 2]);
      expect(r.stderr).toContain(why);
      expect(r.calls).toEqual([]);
    }
  });
});

describe('azure/provision.sh on an existing app', () => {
  it('reconciles ingress, resources, replicas and env instead of creating', () => {
    const r = dry('azure/provision.sh', ['app'], { KIT_DEPLOY_DRY_EXISTING: 'app' });
    expect(r.status).toBe(0);
    expect(dryCommands(r.stdout, 'az containerapp create')).toEqual([]);
    expect(oneCommand(r.stdout, 'az containerapp ingress update ')).toContain('--type external --target-port 8080 --transport auto');
    const upd = oneCommand(r.stdout, 'az containerapp update ');
    expect(upd).toContain('--cpu 0.5 --memory 1Gi --min-replicas 1 --max-replicas 3 --replace-env-vars PORT=8080');
    expect(upd).toContain('DATABASE_URL=secretref:database-url');
    expect(upd).not.toContain('--image');
  });

  it('re-sets secrets from files and restarts the revision only with --restart', () => {
    const a = dry('azure/provision.sh', ['secrets'], { KIT_DEPLOY_DRY_EXISTING: 'app' });
    expect(oneCommand(a.stdout, 'az containerapp secret set ')).toContain("--secrets 'database-url=@<secret-dir>/database-url' -o none");
    expect(dryCommands(a.stdout, 'az containerapp revision restart')).toEqual([]);
    const b = dry('azure/provision.sh', ['secrets', '--restart'], { KIT_DEPLOY_DRY_EXISTING: 'app' });
    expect(oneCommand(b.stdout, 'az containerapp revision restart ')).toContain("--revision '<latest-revision>'");
  });

  it('only binds a hostname that was added before, and skips a bound one', () => {
    const r = dry('azure/provision.sh', ['domain'], { KIT_DEPLOY_DRY_EXISTING: 'hostname:kit-example.test' });
    expect(dryCommands(r.stdout, 'az containerapp hostname add --hostname kit-example.test')).toEqual([]);
    expect(dryCommands(r.stdout, 'az containerapp hostname bind --hostname kit-example.test')).toHaveLength(1);
    expect(dryCommands(r.stdout, 'az containerapp hostname add --hostname www.kit-example.test')).toHaveLength(1);
  });

  it('domain --skip-dns-check binds without the DNS-over-HTTPS check', () => {
    const r = dry('azure/provision.sh', ['domain', '--skip-dns-check']);
    expect(r.stdout).not.toContain('dns-check');
  });
});

describe('azure/provision.sh foundation', () => {
  it('creates only what is missing, never a Postgres server, and passes the workspace key as @file', () => {
    const r = dry('azure/provision.sh', ['foundation']);
    expect(r.status).toBe(0);
    expect(oneCommand(r.stdout, 'az group create ')).toContain('--name kit-test-rg-does-not-exist --location eastus');
    expect(oneCommand(r.stdout, 'az acr create ')).toContain('--sku Basic --location eastus --admin-enabled true');
    expect(oneCommand(r.stdout, 'az monitor log-analytics workspace create ')).toContain('--workspace-name kit-test-logs-does-not-exist');
    const env = oneCommand(r.stdout, 'az containerapp env create ');
    expect(env).toContain("--logs-workspace-id '<log-workspace-id>' --logs-workspace-key '@<secret-dir>/log-workspace-key'");
    expect(dryCommands(r.stdout).join('\n')).not.toMatch(/flexible-server create|postgres .* create/);
    expect(r.stderr).toContain('never creates one');
  });

  it('is not part of all', () => {
    const r = dry('azure/provision.sh', ['all']);
    expect(r.stdout).not.toContain('== foundation');
    expect(r.stdout).not.toContain('az group create');
  });
});

describe('azure/provision.sh database: passwords', () => {
  it('a role without a DSN is created without a password and the run ends with exit 3', () => {
    const f = editEnv((t) => t.replace(/^DB_PUBLISHER_DSN=.*$/m, 'DB_PUBLISHER_DSN='));
    const r = runScript(sb, 'azure/provision.sh', ['--env', f, '--dry-run', 'database']);
    expect(r.status).toBe(3);
    expect(r.stderr).toContain('roles without a password');
    expect(r.stderr).toContain('kitexample_publisher');
    expect(sqlBlocks(r.stdout)[0]).not.toContain('ALTER ROLE "kitexample_publisher" WITH PASSWORD');
  });

  it('--write-new-dsns generates the missing ones instead', () => {
    const f = editEnv((t) => t.replace(/^DB_PUBLISHER_DSN=.*$/m, 'DB_PUBLISHER_DSN=env:NOT_SET_ANYWHERE'));
    const r = runScript(sb, 'azure/provision.sh', ['--env', f, '--dry-run', 'database', '--write-new-dsns', 'new.env']);
    expect(r.status).toBe(0);
    expect(r.stdout).toContain("[dry-run] generate a password for kitexample_publisher; append NOT_SET_ANYWHERE='postgresql://kitexample_publisher:<generated>@<db-host>:5432/kitexample?sslmode=require'");
    expect(sqlBlocks(r.stdout)[0]).toContain(`ALTER ROLE "kitexample_publisher" WITH PASSWORD '<scram-sha-256 verifier of a generated password>';`);
  });

  it('refuses a DSN that logs in as a different role', () => {
    const secrets = path.join(sb.fx, 'secrets-wrong.env');
    fs.writeFileSync(secrets, fs.readFileSync(path.join(sb.fx, 'secrets.env'), 'utf8')
      .replace("KIT_TEST_DATABASE_URL_APP='postgresql://kitexample_app:", "KIT_TEST_DATABASE_URL_APP='postgresql://kitexample_agents:"));
    const f = editEnv((t) => t.replace(/^SECRETS_FILE=.*$/m, 'SECRETS_FILE=secrets-wrong.env'));
    const r = runScript(sb, 'azure/provision.sh', ['--env', f, '--dry-run', 'database']);
    expect(r.status).toBe(3);
    expect(r.stderr).toContain("logs in as 'kitexample_agents', expected role 'kitexample_app'");
    expectNoSecrets(r.all);
  });
});

describe('azure/deploy.sh --dry-run', () => {
  let r: RunResult;
  beforeAll(() => {
    r = runScript(sb, 'azure/deploy.sh', ['--env', sb.envFile, '--dry-run', '--image-tag', '20261117-120000']);
  });

  it('runs checks, gate, build, push, migrate, release, smoke in order and calls nothing', () => {
    expect(r.status).toBe(0);
    expect(cloudCalls(r.calls)).toEqual([]);
    expectNoSecrets(r.all);
    const order = ['== checks ==', '== gate ==', '== build ', '== push ==', '== migrate ==', '== release ', '== smoke =='];
    const at = order.map((h) => r.stdout.indexOf(h));
    expect(at.every((i) => i >= 0)).toBe(true);
    expect([...at].sort((a, b) => a - b)).toEqual(at);
    expect(r.stdout).toContain('site.json cache: htmlSMaxAgeSeconds=600');
    expect(r.stdout).toContain('Dockerfile.azure: up to date with the kit template');
  });

  it('gates, builds the rendered Dockerfile and pushes the tag and latest', () => {
    expect(oneCommand(r.stdout, 'bash ')).toMatch(/predeploy-gate\.sh --site-root \S+\/fx\/site --suite tests\/run-local-suite\.sh -- --workers=4$/);
    const build = oneCommand(r.stdout, 'docker build ');
    expect(build).toMatch(/^\[dry-run\] docker build --platform linux\/amd64 --file \S+\/site\/Dockerfile\.azure --tag kittestacrdoesnotexist\.azurecr\.io\/kitexample:20261117-120000 /);
    expect(build).toContain(`--label affiliate-kit.app=kitexample --label affiliate-kit.version=${KIT_VERSION}`);
    expect(oneCommand(r.stdout, 'az acr login ')).toBe('[dry-run] az acr login --name kittestacrdoesnotexist');
    expect(dryCommands(r.stdout, 'docker push ')).toEqual([
      '[dry-run] docker push kittestacrdoesnotexist.azurecr.io/kitexample:20261117-120000',
      '[dry-run] docker push kittestacrdoesnotexist.azurecr.io/kitexample:latest',
    ]);
  });

  it('migrates inside the new image with the owner DSN in its environment only', () => {
    expect(r.stdout).toContain('[dry-run] DATABASE_URL=<env:KIT_TEST_DATABASE_URL_OWNER> docker run --rm -e DATABASE_URL kittestacrdoesnotexist.azurecr.io/kitexample:20261117-120000 node dist/src/migrate.js');
  });

  it('updates the revision with resources, replicas, env and secret refs, then waits for it', () => {
    const upd = oneCommand(r.stdout, 'az containerapp update ');
    expect(upd).toBe('[dry-run] az containerapp update --name kitexample --resource-group kit-test-rg-does-not-exist '
      + '--image kittestacrdoesnotexist.azurecr.io/kitexample:20261117-120000 --cpu 0.5 --memory 1Gi --min-replicas 1 '
      + '--max-replicas 3 --replace-env-vars PORT=8080 NODE_ENV=production PG_POOL_MAX=5 SITE_ID=kit-example '
      + 'AI_TRAFFIC_EXCLUDE_IPS=198.51.100.7 DATABASE_URL=secretref:database-url -o none');
    expect(r.stdout).toContain('(probe) az containerapp ingress show --name kitexample --resource-group kit-test-rg-does-not-exist --query targetPort -o tsv');
    expect(r.stdout).toContain('(wait up to 600s) revision <latest-revision> becomes the latest ready revision');
    expect(r.stdout).toContain('--query properties.latestReadyRevisionName -o tsv');
    expect(r.stdout).not.toMatch(/secret set|--secrets/);
  });

  it('smoke-checks the origin through the app FQDN and the public domain, with the HTML cache limit', () => {
    const smokes = dryCommands(r.stdout).filter((l) => l.includes('deploytool.py smoke'));
    expect(smokes).toHaveLength(2);
    expect(smokes[0]).toContain("--label origin --url https://kit-example.test --max-shared-age 600 --timeout 180");
    expect(smokes[0]).toContain("--connect-to '<fqdn>:443' --path / --path /health --path /robots.txt --path /llms.txt");
    expect(smokes[1]).toContain('--label public --url https://kit-example.test');
    expect(smokes[1]).toContain('--optional');
    expect(r.stdout.trim().split('\n').pop()).toBe(
      '{"app_name": "kitexample", "url": "https://<fqdn>", "image": "kittestacrdoesnotexist.azurecr.io/kitexample:20261117-120000"}');
  });

  it('smoke-checks the FQDN itself before the custom domain is bound', () => {
    const s = dry('azure/deploy.sh', ['--smoke-only', '--image-tag', 't1'], { KIT_DEPLOY_DRY_MISSING: 'hostname-bound' });
    const origin = dryCommands(s.stdout).find((l) => l.includes('--label origin'))!;
    expect(origin).toContain("--url 'https://<fqdn>'");
    expect(origin).not.toContain('--connect-to');
  });

  it('stage selection: --build-only, --skip-build, --only, and the tag rules', () => {
    const b = dry('azure/deploy.sh', ['--build-only']);
    expect(b.status).toBe(0);
    expect(dryCommands(b.stdout, 'docker build')).toHaveLength(1);
    expect(dryCommands(b.stdout, 'az containerapp update')).toEqual([]);
    expect(b.stdout).not.toContain('deploytool.py smoke');
    expect(b.stdout).toMatch(/kitexample:\d{8}-\d{6}/);

    const s = dry('azure/deploy.sh', ['--skip-build', '--image-tag', 't2']);
    expect(dryCommands(s.stdout, 'docker build')).toEqual([]);
    expect(s.stdout).toContain('(probe) az acr repository show --name kittestacrdoesnotexist --image kitexample:t2');
    expect(dryCommands(s.stdout, 'az containerapp update')).toHaveLength(1);

    expect(runScript(sb, 'azure/deploy.sh', ['--env', sb.envFile, '--dry-run', '--skip-build']).status).toBe(2);
    const bad = runScript(sb, 'azure/deploy.sh', ['--env', sb.envFile, '--dry-run', '--only', 'release,ship', '--image-tag', 't']);
    expect(bad.status).toBe(2);
    expect(bad.stderr).toContain("unknown stage 'ship'");
    const full = dry('azure/deploy.sh', ['--only', 'release', '--image-tag', 'kittestacrdoesnotexist.azurecr.io/kitexample:t3']);
    expect(oneCommand(full.stdout, 'az containerapp update ')).toContain('--image kittestacrdoesnotexist.azurecr.io/kitexample:t3 ');
    const other = runScript(sb, 'azure/deploy.sh', ['--env', sb.envFile, '--dry-run', '--only', 'release', '--image-tag', 'evil.io/x:1']);
    expect(other.status).toBe(2);
  });

  it('--skip-tests[=REASON] skips the gate loudly', () => {
    const s = dry('azure/deploy.sh', ['--build-only', '--skip-tests=hotfix for outage 123']);
    expect(s.stdout).toContain('!! --skip-tests: DEPLOYING CODE THE PRE-DEPLOY GATE HAS NOT TESTED.');
    expect(s.stdout).toContain('!! Reason: hotfix for outage 123');
    expect(s.stdout).not.toContain('predeploy-gate.sh');
  });

  it('exits 3 when the app does not exist yet', () => {
    const s = runScript(sb, 'azure/deploy.sh', ['--env', sb.envFile, '--dry-run', '--only', 'release', '--image-tag', 't'],
      { KIT_DEPLOY_DRY_MISSING: 'app' });
    expect(s.status).toBe(3);
    expect(s.stderr).toContain('run provision.sh app first');
  });
});

describe('azure/deploy.sh pre-build checks', () => {
  it('refuses a site.json whose HTML cache exceeds s-maxage 600', () => {
    const sj = path.join(sb.fx, 'site/site.json');
    const orig = fs.readFileSync(sj, 'utf8');
    try {
      fs.writeFileSync(sj, orig.replace('"htmlSMaxAgeSeconds": 600', '"htmlSMaxAgeSeconds": 3600'));
      const r = runScript(sb, 'azure/deploy.sh', ['--env', sb.envFile, '--dry-run', '--build-only']);
      expect(r.status).toBe(2);
      expect(r.stderr).toContain('cache.htmlSMaxAgeSeconds=3600 > 600');
      expect(dryCommands(r.stdout, 'docker build')).toEqual([]);
    } finally {
      fs.writeFileSync(sj, orig);
    }
  });

  it('refuses a rendered Dockerfile that drifted from the template, and secrets baked into an image', () => {
    const df = path.join(sb.fx, 'site/Dockerfile.azure');
    const orig = fs.readFileSync(df, 'utf8');
    try {
      fs.writeFileSync(df, orig.replace('EXPOSE 8080', 'EXPOSE 9090'));
      const a = runScript(sb, 'azure/deploy.sh', ['--env', sb.envFile, '--dry-run', '--build-only']);
      expect(a.status).toBe(2);
      expect(a.stderr).toContain('has drifted from the kit template');
      fs.writeFileSync(df, 'FROM node:22-alpine\nENV DATABASE_URL=postgres://x\nCMD ["node", "x.js"]\n');
      const b = runScript(sb, 'azure/deploy.sh', ['--env', sb.envFile, '--dry-run', '--build-only']);
      expect(b.status).toBe(2);
      expect(b.stderr).toContain('secret-looking ARG/ENV');
    } finally {
      fs.writeFileSync(df, orig);
    }
  });
});

describe('azure/teardown.sh', () => {
  it('deletes only the site: app, optionally certificates, images and the database', () => {
    const r = dry('azure/teardown.sh', ['--with-certificates', '--with-images', '--with-database']);
    expect(r.status).toBe(0);
    expect(oneCommand(r.stdout, 'az containerapp delete ')).toBe(
      '[dry-run] az containerapp delete --name kitexample --resource-group kit-test-rg-does-not-exist --yes -o none');
    expect(dryCommands(r.stdout, 'az containerapp env certificate delete ')).toHaveLength(2);
    expect(oneCommand(r.stdout, 'az acr repository delete ')).toContain('--repository kitexample --yes');
    const [drop] = sqlBlocks(r.stdout);
    expect(drop).toContain('DROP DATABASE IF EXISTS "kitexample" WITH (FORCE);');
    expect(drop).toContain('DROP ROLE IF EXISTS "kitexample_publisher";');
    expect(r.all).not.toMatch(/az group delete|containerapp env delete|acr delete|flexible-server delete/);
  });

  it('without flags deletes the app only', () => {
    const r = dry('azure/teardown.sh', []);
    expect(dryCommands(r.stdout).filter((l) => !l.includes('(probe)') && !l.includes('affiliate-kit deploy'))).toEqual([
      '[dry-run] az containerapp delete --name kitexample --resource-group kit-test-rg-does-not-exist --yes -o none',
    ]);
  });

  it('a real run needs --confirm APP_NAME', () => {
    const r = runScript(sb, 'azure/teardown.sh', ['--env', sb.envFile]);
    expect(r.status).toBe(2);
    expect(r.stderr).toContain('pass --confirm kitexample');
    expect(r.calls).toEqual([]);
    const wrong = runScript(sb, 'azure/teardown.sh', ['--env', sb.envFile, '--confirm', 'someothersite']);
    expect(wrong.status).toBe(2);
    expect(wrong.calls).toEqual([]);
  });

  it('--with-images first asks which Container Apps and jobs run images from the repository, and stops if any other does', () => {
    const ok = dry('azure/teardown.sh', ['--with-images']);
    expect(ok.status).toBe(0);
    const q = "--query '[].{id: id, images: [properties.template.containers[].image, properties.template.initContainers[].image]}' -o json";
    expect(ok.stdout).toContain(`[dry-run] (probe) az containerapp list ${q}`);
    expect(ok.stdout).toContain(`[dry-run] (probe) az containerapp job list ${q}`);
    expect(ok.stdout.indexOf('az containerapp job list')).toBeLessThan(ok.stdout.indexOf('[dry-run] az containerapp delete'));
    const r = runScript(sb, 'azure/teardown.sh', ['--env', sb.envFile, '--dry-run', '--with-images', '--with-certificates'],
      { KIT_DEPLOY_DRY_EXISTING: 'image-users' });
    expect(r.status).toBe(2);
    expect(r.stderr).toContain('repository kitexample in kittestacrdoesnotexist is in use by other Container Apps or jobs');
    expect(dryCommands(r.stdout).filter((l) => !l.includes('(probe)') && !l.includes('affiliate-kit deploy'))).toEqual([]);
  });

  it('--with-certificates reads the certificates this site\'s app binds before deleting the app', () => {
    const r = dry('azure/teardown.sh', ['--with-certificates']);
    const bound = r.stdout.indexOf("[dry-run] (probe) az containerapp hostname list --name kitexample --resource-group kit-test-rg-does-not-exist --query '[].certificateId' -o tsv");
    expect(bound).toBeGreaterThan(0);
    expect(bound).toBeLessThan(r.stdout.indexOf('[dry-run] az containerapp delete'));
    // Without the app there is nothing bound: only failed attempts would go.
    const gone = runScript(sb, 'azure/teardown.sh', ['--env', sb.envFile, '--dry-run', '--with-certificates'], { KIT_DEPLOY_DRY_MISSING: 'app' });
    expect(gone.status).toBe(0);
    expect(dryCommands(gone.stdout, 'az containerapp env certificate delete')).toEqual([]);
    expect(gone.stderr).toContain('kit-example.test: keeping managed certificate <certificates:kit-example.test>');
  });
});

describe('azure/teardown.sh: real mode against stubs (what the guards read)', () => {
  const ID = (rg: string, kind: string, name: string) => `/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/${rg}/providers/Microsoft.App/${kind}/${name}`;
  const own = { id: ID('KIT-TEST-RG-DOES-NOT-EXIST', 'containerApps', 'kitexample'), images: [['kittestacrdoesnotexist.azurecr.io/kitexample:t1'], null] };
  const base = [
    { match: 'account show', stdout: '00000000-0000-0000-0000-000000000000\n' },
    { match: 'tags."affiliate-kit-app"', stdout: 'kitexample\n' },
    { match: '--query name -o tsv', stdout: 'kitexample\n' },
    { match: 'acr repository show', stdout: 'kitexample\n' },
    { match: 'containerapp job list', stdout: '[]\n' },
  ];
  const azCalls = (r: RunResult) => r.calls.filter((c) => c.tool === 'az').map((c) => c.argv.join(' '));
  const teardown = (args: string[]) => runScript(sb, 'azure/teardown.sh', ['--env', sb.envFile, '--confirm', 'kitexample', ...args]);

  it('--with-images: another app or job running an image from the repository (any tag, a digest, an init container) stops it', () => {
    for (const other of [
      { id: ID('another-rg', 'containerApps', 'kitexample'), images: [['kittestacrdoesnotexist.azurecr.io/kitexample:20261101-000000'], null] },
      { id: ID('kit-test-rg-does-not-exist', 'containerApps', 'agents'), images: [['nginx'], ['kittestacrdoesnotexist.azurecr.io/kitexample@sha256:0123']] },
    ]) {
      setResponses(sb, [{ match: 'containerapp list', stdout: `${JSON.stringify([own, other])}\n` }, ...base]);
      const r = teardown(['--with-images']);
      expect([other.id, r.status]).toEqual([other.id, 2]);
      expect(r.stderr).toContain(other.id);
      expect(azCalls(r).filter((c) => / delete /.test(` ${c} `))).toEqual([]);
    }
    const job = { id: ID('kit-test-rg-does-not-exist', 'jobs', 'kitexample'), images: [['kittestacrdoesnotexist.azurecr.io/kitexample'], null] };
    setResponses(sb, [{ match: 'containerapp list', stdout: `${JSON.stringify([own])}\n` },
      { match: 'containerapp job list', stdout: `${JSON.stringify([job])}\n` }, ...base]);
    const r = teardown(['--with-images']);
    expect(r.status).toBe(2);
    expect(r.stderr).toContain(job.id);
  });

  it('--with-images: the site\'s own app (by resource id) and look-alike repositories do not count; the repository goes', () => {
    const lookalike = { id: ID('kit-test-rg-does-not-exist', 'containerApps', 'kitexample-x'), images: [['kittestacrdoesnotexist.azurecr.io/kitexample-x:t1'], null] };
    setResponses(sb, [{ match: 'containerapp list', stdout: `${JSON.stringify([own, lookalike])}\n` }, ...base]);
    const r = teardown(['--with-images']);
    expect(r.status).toBe(0);
    const calls = azCalls(r);
    expect(calls.filter((c) => c.startsWith('containerapp delete'))).toHaveLength(1);
    expect(calls.filter((c) => c.startsWith('acr repository delete'))).toEqual([
      'acr repository delete --name kittestacrdoesnotexist --repository kitexample --yes -o none']);
  });

  it('a query that fails stops it before anything is deleted', () => {
    setResponses(sb, [{ match: 'containerapp list', exit: 1, stderr: 'ERROR: (AuthorizationFailed) no read access\n' }, ...base]);
    const r = teardown(['--with-images']);
    expect(r.status).toBe(1);
    expect(r.stderr).toContain('a read-only query failed');
    expect(azCalls(r).filter((c) => / delete /.test(` ${c} `))).toEqual([]);
  });

  it('--with-certificates deletes the certificates the app binds and failed attempts, and keeps any other', () => {
    const envId = '/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/kit-test-rg-does-not-exist/providers/Microsoft.App/managedEnvironments/kit-test-env-does-not-exist/managedCertificates';
    setResponses(sb, [
      { match: 'hostname list', stdout: `${envId}/mc-apex-bound\n${envId}/mc-www-bound\n` },
      { match: "subjectName=='kit-example.test'", stdout: 'mc-apex-bound\tSucceeded\nmc-apex-other\tSucceeded\n' },
      { match: "subjectName=='www.kit-example.test'", stdout: 'mc-www-bound\tSucceeded\nmc-www-failed\tFailed\n' },
      ...base,
    ]);
    const r = teardown(['--with-certificates']);
    expect(r.status).toBe(0);
    const calls = azCalls(r);
    expect(calls.findIndex((c) => c.startsWith('containerapp hostname list'))).toBeLessThan(calls.findIndex((c) => c.startsWith('containerapp delete')));
    expect(calls.filter((c) => c.startsWith('containerapp env certificate delete')).map((c) => c.match(/--certificate (\S+)/)?.[1]))
      .toEqual(['mc-apex-bound', 'mc-www-bound', 'mc-www-failed']);
    expect(r.stderr).toContain('kit-example.test: keeping managed certificate mc-apex-other (Succeeded)');
  });
});

describe('azure/deploy.sh: stage selection and the gate policy', () => {
  const run = (args: string[]) => runScript(sb, 'azure/deploy.sh', ['--env', sb.envFile, '--dry-run', ...args]);

  it.each([[['--only', '']], [['--only=']], [['--only', ' , ']]])('an empty stage list %j is an error, never a full deploy', (args) => {
    const r = run([...args, '--image-tag', 't1']);
    expect(r.status).toBe(2);
    expect(r.stderr).toContain('--only needs at least one stage');
    expect(dryCommands(r.stdout)).toEqual([]);
    expect(r.calls).toEqual([]);
  });

  it('refuses to build without the gate stage unless --gated-by or --skip-tests says why', () => {
    const r = run(['--only', 'build,push,migrate,release,smoke', '--image-tag', 't1']);
    expect(r.status).toBe(2);
    expect(r.stderr).toContain('builds without the gate stage, and untested code does not ship');
    expect(dryCommands(r.stdout, 'docker build')).toEqual([]);
    expect(r.calls).toEqual([]);
    expect(run(['--only', 'build', '--image-tag', 't1']).status).toBe(2);
  });

  it('--gated-by=WHO builds without repeating the gate and says who ran it', () => {
    const r = dry('azure/deploy.sh', ['--only', 'build', '--gated-by=deployer', '--image-tag', 't1']);
    expect(r.status).toBe(0);
    expect(r.stdout).toContain('gate: run by deployer on the tree being built (--gated-by); not repeated here');
    expect(r.stdout).not.toContain('predeploy-gate.sh');
    expect(dryCommands(r.stdout, 'docker build')).toHaveLength(1);
  });

  it('--skip-tests on a build without the gate stage still prints the banner', () => {
    const r = dry('azure/deploy.sh', ['--only', 'build', '--skip-tests=hotfix', '--image-tag', 't1']);
    expect(r.status).toBe(0);
    expect(r.stdout).toContain('!! --skip-tests: DEPLOYING CODE THE PRE-DEPLOY GATE HAS NOT TESTED.');
  });

  it.each([
    [['--gated-by=deployer'], 'leave the gate stage out'],
    [['--only', 'build', '--gated-by=deployer', '--skip-tests'], 'contradict each other'],
    [['--only', 'build', '--gated-by=bad name'], '--gated-by needs a name'],
  ])('rejects %j', (args, why) => {
    const r = run([...args, '--image-tag', 't1']);
    expect(r.status).toBe(2);
    expect(r.stderr).toContain(why);
  });

  it('a release of an image this run did not build says so', () => {
    const r = dry('azure/deploy.sh', ['--skip-build', '--image-tag', 't2']);
    expect(r.stdout).toContain('deploying the existing image kittestacrdoesnotexist.azurecr.io/kitexample:t2 (not built in this run): the gate tested whatever built it');
    const b = dry('azure/deploy.sh', ['--image-tag', 't3']);
    expect(b.stdout).not.toContain('not built in this run');
  });

  it('--rollback-on-smoke-failure reads the serving image before it releases', () => {
    const r = dry('azure/deploy.sh', ['--only', 'release,smoke', '--rollback-on-smoke-failure', '--image-tag', 't4']);
    expect(r.status).toBe(0);
    const at = r.stdout.indexOf("(probe) az containerapp show --name kitexample --resource-group kit-test-rg-does-not-exist --query 'properties.template.containers[0].image'");
    expect(at).toBeGreaterThan(0);
    expect(at).toBeLessThan(r.stdout.indexOf('[dry-run] az containerapp update'));
  });
});

describe('ownership: only this site\'s Container App is changed', () => {
  const tagProbe = "(probe) az containerapp show --name kitexample --resource-group kit-test-rg-does-not-exist --query 'tags.\"affiliate-kit-app\"' -o tsv";

  it('deploy checks the affiliate-kit-app tag before it releases, and refuses an untagged app', () => {
    const ok = dry('azure/deploy.sh', ['--only', 'release', '--image-tag', 't1']);
    expect(ok.stdout.indexOf(tagProbe)).toBeGreaterThan(0);
    expect(ok.stdout.indexOf(tagProbe)).toBeLessThan(ok.stdout.indexOf('[dry-run] az containerapp update'));
    const r = runScript(sb, 'azure/deploy.sh', ['--env', sb.envFile, '--dry-run', '--only', 'release', '--image-tag', 't1'],
      { KIT_DEPLOY_DRY_MISSING: 'app-owner-tag' });
    expect(r.status).toBe(2);
    expect(r.stderr).toContain('Container App kitexample in kit-test-rg-does-not-exist exists but was not created by the kit for kitexample');
    expect(dryCommands(r.stdout, 'az containerapp update')).toEqual([]);
  });

  it('provision refuses to reconcile an untagged app, and --adopt-existing tags it first', () => {
    const env = { KIT_DEPLOY_DRY_EXISTING: 'app', KIT_DEPLOY_DRY_MISSING: 'app-owner-tag' };
    const r = runScript(sb, 'azure/provision.sh', ['--env', sb.envFile, '--dry-run', 'app'], env);
    expect(r.status).toBe(2);
    expect(r.stderr).toContain('re-run provision.sh with --adopt-existing');
    expect(dryCommands(r.stdout, 'az containerapp ingress update')).toEqual([]);
    const a = runScript(sb, 'azure/provision.sh', ['--env', sb.envFile, '--dry-run', 'app', '--adopt-existing'], env);
    expect(a.status).toBe(0);
    const tag = oneCommand(a.stdout, 'az containerapp update --name kitexample --resource-group kit-test-rg-does-not-exist --tags ');
    expect(tag).toContain(`--tags affiliate-kit=${KIT_VERSION} affiliate-kit-app=kitexample -o none`);
    expect(a.stdout.indexOf(tag)).toBeLessThan(a.stdout.indexOf('[dry-run] az containerapp ingress update'));
  });

  it('secrets and domain check the tag too; secrets reads what to remove before it sets anything', () => {
    const s2 = dry('azure/provision.sh', ['secrets'], { KIT_DEPLOY_DRY_EXISTING: 'app' });
    expect(s2.stdout).toContain(tagProbe);
    expect(s2.stdout).toContain("(probe) az containerapp secret list --name kitexample --resource-group kit-test-rg-does-not-exist --query '[].name' -o tsv");
    expect(s2.stdout).toContain("--query 'properties.configuration.registries[].passwordSecretRef'");
    expect(s2.stdout.indexOf('secret list')).toBeLessThan(s2.stdout.indexOf('[dry-run] az containerapp secret set'));
    expect(s2.stdout).toContain('a real run removes the secrets on the app that APP_SECRETS no longer lists');
    const d = runScript(sb, 'azure/provision.sh', ['--env', sb.envFile, '--dry-run', 'domain', '--skip-dns-check'],
      { KIT_DEPLOY_DRY_MISSING: 'app-owner-tag' });
    expect(d.status).toBe(2);
    expect(dryCommands(d.stdout, 'az containerapp hostname')).toEqual([]);
  });

  it('teardown refuses an app that is not this site\'s before deleting anything', () => {
    const r = runScript(sb, 'azure/teardown.sh', ['--env', sb.envFile, '--dry-run', '--with-images', '--with-database'],
      { KIT_DEPLOY_DRY_MISSING: 'app-owner-tag' });
    expect(r.status).toBe(2);
    expect(dryCommands(r.stdout).filter((l) => !l.includes('(probe)') && !l.includes('affiliate-kit deploy'))).toEqual([]);
  });

  it('--adopt-existing reaches the database SQL', () => {
    const r = dry('azure/provision.sh', ['database', '--adopt-existing']);
    expect(sqlBlocks(r.stdout)[0]).toContain('adopt constant boolean := true;');
  });
});

describe('azure/teardown.sh --with-database', () => {
  it('drops only after the same ownership check, before any DROP', () => {
    const r = dry('azure/teardown.sh', ['--with-database']);
    const [drop] = sqlBlocks(r.stdout);
    const check = drop.indexOf("RAISE EXCEPTION 'affiliate-kit (%, app kitexample): not created by the kit for this site, so nothing was changed: %', 'teardown', strangers");
    expect(check).toBeGreaterThan(0);
    expect(drop.indexOf('DROP DATABASE')).toBeGreaterThan(check);
    expect(drop).toContain('adopt constant boolean := false;');
  });

  it('a real run needs --confirm-database DB_NAME as well', () => {
    const a = runScript(sb, 'azure/teardown.sh', ['--env', sb.envFile, '--confirm', 'kitexample', '--with-database']);
    expect(a.status).toBe(2);
    expect(a.stderr).toContain('pass --confirm-database kitexample');
    expect(a.calls).toEqual([]);
    const b = runScript(sb, 'azure/teardown.sh', ['--env', sb.envFile, '--confirm', 'kitexample', '--with-database', '--confirm-database', 'otherdb']);
    expect(b.status).toBe(2);
    expect(b.calls).toEqual([]);
  });
});

describe('--help', () => {
  it.each(['azure/provision.sh', 'azure/deploy.sh', 'azure/teardown.sh'])('%s prints its usage', (s) => {
    const r = runScript(sb, s, ['--help']);
    expect(r.status).toBe(0);
    expect(r.stdout).toContain(s);
    expect(r.stdout).toMatch(/Exit: 0 ok/);
    expect(r.stdout).toContain('bash -x');
  });
});
