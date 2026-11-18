-- Re-stamps the fixture at every gate start (tests/run-local-suite.sh applies
-- it after db/fixtures/seed.sql, once the app under test is healthy). The
-- fixture database is long-lived, so anything dated must be moved back into
-- the window the gate expects:
--   - Amazon check times (when the site has a catalog): rows meant to show a
--     price get a fetched_at a little in the past, inside the 24h window; a
--     row meant to be stale stays days old;
--   - article dates, so "latest articles" (llms.txt) keeps listing the fixture.
-- Idempotent, and a no-op on a database the app has not migrated yet.
DO $freshen$
BEGIN
  IF to_regclass('public.editorial_articles') IS NULL THEN
    RAISE NOTICE 'freshen: editorial_articles does not exist yet; nothing to do';
    RETURN;
  END IF;
  UPDATE editorial_articles SET published_at = now() - interval '2 days'
   WHERE slug = 'how-we-compare-products' AND status = 'published';
END
$freshen$;
