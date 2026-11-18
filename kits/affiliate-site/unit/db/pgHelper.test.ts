/**
 * helpers/pg.ts refuses to run the *.pg suites' superuser DDL (CREATE / DROP
 * DATABASE, CREATE / DROP ROLE) anywhere that does not look throwaway, and
 * never echoes the URL (it carries the password). Offline.
 */
import { adminUrlProblem, foreignDatabases, SCRATCH_DB_PREFIX } from './helpers/pg';

const env = (over: Record<string, string> = {}): NodeJS.ProcessEnv => ({ ...over });

describe('adminUrlProblem', () => {
  it.each([
    'postgres://postgres:pw@127.0.0.1:55740/postgres',
    'postgresql://postgres:pw@127.10.0.9/postgres',
    'postgres://postgres:pw@localhost:5432/postgres',
    'postgres://postgres:pw@[::1]:5432/postgres',
    'postgres:///postgres?host=/var/run/postgresql',
    'postgres:///postgres',
  ])('accepts a loopback or Unix-socket server: %s', (url) => {
    expect(adminUrlProblem(url, env())).toBe('');
  });

  it('refuses a remote host and names only the host, never the password', () => {
    const problem = adminUrlProblem('postgres://admin:s3cret-value@db.example.com:5432/postgres', env());
    expect(problem).toMatch(/names host db\.example\.com, which is not loopback/);
    expect(problem).toMatch(/KIT_TEST_DATABASE_ALLOW_REMOTE=1/);
    expect(problem).not.toContain('s3cret-value');
    expect(problem).not.toContain('admin');
  });

  it('checks a host query parameter too (node-postgres lets it override the URL host)', () => {
    expect(adminUrlProblem('postgres://u:pw@127.0.0.1/postgres?host=db.example.com', env())).toMatch(/db\.example\.com/);
  });

  it('falls back to PGHOST when the URL names no host', () => {
    expect(adminUrlProblem('postgres:///postgres', env({ PGHOST: 'db.example.com' }))).toMatch(/db\.example\.com/);
  });

  it('allows a remote server only with KIT_TEST_DATABASE_ALLOW_REMOTE=1', () => {
    expect(adminUrlProblem('postgres://u:pw@db.example.com/postgres', env({ KIT_TEST_DATABASE_ALLOW_REMOTE: '1' }))).toBe('');
    expect(adminUrlProblem('postgres://u:pw@db.example.com/postgres', env({ KIT_TEST_DATABASE_ALLOW_REMOTE: 'true' }))).not.toBe('');
  });

  it('refuses what is not a postgres URL without echoing it', () => {
    expect(adminUrlProblem('not a url with secret-pw', env())).toBe('KIT_TEST_DATABASE_URL does not parse as a postgres:// URL');
    expect(adminUrlProblem('https://user:secret-pw@127.0.0.1/', env())).toMatch(/must be a postgres:\/\/ URL \(got scheme https:\)/);
  });
});

describe('foreignDatabases', () => {
  it("lists databases that are neither postgres, a template nor one of the suites' scratch databases", async () => {
    const calls: Array<{ text: string; values?: unknown[] }> = [];
    const fake = {
      async query(text: string, values?: unknown[]) {
        calls.push({ text, values });
        return { rows: [{ datname: 'shop_production' }] };
      },
    };
    expect(await foreignDatabases(fake)).toEqual(['shop_production']);
    expect(calls[0].text).toMatch(/NOT datistemplate AND datname <> 'postgres'/);
    expect(calls[0].values).toEqual([SCRATCH_DB_PREFIX]);
    expect(SCRATCH_DB_PREFIX).toBe('kit_');
  });
});
