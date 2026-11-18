-- 0001: the gate fixture's own tables, applied after the kit's migrations by
-- runKitMigrations at boot (the same path a real site takes).
--
-- The shape follows the kit's cache model, cut down to what the gate needs:
--   fixture_products     our own catalogue rows (model names, specs, lifecycle)
--   fixture_item_cache   what the Amazon API returned, kept at most 24h
--                        (title, image link, detail URL, price, availability)
--   fixture_recalls      recall notices; an active one removes the buy button
--   fixture_best_lists   a ranked list page
--   fixture_notes        hand-written HTML pages (a renderer that trusts its
--                        HTML, the way many real renderers do); the violations
--                        scenario seeds its non-compliant pages here
-- editorial_articles (guides) is the kit's own table (kit_0003).
SET LOCAL lock_timeout = '5s';

CREATE TABLE IF NOT EXISTS fixture_products (
  asin              VARCHAR(10) PRIMARY KEY CHECK (asin ~ $re$^[A-Z0-9]{10}$$re$),
  model_name        TEXT NOT NULL,
  brand             TEXT NOT NULL,
  category          TEXT NOT NULL,
  lifecycle         TEXT NOT NULL DEFAULT 'live' CHECK (lifecycle IN ('live', 'unavailable', 'retired')),
  is_active         BOOLEAN NOT NULL DEFAULT TRUE,
  quality_flags     JSONB NOT NULL DEFAULT '[]'::jsonb,
  summary           TEXT NOT NULL,
  capacity_wh       INTEGER,
  weight_kg         NUMERIC(5, 1),
  alternative_asin  VARCHAR(10) REFERENCES fixture_products (asin),
  rank              INTEGER NOT NULL DEFAULT 100
);

CREATE TABLE IF NOT EXISTS fixture_item_cache (
  asin               VARCHAR(10) PRIMARY KEY REFERENCES fixture_products (asin),
  title              TEXT,
  image_url          TEXT,
  detail_page_url    TEXT,
  price              NUMERIC(10, 2),
  list_price         NUMERIC(10, 2),
  currency           VARCHAR(3),
  availability_type  TEXT,
  fetched_at         TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS fixture_recalls (
  id            TEXT PRIMARY KEY,
  asin          VARCHAR(10) NOT NULL REFERENCES fixture_products (asin),
  title         TEXT NOT NULL,
  hazard        TEXT NOT NULL,
  remedy        TEXT NOT NULL,
  status        TEXT NOT NULL CHECK (status IN ('stop-use', 'repair-hold', 'closed')),
  announced_on  DATE NOT NULL,
  source_url    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS fixture_recalls_asin ON fixture_recalls (asin) WHERE status <> 'closed';

CREATE TABLE IF NOT EXISTS fixture_best_lists (
  slug        TEXT PRIMARY KEY,
  title       TEXT NOT NULL,
  intro_md    TEXT NOT NULL,
  asins       TEXT[] NOT NULL,
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS fixture_notes (
  slug            TEXT PRIMARY KEY,
  title           TEXT NOT NULL,
  description     TEXT NOT NULL,
  body_html       TEXT NOT NULL,
  head_html       TEXT,
  jsonld          JSONB,
  amazon_content  BOOLEAN NOT NULL DEFAULT FALSE,
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The /r/ redirect's lookup: buyable is false while an active recall exists.
CREATE OR REPLACE VIEW fixture_redirect_lookup AS
SELECT p.asin,
       p.is_active,
       p.quality_flags,
       (p.lifecycle = 'live' AND NOT EXISTS (
          SELECT 1 FROM fixture_recalls r WHERE r.asin = p.asin AND r.status <> 'closed')) AS buyable,
       c.detail_page_url,
       p.model_name AS title,
       p.brand
  FROM fixture_products p
  LEFT JOIN fixture_item_cache c ON c.asin = p.asin;
