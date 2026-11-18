/**
 * mdToHtml: SpecPicks' markdown renderer, ported, with GFM tables rendered as
 * table.md-table, and the kit's Amazon rules applied to every link and image
 * in a stored body (re-tagged with the site's own tag or unlinked; no Amazon
 * CDN images). The first block ports SpecPicks' own tests
 * (tests/unit/markdownLinkNested.test.ts) with the site origin swapped.
 */
import * as fs from 'fs';
import * as path from 'path';
import { AFFILIATE_REL, PLACEMENT_ATTR, cta } from '../../server/src/links';
import { loadSiteContext, parseSiteContext, type SiteJson } from '../../server/src/siteContext';
import {
  DEFAULT_MD_PLACEMENT,
  MD_LINK_TEXT,
  enforceAffiliateLinks,
  headingId,
  mdLinkRe,
  mdToHtml,
  productHrefRe,
  resolveNestedLink,
  underscoreEmphasis,
  unwrapNestedLinks,
} from '../../server/src/render/md';

const KIT = path.resolve(__dirname, '../..');
const ctx = loadSiteContext(path.join(KIT, 'fixtures/site.example.json'), { env: {} });

const URL_4475 = 'https://huggingface.co/black-forest-labs/FLUX.2-klein-4B';
const TEXT_4475 = 'Black Forest Labs — FLUX.2 [klein] 4B model card, Hugging Face';
const CITE_4475 = `[${TEXT_4475}](${URL_4475})`;
const EXT = 'rel="noopener noreferrer" target="_blank"';

/** Every <a ...> start tag in `html`, with its attributes. */
function anchors(html: string): Array<Record<string, string>> {
  return Array.from(html.matchAll(/<a\b([^>]*)>/g)).map((m) => {
    const attrs: Record<string, string> = {};
    for (const a of (m[1] as string).matchAll(/([\w-]+)="([^"]*)"/g)) attrs[a[1] as string] = (a[2] as string).replace(/&amp;/g, '&');
    return attrs;
  });
}

/** Visible text: tags dropped, the common entities decoded. */
function text(html: string): string {
  return html.replace(/<[^>]+>/g, '').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, '&');
}

describe('mdToHtml: link text with brackets (ported from SpecPicks)', () => {
  it('renders the 4475 citation as one anchor that keeps [klein] in its text', () => {
    const html = mdToHtml(ctx, `- ${CITE_4475} (accessed 2026-10-17)`);
    expect(html).toContain(`<li><a href="${URL_4475}" ${EXT}>${TEXT_4475}</a> (accessed 2026-10-17)</li>`);
    expect(html).not.toContain('](');
  });

  it('renders two links on one line when only one has brackets', () => {
    const html = mdToHtml(ctx, `See ${CITE_4475} and [JarvisLabs — Running FLUX.2 Klein](https://docs.jarvislabs.ai/x).`);
    expect(html.match(/<a /g)).toHaveLength(2);
    expect(html).toContain(`>${TEXT_4475}</a>`);
    expect(html).toContain('>JarvisLabs — Running FLUX.2 Klein</a>');
  });

  it('leaves plain brackets alone: footnotes, indexes, checkboxes', () => {
    expect(mdToHtml(ctx, 'Footnote [1], index a[0] and a box [x] stay text.')).toBe('<p>Footnote [1], index a[0] and a box [x] stay text.</p>');
  });

  it('links only the real link after a bracket pair that has no url', () => {
    expect(mdToHtml(ctx, 'See [1] and [the docs](https://docs.example.org/d).'))
      .toBe(`<p>See [1] and <a href="https://docs.example.org/d" ${EXT}>the docs</a>.</p>`);
  });

  it('links only the inner link when the outer bracket never closes', () => {
    expect(mdToHtml(ctx, '[foo [bar](https://docs.example.org/b)')).toBe(`<p>[foo <a href="https://docs.example.org/b" ${EXT}>bar</a></p>`);
  });

  it('renders images, with brackets in the alt or an empty alt, and never wraps them in an anchor', () => {
    expect(mdToHtml(ctx, '![FLUX.2 [klein] sample](https://cdn.example.org/a.png)'))
      .toBe('<p><img src="https://cdn.example.org/a.png" alt="FLUX.2 [klein] sample" loading="lazy" /></p>');
    expect(mdToHtml(ctx, '![](https://cdn.example.org/b.png)')).toBe('<p><img src="https://cdn.example.org/b.png" alt="" loading="lazy" /></p>');
  });

  it('renders an image and a bracketed link on the same line', () => {
    const html = mdToHtml(ctx, `![card](https://cdn.example.org/i.png) ${CITE_4475}`);
    expect(html).toContain('<img src="https://cdn.example.org/i.png" alt="card" loading="lazy" />');
    expect(html).toContain(`>${TEXT_4475}</a>`);
  });

  it('a product link inside Amazon link text: one anchor, the inner product url, the words of both', () => {
    const html = mdToHtml(ctx, '[Check the [Noctua NH-U12S](/product/B00C9EYVGY) on Amazon](https://www.amazon.com/dp/B00C9EYVGY?tag=someone-20)');
    expect(html).toBe('<p><a href="https://example.com/product/B00C9EYVGY">Check the Noctua NH-U12S on Amazon</a></p>');
  });

  it('an Amazon url for another product gives way to the inner product link', () => {
    const line = '[Check the [HyperX QuadCast 2](/product/B0D9MCK4R8) on Amazon.](https://www.amazon.com/dp/B06W2KLM3S)';
    expect(mdToHtml(ctx, line)).toBe('<p><a href="https://example.com/product/B0D9MCK4R8">Check the HyperX QuadCast 2 on Amazon.</a></p>');
    expect(mdToHtml(ctx, line)).not.toContain('B06W2KLM3S');
  });

  it('a citation whose text names a product keeps the citation url', () => {
    const url = 'https://gamersnexus.net/coolers/50-noctua-air-cooler-review-nh-u12s-redux-vs-stock-amd-coolers-more';
    expect(mdToHtml(ctx, `[Gamers Nexus: [Noctua NH-U12S](/product/B00C9EYVGY) Review](${url})`))
      .toBe(`<p><a href="${url}" ${EXT}>Gamers Nexus: Noctua NH-U12S Review</a></p>`);
  });

  it('gives a heading with a bracketed link a clean id', () => {
    const html = mdToHtml(ctx, '## Using [FLUX.2 [dev]](https://docs.example.org/dev)');
    expect(html).toContain('id="using-flux2-dev"');
    expect(html).toContain('>FLUX.2 [dev]</a>');
  });

  it('does not backtrack badly on unbalanced input', () => {
    const t0 = Date.now();
    mdToHtml(ctx, `${'[a '.repeat(4000)} ${'['.repeat(4000)}x ${'['}${'b'.repeat(20000)}`);
    expect(Date.now() - t0).toBeLessThan(2000);
  });
});

