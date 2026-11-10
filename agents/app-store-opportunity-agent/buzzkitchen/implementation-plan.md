# BuzzKitchen — Implementation Plan

## Full File Tree

```
buzzkitchen/
├── frontend/
│   ├── src/
│   │   ├── pages/
│   │   │   ├── Home.tsx
│   │   │   ├── RecipeDetail.tsx
│   │   │   ├── Search.tsx
│   │   │   ├── Pantry.tsx
│   │   │   ├── ShoppingList.tsx
│   │   │   ├── ShoppingListDetail.tsx
│   │   │   ├── MealPlan.tsx
│   │   │   ├── Feed.tsx
│   │   │   ├── Profile.tsx
│   │   │   └── Login.tsx
│   │   ├── components/
│   │   │   ├── RecipeCard.tsx
│   │   │   ├── VideoPlayer.tsx
│   │   │   ├── PantryMatchBanner.tsx
│   │   │   ├── ShoppingListItem.tsx
│   │   │   ├── CookLogCard.tsx
│   │   │   ├── NavBar.tsx
│   │   │   └── Layout.tsx
│   │   ├── api/
│   │   │   ├── recipes.ts
│   │   │   ├── pantry.ts
│   │   │   ├── shoppingLists.ts
│   │   │   ├── mealPlan.ts
│   │   │   ├── cookLogs.ts
│   │   │   ├── social.ts
│   │   │   └── auth.ts
│   │   ├── hooks/
│   │   │   ├── useRecipes.ts
│   │   │   ├── usePantry.ts
│   │   │   ├── useAuth.ts
│   │   │   └── useFeed.ts
│   │   ├── lib/
│   │   │   ├── axios.ts
│   │   │   ├── queryClient.ts
│   │   │   └── auth.ts
│   │   ├── App.tsx
│   │   └── main.tsx
│   ├── index.html
│   ├── vite.config.ts
│   ├── tailwind.config.js
│   ├── postcss.config.js
│   ├── tsconfig.json
│   └── package.json
├── mobile/
│   ├── app/
│   │   ├── (tabs)/
│   │   │   ├── index.tsx
│   │   │   ├── search.tsx
│   │   │   ├── plan.tsx
│   │   │   ├── feed.tsx
│   │   │   └── profile.tsx
│   │   ├── recipe/
│   │   │   └── [id].tsx
│   │   ├── cook/
│   │   │   └── [id].tsx
│   │   ├── pantry.tsx
│   │   ├── log/
│   │   │   └── new.tsx
│   │   └── _layout.tsx
│   ├── components/
│   │   ├── RecipeCard.tsx
│   │   ├── VideoPlayer.tsx
│   │   ├── IngredientRow.tsx
│   │   ├── StepCard.tsx
│   │   ├── PantryMatchBanner.tsx
│   │   ├── MatchScorePill.tsx
│   │   ├── CookLogCard.tsx
│   │   ├── StreakBadge.tsx
│   │   ├── MadeItSheet.tsx
│   │   ├── AisleSection.tsx
│   │   ├── PaywallScreen.tsx
│   │   └── ui/
│   │       ├── PrimaryButton.tsx
│   │       ├── SecondaryButton.tsx
│   │       └── LoadingSpinner.tsx
│   ├── lib/
│   │   ├── axios.ts
│   │   ├── auth.ts
│   │   ├── storage.ts
│   │   └── queryClient.ts
│   ├── app.json
│   ├── eas.json
│   ├── metro.config.js
│   ├── tsconfig.json
│   └── package.json
├── src/
│   ├── simple-server.ts
│   ├── middleware/
│   │   ├── auth.ts
│   │   ├── optionalAuth.ts
│   │   ├── rateLimiter.ts
│   │   ├── requestLogger.ts
│   │   └── errorHandler.ts
│   ├── lib/
│   │   ├── prisma.ts
│   │   ├── jwt.ts
│   │   ├── storage.ts
│   │   ├── imageUpload.ts
│   │   ├── pantryMatch.ts
│   │   ├── shoppingListMerge.ts
│   │   ├── streakUpdater.ts
│   │   └── lineShare.ts
│   ├── seed.ts
│   └── index.ts
├── prisma/
│   ├── schema.prisma
│   └── migrations/
│       └── .gitkeep
├── packages/
│   └── shared/
│       ├── src/
│       │   ├── types/
│       │   │   ├── recipe.ts
│       │   │   ├── user.ts
│       │   │   ├── pantry.ts
│       │   │   ├── shoppingList.ts
│       │   │   ├── cookLog.ts
│       │   │   └── api.ts
│       │   ├── api/
│       │   │   └── endpoints.ts
│       │   └── storage/
│       │       └── keys.ts
│       ├── tsconfig.json
│       └── package.json
├── agents/
│   ├── buzzkitchen-seo-opportunity-agent/
│   │   ├── agent.py
│   │   ├── manifest.json
│   │   ├── site.yaml
│   │   └── AGENT.md
│   └── buzzkitchen-catalog-audit-agent/
│       ├── agent.py
│       ├── manifest.json
│       └── AGENT.md
├── scripts/
│   ├── generate-shopping-list-text.ts
│   ├── update-streaks.ts
│   └── seed-featured-recipes.ts
├── tests/
│   ├── e2e/
│   │   ├── recipe-browse.spec.ts
│   │   ├── pantry-match.spec.ts
│   │   ├── shopping-list.spec.ts
│   │   └── cook-log.spec.ts
│   └── unit/
│       ├── pantryMatch.test.ts
│       ├── shoppingListMerge.test.ts
│       └── streakUpdater.test.ts
├── azure/
│   ├── deploy.sh
│   └── provision.sh
├── Dockerfile
├── Dockerfile.azure
├── docker-compose.yml
├── package.json
├── tsconfig.json
├── .gitignore
├── .env.example
├── README.md
└── CLAUDE.md
```

