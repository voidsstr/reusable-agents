import type { Cta } from './pageAudit';

/**
 * Pure helpers of the all-pages engine (no Playwright import), so tests/unit
 * can exercise them.
 */

/** express-style route pattern ('/product/:asin') -> a regex over a path. */
export function routeRegex(route: string): RegExp {
  if (route === '*') return /^$/;
  const re = route
    .split(/(:[A-Za-z_][A-Za-z0-9_]*)/)
    .map((part) => (part.startsWith(':') ? '[^/]+' : part.replace(/[.+?^${}()|[\]\\*]/g, '\\$&')))
    .join('');
  return new RegExp(`^${re}$`);
}

/** rel, target and tag of one CTA (kit-compliance enforces the full licence rules; this keeps the per-family view). */
export function ctaProblems(c: Pick<Cta, 'href' | 'kind' | 'rel' | 'target'>, allowedTags: readonly string[]): string[] {
  const out: string[] = [];
  const rel = new Set(c.rel.split(/\s+/).filter(Boolean));
  const need = c.kind === 'click' ? ['sponsored', 'nofollow'] : ['sponsored', 'nofollow', 'noopener'];
  const missing = need.filter((r) => !rel.has(r));
  if (missing.length) out.push(`rel="${c.rel}" lacks ${missing.join(', ')}`);
  if (c.kind === 'amazon') {
    let u: URL | null = null;
    try { u = new URL(c.href); } catch { out.push('unparseable href'); }
    if (u) {
      const tag = u.searchParams.get('tag') || u.searchParams.get('AssociateTag') || '';
      if (!allowedTags.includes(tag)) out.push(`affiliate tag "${tag}" is not one of the site's`);
    }
    if (c.target !== '_blank') out.push(`target="${c.target}"`);
  }
  return out.map((m) => `${c.kind} CTA ${c.href.slice(0, 80)}: ${m}`);
}
