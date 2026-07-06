# BulkWise -- Implementation Plan

## File Tree

```
bulkwise/
  frontend/
    src/
      main.tsx
      App.tsx
      routes/
        Home.tsx
        List.tsx
        Deals.tsx
        Gas.tsx
        Settings.tsx
      components/
        SavingsCard.tsx
        DealCard.tsx
        ListItem.tsx
        GasCard.tsx
        MembershipCard.tsx
        PriceSparkline.tsx
        PaywallModal.tsx
      hooks/
        useList.ts
        useDeals.ts
        useGas.ts
        useSavings.ts
        useHousehold.ts
        useWS.ts
      context/
        AuthContext.tsx
        ThemeContext.tsx
        WarehouseContext.tsx
      lib/
        api.ts
        unitPrice.ts
    index.html
    vite.config.ts
    tailwind.config.js
    package.json
  mobile/
    app/
      _layout.tsx
      (tabs)/
        _layout.tsx
        index.tsx
        list.tsx
        deals.tsx
        gas.tsx
        settings.tsx
      onboarding.tsx
    components/
      ListItem.tsx
      DealCard.tsx
      SavingsCard.tsx
      GasCard.tsx
      MembershipCard.tsx
      PriceSparkline.tsx
      PaywallSheet.tsx
      OfflineBanner.tsx
    lib/
      api.ts
      storage.ts
      ws.ts
      queue.ts
      unitPrice.ts
    app.json
    eas.json
    package.json
  src/
    index.ts
    routes/
      auth.ts
      warehouses.ts
      lists.ts
      deals.ts
      products.ts
      savings.ts
      household.ts
      ai.ts
      health.ts
    middleware/
      auth.ts
      optionalAuth.ts
      rateLimit.ts
      errorHandler.ts
    lib/
      prisma.ts
      jwt.ts
      ws.ts
      validation.ts
      llm.ts
  prisma/
    schema.prisma
    migrations/
    seed.ts
  packages/
    shared/
      src/
        types/
          index.ts
        api/
          client.ts
        storage/
          index.ts
      package.json
      tsconfig.json
  agents/
    bulkwise-seo-opportunity-agent/
      AGENT.md
      agent.py
      manifest.json
      site.yaml
    bulkwise-catalog-audit-agent/
      AGENT.md
      agent.py
      manifest.json
  scripts/
    seed-warehouses.ts
    backfill-prices.ts
  tests/
    e2e/
      onboarding.spec.ts
      list.spec.ts
    api/
      auth.test.ts
      lists.test.ts
      deals.test.ts
  azure/
    deploy.sh
    containerapp.bicep
  Dockerfile
  Dockerfile.azure
  docker-compose.yml
  package.json
  tsconfig.json
  .gitignore
  .env.example
  README.md
  CLAUDE.md
```

## Milestones

### M1 (Week 1-2): Onboarding + Core Shell

1. Init monorepo scaffold: root `package.json` (workspaces), `tsconfig.json`, `.gitignore`, `.env.example`.
2. Add `packages/shared` with base TypeScript types (`User`, `Warehouse`, `ShoppingList`, `Deal`, `Product`, `SavingsLog`, `Household`) and API client stub.
3. Scaffold backend `src/index.ts` single-file Express server with `/api/health` and pino logging.
4. Add Prisma 5, `prisma/schema.prisma` with all 14 models, run initial local migration.
5. Add `prisma/seed.ts` with sample warehouse data; wire `scripts/seed-warehouses.ts`.
6. Wire `docker-compose.yml` (services: `db` postgres:15, `backend`, `frontend`) for local dev.
7. Scaffold Vite 5.2 + React 18.2 + TypeScript 5.4 frontend with Tailwind 3.4 and design tokens.
8. Add `AuthContext`, `ThemeContext`, `WarehouseContext`; wire light/dark theme with CSS vars from palette.
9. Scaffold Expo 54 mobile app with `app.json` (`bundleIdentifier: com.bulkwise.app`) and `eas.json` (development/preview/production profiles).
10. Add React Navigation 6 bottom tab shell with 5 tabs (Home, List, Deals, Gas, Settings).
11. Build onboarding step 1: household name input + member-count chips (1-5), progress dots, "Continue" CTA.
12. Build onboarding step 2: location permission prompt + nearest-warehouse list sorted by distance, tap-to-select.
13. Build onboarding step 3: 3 live deal cards for the chosen warehouse + "Start Saving" CTA, no email gate.
14. Build Home tab shell: `SavingsCard` (amber), coupon horizontal rail, `GasCard` compact, stock-alerts feed, lists preview, FAB.
15. Build `MembershipCard` with barcode display reachable from tab bar in <= 2 taps.
16. Build `DealCard` component (image, title, sale/original price, savings badge, expiry).
17. Build shared `ListItem` component (checkbox, thumbnail, name, quantity, unit-price caption, coupon chip).
18. Set up GitHub Actions CI workflow: lint (ESLint), typecheck (`tsc --noEmit`), test (Vitest scaffold), build check on each push.

