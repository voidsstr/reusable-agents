-- kit_0004: the verified-human click views.
--
-- ONE DEFINITION OF "A PERSON CLICKED A BUY LINK". The fleet's definition is
-- framework/core/human_clicks.py: a row is human only if no per-row rule fires
-- (checked in this order, the first one naming the verdict):
--   site-flagged-bot   is_bot (botDetect.classifyClick at insert time)
--   no-ua              empty user agent
--   ua-bot             UA names a crawler, headless browser or HTTP library
--   stale-browser      Chromium or Firefox major more than 16 behind the
--                      current release (extrapolated every 28 days from a
--                      2025 anchor); SamsungBrowser, UC, Yandex, Huawei,
--                      MIUI, QQ and Silk are exempt
--   excluded-country   country SG (a headless datacenter fleet)
--   datacenter-ip      a cloud / hosting address prefix
--   no-referer         no on-site referer (source_page)
--   scanner-referer    the referer is attack-scanner exhaust
-- and its IP does not exceed 20 such rows on that UTC day (`velocity`).
-- kit_click_row_verdict() below is that rule set with human_clicks' DEFAULT_SPEC
-- and the outbound_clicks column spec (time_col clicked_at, referer_col
-- source_page, country_col country, bot_flag_col is_bot), and
-- outbound_clicks_verdicts applies the velocity cut the way human_clicks does:
-- over every row that passed the per-row rules, whatever its via or target.
-- unit/db/humanClicksConsistency.test.ts diffs every constant here against
-- human_clicks.py, and unit/db/views.pg.test.ts compares the two verdicts row
-- by row on a real database.
--
-- WHAT THE SQL CANNOT SEE: overrides in storage config/human-click-filter-config.json
-- (for example a Safari floor or extra datacenter prefixes) apply only to
-- human_clicks.py. Goal metrics therefore come from human_clicks.py; these views
-- serve SQL readers (agents' growth queries, the price verifier, ad-hoc checks)
-- and agree with it on the defaults. To change a constant for one site, replace
-- kit_click_row_verdict() in a site migration (same signature, so the views
-- pick it up); never fork the views.
--
-- VIEWS (column lists identical to the reference deployment's live views, so
-- its readers work unchanged):
--   outbound_clicks_verdicts            every row with click_day (UTC date),
--                                       per_ip_day and verdict. New in the kit:
--                                       the auditable breakdown.
--   outbound_clicks_human               human rows of every via.
--   outbound_clicks_verified_human      human beacon rows: the goal series.
--   amazon_clicks_verified_human_daily  verified human Amazon clicks per
--                                       America/New_York day.
-- Differences from the reference deployment's views, all toward human_clicks.py:
-- the full per-row rule set instead of is_bot alone, and per_ip_day counts the
-- day's rows that passed the per-row rules across every via (a /r/ click logs
-- one row per via, so its effective cut is about 10 clicks a day). Rows with a
-- NULL clicked_at have no day and are left out, as human_clicks' window does.
-- On the oldest day of a windowed human_clicks count only the rows inside the
-- window feed the velocity rule; these views always see the whole UTC day.
--
-- COST: the velocity window runs over every row the query reads. A predicate on
-- clicked_at cannot be pushed below a window function, but one on click_day
-- can (it is a partition key), so filter outbound_clicks_verdicts on click_day
-- for windowed counts, e.g. WHERE click_day > (now() AT TIME ZONE 'UTC')::date - 30.
--
-- IDEMPOTENT: CREATE OR REPLACE throughout. Replacing a view keeps its column
-- list, so a database that already has these views (a site adopting the kit)
-- takes the kit's definitions in place. lock_timeout and statement_timeout:
-- see kit_0001.
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '15min';

-- The per-row rules, in human_clicks' order. NULL means every rule passed.
-- p_ip is the address as text (CAST(inet AS text) carries a /32 or /128 suffix,
-- which is stripped). Regex constants are dollar-quoted so no escape
-- processing applies; they are byte-identical to the patterns human_clicks.py
-- sends as query parameters.
CREATE OR REPLACE FUNCTION kit_click_row_verdict(
    p_is_bot     boolean,
    p_user_agent text,
    p_ip         text,
    p_country    text,
    p_referer    text)
