# BuzzKitchen — UX Specification

## Colour Palette

| Token | Hex | Usage |
|---|---|---|
| `primary` | `#E8452C` | Primary CTAs, active tab indicator, streak flame |
| `secondary` | `#FF9500` | Match-score pill, star ratings, premium badge |
| `success` | `#27AE60` | Pantry match confirmation, step complete check |
| `surface-light` | `#FAFAF8` | Light mode background, card surfaces |
| `surface-dark` | `#1A1A1A` | Dark mode background |
| `text-primary` | `#1C1410` | Primary body text (light mode) |
| `text-secondary` | `#6B5B4F` | Secondary labels, captions, placeholder text |

**WCAG Contrast Notes:** `text-primary #1C1410` on `surface-light #FAFAF8` = 16.9:1 (AAA). `primary #E8452C` on `surface-light` = 4.7:1 (AA for large text and UI components). All interactive labels use `text-primary`, never `text-secondary` alone.

**Dark Mode Mapping:** `surface-dark #1A1A1A` replaces `surface-light`. `text-primary` lightens to `#F5F0EC`. `text-secondary` lightens to `#A89A90`. `primary` and `secondary` remain unchanged (sufficient contrast on dark surface). Cards use `#242424` on dark.

## Typography Scale

| Token | Size | Weight | Usage |
|---|---|---|---|
| `display` | 28sp | 700 | Hands-free step text, hero numbers |
| `heading` | 22sp | 600 | Screen titles, recipe card title |
| `subheading` | 17sp | 600 | Section headers, modal titles |
| `body` | 15sp | 400 | Recipe steps, ingredient rows, feed captions |
| `caption` | 12sp | 400 | Timestamps, meta labels, aisle category |
| `label` | 11sp | 500 | Pill labels, tab bar labels, badge text |

**Font:** Noto Sans JP (Google Fonts CDN on web; bundled subset on mobile via `expo-font`). Fallbacks: `Hiragino Sans` (iOS), `Noto Sans CJK JP` (Android), `sans-serif`. **sp units note:** All mobile sizes are in `sp` (scale-independent pixels, equivalent to React Native `fontSize` points) so they respect the system accessibility font-scale setting.

## Screen Specifications

---

### 1. Onboarding

**Layout:** Full-bleed `HeroCycler` (auto-scrolling carousel of 3 hero images: sizzling gyudon, vibrant ramen, plated karaage) occupies 60% of screen height. Bottom sheet `OnboardingSheet` slides up with headline, subheadline, and dual CTA buttons. No carousel dots — swipe affordance only.

**Components:** `HeroCycler`, `OnboardingSheet`, `PrimaryButton`, `SecondaryButton`, `TermsCaption`.

**Primary Action:** Tap "今すぐ始める" — enters guest mode, navigates to Home with demo pantry item pre-loaded. No email or permission required.

**Micro-copy:**
- Headline: 今夜、何作る？
- Subheadline: 冷蔵庫にあるものから、今すぐ作れるレシピを見つけよう。
- Primary CTA: 今すぐ始める
- Secondary CTA: アカウントをお持ちの方はこちら
- Terms caption: 続行することで利用規約に同意します

---

### 2. Home

**Layout:** Sticky header with BuzzKitchen wordmark and search icon. Below header: conditional `PantryMatchBanner` (visible only when ≥1 pantry item exists; shows top recipe name, match score pill in `secondary #FF9500`, and a 全部見る link). Then a horizontal `CookLogStrip` showing the last 4 cook logs from followed users as circular thumbnails with a streak badge (flame icon in `primary #E8452C` plus count) on the user's avatar. Below: 2-column `RecipeCard` grid with infinite scroll.

**Components:** `StickyHeader`, `PantryMatchBanner`, `CookLogStrip`, `StreakBadge`, `RecipeCard`, `MatchScorePill`, `FabSearchButton`.

**Primary Action:** Tap `RecipeCard` → navigate to Recipe Detail.

**Micro-copy:**
- PantryMatchBanner: 今すぐ作れそう！%d品マッチ
- CookLogStrip header: みんなの料理
- Empty state: 食材を登録するとマッチするレシピが見つかります
- Streak badge tooltip: %d日連続調理中！

---

### 3. Recipe Detail

