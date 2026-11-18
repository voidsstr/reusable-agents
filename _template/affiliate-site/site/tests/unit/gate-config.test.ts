import { gateConfigProblems } from '../../vendor/affiliate-kit/tests/ui/lib/gateConfig';
import type { PageFamily } from '../../vendor/affiliate-kit/tests/ui/lib/manifestTypes';
import { pageRoutes } from '../../src/routes/table';
import {
  CONTENT_POLICY_WAIVED, CONTENT_RULE_SETS, GatePolicyError, RECALLS_WAIVED, composeGateConfig, config, type PageManifest,
} from '../ui/gate.config';
import { PAGE_MANIFEST } from '../ui/pages.manifest';

/**
 * The deploy gate's config, checked offline with the kit's own validator
 * (the one every gate spec runs at load), so a config the gate would refuse
 * fails `npm test` first. And the split between the operator-owned policy
 * (tests/ui/gate.config.ts) and the page manifest (tests/ui/pages.manifest.ts):
 * the manifest can describe pages, never grant the gate a skip.
 */

const family = (over: Partial<PageFamily> = {}): PageFamily => ({
  id: 'extra', routes: [], sourceLabel: 'fixed', source: async () => ['/'], sample: 1, robots: 'index', monetized: false, ...over,
});

describe('the gate config', () => {
  it('passes the kit\'s gate-config validation', () => {
    expect(gateConfigProblems(config)).toEqual([]);
  });

  it('names the recall source or the reason there is none, and the content rule sets or the reason there are none', () => {
    const c = config.compliance ?? {};
    expect(typeof c.recalledProducts === 'function' || (typeof c.recallsWaived === 'string' && c.recallsWaived === RECALLS_WAIVED)).toBe(true);
    if (CONTENT_RULE_SETS.length) {
      expect(c.contentPolicy?.ruleSets).toEqual([...CONTENT_RULE_SETS]);
      expect(CONTENT_POLICY_WAIVED).toBe('');
    } else {
      expect(c.contentPolicy).toBeUndefined();
      expect(c.contentPolicyWaived?.trim().length ?? 0).toBeGreaterThanOrEqual(10);
    }
  });

  it('declares the route table, so a route with no family fails the gate', async () => {
    expect(await config.routes?.declared()).toEqual(pageRoutes());
  });

  it('is the page manifest plus the policy', () => {
    expect(config.families.map((f) => f.id)).toEqual(PAGE_MANIFEST.families.map((f) => f.id));
    expect(config.sitemaps?.files).toEqual(PAGE_MANIFEST.sitemaps?.files);
  });
});

describe('the page manifest cannot grant the gate a skip', () => {
  const refused = (m: PageManifest): string[] => {
    try {
      composeGateConfig(m);
      return [];
    } catch (e) {
      if (!(e instanceof GatePolicyError)) throw e;
      return [...e.problems];
    }
  };

  it('a compliance source or a waiver set in the manifest is refused', () => {
    const m = { ...PAGE_MANIFEST, compliance: { recallsWaived: 'x' }, unbuyable: { products: async () => [] } } as unknown as PageManifest;
    expect(refused(m)).toEqual([
      expect.stringContaining("pages.manifest.ts sets 'compliance'"),
      expect.stringContaining("pages.manifest.ts sets 'unbuyable'"),
    ]);
  });

  it('a family that carries its own check exception or empty-fixture allowance is refused', () => {
    const m: PageManifest = {
      ...PAGE_MANIFEST,
      families: [...PAGE_MANIFEST.families, family({ exceptions: { images: 'no hero yet' }, fixtureOptional: 'none seeded' })],
    };
    expect(refused(m)).toEqual([
      expect.stringContaining("family 'extra' sets exceptions"),
      expect.stringContaining("family 'extra' sets fixtureOptional"),
    ]);
  });

  it('a sitemap fixture waiver in the manifest is refused', () => {
    const m = { ...PAGE_MANIFEST, sitemaps: { files: ['sitemap-pages.xml'], emptyOnFixture: { 'sitemap-x.xml': 'r' } } } as unknown as PageManifest;
    expect(refused(m)).toEqual([expect.stringContaining('sets sitemaps.emptyOnFixture')]);
  });

  it('the scaffold\'s own manifest composes cleanly', () => {
    expect(refused(PAGE_MANIFEST)).toEqual([]);
  });
});
