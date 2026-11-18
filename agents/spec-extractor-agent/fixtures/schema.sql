-- Reference schema for the spec-extractor TABLE CONTRACT (AGENT.md).
--
-- A site migration may use other names: every table and column below is
-- named in the site's site.yaml `spec_extractor.tables` / `kinds.*.typed` /
-- `compat` / `runtime` blocks. What the contract needs is the SHAPE:
--   * spec_sources has a UNIQUE (model_id, url) and an engine-owned jsonb
--     bookkeeping column (default name fetch_state);
--   * spec_values has a text extractor_id (engine rows always set it;
--     operator rows leave it NULL or start it with 'manual-entry');
--   * each typed spec table has a UNIQUE key column (default model_id);
--   * runtime estimates are unique per (model_id, preset_id).
-- framework/tests/test_spec_extractor_agent.py loads this file into a
-- throwaway postgres:16 database. Nothing here is a production migration.

CREATE TABLE brands (
  id          bigserial PRIMARY KEY,
  slug        text NOT NULL UNIQUE,
  name        text NOT NULL
);

CREATE TABLE models (
  id                bigserial PRIMARY KEY,
  brand_id          bigint REFERENCES brands(id),
  kind              text NOT NULL,
  model_name        text NOT NULL,
  model_number      text,
  lifecycle         text NOT NULL DEFAULT 'live',
  spec_status       text,
  spec_completeness numeric(5,4),
  spec_checked_at   timestamptz
);

-- The engine reads models through a view that joins the brand slug.
CREATE VIEW v_spec_models AS
  SELECT m.id, m.kind, b.slug AS brand, m.model_name AS name, m.model_number, m.lifecycle
    FROM models m LEFT JOIN brands b ON b.id = m.brand_id;

CREATE TABLE spec_sources (
  id              bigserial PRIMARY KEY,
  model_id        bigint NOT NULL REFERENCES models(id) ON DELETE CASCADE,
  kind            text NOT NULL CHECK (kind IN ('manufacturer-page', 'manual-pdf', 'manufacturer-faq',
                                               'measured-test', 'cert-listing', 'manual-entry')),
  url             text NOT NULL,
  publisher       text,
  fetched_at      timestamptz,
  content_sha256  text,
  fetch_state     jsonb NOT NULL DEFAULT '{}'::jsonb,
  UNIQUE (model_id, url)
);

CREATE TABLE spec_values (
  id            bigserial PRIMARY KEY,
  model_id      bigint NOT NULL REFERENCES models(id) ON DELETE CASCADE,
  field         text NOT NULL,
  value_num     numeric,
  value_text    text,
  unit          text,
  source_id     bigint REFERENCES spec_sources(id) ON DELETE CASCADE,
  extractor     text NOT NULL CHECK (extractor IN ('deterministic', 'haiku', 'opus-adjudicated', 'manual')),
  extractor_id  text,
  confidence    numeric NOT NULL DEFAULT 1,
  status        text NOT NULL CHECK (status IN ('accepted', 'conflict', 'rejected', 'superseded')),
  snippet       text,
  qualifier     text,
  conflict_id   text
);
CREATE INDEX spec_values_model_idx ON spec_values (model_id);

-- Typed projections (one row per model, values in each field's canonical unit).
CREATE TABLE station_specs (
  model_id           bigint PRIMARY KEY REFERENCES models(id) ON DELETE CASCADE,
  model_number       text,
  capacity_wh        numeric,
  chemistry          text,
  ac_cont_w          numeric,
  ac_surge_w         numeric,
  ups_ms             numeric,
  ups_supported      boolean,
  pv_v_min           numeric,
  pv_voc_max         numeric,
  pv_w_max           numeric,
  weight_kg          numeric,
  dimensions_mm      numeric[],
  spec_completeness  numeric(5,4),
  validated_at       timestamptz
);

CREATE TABLE panel_specs (
  model_id               bigint PRIMARY KEY REFERENCES models(id) ON DELETE CASCADE,
  rated_w                numeric,
  voc                    numeric,
  vmp                    numeric,
  isc                    numeric,
  temp_coeff_voc_pct_c   numeric,
  connector              text,
  validated_at           timestamptz
);

-- framework.core.compat_graph.reference_ddl('compat_edges') (kept in sync by the tests).
CREATE TABLE compat_edges (
  id bigserial PRIMARY KEY,
  a_model_id bigint NOT NULL,
  b_model_id bigint NOT NULL,
  relation text NOT NULL,
  verdict text NOT NULL CHECK (verdict IN ('fits', 'fits_with_adapter', 'fits_in_series', 'fits_in_parallel', 'over_limit', 'no_fit', 'unknown')),
  config jsonb NOT NULL DEFAULT '{}'::jsonb,
  via_model_id bigint,
  rule_id text NOT NULL,
  rule_version text NOT NULL,
  evidence_source_ids bigint[] NOT NULL DEFAULT '{}',
  confidence text NOT NULL CHECK (confidence IN ('manual', 'manufacturer-listed', 'computed')),
  computed_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (a_model_id, b_model_id, relation, rule_id)
);
CREATE INDEX compat_edges_b_idx ON compat_edges (b_model_id, relation);

-- Operator / manufacturer fit statements that are not spec fields.
CREATE TABLE compat_fit_records (
  id            bigserial PRIMARY KEY,
  a_model_id    bigint NOT NULL,
  b_model_id    bigint NOT NULL,
  relation      text NOT NULL,
  verdict       text NOT NULL DEFAULT 'fits',
  origin        text NOT NULL DEFAULT 'manufacturer',
  config        jsonb NOT NULL DEFAULT '{}'::jsonb,
  via_model_id  bigint,
  source_url    text,
  source_kind   text,
  publisher     text,
  source_id     bigint,
  statement     text
);

CREATE TABLE load_presets (
  id          text PRIMARY KEY,
  label       text NOT NULL,
  avg_w       numeric NOT NULL,
  surge_w     numeric,
  duty_cycle  numeric,
  wh_per_day  numeric,
  source_url  text NOT NULL,
  medical     boolean NOT NULL DEFAULT false CHECK (medical = false)
);

CREATE TABLE runtime_estimates (
  model_id     bigint NOT NULL REFERENCES models(id) ON DELETE CASCADE,
  preset_id    text NOT NULL REFERENCES load_presets(id) ON DELETE CASCADE,
  basis        text NOT NULL,
  usable_wh    numeric NOT NULL,
  hours_est    numeric NOT NULL,
  hours_low    numeric NOT NULL,
  hours_high   numeric NOT NULL,
  surge_ok     boolean,
  computed_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (model_id, preset_id)
);

CREATE TABLE products (
  id         bigserial PRIMARY KEY,
  asin       text NOT NULL UNIQUE CHECK (asin ~ '^B[0-9A-Z]{9}$'),
  model_id   bigint REFERENCES models(id),
  kind       text NOT NULL,
  is_active  boolean NOT NULL DEFAULT true
);
