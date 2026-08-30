# Category Integrity Agent

Keeps products out of categories they do not belong in, so a buying guide stops
listing slow cookers.

## Why it exists

2026-08-30, `/buying-guide/games-cartridges` on specpicks listed a Crock-Pot
slow cooker, Wondercide flea spray, Sony headphones, Energizer batteries, SanDisk
USB drives, an HP 952XL **ink cartridge** and a TOSY flying ring. Nothing checked.

The upstream cause is measurable: **81,345 of 146,073 active products (55.7%)**
carry `category_confidence IS NULL` — filed by a path that never ran the scoring
categoriser at all.

## It answers a narrow question on purpose

Two questions were measured against the live catalogue:

| question | flagged | precision |
|---|---|---|
| "is this the *right* category?" | 17,934 | ~50% on a hand-checked sample |
| **"is this product in our domain at all?"** | **2,617** | unambiguous on inspection |

The first is arguable — a CD-ripper filed under `retro-software` is a judgement
call, and a checker that argues is one nobody trusts. The second is not: chair-leg
protectors, potato-sack race bags, beard trimmers, eyebrow pencils and beef
protein powder are not PC hardware under any reading.

So this agent acts **only** on the second. `scripts/verify-categorizations.ts`
answers the first and is deliberately report-only — measured at ~21% precision,
it would de-categorise roughly 46,000 correct products.

## What it does, and what it refuses to do

- **De-categorises** (`category_id = NULL`) products that are off-domain **and**
  were never scored. That removes them from buying guides immediately, and the
  site's own `assign-categories` can place them properly later.
- **Never moves** a product to a guessed category. A wrong home is worse than no
  home.
- **Never touches** `category_validated = true`. That is somebody's decision and
  a keyword test does not get to overrule it.
- **Refuses to run** on an empty vocabulary rather than judge blind.

## Both halves of the test are required

A title is off-domain when it contains foreign vocabulary **and** no domain
vocabulary. Either half alone misfires: foreign-only removes a "gaming chair"
(furniture words plus `gaming`), and absent-domain-only removes anything titled
with just a model number.

## What goes to the implementer

De-categorising is treating the symptom. When the unscored share exceeds
`CATEGORY_UNSCORED_ALERT` (default 25% — it is currently **54%**), the agent
writes a recommendation for the implementer to fix the *writer*: every path that
sets `category_id` without a confidence should run the same exported scoring
rules, or leave the category NULL rather than guess.

Data repair is deterministic and safe to automate. Changing the categoriser is
not, which is why that half goes out as a recommendation instead.

| env | default | meaning |
|---|---|---|
| `CATEGORY_INTEGRITY_CONFIG` | — | required; the site's `site.yaml` |
| `CATEGORY_MAX_FIX_PER_RUN` | `500` | de-categorisations per tick |
| `CATEGORY_UNSCORED_ALERT` | `0.25` | unscored share that raises a code-fix rec |
