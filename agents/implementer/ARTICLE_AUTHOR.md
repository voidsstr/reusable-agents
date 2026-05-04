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
(no HTML). Structure must follow the `outline` array verbatim — each
entry is a section heading. Open with a 30-80 word direct-answer
paragraph that answers `target_query` literally, then continue with
the rest of the outline.

### Length floor: 2000 words minimum (~3 pages rendered)

Hit `max(expected_word_count, 2000)`. **Articles under 2000 words MUST
be expanded** — they look thin in the SERP, lose to longer competitors,
and read as filler. The SEO analyzer flags any shipped article whose
`wordCount` JSON-LD value is below the site's `min_words` threshold;
those get auto-queued back for expansion the same way as a fresh rec.
If your draft is short, add:
- A "Real-world numbers" or "Benchmark table" section with concrete
  measurements (FPS, watts, $, tokens/sec, RPS, etc.)
- A "Common pitfalls" or "Gotchas" section with 3-5 specific failure
  modes you've seen for this hardware/topic
- A "When NOT to" section with a clear no-fit case
- Comparison tables (markdown tables; render fine on the site)
- 2-3 worked examples / mini-case-studies

A 2000-word article is ~3 pages at typical reading speed (220 wpm =
~9 min read). The site shows estimated_read_time prominently — readers
expect substantive content for that signal.

### Internal links — ABSOLUTE paths only (MANDATORY)

Every internal link in `body_md` MUST be an absolute path starting with
`/`. The site's SSR markdown renderer used to silently fall back to
relative resolution, so a link like `[Title](classic-beef-stir-fry)`
on `/blog/some-post` resolved to `/blog/classic-beef-stir-fry` (404)
on first crawl. The renderer now rewrites slug-only hrefs to
`/recipes/<slug>`, but you MUST still write the absolute path so the
markdown source is correct in the DB and the analyzer's
`broken-internal-link` detector stays clean.

| Reference | Correct markdown |
|---|---|
| Recipe by slug | `[Classic Beef Stir Fry](/recipes/classic-beef-stir-fry)` |
| Kitchen product by slug | `[Chef's Knife](/k/chefs-knife)` |
| Other blog article | `[Mediterranean Meal Plan](/blog/complete-mediterranean-meal-plan)` |
| Feature page | `[Meal Plan](/meal-plan)` |
| External / outbound citation | `[Serious Eats — Knife Skills](https://www.seriouseats.com/...)` |

**FORBIDDEN**:
- `[Title](classic-beef-stir-fry)` — slug only, no leading slash
- `[Title](recipes/classic-beef-stir-fry)` — relative path, no leading slash
- `[Title](./recipes/classic-beef-stir-fry)` — explicit relative

The reusable SEO analyzer ships a `broken-internal-link` rec type
(reusable-agents/agents/seo-analyzer/analyzer.py) that flags any page
with `body_relative_link_count > 0` — every relative-href occurrence
on a published article is a high-priority rec the implementer would
have to fix on the next run anyway. Save the cycle: write absolute
paths from the start.

### Voice + style
- Plain English, second person, no hedging. We're a hardware editorial
  site — readers are technical buyers, not novices.
- Specific numbers wherever possible (VRAM, watts, MHz, $, FPS,
  tokens/sec). Avoid "fast" / "good" / "decent" without numbers.
- Year-stamp current claims ("as of 2026") so the article ages well.
- Cite first-party sources inline by hostname when relevant
  (anandtech.com, techpowerup.com, official manufacturer docs).
- No marketing fluff. No "in today's fast-paced world" intros.

### SEO-completion checklist — every column must be populated

Every analyzer rule that fires on article pages keys off a column on
the row. Leaving any blank trips an auto-queue rec on the next 3-hour
SEO tick. The proposal carries most of these — COPY them into the
INSERT verbatim; do NOT re-derive.