### M2 (Week 3-5): Core Features + Auth

19. Implement `POST /api/auth/google`: passport-google-oauth20 strategy, upsert User, issue JWT + refresh token.
20. Add `GET /api/auth/me` and `POST /api/auth/refresh`; write `src/lib/jwt.ts` (sign/verify helpers).
21. Add `src/middleware/auth.ts` (require valid JWT) and `src/middleware/optionalAuth.ts` (attach userId if present).
22. Implement `@react-oauth/google` web sign-in flow and mobile Google auth via Expo `expo-auth-session`.
23. Implement lists CRUD: `GET/POST /api/lists`, `GET/POST/PATCH/DELETE /api/lists/:id/items`, `DELETE /api/lists/:id`.
24. Build Shopping List screen: filter tabs (All/To Buy/In Cart/Coupons), scrollable list with `ListItem`, FAB add.
25. Add unit-price calculation in `packages/shared/src/lib/unitPrice.ts`; display $/oz, $/ct, $/lb in list items.
26. Implement AsyncStorage offline cache in `mobile/lib/storage.ts`: lists hydrate from cache on mount, network is enhancement.
27. Implement mutation queue in `mobile/lib/queue.ts`: writes enqueue locally, flush on reconnect via `@react-native-community/netinfo`; show `OfflineBanner` when offline.
28. Stand up WebSocket server in `src/lib/ws.ts` using `ws` 8.13, attached to the same `http.Server` as Express.
29. Implement list subscription in WS hub: clients send `subscribe_list`; server broadcasts `list_item_added/updated/deleted` to all subscribers.
30. Wire `mobile/lib/ws.ts` and `frontend/src/hooks/useWS.ts` for real-time list sync; update local cache on incoming events.
31. Implement `POST /api/household`, `POST /api/household/join`, `GET /api/household`; generate `inviteCode` as `BULK-XXXX`.
32. Build Household section in Settings: member list, invite code display + share button, leave/manage.
33. Implement `GET /api/warehouses` with optional lat/lng distance sort using Postgres `earth_distance` or JS haversine.
34. Implement `GET /api/warehouses/:id/gas` and `POST /api/warehouses/:id/gas/confirm`; compute `savingsVsNearby` from recent `GasConfirmation` average vs stored nearby-station average.
35. Build Gas tab: compact `GasCard` with live price, savings badge, "confirmed X min ago", confirm button; subscribe to `gas_price_updated` WS events.
36. Implement `GET /api/products/search` and `GET /api/products/barcode/:upc`.
37. Add `expo-barcode-scanner` UPC scan in mobile: scan -> lookup product -> pre-fill add-to-list bottom sheet.
38. Implement `GET /api/products/:id/stock` and `POST /api/products/:id/stock`; show community stock status with timestamp on product detail.
39. Implement `GET /api/savings` and `POST /api/savings/log`; build Membership ROI Dashboard on Home tab savings card with breakeven progress bar.
40. Implement `GET /api/deals` (paginated + filterable) and `GET /api/deals/:id`; implement `POST /api/deals/:id/confirm`.
41. Build Deals/Coupon Book screen: 2-col grid, sticky filter bar (category chips, Saved, Expiring soon), deal detail bottom sheet with confirm button.
42. Add `expo-notifications` push: register token on auth, send push on coupon drops, expiring-in-3-day coupons, and stock-restock alerts.

