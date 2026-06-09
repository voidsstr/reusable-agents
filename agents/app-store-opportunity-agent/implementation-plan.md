# Tanki — Implementation Plan

## Repository Structure

```
tanki/
├── frontend/
│   ├── src/
│   │   ├── pages/
│   │   │   ├── HomePage.tsx
│   │   │   ├── LoginPage.tsx
│   │   │   ├── OnboardingPage.tsx
│   │   │   ├── DeckListPage.tsx
│   │   │   ├── DeckDetailPage.tsx
│   │   │   ├── StudySessionPage.tsx
│   │   │   ├── SessionCompletePage.tsx
│   │   │   ├── CommunityPage.tsx
│   │   │   ├── UpgradePage.tsx
│   │   │   ├── SettingsPage.tsx
│   │   │   └── NotFoundPage.tsx
│   │   ├── components/
│   │   │   ├── layout/
│   │   │   │   ├── AppShell.tsx
│   │   │   │   ├── Sidebar.tsx
│   │   │   │   ├── TopBar.tsx
│   │   │   │   └── BottomNav.tsx
│   │   │   ├── decks/
│   │   │   │   ├── DeckCard.tsx
│   │   │   │   ├── DeckCardSkeleton.tsx
│   │   │   │   ├── DeckGrid.tsx
│   │   │   │   ├── CreateDeckModal.tsx
│   │   │   │   └── DeckOptionsMenu.tsx
│   │   │   ├── cards/
│   │   │   │   ├── CardEditor.tsx
│   │   │   │   ├── CardEditorRow.tsx
│   │   │   │   ├── FlashCard.tsx
│   │   │   │   ├── FlashCardFlip.tsx
│   │   │   │   └── GeneratedCardsPreview.tsx
│   │   │   ├── study/
│   │   │   │   ├── StudyRatingButtons.tsx
│   │   │   │   ├── ProgressBar.tsx
│   │   │   │   ├── ExamCountdownBanner.tsx
│   │   │   │   └── SessionSummary.tsx
│   │   │   ├── ai/
│   │   │   │   ├── FileUploadZone.tsx
│   │   │   │   ├── GenerationStatusModal.tsx
│   │   │   │   └── AiCallsRemainingBadge.tsx
│   │   │   ├── community/
│   │   │   │   ├── CommunityDeckCard.tsx
│   │   │   │   ├── ExamTagChips.tsx
│   │   │   │   └── CommunitySearchBar.tsx
│   │   │   ├── auth/
│   │   │   │   ├── GoogleSignInButton.tsx
│   │   │   │   └── AuthGuard.tsx
│   │   │   └── ui/
│   │   │       ├── Button.tsx
│   │   │       ├── Badge.tsx
│   │   │       ├── Modal.tsx
│   │   │       ├── Toast.tsx
│   │   │       ├── Spinner.tsx
│   │   │       ├── EmptyState.tsx
│   │   │       └── XpAnimation.tsx
│   │   ├── api/
│   │   │   ├── client.ts
│   │   │   ├── auth.ts
│   │   │   ├── decks.ts
│   │   │   ├── cards.ts
│   │   │   ├── study.ts
│   │   │   ├── ai.ts
│   │   │   ├── community.ts
│   │   │   └── users.ts
│   │   ├── hooks/
│   │   │   ├── useAuth.ts
│   │   │   ├── useDecks.ts
│   │   │   ├── useCards.ts
│   │   │   ├── useStudySession.ts
│   │   │   ├── useAiGeneration.ts
│   │   │   ├── useCommunity.ts
│   │   │   ├── useSubscription.ts
│   │   │   ├── useStreaks.ts
│   │   │   └── useWebSocket.ts
│   │   ├── lib/
│   │   │   ├── queryClient.ts
│   │   │   ├── wsClient.ts
│   │   │   ├── auth.ts
│   │   │   ├── formatters.ts
│   │   │   └── constants.ts
│   │   ├── App.tsx
│   │   └── main.tsx
│   ├── index.html
│   ├── vite.config.ts
│   ├── tailwind.config.js
│   ├── tsconfig.json
│   └── package.json
├── mobile/
│   ├── app/
│   │   ├── (tabs)/
│   │   │   ├── _layout.tsx
│   │   │   ├── index.tsx
│   │   │   ├── community.tsx
│   │   │   ├── camera.tsx
│   │   │   └── settings.tsx
│   │   ├── deck/
│   │   │   ├── [id].tsx
│   │   │   └── new.tsx
│   │   ├── study/
│   │   │   ├── [deckId].tsx
│   │   │   └── complete.tsx
│   │   ├── auth/
│   │   │   ├── login.tsx
│   │   │   └── onboarding.tsx
│   │   ├── ai/
│   │   │   ├── camera.tsx
│   │   │   └── preview.tsx
│   │   ├── community/
│   │   │   └── [id].tsx
│   │   ├── upgrade.tsx
│   │   ├── _layout.tsx
│   │   └── +not-found.tsx
│   ├── components/
│   │   ├── decks/
│   │   │   ├── DeckCard.tsx
│   │   │   ├── DeckGrid.tsx
│   │   │   └── CreateDeckSheet.tsx
│   │   ├── cards/
│   │   │   ├── CardEditorSheet.tsx
│   │   │   ├── FlashCard.tsx
│   │   │   └── GeneratedCardsPreview.tsx
│   │   ├── study/
│   │   │   ├── StudyRatingButtons.tsx
│   │   │   ├── ProgressBar.tsx
│   │   │   ├── ExamCountdownBanner.tsx
│   │   │   └── SessionSummary.tsx
│   │   ├── ai/
│   │   │   ├── CaptureOverlay.tsx
│   │   │   └── GenerationProgress.tsx
│   │   ├── community/
│   │   │   ├── CommunityDeckCard.tsx
│   │   │   └── ExamTagChips.tsx
│   │   ├── auth/
│   │   │   ├── GoogleSignInButton.tsx
│   │   │   └── AppleSignInButton.tsx
│   │   └── ui/
│   │       ├── Button.tsx
│   │       ├── Badge.tsx
│   │       ├── BottomSheet.tsx
│   │       ├── Spinner.tsx
│   │       ├── EmptyState.tsx
│   │       ├── XpAnimation.tsx
│   │       └── ThemedView.tsx
│   ├── lib/
│   │   ├── api.ts
│   │   ├── auth.ts
│   │   ├── storage.ts
│   │   ├── wsClient.ts
│   │   ├── notifications.ts
│   │   └── constants.ts
│   ├── hooks/
│   │   ├── useAuth.ts
│   │   ├── useDecks.ts
│   │   ├── useStudySession.ts
│   │   ├── useAiGeneration.ts
│   │   ├── useStreaks.ts
│   │   └── useSubscription.ts
│   ├── assets/
│   │   ├── icon.png
│   │   ├── splash.png
│   │   └── adaptive-icon.png
│   ├── app.json
│   ├── eas.json
│   ├── metro.config.js
│   ├── tsconfig.json
│   └── package.json
├── src/
│   ├── simple-server.ts
│   ├── index.ts
│   ├── seed.ts
│   ├── middleware/
│   │   ├── auth.ts
│   │   ├── requireAuth.ts
│   │   ├── optionalAuth.ts
│   │   ├── rateLimiter.ts
│   │   ├── featureGate.ts
│   │   └── errorHandler.ts
│   ├── lib/
│   │   ├── azure-openai.ts
│   │   ├── azure-speech.ts
│   │   ├── azure-blob.ts
│   │   ├── fsrs.ts
│   │   ├── pdf-extract.ts
│   │   ├── ai-prompt.ts
│   │   ├── jwt.ts
│   │   ├── mailer.ts
│   │   ├── cron.ts
│   │   └── websocket.ts
│   └── routes/
│       ├── auth.ts
│       ├── users.ts
│       ├── decks.ts
│       ├── cards.ts
│       ├── study.ts
│       ├── ai.ts
│       ├── audio.ts
│       ├── community.ts
│       ├── subscriptions.ts
│       └── health.ts
├── prisma/
│   ├── schema.prisma
│   └── migrations/
│       ├── 0001_users_streaks/
│       │   └── migration.sql
│       ├── 0002_decks_cards/
│       │   └── migration.sql
│       ├── 0003_ai_jobs/
│       │   └── migration.sql
│       ├── 0004_study_sessions/
│       │   └── migration.sql
│       ├── 0005_community_decks/
│       │   └── migration.sql
│       ├── 0006_notifications/
│       │   └── migration.sql
│       └── 0007_subscriptions/
│           └── migration.sql
├── packages/
│   └── shared/
│       ├── src/
│       │   ├── types/
│       │   │   ├── user.ts
│       │   │   ├── deck.ts
│       │   │   ├── card.ts
│       │   │   ├── study.ts
│       │   │   ├── community.ts
│       │   │   ├── ai.ts
│       │   │   ├── subscription.ts
│       │   │   └── index.ts
│       │   ├── api/
│       │   │   ├── auth.ts
│       │   │   ├── decks.ts
│       │   │   ├── cards.ts
│       │   │   ├── study.ts
│       │   │   ├── ai.ts
│       │   │   ├── community.ts
│       │   │   ├── users.ts
│       │   │   └── index.ts
│       │   └── storage/
│       │       └── tokenStorage.ts
│       ├── tsconfig.json
│       └── package.json
├── agents/
│   ├── tanki-seo-opportunity-agent/
│   │   ├── manifest.json
│   │   └── site.yaml
│   ├── tanki-catalog-audit-agent/
│   │   ├── manifest.json
│   │   └── site.yaml
│   └── register-with-framework.sh
├── scripts/
│   ├── seed-community-decks.ts
│   └── backfill-tts-audio.ts
├── tests/
│   └── e2e/
│       ├── onboarding.spec.ts
│       ├── deck-crud.spec.ts
│       ├── study-session.spec.ts
│       ├── ai-generation.spec.ts
│       └── community.spec.ts
├── azure/
│   ├── deploy.sh
│   └── provision.sh
├── .github/
│   └── workflows/
│       ├── deploy.yml
│       └── test.yml
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

## Milestones

### M0: Scaffolding (Day 1) — "skeleton runs"

Every commit leaves the repo in a passing state: `npm run typecheck` green, `docker-compose up` boots the backend to `/health → 200`.

**Commit 1 — Init monorepo root**

Files created:
- `package.json` — workspaces: `["frontend", "mobile", "src", "packages/*"]`; root scripts: `typecheck`, `lint`, `test`, `dev`
- `tsconfig.json` — base strict config referenced by all packages
- `.gitignore` — node_modules, dist, .env, *.tsbuildinfo, .expo, prisma/migrations/dev, azure credentials
- `.env.example` — every env var the app reads, grouped by service, with safe placeholder values:

```
# Database
DATABASE_URL=postgresql://tanki:tanki@localhost:5432/tanki