| Column | Source | Why it matters |
|---|---|---|
| `title` | proposal | analyzer.title-length 30-70 |
| `subtitle` | runbook (you write) | shown in lede + meta fallback |
| `excerpt` | runbook (you write) | meta description 120-165 chars; **NEVER** dupe the title |
| `body_md` | runbook (you write) | thin-content + word-count + internal-link + outbound-citation rules |
| `hero_image_url` | proposal (preferred) → product-link query (fallback) | OG image + JSON-LD image |
| `bucket` / `category` | proposal | analyzer's article-rule selectors |
| `tags` | **proposal** | tag-cluster graph; copy verbatim |
| `difficulty` | **proposal** | JSON-LD audience tier |
| `estimated_read_time` | runbook (derive: words / 220) | meta strip + JSON-LD timeRequired |
| `primary_keyword` / `secondary_keywords` | proposal | JSON-LD keywords + analyzer tracks ranking |
| `related_recipe_slugs` / `related_kitchen_slugs` / `related_hardware_slugs` / `related_product_asins` | proposal | inline featured-product autolink |
| `related_article_slugs` | runbook (pick from sitemap) | drives related-articles rail + cluster graph |
| `faqs` | **proposal — 5 Q/A pairs, ≥40 words each** | drives FAQPage JSON-LD; analyzer.faq-quality-thin |
| `outbound_citations` | **proposal — 3 authoritative URLs** | analyzer.eeat-outbound-citation-count; you MUST also link to each inside body_md |
| `author` | always `"Mike Perry"` (SpecPicks) or `"AislePrompt Team"` (AislePrompt) | E-E-A-T |
| `status` | `"published"` | filter |
| `published_at` / `written_at` | `now()` | dateModified ≥30% |

**Hard constraints, repeated for emphasis:**
- `excerpt` MUST be a hand-written 145-160-char summary, **NOT** the
  title verbatim. The 4-30 mediterranean-meal-plan article had
  `excerpt = title` and lost every SERP comparison vs longer competitors.
- `faqs` MUST contain ≥3 entries (5 preferred), each `answer` ≥40
  words, distinct questions. The proposal carries them; copy verbatim
  unless you need to fact-check a specific claim against the article
  body.
- `outbound_citations` is an array of URLs the body actually links to.
  After you finish writing, audit body_md to confirm each URL appears
  inline as `[Source name](https://...)` markdown — not just listed
  in this column.
- All internal links use absolute paths starting with `/` — the
  "Internal links" section above is non-negotiable.

### Other frontmatter notes
- `hero_image_url` — **MANDATORY: set this BEFORE the INSERT** (see "Picking the hero image" below). Leaving it NULL forces the SSR fallback, which used to pick HDMI cables / PSUs that were marketed "for RTX 5090". Set the right image at write-time so the article never depends on the fallback.
- `related_hardware_slugs` — array of hardware slugs from the products
  table (from `expected_products_or_hardware` if present)
