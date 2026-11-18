-- SYNTHETIC site catalog for the recall-gate DB tests (a throwaway Postgres
-- container). Shaped like the affiliate-site design: our own models + brands,
-- one products row per ASIN. Real site migrations own their schema.
CREATE TABLE brands (id bigint PRIMARY KEY, name text NOT NULL);
CREATE TABLE models (
    id bigint PRIMARY KEY,
    brand_id bigint NOT NULL REFERENCES brands (id),
    model_name text,
    model_number text,
    family text,
    slug text NOT NULL
);
CREATE TABLE products (
    id bigint PRIMARY KEY,
    model_id bigint NOT NULL REFERENCES models (id),
    asin text,
    category text,
    is_active boolean NOT NULL DEFAULT true
);
CREATE TABLE kit_components (kit_model_id bigint NOT NULL, component_model_id bigint NOT NULL);