# Auth
JWT_SECRET=change_me_in_production
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
APPLE_TEAM_ID=
APPLE_KEY_ID=
APPLE_PRIVATE_KEY=

# Azure OpenAI
AZURE_OPENAI_ENDPOINT=
AZURE_OPENAI_API_KEY=
AZURE_OPENAI_DEPLOYMENT_GPT4O=gpt-4o
AZURE_OPENAI_API_VERSION=2024-02-01

# Azure Cognitive Services (TTS)
AZURE_SPEECH_KEY=
AZURE_SPEECH_REGION=japaneast

# Azure Blob Storage
AZURE_STORAGE_CONNECTION_STRING=
AZURE_STORAGE_CONTAINER_UPLOADS=tanki-uploads
AZURE_STORAGE_CONTAINER_AUDIO=tanki-audio

# RevenueCat
REVENUECAT_API_KEY=
REVENUECAT_WEBHOOK_SECRET=

# Ollama fallback
OLLAMA_BASE_URL=http://localhost:11434

# App
PORT=3000
NODE_ENV=development
FRONTEND_URL=http://localhost:5173
```

**Commit 2 — Scaffold frontend**

Files created:
- `frontend/package.json` — react@18, react-dom, react-router-dom@6, @tanstack/react-query@5, @headlessui/react, axios, tailwindcss, @vitejs/plugin-react, typescript, vite
- `frontend/vite.config.ts` — proxy `/api` to `localhost:3000`, path alias `@/` → `src/`
- `frontend/tailwind.config.js` — `content: ["./src/**/*.{ts,tsx}"]`, extend colors: `primary` (indigo), `accent` (amber for Japanese study warmth)
- `frontend/tsconfig.json` — extends root, `baseUrl: "src"`, `paths: { "@/*": ["*"] }`
- `frontend/index.html` — charset utf-8, viewport, `<div id="root">`, link to `/src/main.tsx`
- `frontend/src/main.tsx` — `ReactDOM.createRoot` + `QueryClientProvider` + `<App />`
- `frontend/src/App.tsx` — `<BrowserRouter>` with `<Routes>`: `/` → `HomePage`, `/login` → `LoginPage`, `*` → `NotFoundPage`; `<Toaster>` overlay
- `frontend/src/pages/HomePage.tsx` — placeholder h1 "Tanki — AI Study Cards"
- `frontend/src/pages/LoginPage.tsx` — placeholder
- `frontend/src/pages/NotFoundPage.tsx` — placeholder

**Commit 3 — Scaffold mobile**

Files created:
- `mobile/package.json` — expo@54, react-native, expo-router@4, nativewind@4, react-native-reanimated, react-native-gesture-handler, @react-native-async-storage/async-storage, expo-camera, expo-av, expo-notifications, expo-apple-authentication, expo-purchases (RevenueCat)
- `mobile/app.json` — slug `tanki`, bundleIdentifier `com.tanki.app`, scheme `tanki`
- `mobile/eas.json` — build profiles: `development` (internal distribution), `preview` (TestFlight), `production`
- `mobile/metro.config.js` — NativeWind preset
- `mobile/tsconfig.json` — extends Expo's strict base, path alias `@/` → `./`
- `mobile/app/_layout.tsx` — `<Stack>` root with gesture handler + reanimated setup
- `mobile/app/(tabs)/_layout.tsx` — `<Tabs>` with 5 tabs: Home (house icon), Camera (camera icon), Community (globe icon), Settings (gear icon)
- `mobile/app/(tabs)/index.tsx` — placeholder deck list screen
- `mobile/app/+not-found.tsx` — 404 screen

**Commit 4 — Scaffold backend**

Files created:
- `src/package.json` — express@4, cors, helmet, morgan, dotenv, jsonwebtoken, passport, passport-google-oauth20, @prisma/client, multer, ws, pdf-parse, typescript, ts-node-dev, @types/*
- `src/index.ts` — loads .env, calls `startServer()`
- `src/simple-server.ts` — single Express app: cors + helmet + morgan + express.json(); mounts all route files; starts HTTP server + WebSocket server on same port; graceful shutdown on SIGTERM
- `src/routes/health.ts` — `GET /health` returns `{ status: "ok", ts: new Date().toISOString() }`
- `src/middleware/errorHandler.ts` — catches Error, returns `{ error: message }` with appropriate status

**Commit 5 — PostgreSQL + Prisma init**

Files created:
- `docker-compose.yml`:

```yaml
services:
  db:
    image: postgres:15-alpine
    environment:
      POSTGRES_USER: tanki
      POSTGRES_PASSWORD: tanki
      POSTGRES_DB: tanki
    ports:
      - "5432:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data
  api:
    build: .
    ports:
      - "3000:3000"
    environment:
      DATABASE_URL: postgresql://tanki:tanki@db:5432/tanki
    depends_on:
      - db
    volumes:
      - .:/app
      - /app/node_modules
volumes:
  pgdata:
```

- `prisma/schema.prisma` — datasource postgres + generator client; empty model block (models added per milestone)
- `src/lib/prisma.ts` — singleton PrismaClient with `log: ["query"]` in dev

**Commit 6 — packages/shared foundation**

Files created:
- `packages/shared/package.json` — name `@tanki/shared`, typescript, tsup for build
- `packages/shared/tsconfig.json` — strict, `declaration: true`
- `packages/shared/src/types/user.ts` — `User`, `UserStats`, `SubscriptionTier` enum
- `packages/shared/src/types/deck.ts` — `Deck`, `CreateDeckInput`, `UpdateDeckInput`
- `packages/shared/src/types/card.ts` — `Card`, `CreateCardInput`, `UpdateCardInput`
- `packages/shared/src/types/index.ts` — re-exports all types
- `packages/shared/src/api/index.ts` — `createApiClient(baseUrl, getToken)` returns typed fetch wrappers
- `packages/shared/src/storage/tokenStorage.ts` — abstract interface + web impl (localStorage) + native impl (AsyncStorage)

**Commit 7 — GitHub Actions CI**

Files created:
- `.github/workflows/test.yml`:

```yaml
name: CI
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with: { node-version: "18" }
      - run: npm ci
      - run: npm run typecheck
      - run: npm run lint
