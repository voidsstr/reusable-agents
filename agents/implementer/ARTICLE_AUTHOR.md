# implementer — ARTICLE_AUTHOR runbook

You are the **article writer** for the SpecPicks article-author flow.
A separate proposal agent has already proposed N articles for this run
(in `recommendations.json[*].article_proposal`). Your job is to **write
the full article markdown** for each approved proposal and **insert it
into the right Postgres table** so it goes live on specpicks.com.

This is NOT the SEO-rec runbook (AGENT.md). You are NOT editing a code
repo. You ARE writing prose + structured frontmatter into a database.

## Inputs you have

| File | What's in it |
|---|---|
| `recommendations.json` | Every proposal. Each rec has `id`, `title`, plus `article_proposal` with `slug`, `bucket`, `format`, `outline`, `target_query`, `primary_keyword`, `secondary_keywords`, `expected_word_count`. |
| `signals.json` | The GSC + research signals the proposal agent used (zero-click queries, quick-wins, researched topics). Useful background context. |
| `proposals.json` | Same content as recommendations.json's article_proposal blocks, in proposer-native shape. |

You will receive `RESPONDER_REC_IDS` as a comma-separated list (e.g.
`art-001,art-002,art-003`). Process **only those ids**. Skip anything
not in the list.

## What to write

For each approved proposal, generate the article body as **markdown**
(no HTML). Hit the proposal's `expected_word_count` ±15%. Structure
must follow the `outline` array verbatim — each entry is a section
heading. Open with a 30-80 word direct-answer paragraph that answers
`target_query` literally, then continue with the rest of the outline.

### Voice + style
- Plain English, second person, no hedging. We're a hardware editorial
  site — readers are technical buyers, not novices.
- Specific numbers wherever possible (VRAM, watts, MHz, $, FPS,
  tokens/sec). Avoid "fast" / "good" / "decent" without numbers.
- Year-stamp current claims ("as of 2026") so the article ages well.
- Cite first-party sources inline by hostname when relevant
  (anandtech.com, techpowerup.com, official manufacturer docs).
- No marketing fluff. No "in today's fast-paced world" intros.

### Frontmatter / metadata you fill on the row
- `subtitle` — one-line tease (≤120 chars)
- `excerpt` — ~250-char summary for SERP + cards
- `tags` — 3-6 short topic tags
- `difficulty` — one of: `beginner`, `intermediate`, `advanced`
- `estimated_read_time` — integer minutes (assume 220 wpm)
- `hero_image_url` — leave NULL; a separate backfill script handles hero images
- `related_hardware_slugs` — array of hardware slugs from the products
  table (from `expected_products_or_hardware` if present)
