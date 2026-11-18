-- 0004: rows at the edges of the clean fixture's rules (both databases).
--
--   B0GATE0009  last checked on Amazon just past the quotable window (24h
--               minus every HTML cache lifetime: 23h49m for this site.json)
--               but inside 24h; db/freshen.sql keeps it at 23h53m. Its page
--               must show no price, no Offer and no Amazon title. If the kit's
--               quotablePrice() slipped back to a flat 24h, the page would show
--               a stamp 23h53m old, and kit-compliance fails a stamp past the
--               window (tests/ui/fixture-site.spec.ts checks the page too).
--   B0GATE0006  its summary said Amazon "no longer has [it] in stock": an
--               undated availability claim outside a stamp (2(i)). Its page
--               says so with the kit's dated availability stamp instead.
--
-- New rows go in a new file: the fixture database is long-lived, and the
-- runner records each file once by name.
SET LOCAL lock_timeout = '5s';

INSERT INTO fixture_products (asin, model_name, brand, category, lifecycle, summary, capacity_wh, weight_kg, alternative_asin, rank) VALUES
  ('B0GATE0009', 'Fabrikam Cell 900', 'Fabrikam', 'widgets', 'live',
   'A 960 Wh widget whose last Amazon price check is just past the window a price may be shown in.', 960, 10.4, NULL, 9)
ON CONFLICT (asin) DO NOTHING;

INSERT INTO fixture_item_cache (asin, title, image_url, detail_page_url, price, list_price, currency, availability_type, fetched_at) VALUES
  ('B0GATE0009', 'Fabrikam Cell 900 Widget, 960Wh, Expandable Battery, Quiet Charging Mode',
   'https://m.media-amazon.com/images/I/GATE0009.jpg', 'https://www.amazon.com/dp/B0GATE0009?tag=gatefixture-20&linkCode=ogi',
   799.00, NULL, 'USD', 'IN_STOCK', now() - interval '23 hours 53 minutes')
ON CONFLICT (asin) DO NOTHING;

UPDATE fixture_products
   SET summary = 'A 640 Wh widget with dual charging, built for use outdoors.'
 WHERE asin = 'B0GATE0006' AND summary ILIKE '%in stock%';