describe('the link patterns (ported)', () => {
  it('mdLinkRe groups text and url; unwrapNestedLinks keeps the words', () => {
    expect(`x ${CITE_4475} y`.replace(mdLinkRe(), '<$1|$2>')).toBe(`x <${TEXT_4475}|${URL_4475}> y`);
    expect(unwrapNestedLinks('a [b](c) d')).toBe('a b d');
    expect(MD_LINK_TEXT).toBe(String.raw`(?:[^\[\]]|\[[^\[\]]*\])`);
  });

  const CASES: Array<[string, string, string, string]> = [
    ['Check the [HyperX QuadCast 2](/product/B0D9MCK4R8) on Amazon.', 'https://www.amazon.com/dp/B06W2KLM3S',
      'Check the HyperX QuadCast 2 on Amazon.', '/product/B0D9MCK4R8'],
    ['See the [RTX 3060](https://example.com/product/B08WHJPBFX) deal', 'https://amzn.to/3abcdE',
      'See the RTX 3060 deal', 'https://example.com/product/B08WHJPBFX'],
    ['See the [RTX 3060](https://www.example.com/product/B08WHJPBFX) deal', 'https://a.co/d/xyz',
      'See the RTX 3060 deal', 'https://www.example.com/product/B08WHJPBFX'],
    ['Gamers Nexus: [Noctua NH-U12S](/product/B00C9EYVGY) Review', 'https://gamersnexus.net/coolers/x',
      'Gamers Nexus: Noctua NH-U12S Review', 'https://gamersnexus.net/coolers/x'],
    ['our [station guide](/guides/stations) on Amazon', 'https://www.amazon.com/dp/B08WHJPBFX',
      'our station guide on Amazon', 'https://www.amazon.com/dp/B08WHJPBFX'],
    ['the [RTX 3060](/product/B08WHJPBFX) review', 'https://docs.example.org/amazon.com/dp/B08WHJPBFX',
      'the RTX 3060 review', 'https://docs.example.org/amazon.com/dp/B08WHJPBFX'],
    ['a [product on another site](https://other.example.org/product/B08WHJPBFX)', 'https://www.amazon.com/dp/B08WHJPBFX',
      'a product on another site', 'https://www.amazon.com/dp/B08WHJPBFX'],
    [TEXT_4475, URL_4475, TEXT_4475, URL_4475],
  ];
  it.each(CASES)('resolveNestedLink %s -> %s', (t, u, wantText, wantUrl) => {
    expect(resolveNestedLink(ctx, t, u)).toEqual({ text: wantText, url: wantUrl });
  });

  it('productHrefRe follows the site\'s product route and domain', () => {
    expect(productHrefRe(ctx).test('/product/B08WHJPBFX')).toBe(true);
    expect(productHrefRe(ctx).test('https://www.example.com/product/B08WHJPBFX?x=1')).toBe(true);
    expect(productHrefRe(ctx).test('https://other.example.org/product/B08WHJPBFX')).toBe(false);
    const j = JSON.parse(fs.readFileSync(path.join(KIT, 'fixtures/site.example.json'), 'utf8')) as SiteJson;
    const other = parseSiteContext({ ...j, routes: { product: '/p/{asin}/specs' } }, { env: {} });
    expect(productHrefRe(other).test('/p/B08WHJPBFX/specs')).toBe(true);
    expect(productHrefRe(other).test('/product/B08WHJPBFX')).toBe(false);
  });
});