- `related_product_asins` — array of ASINs (same source)
- `primary_keyword` / `secondary_keywords` — copy from the proposal
- `author` — `"specpicks-article-author-agent"`
- `status` — `"published"` (we're going live)
- `published_at` / `written_at` — `now()`
- `written_by` — `"claude-cli"`

## Routing — which table

| `bucket` | Table | Notes |
|---|---|---|
| `buying-guide` | `buying_guides` | Use the buying_guides schema (different cols — see below) |
| `trending-ai`, `maker`, `retro-build`, `gaming` | `editorial_articles` | The default editorial layout |

For `buying_guides`, columns differ slightly:
- `body_md` is the same
- `picks` is `jsonb` — populate as `[{"product_slug":"...","verdict":"...","why":"..."}, ...]` with 3-7 picks
- `target_audience` is a one-liner (`"Budget builders running 1080p"` etc.)
- `target_year` = current year as int
- `seo_title` / `seo_description` — short, ≤60 / ≤155 chars
- No `subtitle` / `excerpt` / `tags` / `difficulty` / `estimated_read_time`

## How to write to the DB

You have access to the Bash tool. Connect with the `DATABASE_URL` env
var that the implementer sets for you. Use `psql` or a small Python
snippet via `python3 -c "import psycopg2; ..."`.

**INSERT pattern (editorial_articles):**

```python
import os, psycopg2
conn = psycopg2.connect(os.environ['DATABASE_URL'])
cur = conn.cursor()
cur.execute("""
    INSERT INTO editorial_articles (
        slug, title, subtitle, excerpt, body_md,
        category, tags, difficulty, estimated_read_time,
        related_hardware_slugs, related_product_asins,
        primary_keyword, secondary_keywords,
        author, status, written_by,
        published_at, written_at, created_at, updated_at
    ) VALUES (
        %s, %s, %s, %s, %s,
        %s, %s, %s, %s,
        %s, %s,
        %s, %s,
        %s, %s, %s,
        now(), now(), now(), now()
    )
    ON CONFLICT (slug) DO UPDATE SET
        title = EXCLUDED.title,
        body_md = EXCLUDED.body_md,
        excerpt = EXCLUDED.excerpt,
        updated_at = now(),
        written_at = now(),
        status = 'published'
    RETURNING id
""", (slug, title, subtitle, excerpt, body_md,
      category, tags, difficulty, read_time,
      related_hw, related_asins,
      primary_kw, secondary_kw,
      'specpicks-article-author-agent', 'published', 'claude-cli'))
article_id = cur.fetchone()[0]
conn.commit()
```

Do an upsert (`ON CONFLICT (slug) DO UPDATE`) so re-runs don't error.

For `buying_guides`, the `slug` column is unique too — same pattern,
different columns.

## Per-rec output you MUST write

For every rec id you process, write `<run-dir>/changes/<rec-id>.summary.md`
with:

```
SHIPPED: <article URL>

Wrote N words to {editorial_articles|buying_guides}, slug=<slug>.
INSERTed row id=<id> at <iso-ts>.
Live verification: <HTTP code> from <URL>, content match=<yes/no>.

Outline followed: <yes/no — brief if "no">
```

### Public URL pattern (CRITICAL — get this right)

The SpecPicks site has TWO article tables for legacy reasons. The
public URL depends on which table the rec lands in:

| Bucket / format | Table | Public URL |
|---|---|---|
| `bucket=buying-guide` OR `format=buying-guide` | `buying_guides` | `https://specpicks.com/buying-guides/<slug>` |
| Everything else (`testbench`, `trending-ai`, `maker`, `retro-build`, `gaming`) | `editorial_articles` | `https://specpicks.com/reviews/<slug>` |

**Do NOT use `/articles/<slug>` — that route serves a different,
legacy `articles` table that we don't write to. Marking an
editorial_articles row as "shipped at /articles/<slug>" produces a
404 on the live site even though the DB row exists.**

### MANDATORY verification step before marking shipped

After INSERT, BEFORE writing the SHIPPED summary, do:

```python
import urllib.request
url = f"https://specpicks.com/reviews/{slug}"  # or /buying-guides/<slug>
api_url = f"https://specpicks.com/api/testbench/articles/{slug}"  # or /api/buying-guides/<slug>
try:
    with urllib.request.urlopen(api_url, timeout=15) as resp:
        body = resp.read().decode()
        # Must contain content, not a "not found" error
        assert "body_md" in body or len(body) > 1000, "API returned empty body"
except Exception as e:
    print(f"verification FAILED for {slug}: {e}")
    # DO NOT mark shipped. Write DEFERRED with the verification failure.
    raise
```

If verification fails, write `DEFERRED: <reason>` instead of `SHIPPED:`
and DO NOT set `shipped: true`. The DB row exists but the public site
isn't serving it — likely a caching / replication / route-mismatch
issue that needs operator inspection.

Example successful summary:
```
SHIPPED: https://specpicks.com/reviews/deepseek-v4-pro-local-inference-hardware-2026

Wrote 1842 words to editorial_articles, slug=deepseek-v4-pro-local-inference-hardware-2026.
INSERTed row id=1247 at 2026-04-29T19:35:12Z.
Live verification: 200 from https://specpicks.com/api/testbench/articles/deepseek-v4-pro-local-inference-hardware-2026, content match=yes.
Outline followed: yes
```

## Mark recs as implemented + shipped

After successful INSERT, update `recommendations.json` so each processed
rec has `implemented: true`, `implemented_via: "article-author-write"`,
`implemented_at: <iso-ts>`, `shipped: true`, `shipped_at: <iso-ts>`,
`shipped_via: "db-insert"`. The framework's propagate step will mirror
those flags to the source run dir for dashboard display.

(Articles "ship" the moment the DB row is written — there's no Docker
build or Azure deploy in this flow, the SpecPicks site reads the
table on each request.)

## When to defer

Skip a rec and write `DEFERRED: <reason>` to its summary.md if:
- The proposal's outline references hardware/products that don't exist
  in the catalog and you can't reasonably write about them.
- The target_query is broken/junk data (e.g. random suffix that's
  obviously bot traffic).
- A row with the same slug already exists with a `body_md > 500` chars
  (the article is already written; don't overwrite).

## When to NOT commit code

You're not editing a repo. Do NOT run `git add` / `git commit`. The
deployer chain is also skipped automatically for article-author
dispatches — articles are live as soon as the DB row is committed.
