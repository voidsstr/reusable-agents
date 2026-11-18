/**
 * Entry point of the kit's browser bundle (scripts/build-client.mjs builds it
 * into client/dist/kit-client.js as one self-contained IIFE).
 *
 * Load it on every page (kitClientScriptTag(bundle), `defer`). The click
 * beacon installs immediately; the sticky-bar sizing waits for the DOM.
 */
import { installClickBeacon } from './clickBeacon';
import { installStickyBar } from './stickyBar';

installClickBeacon();

if (typeof document !== 'undefined') {
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => { installStickyBar(); }, { once: true });
  } else {
    installStickyBar();
  }
}