```

**Commit 8 — Dockerfiles**

Files created:
- `Dockerfile` — multi-stage: `node:18-alpine` builder installs deps + compiles `src/` with tsc; runner copies dist + node_modules, `CMD ["node", "dist/index.js"]`
- `Dockerfile.azure` — same as Dockerfile but sets `NODE_ENV=production` and strips dev deps; used by `azure/deploy.sh`

---

### M1: Auth + User Profile (Days 2–3) — "first login"

**Commit 1 — Prisma schema: users + streaks**

`prisma/schema.prisma` additions:

```prisma
model User {
  id                    String    @id @default(cuid())
  email                 String    @unique
  displayName           String
  avatarUrl             String?
  googleId              String?   @unique
  appleId               String?   @unique
  subscriptionTier      String    @default("free")
  subscriptionExpiresAt DateTime?
  aiCallsThisMonth      Int       @default(0)
  aiCallsResetAt        DateTime  @default(now())
  examDate              DateTime?
  dailyGoalCards        Int       @default(20)
  ttsVoice              String    @default("ja-JP-NanamiNeural")
  ttsSpeed              Float     @default(1.0)
  pushToken             String?
  createdAt             DateTime  @default(now())
  updatedAt             DateTime  @updatedAt
  streaks               Streak?
  decks                 Deck[]
  studySessions         StudySession[]
}

model Streak {
  id             String   @id @default(cuid())
  userId         String   @unique
  currentStreak  Int      @default(0)
  longestStreak  Int      @default(0)
  lastStudiedAt  DateTime?
  freezesLeft    Int      @default(0)
  xp             Int      @default(0)
  level          Int      @default(1)
  user           User     @relation(fields: [userId], references: [id], onDelete: Cascade)
}
```

Run migration: `npx prisma migrate dev --name users_streaks`

**Commit 2 — Backend auth routes**

Files created/modified:
- `src/lib/jwt.ts` — `signToken(userId)` → 30-day JWT; `verifyToken(token)` → `{ userId }`; uses `JWT_SECRET` env
- `src/routes/auth.ts`:
  - `GET /auth/google` — passport.authenticate redirect
  - `GET /auth/google/callback` — passport callback → upsert user → sign JWT → redirect to `FRONTEND_URL/?token=<jwt>`
  - `GET /auth/me` — requireAuth → return `req.user`
  - `POST /auth/apple` — decode Apple identity token → upsert user → sign JWT → return `{ token, user }`
- `src/middleware/requireAuth.ts` — extracts Bearer token, calls `verifyToken`, attaches `req.user`; returns 401 on failure
- `src/middleware/optionalAuth.ts` — same but calls `next()` on missing/invalid token (attaches null user)
- `src/routes/users.ts`:
  - `GET /users/me` — requireAuth → Prisma `findUnique` with streak relation
  - `PATCH /users/me` — requireAuth → update displayName/examDate/dailyGoalCards/ttsVoice/ttsSpeed/pushToken
  - `GET /users/me/stats` — requireAuth → return `{ totalDecks, totalCards, totalReviews, currentStreak, xp, level, aiCallsThisMonth, aiCallsRemaining }`

**Commit 3 — Frontend Google OAuth**

Files created/modified:
- `frontend/src/lib/auth.ts` — `setToken(t)` (localStorage), `getToken()`, `clearToken()`, `isLoggedIn()`
- `frontend/src/api/auth.ts` — `fetchMe()` calls `GET /auth/me` with Bearer header
- `frontend/src/hooks/useAuth.ts` — React Query `useQuery("me", fetchMe)`; reads token from localStorage on mount; exposes `{ user, isLoading, logout }`
- `frontend/src/pages/LoginPage.tsx` — "Sign in with Google" button → `window.location.href = "/api/auth/google"`; reads `?token=` param on mount, stores it, redirects to `/decks`
- `frontend/src/components/auth/GoogleSignInButton.tsx` — styled button with Google logo SVG
- `frontend/src/components/auth/AuthGuard.tsx` — wraps protected routes; redirects to `/login` if not authenticated
- `frontend/src/pages/OnboardingPage.tsx` — 3-slide carousel: "Capture any text", "AI builds your cards", "Study smarter with your exam date"; "Get Started" → `/login`; "Continue as Guest" → `/decks`
- `frontend/src/App.tsx` updated — `/` shows `OnboardingPage` (if no token) or redirects to `/decks`

**Commit 4 — Mobile Google + Apple sign-in**

Files created/modified:
- `mobile/lib/auth.ts` — `setToken`/`getToken`/`clearToken` via `AsyncStorage`; `isLoggedIn()`
- `mobile/lib/api.ts` — axios instance with `baseURL` from constants; request interceptor attaches Bearer token
- `mobile/hooks/useAuth.ts` — wraps `lib/auth.ts`; exposes `user`, `isLoading`, `login(token)`, `logout()`
- `mobile/components/auth/GoogleSignInButton.tsx` — expo-auth-session Google OAuth2 flow → exchange code → POST `/auth/google/mobile` → store JWT
- `mobile/components/auth/AppleSignInButton.tsx` — `expo-apple-authentication` → identityToken → POST `/auth/apple` → store JWT
- `mobile/app/auth/login.tsx` — shows both sign-in buttons; "Continue as Guest" navigates to `/(tabs)`
- `mobile/app/auth/onboarding.tsx` — 3-slide onboarding with `FlatList` + pagination dots; "Get Started" → `auth/login`
- `mobile/app/_layout.tsx` — on mount: checks stored token; if none → redirect to `auth/onboarding`

**Commit 5 — packages/shared User type + auth API client**

- `packages/shared/src/types/user.ts` finalized with all fields from Prisma model
- `packages/shared/src/api/auth.ts` — `fetchMe(client)`, `patchMe(client, input)`, `fetchStats(client)` typed wrappers

**Commit 6 — Guest mode**

- Backend: `GET /decks` and `GET /community/decks` use `optionalAuth` — guests get read access to community decks and can create local-only decks (no `userId`, stored in `guestSessionId` cookie)
- Frontend: `OnboardingPage` "Continue as Guest" stores a `guestSessionId` UUID, allows deck creation with in-memory state, shows "Sign in to sync" banner when guest creates a deck

**Commit 7 — Backend /users/me/stats stub**

- `GET /users/me/stats` returns real counts via Prisma aggregates (COUNT decks, cards, reviews), streak data, XP, AI calls used/remaining
- Frontend `SettingsPage.tsx` placeholder shows these stats

---

### M2: Decks + Cards CRUD (Days 4–5) — "first deck"

**Commit 1 — Prisma: decks + cards**

`prisma/schema.prisma` additions:

```prisma
model Deck {
  id              String   @id @default(cuid())
  userId          String?
  title           String
  description     String   @default("")
  examTag         String?
  isPublic        Boolean  @default(false)
  masteryPercent  Float    @default(0)
  cardCount       Int      @default(0)
  color           String   @default("#6366f1")
  emoji           String   @default("📚")
  createdAt       DateTime @default(now())
  updatedAt       DateTime @updatedAt
  user            User?    @relation(fields: [userId], references: [id], onDelete: Cascade)
  cards           Card[]
  communityDownloads CommunityDeckDownload[]
}

