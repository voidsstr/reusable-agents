# BulkWise -- UX Research

## Costco Official App -- Public Review Complaint Themes

Synthesized from public app-store reviews of the official Costco app (4.86 stars, ~1.2M reviews). High rating, but the trip-time experience leaks the following recurring complaints:

1. **Navigation maze** -- core trip tasks (list, coupons, gas, card) are buried under e-commerce menus; members hunt across tabs to do simple things.
2. **Surface-level search** -- search returns shoppable .com SKUs, not warehouse items; typos and category intent are poorly handled.
3. **Buried coupon book** -- the digital coupon book is hard to find, slow to load, and not searchable; members miss savings they qualify for.
4. **Poor gas UI** -- gas pricing is hard to locate, often stale, and gives no comparison context versus nearby stations.
5. **No offline list** -- the list is useless in low-signal warehouse interiors; the app stalls when connectivity drops.
6. **No household sharing** -- partners cannot co-edit a list; people resort to texting items back and forth.
7. **Slow membership card access** -- the digital card takes too many taps to reach at the register and self-checkout, holding up the line.

## Adjacent App Teardowns

### Grocery Pal (3.61 stars)

- **What to copy:** the circular/coupon *alert* concept -- proactively surfacing relevant savings tied to items a user cares about.
- **What's broken:** dated ~2015 UI; scraped pricing data lags 24-48 hours so deals are often wrong by the time the user is in-store; no community layer to validate or freshen data.

### Shop: All Your Favorite Brands (4.82 stars)

- **What to copy:** the timeline-style order tracking UI (clear vertical status stages) and the concise, friendly push-notification copy format.
- **What's broken:** no warehouse-club coverage at all; the discovery surface feels like a wall of ads rather than a tool.

### Walmart App (4.82 stars)

- **What to copy:** the home-screen *savings counter* (a running, motivating total) and the visible price-match history.
- **What's broken:** feature bloat (too many unrelated surfaces); the in-store map is fragile and frequently wrong; Scan & Go introduces friction instead of removing it.

## Design Principles (derived from the research)

1. **One thing per screen** -- each screen has a single primary job; avoid the navigation maze by refusing to stack unrelated tasks.
2. **Membership card in <= 2 taps** -- the card is reachable from anywhere in two taps or fewer; nothing slows the register line.
3. **Offline-first list** -- the shopping list must be fully usable with zero connectivity; sync is a background concern, never a blocker.
4. **Community timestamps > scraped data** -- prefer fresh, human-confirmed data with visible timestamps over stale scrapes; always show "confirmed X minutes ago."
5. **No modal hell** -- prefer bottom sheets and inline detail over stacked modals; the user should never feel trapped behind dialogs.
