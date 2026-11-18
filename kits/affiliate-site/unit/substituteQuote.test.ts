/**
 * The substitute an unbuyable product page leads with is remembered for the
 * head render and quoted only while its price is quotable. No upstream test
 * existed; these pin the port's behaviour.
 */
import * as path from 'path';
import {
  SUBSTITUTE_TTL_MS,
  SubstituteMemo,
  recallSubstitute,
  rememberSubstitute,
  resetSubstituteMemo,
  type SubstituteQuote,
} from '../server/src/substituteQuote';
import { loadSiteContext } from '../server/src/siteContext';

const ctx = loadSiteContext(path.resolve(__dirname, '../fixtures/site.example.json'), { env: {} });
const T0 = Date.parse('2026-11-17T12:00:00Z');

const quote = (over: Partial<SubstituteQuote> = {}): SubstituteQuote => ({
  asin: 'B0C5C9HMQZ',
  title: 'EcoFlow DELTA 2',
  brand: 'EcoFlow',
  spec: '1,024 Wh LiFePO4, 1,800 W',
  price: 449,
  currency: 'USD',
  fetchedAt: new Date(T0 - 2 * 3600e3).toISOString(),
  ...over,
});

describe('SubstituteMemo', () => {
  it('recalls what the body recorded, with the quotable price', () => {
    let now = T0;
    const m = new SubstituteMemo({ now: () => now });
    m.remember('B0DS2X13PH', quote());
    expect(m.recall(ctx, 'B0DS2X13PH')).toEqual({ ...quote(), quote: 449 });
    expect(m.recall(ctx, 'B000000000')).toBeNull();
    now = T0 + 60_000;
    expect(m.recall(ctx, 'B0DS2X13PH')?.quote).toBe(449);
  });

  it('re-checks the price rule at recall time and never quotes a stale or foreign price', () => {
    const m = new SubstituteMemo({ now: () => T0 });
    m.remember('P1', quote({ fetchedAt: new Date(T0 - 23.9 * 3600e3).toISOString() }));
    m.remember('P2', quote({ currency: 'CAD' }));
    m.remember('P3', quote({ price: 0.5 }));
    expect(m.recall(ctx, 'P1')).toBeNull(); // inside 24h, outside 24h minus the cache age
    expect(m.recall(ctx, 'P2')).toBeNull();
    expect(m.recall(ctx, 'P3')).toBeNull();
    expect(m.size).toBe(3); // an unquotable price is not a reason to forget the pick
  });

  it('forgets an entry after the TTL', () => {
    let now = T0;
    const m = new SubstituteMemo({ now: () => now });
    m.remember('P1', quote({ fetchedAt: new Date(T0).toISOString() }));
    now = T0 + SUBSTITUTE_TTL_MS + 1;
    expect(m.recall(ctx, 'P1')).toBeNull();
    expect(m.size).toBe(0);
  });

  it('clears on null, ignores an empty key, and copies the quote', () => {
    const m = new SubstituteMemo({ now: () => T0 });
    const q = quote();
    m.remember('P1', q);
    q.price = 1; // mutating the caller's object must not change the memo
    expect(m.recall(ctx, 'P1')?.quote).toBe(449);
    m.remember('P1', null);
    expect(m.recall(ctx, 'P1')).toBeNull();
    m.remember('', quote());
    expect(m.size).toBe(0);
  });

  it('evicts the oldest entry at the cap, and re-remembering refreshes recency', () => {
    const m = new SubstituteMemo({ now: () => T0, maxEntries: 2 });
    m.remember('A', quote());
    m.remember('B', quote());
    m.remember('A', quote()); // A is now newest
    m.remember('C', quote()); // evicts B
    expect(m.recall(ctx, 'A')).not.toBeNull();
    expect(m.recall(ctx, 'B')).toBeNull();
    expect(m.recall(ctx, 'C')).not.toBeNull();
    expect(m.size).toBe(2);
  });
});

describe('module default memo', () => {
  afterEach(() => resetSubstituteMemo());

  it('backs rememberSubstitute / recallSubstitute', () => {
    const fresh = quote({ fetchedAt: new Date().toISOString() });
    rememberSubstitute('B0DS2X13PH', fresh);
    expect(recallSubstitute(ctx, 'B0DS2X13PH')?.asin).toBe('B0C5C9HMQZ');
    resetSubstituteMemo();
    expect(recallSubstitute(ctx, 'B0DS2X13PH')).toBeNull();
  });
});