model Card {
  id             String   @id @default(cuid())
  deckId         String
  front          String
  back           String
  reading        String?
  notes          String?
  audioUrl       String?
  easeFactor     Float    @default(2.5)
  intervalDays   Int      @default(0)
  dueDate        DateTime @default(now())
  reps           Int      @default(0)
  lapses         Int      @default(0)
  createdAt      DateTime @default(now())
  updatedAt      DateTime @updatedAt
  deck           Deck     @relation(fields: [deckId], references: [id], onDelete: Cascade)
  reviews        CardReview[]
}
```

**Commit 2 — Backend deck + card routes**

- `src/routes/decks.ts`:
  - `GET /decks` — requireAuth → list user's decks with `cardCount` aggregate
  - `POST /decks` — requireAuth → featureGate("deck_limit") → create deck
  - `GET /decks/:id` — requireAuth → deck + cards
  - `PATCH /decks/:id` — requireAuth → update title/description/examTag/color/emoji
  - `DELETE /decks/:id` — requireAuth → cascade delete cards
- `src/routes/cards.ts`:
  - `GET /decks/:deckId/cards` — requireAuth → paginated cards (limit/offset)
  - `POST /decks/:deckId/cards` — requireAuth → create card; update deck.cardCount
  - `PATCH /cards/:id` — requireAuth → update front/back/reading/notes
  - `DELETE /cards/:id` — requireAuth → delete; decrement deck.cardCount
- `src/middleware/featureGate.ts` — `featureGate("deck_limit")` checks `user.subscriptionTier === "free" && deckCount >= 3`; returns 403 with `{ code: "UPGRADE_REQUIRED", feature: "deck_limit" }`

**Commit 3 — Frontend deck pages + components**

Files created:
- `frontend/src/pages/DeckListPage.tsx` — `useDecks()` hook → `DeckGrid` + "Create Deck" FAB; AuthGuard wrapped; empty state with camera CTA if 0 decks
- `frontend/src/pages/DeckDetailPage.tsx` — deck header with emoji/color, `masteryPercent` ring, card list, "Add Card" button, "Study" CTA
- `frontend/src/components/decks/DeckCard.tsx` — card with emoji, title, card count, mastery ring (SVG), "Study" button
- `frontend/src/components/decks/DeckGrid.tsx` — responsive CSS grid; renders skeletons while loading
- `frontend/src/components/decks/DeckCardSkeleton.tsx` — Tailwind animate-pulse skeleton
- `frontend/src/components/decks/CreateDeckModal.tsx` — HeadlessUI Dialog; fields: title, description, exam tag dropdown (JLPT N5/N4/N3/N2/N1, センター, 英検 2/準1/1), emoji picker, color picker
- `frontend/src/components/decks/DeckOptionsMenu.tsx` — HeadlessUI Menu: Edit, Share, Delete
- `frontend/src/components/cards/CardEditor.tsx` — full card editor: front textarea, back textarea, reading furigana field, notes field, save/cancel
- `frontend/src/components/cards/CardEditorRow.tsx` — inline row for card list view (click to expand)
- `frontend/src/hooks/useDecks.ts` — `useQuery` + `useMutation` wrappers for all deck endpoints
- `frontend/src/hooks/useCards.ts` — same for card endpoints
- `frontend/src/api/decks.ts` — typed API calls
- `frontend/src/api/cards.ts` — typed API calls

**Commit 4 — Mobile deck screens**

Files created:
- `mobile/app/(tabs)/index.tsx` — deck list with `FlatList`; "Create Deck" FAB; pull-to-refresh; empty state
- `mobile/app/deck/[id].tsx` — deck detail: header with emoji/color/mastery ring, horizontal card scroll preview, full card list, "Study Now" button
- `mobile/app/deck/new.tsx` — create deck form in a Stack screen
- `mobile/components/decks/DeckCard.tsx` — native card with emoji, title, mastery ring (react-native-svg)
- `mobile/components/decks/DeckGrid.tsx` — `FlatList` with 2-column layout
- `mobile/components/decks/CreateDeckSheet.tsx` — `BottomSheet` with form fields
- `mobile/components/cards/CardEditorSheet.tsx` — `BottomSheet` with front/back/reading fields
- `mobile/hooks/useDecks.ts` — React Query wrappers
- `mobile/hooks/useCards.ts` — React Query wrappers

**Commit 5 — packages/shared Deck + Card types + API client**

- `packages/shared/src/types/deck.ts` — `Deck`, `CreateDeckInput`, `UpdateDeckInput`, `ExamTag` enum
- `packages/shared/src/types/card.ts` — `Card`, `CreateCardInput`, `UpdateCardInput`
- `packages/shared/src/api/decks.ts` — typed client functions
- `packages/shared/src/api/cards.ts` — typed client functions

**Commit 6 — Empty states + mastery percent**

- Backend: `/decks/:id` response includes `masteryPercent` = `COUNT(cards WHERE reps > 0 AND intervalDays >= 21) / COUNT(cards) * 100`
- Frontend/mobile: empty state component with Lottie animation placeholder, "No decks yet — create your first!" copy, camera icon CTA

---

### M3: AI Card Generation (Days 6–8) — "the magic moment"

**Commit 1 — File upload infrastructure**

Files created/modified:
- `src/lib/azure-blob.ts` — `uploadFile(buffer, filename, container)` → returns public URL; `deleteFile(url)`; uses `AZURE_STORAGE_CONNECTION_STRING`
- `src/routes/ai.ts` — multer config: memory storage, 20MB limit, accept image/*, application/pdf; `POST /ai/upload` → Azure Blob upload → return `{ uploadUrl, fileId }`
- `prisma/schema.prisma` additions: `AiGenerationJob` model

```prisma
model AiGenerationJob {
  id           String   @id @default(cuid())
  userId       String
  deckId       String?
  status       String   @default("pending")
  fileUrl      String
  fileType     String
  cardsJson    Json?
  errorMessage String?
  createdAt    DateTime @default(now())
  updatedAt    DateTime @updatedAt
}
```

**Commit 2 — AI generation endpoint + job tracking**

- `src/routes/ai.ts` additions:
  - `POST /ai/generate` — requireAuth → featureGate("ai_calls") → create `AiGenerationJob` row → enqueue background job → return `{ jobId }`
  - `GET /ai/jobs/:id` — requireAuth → return job status + cardsJson when complete
- `src/lib/azure-openai.ts` — `AzureOpenAI` client setup; `generateCardsFromImage(imageUrl)` → GPT-4o vision; `generateCardsFromText(text)` → GPT-4o text; Ollama fallback when Azure unavailable
- Background job worker in `simple-server.ts`: in-memory queue (production: Bull/Redis); processes jobs sequentially; emits WebSocket events on status change

**Commit 3 — Azure OpenAI GPT-4o vision (photo OCR → cards)**

`src/lib/azure-openai.ts` — `generateCardsFromImage`:

```typescript
// System prompt excerpt (full version in src/lib/ai-prompt.ts):
// You are a Japanese study expert. Analyze the image and extract study material.
// Return a JSON array of flashcards. Each card must have:
//   front: the Japanese term or question (kanji preferred)
//   back: the answer or definition in Japanese and/or English
//   reading: furigana reading for any kanji (hiragana)
//   notes: usage example or grammar note if helpful
// For vocabulary: front=kanji, back=meaning, reading=hiragana
// For grammar: front=pattern, back=explanation+example
// For exam Q&A: front=question, back=answer with brief rationale
// Return 3-20 cards. If the image has no study content, return [].
```

GPT-4o call with `response_format: { type: "json_object" }`, temperature 0.2 (deterministic extraction), max_tokens 2000.

**Commit 4 — PDF text extraction → cards**

- `src/lib/pdf-extract.ts` — `extractTextFromPdf(buffer)` using `pdf-parse`; chunks text by page; returns `string[]`
- `src/lib/azure-openai.ts` — `generateCardsFromText(text)`: same prompt as image but text input; chunks large PDFs to 3000-token windows, generates cards per chunk, deduplicates by front similarity
- `src/routes/ai.ts` — detects fileType in job processor; routes to image or text path

**Commit 5 — AI prompt tuned for Japanese**

`src/lib/ai-prompt.ts` — exports `SYSTEM_PROMPT` (full prompt) and `buildUserPrompt(context)`:

- Handles mixed kanji/kana/romaji input
- Recognizes JLPT vocabulary list patterns (word + part-of-speech + meaning)
- Recognizes 文法 (grammar) N+verb pattern explanations
- Recognizes センター/共通テスト multiple-choice format → extracts question + correct answer as card
- Recognizes 英検 reading comprehension → extracts key vocabulary
- Adds a `confidence` field (0–1) per card; cards with confidence < 0.5 are flagged for user review
- Strips OCR artifacts (stray punctuation, page numbers)

**Commit 6 — WebSocket job status events**

- `src/lib/websocket.ts` — `wss` instance; `broadcast(userId, event)` sends to all connections for that user; `registerConnection(ws, userId)`
- `simple-server.ts` — `wss.on("connection")` → authenticate via `?token=` query param → register connection
- Job processor emits `{ type: "job_update", jobId, status, progress, cards? }` via `broadcast`
- `frontend/src/hooks/useWebSocket.ts` — `useWebSocket(token)` subscribes; `useAiGeneration` hook listens for job updates and invalidates React Query cache
- `mobile/lib/wsClient.ts` — same pattern with native WebSocket API

**Commit 7 — Mobile camera capture flow**

Files created:
- `mobile/app/ai/camera.tsx` — `expo-camera` full-screen view; tap-to-focus; shutter button; gallery picker fallback; "Scan Document" mode for multi-page capture; on capture → compress to JPEG 80% → POST `/ai/upload` → navigate to `ai/preview` with `jobId`
- `mobile/components/ai/CaptureOverlay.tsx` — alignment guides (corner brackets), flash toggle, camera/gallery toggle
- `mobile/components/ai/GenerationProgress.tsx` — animated progress ring + status text ("Analyzing image...", "Extracting cards...", "Ready!")
- `mobile/app/(tabs)/camera.tsx` — entry point; checks camera permission; redirects to `ai/camera`

**Commit 8 — Mobile generated cards preview + save**

Files created:
- `mobile/app/ai/preview.tsx` — `GeneratedCardsPreview` screen: shows cards in swipeable list; each card is editable inline (tap front/back to edit); confidence badges ("Low confidence" warning); "Save All" → POST `/decks/:id/cards` bulk; "Save Selected" for cherry-picking; "Discard" with confirmation
- `mobile/components/cards/GeneratedCardsPreview.tsx` — card preview component with edit mode

**Commit 9 — Frontend file upload flow**

Files created:
- `frontend/src/components/ai/FileUploadZone.tsx` — react-dropzone; accepts image/* + application/pdf; shows preview; drag-and-drop + click-to-browse; max 20MB; on drop → POST `/ai/upload` → start generation job
- `frontend/src/components/ai/GenerationStatusModal.tsx` — HeadlessUI Dialog; shows progress bar + status text via WebSocket; "Edit Cards" CTA when complete
- `frontend/src/components/cards/GeneratedCardsPreview.tsx` — same as mobile; table layout on desktop; inline edit
- `frontend/src/pages/DeckDetailPage.tsx` updated — "Add Cards from Photo/PDF" button opens `FileUploadZone`

**Commit 10 — Rate limiting: AI calls**

- Backend: `featureGate("ai_calls")` in `src/middleware/featureGate.ts` — free tier: 5 calls/month; checks `users.aiCallsThisMonth` vs limit; resets monthly via `aiCallsResetAt`
- Cron in `src/lib/cron.ts`: `0 0 1 * *` → `UPDATE users SET aiCallsThisMonth = 0, aiCallsResetAt = NOW()`
- Frontend/mobile: `AiCallsRemainingBadge` shows "3 / 5 AI scans this month"; clicking → `/upgrade`
- 403 response includes `{ code: "AI_LIMIT_REACHED", resetAt: "...", upgradeUrl: "/upgrade" }`

---

### M4: Spaced Repetition Study Sessions (Days 9–11) — "daily habit"

**Commit 1 — Prisma: reviews + sessions**

```prisma
model StudySession {
  id           String   @id @default(cuid())
  userId       String
  deckId       String
  startedAt    DateTime @default(now())
  endedAt      DateTime?
  cardsReviewed Int     @default(0)
  xpEarned     Int      @default(0)
  user         User     @relation(fields: [userId], references: [id], onDelete: Cascade)
  reviews      CardReview[]
}