describe('GFM tables render as table.md-table', () => {
  const T = [
    '| Station | Usable Wh | Price |',
    '|:--------|----------:|:-----:|',
    '| **Alpha 1000** | 1,024 | [check](/product/B0ABCDEFGH) |',
    '| Beta \\| Max | 2,048 | `n/a` |',
  ].join('\n');

  it('wraps the table, marks header cells and keeps inline markdown in cells', () => {
    const html = mdToHtml(ctx, T);
    expect(html.startsWith('<div class="md-table-wrap"><table class="md-table"><thead><tr>')).toBe(true);
    expect(html).toContain('<th scope="col" class="md-align-left">Station</th>');
    expect(html).toContain('<th scope="col" class="md-align-right">Usable Wh</th>');
    expect(html).toContain('<th scope="col" class="md-align-center">Price</th>');
    expect(html).toContain('<td class="md-align-left"><strong>Alpha 1000</strong></td>');
    expect(html).toContain('<td class="md-align-center"><a href="https://example.com/product/B0ABCDEFGH">check</a></td>');
    expect(html).toContain('<td class="md-align-left">Beta | Max</td>');
    expect(html).toContain('<td class="md-align-center"><code>n/a</code></td>');
    expect(html.match(/<tr>/g)).toHaveLength(3);
    expect(html.endsWith('</tbody></table></div>')).toBe(true);
  });

  it('pads short rows and cuts long ones to the header width', () => {
    const html = mdToHtml(ctx, '| a | b | c |\n|---|---|---|\n| 1 |\n| 1 | 2 | 3 | 4 |');
    const rows = Array.from(html.matchAll(/<tr>(.*?)<\/tr>/g)).map((m) => (m[1] as string).match(/<t[hd]\b/g)?.length);
    expect(rows).toEqual([3, 3, 3]);
    expect(html).toContain('<tr><td>1</td><td></td><td></td></tr>');
    expect(html).not.toContain('>4<');
  });

  it('accepts tables without outer pipes, right after a paragraph', () => {
    const html = mdToHtml(ctx, 'Compare the two:\nLoad | Watts\n--- | ---\nFridge | 150\n\nAfter.');
    expect(html).toContain('<p>Compare the two:</p>');
    expect(html).toContain('<table class="md-table">');
    expect(html).toContain('<td>Fridge</td><td>150</td>');
    expect(html).toContain('<p>After.</p>');
  });

  it('ends at a blank line or a line with no pipe; a header-only table has an empty body', () => {
    const html = mdToHtml(ctx, '| a | b |\n|---|---|\n| 1 | 2 |\nnot a row\n\n| x |\n|---|');
    expect(html).toContain('<td>1</td><td>2</td></tr></tbody>');
    expect(html).toContain('<p>not a row</p>');
    expect(html).toContain('<thead><tr><th scope="col">x</th></tr></thead><tbody></tbody>');
  });

  it('is not fooled by a lone pipe line, a pipe-less delimiter or a rule', () => {
    expect(mdToHtml(ctx, 'a | b\nplain text')).toBe('<p>a | b plain text</p>');
    expect(mdToHtml(ctx, 'Heading text\n---')).toBe('<p>Heading text</p>\n<hr />');
    expect(mdToHtml(ctx, '| a |\n| b |')).not.toContain('<table');
  });

  it('needs as many delimiter cells as header cells (GFM), else the lines are a paragraph', () => {
    expect(mdToHtml(ctx, 'a | b | c\n--- | ---\n1 | 2 | 3')).toBe('<p>a | b | c --- | --- 1 | 2 | 3</p>');
    expect(mdToHtml(ctx, '| a | b |\n|---|---|---|\n| 1 | 2 |')).not.toContain('<table');
    expect(mdToHtml(ctx, '| a | b \\| c |\n|---|---|\n| 1 | 2 |')).toContain('<th scope="col">b | c</th>');
  });

  it('every table mdToHtml emits carries the md-table class', () => {
    const html = mdToHtml(ctx, `${T}\n\ntext\n\n${T}`);
    expect(html.match(/<table\b[^>]*>/g)).toEqual(['<table class="md-table">', '<table class="md-table">']);
  });
});

