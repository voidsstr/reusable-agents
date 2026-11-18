-- kit_0003: editorial_articles, the site's articles (guides, comparisons,
-- explainers, deal pages).
--
-- COLUMN CONTRACT: identical to the reference deployment's live table (ported
-- from SpecPicks db/migrations 017_testbench, 018_testbench_research_pipeline,
-- 026_editorial_seo_fields and 20260910T094200Z_growth-01-article-series, plus
-- the trend_score / trend_score_updated_at / trend_source / vertical columns
-- that exist only in its information_schema, checked 2026-11-17): the same 34
-- columns in the same order, types and nullability. That is the column set the
-- implementer's ARTICLE_AUTHOR INSERT, the article output gate, the publish gate
-- (framework/cli/article_publish_gate.py), featured rotation, priority and
-- reconcile-shipped expect, so they work unchanged.
--
-- Deliberate differences from the reference table:
--   * author defaults to 'Editorial', not a site name. A site sets its own
--     default from site.json byline.name in a site migration
--     (ALTER TABLE editorial_articles ALTER COLUMN author SET DEFAULT '...').
--   * hero_image_url: two CHECK constraints instead of the reference site's
--     allowlist, which admits Amazon's image CDN.
--       editorial_articles_hero_image_no_amazon_cdn   binding: no Amazon-hosted
--         image is ever stored as a hero (Associates licence; use the local
--         image daemon, or leave it NULL). It rejects an Amazon image CDN host
--         anywhere in the URL (m.media-amazon.com, *.ssl-images-amazon.com,
--         *.images-amazon.com, amazon-adsystem.com, assoc-amazon.com, or a
--         mirror path named after one), and an Amazon storefront or short-link
--         host followed by a path, port or query (amazon.<tld> and its
--         subdomains, which serve images.amazon.com/images/P/<ASIN>... and
--         www.amazon.com/images/I/...; amzn.to/.com/.eu/.asia; a.co), the
--         hosts render/amazonHosts.ts treats as Amazon's. S3 (amazonaws.com)
--         is not Amazon retail and not banned here. Never relax it.
--       editorial_articles_hero_image_host_allowlist  NULL, '', or, with no
--         whitespace or control character anywhere (browsers drop tab, CR and
--         LF from a URL, so '/<TAB>/evil.example/x' loads from another host),
--         a site-relative path that is not protocol-relative, or https on an
--         Azure blob account. Any account passes: a site that knows its
--         storage account pins it, and a site that serves heroes from
--         elsewhere replaces this constraint (DROP CONSTRAINT + ADD
--         CONSTRAINT in a site migration); the Amazon ban stays.
--     A CHECK constraint is the one guard every writer must pass, including a
--     model improvising SQL.
--   * The reference site's category-alias and vertical triggers map its own
--     category names and are niche values, so they are not here. `vertical`
--     stays as a plain nullable column; a site may maintain it.
--
-- updated_at tracks CONTENT edits. updated_at feeds feed <updated>, Article
-- JSON-LD dateModified and the sitemap lastmod, so an UPDATE that moves it
-- forward without changing the text (title, subtitle, excerpt, body_md, faqs,
-- outbound_citations) keeps the old value: hero swaps, related-link arrays,
-- tags, trend scores, view_count and status changes never re-date an article.
-- Moving it backward (a correction) is always allowed. A deliberate forward
-- bump without a text change opts in per transaction:
--     SET LOCAL affiliate_kit.allow_updated_at_bump = 'on';
-- (Ported from the reference site's 20261108T103012Z migration; its GUC name
-- named the site, the kit's does not.)
--
-- Publishing on a content_policy site is a gated state change: render
-- migrations/templates/publish_trigger.sql.tmpl into a site migration (see
-- server/src/db/publishTrigger.ts). It is not installed here because a site
-- without a content policy keeps the reference behaviour.
--
-- IDEMPOTENT: CREATE ... IF NOT EXISTS / CREATE OR REPLACE, and the trigger is
-- created only when no trigger of that name exists. On a database that already
-- has the table (a site adopting the kit), no constraint, trigger or row is
-- touched; only the comments are rewritten and the kit's trigger function is
-- defined (unused there). lock_timeout and statement_timeout: see kit_0001.
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '15min';

CREATE TABLE IF NOT EXISTS editorial_articles (
  id                       SERIAL PRIMARY KEY,
  slug                     VARCHAR(200) UNIQUE NOT NULL,
  title                    VARCHAR(300) NOT NULL,
  subtitle                 VARCHAR(500),
  excerpt                  TEXT,                          -- meta description (145-160 chars)
  body_md                  TEXT NOT NULL,                 -- markdown, rendered at display time
  hero_image_url           TEXT,
  category                 VARCHAR(50) NOT NULL,
  tags                     TEXT[] NOT NULL DEFAULT '{}',  -- e.g. holiday:<id>, lane:<id>
  difficulty               VARCHAR(20),
  estimated_read_time      INT,                           -- minutes
  related_hardware_slugs   TEXT[] NOT NULL DEFAULT '{}',
  related_product_asins    TEXT[] NOT NULL DEFAULT '{}',
  related_article_slugs    TEXT[] NOT NULL DEFAULT '{}',
  primary_keyword          VARCHAR(200),
  secondary_keywords       TEXT[] NOT NULL DEFAULT '{}',
  author                   VARCHAR(100) NOT NULL DEFAULT 'Editorial',
  author_bio               TEXT,
  status                   VARCHAR(20) NOT NULL DEFAULT 'published',  -- 'draft' | 'published' | 'archived'
  featured                 BOOLEAN NOT NULL DEFAULT FALSE,
  view_count               INT NOT NULL DEFAULT 0,
  published_at             TIMESTAMPTZ,
  updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  written_at               TIMESTAMPTZ DEFAULT NOW(),
  written_by               VARCHAR(100) DEFAULT 'generator',
  faqs                     JSONB,                         -- [{question, answer}], drives FAQPage JSON-LD
  outbound_citations       TEXT[],
  trend_score              NUMERIC,
  trend_score_updated_at   TIMESTAMPTZ,
  trend_source             VARCHAR(40),
  vertical                 VARCHAR(20),
  series_slug              TEXT,
  series_order             INTEGER,
  -- Regexes are dollar-quoted so they mean the same whatever
  -- standard_conforming_strings is. In the ban, a storefront or short-link
  -- host must start at a non-hostname character (so notamazon.com and
  -- data.co are not hits) and be followed by a path, port, query or a
  -- character a browser drops (so a file named amazon.webp is not a hit).
  -- '^/[^/\\]' is a site-relative path that is not protocol-relative ('//host'
  -- and '/\host' both load from another host).
  CONSTRAINT editorial_articles_hero_image_no_amazon_cdn CHECK (
    hero_image_url IS NULL
    OR (hero_image_url !~* $re$(media-amazon\.com|ssl-images-amazon\.com|images-amazon\.com|amazon-adsystem\.com|assoc-amazon\.com)$re$
        AND hero_image_url !~* $re$(^|[^a-z0-9-])(amazon(\.[a-z]{2,})+|amzn\.(to|com|eu|asia)|a\.co)[/\\:?#[:space:][:cntrl:]]$re$)
  ),
  CONSTRAINT editorial_articles_hero_image_host_allowlist CHECK (
    hero_image_url IS NULL
    OR hero_image_url = ''
    OR (hero_image_url !~ $re$[[:space:][:cntrl:]]$re$
        AND (hero_image_url ~ $re$^/[^/\\]$re$
             OR hero_image_url ~* $re$^https://[a-z0-9-]+\.blob\.core\.windows\.net/$re$))
  )
);

-- Published-row indexes, under the reference deployment's names.
CREATE INDEX IF NOT EXISTS idx_tb_published ON editorial_articles (published_at DESC) WHERE status = 'published';
CREATE INDEX IF NOT EXISTS idx_tb_category  ON editorial_articles (category)          WHERE status = 'published';
CREATE INDEX IF NOT EXISTS idx_tb_featured  ON editorial_articles (featured, published_at DESC) WHERE status = 'published' AND featured;
CREATE INDEX IF NOT EXISTS idx_editorial_articles_published_id
    ON editorial_articles (id) WHERE status = 'published';
CREATE INDEX IF NOT EXISTS idx_editorial_articles_author_published
    ON editorial_articles (author) WHERE status = 'published';
CREATE INDEX IF NOT EXISTS idx_editorial_articles_trend
    ON editorial_articles (category, trend_score DESC NULLS LAST, published_at DESC) WHERE status = 'published';
CREATE INDEX IF NOT EXISTS idx_editorial_articles_vertical
    ON editorial_articles (vertical, trend_score DESC NULLS LAST, published_at DESC) WHERE status = 'published';
CREATE INDEX IF NOT EXISTS idx_editorial_articles_series
    ON editorial_articles (series_slug, series_order) WHERE series_slug IS NOT NULL;
-- "Which articles mention this ASIN" (PDP related-guides rails, recall pulls).
CREATE INDEX IF NOT EXISTS idx_ea_related_asins_gin
    ON editorial_articles USING gin (related_product_asins);

CREATE OR REPLACE FUNCTION kit_editorial_articles_keep_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $fn$
BEGIN
  IF NEW.updated_at IS NOT DISTINCT FROM OLD.updated_at
     OR NEW.updated_at < OLD.updated_at
     OR COALESCE(current_setting('affiliate_kit.allow_updated_at_bump', true), '') = 'on' THEN
    RETURN NEW;
  END IF;
  IF NEW.title                  IS NOT DISTINCT FROM OLD.title
     AND NEW.subtitle           IS NOT DISTINCT FROM OLD.subtitle
     AND NEW.excerpt            IS NOT DISTINCT FROM OLD.excerpt
     AND NEW.body_md            IS NOT DISTINCT FROM OLD.body_md
     AND NEW.faqs               IS NOT DISTINCT FROM OLD.faqs
     AND NEW.outbound_citations IS NOT DISTINCT FROM OLD.outbound_citations THEN
    NEW.updated_at := OLD.updated_at;
  END IF;
  RETURN NEW;
END
$fn$;

COMMENT ON FUNCTION kit_editorial_articles_keep_updated_at() IS
  'BEFORE UPDATE on editorial_articles: an UPDATE that moves updated_at forward without changing the text (title, subtitle, excerpt, body_md, faqs, outbound_citations) keeps the old value. Opt in to a bump with SET LOCAL affiliate_kit.allow_updated_at_bump = ''on''.';

DO $do$
BEGIN
  IF NOT EXISTS (
    SELECT 1
      FROM pg_trigger
     WHERE tgrelid = 'editorial_articles'::regclass
       AND tgname = 'editorial_articles_updated_at_content_trg'
       AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER editorial_articles_updated_at_content_trg
      BEFORE UPDATE ON editorial_articles
      FOR EACH ROW EXECUTE FUNCTION kit_editorial_articles_keep_updated_at();
  END IF;
END
$do$;

COMMENT ON TABLE editorial_articles IS
  'Editorial articles. Only status = ''published'' rows render. On a content_policy site a trigger (migrations/templates/publish_trigger.sql.tmpl) keeps every write by a role other than the publisher out of the published state, and framework/cli/article_publish_gate.py promotes drafts that pass every guard.';
COMMENT ON COLUMN editorial_articles.written_at IS
  'Timestamp of initial article creation. Distinct from published_at (stays NULL for drafts) and updated_at (changes on every edit).';
COMMENT ON COLUMN editorial_articles.faqs IS
  'Array of {question, answer} objects. SSR emits a FAQPage JSON-LD block when non-empty. Each answer should be ≥40 words.';
COMMENT ON COLUMN editorial_articles.outbound_citations IS
  '≥3 authoritative external URLs the article body links to. Drives the eeat-outbound-citation-count analyzer check.';
COMMENT ON COLUMN editorial_articles.updated_at IS
  'Last CONTENT edit (feed updated, Article dateModified, sitemap lastmod). A forward move without a text change is reverted by kit_editorial_articles_keep_updated_at().';
COMMENT ON COLUMN editorial_articles.hero_image_url IS
  'NULL, a site-relative path, or an https URL on an allowed host, with no whitespace or control character. Never an Amazon-hosted image: an image CDN, an amazon.<tld> or short-link host, or a mirror named after one (CHECK editorial_articles_hero_image_no_amazon_cdn).';
COMMENT ON COLUMN editorial_articles.author IS
  'Byline. The kit default is a neutral placeholder; a site sets its own default from site.json byline.name.';