### M3 (Week 6-8): Monetization + Polish

43. Implement `POST /api/ai/optimize-list`: parse meal-plan text with gpt-4o-mini (structured output), map suggestions to products, return savings estimates; enforce 10 req/min/user via `express-rate-limit`.
44. Build AI optimizer UI: meal-plan text input, loading skeleton, suggestions list with quantity/savings, "Add all to list" CTA.
45. Integrate RevenueCat SDK (`react-native-purchases`) in mobile + RevenueCat REST API in web; configure `bulkwise_pro_monthly` ($2.99) and `bulkwise_pro_annual` ($19.99) products.
46. Build `PaywallSheet` (mobile) / `PaywallModal` (web): Pro feature comparison, price options, 14-day free trial CTA via RevenueCat.
47. Add premium gating: 2nd list creation -> paywall; AI optimizer tap -> paywall; household >1 member -> paywall; >1 gas station -> paywall; history beyond current month -> paywall.
48. Implement price-history endpoint (`GET /api/products/:id/prices`); build `PriceSparkline` with Recharts (web) and RN SVG chart (mobile).
49. Implement `StockAlert` create/delete endpoints; wire restock alert delivery via `expo-notifications` when a new `StockReport` with `status: in_stock` arrives for a watched product.
50. Add full dark-mode pass: CSS var overrides on web (Tailwind `dark:` variants), NativeWind `dark:` variants on mobile, test against palette dark tokens.
51. Run accessibility audit: verify 44pt hit targets on all interactive elements, WCAG AA contrast ratios, `accessibilityLabel` on all controls, `accessibilityViewIsModal` on all sheets.
52. Add Playwright e2e tests in `tests/e2e/`: `onboarding.spec.ts` (3-step flow, no email wall), `list.spec.ts` (add item, check off, offline behavior).
53. Add API integration tests in `tests/api/`: `auth.test.ts`, `lists.test.ts`, `deals.test.ts` against ephemeral Postgres via Docker service in CI.
54. Scaffold `agents/bulkwise-seo-opportunity-agent/` (AGENT.md runbook, `agent.py` subclassing AgentBase, `manifest.json`, `site.yaml` with implementer scope).
55. Scaffold `agents/bulkwise-catalog-audit-agent/` (AGENT.md, `agent.py`, `manifest.json`).
56. Write `azure/deploy.sh`: build images, push to ACR, update Container App revisions for backend + frontend.
57. Write `azure/containerapp.bicep`: defines backend + frontend Container Apps (1-3 replicas, 0.5 vCPU / 1Gi), Postgres Flexible Server, Key Vault secrets.
58. Add CI build-push job in GitHub Actions on push to `main`: build `Dockerfile.azure`, push to `nscappsacr`, call `azure/deploy.sh` for Container App update.
59. Prepare app store assets: 1024x1024 icon, 6.7" + 6.1" screenshots (5 each), App Store Connect listing copy, Play Store listing copy.
60. Configure EAS `production` profiles + submit to App Store (TestFlight first) and Google Play (internal track) via `eas submit`.

## Monetization Details

### Free Tier

- 1 shopping list, maximum 30 items.
- Current-month coupon book only.
- Gas price for 1 (home) warehouse only.
- Current-month savings view; breakeven progress visible.

### BulkWise Pro -- $2.99/month or $19.99/year

- Unlimited shopping lists.
- AI List Optimizer (meal plan to Costco list).
- Price history sparklines (12-month lookback).
- Household sharing up to 5 members with real-time sync.
- Unlimited stock alerts.
- Gas price comparison for up to 3 nearby warehouses.
- 12-month savings history.
- Annual savings PDF export.

### Conversion Strategy

- **Contextual paywalls:** trigger on the 2nd list creation attempt and on the first AI optimizer tap -- the moments of clearest intent.
- **14-day free trial** via RevenueCat: lowers the upgrade barrier; trial converts at higher rates than hard paywall.
- **Household invite exposure:** free members who receive a household invite experience real-time list sync and savings before upgrading; the invite becomes a Pro demo.
- **Target:** 5% paid conversion by day 90 (200 paying users of 8,000 WAM).
