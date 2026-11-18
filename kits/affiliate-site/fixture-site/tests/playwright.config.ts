import { kitPlaywrightConfig } from '../../tests/playwright.base';

// The gate fixture's Playwright config: the kit's specs plus this fixture's own
// (tests/ui), against the image run-local-suite.sh started. A real site's
// config is the same, importing from vendor/affiliate-kit.
export default kitPlaywrightConfig({
  dir: __dirname,
  siteJson: '../site.json',
  gateConfig: './ui/gate.config.ts',
  siteSpecsDir: './ui',
});
