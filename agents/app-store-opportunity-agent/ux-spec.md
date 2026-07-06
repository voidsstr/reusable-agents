# BulkWise -- UX Specification

## Color Palette

| Token | Hex | Usage |
|---|---|---|
| brand | #1B4332 | Deep forest green -- primary brand, headers, primary buttons |
| accent | #F59E0B | Amber/gold -- savings, highlights, FAB, deal badges |
| surface | #F9FAFB | App background |
| surface-elevated | #FFFFFF | Cards, sheets, list rows |
| text-primary | #111827 | Headlines and body text |
| text-secondary | #6B7280 | Secondary/meta text, timestamps, captions |
| border | #E5E7EB | Dividers, card borders, input outlines |
| success | #10B981 | In-stock, confirmed, positive savings |
| error | #EF4444 | Out-of-stock, expired coupons, destructive actions |

### Dark Mode Overrides

| Token | Light | Dark |
|---|---|---|
| brand | #1B4332 | #2D6A4F |
| accent | #F59E0B | #FBBF24 |
| surface | #F9FAFB | #0B0F0D |
| surface-elevated | #FFFFFF | #14201A |
| text-primary | #111827 | #F3F4F6 |
| text-secondary | #6B7280 | #9CA3AF |
| border | #E5E7EB | #243029 |
| success | #10B981 | #34D399 |
| error | #EF4444 | #F87171 |

## Type Scale

Font family: **Inter**, with **SF Pro** (iOS) and **Roboto** (Android) fallbacks.

| Level | Size / Line height | Weight | Usage |
|---|---|---|---|
| display | 32 / 40 px | 600 | Onboarding hero, savings total |
| heading | 24 / 32 px | 600 | Screen titles |
| title | 20 / 28 px | 500 | Card titles, section headers |
| body | 16 / 24 px | 400 | Default body copy |
| label | 14 / 20 px | 500 | Buttons, tabs, list meta |
| caption | 12 / 16 px | 400 | Timestamps, unit-price, helper text |

**Dynamic type:** all text scales with OS font-size settings (iOS Dynamic Type / Android font scale). Layouts use flexible containers and never truncate critical numbers (price, unit-price, savings); minimum scale honored up to the OS accessibility-large setting.

## Key Screen Specs

### 1. Onboarding (3 steps, < 30s, no email wall)

- **Layout:** Full-screen, one step per screen, progress dots at top, large primary CTA at bottom, "Skip" available; **no email/password gate** -- account is created later via one-tap Google when needed.
- **Components:**
  - Step 1 -- Household setup: household name + member-count chips (1-5).
  - Step 2 -- Warehouse selection: location permission prompt then nearest-warehouse list with distance; tap to select home warehouse.
  - Step 3 -- First deals value: shows 3 live coupon-book deals for the chosen warehouse to demonstrate immediate value.
- **Primary Action:** "Continue" (steps 1-2), "Start Saving" (step 3).
- **Micro-copy:** Step 1: "Who's shopping with you?" Step 2: "Pick your home warehouse." Step 3: "Here's what's on sale right now."

### 2. Home Tab

- **Layout:** Vertical scroll; savings card pinned at top; horizontally scrolling rails below; FAB bottom-right.
- **Components:**
  - **Savings card** (amber accent): running accumulated savings + breakeven progress bar.
  - **Coupon rail:** horizontal scroll of current coupon-book deal cards.
  - **Gas card** (compact): home warehouse gas price + savings vs nearby + "confirmed X min ago".
  - **Community stock alerts:** list of recent in/out-of-stock reports for watched items.
  - **Lists preview:** the household's active list with item count + check-off progress.
- **Primary Action:** FAB -> "Add to list" (quick-add sheet).
- **Micro-copy:** Savings card: "You've saved $214 this year -- 71% to breakeven." Gas: "$0.34/gal cheaper than nearby."

### 3. Shopping List

- **Layout:** Filter tabs at top (All / To Buy / In Cart / Coupons); scrollable list; FAB add; bottom sheet for item detail.
- **Components:**
  - **List item anatomy:** leading checkbox -> product thumbnail -> name + quantity -> unit-price caption ($/oz) -> trailing coupon chip (amber) when a coupon applies.
  - **Empty state:** illustration + "Your list is empty. Add your first item or run the AI optimizer."
  - **Bottom-sheet detail:** product image, price + unit-price, price-history sparkline, coupon info, quantity stepper, delete.
- **Primary Action:** tap checkbox to mark in-cart (haptic + strike-through).
- **Micro-copy:** Coupon chip: "Save $4.50". Unit-price caption: "$0.21/oz".

### 4. Deals / Coupon Book

- **Layout:** 2-column grid of deal cards; sticky filter bar at top; tap card -> detail view.
- **Components:**
  - **Deal card anatomy:** product image, title, sale price + struck original, savings badge (amber), expiry date, "ends in N days".
  - **Filter bar:** category chips + "Saved" + "Expiring soon".
  - **Detail view:** large image, price + unit-price, price-history sparkline, expiry, "Confirm this deal" community button, "Add to list".
- **Primary Action:** "Add to list" (detail) / tap card (grid).
- **Micro-copy:** Savings badge: "-$5.00". Confirm button: "Saw this in store? Confirm".

### 5. Settings

- **Layout:** Grouped list, 5 sections with section headers.
- **Components / sections:**
  - **Account:** Google sign-in status, membership number, Costco.com connection, sign out.
  - **Household:** members list (up to 5), invite code, leave/manage household.
  - **Warehouse:** home warehouse, add/switch warehouse, gas preferences.
  - **Notifications:** coupon drops, expiring coupons, stock alerts, gas changes (per-toggle).
  - **Privacy:** data export, delete account, community contribution visibility, ToS/Privacy links.
- **Primary Action:** per-row navigation / toggles.
- **Micro-copy:** Household: "Invite up to 5 people to share lists." Privacy: "Your reports are anonymous to other members."

## Micro-animations

1. **Check-off:** checkbox fills with brand green + light haptic + 150ms strike-through slide.
2. **Savings tick:** savings total counts up (number roll) when new savings logged.
3. **FAB press:** scale-down 0.94 on press, springs back on release.
4. **Coupon confirm:** amber pulse + checkmark morph when a deal is community-confirmed.
5. **Pull-to-refresh:** branded spinner that fades into the savings card on release.

## Accessibility Requirements

- **Hit targets:** minimum 44pt x 44pt for all interactive elements.
- **Contrast:** all text/background pairs meet WCAG AA (4.5:1 body, 3:1 large text).
- **Screen reader:** every control has a descriptive `accessibilityLabel`; list items announce name + price + unit-price + state.
- **Modals/sheets:** set `accessibilityViewIsModal` so screen readers trap focus inside open sheets.
- **Color independence:** color is never the sole differentiator -- in/out-of-stock, expired, and confirmed states also carry an icon and/or text label.
