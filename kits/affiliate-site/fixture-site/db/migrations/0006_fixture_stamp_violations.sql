-- 0006: prices that slip past a dated stamp in the ways kit 0.2.0's stamp rule
-- (kit-compliance, prices and availability) catches: a hand-made "as of" in
-- a sentence with no stamp; stamps that show no time with a zone, keep the
-- 2(i) disclaimer inline with no "Details" control, hold two prices, or show
-- a time that is not their own <time datetime>; and prices a reader never
-- sees but crawlers and assistants read (hidden text, an aria-label), or that
-- hide inside data-kit-not-a-price.
--
-- Like 0003 and 0005, it lands ONLY in kit_fixture_violations; anywhere else
-- it is a no-op. The stamps' check times are left as written (db/freshen.sql
-- does not re-stamp them): each note is red for its own reason whatever their
-- age. tests/selftest expects each one in the prices rule's failures.
SET LOCAL lock_timeout = '5s';

DO $violations$
BEGIN
  IF current_database() <> 'kit_fixture_violations' THEN
    RETURN;
  END IF;

  INSERT INTO fixture_notes (slug, title, description, body_html, head_html, jsonld, amazon_content, updated_at) VALUES
    -- A price dated by hand inside the answer sentence, with no stamp.
    ('sentence-price-note', 'What Runs a Fridge All Day', 'Which widgets run a fridge for a whole day.',
     '<p>Of 6 widgets with verified specs, 2 run a fridge all day; the cheapest is the Northwind PW-500, $349 (as of Nov 17).</p>',
     NULL, NULL, FALSE, '2026-11-12T12:00:00Z'),
    -- A stamp that shows a date but no time and no zone.
    ('stamp-no-time-note', 'Northwind PW-500 Price Check', 'A price check on the Northwind PW-500 widget.',
     '<p>Northwind PW-500: <span class="kit-price-stamp" data-price-stamp="price"><span class="ssr-price">$349.99</span> on Amazon.com as of <time datetime="2026-11-17T12:00:00.000Z">Nov 17</time> (price may vary) <button type="button" class="kit-price-details" popovertarget="nt-pd">Details</button> <span id="nt-pd" class="kit-price-disclaimer" popover role="note">Product prices and availability are accurate as of the date/time indicated and are subject to change. Any price and availability information displayed on Amazon.com at the time of purchase will apply to the purchase of this product.</span></span></p>',
     NULL, NULL, FALSE, '2026-11-12T12:00:00Z'),
    -- A stamp with its 2(i) disclaimer printed inline and no Details control.
    ('stamp-inline-disclaimer-note', 'Contoso Volt 1000 Price Check', 'A price check on the Contoso Volt 1000 widget.',
     '<p>Contoso Volt 1000: <span class="kit-price-stamp" data-price-stamp="price"><span class="ssr-price">$699.00</span> on Amazon.com as of <time datetime="2026-11-17T12:00:00.000Z">2026-11-17 12:00 UTC</time> (price may vary). Product prices and availability are accurate as of the date/time indicated and are subject to change. Any price and availability information displayed on Amazon.com at the time of purchase will apply to the purchase of this product.</span></p>',
     NULL, NULL, FALSE, '2026-11-12T12:00:00Z'),
    -- One stamp wrapped around a sentence that quotes two prices.
    ('two-price-stamp-note', 'Two Widgets Compared on Price', 'The Northwind PW-500 and the Fabrikam Cell 300 side by side.',
     '<p><span class="kit-price-stamp" data-price-stamp="price">The Northwind PW-500 is <span class="ssr-price">$349.99</span> and the Fabrikam Cell 300 sells for $229.95 on Amazon.com as of <time datetime="2026-11-17T12:00:00.000Z">2026-11-17 12:00 UTC</time> <button type="button" class="kit-price-details" popovertarget="tp-pd">Details</button> <span id="tp-pd" class="kit-price-disclaimer" popover role="note">Product prices and availability are accurate as of the date/time indicated and are subject to change. Any price and availability information displayed on Amazon.com at the time of purchase will apply to the purchase of this product.</span></span></p>',
     NULL, NULL, FALSE, '2026-11-12T12:00:00Z'),
    -- A stamp that shows another time than its <time datetime> (the render time, say).
    ('stamp-wrong-time-note', 'Tailspin Spark 800 Price Check', 'A price check on the Tailspin Spark 800 widget.',
     '<p>Tailspin Spark 800: <span class="kit-price-stamp" data-price-stamp="price"><span class="ssr-price">$449.00</span> on Amazon.com as of <time datetime="2026-11-17T12:00:00.000Z">2026-11-17 18:30 UTC</time> (price may vary) <button type="button" class="kit-price-details" popovertarget="wt-pd">Details</button> <span id="wt-pd" class="kit-price-disclaimer" popover role="note">Product prices and availability are accurate as of the date/time indicated and are subject to change. Any price and availability information displayed on Amazon.com at the time of purchase will apply to the purchase of this product.</span></span></p>',
     NULL, NULL, FALSE, '2026-11-12T12:00:00Z'),
    -- Prices a reader does not see: hidden text and an aria-label.
    ('hidden-price-note', 'Northwind PW-500 Specs', 'The Northwind PW-500 widget specs.',
     '<p>The Northwind PW-500 stores 512 Wh.</p><div hidden>Northwind PW-500: $349.99 on Amazon.</div><p><a href="/product/B0GATE0001" aria-label="Northwind PW-500, $349.99 on Amazon">Northwind PW-500 specs</a></p>',
     NULL, NULL, FALSE, '2026-11-12T12:00:00Z'),
    -- An Amazon price inside the marker meant for rates and budgets.
    ('not-a-price-note', 'Widget Running Costs', 'What a widget costs to recharge from the grid.',
     '<p data-kit-not-a-price>Recharging costs about $0.15 per kWh. The Northwind PW-500 is $349 on Amazon this week.</p>',
     NULL, NULL, FALSE, '2026-11-12T12:00:00Z')
  ON CONFLICT (slug) DO NOTHING;
END
$violations$;