---

## M1 — Scaffold + Recipe Browsing (Days 1–14)

**Goal:** A working app from first `git clone` — backend serves recipes, web app browses them, mobile app browses and plays videos.

1. **Monorepo scaffold** — `package.json` with workspaces (frontend, mobile, packages/shared); root `tsconfig.json`; `.gitignore`; `.env.example` with DATABASE_URL, JWT_SECRET, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, AZURE_STORAGE_CONNECTION_STRING, PORT; `CLAUDE.md` with repo orientation notes.

2. **Docker files** — `Dockerfile` multi-stage build (node:18-alpine build stage → production stage; copies `src/` and `prisma/`; runs `prisma migrate deploy` then `node dist/index.js`); `Dockerfile.azure` same build with `NODE_ENV=production`; `docker-compose.yml` with db, backend, and frontend services as specified in tech-stack.md.

3. **Backend skeleton with health endpoint** — `src/index.ts` (Express app, mounts simple-server routes, starts on PORT); `src/simple-server.ts` (inline routes starting with `GET /api/health` returning `{ status: "ok", db: "ok", uptime_seconds, version }`); `src/middleware/requestLogger.ts` (pino-http); `src/middleware/errorHandler.ts` (catch-all returning `{ error, code }`); `src/lib/prisma.ts` (singleton PrismaClient).

4. **Prisma schema for core recipe tables** — `prisma/schema.prisma` with User, Recipe, Ingredient, RecipeIngredient, RecipeStep models exactly as specified in data-model.md. Run `npx prisma migrate dev --name init_recipe_tables`.

5. **Seed data (20 recipes)** — `src/seed.ts` creates 20 Japanese recipes: gyudon, karaage, yakisoba, tamago gohan, miso soup, gyoza, hamburg steak, oyakodon, nikujaga, curry rice, cold soba, zaru udon, tamagoyaki, onigiri, agedashi tofu, chahan, mapo tofu, teriyaki chicken, kakiage, ochazuke — each with a real YouTube video ID, 5–10 ingredients, 4–6 steps, cook time, and difficulty.

6. **Recipe API endpoints** — `simple-server.ts` adds: `GET /api/recipes` (full-text search over title using Prisma `contains` mode insensitive, filters for time_max and vegetarian, pagination 20/page); `GET /api/recipes/featured` (8 recipes with `is_featured = true`); `GET /api/recipes/:id` (with included ingredients and steps sorted by display_order/step_order).

