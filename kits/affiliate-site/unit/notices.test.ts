/**
 * The Amazon notices are pinned to the exact wording on the live policy pages
 * (checked 2026-11-17). These literals are typed independently of
 * server/src/notices.ts on purpose: editing one without the other fails here.
 */
import * as path from 'path';
import {
  AMAZON_CONTENT_NOTICE,
  AMAZON_CONTENT_NOTICE_TEMPLATE,
  AMAZON_SITE_PLACEHOLDER,
  ASSOCIATE_DISCLOSURE,
  NOTICES_VERIFIED_ON,
  PRICE_AVAILABILITY_DISCLAIMER_TEMPLATE,
  amazonContentNoticeHtml,
  containsNotice,
  earningsDisclosureHtml,
  normalizeNoticeText,
  priceAvailabilityDisclaimer,
  priceDisclaimerFor,
} from '../server/src/notices';
import { loadSiteContext } from '../server/src/siteContext';

const ctx = loadSiteContext(path.resolve(__dirname, '../fixtures/site.example.json'), { env: {} });

// From https://affiliate-program.amazon.com/help/operating/policies, IP License (i) and (k).
const POLICY_2I = 'Product prices and availability are accurate as of the date/time indicated and are subject to change. Any price and availability information displayed on [relevant Amazon Site(s), as applicable] at the time of purchase will apply to the purchase of this product.';
const POLICY_2K = 'CERTAIN CONTENT THAT APPEARS [IN THIS APPLICATION or ON THIS SITE, as applicable] COMES FROM AMAZON. THIS CONTENT IS PROVIDED ‘AS IS’ AND IS SUBJECT TO CHANGE OR REMOVAL AT ANY TIME.';
// From https://affiliate-program.amazon.com/help/operating/agreement, section 5.
const AGREEMENT_5 = 'As an Amazon Associate I earn from qualifying purchases.';

describe('IP License 2(i): price and availability disclaimer', () => {
  it('matches the policy text exactly', () => {
    expect(PRICE_AVAILABILITY_DISCLAIMER_TEMPLATE).toBe(POLICY_2I);
    expect(PRICE_AVAILABILITY_DISCLAIMER_TEMPLATE).toContain(AMAZON_SITE_PLACEHOLDER);
  });

  it('names the site\'s Amazon Site in place of the bracket', () => {
    const want = POLICY_2I.replace('[relevant Amazon Site(s), as applicable]', 'Amazon.com');
    expect(priceAvailabilityDisclaimer('Amazon.com')).toBe(want);
    expect(priceDisclaimerFor(ctx)).toBe(want);
    expect(priceDisclaimerFor(ctx)).not.toContain('[');
  });

  it('refuses an empty site name', () => {
    expect(() => priceAvailabilityDisclaimer('  ')).toThrow(/Amazon Site name/);
  });
});

describe('IP License 2(k): Amazon content notice', () => {
  it('matches the policy text exactly, curly quotes included', () => {
    expect(AMAZON_CONTENT_NOTICE_TEMPLATE).toBe(POLICY_2K);
    expect(AMAZON_CONTENT_NOTICE).toBe(POLICY_2K.replace('[IN THIS APPLICATION or ON THIS SITE, as applicable]', 'ON THIS SITE'));
    expect(AMAZON_CONTENT_NOTICE).toContain('‘AS IS’');
    expect(AMAZON_CONTENT_NOTICE).not.toContain("'AS IS'");
  });

  it('renders in plain view with the text verbatim in the HTML source', () => {
    const html = amazonContentNoticeHtml();
    expect(html).toBe(`<p class="kit-amazon-notice" data-amazon-notice="2k" data-nosnippet>${AMAZON_CONTENT_NOTICE}</p>`);
    expect(html).not.toMatch(/hidden|display:\s*none|aria-hidden/);
  });
});

describe('Operating Agreement section 5', () => {
  it('pins the statement', () => {
    expect(ASSOCIATE_DISCLOSURE).toBe(AGREEMENT_5);
    expect(NOTICES_VERIFIED_ON).toMatch(/^\d{4}-\d{2}-\d{2}$/);
  });

  it('renders the site\'s disclosure, linked to its disclosure page', () => {
    const html = earningsDisclosureHtml(ctx);
    expect(html).toBe('<p class="kit-earnings-disclosure" data-earnings-disclosure><a href="https://example.com/disclosure">As an Amazon Associate, ExamplePicks earns from qualifying purchases.</a></p>');
  });

  it('escapes the configured text', () => {
    const c = { ...ctx, disclosure: { earnings: 'As an Amazon Associate, Bob\'s <Picks> earns from qualifying purchases.', pagePath: '/d' } };
    expect(earningsDisclosureHtml(c)).toContain('Bob&#39;s &lt;Picks&gt;');
  });
});

describe('notice matching for compliance checks', () => {
  it('ignores whitespace differences only', () => {
    const page = `<footer>\n  ${AMAZON_CONTENT_NOTICE.replace('AMAZON. THIS', 'AMAZON.\n      THIS')}\n</footer>`;
    expect(containsNotice(page, AMAZON_CONTENT_NOTICE)).toBe(true);
    expect(containsNotice(page.replace('‘AS IS’', "'AS IS'"), AMAZON_CONTENT_NOTICE)).toBe(false);
    expect(normalizeNoticeText('  a \n\t b  ')).toBe('a b');
  });
});
