# BuzzKitchen — API Specification

All endpoints are prefixed with `/api`. Responses are JSON. Error shape: `{ "error": "<message>", "code": "<snake_case_code>" }`.

**Auth levels:**
- `None` — public, no token required
- `JWT-optional` — reads JWT if present (personalises response), does not require it
- `JWT` — requires valid Bearer token; returns 401 if missing or expired

---

## Auth

### POST /api/auth/google
**Auth:** None  
**Request:** `{ id_token: string }` — Google ID token from client  
**Response:** `{ token: string, user: { id, display_name, avatar_url, email } }`  
**Notes:** Creates user on first sign-in (upsert by google_sub). Returns signed JWT with 7-day expiry.

### POST /api/auth/apple
**Auth:** None  
**Request:** `{ identity_token: string, full_name?: { givenName: string, familyName: string } }`  
**Response:** `{ token: string, user: { id, display_name, avatar_url, email } }`  
**Notes:** Verifies Apple identity token. full_name only sent on first sign-in.

### POST /api/auth/logout
**Auth:** JWT  
**Request:** (empty)  
**Response:** `{ success: true }`  
**Notes:** Revokes server-side token record for blacklist implementation.

### GET /api/auth/me
**Auth:** JWT  
**Response:** `{ id, display_name, avatar_url, email, cooking_streak, premium_until, created_at }`

---

## Recipes

### GET /api/recipes
**Auth:** JWT-optional  
**Query params:** `q` (full-text), `time_max` (integer minutes), `match_min` (integer 0–100, requires auth), `vegetarian` (boolean), `page` (integer, default 1)  
**Response:** `{ recipes: RecipeSummary[], total: number, page: number, page_size: 20 }`  
**RecipeSummary:** `{ id, title, thumbnail_url, cook_time_min, difficulty, match_score? }`

### GET /api/recipes/featured
**Auth:** None  
**Response:** `{ recipes: RecipeSummary[] }` — 8 editor-curated recipes updated weekly

### GET /api/recipes/pantry-match
**Auth:** JWT  
**Query params:** `threshold` (integer 0–100, default 70)  
**Response:** `{ recipes: Array<RecipeSummary & { match_score: number, missing_ingredients: string[] }> }` — sorted by match_score DESC, top 20

### GET /api/recipes/:id
**Auth:** JWT-optional  
**Response:** `{ id, title, description, thumbnail_url, youtube_video_id, cook_time_min, prep_time_min, servings, difficulty, tags: string[], ingredients: IngredientLine[], steps: StepLine[], match_score?, cook_log_count, bookmark_count, created_at }`  
**IngredientLine:** `{ id, name, quantity, unit, is_optional }`  
**StepLine:** `{ order, text, youtube_timestamp_sec? }`

---

## Ingredients

### GET /api/ingredients
**Auth:** None  
**Query params:** `q` (string, min 1 char)  
**Response:** `{ ingredients: Array<{ id, name, category }> }` — max 20 results for autocomplete

---

## Pantry

### GET /api/pantry
**Auth:** JWT  
**Response:** `{ items: PantryItem[] }`  
**PantryItem:** `{ id, ingredient_id, ingredient_name, quantity, unit, added_at }`

### POST /api/pantry
**Auth:** JWT  
**Request:** `{ ingredient_id: string, quantity?: number, unit?: string }`  
**Response:** `{ item: PantryItem }`

### PUT /api/pantry/:id
**Auth:** JWT  
**Request:** `{ quantity?: number, unit?: string }`  
**Response:** `{ item: PantryItem }`

### DELETE /api/pantry/:id
**Auth:** JWT  
**Response:** `{ success: true }`

### DELETE /api/pantry
**Auth:** JWT  
**Request:** (empty) — clears entire pantry  
**Response:** `{ deleted_count: number }`

---

## Shopping Lists

### GET /api/shopping-lists
**Auth:** JWT  
**Response:** `{ lists: Array<{ id, title, item_count, created_at, updated_at }> }`

### POST /api/shopping-lists
**Auth:** JWT  
**Request:** `{ title?: string }`  
**Response:** `{ list: ShoppingList }`  
**ShoppingList:** `{ id, title, items: ShoppingListItem[], created_at }`  
**ShoppingListItem:** `{ id, ingredient_name, quantity, unit, aisle_category, checked, added_manually }`

### GET /api/shopping-lists/:id
**Auth:** JWT  
**Response:** `{ list: ShoppingList }`