describe('blocks (ported behaviour)', () => {
  it('demotes # to h2 and gives every heading a stable, de-duplicated id', () => {
    const html = mdToHtml(ctx, '# Title\n\n## Pros\n\n### Pros\n\n#### Cons ##\n\n## \n');
    expect(html).toBe([
      '<h2 class="ssr-h2" id="title">Title</h2>',
      '<h2 class="ssr-h2" id="pros">Pros</h2>',
      '<h3 class="ssr-h3" id="pros-1">Pros</h3>',
      '<h4 class="ssr-h4" id="cons">Cons</h4>',
    ].join('\n'));
  });

  it('wraps an authored Quick Answer section up to the next h2, id on the wrapper only', () => {
    const html = mdToHtml(ctx, '## Quick Answer\n\nYes, about 20 hours.\n\n- point\n\n## Details\n\nMore.');
    expect(html).toBe([
      '<section class="ssr-quick-answer-authored" id="quick-answer">',
      '<h2 class="ssr-h2">Quick Answer</h2>',
      '<p>Yes, about 20 hours.</p>',
      '<ul class="ssr-list">',
      '<li>point</li>',
      '</ul>',
      '</section>',
      '<h2 class="ssr-h2" id="details">Details</h2>',
      '<p>More.</p>',
    ].join('\n'));
    expect(mdToHtml(ctx, '### Quick Answer\nIt runs to the end.')).toMatch(/<\/p>\n<\/section>$/);
  });

  it('renders lists, keeps an ordered list\'s start, and lets "1." end a paragraph', () => {
    expect(mdToHtml(ctx, '- a\n* b\n+ c')).toBe('<ul class="ssr-list">\n<li>a</li>\n<li>b</li>\n<li>c</li>\n</ul>');
    expect(mdToHtml(ctx, 'Steps:\n1. one\n2. two')).toBe('<p>Steps:</p>\n<ol class="ssr-list">\n<li>one</li>\n<li>two</li>\n</ol>');
    expect(mdToHtml(ctx, '3. three\n4. four')).toContain('<ol class="ssr-list" start="3">');
    expect(mdToHtml(ctx, '- a\n1. b')).toBe('<ul class="ssr-list">\n<li>a</li>\n</ul>\n<ol class="ssr-list">\n<li>b</li>\n</ol>');
  });

  it('keeps a wrapped list item\'s continuation lines in the item', () => {
    expect(mdToHtml(ctx, '- Shopping elsewhere? A [UK listing](/a) and a\n  [short link](/b) are not ours.\n- next\nlazy line'))
      .toBe('<ul class="ssr-list">\n<li>Shopping elsewhere? A <a href="https://example.com/a">UK listing</a> and a '
        + '<a href="https://example.com/b">short link</a> are not ours.</li>\n<li>next lazy line</li>\n</ul>');
    expect(mdToHtml(ctx, '1. one\n   wrapped\n## Next')).toBe('<ol class="ssr-list">\n<li>one wrapped</li>\n</ol>\n<h2 class="ssr-h2" id="next">Next</h2>');
  });

  it('renders blockquotes, rules and paragraphs', () => {
    expect(mdToHtml(ctx, '> one\n> two')).toBe('<blockquote class="ssr-quote">one two</blockquote>');
    expect(mdToHtml(ctx, 'a\n\n---\n\n***\n\n___')).toBe('<p>a</p>\n<hr />\n<hr />\n<hr />');
    expect(mdToHtml(ctx, 'line one\nline two\n\nnext')).toBe('<p>line one line two</p>\n<p>next</p>');
  });

  it('passes raw HTML blocks through, but never an autolink line', () => {
    const raw = '<figure class="x">\n<figcaption>Cap</figcaption>\n</figure>';
    expect(mdToHtml(ctx, `${raw}\n\nafter`)).toBe(`${raw}\n<p>after</p>`);
    expect(mdToHtml(ctx, '<https://docs.example.org/a_b>')).toBe(`<p><a href="https://docs.example.org/a_b" ${EXT}>https://docs.example.org/a_b</a></p>`);
    expect(mdToHtml(ctx, 'see <https://docs.example.org/_x_/y>')).toBe(`<p>see <a href="https://docs.example.org/_x_/y" ${EXT}>https://docs.example.org/_x_/y</a></p>`);
  });

  it('ends a paragraph at a block-level HTML line, never at an inline tag', () => {
    expect(mdToHtml(ctx, 'Intro line\n<div class="buystrip">x</div>')).toBe('<p>Intro line</p>\n<div class="buystrip">x</div>');
    expect(mdToHtml(ctx, 'Intro line\n<span>inline</span> text')).toBe('<p>Intro line <span>inline</span> text</p>');
  });

  it('escapes fenced code exactly, with a safe language badge and no inline script or style', () => {
    const html = mdToHtml(ctx, '```html <ignored>\n<a href="x">&amp;</a>\n*not em*\n```');
    expect(html).toBe('<div class="ssr-code-block"><span class="ssr-code-lang">html</span><pre class="ssr-code">'
      + '<code data-lang="html">&lt;a href=&quot;x&quot;&gt;&amp;amp;&lt;/a&gt;\n*not em*</code></pre></div>');
    expect(mdToHtml(ctx, '```"><script>\nx\n```')).toBe('<div class="ssr-code-block"><pre class="ssr-code"><code>x</code></pre></div>');
    expect(mdToHtml(ctx, '```js\nlet a = 1;\n```')).not.toMatch(/onclick|style=|<script/);
  });

  it('removes HTML comments outside code, keeps them inside code', () => {
    expect(mdToHtml(ctx, 'a <!-- agent: draft --> b\n\n<!--\nmulti\n-->\nc')).toBe('<p>a  b</p>\n<p>c</p>');
    expect(mdToHtml(ctx, '```\n<!-- keep -->\n```')).toContain('&lt;!-- keep --&gt;');
    expect(mdToHtml(ctx, 'use `<!-- x -->` here')).toBe('<p>use <code>&lt;!-- x --&gt;</code> here</p>');
    expect(mdToHtml(ctx, 'a <!--> b <!---> c <!-- d --!> e')).toBe('<p>a  b  c  e</p>');
  });

  it('shows a comment opener that never closes as text, so it cannot swallow the page after the body', () => {
    const html = mdToHtml(ctx, '## Quick Answer\n\nAbout 20 hours.\n\n<!-- sources: jackery manual p.12, \n\n## Details\n\nMore.');
    expect(html).toBe([
      '<section class="ssr-quick-answer-authored" id="quick-answer">',
      '<h2 class="ssr-h2">Quick Answer</h2>',
      '<p>About 20 hours.</p>',
      '<p>&lt;!-- sources: jackery manual p.12,</p>',
      '</section>',
      '<h2 class="ssr-h2" id="details">Details</h2>',
      '<p>More.</p>',
    ].join('\n'));
    expect(html).not.toContain('<!--');
    expect(mdToHtml(ctx, 'closed <!-- a --> then open <!-- b')).toBe('<p>closed  then open &lt;!-- b</p>');
    expect(mdToHtml(ctx, 'use `<!--` here')).toBe('<p>use <code>&lt;!--</code> here</p>');
    // A "-->" past a code fence must not pair with the opener and hide the code block between them.
    expect(mdToHtml(ctx, 'a <!-- note\n```\nx\n```\nb --> c')).toBe(
      '<p>a &lt;!-- note</p>\n<div class="ssr-code-block"><pre class="ssr-code"><code>x</code></pre></div>\n<p>b --> c</p>');
  });

  it('handles empty input, CRLF and stray NULs', () => {
    expect(mdToHtml(ctx, '')).toBe('');
    expect(mdToHtml(ctx, null)).toBe('');
    expect(mdToHtml(ctx, 'a\r\nb\r\n\r\nc')).toBe('<p>a b</p>\n<p>c</p>');
    expect(mdToHtml(ctx, 'x\u00000\u0000y')).toBe('<p>x0y</p>');
  });
});