7. **Shared types package** — `packages/shared/src/types/recipe.ts` defines RecipeSummary, RecipeDetail, IngredientLine, StepLine TypeScript interfaces; `packages/shared/src/types/user.ts`, `api.ts`, `pantry.ts`, `shoppingList.ts`, `cookLog.ts`; `packages/shared/package.json` with `"name": "@buzzkitchen/shared"` and `"main": "src/index.ts"`; frontend and mobile both add `"@buzzkitchen/shared": "workspace:*"`.

8. **Mobile Expo scaffold + Home screen + RecipeCard + VideoPlayer** — `mobile/` init via `npx create-expo-app@latest`; update `app.json` (name: BuzzKitchen, slug: buzzkitchen, scheme: buzzkitchen); install NativeWind 4.2, TanStack Query 5, Zustand 4, Axios, react-native-youtube-iframe 2.3; `mobile/app/(tabs)/index.tsx` renders 2-column FlatList of RecipeCard fetching `GET /api/recipes`; RecipeCard shows thumbnail, title, cook time badge, and difficulty chip.

9. **Mobile Recipe Detail screen** — `mobile/app/recipe/[id].tsx` fetches `GET /api/recipes/:id`; renders VideoPlayer (react-native-youtube-iframe with `webViewProps={{ allowsInlineMediaPlayback: true }}`); IngredientList with checkboxes; StepList with numbered StepCard components; TimestampBadge seeks YouTube player to `youtube_timestamp_sec` on tap.

10. **Mobile Search screen** — `mobile/app/(tabs)/search.tsx` with TextInput debounced 300ms calling `GET /api/recipes?q=`; results in FlatList of RecipeCard; filter bottom sheet with `time_max` slider and vegetarian toggle.

11. **Web scaffold + Home + Recipe Detail** — `frontend/` init via `npm create vite@latest -- --template react-ts`; install Tailwind 3.4, React Router 6.14, TanStack Query 5, Axios, @headlessui/react, @heroicons/react; `src/pages/Home.tsx` (2-col recipe grid); `src/pages/RecipeDetail.tsx` (YouTube iframe embed via react-youtube, ingredient list, numbered step list).

12. **CI workflow** — `.github/workflows/ci.yml` as specified in tech-stack.md: checkout → setup-node → `npm ci` → `npm run typecheck` → `npm run lint` → `npm test` with postgres service → docker build → conditional deploy on main.

13. **Azure provision and deploy scripts** — `azure/provision.sh` creates resource group `buzzkitchen-rg`, ACR `buzzkitchenacr`, PostgreSQL Flexible Server, Container Apps environment, backend Container App, frontend Container App, Key Vault with all secrets; `azure/deploy.sh` builds and pushes both images to ACR and updates Container App revisions.

14. **Smoke test and README polish** — `tests/e2e/recipe-browse.spec.ts` (Playwright: navigate to `/`, assert 8 recipe cards, click first, assert YouTube iframe present); verify README quick-start commands work against fresh docker-compose; push and confirm CI green.

---

## M2 — Pantry + Shopping List + Cooking Mode (Days 15–35)

**Goal:** The three differentiating features are live — pantry match, shopping list with LINE share, and hands-free cooking mode.

15. **Google OAuth + JWT backend** — `src/middleware/auth.ts` (verifyJWT: reads `Authorization: Bearer`, throws 401 on failure); `src/middleware/optionalAuth.ts` (same but calls next() on missing token); `simple-server.ts` adds `POST /api/auth/google` (verifies Google ID token via google-auth-library, upserts User, signs JWT with 7-day expiry); `POST /api/auth/logout` (JWT required, records revoked token in memory Set).

