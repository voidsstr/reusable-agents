/**
 * Sticky mobile buy bar: publishes the bar's rendered height (browser).
 *
 * The bar's markup and CSS are server-side (server/src/http/stickyBar.ts).
 * It is position:fixed at the bottom of a phone screen, so without help it
 * covers the end of the page: the last lines of the footer, which carries
 * the earnings disclosure and the Amazon content notice. SpecPicks measured
 * the bar and published its height as a CSS variable the page pads by
 * (frontend/src/ssr/SsrLandingContent.tsx); this is that, without React:
 *
 *   --kit-sticky-h on <html> = the tallest bar's height in px ('0px' while
 *   it is hidden: desktop widths, print, or a [data-kit-sticky-suppress]
 *   element in the page).
 *   class kit-has-sticky on <html> while the page has a bar.
 *
 * STICKY_BAR_CSS pads <body> by var(--kit-sticky-h, 96px) on html.kit-has-sticky
 * (any browser) and on body:has(.kit-sticky-bar) (browsers with :has(), even
 * before this script runs), so the page ends clear of the bar, exactly once
 * the height is measured.
 */
import { STICKY_ACTIVE_CLASS, STICKY_BAR_ATTR, STICKY_HEIGHT_VAR } from '../../server/src/http/clientContract';

const INSTALL_FLAG = '__kitStickyBar';

interface ResizeObserverLike {
  observe(el: Element): void;
  disconnect(): void;
}
type ResizeObserverCtor = new (cb: () => void) => ResizeObserverLike;

/** The tallest bar's rendered height, rounded to whole px. */
export function stickyBarHeight(bars: ReadonlyArray<Pick<Element, 'getBoundingClientRect'>>): number {
  let h = 0;
  for (const b of bars) h = Math.max(h, b.getBoundingClientRect().height || 0);
  return Math.round(h);
}

/**
 * Measure now and whenever a bar or the viewport resizes. Returns an
 * uninstall function (a no-op when there is no bar or it was already
 * installed).
 */
export function installStickyBar(win?: Window): () => void {
  const noop = (): void => undefined;
  const w = win ?? (typeof window !== 'undefined' ? window : undefined);
  if (!w || !w.document) return noop;
  const flags = w as unknown as Record<string, unknown>;
  if (flags[INSTALL_FLAG]) return noop;
  const bars = Array.from(w.document.querySelectorAll<HTMLElement>(`[${STICKY_BAR_ATTR}]`));
  if (!bars.length) return noop;
  flags[INSTALL_FLAG] = true;

  const root = w.document.documentElement;
  const rootStyle = root.style;
  const update = (): void => {
    rootStyle.setProperty(STICKY_HEIGHT_VAR, `${stickyBarHeight(bars)}px`);
  };
  update();
  root.classList.add(STICKY_ACTIVE_CLASS);
  const RO = (w as unknown as { ResizeObserver?: ResizeObserverCtor }).ResizeObserver;
  const ro = typeof RO === 'function' ? new RO(update) : null;
  for (const b of bars) ro?.observe(b);
  w.addEventListener('resize', update);
  w.addEventListener('orientationchange', update);
  return () => {
    ro?.disconnect();
    w.removeEventListener('resize', update);
    w.removeEventListener('orientationchange', update);
    rootStyle.removeProperty(STICKY_HEIGHT_VAR);
    root.classList.remove(STICKY_ACTIVE_CLASS);
    flags[INSTALL_FLAG] = false;
  };
}
