/**
 * Retired products: Amazon-unavailable rows get a real (noindexed) page and a
 * tagged SEARCH link, junk rows stay thin. Ported from SpecPicks
 * tests/unit/inactiveProduct.test.ts and productLinkGuard.test.ts ('shared
 * retired-page rule'); the kit has no eBay/retro branch.
 */
import * as path from 'path';
import {
  amazonSearchUrlFor,
  inactivePageRenders,
  inactiveReason,
  productSearchTerms,
  type InactiveProductRow,
} from '../server/src/productAvailability';
import { loadSiteContext } from '../server/src/siteContext';

const ctx = loadSiteContext(path.resolve(__dirname, '../fixtures/site.example.json'), { env: {} });

describe('inactiveReason', () => {
  it.each<[unknown, string | null]>([
    [['no-buybox', 'no-buybox'], 'unavailable-on-amazon'],
    [['dead-amazon-link', 'unavailable-listing', 'amazon-not-found'], 'unavailable-on-amazon'],
    [['stale-price-unbuyable'], 'unavailable-on-amazon'],
    [['no-buybox', 'price-implausible-20260903'], 'unavailable-on-amazon'],
    [['No-Buybox'], 'unavailable-on-amazon'],
    [['no-buybox', 'ai-unclassifiable'], 'junk'],
    [['off-vertical-solar-charge-controller'], 'junk'],
    [['miscategorised:gaming-desks:pi-rec-013'], 'junk'],
    [['miscategorized-power'], 'junk'],
    [['foreign-marketplace-2026-05-24'], 'junk'],
    [['shelf-image-mismatch-20260902c', 'dead-amazon-link'], 'junk'],
    [['no-buyboxes-anywhere'], null],
    [[], null],
    [null, null],
    [{}, null],
    ['["no-buybox"]', 'unavailable-on-amazon'],
    ['not json', null],
  ])('%j -> %s', (flags, want) => {
    expect(inactiveReason(flags)).toBe(want);
  });
});

describe('search fallbacks', () => {
  it('builds a tagged Amazon SEARCH url from brand + model, never a /dp/ link', () => {
    const u = amazonSearchUrlFor(ctx, 'MSI GeForce RTX 3060 Ventus 3X 12G OC, Gaming Graphics Card - RTX 3060', 'MSI', 'kitexample-20');
    expect(u).toBe('https://www.amazon.com/s?k=MSI+GeForce+RTX+3060+Ventus+3X+12G&tag=kitexample-20');
    expect(u).not.toContain('/dp/');
  });

  it('defaults to the partner tag and prefixes a brand the title omits', () => {
    expect(amazonSearchUrlFor(ctx, 'Explorer 1000 v2 Portable Power Station (1070Wh)', 'Jackery'))
      .toBe('https://www.amazon.com/s?k=Jackery+Explorer+1000+v2+Portable+Power+Station&tag=kitexample-20');
  });

  it('keeps one of the site\'s own tags, and replaces any other with the partner tag', () => {
    expect(amazonSearchUrlFor(ctx, 'Jackery Explorer 1000 v2', 'Jackery', 'kitexample-guides-20'))
      .toBe('https://www.amazon.com/s?k=Jackery+Explorer+1000+v2&tag=kitexample-guides-20');
    expect(amazonSearchUrlFor(ctx, 'Jackery Explorer 1000 v2', 'Jackery', 'evil-20'))
      .toBe('https://www.amazon.com/s?k=Jackery+Explorer+1000+v2&tag=kitexample-20');
  });

  it('keeps the model head', () => {
    expect(productSearchTerms('Nintendo Switch Lite - Turquoise', 'Nintendo')).toBe('Nintendo Switch Lite Turquoise');
    expect(productSearchTerms('Jackery Explorer 1000 v2, 1070Wh', 'Jackery')).toBe('Jackery Explorer 1000 v2');
    expect(productSearchTerms('', 'Anker')).toBe('Anker');
    expect(productSearchTerms('!!', null)).toBe('!!');
  });

  it('drops one-character tokens, as upstream does (known gap: a trailing single-digit model number)', () => {
    // Faithful port of SpecPicks' filter(w => w.length > 1). "DELTA 2" searches as
    // "EcoFlow DELTA"; fix upstream and here together (kit-change path), not here alone.
    expect(productSearchTerms('EcoFlow DELTA 2 | 1024Wh LFP', 'ecoflow')).toBe('EcoFlow DELTA');
  });
});

describe('shared retired-page rule (no eBay branch)', () => {
  const unavailable: InactiveProductRow = {
    title: 'MSI GeForce RTX 3060 Ventus 3X 12G OC', category_slug: 'gpus',
    main_image_url: 'https://cdn.example.com/x.jpg', quality_flags: ['no-buybox'],
  };

  it('renders only Amazon-unavailable rows that have a title, image and category', () => {
    expect(inactivePageRenders(unavailable)).toBe(true);
    expect(inactivePageRenders({ ...unavailable, quality_flags: ['off-vertical-console'] })).toBe(false);
    expect(inactivePageRenders({ ...unavailable, quality_flags: [] })).toBe(false);
    expect(inactivePageRenders({ ...unavailable, main_image_url: null })).toBe(false);
    expect(inactivePageRenders({ ...unavailable, category_slug: '' })).toBe(false);
    expect(inactivePageRenders({ ...unavailable, title: null })).toBe(false);
    expect(inactivePageRenders(null)).toBe(false);
  });
});
