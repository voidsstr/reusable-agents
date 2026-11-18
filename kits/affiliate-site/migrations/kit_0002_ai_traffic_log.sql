-- kit_0002: ai_traffic_log, AI-assistant traffic the site actually received.
--
-- COLUMN CONTRACT: identical to the reference deployment's live table (ported
-- from SpecPicks db/migrations 20260526T140000Z_ai-traffic-log,
-- 20260925T161000Z_links-ai-traffic-log-verification and
-- 20261011T013000Z_seo-rec009-ai-traffic-path-ts-index, and checked against
-- its information_schema on 2026-11-17): the same 14 columns in the same
-- order, types, nullability and defaults, so framework/core/ai_traffic.py, the
-- sitemap's "products AI assistants use" rule and the agents' AI-landing
-- signal read it unchanged.
--
-- Only AI-relevant hits are written (aiCrawlerVerify.classifyAiTraffic returns
-- null otherwise), so the table stays small next to request logs:
--   kind = 'crawler'   an AI or search bot fetch, classified by user agent
--   kind = 'referral'  a person arriving from an AI-search origin (referer/utm)
-- ip_verified / ip_check record who is really behind a crawler row
-- (AiCrawlerVerifier.verify):
--   true   the address is in the vendor's published ranges (ip_check
--          'range:<list>' or 'static-range:<id>');
--   false  a list exists and the address is not in it ('not-in-range:<list>'),
--          or it is one of our own hosts ('excluded-ip');
--   NULL   unknown ('no-published-list', 'list-unavailable', 'no-ip').
-- ip_address is the right-most public X-Forwarded-For entry (clientIp());
-- forwarded_for keeps the raw header when it held more than that address.
--
-- IDEMPOTENT: CREATE ... IF NOT EXISTS throughout. On a database that already
-- has the table only the comments are rewritten. lock_timeout and
-- statement_timeout: see kit_0001.
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '15min';

CREATE TABLE IF NOT EXISTS ai_traffic_log (
  id            BIGSERIAL PRIMARY KEY,
  ts            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  kind          TEXT NOT NULL,            -- 'crawler' | 'referral'
  source        TEXT NOT NULL,            -- gptbot | claudebot | perplexitybot | chatgpt | perplexity | claude | ...
  path          TEXT,
  status_code   INTEGER,
  user_agent    TEXT,
  referer       TEXT,
  ip_address    TEXT,
  site_id       TEXT,
  ip_verified   BOOLEAN,
  ip_check      TEXT,
  duration_ms   INTEGER,                  -- server time for the page
  forwarded_for TEXT
);

CREATE INDEX IF NOT EXISTS idx_ai_traffic_ts          ON ai_traffic_log (ts DESC);
CREATE INDEX IF NOT EXISTS idx_ai_traffic_kind_source ON ai_traffic_log (kind, source, ts DESC);
-- Path-prefix rankings over a time window ("which pages do assistants fetch")
-- become index-only range scans.
CREATE INDEX IF NOT EXISTS idx_ai_traffic_path_ts
    ON ai_traffic_log (path text_pattern_ops, ts) INCLUDE (kind, status_code);

COMMENT ON TABLE ai_traffic_log IS
  'AI-assistant traffic: kind=crawler (AI or search bot fetch, by user agent) or kind=referral (a person arriving from an AI-search origin). ip_verified: true = in the vendor''s published ranges, false = a list exists and the address is not in it (or it is our own host), NULL = unknown. Read crawler rows with ip_verified IS NOT FALSE (framework/core/ai_traffic.py does).';
COMMENT ON COLUMN ai_traffic_log.ip_check IS
  'Why ip_verified is what it is: static-range:<id>, range:<list>, not-in-range:<list>, excluded-ip, no-published-list, list-unavailable or no-ip.';
