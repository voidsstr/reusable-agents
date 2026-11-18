import { kitPlaywrightConfig } from '../vendor/affiliate-kit/tests/playwright.base';

// The deploy gate's Playwright config: the kit's specs (project 'kit') plus this
// site's own (project 'site', tests/ui/*.spec.ts), against the production image
// tests/run-local-suite.sh started. The kit's specs read this site through
// site.json and tests/ui/gate.config.ts (the operator-owned gate policy, which
// composes the page manifest, tests/ui/pages.manifest.ts).
export default kitPlaywrightConfig({
  dir: __dirname,
  siteJson: '../site.json',
  gateConfig: './ui/gate.config.ts',
  siteSpecsDir: './ui',
});