- `related_product_asins` — array of ASINs (same source)
- `primary_keyword` / `secondary_keywords` — copy from the proposal
- `author` — **always `"Mike Perry"`**. The agent is a writing assistant, not the byline — every article on the site is published under Mike Perry's name. Do NOT use the agent id, "SpecPicks Editorial", or any other placeholder.
- `status` — `"published"` (we're going live)
- `published_at` / `written_at` — `now()`
- `written_by` — `"claude-cli"`

## Routing — which table

**ALL article types go to `editorial_articles`.** The public site
serves `editorial_articles` rows at `/reviews/<slug>` via
`/api/testbench/articles/<slug>`. The `buying_guides` table exists
but has NO public API endpoint — writing there produces orphaned
rows that 404 on the live site even with `status='published'`.

| `bucket` / `format` | Table | URL |
|---|---|---|
| Any (testbench, trending-ai, maker, retro-build, gaming, **buying-guide**) | `editorial_articles` | `https://specpicks.com/reviews/<slug>` |

For buying-guide bucket articles: write to `editorial_articles` with
`category='buying-guide'`. Append the picks list to body_md as a
`## Top picks` section so the buying-guide format survives:

```markdown
## Top picks

### #1: NVIDIA RTX 5090
**Verdict:** Best for 32B models with full BF16, $1999, 32GB VRAM

This is the only consumer card with enough VRAM to load BF16 weights of
27B-32B models without offload. Power-hungry (575W TGP) but worth it
for production-grade local inference.

### #2: ...
```

Then INSERT into editorial_articles like any other article — populate
all the standard editorial columns, and set `category='buying-guide'`
so the dashboard / SEO analyzer can distinguish them.

## Picking the hero image (MANDATORY before INSERT)

**First, check the proposal.** If the rec carries
`implementation_outline.hero_image_url` (or `proposal.hero_image_url`)
already populated by the proposer, **use that value verbatim**. The
proposer ran an image search at proposal time using the same SearxNG
allow-list the recipe scraper uses, so the URL is already vetted. Only
run the queries below if the proposal field is empty.

The article's `hero_image_url` must be a real photo of one of the
hardware items the article is about. Run this query against the DB
BEFORE the INSERT and use the returned URL in the `hero_image_url`
column:

```sql
SELECT p.main_image_url
  FROM product_hardware_links l
  JOIN products p ON p.id = l.product_id
  JOIN hardware_specs h ON h.id = l.hardware_id
 WHERE h.slug = ANY(<related_hardware_slugs>)
   AND p.is_active AND p.main_image_url IS NOT NULL
   -- Must actually be that hardware, not an accessory marketed "for X":
   AND lower(p.title) LIKE '%' || lower(h.name) || '%'
   -- Defense-in-depth blacklist — accessories whose title still
   -- contains the GPU name (e.g. "RTX 5090 GPU support brace"):
   AND p.title !~* '\y(cable|cord|adapter|bracket|water.?block|riser|thermal|backplate|heatsink|gpu.?stand|gpu.?holder|brace|sag|comb|mounting|enclosure|hub|extension|dock|capture.?card|rgb.?(strip|fan)|psu|power.?supply|case\y|fan\y|frame|stand\y|holder|protector|sleeve|cooler\y|cooling\s+fan)\y'
 ORDER BY p.review_count DESC NULLS LAST
 LIMIT 1;
```

If that returns nothing (e.g. an article about software like Mistral
3.5 with no hardware product), fall back to a category-default image
(or leave NULL — the SSR layer's DEFAULT_IMAGE picks up). The 4-30
gemma-4 hero-image incident was exactly this: hero_image_url was NULL,
the broken fallback picked an HDMI cable from product_hardware_links
because it was marketed "for RTX 5090". Setting hero_image_url at
write-time avoids the fallback entirely.

### Image must match the SUBJECT, not just the era / category

**A correct hero shows the actual hardware/product the article is
about.** Common-mode failures we've found and rolled back:

- ❌ "ATI Radeon 9700 Pro install guide" used an RTX 9070 XT photo
  (wrong product, just because the model number contained "9070")
- ❌ Pentium III Tualatin guide used a Pentium Pro logo (wrong product)
- ❌ "Best 4K monitor under $700" used an NVIDIA Titan GPU photo
- ❌ Sim-racing wheel buying guide used Hot Wheels Unleashed game art
- ❌ Raspberry Pi LLM article used a Midjourney AI artwork
- ❌ Many articles used `Golden_ratio_logo_design_technique.jpg` from
  Wikipedia commons as a generic "tech-ish" placeholder. Never do this.

If the SQL query above returns no rows, **DO NOT** substitute a
generic Wikipedia image as a placeholder. Acceptable fallbacks
(in order):

1. Search products by article-keyword title match — same query but
   replacing the `h.slug = ANY(...)` predicate with
   `lower(p.title) LIKE '%<primary_keyword>%'`.
2. eBay listing image for any related product (use
   `i.ebayimg.com/.../s-l1600.<ext>` — that token gets the largest
   reliable size).
3. Leave `hero_image_url = NULL`. The SSR layer renders a clean
   no-image card; that's strictly better than a misleading photo.

After you've INSERTed the article, the cron job
`scripts/refresh-article-hero-images.py --all-published` runs and
will only IMPROVE — never replace — a correctly-set image. So get it
right at write-time.

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
      'Mike Perry', 'published', 'claude-cli'))