RETURNS text
LANGUAGE sql
STABLE
PARALLEL SAFE
AS $fn$
SELECT CASE
  WHEN COALESCE(p_is_bot, FALSE) THEN 'site-flagged-bot'
  WHEN COALESCE(TRIM(p_user_agent), '') = '' THEN 'no-ua'
  WHEN p_user_agent ~* $re$(bot|spider|crawler|slurp|bingpreview|preview|facebookexternalhit|semrush|ahrefs|mj12|dotbot|petal|yandex|bytespider|gptbot|claudebot|ccbot|perplexity|applebot|amazonbot|dataforseo|serpstat|screaming\ frog|monitor|uptime|pingdom|lighthouse|pagespeed|headless|phantom|puppeteer|playwright|selenium|webdriver|cypress|electron/|scrapingbee|zenrows|apify|python|curl|wget|scrapy|http\-client|httpclient|okhttp|java/|go\-http|libwww|lwp::|apache\-httpclient|axios|node\-fetch|undici|postman|insomnia|httpie|aiohttp|guzzle)$re$
    THEN 'ua-bot'
  WHEN ((COALESCE(substring(p_user_agent from $re$Chrome/([0-9]{1,6})$re$)::int, 2147483647)
           < 140 + GREATEST(0, (now() AT TIME ZONE 'UTC')::date - DATE '2025-09-02') / 28 - 16
         OR COALESCE(substring(p_user_agent from $re$Firefox/([0-9]{1,6})$re$)::int, 2147483647)
           < 142 + GREATEST(0, (now() AT TIME ZONE 'UTC')::date - DATE '2025-08-19') / 28 - 16)
        AND p_user_agent !~* $re$SamsungBrowser|UCBrowser|YaBrowser|HuaweiBrowser|MiuiBrowser|QQBrowser|Silk/$re$)
    THEN 'stale-browser'
  WHEN upper(COALESCE(p_country, '')) = ANY (ARRAY['SG']) THEN 'excluded-country'
  WHEN split_part(p_ip, '/', 1) ~ $re$^(43\.|49\.51\.|101\.32\.|101\.33\.|101\.34\.|101\.35\.|106\.52\.|106\.53\.|106\.54\.|106\.55\.|118\.195\.|119\.28\.|119\.29\.|124\.156\.|124\.220\.|124\.221\.|124\.222\.|124\.223\.|129\.226\.|150\.109\.|162\.62\.|170\.106\.|175\.24\.|175\.27\.|175\.178\.|47\.74\.|47\.75\.|47\.76\.|47\.77\.|47\.78\.|47\.79\.|47\.80\.|47\.81\.|47\.82\.|47\.83\.|47\.84\.|47\.85\.|47\.86\.|47\.87\.|47\.88\.|47\.89\.|47\.90\.|47\.91\.|47\.236\.|47\.237\.|47\.241\.|47\.242\.|47\.243\.|47\.244\.|47\.245\.|47\.246\.|47\.250\.|47\.251\.|47\.252\.|47\.253\.|47\.254\.|8\.208\.|8\.209\.|8\.210\.|8\.211\.|8\.212\.|8\.213\.|8\.214\.|8\.215\.|8\.216\.|8\.217\.|8\.218\.|8\.219\.|8\.220\.|8\.221\.|8\.222\.|1\.92\.|110\.238\.|119\.8\.|119\.12\.|119\.13\.|121\.36\.|121\.37\.|124\.70\.|124\.71\.|139\.9\.|139\.159\.|159\.138\.|166\.108\.|190\.92\.|104\.28\.|172\.104\.|139\.162\.|45\.79\.|45\.33\.|50\.116\.|159\.89\.|167\.71\.|134\.209\.|165\.227\.|207\.154\.|128\.199\.|139\.59\.|188\.166\.|206\.189\.|159\.223\.|143\.198\.|146\.190\.|64\.225\.|68\.183\.|45\.32\.|45\.63\.|45\.76\.|45\.77\.|66\.42\.|108\.61\.|149\.28\.|155\.138\.|207\.148\.|51\.15\.|163\.172\.|212\.47\.|35\.|34\.|52\.|54\.|18\.|13\.|20\.|40\.|5\.9\.|88\.198\.|78\.46\.|95\.216\.|116\.202\.|135\.181\.|65\.108\.|65\.21\.|51\.68\.|51\.75\.|51\.77\.|51\.79\.|51\.81\.|51\.83\.|51\.89\.|51\.91\.|51\.161\.|51\.178\.|51\.195\.|51\.210\.|51\.222\.|54\.36\.|54\.37\.|54\.38\.|147\.135\.|149\.56\.|158\.69\.|192\.99\.|198\.27\.|129\.146\.|132\.145\.|150\.136\.|152\.67\.|158\.101\.|193\.122\.|66\.249\.|157\.55\.|207\.46\.)$re$
    THEN 'datacenter-ip'
  WHEN COALESCE(TRIM(p_referer), '') = '' THEN 'no-referer'
  WHEN COALESCE(p_referer, '') ~* $re$[()<>']|sysdate|sleep\(|union\s+select|\.jsp|\.php|\.asp|etc/passwd|\.\./|^/(web|wp-admin|wp-login|cgi-bin|phpmyadmin|admin)(/|$)$re$
    THEN 'scanner-referer'
  ELSE NULL
END
$fn$;

COMMENT ON FUNCTION kit_click_row_verdict(boolean, text, text, text, text) IS
  'Per-row click verdict of framework/core/human_clicks.py (DEFAULT_SPEC): site-flagged-bot, no-ua, ua-bot, stale-browser, excluded-country, datacenter-ip, no-referer or scanner-referer; NULL when every rule passes. Arguments: the row''s bot flag, user agent, IP as text, country, on-site referer.';

CREATE OR REPLACE VIEW outbound_clicks_verdicts AS
SELECT v.id, v.site_id, v.asin, v.tracking_id, v.source_page, v.source_kind,
       v.target, v.ip_address, v.user_agent, v.country, v.device_type,
       v.clicked_at, v.is_bot, v.bot_reason, v.classified_at, v.via,
       v.accept_language, v.session_pageviews, v.ms_on_page, v.placement,
       v.click_day,
       v.per_ip_day,
       CASE
         WHEN v.row_verdict IS NOT NULL THEN v.row_verdict
         WHEN v.per_ip_day > 20 THEN 'velocity'
         ELSE 'human'
       END AS verdict
  FROM (
    SELECT r.*,
           COUNT(*) FILTER (WHERE r.row_verdict IS NULL)
             OVER (PARTITION BY r.ip_key, r.click_day) AS per_ip_day
      FROM (
        SELECT o.id, o.site_id, o.asin, o.tracking_id, o.source_page, o.source_kind,
               o.target, o.ip_address, o.user_agent, o.country, o.device_type,
               o.clicked_at, o.is_bot, o.bot_reason, o.classified_at, o.via,
               o.accept_language, o.session_pageviews, o.ms_on_page, o.placement,
               split_part(CAST(o.ip_address AS text), '/', 1) AS ip_key,
               (o.clicked_at AT TIME ZONE 'UTC')::date AS click_day,
               kit_click_row_verdict(o.is_bot, o.user_agent, CAST(o.ip_address AS text),
                                     o.country, o.source_page) AS row_verdict
          FROM outbound_clicks o
         WHERE o.clicked_at IS NOT NULL
      ) r
  ) v;

COMMENT ON VIEW outbound_clicks_verdicts IS
  'Every outbound click with its framework/core/human_clicks.py verdict (human, velocity, or the first per-row rule that fired), the UTC click_day and per_ip_day (rows from that IP that day that passed the per-row rules, any via). Filter on click_day, not clicked_at, so the predicate reaches the table.';

CREATE OR REPLACE VIEW outbound_clicks_human AS
SELECT id, site_id, asin, tracking_id, source_page, source_kind, target,
       ip_address, user_agent, country, device_type, clicked_at,
       is_bot, bot_reason, classified_at, per_ip_day
  FROM outbound_clicks_verdicts
 WHERE verdict = 'human';

COMMENT ON VIEW outbound_clicks_human IS
  'Outbound clicks of every via that framework/core/human_clicks.py counts as a person (per-row rules plus the >20/IP/UTC-day velocity cut). A /r/ click has a redirect row and a beacon row: never sum across via. For the goal series use outbound_clicks_verified_human.';

CREATE OR REPLACE VIEW outbound_clicks_verified_human AS
SELECT id, site_id, asin, tracking_id, source_page, source_kind, target,
       ip_address, user_agent, country, device_type, clicked_at,
       placement, session_pageviews, ms_on_page, per_ip_day
  FROM outbound_clicks_verdicts
 WHERE via = 'beacon'
   AND verdict = 'human';

COMMENT ON VIEW outbound_clicks_verified_human IS
  'Affiliate clicks made in a real JS-executing browser tab (via=beacon) that framework/core/human_clicks.py counts as a person: every per-row rule plus the >20/IP/UTC-day velocity cut. The goal metric source for human Amazon clicks.';

CREATE OR REPLACE VIEW amazon_clicks_verified_human_daily AS
SELECT (clicked_at AT TIME ZONE 'America/New_York')::date AS day,
       COUNT(*)                   AS clicks,
       COUNT(DISTINCT ip_address) AS people,
       COUNT(*) FILTER (WHERE source_kind = 'product') AS product_page_clicks
  FROM outbound_clicks_verified_human
 WHERE target = 'amazon'
 GROUP BY 1;

COMMENT ON VIEW amazon_clicks_verified_human_daily IS
  'Verified human Amazon clicks per day (America/New_York). Goals-tab metric: SELECT clicks FROM amazon_clicks_verified_human_daily WHERE day = CURRENT_DATE - 1.';