### POST /api/shopping-lists/:id/recipes
**Auth:** JWT  
**Request:** `{ recipe_id: string, servings?: number }` — merges recipe ingredients into list (dedup + sum)  
**Response:** `{ list: ShoppingList, added_count: number, merged_count: number }`

### PUT /api/shopping-lists/:id/items/:itemId
**Auth:** JWT  
**Request:** `{ checked?: boolean, quantity?: number, unit?: string }`  
**Response:** `{ item: ShoppingListItem }`

### DELETE /api/shopping-lists/:id
**Auth:** JWT  
**Response:** `{ success: true }`

---

## Meal Plans

### GET /api/meal-plans
**Auth:** JWT  
**Query params:** `week` (ISO date string of Monday, e.g. 2026-09-21)  
**Response:** `{ slots: MealPlanSlot[] }`  
**MealPlanSlot:** `{ date, slot_type: breakfast|lunch|dinner, recipe: RecipeSummary }`

### PUT /api/meal-plans/:date
**Auth:** JWT  
**Request:** `{ slot_type: "breakfast" | "lunch" | "dinner", recipe_id: string }`  
**Response:** `{ slot: MealPlanSlot }`

### DELETE /api/meal-plans/:date
**Auth:** JWT  
**Request:** `{ slot_type: "breakfast" | "lunch" | "dinner" }`  
**Response:** `{ success: true }`

### GET /api/meal-plans/shopping-list
**Auth:** JWT  
**Query params:** `week` (ISO date string of Monday)  
**Response:** `{ list: ShoppingList }` — new shopping list aggregating all meal plan recipes for the week

---

## Cook Logs

### GET /api/cook-logs
**Auth:** JWT  
**Response:** `{ logs: CookLog[] }` — current user's logs, newest first  
**CookLog:** `{ id, recipe_id, recipe_title, recipe_thumbnail, rating, note, photos: string[], cooked_at, reaction_counts: { [emoji]: number } }`

### POST /api/cook-logs
**Auth:** JWT  
**Request:** multipart/form-data — `recipe_id: string, rating: 1–5, note?: string (max 140 chars), photos?: File[]`  
**Response:** `{ log: CookLog }`  
**Notes:** Photos uploaded to Azure Blob, resized to max 1200px WebP via sharp before storage.

### GET /api/recipes/:id/cook-logs
**Auth:** JWT-optional  
**Query params:** `cursor` (pagination cursor), `limit` (default 20)  
**Response:** `{ logs: CookLog[], next_cursor?: string }`

### DELETE /api/cook-logs/:id
**Auth:** JWT  
**Response:** `{ success: true }` — only log owner may delete

---

## Social

### GET /api/feed
**Auth:** JWT  
**Query params:** `cursor` (pagination cursor)  
**Response:** `{ items: FeedItem[], next_cursor?: string }`  
**FeedItem:** `{ cook_log_id, user: { id, display_name, avatar_url }, recipe: RecipeSummary, rating, note, photos: string[], cooked_at, reaction_counts, user_reaction? }`

### POST /api/users/:id/follow
**Auth:** JWT  
**Response:** `{ success: true }`

### DELETE /api/users/:id/follow
**Auth:** JWT  
**Response:** `{ success: true }`

### POST /api/cook-logs/:id/react
**Auth:** JWT  
**Request:** `{ emoji: "🍳" | "❤️" | "😋" | "👏" | "🔥" }`  
**Response:** `{ reaction_counts: { [emoji]: number }, user_reaction: string }`

### DELETE /api/cook-logs/:id/react
**Auth:** JWT  
**Response:** `{ reaction_counts: { [emoji]: number }, user_reaction: null }`

---

## Users

### GET /api/users/:id
**Auth:** JWT-optional  
**Response:** `{ id, display_name, avatar_url, cooking_streak, cook_log_count, follower_count, following_count, is_following? }`

### PUT /api/users/me
**Auth:** JWT  
**Request:** `{ display_name?: string, avatar_url?: string }`  
**Response:** `{ user: { id, display_name, avatar_url, email } }`

### GET /api/users/me/bookmarks
**Auth:** JWT  
**Response:** `{ recipes: RecipeSummary[] }`

### POST /api/recipes/:id/bookmark
**Auth:** JWT  
**Response:** `{ success: true }`

### DELETE /api/recipes/:id/bookmark
**Auth:** JWT  
**Response:** `{ success: true }`

---

## Health

### GET /api/health
**Auth:** None  
**Response:** `{ status: "ok", db: "ok" | "error", uptime_seconds: number, version: string }`
