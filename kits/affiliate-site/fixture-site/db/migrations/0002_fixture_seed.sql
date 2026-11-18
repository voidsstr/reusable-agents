-- 0002: the clean fixture. Every page it produces must pass the kit gate.
--
-- Prices are stamped relative to now() here and re-stamped by db/freshen.sql
-- at every gate start (the fixture database is long-lived), so the fresh rows
-- stay quotable and the deliberately stale one (B0GATE0008) stays stale.
-- ASINs are synthetic (B0GATE....): nothing here names a real listing, and
-- every Amazon URL below is only ever answered locally by the gate's browser.
SET LOCAL lock_timeout = '5s';

INSERT INTO fixture_products (asin, model_name, brand, category, lifecycle, summary, capacity_wh, weight_kg, alternative_asin, rank) VALUES
  ('B0GATE0001', 'Northwind PW-500', 'Northwind', 'widgets', 'live',
   'A mid-size widget with a 512 Wh pack, two AC outlets and a 70-minute wall recharge.', 512, 6.1, NULL, 2),
  ('B0GATE0002', 'Contoso Volt 1000', 'Contoso', 'widgets', 'live',
   'A 1,024 Wh widget with four AC outlets and pass-through charging.', 1024, 11.3, NULL, 1),
  ('B0GATE0003', 'Fabrikam Cell 300', 'Fabrikam', 'widgets', 'live',
   'A small 288 Wh widget for phones, laptops and lights.', 288, 3.4, NULL, 4),
  ('B0GATE0004', 'Northwind PW-2000', 'Northwind', 'widgets', 'live',
   'A 2,048 Wh widget on wheels with a 2,400 W inverter.', 2048, 22.0, NULL, 3),
  ('B0GATE0005', 'Contoso Volt Mini', 'Contoso', 'widgets', 'live',
   'The smallest widget here: 256 Wh in under 3 kg.', 256, 2.9, NULL, 5),
  ('B0GATE0006', 'Fabrikam Cell 600', 'Fabrikam', 'widgets', 'unavailable',
   'A 640 Wh widget that Amazon no longer has in stock.', 640, 7.2, 'B0GATE0001', 6),
  ('B0GATE0007', 'Tailspin Spark 800', 'Tailspin', 'widgets', 'live',
   'A 768 Wh widget under an active stop-use recall.', 768, 8.8, 'B0GATE0002', 7),
  ('B0GATE0008', 'Tailspin Spark 400', 'Tailspin', 'widgets', 'live',
   'A 384 Wh widget whose last Amazon price check is older than a day.', 384, 4.6, NULL, 8)
ON CONFLICT (asin) DO NOTHING;

INSERT INTO fixture_item_cache (asin, title, image_url, detail_page_url, price, list_price, currency, availability_type, fetched_at) VALUES
  ('B0GATE0001', 'Northwind PW-500 Portable Widget, 512Wh Capacity, Fast Wall Recharge, Gray',
   'https://m.media-amazon.com/images/I/GATE0001.jpg', 'https://www.amazon.com/dp/B0GATE0001?tag=gatefixture-20&linkCode=ogi',
   349.99, 399.99, 'USD', 'IN_STOCK', now() - interval '90 minutes'),
  ('B0GATE0002', 'Contoso Volt 1000 Widget with Four AC Outlets and Pass-Through Charging, 1024Wh',
   'https://m.media-amazon.com/images/I/GATE0002.jpg', 'https://www.amazon.com/dp/B0GATE0002?tag=gatefixture-20&linkCode=ogi',
   699.00, 799.00, 'USD', 'IN_STOCK', now() - interval '90 minutes'),
  ('B0GATE0003', 'Fabrikam Cell 300 Compact Widget, 288Wh, USB-C PD 100W, Camping and Travel',
   'https://m.media-amazon.com/images/I/GATE0003.jpg', 'https://www.amazon.com/dp/B0GATE0003?tag=gatefixture-20&linkCode=ogi',
   229.95, NULL, 'USD', 'IN_STOCK', now() - interval '90 minutes'),
  ('B0GATE0004', 'Northwind PW-2000 Rolling Widget, 2048Wh, 2400W Inverter, Expandable',
   'https://m.media-amazon.com/images/I/GATE0004.jpg', 'https://www.amazon.com/dp/B0GATE0004?tag=gatefixture-20&linkCode=ogi',
   1499.00, 1799.00, 'USD', 'IN_STOCK', now() - interval '90 minutes'),
  ('B0GATE0005', 'Contoso Volt Mini Widget, 256Wh Ultralight Pack for Phones and Laptops',
   'https://m.media-amazon.com/images/I/GATE0005.jpg', 'https://www.amazon.com/dp/B0GATE0005?tag=gatefixture-20&linkCode=ogi',
   189.00, 219.00, 'USD', 'IN_STOCK_SCARCE', now() - interval '90 minutes'),
  ('B0GATE0006', 'Fabrikam Cell 600 Widget, 640Wh, Dual Charging, Outdoor Ready',
   NULL, 'https://www.amazon.com/dp/B0GATE0006?tag=gatefixture-20&linkCode=ogi',
   NULL, NULL, NULL, 'OUT_OF_STOCK', now() - interval '2 hours'),
  ('B0GATE0007', 'Tailspin Spark 800 Widget, 768Wh, Wireless Charging Pad, Black',
   'https://m.media-amazon.com/images/I/GATE0007.jpg', 'https://www.amazon.com/dp/B0GATE0007?tag=gatefixture-20&linkCode=ogi',
   549.00, NULL, 'USD', 'IN_STOCK', now() - interval '90 minutes'),
  ('B0GATE0008', 'Tailspin Spark 400 Widget, 384Wh, Quiet Fan, Travel Size',
   'https://m.media-amazon.com/images/I/GATE0008.jpg', 'https://www.amazon.com/dp/B0GATE0008?tag=gatefixture-20&linkCode=ogi',
   279.00, NULL, 'USD', 'IN_STOCK', now() - interval '3 days')
