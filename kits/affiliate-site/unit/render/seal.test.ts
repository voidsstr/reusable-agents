/**
 * sealHtmlFragment: nothing in a content fragment may reach past its end and
 * swallow or hide the page chrome after it (the footer carries the 2(k)
 * notice and the earnings disclosure). Constructs that cannot be closed are
 * escaped and shown as text; elements merely left open get their end tags.
 * Each malformed case below was checked in headless Chromium: unsealed, the
 * footer text after it disappears (or turns into a link); sealed, it shows.
 */
import * as fs from 'fs';
import * as path from 'path';
import { cta } from '../../server/src/links';
import { amazonContentNoticeHtml } from '../../server/src/notices';
import { priceStamp } from '../../server/src/price';
import { loadSiteContext } from '../../server/src/siteContext';
import { jsonLdScripts, productLd } from '../../server/src/render/jsonld';
import { mdToHtml } from '../../server/src/render/md';
import { sealHtmlFragment } from '../../server/src/render/seal';

const KIT = path.resolve(__dirname, '../..');
const ctx = loadSiteContext(path.join(KIT, 'fixtures/site.example.json'), { env: {} });
const NOW = Date.parse('2026-11-17T12:00:00Z');

describe('sealHtmlFragment leaves well-formed markup alone', () => {
  const guide = mdToHtml(ctx, fs.readFileSync(path.join(__dirname, 'fixtures/guide.md'), 'utf8'));
  const cases: string[] = [
    '',
    'plain text, 5 < 6 and a <3',
    '<p>fine</p><br><img src="/a.png" alt="a">',
    '<a title="x > y" href="/z">ok</a>',
    '<script>if (a < b && c<d) { x = "</p>"; }</script>',
    '<script><!-- a --></script><script>if (a<!--b) {}</script><script><!--<script>x</script>y</script>',
    '<style>.a>.b{color:red}</style>',
    '<!-- note --><!----><!--><!--->',
    '<template><p>x</p></template><select><option>a</option></select>',
    '<table><tr><td>x</td></tr></table><object>o</object>',
    '<svg viewBox="0 0 1 1"><style>.s{fill:red}</style><title>t</title><path d="M0 0"/></svg>',
    '<math><![CDATA[ x ]]></math><svg/><p>after</p>',
    '<body><p>no attributes, no effect</p>',
    '<b>bold</b> <i>it<b>alic</b></i> <a href="/1">one</a>',
    cta(ctx, { asin: 'B0ABCDEFGH', placement: 'pdp-hero' }),
    priceStamp(ctx, { price: 899, currency: 'USD', fetchedAt: '2026-11-17T10:00:00Z' }, { now: NOW }),
    jsonLdScripts([productLd(ctx, { asin: 'B0ABCDEFGH', name: 'Alpha </script> <!-- 2000' }, NOW)]),
    amazonContentNoticeHtml(),
    guide,
  ];

  it.each(cases.map((c) => [c.slice(0, 60), c]))('%s', (_label, html) => {
    expect(sealHtmlFragment(html)).toBe(html);
  });

  it('turns null and undefined into the empty string', () => {
    expect(sealHtmlFragment(null)).toBe('');
    expect(sealHtmlFragment(undefined)).toBe('');
  });
});