16. **Apple Sign In backend** — `simple-server.ts` adds `POST /api/auth/apple` (verifies Apple identity token via jwks-rsa against Apple's public key endpoint; upserts User by apple_sub; returns same JWT response shape); `GET /api/auth/me` (JWT required, returns current user row).

17. **Mobile auth screens and JWT storage** — `mobile/lib/auth.ts` (storeToken/getToken/clearToken via AsyncStorage; Axios interceptor adds Authorization Bearer header); `mobile/app/_layout.tsx` checks stored token on mount, navigates to tabs or shows Onboarding; Google OAuth via expo-auth-session (PKCE flow); Apple sign-in via expo-apple-authentication on iOS.

18. **Prisma schema for pantry and shopping tables** — add PantryItem, ShoppingList, ShoppingListItem, MealPlanSlot models to `prisma/schema.prisma` exactly as in data-model.md; run `npx prisma migrate dev --name add_pantry_shopping_mealplan`.

19. **Pantry CRUD endpoints** — `simple-server.ts` adds `GET /api/pantry`, `POST /api/pantry`, `PUT /api/pantry/:id`, `DELETE /api/pantry/:id`, `DELETE /api/pantry` (all JWT-required); `GET /api/ingredients?q=` (no auth, case-insensitive name search for autocomplete, max 20 results).

20. **Pantry-match algorithm endpoint** — `src/lib/pantryMatch.ts` implements: fetch user's pantry ingredient_id set; for each recipe count how many non-optional RecipeIngredient rows have ingredient_id in the pantry set; score = matching / total × 100; `simple-server.ts` adds `GET /api/recipes/pantry-match?threshold=70` (JWT required); returns recipes sorted by score DESC with match_score and missing_ingredients[] augmented. Unit test in `tests/unit/pantryMatch.test.ts`.

21. **Mobile Pantry screen with autocomplete** — `mobile/app/pantry.tsx` with TextInput debounced 400ms → `GET /api/ingredients?q=` → AutocompleteDropdown (absolute-positioned FlatList below input); tap suggestion → `POST /api/pantry`; pantry list as SwipeToDeleteRow components; `DELETE /api/pantry/:id` on swipe.

22. **PantryMatchBanner on Home** — `mobile/components/PantryMatchBanner.tsx` visible when pantry count > 0; fetches top result from `GET /api/recipes/pantry-match?threshold=60&limit=1`; renders recipe thumbnail, name, and MatchScorePill in secondary `#FF9500`; navigates to recipe detail on tap; rendered as FlatList header in `mobile/app/(tabs)/index.tsx`.

23. **Hands-free cooking mode screen** — `mobile/app/cook/[id].tsx` — full-screen layout; `useKeepAwake()` from expo-keep-awake; step text at display size 28sp, `#1C1410`, centered; Next Step button 80pt height, full width, primary `#E8452C` background, 22sp white label; Previous Step ghost button 40pt above; PiP VideoPlayer pinned bottom-right at 160×90pt with draggable gesture; progress indicator at top in caption size.

24. **Shopping list CRUD and recipe-merge endpoint** — `src/lib/shoppingListMerge.ts` implements merge: for each new ingredient find existing item in list by ingredient_id; if found convert compatible units and sum quantities; else add new row; aisle_category from Ingredient.aisle_category; `simple-server.ts` adds all Shopping List endpoints per api-spec.md. Unit test in `tests/unit/shoppingListMerge.test.ts`.

25. **Mobile shopping list with aisle groups and LINE share** — AisleSection component renders collapsible sections per aisle_category; ShoppingListItem renders checkbox row; `PUT /api/shopping-lists/:id/items/:itemId` on checkbox toggle; `mobile/lib/lineShare.ts` generates LINE URL scheme deep-link with plain-text formatted list grouped by category; Share button triggers `Linking.openURL(lineUrl)`.

26. **Web pantry and shopping pages** — `frontend/src/pages/Pantry.tsx` (ingredient autocomplete plus pantry list); `frontend/src/pages/ShoppingListDetail.tsx` (aisle-grouped list, check-off, LINE share CTA); both wired to TanStack Query mutations.

27. **Meal plan endpoints** — `simple-server.ts` adds `GET /api/meal-plans?week`, `PUT /api/meal-plans/:date`, `DELETE /api/meal-plans/:date`, `GET /api/meal-plans/shopping-list?week` (aggregates all recipe ingredients for the week and creates a new ShoppingList); all JWT-required.

28. **Mobile meal plan 7-day calendar** — `mobile/app/(tabs)/plan.tsx` — horizontal scroll of 7 day columns Monday through Sunday; each column has 3 slot rows (breakfast/lunch/dinner) rendered as small RecipeCard or AddSlotButton; tap AddSlotButton opens recipe picker sheet; bottom CTA calls `GET /api/meal-plans/shopping-list?week=` and navigates to shopping list.

29. **E2E tests for critical flows** — `tests/e2e/pantry-match.spec.ts` (add 3 pantry items, assert PantryMatchBanner shows ≥1 recipe with score ≥70); `tests/e2e/shopping-list.spec.ts` (open recipe, tap Add to Shopping List, navigate to list, assert ingredients grouped by aisle, tap LINE share, assert URL scheme triggered).

---

## M3 — Social + Retention + Monetisation (Days 36–56)

**Goal:** Cook logs, social feed, streak, push notifications, RevenueCat paywall, SEO agent, and App Store submission.

30. **Prisma schema for social tables** — add CookLog, CookLogPhoto, CookLogReaction, UserFollow, RecipeBookmark models; add cooking_streak, last_cooked_at, premium_until, push_token fields to User; run `npx prisma migrate dev --name add_social_streak_premium`.

31. **Cook log CRUD and photo upload** — `src/lib/imageUpload.ts` (multer memory storage + sharp resize to max 1200px WebP + upload to Azure Blob container `cook-log-photos` + return public URL); `simple-server.ts` adds `POST /api/cook-logs` (multipart, max 3 photos, resize pipeline); `GET /api/cook-logs` (user's own logs); `GET /api/recipes/:id/cook-logs` (cursor-paginated); `DELETE /api/cook-logs/:id` (owner only).

32. **Mobile Made it! screen with camera and star rating** — `mobile/app/log/new.tsx` — half-sheet modal; expo-image-picker for camera or library; StarRatingInput (5 tappable stars filling secondary `#FF9500`); TextInput max 140 chars with live character count; "投稿する" primary CTA posts multipart form to `POST /api/cook-logs`; on success plays confetti animation via react-native-confetti-cannon and navigates back; MadeItCTA changes label to "また作った！".

33. **Cook log wall on recipe detail** — `mobile/app/recipe/[id].tsx` adds CookLogWall section below steps; `GET /api/recipes/:id/cook-logs?limit=9`; 3-column photo grid of CookLogThumbnail (circular crop, star count overlay, avatar bottom-right); tap opens full CookLogCard modal with photo, rating, note, user, relative time, and reaction strip.

34. **Social feed endpoint (paginated cursor)** — `simple-server.ts` adds `GET /api/feed?cursor` (JWT required; queries CookLog joined with User, Recipe, CookLogPhoto, CookLogReaction counts WHERE user_id is in the followed set or is self; cursor is base64-encoded cooked_at DESC timestamp; page size 20).

35. **Mobile social feed tab** — `mobile/app/(tabs)/feed.tsx` — FlashList of CookLogCard components; infinite scroll via TanStack Query useInfiniteQuery; each card shows recipe thumbnail, user avatar, display name, relative time, optional photos, star rating, note text, and emoji reaction strip; pull-to-refresh.

36. **Follow/unfollow and emoji reaction endpoints** — `simple-server.ts` adds `POST /api/users/:id/follow`, `DELETE /api/users/:id/follow`; `POST /api/cook-logs/:id/react` (upserts CookLogReaction, returns updated counts); `DELETE /api/cook-logs/:id/react` (removes reaction); mobile uses optimistic update in TanStack Query mutation.

37. **Push notifications** — `mobile/lib/notifications.ts` registers Expo push token on login and sends to `PUT /api/users/me` as push_token; `scripts/update-streaks.ts` runs nightly: for users with last_cooked_at before yesterday reset cooking_streak to 0; for users with push_token and cooking_streak > 0 who have not cooked today send streak nudge via Expo Push API; separate `scripts/dinner-reminder.ts` sends 5 PM JST push to all users with push_token with deep link to top pantry-match recipe.

38. **Streak logic and StreakBadge component** — `src/lib/streakUpdater.ts` called inside `POST /api/cook-logs`: if last_cooked_at is today no-op; if yesterday increment cooking_streak + 1; else reset to 1; always set last_cooked_at = now(); `mobile/components/StreakBadge.tsx` shows flame emoji plus streak count, animates scale on mount if streak ≥ 3, hidden if streak = 0. Unit test in `tests/unit/streakUpdater.test.ts`.

39. **Recipe bookmarks backend and mobile** — `simple-server.ts` adds `GET /api/users/me/bookmarks`, `POST /api/recipes/:id/bookmark`, `DELETE /api/recipes/:id/bookmark` (upsert/delete RecipeBookmark); mobile: bookmark icon (outline → filled on tap) on RecipeCard and recipe detail header; saved recipes appear in a 保存済み section on `mobile/app/(tabs)/profile.tsx`.

40. **RevenueCat in-app purchase integration** — `mobile/lib/revenueCat.ts` init `Purchases.configure({ apiKey: RC_API_KEY })` on app mount; `mobile/components/PaywallScreen.tsx` shows feature list with checkmarks, monthly ¥600/month tile and annual ¥4,800/year tile as PurchasesPackage objects, and primary CTA calling `Purchases.purchasePackage()`; backend webhook `POST /api/webhooks/revenuecat` verifies X-RevenueCat-Signature header and sets `User.premium_until`.

41. **Paywall screen triggers** — Premium gate on: Made it! after 5 free log posts (prompt to upgrade); ad banner slot in recipe detail (free: 1 banner shown, premium: hidden); `is_premium = true` recipes show lock icon for free users, unlocked for premium.

42. **buzzkitchen-seo-opportunity-agent scaffold** — `agents/buzzkitchen-seo-opportunity-agent/manifest.json` (id: buzzkitchen-seo-opportunity-agent, category: seo, cron: `0 */2 * * *`); `agents/buzzkitchen-seo-opportunity-agent/site.yaml` (site_name: buzzkitchen, base_url: https://buzzkitchen.app, target_language: ja, implementer.allowed_paths: ["frontend/src/**", "src/**"]); `agents/buzzkitchen-seo-opportunity-agent/agent.py` (AgentBase subclass with 3 goals: increase recipe organic clicks, grow indexed pages count, improve recipe rich-result coverage); `AGENT.md` with runbook.

43. **GlitchTip error tracking** — `mobile/` installs @sentry/react-native; init in `app/_layout.tsx` with EXPO_PUBLIC_GLITCHTIP_DSN and tracesSampleRate 0.1; backend installs @sentry/node; init in `src/index.ts` before routes; `src/middleware/errorHandler.ts` calls Sentry.captureException before responding; GLITCHTIP_DSN added to `.env.example` and Key Vault secrets in `azure/provision.sh`.

44. **Lighthouse CI performance budget** — `lighthouserc.js` at repo root with assertions: LCP ≤ 2500ms, TBT ≤ 200ms, CLS ≤ 0.1, FCP ≤ 1800ms for URLs `/` and `/recipe/sample-id`; `.github/workflows/ci.yml` adds lhci autorun step after Vite build against preview deployment; CI fails if assertions not met.

45. **Onboarding A/B test** — `mobile/app/_layout.tsx` assigns variant A or B on first launch (stored in AsyncStorage); variant A headline: 今夜、何作る？; variant B headline: 冷蔵庫から作れるレシピを探そう; `mobile/lib/analytics.ts` logs onboarding_viewed and onboarding_cta_tapped events with variant to `POST /api/events` (no-auth, stored in Blob); outcome metric: guest-to-registered conversion within 7 days.

46. **buzzkitchen-catalog-audit-agent scaffold** — `agents/buzzkitchen-catalog-audit-agent/manifest.json` (id: buzzkitchen-catalog-audit-agent, category: research, cron: `0 6 * * *`); `agent.py` (AgentBase subclass; scans all Recipe rows for missing youtube_video_id, missing thumbnail_url, step count < 2, ingredient count < 3; queues content-completion recs to the implementer); `AGENT.md`.

47. **App Store and Play Store submission via EAS** — `mobile/eas.json` with build profiles (development, preview, production) and submit profiles (ios with appleId, ascAppId, appleTeamId; android with serviceAccountKeyPath); `mobile/app.json` updated with `ios.bundleIdentifier: com.buzzkitchen.app`, `android.package: com.buzzkitchen.app`, camera and notification usage description strings in Japanese; `scripts/submit.sh` runs `eas build --platform all --profile production` then `eas submit --platform all`.
