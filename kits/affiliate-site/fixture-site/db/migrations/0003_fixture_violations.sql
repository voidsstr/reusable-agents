-- 0003: the deliberately seeded violations. They land ONLY in the database
-- named kit_fixture_violations (db/init/01-databases.sql creates it); in any
-- other database this file is a no-op, so the clean fixture stays clean.
--
-- Each row breaks exactly one rule the kit gate enforces, in the way a real
-- site breaks it (hand-written HTML that bypasses the kit helpers, bad content
-- an author wrote, a constraint a migration dropped). tests/selftest checks
-- that the gate turns red on every one of them, and on nothing in the clean
-- database. The robots.txt and llms.txt violations come from config instead
-- (site.violations.json, llms.violations.json).
SET LOCAL lock_timeout = '5s';

DO $violations$
BEGIN
  IF current_database() <> 'kit_fixture_violations' THEN
    RETURN;
  END IF;

  -- A stored Amazon-CDN hero needs both hero CHECKs gone: a site that adopted
  -- the kit on an older table, or a migration that "relaxed" them.
  ALTER TABLE editorial_articles DROP CONSTRAINT IF EXISTS editorial_articles_hero_image_no_amazon_cdn;
  ALTER TABLE editorial_articles DROP CONSTRAINT IF EXISTS editorial_articles_hero_image_host_allowlist;

  INSERT INTO editorial_articles
    (slug, title, excerpt, body_md, hero_image_url, category, related_product_asins, status, published_at, updated_at)
  VALUES
    -- hero_image_url on Amazon's image CDN (IP License 2(h)).
    ('amazon-hero-guide', 'Widget Buying Mistakes to Avoid',
     'Five mistakes people make when they buy a widget, and how to avoid each one before an outage.',
     $md$Most widget regrets come from buying too small. Count your loads first.$md$,
     'https://m.media-amazon.com/images/I/hero-violation.jpg', 'guides', ARRAY[]::text[],
     'published', '2026-11-05T12:00:00Z', '2026-11-05T12:00:00Z'),
    -- A comparison table with a blank line between its header and delimiter
    -- rows: GFM does not render it, so the reader sees raw pipes.
    ('broken-table-guide', 'Widget Sizes at a Glance',
     'Capacity and weight for three widgets, side by side, so you can see the trade-off between run time and carrying.',
     $md$Here is how the three sizes compare.

| Widget | Capacity | Weight |

| --- | --- | --- |
| Northwind PW-500 | 512 Wh | 6.1 kg |
| Contoso Volt 1000 | 1,024 Wh | 11.3 kg |$md$,
     NULL, 'guides', ARRAY[]::text[], 'published', '2026-11-06T12:00:00Z', '2026-11-06T12:00:00Z'),
    -- Content policy: an instruction the fixture rule set rejects.
    ('unsafe-advice-guide', 'Running Your House From a Widget',
     'What a widget can and cannot run during an outage, with the wiring shortcut people ask about most.',
     $md$During an outage you can backfeed the house through the dryer outlet with a double-male cord, and every circuit comes back on.$md$,
     NULL, 'guides', ARRAY[]::text[], 'published', '2026-11-07T12:00:00Z', '2026-11-07T12:00:00Z'),
    -- A buy button for the recalled SKU, written into a guide.
    ('recalled-pick-guide', 'A Widget for Camping Trips',
     'A camping widget pick with wireless charging, enough capacity for a long weekend and a carry handle.',
     $md$For a long weekend, the Tailspin Spark 800 has the capacity and the wireless pad. [See the Tailspin Spark 800 on Amazon](https://www.amazon.com/dp/B0GATE0007)$md$,
     NULL, 'guides', ARRAY[]::text[], 'published', '2026-11-08T12:00:00Z', '2026-11-08T12:00:00Z')
  ON CONFLICT (slug) DO NOTHING;

  INSERT INTO fixture_notes (slug, title, description, body_html, head_html, jsonld, amazon_content, updated_at) VALUES
    -- An Amazon link with someone else's tag, no rel and no target.
    ('foreign-tag-note', 'Where to Buy the PW-500', 'Where to buy the Northwind PW-500 widget.',
     '<p>Buy the <a href="https://www.amazon.com/dp/B0GATE0001?tag=someoneelse-20">Northwind PW-500</a> here.</p>',
     NULL, NULL, FALSE, '2026-11-09T12:00:00Z'),
    -- Prices printed without a stamp or the 2(i) disclaimer.
    ('unstamped-price-note', 'Widget Prices Today', 'What the widgets cost on Amazon today.',
     '<p>The Northwind PW-500 is $349.99 on Amazon right now.</p><p>Contoso Volt 1000: <span class="ssr-price">$699.00</span></p>',
     NULL, NULL, FALSE, '2026-11-09T12:00:00Z'),
    -- A hand-made stamp: a week old and without the 2(i) disclaimer.
    ('stale-stamp-note', 'Fabrikam Cell 300 Price Check', 'A price check on the Fabrikam Cell 300 widget.',
     '<p>Fabrikam Cell 300: <span class="kit-price-stamp" data-price-stamp="price"><span class="ssr-price">$229.95</span> <span class="ssr-price-checked">on Amazon.com as of <time datetime="2026-01-02T03:04:05.000Z">2026-01-02 03:04 UTC</time> (price may vary)</span></span></p>',
     NULL, NULL, FALSE, '2026-11-09T12:00:00Z'),
    -- An Amazon product title on a page that never shows the 2(k) notice.
    ('amazon-title-note', 'The Volt 1000 Listing', 'How the Contoso Volt 1000 is listed.',
     '<p>Amazon lists it as Contoso Volt 1000 Widget with Four AC Outlets and Pass-Through Charging, 1024Wh.</p>',
     NULL, NULL, FALSE, '2026-11-09T12:00:00Z'),
    -- An Offer whose price was checked in January, and a rating we never measured.
    ('stale-offer-note', 'Contoso Volt 1000 Notes', 'Notes on the Contoso Volt 1000 widget.',
     '<p>Notes on the Contoso Volt 1000 widget.</p>',
     NULL,
     '{"@context": "https://schema.org", "@type": "Product", "name": "Contoso Volt 1000", "sku": "B0GATE0002",
       "aggregateRating": {"@type": "AggregateRating", "ratingValue": "4.8", "reviewCount": "120"},
       "offers": {"@type": "Offer", "url": "https://www.amazon.com/dp/B0GATE0002?tag=gatefixture-20", "priceCurrency": "USD",
                  "price": "699.00", "availability": "https://schema.org/InStock", "validFrom": "2026-01-02T00:00:00Z"}}'::jsonb,
     FALSE, '2026-11-09T12:00:00Z'),
    -- Price tracking and a "lowest ever" claim (Participation Requirements 6(y)).
    ('price-alert-note', 'Widget Deals', 'Widget deals this week.',
     '<p>The Northwind PW-500 is at its lowest price ever this week.</p><form class="fx-alert" action="/api/price-alert" method="post"><label for="pa">Email me when the price drops</label> <input id="pa" name="price_alert_email" type="email"> <button type="submit">Set a price alert</button></form>',
     NULL, NULL, FALSE, '2026-11-09T12:00:00Z'),
    -- An og:image on Amazon's image CDN, added by hand in <head>.
    ('amazon-og-note', 'Widget Photo Notes', 'Notes on widget photos.',
     '<p>Notes on widget photos.</p>',
     '<meta property="og:image" content="https://m.media-amazon.com/images/I/og-violation.jpg">', NULL, FALSE, '2026-11-09T12:00:00Z')
  ON CONFLICT (slug) DO NOTHING;
END
$violations$;