describe('sealHtmlFragment escapes what can never be closed, so it swallows nothing', () => {
  it.each<[string, string, string]>([
    ['a truncated comment', '<p>a</p><!-- sources: manual p.12,', '<p>a</p>&lt;!-- sources: manual p.12,'],
    ['a closed comment, then an open one', '<!-- a --><p>b</p><!-- c', '<!-- a --><p>b</p>&lt;!-- c'],
    ['an open CDATA section', '<![CDATA[ x', '&lt;![CDATA[ x'],
    ['an open doctype', '<!DOCTYPE html', '&lt;!DOCTYPE html'],
    ['an open processing instruction', '<?xml version', '&lt;?xml version'],
    ['an open bogus end tag', '</ x', '&lt;/ x'],
    ['a tag with an attribute quote left open', '<p class="x>hello', '&lt;p class="x>hello'],
    ['a stray "<word" with no ">"', '<p>if load<fridge', '<p>if load&lt;fridge'],
    ['an end tag the fragment ends inside', '<p>x</p', '<p>x&lt;/p'],
  ])('%s', (_why, input, want) => {
    expect(sealHtmlFragment(input)).toBe(want);
  });

  it.each(['script', 'style', 'textarea', 'title', 'xmp', 'iframe', 'noembed', 'noframes', 'noscript'])(
    'an unclosed <%s> shows as text, and its content reads as markup again',
    (name) => {
      expect(sealHtmlFragment(`<${name} class="c">x <b>y</b>`)).toBe(`&lt;${name} class="c">x <b>y</b>`);
      expect(sealHtmlFragment(`<${name}>x</${name}`)).toBe(`&lt;${name}>x&lt;/${name}`);
      expect(sealHtmlFragment(`<${name}>a</${name} x="`)).toBe(`&lt;${name}>a&lt;/${name} x="`);
    },
  );

  it('plaintext never ends, so it is always shown as text', () => {
    expect(sealHtmlFragment('<plaintext>x</plaintext>')).toBe('&lt;plaintext>x</plaintext>');
  });

  it('follows a script\'s escaped runs: "<!--<script>" hides the next "</script>"', () => {
    expect(sealHtmlFragment('<script><!--<script>x</script>y')).toBe('&lt;script>&lt;!--<script>x</script>y');
    expect(sealHtmlFragment('<script><!--<script>x</script>-->y</script>')).toBe('<script><!--<script>x</script>-->y</script>');
  });

  it('escapes a stray <html> or <body> that would merge its attributes into the page', () => {
    expect(sealHtmlFragment('<p>x</p><body hidden>')).toBe('<p>x</p>&lt;body hidden>');
    expect(sealHtmlFragment('<body style="display:none">')).toBe('&lt;body style="display:none">');
    expect(sealHtmlFragment('<html hidden>')).toBe('&lt;html hidden>');
  });
});

describe('sealHtmlFragment closes what is merely left open', () => {
  it.each<[string, string, string]>([
    ['a template', '<template><p>x</p>', '<template><p>x</p></template>'],
    ['a select', '<select><option>a', '<select><option>a</select>'],
    ['a table cell', '<table><tr><td>x', '<table><tr><td>x</table>'],
    ['an object and a marquee', '<object>o<marquee>m', '<object>o<marquee>m</marquee></object>'],
    ['a hidden formatting element', '<p><b hidden>x</p>', '<p><b hidden>x</p></b>'],
    ['a link (re-created around later text)', '<p><a href="/x">buy</p>', '<p><a href="/x">buy</p></a>'],
    ['nested formatting, innermost first', '<b><i><nobr hidden>x', '<b><i><nobr hidden>x</nobr></i></b>'],
    ['a second <a>, which closes the first', '<a href="/1">x<a href="/2">y', '<a href="/1">x<a href="/2">y</a>'],
    ['a nested <select>, which closes the first', '<select><option>a<select><option>b', '<select><option>a<select><option>b'],
    ['nothing for stray end tags', '</b></template>x', '</b></template>x'],
  ])('%s', (_why, input, want) => {
    expect(sealHtmlFragment(input)).toBe(want);
  });
});

describe('foreign content (svg, math)', () => {
  it('holds no raw-text element, so a comment opener inside an svg <style> is caught', () => {
    expect(sealHtmlFragment('<svg><style>/* <!-- */</style></svg>')).toBe('<svg><style>/* &lt;!-- */</style></svg>');
    expect(sealHtmlFragment('<svg><title>a</svg><p>b</p>')).toBe('<svg><title>a</svg><p>b</p>');
  });

  it('needs "]]>" to end CDATA; a self-closed <svg/> opens nothing', () => {
    expect(sealHtmlFragment('<svg><![CDATA[ x')).toBe('<svg>&lt;![CDATA[ x');
    expect(sealHtmlFragment('<svg/><style>a')).toBe('<svg/>&lt;style>a');
  });
});

describe('sealHtmlFragment is a fixed point and stays fast on hostile input', () => {
  it('sealing twice changes nothing', () => {
    for (const input of [
      '<p>a</p><!-- x', '<script>x', '<template><b hidden>x', '<p class="x>y', '<svg><style><!--', '<select><option>a<b>',
      '<script><!--<script>x</script>y', '<body hidden><table><tr><td>x', '<![CDATA[ <!-- <a href="', '<a href="/1"><a href="/2">',
    ]) {
      const once = sealHtmlFragment(input);
      expect(sealHtmlFragment(once)).toBe(once);
    }
  });

  it.each<[string, string]>([
    ['open quotes', '<a b="'.repeat(20000)],
    ['comment openers', '<!--'.repeat(20000)],
    ['script openers', '<script>'.repeat(20000)],
    ['textarea openers', '<textarea>'.repeat(20000)],
    ['formatting tags', '<b>'.repeat(20000)],
  ])('%s', (_why, input) => {
    const t0 = Date.now();
    const out = sealHtmlFragment(input);
    expect(Date.now() - t0).toBeLessThan(1500);
    expect(sealHtmlFragment(out)).toBe(out);
  });
});