ON CONFLICT (asin) DO NOTHING;

INSERT INTO fixture_recalls (id, asin, title, hazard, remedy, status, announced_on, source_url) VALUES
  ('rc-2026-001', 'B0GATE0007', 'Tailspin Spark 800 widgets recalled for overheating',
   'The pack can overheat while charging, posing a fire hazard.',
   'Stop using the widget and contact Tailspin for a free replacement.',
   'stop-use', '2026-10-01', 'https://www.cpsc.gov/Recalls')
ON CONFLICT (id) DO NOTHING;

INSERT INTO fixture_best_lists (slug, title, intro_md, asins, updated_at) VALUES
  ('best-widgets', 'Best Widgets for Home Backup',
   $md$Ranked by usable capacity per kilogram, from the specs each maker publishes.

| Rank | Widget | Capacity | Weight |
| ---: | --- | ---: | ---: |
| 1 | Contoso Volt 1000 | 1,024 Wh | 11.3 kg |
| 2 | Northwind PW-500 | 512 Wh | 6.1 kg |
| 3 | Northwind PW-2000 | 2,048 Wh | 22.0 kg |
| 4 | Fabrikam Cell 300 | 288 Wh | 3.4 kg |$md$,
   ARRAY['B0GATE0002', 'B0GATE0001', 'B0GATE0004', 'B0GATE0003'], '2026-11-02T12:00:00Z')
ON CONFLICT (slug) DO NOTHING;

INSERT INTO editorial_articles
  (slug, title, subtitle, excerpt, body_md, hero_image_url, category, tags, related_product_asins,
   author, status, published_at, updated_at, faqs, outbound_citations)
VALUES
  ('choosing-a-widget', 'How to Choose a Portable Widget',
   'Capacity, weight and outlets, compared',
   'Pick a widget by the hours you need, the outlets you use and the weight you can carry: a comparison of four models with a safety note on home wiring.',
   $md$A widget's capacity, in watt-hours, decides how long it runs your devices; its weight decides whether you will carry it. Start with the loads you need and work back to the size.

## Compare the models

| Widget | Capacity | Weight | AC outlets |
| --- | ---: | ---: | :---: |
| Northwind PW-500 | 512 Wh | 6.1 kg | 2 |
| Contoso Volt 1000 | 1,024 Wh | 11.3 kg | 4 |
| Fabrikam Cell 300 | 288 Wh | 3.4 kg | 1 |

## Safety

Plug appliances straight into the widget. Connecting one to your home's wiring needs a transfer switch installed by a licensed electrician; never backfeed through an outlet.

## Which one to buy

For a weekend away, the [Fabrikam Cell 300](/product/B0GATE0003) is enough. For a longer outage, look at the [Contoso Volt 1000](/product/B0GATE0002).$md$,
   '/assets/hero-guide.svg', 'guides', ARRAY['lane:widgets'], ARRAY['B0GATE0002', 'B0GATE0001'],
   'GateFixture Editorial Team', 'published', '2026-11-01T12:00:00Z', '2026-11-01T12:00:00Z',
   '[{"question": "How big a widget do I need?", "answer": "Add up the watts of what you will run and multiply by the hours you need; pick a widget with at least that many watt-hours."},
     {"question": "Can a widget power my whole house?", "answer": "Not through an outlet. Whole-home backup needs a transfer switch or interlock installed by a licensed electrician."}]'::jsonb,
   ARRAY['https://www.energy.gov/']),
  ('widget-care', 'Widget Care Basics',
   'Storage, charging and the cold',
   'Keep a widget ready for the next outage: store it half charged, top it up every few months and keep it out of freezing garages.',
   $md$A widget that sits in a closet for a year may not be ready when the lights go out. Three habits keep it ready.

## Store it half charged

Most packs age slowest at about half charge. Top it up every three months.

## Keep it out of the cold

Charging below freezing can damage the cells. Bring it inside before you charge it.

## Test it

Run a lamp from it twice a year, so you know it works before you need it. The [Fabrikam Cell 300](/product/B0GATE0003) takes about two hours to refill from the wall.$md$,
   NULL, 'guides', ARRAY['lane:widgets'], ARRAY['B0GATE0003'],
   'GateFixture Editorial Team', 'published', '2026-11-03T12:00:00Z', '2026-11-03T12:00:00Z',
   NULL, NULL)
ON CONFLICT (slug) DO NOTHING;

INSERT INTO fixture_notes (slug, title, description, body_html, amazon_content, updated_at) VALUES
  ('how-this-fixture-works', 'How This Fixture Works',
   'What the gate fixture is for: a site built only from kit modules, so the kit gate can prove each of its checks.',
   '<p>This site exists so the affiliate-site kit''s deploy gate can prove itself. Every page is rendered by kit modules from a small seeded database.</p>
<ul>
  <li>Product pages quote a price only with the time it was checked and the full disclaimer.</li>
  <li>Guides render their comparison tables from Markdown.</li>
  <li>A recalled product keeps its page, but never a buy button.</li>
</ul>
<p>Read <a href="/disclosure">how this site earns money</a> or start with the <a href="/guides">buying guides</a>.</p>',
   FALSE, '2026-11-04T12:00:00Z')
ON CONFLICT (slug) DO NOTHING;