model CardReview {
  id          String   @id @default(cuid())
  sessionId   String
  cardId      String
  rating      Int
  responseMs  Int
  easeBefore  Float
  easeAfter   Float
  intervalBefore Int
  intervalAfter  Int
  reviewedAt  DateTime @default(now())
  session     StudySession @relation(fields: [sessionId], references: [id], onDelete: Cascade)
  card        Card         @relation(fields: [cardId], references: [id], onDelete: Cascade)
}
```

**Commit 2 — FSRS-4.5 algorithm**

`src/lib/fsrs.ts`:

```typescript
// FSRS-4.5 open-source algorithm — adapted from github.com/open-spaced-repetition/fsrs4anki
// Credit: Jarrett Ye (L-M-Sherlock) — MIT License

export const FSRS_PARAMS = {
  w: [0.4072, 1.1829, 3.1262, 15.4722, 7.2102, 0.5316, 1.0651, 0.06509,
      1.616, 0.1544, 1.0071, 1.9395, 0.11, 0.29605, 2.2698, 0.2658,
      2.9898, 0.51],
  requestRetention: 0.9,
  maximumInterval: 36500,
};

export type Rating = 1 | 2 | 3 | 4; // Again=1, Hard=2, Good=3, Easy=4

export interface FSRSCard {
  easeFactor: number;
  intervalDays: number;
  reps: number;
  lapses: number;
  dueDate: Date;
}

