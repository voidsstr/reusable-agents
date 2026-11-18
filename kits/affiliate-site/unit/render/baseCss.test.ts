/**
 * base.css: the file and its embedded copy (BASE_CSS) stay identical, and a
 * static legibility lint holds the stylesheet to the theme contract: every
 * text colour is a token validated against the background it sits on
 * (THEME_CONTRAST_PAIRS), links inside surface-coloured boxes take the text
 * colour, the CTA text sits only on the CTA background, and the fixed code
 * colours pass WCAG AA. Every class the render helpers emit is styled.
 *
 * KIT_REGEN_BASE_CSS=1 npx jest unit/render/baseCss.test.ts rewrites
 * server/src/render/baseCss.ts from base.css.
 */
import * as fs from 'fs';
import * as path from 'path';
import { contrastRatio, loadSiteContext, THEME_CONTRAST_PAIRS } from '../../server/src/siteContext';
import { BASE_CSS, BASE_CSS_MIN } from '../../server/src/render/baseCss';

const RENDER = path.resolve(__dirname, '../../server/src/render');
const CSS_FILE = path.join(RENDER, 'base.css');
const TS_FILE = path.join(RENDER, 'baseCss.ts');
const css = fs.readFileSync(CSS_FILE, 'utf8');

/** The source of baseCss.ts for a given base.css. */
function baseCssModuleSource(text: string): string {
  if (/[`\\]|\$\{/.test(text)) throw new Error('base.css must not hold a backtick, a backslash or "${" (it is embedded in a template literal)');
  return [
    '/**',
    ' * base.css as a string, so renderPage() can inline it however the kit is',
    ' * compiled or bundled (tsc does not copy .css files). Generated from',
    ' * base.css: unit/render/baseCss.test.ts fails when the two differ, and',
    ' * KIT_REGEN_BASE_CSS=1 npx jest unit/render/baseCss.test.ts rewrites this file.',
    ' */',
    `export const BASE_CSS = \`${text}\`;`,
    '',
    '/** BASE_CSS without comments and blank lines: what a page actually inlines. */',
    'export const BASE_CSS_MIN: string = BASE_CSS',
    "  .replace(/\\/\\*[\\s\\S]*?\\*\\//g, '')",
    "  .split('\\n')",
    '  .map((l) => l.trim())',
    '  .filter(Boolean)',
    "  .join('\\n');",
    '',
  ].join('\n');
}

const REGEN = process.env.KIT_REGEN_BASE_CSS === '1';
if (REGEN) fs.writeFileSync(TS_FILE, baseCssModuleSource(css));

interface Rule {
  selectors: string[];
  decls: Map<string, string>;
  at: string | null;
}

/** A small CSS parser: rules with their declarations, @-blocks flattened with their prelude. */
function parseCss(text: string, at: string | null = null): Rule[] {
  const src = text.replace(/\/\*[\s\S]*?\*\//g, '');
  const out: Rule[] = [];
  let i = 0;
  while (i < src.length) {
    const open = src.indexOf('{', i);
    if (open < 0) break;
    const prelude = src.slice(i, open).trim();
    if (prelude.startsWith('@')) {
      let depth = 1;
      let j = open + 1;
      while (j < src.length && depth) { if (src[j] === '{') depth++; else if (src[j] === '}') depth--; j++; }
      out.push(...parseCss(src.slice(open + 1, j - 1), prelude));
      i = j;
      continue;
    }
    const close = src.indexOf('}', open);
    const decls = new Map<string, string>();
    for (const d of src.slice(open + 1, close).split(';')) {
      const k = d.indexOf(':');
      if (k > 0) decls.set(d.slice(0, k).trim().toLowerCase(), d.slice(k + 1).trim().toLowerCase());
    }
    out.push({ selectors: prelude.split(',').map((s) => s.trim()), decls, at });
    i = close + 1;
  }
  return out;
}

const rules = parseCss(BASE_CSS);
const v = (token: string): string => `var(--kit-${token})`;
const CODE_BG = '#0f172a';
const CODE_TEXT = ['#e2e8f0', '#cbd5e1'];
const TEXT_COLORS = new Set([v('text'), v('muted'), v('link'), v('cta-text'), 'inherit', 'currentcolor', ...CODE_TEXT]);
const BACKGROUNDS = new Set([v('bg'), v('surface'), v('cta-bg'), 'transparent', 'none', CODE_BG]);

function bgOf(r: Rule): string | undefined {
  return r.decls.get('background-color') ?? r.decls.get('background');
}

describe('base.css and BASE_CSS', () => {
  it('are identical, and baseCss.ts is exactly what the generator writes', () => {
    expect(fs.readFileSync(TS_FILE, 'utf8')).toBe(baseCssModuleSource(css));
    // A regenerating run imported the previous BASE_CSS before rewriting it; the next run checks it.
    if (!REGEN) expect(BASE_CSS).toBe(css);
  });

  it('BASE_CSS_MIN drops comments and blank lines and nothing else', () => {
    expect(BASE_CSS_MIN).not.toMatch(/\/\*|\*\/|\n\s*\n/);
    expect(parseCss(BASE_CSS_MIN)).toEqual(rules);
  });

  it('fetches nothing and imports nothing', () => {
    expect(css).not.toMatch(/@import|url\(|expression\(/i);
  });
});

describe('the legibility lint', () => {
  it('parses every rule', () => {
    expect(rules.length).toBeGreaterThan(60);
  });

  it('uses only validated text colours and known backgrounds', () => {
    const badColor = rules.filter((r) => r.decls.has('color') && !TEXT_COLORS.has(r.decls.get('color') as string));
    const badBg = rules.filter((r) => bgOf(r) !== undefined && !BACKGROUNDS.has(bgOf(r) as string));
    expect(badColor.map((r) => r.selectors.join(','))).toEqual([]);
    expect(badBg.map((r) => r.selectors.join(','))).toEqual([]);
  });

  it('never uses a decorative token (accent, border, focus) for text', () => {
    expect(BASE_CSS).not.toMatch(/(?:^|[;{])\s*color:\s*var\(--kit-(?:accent|border|focus|surface|bg|cta-bg)\)/m);
  });

  it('puts only text or muted on a surface background, and only the CTA text on the CTA background', () => {
    for (const r of rules) {
      const bg = bgOf(r);
      const color = r.decls.get('color');
      if (bg === v('surface') && color) expect([v('text'), v('muted')]).toContain(color);
      if (bg === v('cta-bg')) expect(color).toBe(v('cta-text'));
      if (color === v('cta-text')) for (const s of r.selectors) expect(s.startsWith('.ssr-amz-cta')).toBe(true);
      if (color === v('link')) expect([undefined, v('bg'), 'transparent']).toContain(bg);
      if (bg === CODE_BG && color) expect(CODE_TEXT).toContain(color);
    }
  });

  it('gives links inside every surface-coloured box the text colour', () => {
    const surfaces = rules.filter((r) => bgOf(r) === v('surface')).flatMap((r) => r.selectors).filter((s) => !s.endsWith('code'));
    expect(surfaces.length).toBeGreaterThan(0);
    for (const s of surfaces) {
      const linkRule = rules.find((r) => r.selectors.includes(`${s} a`));
      expect(linkRule?.decls.get('color')).toBe(v('text'));
    }
    // inline code inside a link drops its surface background
    expect(rules.find((r) => r.selectors.includes('a>code'))?.decls.get('background')).toBe('transparent');
  });

  it('keeps the fixed code colours at WCAG AA on the code background', () => {
    for (const c of CODE_TEXT) expect(contrastRatio(c, CODE_BG)).toBeGreaterThanOrEqual(4.5);
    expect(contrastRatio('#334155', CODE_BG)).toBeGreaterThan(1.2); // the border is visible, not text
  });

  it('uses !important only to honour reduced motion', () => {
    for (const r of rules) {
      for (const val of r.decls.values()) if (val.includes('!important')) expect(r.at).toMatch(/prefers-reduced-motion/);
    }
  });

  it('relies on the token pairs the site.json loader validates', () => {
    const pairs = THEME_CONTRAST_PAIRS.map(([fg, bg]) => `${fg}/${bg}`);
    for (const need of ['text/bg', 'text/surface', 'muted/bg', 'muted/surface', 'link/bg', 'ctaText/ctaBg', 'focus/bg']) expect(pairs).toContain(need);
    const ctx = loadSiteContext(path.resolve(__dirname, '../../fixtures/site.example.json'), { env: {} });
    for (const mode of ['light', 'dark'] as const) {
      expect(contrastRatio(ctx.theme[mode].link, ctx.theme[mode].bg)).toBeGreaterThanOrEqual(4.5);
    }
  });
});

describe('coverage', () => {
  it('styles every class the render helpers and the kit stamps emit', () => {
    const emitted = [
      // layout and blocks
      'kit-wrap', 'kit-skip-link', 'kit-header', 'kit-header-inner', 'kit-brand', 'kit-nav', 'kit-main', 'kit-footer',
      'kit-footer-cols', 'kit-footer-heading', 'kit-footer-links', 'kit-copyright', 'kit-breadcrumb', 'kit-bc-current',
      'kit-faq', 'kit-faq-item', 'kit-amazon-notice', 'kit-earnings-disclosure', 'kit-earnings-disclosure--top',
      // markdown
      'ssr-list', 'ssr-quote', 'ssr-quick-answer-authored', 'md-table-wrap', 'md-table', 'md-align-left', 'md-align-center',
      'md-align-right', 'ssr-code-block', 'ssr-code-lang', 'ssr-code',
      // links.ts and price.ts
      'ssr-amz-cta', 'ssr-detail-link', 'ssr-price', 'ssr-price-checked', 'kit-availability', 'kit-availability--unknown',
    ];
    const missing = emitted.filter((c) => !new RegExp(`\\.${c.replace(/-/g, '\\-')}(?![\\w-])`).test(BASE_CSS));
    expect(missing).toEqual([]);
  });
});