article_id = cur.fetchone()[0]
conn.commit()
```

Do an upsert (`ON CONFLICT (slug) DO UPDATE`) so re-runs don't error.

For `buying_guides`, the `slug` column is unique too — same pattern,
different columns.

### MANDATORY: queue eBay fetch for any hardware product mentions

After a successful INSERT/UPDATE on `editorial_articles` (or `buying_guides`),
extract any **hardware product mentions** from the article body and queue
an on-demand eBay fetch via the framework handoff primitive. This makes
sure the just-published article has fresh listings rendered inline by the
testbench API on the next eBay tick (instead of waiting up to 30 min for
the rotation seed pool to come around).

For each ASIN, product slug, or hardware spec the article references:

```python
import sys
sys.path.insert(0, "/home/voidsstr/development/reusable-agents")
from framework.core.handoff import send_handoff

for mention in product_mentions:   # the same list you wrote into
                                   # `related_product_asins` /
                                   # `related_hardware_slugs` above
    send_handoff(
        from_agent="seo-implementer",
        to_agent="ebay-product-sync-agent",
        work_type="ebay_fetch_for_product",
        rec_id=rec_id,
        rec={
            "product_query":    mention["query_text"],   # eBay search query
                                                          # (e.g. "GeForce 4 Ti 4600")
            "product_category": mention.get("category_slug"),  # e.g. "retro-gpus"
            "asin":             mention.get("asin", ""),
            "hardware_slug":    mention.get("hardware_slug", ""),
            "source_article_slug": slug,
        },
        source_run_ts=os.environ.get("RESPONDER_RUN_TS", ""),
        source_agent="article-author",
        rationale=f"article '{slug}' published — fetch fresh eBay listings "
                  f"for inline product cards",
    )
```

If the article has zero hardware mentions, skip this step. The eBay
agent's run loop reads handoffs into `self.inbound_handoffs` and injects
them at the head of its priority seed queue.

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

### Public URL pattern

ALL articles render at `https://specpicks.com/reviews/<slug>`. The
public API is `https://specpicks.com/api/testbench/articles/<slug>`.

Do NOT use:
- `/articles/<slug>` — that's a legacy `articles` table we don't write to
- `/buying-guides/<slug>` — that's category-driven auto-pages, not
  editorial buying-guides

### MANDATORY: write a per-rec verification script

After every successful INSERT (and before writing the SHIPPED summary),
write a verification doc to:

  `<run-dir>/verifications/<rec_id>.json`

Shape:
```json
{
  "rec_id": "art-001",
  "rec_type": "article-author-proposal",
  "generated_at": "<iso8601>",
  "generated_by": "implementer",
  "explanation": "1-2 sentence plain-English description of what's being verified",
  "script_js": "async function verify({ proxyFetch }) { /* ... */ }"
}
```

The dashboard's shipped tab has a "Verify in production" button per rec
that runs `script_js` client-side and shows the result + explanation in
a popup. The script must:

- Be the function literal `async function verify({ proxyFetch })`
- Return `{ ok: boolean, evidence: <any> }`
- Use the provided `proxyFetch(url)` helper (NOT browser `fetch`) — it
  goes through the framework API's allow-listed proxy and bypasses CORS
- Be specific: don't just check 200; check that the SPECIFIC content
  this rec produced is in the page

Example for an article-author rec:

```js
async function verify({ proxyFetch }) {
  const slug = "deepseek-v4-pro-local-inference-hardware-2026";
  const r = await proxyFetch(`https://specpicks.com/api/testbench/articles/${slug}`);
  if (!r.ok) return { ok: false, evidence: { http_status: r.status } };
  const obj = JSON.parse(r.body || "{}");
  const has_body = (obj.body_md || "").length > 1000;
  const has_slug = obj.slug === slug;
  return {
    ok: has_body && has_slug,
    evidence: {
      api_status: r.status,
      body_md_length: (obj.body_md || "").length,
      slug_match: has_slug,
      title: obj.title,
    },
  };
}
```

**Don't write the SHIPPED summary if you can't write a verification
script.** If the rec is hard to verify automatically, set
`script_js: ""` and put the manual verification steps in `explanation`.
Mark the rec implemented but NOT shipped — operator decides.

### MANDATORY verification step before marking shipped

After INSERT, BEFORE writing the SHIPPED summary, hit the API and
confirm the article actually serves. **The verification MUST retry with
backoff** — Azure Container App + Postgres replica lag can take up to
~30s before a freshly-inserted row is served by the public API. A
single immediate fetch will frequently return body_md=0 right after
INSERT even when the row is in the DB and will serve correctly seconds
later. The 4-29 22:45Z dispatch shipped 0/5 articles for exactly this
reason — all 5 were inserted but the immediate verify saw empty bodies
and marked them DEFERRED.

```python
import time, json, urllib.request
api_url = f"https://specpicks.com/api/testbench/articles/{slug}"

def _verify_once():
    req = urllib.request.Request(api_url, headers={"User-Agent": "specpicks-impl/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = resp.read().decode()
        try:
            d = json.loads(body)
        except Exception:
            return False, f"non-JSON response: {body[:200]}"
        article = d.get("article") or d
        body_md = article.get("body_md") or ""
        if len(body_md) > 1000:
            return True, f"body_md={len(body_md)} chars"
        return False, f"body_md={len(body_md)} chars (need >1000)"

ok, detail = False, ""
# Retry up to 6 times: 0s, 3s, 6s, 12s, 20s, 30s — total ~70s
for delay in (0, 3, 6, 12, 20, 30):
    if delay:
        time.sleep(delay)
    try:
        ok, detail = _verify_once()
    except Exception as e:
        ok, detail = False, f"fetch error: {e}"
    if ok:
        break

if not ok:
    print(f"verification FAILED for {slug} after retries: {detail}")
    # Even after 70s of retries, the row isn't serving. Write DEFERRED
    # with the verification failure so a human can investigate.
    raise RuntimeError(f"verify_failed: {detail}")
```

If verification fails after all retries, write `DEFERRED: <reason>`
instead of `SHIPPED:` and DO NOT set `shipped: true`. The DB row exists
but the public site isn't serving it — likely a slug typo, status≠
'published', or a CDN cache that needs to expire.

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
`shipped_via: "db-insert"`, `public_url: "https://specpicks.com/reviews/<slug>"`.
The framework's propagate step will mirror those flags to the source
run dir for dashboard display.

### MANDATORY: write applied-recs.json

You MUST write `<run_dir>/applied-recs.json` listing every rec_id you
successfully inserted. Without this file the implementer's run.sh marks
the dispatch `paused` (because there's no git commit to detect),
emails the user "0 of N applied", and the dashboard shows the recs as
NOT shipped — even though the DB rows are live. The 4-29 22:45Z and
4-30 00:45Z runs both shipped 0/5 in the email despite all 10 articles
being inserted and live, for exactly this reason.

```python
import json, pathlib
applied = pathlib.Path("/path/to/run_dir/applied-recs.json")
applied.write_text(json.dumps({
    "applied_rec_ids": ["art-001", "art-002", "art-003"],  # whatever you actually inserted
    "method": "article-author-db-insert",
    "public_urls": {
        "art-001": "https://specpicks.com/reviews/<slug-1>",
        "art-002": "https://specpicks.com/reviews/<slug-2>",
        "art-003": "https://specpicks.com/reviews/<slug-3>",
    },
}, indent=2))
```

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