describe('inline markdown', () => {
  it('bold, italic and underscore emphasis; intraword underscores survive', () => {
    expect(mdToHtml(ctx, '**b** *i* __B__ _I_ snake_case_name model_x1')).toBe('<p><strong>b</strong> <em>i</em> <strong>B</strong> <em>I</em> snake_case_name model_x1</p>');
    expect(underscoreEmphasis('nvidia_rtx_3060 and _this_')).toBe('nvidia_rtx_3060 and <em>this</em>');
  });

  it('code spans and urls are set aside before emphasis runs', () => {
    expect(mdToHtml(ctx, 'Hosts `*.media-amazon.com` and `*ssl-images-amazon.com` only.'))
      .toBe('<p>Hosts <code>*.media-amazon.com</code> and <code>*ssl-images-amazon.com</code> only.</p>');
    expect(mdToHtml(ctx, 'see [doc](https://docs.example.org/_a_/b*c*d) now'))
      .toBe(`<p>see <a href="https://docs.example.org/_a_/b*c*d" ${EXT}>doc</a> now</p>`);
    expect(mdToHtml(ctx, '`[x](y)` stays code')).toBe('<p><code>[x](y)</code> stays code</p>');
  });

  it('keeps emphasis and code inside link text, escaping the rest', () => {
    expect(mdToHtml(ctx, '[`qwen3:14b` **tag** & <b>x</b>](https://docs.example.org/q)'))
      .toBe(`<p><a href="https://docs.example.org/q" ${EXT}><code>qwen3:14b</code> <strong>tag</strong> &amp; &lt;b&gt;x&lt;/b&gt;</a></p>`);
  });

  it('honours backslash escapes', () => {
    expect(mdToHtml(ctx, '\\*not italic\\* costs \\$499 \\_x\\_ a\\\\b')).toBe('<p>*not italic* costs $499 _x_ a\\b</p>');
  });

  it('strips a markdown link title from the url', () => {
    expect(mdToHtml(ctx, '[a](https://docs.example.org/a "Title")')).toBe(`<p><a href="https://docs.example.org/a" ${EXT}>a</a></p>`);
  });
});