**Layout:** Full-width `VideoPlayer` (react-native-youtube-iframe, 16:9 aspect, tap to enter fullscreen). Below: recipe meta row (cook time, servings, difficulty, match score pill). `CookingModeToggle` button (full-width secondary style). `IngredientList` with quantity, unit, and ingredient name; each row has a checkbox. `StepList` with numbered cards; each card shows step text and a timestamp badge (e.g. 2:34) that deep-links into the YouTube player at that second. Below steps: `MadeItCTA` button (primary red). `CookLogWall` section titled みんなの作り方 followed by a 3-column photo grid of cook logs.

**Components:** `VideoPlayer`, `CookingModeToggle`, `IngredientList`, `CheckableIngredientRow`, `StepList`, `StepCard`, `TimestampBadge`, `AddToShoppingListButton`, `MadeItCTA`, `CookLogWall`, `CookLogThumbnail`.

**Primary Action:** Tap 作った！ → opens CookLog half-sheet modal.

**Micro-copy:**
- CookingModeToggle: ハンズフリーで調理する
- AddToShopping: 買い物リストに追加
- MadeItCTA: 作った！
- CookLogWall header: みんなの作り方 (%d件)

---

### 4. Pantry

**Layout:** Top bar with back chevron and title 食材管理. Below: `PantrySearchInput` with autocomplete dropdown (ingredient name matches from `GET /api/ingredients?q=`). Below input: list of `IngredientRow` items (ingredient name, quantity, unit, delete swipe action). Sticky bottom bar: `MatchCountPill` showing 今すぐ作れるレシピ: %d品 with a 見る button navigating to filtered recipe list.

**Components:** `PantrySearchInput`, `AutocompleteDropdown`, `IngredientRow`, `SwipeToDeleteRow`, `MatchCountPill`, `AddIngredientFAB`.

**Primary Action:** Type ingredient name → select from autocomplete → ingredient added to pantry list.

**Micro-copy:**
- Search placeholder: 食材を追加…（例: 卵、醤油）
- Empty state: 食材を追加して、今夜作れるレシピを見つけましょう
- MatchCountPill: 今すぐ作れるレシピ: %d品
- Delete confirmation: 削除しますか？

---

### 5. Shopping List

**Layout:** List title editable inline at top. `AisleSection` groups: 肉・魚, 野菜・果物, 乳製品・卵, 調味料, 米・麺・パン, その他. Each section is collapsible. Items show checkbox, quantity, and ingredient name. Checked items move to a bought section below a divider. Sticky bottom bar with `ShareButton` (LINE icon plus LINEでシェア) and `AddManually` inline text input.

**Components:** `AisleSection`, `ShoppingListItem`, `CheckboxRow`, `ShareButton`, `AddManuallyInput`, `StickyActionBar`, `ListTitleInput`.

**Primary Action:** Tap LINEでシェア → generates LINE URL scheme with plain-text formatted list.

**Micro-copy:**
- ShareButton: LINEでシェア
- AddManually placeholder: アイテムを追加…
- Section collapse: %d品 count badge
- Empty state: レシピから食材を追加しましょう
- LINE share format: 🛒 買い物リスト\n\n【肉・魚】\n・鶏もも肉 300g\n…

---

### 6. Settings

**Layout:** `ProfileRow` at top (avatar, display name, email; tap to edit profile). Section headers are `label`-size uppercase. Rows are full-width tap targets with right-chevron or toggle.

**Rows:**
- `ToggleRow` ダークモード — toggles dark mode
- `ToggleRow` 食材の単位 — g/ml (metric) or oz/fl oz (imperial)
- `ToggleRow` プッシュ通知 — enable/disable all notifications
- `LanguageRow` — current: 日本語 / English; opens language picker sheet
- `SignOutRow` — tap opens confirmation dialog then clears JWT

**Components:** `ProfileRow`, `ToggleRow`, `LanguageRow`, `SignOutRow`, `SectionHeader`, `DangerRow`.

**Primary Action:** Edit profile → `PUT /api/users/me`.

**Micro-copy:**
- ProfileRow subtitle: プレミアム会員 (premium) or 無料プラン (free)
- SignOut: ログアウト
- SignOut confirm: 本当にログアウトしますか？
- Language picker title: 言語 / Language