export function scheduleCard(card: FSRSCard, rating: Rating, reviewDate: Date): FSRSCard
// Returns updated card fields after a review
```

Full FSRS-4.5 implementation: stability, difficulty, retrievability calculations; new card bootstrap intervals (`[1,2,4,4]` days for ratings 1-4); review intervals with fuzz ±10%; lapses handled with stability penalty.

**Commit 3 — Study session API**

`src/routes/study.ts`:
- `POST /sessions/start` — requireAuth → find due cards for `deckId` (`dueDate <= now()`, ordered by dueDate ASC, limit = `min(dailyGoalCards, 50)`); create `StudySession`; return `{ sessionId, cards }`
- `POST /sessions/:id/review` — requireAuth → body `{ cardId, rating, responseMs }` → run FSRS → update card → create CardReview → return `{ nextCard, sessionStats }`
- `POST /sessions/:id/end` — requireAuth → close session; calculate XP earned; update streak; return `{ session, xpEarned, streakUpdate, levelUp }`
- Streak logic: if `lastStudiedAt` is yesterday or null → increment streak; if 2+ days ago → reset to 1; update `longestStreak` if new record; XP: +1 per Good/Easy, +2 bonus per 10 cards

**Commit 4 — Mobile study session screen**

Files created:
- `mobile/app/study/[deckId].tsx` — full-screen study mode; fetches session; card flip animation with `react-native-reanimated` (rotateY 0→180deg, 300ms spring); shows front on load; tap or "Reveal" button shows back; `StudyRatingButtons` below; progress bar top
- `mobile/components/study/StudyRatingButtons.tsx` — four buttons: Again (red), Hard (orange), Good (green), Easy (blue); shows `+1 XP` / `+2 XP` micro-labels; disabled 200ms after tap to prevent double-rating
- `mobile/components/study/ProgressBar.tsx` — thin bar at top; animates on each review
- `mobile/components/study/ExamCountdownBanner.tsx` — shows "14 days until 共通テスト" if `user.examDate` within 30 days
- `mobile/components/cards/FlashCard.tsx` — card component with front/back flip; reading (furigana) shown below front; audio play button if `audioUrl` present

**Commit 5 — Mobile session complete screen**

Files created:
- `mobile/app/study/complete.tsx` — shows: cards reviewed count, XP earned with number-counter animation, streak fire animation if streak extended, level-up overlay if level crossed, "Study Again" + "Back to Decks" CTAs
- `mobile/components/study/SessionSummary.tsx` — summary stats grid (reviewed/due/correct rate)
- `mobile/components/ui/XpAnimation.tsx` — `+12 XP` floating text animation (fade up, scale)

**Commit 6 — Backend streak calculation**

- `src/routes/study.ts` `POST /sessions/:id/end`:
  - `upsert` on `Streak` table
  - `CURRENT_DATE - lastStudiedAt::date` determines streak logic
  - `xp += cardsReviewed + (cardsReviewed >= 10 ? 2 : 0)`
  - `level = Math.floor(Math.sqrt(xp / 100)) + 1` (level curve)
  - Returns `{ streakBroken: bool, streakExtended: bool, newStreak: int, xpTotal: int, levelUp: bool, newLevel: int }`

**Commit 7 — Frontend web study session**

- `frontend/src/pages/StudySessionPage.tsx` — same logic as mobile; CSS 3D card flip with `transform-style: preserve-3d`; keyboard shortcuts: Space=reveal, 1=Again, 2=Hard, 3=Good, 4=Easy
- `frontend/src/pages/SessionCompletePage.tsx` — summary stats + XP animation (CSS keyframe)
- `frontend/src/components/study/FlashCardFlip.tsx` — CSS 3D flip component
- `frontend/src/components/study/StudyRatingButtons.tsx` — four HeadlessUI buttons

**Commit 8 — XP system + level milestones**

- `packages/shared/src/types/study.ts` — `StudySession`, `CardReview`, `Rating`, `SessionResult`, `StreakUpdate` types
- Level milestone notifications (backend): level 5, 10, 25, 50 → create notification row (consumed by M8)
- Frontend/mobile: level badge in TopBar/tab header; level-up modal with confetti (lottie or pure CSS)

**Commit 9 — Exam countdown + daily goal auto-calc**

- Backend `PATCH /users/me`: when `examDate` is set, auto-calculate `dailyGoalCards`:
  - `daysLeft = (examDate - today).days`
  - `totalCardsRemaining = unmastered cards across all decks`
  - `dailyGoalCards = Math.ceil(totalCardsRemaining / daysLeft * 1.2)` (20% buffer), clamped 10–100
- Mobile `ExamCountdownBanner` shows: "12 days left — review 34 cards today to be ready"
- Frontend: same banner on `DeckListPage`

---

### M5: TTS Audio (Days 12–13) — "listening practice"

**Commit 1 — Azure Cognitive Services TTS backend**

Files created/modified:
- `src/lib/azure-speech.ts`:
  - `generateSpeech(text, voice, speed)` → Buffer (MP3)
  - Supports voices: `ja-JP-NanamiNeural` (female, default), `ja-JP-KeitaNeural` (male), `ja-JP-AoiNeural` (young female)
  - Speed: 0.75x, 1.0x, 1.25x, 1.5x mapped to SSML `<prosody rate=...>`
  - SSML template: `<speak><voice name="{voice}"><prosody rate="{rate}">{text}</prosody></voice></speak>`
  - Caches MP3 to Azure Blob `tanki-audio/{cardId}-{voice}-{speed}.mp3`; returns URL
- `src/routes/audio.ts`:
  - `POST /cards/:id/audio` — requireAuth → check if `audioUrl` already set → if not, call `generateSpeech` with card's `front` text → save URL to `cards.audioUrl` → return `{ audioUrl }`
  - `POST /decks/:id/audio/bulk` — requireAuth → queue TTS generation for all cards in deck without audio (pro feature, processes async)

**Commit 2 — TTS audio in mobile study session**

Files modified:
- `mobile/components/cards/FlashCard.tsx` — audio play button (speaker icon) on card face; `expo-av` `Audio.Sound.createAsync(audioUrl)` → `sound.playAsync()`; shows loading spinner during generation if `audioUrl` null; auto-plays on card reveal if user setting `autoPlayAudio=true`
- `mobile/app/study/[deckId].tsx` — on session start, pre-load audio for first 3 cards; on "Good/Easy" rating, pre-load audio for next card

**Commit 3 — Mobile TTS settings**

Files modified:
- `mobile/app/(tabs)/settings.tsx` — TTS section: voice selector (3 options with play preview button), speed slider (0.75x / 1.0x / 1.25x / 1.5x), auto-play toggle; changes PATCH `/users/me`
- `src/routes/users.ts` — accepts `ttsVoice` and `ttsSpeed` in PATCH body

**Commit 4 — Frontend TTS playback**

- `frontend/src/components/study/FlashCardFlip.tsx` — audio button using `<audio>` element; lazy-generates audio (POST if no URL); keyboard shortcut `A` = play audio
- `frontend/src/pages/SettingsPage.tsx` — same voice/speed settings

**Commit 5 — backfill-tts-audio.ts script**

`scripts/backfill-tts-audio.ts`:
- Queries all cards without `audioUrl` across all decks
- Generates TTS in batches of 10 (rate limit aware)
- Progress bar via `ora`; logs successes/failures
- Usage: `npx ts-node scripts/backfill-tts-audio.ts --voice=ja-JP-NanamiNeural --dry-run`

---

### M6: Community Deck Library (Days 14–16) — "network effect"

**Commit 1 — Prisma: community_deck_downloads + deck public fields**

```prisma
// Added to Deck model:
//   isPublic Boolean @default(false)
//   examTag  String?  (already added in M2)
//   publishedAt DateTime?
//   downloadCount Int @default(0)
//   rating Float @default(0)
//   ratingCount Int @default(0)