describe('links to this site and elsewhere', () => {
  it('makes site links absolute on the canonical origin, with no rel or target', () => {
    expect(mdToHtml(ctx, '[a](/guides/x) [b](guides/y) [c](https://www.example.com/kits/z?q=1#t)')).toBe(
      '<p><a href="https://example.com/guides/x">a</a> <a href="https://example.com/guides/y">b</a> '
      + '<a href="https://example.com/kits/z?q=1#t">c</a></p>');
  });

  it('keeps in-page fragments and mailto links; drops script and data urls but keeps the words', () => {
    expect(mdToHtml(ctx, '[picks](#top-picks) [mail](mailto:hi@example.com)'))
      .toBe('<p><a href="#top-picks">picks</a> <a href="mailto:hi@example.com">mail</a></p>');
    expect(mdToHtml(ctx, '[x](javascript:alert(1)) [y](data:text/html,hi)')).toBe('<p>x) y</p>');
  });

  it('external links open in a new tab with noopener noreferrer; protocol-relative becomes https', () => {
    expect(mdToHtml(ctx, '[a](//docs.example.org/a)')).toBe(`<p><a href="https://docs.example.org/a" ${EXT}>a</a></p>`);
  });

  it('rewriteLink can replace a url, drop a link (keeping its text) or leave it', () => {
    const rewriteLink = (url: string): string | null | undefined => {
      if (url.includes('/vs/')) return null;
      if (url.includes('/old/')) return '/new/path';
      return undefined;
    };
    expect(mdToHtml(ctx, '[A vs B](/vs/a/b) [moved](/old/x) [same](/guides/z)', { rewriteLink })).toBe(
      '<p>A vs B <a href="https://example.com/new/path">moved</a> <a href="https://example.com/guides/z">same</a></p>');
  });
});

