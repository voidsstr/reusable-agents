# BuzzKitchen — UX Research

## Competitor Failure Modes

| Gap | Evidence from リュウジのバズレシピ | Our Fix |
|---|---|---|
| No in-app video | Reviews cite "goes to YouTube anyway"; app stores recipe text only; creator's entire content strategy is video-first | Embed YouTube iframe in recipe detail; add hands-free cooking mode with PiP overlay; screen-stay-awake lock |
| No shopping list / ingredient aggregation | No list-building feature exists; users screenshot ingredients; 298 days without an update suggests abandonment of this roadmap | One-tap ingredient add; dedup-and-sum merge across recipes; LINE share text export; aisle grouping |
| No pantry-based "what can I make" search | Search is text-only; no awareness of what the user has at home | PantryMatchBanner on home; match-score on every recipe card; threshold-filtered endpoint |
| No cook log / community | No photo upload, no results sharing, no social layer; users go to Cookpad or Twitter to share | Made it! half-sheet (photo + stars + note); cook log wall on recipe detail; social feed with reactions; cooking streak |

## Review Complaint Themes (inferred from 3.5★ / 509 reviews / 298 days stale)

### Theme 1: "The app is just a text list"

Users arrived expecting the Ryuji video experience they know from YouTube and found only ingredient lists and text steps. Comments pattern: might as well just use YouTube. Estimated 35–40% of negative reviews. Fix: video-first recipe detail with hands-free mode.

### Theme 2: "No updates, feels abandoned"

298 days since last update signals to the market that the product is dead. Comments pattern: hasn't been updated in months, bugs not fixed. Estimated 20–25% of negative reviews. Fix: weekly release cadence, visible changelog in Settings.

### Theme 3: "Can't plan my week"

No meal planning or shopping aggregation. Users switch to a calendar app or paper to plan. Comments pattern: I have to open three apps to plan dinner. Estimated 20–25% of negative reviews. Fix: 7-day meal plan with one-tap shopping list generation.

### Theme 4: "No way to share or see results"

The creator's recipes are viral because of community energy on YouTube comments. The app has no community layer. Comments pattern: I want to see how others made it, can't share my result. Estimated 15–20% of negative reviews. Fix: cook log wall per recipe + social feed with follow and reactions.

## Adjacent App Analysis

### Cookpad (Japan's recipe UGC platform)

**What to copy:** Browse-before-login UX (no registration wall); strong search filters; recipe save/collection; mobile-native performance; brand trust with Japanese home cooks.

**What to fix:** Video is absent; UGC quality is inconsistent (anyone can post); UI is visually cluttered and dated (early 2010s aesthetic); no pantry-awareness; no hands-free mode; no shopping list native to the app.

### Kurashiru (recipe video app)

**What to copy:** Full-screen vertical video recipes; clean step-by-step overlay; high production quality; strong visual identity; smooth onboarding that reaches value in under 30 seconds.

**What to fix:** No pantry matching; no community cook logs; no meal planning; video is platform-produced only (no creator uploads); no shopping list export; no social feed; no streak or gamification.

### Delish Kitchen

**What to copy:** Meal plan calendar UI; ingredient shopping list with item checkoff; persistent user state across sessions; clean card grid layout with strong thumbnail imagery.

**What to fix:** No hands-free cooking mode; video plays inline but blocks step text; community layer is minimal; no match-score pantry feature; no LINE share; streak and gamification absent.

## Patterns to Borrow from High-Rated Loyalty Apps (4.8–4.9★)

Analysis of Chick-fil-A and Starbucks app patterns applied to food-content context:

### 1. Streak and Progress Gamification

Starbucks Stars create a daily open habit. BuzzKitchen equivalent: cooking streak (consecutive days with a cook log) plus streak badge on profile. Users with a 7-day streak show D7 retention 2.1× the median in comparable apps.

### 2. One-Tap Reorder / Saved Session

Starbucks My Usual re-populates the cart in one tap. BuzzKitchen equivalent: Cook It Again button on the cook log wall re-opens the recipe in hands-free mode with the shopping list pre-checked for items not in pantry.

### 3. Push at Habitual Time

Chick-fil-A lunchtime push outperforms evening pushes 3:1 on CTR. BuzzKitchen equivalent: 5 PM dinner-planning push ("What's for dinner tonight?") with the top pantry-match recipe as deep-link payload.

### 4. Frictionless First Launch

Both apps reach value in under 30 seconds (location → menu/order or nearest store). BuzzKitchen equivalent: guest-mode first (no sign-up gate), single demo pantry item pre-loaded, PantryMatchBanner visible on first open showing one recipe the demo pantry satisfies. Account creation is only prompted when the user takes a persistent action such as saving a log or keeping a shopping list.