model CommunityDeckDownload {
  id           String   @id @default(cuid())
  userId       String
  sourceDeckId String
  forkDeckId   String   @unique
  downloadedAt DateTime @default(now())
  sourceDeck   Deck     @relation(fields: [sourceDeckId], references: [id])
}
```

**Commit 2 — Backend community routes**

`src/routes/community.ts`:
- `POST /decks/:id/publish` — requireAuth → proFeature("publish") → set `isPublic=true`, `publishedAt=now()`
- `GET /community/decks` — optionalAuth → query params: `examTag`, `q` (title search), `sort` (popular/new/rating), `page`; returns paginated decks with card count, download count, creator displayName (no email)
- `GET /community/decks/:id` — optionalAuth → deck detail with first 5 cards preview
- `POST /community/decks/:id/download` — requireAuth → featureGate("deck_limit") → deep-clone deck + all cards into user's library → create `CommunityDeckDownload` row → increment `downloadCount`; returns new `forkDeckId`
- `POST /community/decks/:id/rate` — requireAuth → upsert rating (1–5); recalculate `rating` average + `ratingCount`

**Commit 3 — Mobile community tab**

Files created:
- `mobile/app/(tabs)/community.tsx` — exam tag chips row (JLPT N5/N4/N3/N2/N1, センター, 英検, 漢字) with horizontal scroll; search bar; `FlatList` of community decks; pull-to-refresh; sort toggle (Popular/New)
- `mobile/app/community/[id].tsx` — deck detail: title, creator, card count, rating stars, card preview list, "Download to My Decks" button with duplicate check
- `mobile/components/community/CommunityDeckCard.tsx` — deck card with exam tag badge, download count, star rating, card count
- `mobile/components/community/ExamTagChips.tsx` — horizontal scrollable chip row; selected chip highlighted

**Commit 4 — Frontend community page**

Files created:
- `frontend/src/pages/CommunityPage.tsx` — same UX as mobile; exam tag filters as horizontal chip row; search input; deck grid; pagination
- `frontend/src/components/community/CommunityDeckCard.tsx` — deck card
- `frontend/src/components/community/ExamTagChips.tsx` — chip row
- `frontend/src/components/community/CommunitySearchBar.tsx` — debounced search input

**Commit 5 — Seed script: starter decks**

`scripts/seed-community-decks.ts`:

Creates a system user `system@tanki.app` (no auth, seed-only) and publishes 25 starter decks:
- JLPT N5: 100 essential vocabulary cards (number words, basic verbs, counters)
- JLPT N4: 300 vocabulary + 50 grammar patterns
- JLPT N3: 500 vocabulary + 100 grammar patterns
- JLPT N2: 1000 vocabulary + 200 grammar patterns
- JLPT N1: 2000 vocabulary cards
- 英検 2級: 400 vocabulary cards
- センター頻出漢字: 200 kanji readings
- 共通テスト英単語: 300 English-Japanese pairs

Each card has: front (Japanese kanji), back (English meaning + Japanese explanation), reading (hiragana furigana), notes (usage example). Idempotent (upsert by front+deckId).

Usage: `npx ts-node scripts/seed-community-decks.ts`

**Commit 6 — packages/shared community types + API client**

- `packages/shared/src/types/community.ts` — `CommunityDeck`, `DownloadDeckResult`, `ExamTag` enum with all values
- `packages/shared/src/api/community.ts` — typed wrappers

---

### M7: Monetization + Subscriptions (Days 17–18) — "revenue"

**Commit 1 — Backend subscription fields + feature gate**

- `prisma/schema.prisma`: `User.subscriptionTier` (`"free"` | `"pro"`), `User.subscriptionExpiresAt`
- `src/middleware/featureGate.ts` finalized — all gates:
  - `deck_limit`: free tier max 3 decks
  - `ai_calls`: free tier 5 AI scans/month
  - `publish`: pro only (publish deck to community)
  - `cloud_sync`: pro only (sync enabled, free shows "Sign in to sync" banner but data stored locally)
  - `streak_freeze`: pro only (1 freeze/week)
  - `bulk_tts`: pro only (bulk TTS generation for whole deck)
- Gate returns `{ code: "UPGRADE_REQUIRED", feature, limits: { current, max }, upgradeUrl }`

**Commit 2 — Free tier enforcement**

- `GET /decks` (free): returns all decks but `isLimitReached: true` flag when count >= 3
- `POST /decks` (free): 403 when count >= 3
- `POST /ai/generate` (free): 403 when `aiCallsThisMonth >= 5`
- All gated routes return upgrade prompt JSON

**Commit 3 — Mobile RevenueCat integration**

Files created:
- `mobile/lib/revenuecat.ts` — `Purchases.configure(REVENUECAT_API_KEY)`; `getOfferings()` → returns monthly/annual product with pricing; `purchasePackage(pkg)` → returns `CustomerInfo`; `restorePurchases()`
- `mobile/app/upgrade.tsx` — `PaywallScreen`: feature comparison table (Free vs Pro), price selector (monthly/annual with savings badge), "Start Free Trial" CTA (7-day trial), "Restore Purchases" link
- `mobile/components/ui/PaywallSheet.tsx` — bottom sheet paywall triggered from feature gates
- `mobile/hooks/useSubscription.ts` — `useQuery` on `Purchases.getCustomerInfo()`; exposes `isPro`, `expiresAt`

Pro features listed on paywall:
- Unlimited decks (free: 3)
- 50 AI scans/month (free: 5)
- Cloud sync across all devices
- Publish decks to community library
- Streak freeze (1/week)
- Bulk TTS audio generation
- Priority support

**Commit 4 — Mobile upgrade triggers**

- When deck creation hits limit: `DeckGrid` shows "Upgrade to add more decks" card; tapping → `PaywallSheet`
- When AI scan hits limit: `GenerationProgress` shows "You've used all 5 free scans this month" + upgrade CTA
- When cloud sync accessed by free user: `SettingsPage` shows locked `CloudSyncRow` with pro badge

**Commit 5 — Frontend /upgrade page**

Files created:
- `frontend/src/pages/UpgradePage.tsx` — full-page upgrade CTA; feature comparison table (`<table>` with checkmarks); pricing cards (monthly ¥980/month, annual ¥7,800/year); "Subscribe" button → links to mobile app (primary subscription surface); for web-only users → Stripe checkout (future milestone placeholder)
- `frontend/src/components/ui/PricingCard.tsx` — pricing card component

**Commit 6 — RevenueCat webhook**

`src/routes/subscriptions.ts`:
- `POST /subscriptions/revenuecat/webhook` — verifies `X-RevenueCat-Signature` header (HMAC SHA256 with `REVENUECAT_WEBHOOK_SECRET`); handles events: `INITIAL_PURCHASE`, `RENEWAL`, `CANCELLATION`, `EXPIRATION`; updates `users.subscriptionTier` + `users.subscriptionExpiresAt` via Prisma

**Commit 7 — Free tier cloud sync**

- Free users: decks stored server-side (they ARE cloud-stored after login), but sync is limited to 3 decks. "Cloud sync" pro feature = unlimited decks synced + higher card count limit (unlimited vs 100 cards/deck on free).
- If user hits card limit, graceful degradation: `POST /cards` returns 403 with `{ code: "UPGRADE_REQUIRED" }`, mobile shows paywall sheet, web shows upgrade banner. No silent data loss, no crash.

---

### M8: Notifications + Polish (Days 19–21) — "retention"

**Commit 1 — Prisma: notifications**

```prisma
model Notification {
  id        String   @id @default(cuid())
  userId    String
  type      String
  title     String
  body      String
  data      Json?
  readAt    DateTime?
  sentAt    DateTime?
  createdAt DateTime @default(now())
  user      User     @relation(fields: [userId], references: [id], onDelete: Cascade)
}
```

Types: `streak_extended`, `streak_broken`, `streak_milestone`, `exam_countdown_7d`, `exam_countdown_3d`, `exam_countdown_1d`, `level_up`, `daily_reminder`, `deck_published_downloaded`

**Commit 2 — Backend notification cron**

`src/lib/cron.ts`:
- `0 0 * * *` (midnight JST): for each user with `examDate` — check days remaining; create exam_countdown notifications at 7d, 3d, 1d
- `0 20 * * *` (8pm JST default, respects user preference): create `daily_reminder` notification for users who have not studied today
- Streak break detection: on session end, if `streak > 0 && daysSinceLastStudy > 1` → create `streak_broken` notification
- `src/routes/users.ts` — `PATCH /users/me/reminder-time` → store preferred reminder time (default 20:00 JST)

**Commit 3 — Mobile push notifications**

Files created:
- `mobile/lib/notifications.ts`:
  - `registerForPushNotifications()` → `expo-notifications` → returns Expo push token → store in `AsyncStorage` and PATCH `/users/me` with `pushToken`
  - `setupNotificationHandlers()` → `Notifications.addNotificationReceivedListener`, `addNotificationResponseReceivedListener`
- `mobile/app/_layout.tsx` updated — call `registerForPushNotifications()` after login; set notification categories (study, streak, exam)
- Backend: Expo Push Notifications API (`https://exp.host/--/api/v2/push/send`) batch endpoint for sending push messages from cron jobs

**Commit 4 — Daily reminder at chosen time**

- `mobile/app/(tabs)/settings.tsx` additions: notification section — time picker for daily reminder (`@react-native-community/datetimepicker`), toggle for each notification type (study reminder, streak alerts, exam countdown)
- Backend stores preferences as JSON in `users.notificationPrefs`

**Commit 5 — Streak freeze (pro)**

- Backend `POST /sessions/:id/end`: if `daysSinceLastStudy === 1 && user.isPro && streak.freezesLeft > 0` → decrement `freezesLeft`, keep streak intact
- Mobile `SettingsPage`: "Streak Freezes: 1 remaining" with tooltip explaining the feature; frozen streaks show snowflake ❄️ badge on streak counter
- Cron: `0 0 * * 1` (Monday) → restore 1 freeze for pro users

**Commit 6 — Dark mode**

- `frontend/tailwind.config.js`: `darkMode: "class"`; extend `colors.surface`, `colors.on-surface`
- `frontend/src/App.tsx`: reads `prefers-color-scheme` media query; stores in localStorage; applies `dark` class to `<html>`
- All frontend components: add `dark:` Tailwind variants for bg/text/border
- `mobile/lib/constants.ts`: theme tokens (light + dark)
- `mobile/components/ui/ThemedView.tsx`: reads `useColorScheme()` from NativeWind; applies correct palette
- All mobile components: add `dark:` NativeWind variants

**Commit 7 — Playwright e2e tests**

Files created:
- `tests/e2e/onboarding.spec.ts` — open app → see onboarding slides → continue as guest → see empty deck list
- `tests/e2e/deck-crud.spec.ts` — login (mock auth) → create deck → add 3 cards manually → verify deck shows mastery 0% → edit card → delete deck
- `tests/e2e/study-session.spec.ts` — create deck with 5 seeded cards → start study session → rate all 4 cards → verify session complete screen shows XP → verify streak incremented
- `tests/e2e/ai-generation.spec.ts` — mock Azure OpenAI response → upload image → verify generated cards preview → save → verify deck card count

**Commit 8 — Performance polish**

- Frontend:
  - React Query: `staleTime: 60_000` on deck list; `gcTime: 300_000` on community decks
  - `DeckGrid` + `CardList` virtualized with `@tanstack/react-virtual` for 500+ cards
  - Hero images: `loading="lazy"` + `width`/`height` attributes to prevent CLS
  - Bundle: `vite.config.ts` `build.rollupOptions.output.manualChunks` splitting react, tanstack-query, headlessui into separate chunks
- Mobile:
  - `FlatList` `removeClippedSubviews={true}` + `maxToRenderPerBatch={10}` on card lists
  - `expo-image` instead of `Image` for async decoding + memory cache
  - Pre-fetching next deck's cards on deck detail screen hover/focus

---

### M9: Agents + SEO (Days 22–23) — "growth automation"

**Commit 1 — tanki-seo-opportunity-agent**

`agents/tanki-seo-opportunity-agent/manifest.json`:

