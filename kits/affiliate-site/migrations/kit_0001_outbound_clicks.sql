-- kit_0001: outbound_clicks, one row per affiliate click-out per logger.
--
-- COLUMN CONTRACT: identical to the reference deployment's live table (ported
-- from SpecPicks db/migrations 010_click_tracking,
-- 20260922T120000Z_outbound-clicks-bot-classification and
-- 20260924T230000Z_outbound-clicks-verified-human, and checked against its
-- information_schema on 2026-11-17): the same 20 columns in the same order,
-- with the same types, nullability and defaults. Every reader therefore works
-- unchanged: framework/core/human_clicks.py, the kit's verifiedClicks.ts, the
-- goal trackers and growth queries. The indexes are the reference
-- deployment's live set, under the same names.
--
-- WRITERS (kit HTTP middleware, build item K2)
--   via = 'redirect'  the /r/<ASIN> logger, before its 302 to Amazon.
--   via = 'beacon'    POST /api/outbound-click from the kit's click beacon: a
--                     real, JS-executing tab clicked a CTA. The verified-human
--                     series (kit_0004) is beacon rows only. A click on a /r/
--                     link writes one row of each kind, so never sum across
--                     `via`.
-- Rows are never deleted for being bots. is_bot / bot_reason are labels set by
-- botDetect.classifyClick at insert time (classified_at says when), so a rule
-- can be revisited and replayed. Read paths filter, or use the kit_0004 views.
--
-- IDEMPOTENT: CREATE ... IF NOT EXISTS throughout. On a database that already
-- has the table (a site adopting the kit) only the comments are rewritten.
--
-- lock_timeout: the runner sends this file as one implicit transaction, so SET
-- LOCAL holds for every statement below and nothing after it. On a busy
-- database, waiting behind a long reader would queue every click INSERT behind
-- this file; failing after 5 s lets the boot retry instead.
-- statement_timeout: every site role carries its own (binding amendment 9),
-- and the runner may be one with a short one; an index build on a grown table
-- must not be cancelled by it, because a failed file stops the boot. Each
-- statement after this line gets 15 minutes (PostgreSQL 13+ times every
-- statement of a multi-statement query on its own).
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '15min';

CREATE TABLE IF NOT EXISTS outbound_clicks (
  id                BIGSERIAL PRIMARY KEY,
  site_id           VARCHAR(100) NOT NULL,             -- site.json site.id
  asin              VARCHAR(20)  NOT NULL,
  tracking_id       VARCHAR(100),                      -- the Amazon tag the click was routed to
  source_page       VARCHAR(500),                      -- referer pathname at click time
  source_kind       VARCHAR(50),                       -- links.classifySource(): product, category, article, home, ...
  target            VARCHAR(20)  NOT NULL DEFAULT 'amazon',
  ip_address        INET,
  user_agent        VARCHAR(500),
  country           VARCHAR(10),                       -- ISO-3166 alpha-2 from the CDN header
  device_type       VARCHAR(50),
  clicked_at        TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
  is_bot            BOOLEAN NOT NULL DEFAULT FALSE,
  bot_reason        TEXT,
  classified_at     TIMESTAMPTZ,
  via               TEXT NOT NULL DEFAULT 'redirect',  -- 'redirect' | 'beacon'
  accept_language   TEXT,
  session_pageviews INT,
  ms_on_page        INT,
  placement         TEXT                               -- data-cta-placement of the clicked CTA
);

CREATE INDEX IF NOT EXISTS idx_outbound_clicks_asin        ON outbound_clicks (asin);
CREATE INDEX IF NOT EXISTS idx_outbound_clicks_clicked_at  ON outbound_clicks (clicked_at);
CREATE INDEX IF NOT EXISTS idx_outbound_clicks_source_kind ON outbound_clicks (source_kind);
CREATE INDEX IF NOT EXISTS idx_outbound_clicks_site_id     ON outbound_clicks (site_id);

-- The hot read is "real clicks in a recent window": the flag first, time second.
CREATE INDEX IF NOT EXISTS idx_outbound_clicks_human_time
    ON outbound_clicks (is_bot, clicked_at DESC);
CREATE INDEX IF NOT EXISTS idx_outbound_clicks_human_target_time
    ON outbound_clicks (is_bot, target, clicked_at DESC);
-- The per-IP-per-day velocity rule.
CREATE INDEX IF NOT EXISTS idx_outbound_clicks_ip_day
    ON outbound_clicks (ip_address, clicked_at);
-- verifiedClicks30dByAsin(): via='beacon' AND target='amazon' AND NOT is_bot
-- over the last 30 days.
CREATE INDEX IF NOT EXISTS idx_outbound_clicks_via_time
    ON outbound_clicks (via, target, clicked_at DESC) WHERE NOT is_bot;

COMMENT ON TABLE outbound_clicks IS
  'Affiliate click-outs. via=redirect: the /r/<ASIN> logger; via=beacon: the in-page click beacon (a real JS tab). A /r/ click writes one row of each kind, so never sum across via. is_bot/bot_reason are insert-time labels, never grounds for deletion. Count people with the kit_0004 views or framework/core/human_clicks.py, never raw rows.';
COMMENT ON COLUMN outbound_clicks.via IS
  'redirect = server-side /r/<ASIN> logger; beacon = POST /api/outbound-click from a JS-executing tab (the verified-human series).';
COMMENT ON COLUMN outbound_clicks.is_bot IS
  'Insert-time verdict of botDetect.classifyClick (reason in bot_reason). A label: rows are kept so rules can be replayed.';
