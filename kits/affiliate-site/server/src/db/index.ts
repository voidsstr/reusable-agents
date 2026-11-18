/**
 * Affiliate-site kit: database layer (build item K4).
 *
 *   migrate.ts         the migration runner (advisory lock + ledger), the
 *                      kit's own migrations (runKitMigrations) and the
 *                      `migrate` command line
 *   publishTrigger.ts  renders the editorial_articles publish guard for a
 *                      content_policy site
 *
 * The SQL lives in ../../../migrations (kit_0001..kit_0004 and
 * templates/publish_trigger.sql.tmpl).
 */
export * from './migrate';
export * from './publishTrigger';
