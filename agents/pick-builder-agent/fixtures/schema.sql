-- Reference schema for the pick-builder TABLE CONTRACT (AGENT.md): a minimal
-- cache-model catalog. Names match site.example.yaml; a site may use others.
-- What the contract needs is the SHAPE:
--   * one eligibility relation (here the view v_pick_eligible) holding every
--     product a pick may name, with its fresh price and the attributes the
--     selectors read;
--   * ladders / tiers (budget + params) / slots (tier_id, role, rank,
--     product_id NULL = deliberately empty, reason, eligibility jsonb);
--   * pick lists (kind + params) / items (list_id, rank, product_id, reason);
--   * a substitute-subjects relation (every product that may need one, with
--     an unbuyable flag) and the substitutes output table;
--   * optionally compat_edges (framework.core.compat_graph.reference_ddl).
-- framework/tests/test_pick_builder_agent.py loads this file into a
-- throwaway postgres:16 database. Nothing here is a production migration.

CREATE TABLE models (
  id                bigserial PRIMARY KEY,
  kind              text NOT NULL,
  model_name        text NOT NULL,
  grid_interactive  boolean NOT NULL DEFAULT false
);

-- Our own (manufacturer-sourced) specs, one row per model.
CREATE TABLE model_specs (
  model_id      bigint PRIMARY KEY REFERENCES models(id) ON DELETE CASCADE,
  capacity_wh   numeric,
  usable_wh     numeric,
  ac_cont_w     numeric,
  rated_w       numeric
);

CREATE TABLE products (
  id                   bigserial PRIMARY KEY,
  asin                 text NOT NULL UNIQUE CHECK (asin ~ '^B[0-9A-Z]{9}$'),
  model_id             bigint REFERENCES models(id),
  kind                 text NOT NULL,
  brand                text,
  is_active            boolean NOT NULL DEFAULT true,
  lifecycle            text NOT NULL DEFAULT 'live',
  category_confidence  numeric NOT NULL DEFAULT 1.0
);

-- The 24h display cache (Amazon fields NULLed by the purge).
CREATE TABLE amazon_item_cache (
  asin        text PRIMARY KEY,
  price       numeric,
  list_price  numeric,
  currency    text,
  outcome     text,
  fetched_at  timestamptz
);

CREATE TABLE recall_holds (
  product_id  bigint NOT NULL,
  tier        text NOT NULL
);

CREATE VIEW v_pick_eligible AS
  SELECT p.id AS product_id, p.asin, p.model_id, p.kind, p.brand, c.price, c.list_price, c.currency,
         p.category_confidence, s.capacity_wh, s.usable_wh, s.ac_cont_w, s.rated_w
    FROM products p
    JOIN models m ON m.id = p.model_id
    JOIN amazon_item_cache c ON c.asin = p.asin
    LEFT JOIN model_specs s ON s.model_id = p.model_id
   WHERE p.is_active AND p.lifecycle = 'live' AND NOT m.grid_interactive
     AND c.outcome = 'priced' AND c.fetched_at > now() - interval '24 hours'
     AND c.price >= 1 AND (c.currency IS NULL OR c.currency = 'USD')
     AND (c.list_price IS NULL OR c.list_price >= c.price)
     AND p.category_confidence >= 0.5
     AND NOT EXISTS (SELECT 1 FROM recall_holds r WHERE r.product_id = p.id);

-- Every active product, buyable or not: the substitutes map covers them all.
CREATE VIEW v_substitute_subjects AS
  SELECT p.id AS product_id, p.asin, p.model_id, p.kind, s.usable_wh, s.ac_cont_w,
         NOT EXISTS (SELECT 1 FROM v_pick_eligible e WHERE e.product_id = p.id) AS unbuyable
    FROM products p LEFT JOIN model_specs s ON s.model_id = p.model_id
   WHERE p.is_active;

CREATE TABLE compat_edges (
  id bigserial PRIMARY KEY,
  a_model_id bigint NOT NULL,
  b_model_id bigint NOT NULL,
  relation text NOT NULL,
  verdict text NOT NULL,
  config jsonb NOT NULL DEFAULT '{}'::jsonb,
  via_model_id bigint,
  rule_id text NOT NULL,
  rule_version text NOT NULL,
  evidence_source_ids bigint[] NOT NULL DEFAULT '{}',
  confidence text NOT NULL,
  computed_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (a_model_id, b_model_id, relation, rule_id)
);

CREATE TABLE ladders (
  id      bigserial PRIMARY KEY,
  slug    text NOT NULL UNIQUE,
  kind    text NOT NULL,
  status  text NOT NULL DEFAULT 'active'
);

CREATE TABLE ladder_tiers (
  id           bigserial PRIMARY KEY,
  ladder_id    bigint NOT NULL REFERENCES ladders(id) ON DELETE CASCADE,
  budget_usd   numeric,
  required_wh  numeric,
  params       jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE ladder_slots (
  tier_id      bigint NOT NULL REFERENCES ladder_tiers(id) ON DELETE CASCADE,
  role         text NOT NULL,
  rank         integer NOT NULL,
  product_id   bigint REFERENCES products(id),
  reason       text,
  eligibility  jsonb,
  updated_at   timestamptz,
  PRIMARY KEY (tier_id, role, rank)
);

CREATE TABLE pick_lists (
  id      bigserial PRIMARY KEY,
  slug    text NOT NULL UNIQUE,
  kind    text NOT NULL,
  params  jsonb NOT NULL DEFAULT '{}'::jsonb,
  status  text NOT NULL DEFAULT 'active'
);

CREATE TABLE pick_list_items (
  list_id     bigint NOT NULL REFERENCES pick_lists(id) ON DELETE CASCADE,
  rank        integer NOT NULL,
  product_id  bigint NOT NULL REFERENCES products(id),
  reason      text,
  PRIMARY KEY (list_id, rank)
);

CREATE TABLE product_substitutes (
  product_id      bigint NOT NULL REFERENCES products(id) ON DELETE CASCADE,
  rank            integer NOT NULL,
  alt_product_id  bigint NOT NULL REFERENCES products(id),
  reason          text,
  PRIMARY KEY (product_id, rank)
);
