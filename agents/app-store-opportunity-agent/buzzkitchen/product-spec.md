# BuzzKitchen — Product Specification

## Vision Statement

BuzzKitchen is the recipe companion that keeps the whole cook-eat loop in one place. A user watches a Ryuji-style hands-free video, taps to add missing ingredients to a LINE-shareable shopping list, logs the result with a photo, and earns a streak — never leaving the app mid-cook again.

## Target Users

### Primary

**Japanese home cook, 25–45, smartphone-first.** Follows food creators on TikTok/YouTube/Instagram. Cooks weeknight meals from ingredients already at home. Uses LINE for household coordination. Frustrated that their favourite creator's app forces them to YouTube for the actual video.

### Secondary

**Japanese food enthusiast, 18–30, community-driven.** Wants to post cooking results, compare with others, and build a recipe collection. Currently migrates between Cookpad (for community) and YouTube (for video) because neither does both.

## MVP Features (V1)

### 1. Hands-Free Video Cooking Mode

Recipe detail embeds the creator's YouTube video via `react-native-youtube-iframe`. A Start Cooking toggle enlarges step text to 28sp, enables an 80pt Next Step button, and pins a Picture-in-Picture video overlay at bottom-right. Screen stays awake. No app-switch required.

### 2. Pantry Scanner with Match Score

Users maintain a pantry list via autocomplete text entry or barcode scan (`expo-barcode-scanner`). Each recipe shows a match score (matching ingredients ÷ total ingredients × 100). The home banner surfaces the top 3 recipes with ≥70% match. Endpoint: `GET /api/recipes/pantry-match?threshold=70`.

### 3. One-Tap Shopping List with LINE Share

From any recipe detail or meal plan, users tap Add to Shopping List. Ingredients are merged (deduped and quantities summed). Items are grouped by aisle category. A Share via LINE button generates a plain-text formatted list for household coordination. Lists are editable and persist across sessions.

### 4. Made It Cook Log with Photo and Rating

After viewing a recipe, users tap Made it! to open a half-sheet with camera capture or photo picker, 1–5 star rating, and an optional 140-character note. Logs are attached to the recipe and visible to followers on the social feed. Drives retention via cooking streak.

### 5. Recipe Search with Filters

Full-text search over recipe name, ingredients, and tags. Filters: max cook time (slider), vegetarian toggle, match-score minimum (requires pantry items). Results sorted by relevance then match score. Paginated at 20 per page.

### 6. Weekly Meal Plan

Seven-day calendar grid. Tap-to-assign recipes per day across breakfast, lunch, and dinner slots. A Generate Shopping List for Week button aggregates all ingredients into a new shopping list. Meal plan persists per user; guest users see a local-only version.

### 7. Social Feed

Paginated cursor-based feed of cook logs from followed users and trending logs. Each card shows: photo, recipe name, star rating, user avatar, relative timestamp, and emoji reaction strip with 5 reactions. Follow and unfollow users from their profile or the feed card.

### 8. Guest Mode First

On first launch, users land directly on the home feed with full recipe browsing and a single pre-loaded pantry item to demonstrate match score — no registration wall. Account creation is prompted at the Made it! action and at shopping list persistence. JWT auth is optional on all read endpoints.

## V2 Feature Ladder

1. **Barcode ingredient scan** — point camera at a product, auto-add to pantry with quantity from OCR.
2. **AI recipe suggestions** — enter available ingredients, receive GPT-4o-powered recipe recommendations via function calling.
3. **Creator channel subscriptions** — follow a chef's recipe feed, receive push notification when new recipes publish.
4. **Nutrition panel** — per-serving macros sourced from USDA FoodData Central API.
5. **Recipe scaling** — tap serving count to scale all ingredient quantities; shopping list reflects scaled amounts.

## V3 Feature Ladder

1. **In-app live cooking sessions** — creator broadcasts via HLS; viewers follow along with synced step highlights.
2. **Household profiles** — shared pantry and meal plan across household members under one subscription.
3. **Regional ingredient substitution** — AI suggests available alternatives when a listed ingredient is outside the user's region.
4. **Brand partnership recipe slots** — sponsored featured recipe injection with disclosure badge; CPM revenue stream.

## Non-Goals

- We do not build a recipe video production tool — we embed existing YouTube content.
- We do not build a general-purpose social network — the feed is recipe-log only.
- We do not scrape or rehost recipes without permission — all recipes are seeded by the operator or submitted by verified creators.
- We do not support desktop PWA or browser extension in V1 — web is marketing and discovery only.

## Success Metrics

| KPI | 30-Day Target | 90-Day Target |
|---|---|---|
| D7 retention | ≥ 22% | ≥ 28% |
| Cook logs per DAU | ≥ 0.18 | ≥ 0.30 |
| Shopping list taps per session | ≥ 0.25 | ≥ 0.38 |
| Pantry-match recipe opens | ≥ 15% of opens | ≥ 25% of opens |
| Hands-free cooking mode activations | ≥ 12% of recipe opens | ≥ 20% of recipe opens |
| Premium subscription conversion | ≥ 1.5% of MAU | ≥ 3.5% of MAU |
