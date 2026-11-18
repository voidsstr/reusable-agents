-- Runs once, when the fixture Postgres container first initialises its data
-- directory (docker-entrypoint-initdb.d). POSTGRES_DB (kit_fixture) already
-- exists; this adds the database the violations scenario renders from. Both
-- are migrated by the fixture app itself at boot (kit migrations, then
-- db/migrations), and 0003_fixture_violations.sql seeds only the second one.
CREATE DATABASE kit_fixture_violations;
