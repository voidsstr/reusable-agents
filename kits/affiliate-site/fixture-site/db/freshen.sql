-- Re-stamp the fixture's Amazon check times at gate start, so the prices and
-- availability the fixture means to show are inside the quotable window, and
-- the rows that are meant to be stale (B0GATE0008, three days) or just past
-- the window (B0GATE0009, 23h53m: past 24h minus the HTML cache lifetimes,
-- inside 24h) stay there. The violations scenario's two hand-made stamps get
-- the same treatment (no such notes exist in the clean database): their
-- <time datetime> and the time they show (kit 0.2.0's stamp rule wants the
-- date, time and zone in view, and the same instant as the datetime), so each
-- stays red for its own reason only. The runner applies it after the app
-- under test is healthy (its migrations, including the seeds, have run).
-- Idempotent, and a no-op on a database the app has not migrated yet.
DO $freshen$
DECLARE
  stamp_hour text := to_char((now() - interval '1 hour') AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"');
  stamp_edge text := to_char((now() - interval '23 hours 53 minutes') AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"');
  shown_hour text := to_char((now() - interval '1 hour') AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI "UTC"');
  shown_edge text := to_char((now() - interval '23 hours 53 minutes') AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI "UTC"');
BEGIN
  IF to_regclass('public.fixture_item_cache') IS NULL THEN
    RAISE NOTICE 'freshen: fixture_item_cache does not exist yet; nothing to do';
    RETURN;
  END IF;
  UPDATE fixture_item_cache SET fetched_at = now() - interval '90 minutes'
   WHERE asin <> ALL (ARRAY['B0GATE0006', 'B0GATE0008', 'B0GATE0009']);
  UPDATE fixture_item_cache SET fetched_at = now() - interval '2 hours' WHERE asin = 'B0GATE0006';
  UPDATE fixture_item_cache SET fetched_at = now() - interval '3 days' WHERE asin = 'B0GATE0008';
  UPDATE fixture_item_cache SET fetched_at = now() - interval '23 hours 53 minutes' WHERE asin = 'B0GATE0009';
  IF to_regclass('public.fixture_notes') IS NOT NULL THEN
    UPDATE fixture_notes SET body_html = regexp_replace(body_html, '<time datetime="[^"]*">[^<]*</time>',
                                                        '<time datetime="' || stamp_hour || '">' || shown_hour || '</time>', 'g')
     WHERE slug = 'hidden-disclaimer-note';
    UPDATE fixture_notes SET body_html = regexp_replace(body_html, '<time datetime="[^"]*">[^<]*</time>',
                                                        '<time datetime="' || stamp_edge || '">' || shown_edge || '</time>', 'g')
     WHERE slug = 'edge-stamp-note';
  END IF;
END
$freshen$;
