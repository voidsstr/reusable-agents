import { test, expect } from '@playwright/test';
import { BASE_URL } from '../../../tests/ui/lib/site';

// The fixture's own spec (the 'site' project), the way a real site keeps its
// niche checks next to the kit's: here, the two data states the kit's price
// rule turns into page states.

test.describe('gate fixture: price freshness drives what a product page shows', () => {
  test('a product whose last Amazon check is older than a day shows no price, no Amazon title, but keeps its buy button', async ({ request }) => {
    const r = await request.get(`${BASE_URL}/product/B0GATE0008`);
    expect(r.status()).toBe(200);
    const html = await r.text();
    expect(html, 'no price stamp').not.toContain('data-price-stamp="price"');
    expect(html, 'no Amazon title').not.toContain('Tailspin Spark 400 Widget, 384Wh');
    expect(html, 'no Offer').not.toContain('"@type":"Offer"');
    expect(html, 'the buy button stays').toMatch(/data-cta-placement="pdp-hero" href="https:\/\/www\.amazon\.com\/dp\/B0GATE0008\?tag=/);
  });

  test('a product checked just past the quotable window (24h minus the cache lifetimes, inside 24h) shows no price, no Offer and no Amazon title', async ({ request }) => {
    // db/freshen.sql keeps B0GATE0009's check 23h53m old; the site's window is 24h minus 660 s of HTML caching.
    const r = await request.get(`${BASE_URL}/product/B0GATE0009`);
    expect(r.status()).toBe(200);
    const html = await r.text();
    expect(html, 'no price or availability stamp').not.toContain('data-price-stamp');
    expect(html, 'no Offer').not.toContain('"@type":"Offer"');
    expect(html, 'no Amazon title').not.toContain('Fabrikam Cell 900 Widget, 960Wh');
    expect(html, 'the buy button stays').toMatch(/data-cta-placement="pdp-hero" href="https:\/\/www\.amazon\.com\/dp\/B0GATE0009\?tag=/);
  });

  test('a fresh product shows its price stamp, the Amazon title it is listed under and an Offer', async ({ request }) => {
    const r = await request.get(`${BASE_URL}/product/B0GATE0001`);
    expect(r.status()).toBe(200);
    const html = await r.text();
    expect(html).toContain('data-price-stamp="price"');
    expect(html).toContain('data-amazon-content>Northwind PW-500 Portable Widget');
    expect(html).toContain('"@type":"Offer"');
  });
});
