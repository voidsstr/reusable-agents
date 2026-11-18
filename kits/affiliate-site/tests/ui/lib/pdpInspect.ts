/**
 * What an unbuyable product page offers a reader, taken in the browser and
 * judged in Node (unbuyable-pdp.spec.ts). Ported from the regexes of
 * SpecPicks tests/ui/unbuyable-pdp.spec.ts, on the DOM instead of the HTML
 * text so markup order and attribute order do not matter.
 */

export interface PdpCta {
  href: string;
  rel: string;
  target: string;
  placement: string;
  /** A dated price stamp sits in the same block as the CTA (up to 4 ancestors up). */
  stampNearby: boolean;
  /** The block's price stamp text ('' when none). */
  stampText: string;
  isSearch: boolean;
}

export interface PdpInspection {
  /** The page states the listing cannot be bought (the kit's unavailable stamp, or the configured text). */
  unbuyable: boolean;
  /** Visible Amazon CTAs inside <main>. */
  ctas: PdpCta[];
}

/** Runs in the page; self-contained. */
export function inspectPdp(arg: { unavailableRe: string }): PdpInspection {
  const re = new RegExp(arg.unavailableRe, 'i');
  const main = document.querySelector('main') ?? document.body;
  const visible = (el: Element): boolean => {
    const e = el as Element & { checkVisibility?: (o: object) => boolean };
    return typeof e.checkVisibility === 'function' ? e.checkVisibility({ visibilityProperty: true }) : true;
  };
  const unbuyable = !!main.querySelector('[data-price-stamp="availability"] .kit-availability--unavailable')
    || re.test((main as HTMLElement).innerText || main.textContent || '');
  const ctas: PdpCta[] = [];
  for (const a of Array.from(main.querySelectorAll<HTMLAnchorElement>('a[href]'))) {
    let u: URL;
    try { u = new URL(a.getAttribute('href') || '', location.href); } catch { continue; }
    if (!/(^|\.)amazon\.com$/i.test(u.hostname) || !visible(a)) continue;
    let block: Element | null = a.parentElement;
    let stamp: Element | null = null;
    for (let i = 0; i < 4 && block && block !== main; i++, block = block.parentElement) {
      stamp = block.querySelector('[data-price-stamp="price"]');
      if (stamp) break;
    }
    ctas.push({
      href: u.toString(),
      rel: (a.getAttribute('rel') || '').toLowerCase(),
      target: (a.getAttribute('target') || '').toLowerCase(),
      placement: a.closest('[data-cta-placement]')?.getAttribute('data-cta-placement') || '',
      stampNearby: !!stamp,
      stampText: (stamp?.textContent || '').replace(/\s+/g, ' ').trim(),
      isSearch: u.pathname === '/s' || u.pathname.startsWith('/s/'),
    });
  }
  return { unbuyable, ctas };
}

/** A CTA that pays: rel sponsored nofollow noopener, target _blank, one of the site's tags. */
export function ctaIsCompliant(c: PdpCta, allowedTags: readonly string[]): boolean {
  const rel = new Set(c.rel.split(/\s+/));
  let tag = '';
  try { tag = new URL(c.href).searchParams.get('tag') || ''; } catch { return false; }
  return ['sponsored', 'nofollow', 'noopener'].every((r) => rel.has(r)) && c.target === '_blank' && allowedTags.includes(tag);
}

/** The time in a stamp's text, e.g. "2026-11-17 14:11 UTC"; '' when none. */
export const STAMP_TIME_RE = /\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC/;

/** Why an unbuyable page is a dead end ([] when it offers something buyable). */
export function unbuyableProblems(page: string, inspection: PdpInspection, allowedTags: readonly string[], altPlacements: readonly string[]): string[] {
  const out: string[] = [];
  const compliant = inspection.ctas.filter((c) => ctaIsCompliant(c, allowedTags));
  if (!inspection.unbuyable) {
    if (!compliant.length) out.push(`${page}: in stock but has no tagged, compliant Amazon CTA`);
    return out;
  }
  const alts = inspection.ctas.filter((c) => altPlacements.includes(c.placement));
  if (alts.length) {
    for (const a of alts) {
      if (!a.stampNearby || !STAMP_TIME_RE.test(a.stampText)) out.push(`${page}: alternative (${a.placement}) has no dated price beside its CTA`);
      if (!ctaIsCompliant(a, allowedTags)) out.push(`${page}: alternative (${a.placement}) CTA is not tagged and compliant: ${a.href.slice(0, 100)}`);
    }
    return out;
  }
  if (!compliant.some((c) => c.isSearch)) {
    out.push(`${page}: unavailable with no alternative and no tagged Amazon search (a dead end for the reader)`);
  }
  return out;
}
