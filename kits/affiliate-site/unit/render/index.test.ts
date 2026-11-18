/**
 * The render barrel and the render sources' hygiene: an explicit public
 * surface, no site names in code (framework-first; unit/index.test.ts only
 * scans server/src's top level), no HTML comments shipped from string
 * literals, no environment reads or logging in pure render helpers, and no
 * dependency beyond the kit's own modules and Node's crypto.
 */
import * as fs from 'fs';
import * as path from 'path';
import * as ts from 'typescript';
import * as render from '../../server/src/render';

const RENDER = path.resolve(__dirname, '../../server/src/render');
const SOURCES = fs.readdirSync(RENDER).filter((f) => f.endsWith('.ts')).sort();

/** Exports a module keeps out of the barrel on purpose. */
const INTERNAL: Readonly<Record<string, readonly string[]>> = {
  'amazonHosts.ts': ['urlHostname'],
};

function codeOnly(src: string): string {
  return src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:\\])\/\/.*$/gm, '$1');
}

describe('render barrel', () => {
  it('exports the helpers each build-spec item names', () => {
    for (const name of [
      'mdToHtml', 'enforceAffiliateLinks', 'MD_TABLE_CLASS',
      'productLd', 'itemListLd', 'isSimilarToLd', 'breadcrumbListLd', 'faqPageLd', 'articleLd', 'organizationLd', 'webSiteLd',
      'jsonLdScripts', 'serializeJsonLd', 'stripAggregateRatings',
      'headTags', 'pageTitle', 'clampTitle', 'metaDescriptionFrom', 'canonicalUrl', 'resolveOgImage', 'isAllowedOgImage',
      'INDEXABLE_PAGE', 'noindexPage', 'robotsMetaContent',
      'buildSitemapSet', 'sitemapUrlsFrom', 'indexableUrls', 'sitemapIndexResponse', 'sitemapFileResponse', 'sitemapUnavailableResponse',
      'renderPage', 'siteHeaderHtml', 'siteFooterHtml', 'breadcrumbNavHtml', 'faqSectionHtml', 'amazonTextHtml', 'BASE_CSS', 'BASE_CSS_MIN',
      'isAmazonImageHost', 'isAmazonOwnedHost', 'sealHtmlFragment',
    ]) {
      expect((render as Record<string, unknown>)[name]).toBeDefined();
    }
  });

  it('re-exports every runtime export of every render module, except the listed internals', () => {
    const barrel = new Set(Object.keys(render));
    const missing: string[] = [];
    for (const f of SOURCES.filter((s) => s !== 'index.ts')) {
      // eslint-disable-next-line @typescript-eslint/no-var-requires
      const mod = require(path.join(RENDER, f)) as Record<string, unknown>;
      for (const name of Object.keys(mod)) {
        if (!barrel.has(name) && !(INTERNAL[f] ?? []).includes(name)) missing.push(`${f}#${name}`);
      }
    }
    expect(missing).toEqual([]);
  });

  it('does not collide with the kit barrel\'s existing names', () => {
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    const kit = require('../../server/src') as Record<string, unknown>;
    const clashes = Object.keys(render).filter((n) => n in kit && kit[n] !== (render as Record<string, unknown>)[n]);
    expect(clashes).toEqual([]);
  });
});

describe('render source hygiene', () => {
  it.each(SOURCES)('%s names no specific site in code', (f) => {
    const code = codeOnly(fs.readFileSync(path.join(RENDER, f), 'utf8'));
    expect(code).not.toMatch(/specpicks|aisleprompt|runtimepicks/i);
  });

  it('base.css names no specific site', () => {
    expect(fs.readFileSync(path.join(RENDER, 'base.css'), 'utf8')).not.toMatch(/specpicks|aisleprompt|runtimepicks/i);
  });

  it.each(SOURCES)('%s ships no HTML comment from a string or template literal', (f) => {
    const file = path.join(RENDER, f);
    const sf = ts.createSourceFile(file, fs.readFileSync(file, 'utf8'), ts.ScriptTarget.Latest, true);
    const kinds = new Set([ts.SyntaxKind.NoSubstitutionTemplateLiteral, ts.SyntaxKind.TemplateHead,
      ts.SyntaxKind.TemplateMiddle, ts.SyntaxKind.TemplateTail, ts.SyntaxKind.StringLiteral]);
    const hits: string[] = [];
    const visit = (n: ts.Node): void => {
      if (kinds.has(n.kind) && n.getText(sf).includes('<!--')) hits.push(n.getText(sf));
      ts.forEachChild(n, visit);
    };
    visit(sf);
    // The one deliberate comment: the reason inside a 503 sitemap body (XML, never a page).
    const allowed = f === 'sitemap.ts' ? hits.filter((h) => !h.includes('<!-- sitemap temporarily unavailable')) : hits;
    expect(allowed.map((h) => h.slice(0, 80))).toEqual([]);
  });

  it.each(SOURCES)('%s reads no environment, logs nothing and imports only the kit and node:crypto', (f) => {
    const src = fs.readFileSync(path.join(RENDER, f), 'utf8');
    const code = codeOnly(src);
    expect(code).not.toMatch(/process\.env|console\./);
    const imports = Array.from(src.matchAll(/^import\s[^;]*?from\s+'([^']+)'/gm)).map((m) => m[1] as string);
    for (const spec of imports) expect(spec).toMatch(/^(?:\.\/[A-Za-z]+|\.\.\/[A-Za-z]+|crypto)$/);
  });
});
