/**
 * server/src/db/index.ts: the database layer's public surface, and proof that
 * the kit barrel (server/src/index.ts) can re-export it (`export * from
 * './db'`) without a name clash.
 */
import * as kit from '../../server/src';
import * as db from '../../server/src/db';

describe('the db barrel', () => {
  it('exports the runner, the kit migrations and the publish-guard renderer', () => {
    for (const name of [
      'MIGRATION_LOCK_KEY', 'DEFAULT_LEDGER_TABLE', 'runPendingMigrations', 'pendingMigrations', 'listMigrationFiles',
      'acquireMigrationLock', 'releaseMigrationLock', 'splitSqlStatements', 'MigrationLockTimeoutError', 'MigrationConfigError',
      'kitMigrationsDir', 'kitMigrationSources', 'runKitMigrations', 'killStaleTransactions', 'migrateCli',
      'renderPublishTrigger', 'validatePublishTriggerParams', 'renderSqlTemplate', 'PublishTriggerError', 'publishTriggerCli',
      'DEFAULT_CONTENT_COLUMNS', 'PUBLISH_GUARD_TRIGGER', 'PUBLISH_GUARD_FUNCTION',
    ]) {
      expect((db as Record<string, unknown>)[name]).toBeDefined();
    }
    expect(db.MIGRATION_LOCK_KEY).toBe(0x6d696772);
  });

  it('shares no export name with the kit barrel, so `export * from \'./db\'` is safe to add (or already added)', () => {
    // Once the kit barrel re-exports ./db, the same names are the same objects.
    const kitExports = kit as Record<string, unknown>;
    const dbExports = db as Record<string, unknown>;
    const clash = Object.keys(db).filter((name) => Object.prototype.hasOwnProperty.call(kit, name) && kitExports[name] !== dbExports[name]);
    expect(clash).toEqual([]);
  });
});
