-- 0005: more deliberately seeded violations, one per way a page can slip
-- past a narrower check: an Amazon buy button written as the kit's own /r/
-- link, as a search for the recalled model, or as a cart link carrying the
-- recalled ASIN second; a page that hides the earnings disclosure; whole-dollar
-- prices, a price in og:title and a product:price meta tag; availability
-- stated outside a stamp; a stamp whose 2(i) disclaimer is hidden with no
-- control to show it; a stamp past the quotable window but inside 24h; a
-- truncated Amazon title with no 2(k) notice; selling copy built on negation
-- words ("never miss", "no other"); an Amazon image stored in an article body;
-- raw JSON-LD with an Amazon Product image and a rating we never measured.
--
-- Like 0003, it lands ONLY in kit_fixture_violations; anywhere else it is a
-- no-op. db/freshen.sql keeps the two hand-made stamps' times where they
-- belong (one hour ago; 23h53m ago). tests/selftest expects each one to turn
-- its own rule red.
SET LOCAL lock_timeout = '5s';

DO $violations$
BEGIN
  IF current_database() <> 'kit_fixture_violations' THEN
    RETURN;
  END IF;

  INSERT INTO editorial_articles
    (slug, title, excerpt, body_md, hero_image_url, category, related_product_asins, status, published_at, updated_at)
  VALUES
    -- A buy button for the recalled SKU through the kit's click redirect;
    -- mdToHtml writes /r/ links absolute on the canonical origin.
    ('recalled-redirect-guide', 'A Widget for a Cabin Weekend',
     'Which widget to bring for two nights in a cabin: capacity for lights and a fan, and a carry handle.',
     $md$Two nights of lights and a fan need about 500 Wh. [Check the Tailspin Spark 800](/r/B0GATE0007) for a pack that size.$md$,
     NULL, 'guides', ARRAY[]::text[], 'published', '2026-11-10T12:00:00Z', '2026-11-10T12:00:00Z'),
    -- An Amazon image link stored in the article body (IP License 2(h));
    -- mdToHtml drops it when rendering, so only the stored row shows it.
    ('amazon-body-image-guide', 'What a Widget Looks Like Inside',
     'The cells, the inverter and the charge controller inside a typical widget, and what each one does.',
     $md$Inside every widget are three parts: the cells, the inverter and the charge controller.

![The cells of a widget](https://m.media-amazon.com/images/I/body-violation.jpg)

The cells store the energy; the inverter turns it into household current.$md$,
     NULL, 'guides', ARRAY[]::text[], 'published', '2026-11-11T12:00:00Z', '2026-11-11T12:00:00Z')
  ON CONFLICT (slug) DO NOTHING;

  INSERT INTO fixture_notes (slug, title, description, body_html, head_html, jsonld, amazon_content, updated_at) VALUES
    -- A compliant Amazon link on a page whose CSS hides the earnings disclosure
    -- (Operating Agreement section 5).
    ('hidden-disclosure-note', 'A Widget for the Car', 'A widget that fits under a car seat.',
     '<p>The Fabrikam Cell 300 fits under a car seat. <a href="https://www.amazon.com/dp/B0GATE0003?tag=gatefixture-20" rel="sponsored nofollow noopener" target="_blank">See the Fabrikam Cell 300 on Amazon</a></p>',
     '<style>.kit-earnings-disclosure{display:none!important}</style>', NULL, FALSE, '2026-11-12T12:00:00Z'),
    -- Whole-dollar prices with no stamp, a price in og:title and a
    -- product:price meta tag (none of them can carry the 2(i) time).
    ('whole-dollar-price-note', 'What a Backup Kit Costs', 'What a home backup kit adds up to.',
     '<p>The Northwind PW-500 sells for $349 right now, and a full backup kit totals $1,249.</p>',
     '<meta property="og:title" content="Northwind PW-500: $349.99 on Amazon"><meta property="product:price:amount" content="349.00"><meta property="product:price:currency" content="USD">',
     NULL, FALSE, '2026-11-12T12:00:00Z'),
    -- Availability stated outside a stamp.
    ('availability-claim-note', 'Fabrikam Cell 300 Stock', 'Where the Fabrikam Cell 300 stands this week.',
     '<p>The Fabrikam Cell 300 is in stock on Amazon today. Only 3 left!</p>',
     NULL, NULL, FALSE, '2026-11-12T12:00:00Z'),
    -- A stamp checked within the window whose 2(i) disclaimer is in the
    -- markup but hidden, with no Details control to show it.
    ('hidden-disclaimer-note', 'Fabrikam Cell 300 Price', 'The Fabrikam Cell 300 price check.',
     '<p>Fabrikam Cell 300: <span class="kit-price-stamp" data-price-stamp="price"><span class="ssr-price">$229.95</span> on Amazon.com as of <time datetime="2026-11-17T12:00:00.000Z">the last check</time> <span style="display:none">Product prices and availability are accurate as of the date/time indicated and are subject to change. Any price and availability information displayed on Amazon.com at the time of purchase will apply to the purchase of this product.</span></span></p>',
     NULL, NULL, FALSE, '2026-11-12T12:00:00Z'),
    -- A complete stamp checked 23h53m ago: inside 24h, but past the quotable
    -- window (24h minus the HTML cache lifetimes), so a cached copy could
    -- outlive 24h.
    ('edge-stamp-note', 'Northwind PW-2000 Price', 'The Northwind PW-2000 price check.',
     '<p>Northwind PW-2000: <span class="kit-price-stamp" data-price-stamp="price"><span class="ssr-price">$1,499.00</span> on Amazon.com as of <time datetime="2026-11-17T12:00:00.000Z">the last check</time> (price may vary) <button type="button" class="kit-price-details" popovertarget="edge-pd">Details</button> <span id="edge-pd" class="kit-price-disclaimer" popover role="note">Product prices and availability are accurate as of the date/time indicated and are subject to change. Any price and availability information displayed on Amazon.com at the time of purchase will apply to the purchase of this product.</span></span></p>',
     NULL, NULL, FALSE, '2026-11-12T12:00:00Z'),
    -- A truncated Amazon title on a page that never shows the 2(k) notice.
    ('truncated-title-note', 'How the Volt 1000 Is Listed', 'How the Contoso Volt 1000 is listed.',
     '<p>Amazon lists it as Contoso Volt 1000 Widget with Four AC Outlets and Pass-Through…</p>',
     NULL, NULL, FALSE, '2026-11-12T12:00:00Z'),
    -- Buy buttons for the recalled SKU that do not name it in a /dp/ link: a
    -- search for its model, and a cart link carrying it second.
    ('recall-search-note', 'Spark 800 Shopping Links', 'Shopping links for the Tailspin widgets.',
     '<p><a href="https://www.amazon.com/s?k=Tailspin+Spark+800&amp;tag=gatefixture-20" rel="sponsored nofollow noopener" target="_blank">Search Amazon for the Tailspin Spark 800</a></p><p><a href="https://www.amazon.com/gp/aws/cart/add.html?ASIN.1=B0GATE0001&amp;ASIN.2=B0GATE0007&amp;tag=gatefixture-20" rel="sponsored nofollow noopener" target="_blank">Add both to your cart</a></p>',
     NULL, NULL, FALSE, '2026-11-12T12:00:00Z'),
    -- Price-tracking and "lowest ever" copy built on negation words (6(y)).
    ('deal-idiom-note', 'This Week in Widgets', 'Widget news this week.',
     '<p>Never miss a deal: set a price alert for this widget.</p><p>Don''t miss out on the lowest price ever for the Contoso Volt 1000.</p><p>No other store beats this all-time low.</p><p>Get notified when the price drops.</p><p>Track this price and we will email you.</p>',
     NULL, NULL, FALSE, '2026-11-12T12:00:00Z'),
    -- JSON-LD a renderer printed itself, past the kit's serializer: an Amazon
    -- CDN image as the Product image, and a rating the site never measured.
    ('raw-jsonld-note', 'Northwind PW-500 Notes', 'Notes on the Northwind PW-500 widget.',
     '<p>Notes on the Northwind PW-500 widget.</p>',
     '<script type="application/ld+json">{"@context":"https://schema.org","@type":"Product","name":"Northwind PW-500","image":"https://m.media-amazon.com/images/I/ld-violation.jpg","aggregateRating":{"@type":"AggregateRating","ratingValue":"4.9","reviewCount":"88"}}</script>',
     NULL, FALSE, '2026-11-12T12:00:00Z')
  ON CONFLICT (slug) DO NOTHING;
END
$violations$;