```json
{
  "id": "tanki-seo-opportunity-agent",
  "name": "Tanki SEO Opportunity Agent",
  "description": "Discovers high-value SEO opportunities for tanki.app — JLPT study, 英検, センター, Japanese grammar content gaps",
  "category": "seo",
  "cron": "30 */2 * * *",
  "timezone": "Asia/Tokyo",
  "owner": "mperry@northernsoftwareconsulting.com",
  "kind": "python",
  "entry_command": "python3 agents/tanki-seo-opportunity-agent/agent.py",
  "capabilities": ["read_web", "write_recommendations"],
  "goals": [
    {
      "id": "organic-clicks-growth",
      "title": "Grow organic search clicks",
      "metric": { "name": "weekly_organic_clicks", "current": 0, "target": 5000, "direction": "up", "unit": "clicks/week" }
    },
    {
      "id": "jlpt-content-coverage",
      "title": "Cover all JLPT study intent keywords",
      "metric": { "name": "jlpt_keywords_covered", "current": 0, "target": 200, "direction": "up", "unit": "keywords" }
    }
  ]
}
```

`agents/tanki-seo-opportunity-agent/site.yaml` — full SEO agent config: `site_url: https://tanki.app`, GSC property, target exam tags (JLPT/英検/センター), implementer scope `["src/**", "frontend/**", "db/**", "*.md"]`, excluded `["mobile/**"]`

**Commit 2 — tanki-catalog-audit-agent**

`agents/tanki-catalog-audit-agent/manifest.json` — community deck quality auditor: checks card count >= 10, front/back populated, no placeholder text ("TODO", "..."), valid furigana readings, no duplicate cards within deck. Runs `0 6 * * *`.

`agents/tanki-catalog-audit-agent/site.yaml` — DB connection from env, audit thresholds, notification email.

Goals:
- `community-deck-quality`: `min_quality_score` across all published decks (target 85%)
- `flagged-decks-resolved`: decks flagged and fixed within 7 days (target 90%)

**Commit 3 — CLAUDE.md**

`CLAUDE.md`:

```markdown
# Tanki — Claude Instructions

## North Star
DAU (daily active users) studying at least one card.
Success = users returning daily to study. Every change should move DAU, retention, or study completion rate.

## Key metrics (all from PostgreSQL)
- DAU: COUNT(DISTINCT userId) from study_sessions WHERE startedAt > NOW() - INTERVAL '1 day'
- D7 retention: users who studied on day 7 after signup / total signups on that cohort day
- Cards reviewed per DAU: AVG(cardsReviewed) per session
- Community deck downloads/day: CommunityDeckDownload created in last 24h

## Decision rule
Before any feature work: does it move DAU? If not clear, ask.
Refuse: UI polish with no retention rationale; refactors with no metric target; features "users might want" with no data.

## Site-specific rules
- All Japanese text in the app uses UTF-8; never escape kanji to HTML entities
- Exam tags are fixed: JLPT_N5 / N4 / N3 / N2 / N1 / EIKEN_2 / EIKEN_PRE1 / EIKEN_1 / CENTER / KYOTSU / KANJI — don't add new ones without updating the ExamTag enum in packages/shared
- TTS always uses Azure Cognitive Services Speech; never use Web Speech API (inconsistent Japanese quality)
- AI card generation always uses GPT-4o (vision); never downgrade to a non-vision model
- Community deck seeder is idempotent — safe to re-run

## Implementer scope
allowed_paths: ["src/**", "frontend/**", "prisma/**", "packages/shared/**", "scripts/**", "*.md", "docker-compose.yml", ".env.example"]
excluded_paths: ["mobile/**", "azure/**", ".github/**"]
post_apply: { kick_mobile_build: false, kick_backend_deploy: true }
```

**Commit 4 — register-with-framework.sh**

`agents/register-with-framework.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail
FRAMEWORK_API_URL="${FRAMEWORK_API_URL:-http://localhost:8093}"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

for manifest in "$REPO_DIR"/agents/*/manifest.json; do
  agent_id="$(jq -r .id "$manifest")"
  echo "Registering $agent_id..."
  curl -sf -X PUT "$FRAMEWORK_API_URL/api/agents/$agent_id" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${FRAMEWORK_API_TOKEN:-}" \
    -d @"$manifest"
  echo " done"
done
```

**Commit 5 — azure/provision.sh + azure/deploy.sh**

`azure/provision.sh`:
- Creates resource group `tanki-rg` in `japaneast`
- Provisions: Azure Container Apps environment, Azure Database for PostgreSQL (Flexible, Burstable B1ms for dev / GP_Standard_D2ds_v4 for prod), Azure Blob Storage account `tankiblob`, Azure Cognitive Services (Speech + OpenAI), Azure Container Registry `tankiacr`
- Outputs all connection strings to `~/.tanki-azure/state.env`

`azure/deploy.sh`:
- Reads `~/.tanki-azure/state.env`
- Builds Docker image → pushes to `tankiacr`
- Updates Container App with new image + env vars
- Smoke check: `curl -sf https://api.tanki.app/health`

**Commit 6 — Final README.md**

`README.md` covers:
- What Tanki is (one paragraph)
- Prerequisites (Node 18, Docker, Azure CLI)
- Local setup (5 commands: clone → cp .env.example .env → docker-compose up -d → npm run db:migrate → npm run dev)
- Running tests (`npm test`)
- Mobile setup (`cd mobile && npx expo start`)
- Deploying to Azure (`bash azure/provision.sh && bash azure/deploy.sh`)
- Architecture diagram (ASCII): Browser/Mobile → Azure Container Apps → PostgreSQL + Blob + OpenAI + Speech
- Agent registration (`FRAMEWORK_API_URL=... bash agents/register-with-framework.sh`)
- Feature gates table (which features are free vs pro)

---

## Definition of Done

Each milestone is complete when:
- `npm run typecheck` exits 0 across all workspaces
- `npm run lint` exits 0
- `docker-compose up` with a fresh database boots backend to `/health → 200`
- All Playwright e2e tests in `tests/e2e/` pass (M8+)
- The feature is demonstrable on a physical device or iOS/Android simulator (all mobile milestones)
- No TypeScript `any` escapes in new code without a `// eslint-disable-next-line` comment + explanation

---

## Implementer Notes

**Parallelism.** Backend and frontend develop in parallel from M0 — they converge at M1 auth. All API contracts are defined in `packages/shared/src/api/` first; frontend + mobile consume those types.

**Shared API calls.** Every API function is implemented once in `packages/shared/src/api/`. Frontend imports from there with a web fetch adapter; mobile imports with an axios adapter. Never duplicate API logic.

**FSRS algorithm.** Adapt from `github.com/open-spaced-repetition/fsrs4anki` (MIT). Credit the original authors in `src/lib/fsrs.ts`. The algorithm is deterministic — write unit tests for the schedule function with known inputs/outputs before wiring it to the database.

**Azure OpenAI key.** Stored in `.env` as `AZURE_OPENAI_API_KEY` — never hardcoded, never committed. The `.gitignore` blocks `.env` and `*.env`. Log a warning at startup if the key is missing but don't crash — fall back to Ollama for dev.

**AI generation prompt is the product.** The prompt in `src/lib/ai-prompt.ts` is the most important piece of logic in the app. Iterate it with real JLPT vocabulary pages, grammar textbook photos, and センター past-paper scans before marking M3 done. Target: >= 90% of extracted cards are correct and usable without editing.

**Ollama fallback.** When `AZURE_OPENAI_ENDPOINT` is not set (local dev without Azure access), `src/lib/azure-openai.ts` routes to `OLLAMA_BASE_URL` with `llava` model for vision and `llama3` for text. Cards generated via Ollama are lower quality — add a `generatedBy: "ollama"` field in the job to surface a "⚠️ Lower quality — Azure OpenAI not configured" warning in the UI.

**RevenueCat.** Configure two products in App Store Connect before M7: `com.tanki.app.pro.monthly` and `com.tanki.app.pro.annual`. RevenueCat handles receipt validation — the backend only receives verified webhook events. Never trust client-reported subscription status; always check `users.subscriptionTier` from the database.

**Japanese text rendering.** Set `lang="ja"` on `<html>` in `frontend/index.html`. Use `font-family: "Noto Sans JP", sans-serif` loaded from Google Fonts as a CSS variable — fallback to system-ui. This ensures correct CJK glyph selection on Windows (without this, kanji render in Chinese glyph variants). In React Native, use `expo-font` to load Noto Sans JP for the same reason.

**Database migrations.** Never edit a committed migration file — create a new migration. The `prisma/migrations/` history is the source of truth for schema evolution. Use `prisma migrate dev` locally and `prisma migrate deploy` in the Azure deploy script.
