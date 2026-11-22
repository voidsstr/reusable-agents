/**
 * Affiliate-site kit: public server API.
 *
 * Import from here (vendor/affiliate-kit/server/src in a site). Later kit
 * items add their modules (HTTP middleware, render helpers, migrations) to
 * this barrel; every export below is stable within a minor version.
 */

/** Kit version; must equal ../../VERSION and package.json (unit/index.test.ts checks). */
export const KIT_VERSION = '0.2.0';

export * from './siteContext';
export * from './indexing';
export * from './notices';
export * from './offer';
export * from './links';
export * from './price';
export * from './botDetect';
export * from './aiCrawlerVerify';
export * from './dbBudget';
export * from './verifiedClicks';
export * from './productAvailability';
export * from './substituteQuote';