describe('Amazon links follow the kit\'s link rules', () => {
  it('re-tags an authored Amazon link with the site tag, AFFILIATE_REL, a new tab and the placement', () => {
    const [a] = anchors(mdToHtml(ctx, '[Buy](https://www.amazon.com/dp/B0ABCDEFGH?tag=someone-20&th=1)'));
    expect(a).toEqual({
      href: 'https://www.amazon.com/dp/B0ABCDEFGH?th=1&tag=kitexample-20',
      rel: AFFILIATE_REL,
      target: '_blank',
      [PLACEMENT_ATTR]: DEFAULT_MD_PLACEMENT,
    });
  });

  it('uses the placement\'s segment tag, and an allowed visit segment tag otherwise', () => {
    const hero = anchors(mdToHtml(ctx, '[Buy](https://www.amazon.com/dp/B0ABCDEFGH)', { amazonPlacement: 'pdp-hero' }))[0];
    expect(hero?.href).toBe('https://www.amazon.com/dp/B0ABCDEFGH?tag=kitexample-pdp-20');
    expect(hero?.[PLACEMENT_ATTR]).toBe('pdp-hero');
    const seg = anchors(mdToHtml(ctx, '[Buy](https://www.amazon.com/dp/B0ABCDEFGH)', { segmentTag: 'kitexample-guides-20' }))[0];
    expect(seg?.href).toContain('tag=kitexample-guides-20');
    const foreign = anchors(mdToHtml(ctx, '[Buy](https://www.amazon.com/dp/B0ABCDEFGH)', { segmentTag: 'thief-20' }))[0];
    expect(foreign?.href).toContain('tag=kitexample-20');
    expect(() => mdToHtml(ctx, 'x', { amazonPlacement: 'Bad Placement' })).toThrow(/placement/);
  });

  it('keeps a tag that is already one of the site\'s own, and a K1 CTA\'s segment tag survives the enforcement pass', () => {
    const own = anchors(mdToHtml(ctx, '[Buy](https://www.amazon.com/dp/B0ABCDEFGH?tag=kitexample-guides-20)'))[0];
    expect(own?.href).toBe('https://www.amazon.com/dp/B0ABCDEFGH?tag=kitexample-guides-20');
    const twoTags = anchors(mdToHtml(ctx, '[Buy](https://www.amazon.com/dp/B0ABCDEFGH?tag=kitexample-guides-20&Tag=thief-20)'))[0];
    expect(twoTags?.href).toBe('https://www.amazon.com/dp/B0ABCDEFGH?tag=kitexample-20');
    const button = cta(ctx, { asin: 'B0ABCDEFGH', placement: 'guide-buystrip', segmentTag: 'kitexample-guides-20' });
    expect(anchors(enforceAffiliateLinks(ctx, button))[0]?.href).toContain('tag=kitexample-guides-20');
    expect(anchors(enforceAffiliateLinks(ctx, button))[0]?.[PLACEMENT_ATTR]).toBe('guide-buystrip');
  });

  it('drops links it cannot tag (other marketplaces, short links, image CDNs) but keeps their words', () => {
    const html = mdToHtml(ctx, '[uk](https://www.amazon.co.uk/dp/B0ABCDEFGH) [s](https://amzn.to/3x) [c](https://a.co/d/1) [img](https://m.media-amazon.com/images/I/x.jpg) [evil](https://amazon.com.evil.io/dp/B0ABCDEFGH)');
    expect(html).toBe('<p>uk s c img evil</p>');
  });

  it('normalises a plain-http or bare-domain Amazon link to https with the tag', () => {
    expect(anchors(mdToHtml(ctx, '[a](http://amazon.com/dp/B0ABCDEFGH)'))[0]?.href).toBe('https://amazon.com/dp/B0ABCDEFGH?tag=kitexample-20');
  });

  it('drops Amazon CDN images, markdown or raw HTML, and a link left with nothing in it', () => {
    expect(mdToHtml(ctx, '![x](https://m.media-amazon.com/images/I/a.jpg)')).toBe('');
    expect(mdToHtml(ctx, 'Look ![x](//images-na.ssl-images-amazon.com/images/I/a.jpg) here')).toBe('<p>Look  here</p>');
    expect(mdToHtml(ctx, '[![x](https://m.media-amazon.com/i.jpg)](https://www.amazon.com/dp/B0ABCDEFGH)')).toBe('');
    expect(mdToHtml(ctx, '<p><img src="https://m.media-amazon.com/i.jpg" alt=""> ok</p>')).toBe('<p> ok</p>');
    expect(mdToHtml(ctx, '<picture><source srcset="https://cdn.example.org/a.webp 1x, https://m.media-amazon.com/b.jpg 2x"><img src="/a.png" alt="a"></picture>'))
      .toBe('<picture><img src="/a.png" alt="a"></picture>');
  });

  it('re-tags raw-HTML Amazon anchors, keeps their class, data attributes and valid placement, drops handlers', () => {
    const html = mdToHtml(ctx, '<p><a class="amz-cta" data-x="1" href="https://www.amazon.com/dp/B0ABCDEFGH?tag=other-20&amp;psc=1" rel="nofollow noreferrer" onclick="steal()" data-cta-placement="sticky-mobile">Buy</a></p>');
    expect(html).toBe('<p><a class="amz-cta" data-x="1" href="https://www.amazon.com/dp/B0ABCDEFGH?psc=1&amp;tag=kitexample-sticky-20" '
      + `rel="${AFFILIATE_REL}" target="_blank" ${PLACEMENT_ATTR}="sticky-mobile">Buy</a></p>`);
  });

  it('unlinks raw-HTML anchors it cannot tag, closing the span where the anchor closed', () => {
    expect(mdToHtml(ctx, '<p><a href="https://amzn.to/abc">deal <b>now</b></a> and <a href="/guides/x">guide</a></p>'))
      .toBe('<p><span class="md-unlinked">deal <b>now</b></span> and <a href="/guides/x">guide</a></p>');
  });

  it('treats a link to the kit\'s /r/<ASIN> redirect as an affiliate link, in markdown and raw HTML', () => {
    const md = anchors(mdToHtml(ctx, 'See [Buy on Amazon](/r/B0ABCDEFGH) now.', { amazonPlacement: 'guide-body' }))[0];
    expect(md).toEqual({ href: 'https://example.com/r/B0ABCDEFGH', rel: AFFILIATE_REL, target: '_blank', [PLACEMENT_ATTR]: 'guide-body' });
    expect(mdToHtml(ctx, '<p><a class="buy" href="/r/B0ABCDEFGH/" onclick="x()">Buy</a></p>')).toBe(
      `<p><a class="buy" href="https://example.com/r/B0ABCDEFGH/" rel="${AFFILIATE_REL}" target="_blank" ${PLACEMENT_ATTR}="${DEFAULT_MD_PLACEMENT}">Buy</a></p>`);
    const raw = enforceAffiliateLinks(ctx, '<a href="https://www.example.com/r/B0ABCDEFGH?x=1" data-cta-placement="sticky-mobile">b</a>');
    expect(anchors(raw)[0]).toMatchObject({ href: 'https://example.com/r/B0ABCDEFGH?x=1', rel: AFFILIATE_REL, [PLACEMENT_ATTR]: 'sticky-mobile' });
    expect(enforceAffiliateLinks(ctx, raw)).toBe(raw);
    // Not the redirect route's ASIN rule: plain internal links.
    expect(mdToHtml(ctx, '[a](/r/b0abcdefgh) [b](/r/B0ABCDEFGH/more) [c](/r/short)')).toBe(
      '<p><a href="https://example.com/r/b0abcdefgh">a</a> <a href="https://example.com/r/B0ABCDEFGH/more">b</a> <a href="https://example.com/r/short">c</a></p>');
  });

  it('enforceAffiliateLinks is idempotent and leaves non-Amazon markup alone', () => {
    const src = '<a href="https://www.amazon.com/dp/B0ABCDEFGH">x</a> <a href="https://amzn.to/1">y</a> <a href="/z" rel="me">z</a> <img src="/i.png">';
    const once = enforceAffiliateLinks(ctx, src);
    expect(enforceAffiliateLinks(ctx, once)).toBe(once);
    expect(once).toContain('<a href="/z" rel="me">z</a> <img src="/i.png">');
    const md = mdToHtml(ctx, fs.readFileSync(path.join(__dirname, 'fixtures/guide.md'), 'utf8'));
    expect(enforceAffiliateLinks(ctx, md)).toBe(md);
  });
});

describe('a body cannot pose as a kit notice or leak past its end', () => {
  it('removes the kit\'s notice markers from raw HTML, keeping every other attribute as written', () => {
    expect(mdToHtml(ctx, 'Intro.\n\n<p data-amazon-notice="2k" class="x">See the footer</p>\n\n<div DATA-EARNINGS-DISCLOSURE title=\'t&amp;u\'>fake</div>\n\n<span data-amazon-notice=2k hidden></span>'))
      .toBe('<p>Intro.</p>\n<p class="x">See the footer</p>\n<div title=\'t&amp;u\'>fake</div>\n<span hidden></span>');
    expect(mdToHtml(ctx, '<p title="data-amazon-notice=2k">x</p>')).toBe('<p title="data-amazon-notice=2k">x</p>');
    expect(mdToHtml(ctx, '`data-amazon-notice="2k"`')).toBe('<p><code>data-amazon-notice=&quot;2k&quot;</code></p>');
  });

  it('seals raw HTML the body leaves open (unclosed script, formatting tag)', () => {
    expect(mdToHtml(ctx, '<div class="x"><script>track()\n\nAfter.')).toBe('<div class="x">&lt;script>track()\n<p>After.</p>');
    expect(mdToHtml(ctx, '<p><b hidden>note</p>\n\nAfter.')).toBe('<p><b hidden>note</p>\n<p>After.</p></b>');
  });
});

describe('a realistic guide body (fixtures/guide.md)', () => {
  const html = mdToHtml(ctx, fs.readFileSync(path.join(__dirname, 'fixtures/guide.md'), 'utf8'));
  const visible = text(html.replace(/<pre\b[\s\S]*?<\/pre>|<code>[\s\S]*?<\/code>/g, ''));

  it('leaves no markdown syntax in the visible text', () => {
    const LEAK = /\*\*[^*\s][^*]{0,80}\*\*|(?:^|\n)\s{0,3}#{2,6}\s+\S|\|\s*:?-{3,}:?\s*\||```|\[[^\]\n]{1,120}\]\((?:https?:\/\/|\/)[^)\s]*\)/;
    expect(visible).not.toMatch(LEAK);
  });

  it('renders every table as md-table, inside a scroll wrapper', () => {
    const tables = html.match(/<table\b[^>]*>/g) ?? [];
    expect(tables.length).toBeGreaterThanOrEqual(2);
    for (const t of tables) expect(t).toBe('<table class="md-table">');
    expect(html.match(/<div class="md-table-wrap">/g)?.length).toBe(tables.length);
  });

  it('every Amazon link carries an allowed tag, AFFILIATE_REL, target and a placement', () => {
    const amazon = anchors(html).filter((a) => /amazon\./.test(a.href ?? ''));
    expect(amazon.length).toBeGreaterThanOrEqual(2);
    for (const a of amazon) {
      const tag = new URL(a.href as string).searchParams.getAll('tag');
      expect(tag).toHaveLength(1);
      expect(ctx.amazon.allowedTags).toContain(tag[0]);
      expect(a.rel).toBe(AFFILIATE_REL);
      expect(a.target).toBe('_blank');
      expect(a[PLACEMENT_ATTR]).toMatch(/^[a-z0-9][a-z0-9-]{0,39}$/);
    }
  });

  it('holds no Amazon-hosted image or short link, and unique heading ids', () => {
    expect(html).not.toMatch(/media-amazon\.com|ssl-images-amazon\.com|amzn\.to/);
    const ids = Array.from(html.matchAll(/\sid="([^"]+)"/g)).map((m) => m[1]);
    expect(new Set(ids).size).toBe(ids.length);
    expect(html).not.toContain('<h1');
  });
});

describe('headingId (ported)', () => {
  it('strips inline markdown, hyphenates and de-duplicates', () => {
    const seen = new Map<string, number>();
    expect(headingId('Using `jq` with **[the API](https://x.y)**', seen)).toBe('using-jq-with-the-api');
    expect(headingId('Using jq with the API', seen)).toBe('using-jq-with-the-api-1');
    expect(headingId('???', seen)).toBe('');
    expect(headingId('x'.repeat(100), seen)).toHaveLength(80);
  });
});
