/**
 * The kit_0004 verified-human views on a real Postgres (KIT_TEST_DATABASE_URL;
 * see helpers/pg.ts).
 *
 * The core check runs framework/core/human_clicks.py's own breakdown query
 * (per row, via group_col='id') against the same rows and requires the
 * view's verdict for every row to be identical: no second definition of a
 * person. The rows cover every rule, the order rules fire in, the velocity cut
 * across vias, UTC day boundaries under a non-UTC session, IPv6 and NULLs.
 */
import fs from 'fs';
import path from 'path';
import type { Pool } from 'pg';
import { runKitMigrations } from '../../server/src/db/migrate';
import { createScratchDb, describePg, type ScratchDb } from './helpers/pg';
import { frameworkRoot, pythonCan, runPythonJson } from './helpers/python';

const KIT_MIGRATIONS = path.resolve(__dirname, '..', '..', 'migrations');
const VIEWS_SQL = fs.readFileSync(path.join(KIT_MIGRATIONS, 'kit_0004_views.sql'), 'utf8');

const DAY_MS = 86_400_000;
const utcDate = (offsetDays: number) => new Date(Date.now() - offsetDays * DAY_MS).toISOString().slice(0, 10);
const DAY1 = utcDate(3);
const DAY2 = utcDate(2);

/** human_clicks' stale-browser floor for an anchor, today (UTC). */
function minMajor(anchorMajor: number, anchorDate: string, cadence = 28, slack = 16): number {
  const elapsed = Math.max(0, Math.floor((Date.parse(utcDate(0)) - Date.parse(anchorDate)) / DAY_MS));
  return anchorMajor + Math.floor(elapsed / cadence) - slack;
}
const CHROME_MIN = minMajor(140, '2025-09-02');
const FIREFOX_MIN = minMajor(142, '2025-08-19');
const chrome = (major: number) => `Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/${major}.0.0.0 Safari/537.36`;
const firefox = (major: number) => `Mozilla/5.0 (X11; Linux x86_64; rv:${major}.0) Gecko/20100101 Firefox/${major}.0`;
const FRESH = chrome(CHROME_MIN + 5);

interface Row {
  label: string;
  expect: string;
  via?: string;
  target?: string;
  is_bot?: boolean;
  user_agent?: string | null;
  ip_address?: string | null;
  country?: string | null;
  source_page?: string | null;
  source_kind?: string;
  clicked_at?: string;
}

const base = (label: string, expect: string, over: Partial<Row> = {}): Row => ({
  label, expect, via: 'beacon', target: 'amazon', is_bot: false, user_agent: FRESH, ip_address: `73.${label.length}.${label.charCodeAt(0)}.${label.charCodeAt(label.length - 1)}`,
  country: 'US', source_page: '/product/B0AAAAAAA1', source_kind: 'product', clicked_at: `${DAY1}T12:00:00Z`, ...over,
});

const ROWS: Row[] = [
  base('human', 'human'),
  base('human redirect twin', 'human', { via: 'redirect' }),
  base('human ipv6', 'human', { ip_address: '2001:db8::1' }),
  base('human null ip', 'human', { ip_address: null }),
  base('human ebay', 'human', { target: 'ebay' }),
  base('flagged', 'site-flagged-bot', { is_bot: true }),
  base('flagged beats no-ua', 'site-flagged-bot', { is_bot: true, user_agent: '' }),
  base('empty ua', 'no-ua', { user_agent: '' }),
  base('blank ua', 'no-ua', { user_agent: '   ' }),
  base('null ua', 'no-ua', { user_agent: null }),
  base('googlebot', 'ua-bot', { user_agent: 'Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)' }),
  base('python', 'ua-bot', { user_agent: 'python-requests/2.32.3' }),
  base('headless', 'ua-bot', { user_agent: chrome(CHROME_MIN + 5).replace('Chrome/', 'HeadlessChrome/') }),
  base('screaming frog', 'ua-bot', { user_agent: 'Screaming Frog SEO Spider/20.4' }),
  base('ua-bot beats datacenter', 'ua-bot', { user_agent: 'curl/8.5.0', ip_address: '43.1.2.3' }),
  base('stale chrome', 'stale-browser', { user_agent: chrome(CHROME_MIN - 1) }),
  base('chrome at the floor', 'human', { user_agent: chrome(CHROME_MIN) }),
  base('stale firefox', 'stale-browser', { user_agent: firefox(FIREFOX_MIN - 1) }),
  base('stale but samsung', 'human', { user_agent: `${chrome(100)} SamsungBrowser/20.0` }),
  base('singapore', 'excluded-country', { country: 'SG' }),
  base('singapore lower', 'excluded-country', { country: 'sg' }),
  base('tencent', 'datacenter-ip', { ip_address: '43.128.1.1' }),
  base('gcp', 'datacenter-ip', { ip_address: '35.1.2.3' }),
  base('alibaba', 'datacenter-ip', { ip_address: '47.79.10.10' }),
  base('not alibaba', 'human', { ip_address: '47.73.10.10' }),
  base('no referer', 'no-referer', { source_page: null }),
  base('empty referer', 'no-referer', { source_page: '' }),
  base('wp-admin', 'scanner-referer', { source_page: '/wp-admin/setup.php' }),
  base('sqli', 'scanner-referer', { source_page: "/product/x' or 1=1" }),
  base('traversal', 'scanner-referer', { source_page: '/x/../../etc/passwd' }),
];

// 11 beacon + 10 redirect rows from one IP on one UTC day: 21 rows that pass
// every per-row rule, so all 21 are velocity, whatever their via. A flagged row
// on the same IP and day does not count toward it.
for (let i = 0; i < 21; i++) {
  ROWS.push(base(`sprayer ${i}`, 'velocity', { ip_address: '98.1.1.1', via: i < 11 ? 'beacon' : 'redirect', clicked_at: `${DAY1}T10:${String(i).padStart(2, '0')}:00Z` }));
}
ROWS.push(base('sprayer flagged', 'site-flagged-bot', { ip_address: '98.1.1.1', is_bot: true }));
// Exactly 20 is still people.
for (let i = 0; i < 20; i++) ROWS.push(base(`twenty ${i}`, 'human', { ip_address: '98.2.2.2', clicked_at: `${DAY1}T11:${String(i).padStart(2, '0')}:00Z` }));
// 15 just before and 15 just after UTC midnight: two UTC days of 15, so people,
// even though they are one America/New_York evening.
for (let i = 0; i < 15; i++) {
  ROWS.push(base(`late ${i}`, 'human', { ip_address: '98.3.3.3', clicked_at: `${DAY1}T23:59:${String(10 + i).padStart(2, '0')}Z` }));
  ROWS.push(base(`early ${i}`, 'human', { ip_address: '98.3.3.3', clicked_at: `${DAY2}T00:00:${String(10 + i).padStart(2, '0')}Z` }));
}

async function insertRows(pool: Pool): Promise<Map<string, Row>> {
  const byId = new Map<string, Row>();
  for (const r of ROWS) {
    const res = await pool.query(
      `INSERT INTO outbound_clicks (site_id, asin, source_page, source_kind, target, ip_address, user_agent, country, clicked_at, is_bot, via)
       VALUES ('kit-test', 'B0AAAAAAA1', $1, $2, $3, $4::inet, $5, $6, $7, $8, $9) RETURNING id`,
      [r.source_page, r.source_kind, r.target, r.ip_address, r.user_agent, r.country, r.clicked_at, r.is_bot, r.via],
    );
    byId.set(String(res.rows[0].id), r);
  }
  return byId;
}

describePg('kit_0004 views (real Postgres)', () => {
  let db: ScratchDb;
  let pool: Pool;
  let byId: Map<string, Row>;

  beforeAll(async () => {
    db = await createScratchDb('kit_views');
    await runKitMigrations(db.pool({ max: 1 }), { log: () => {}, warn: () => {}, kitMigrationsDir: KIT_MIGRATIONS });
    // A session in another time zone must not move the UTC day.
    pool = db.pool({ max: 2, options: '-c TimeZone=America/New_York' });
    byId = await insertRows(pool);
  }, 60_000);
  afterAll(async () => { await db.drop(); });

  it('gives every row the verdict the test expects', async () => {
    const r = await pool.query('SELECT id::text AS id, verdict FROM outbound_clicks_verdicts');
    const wrong = r.rows.filter((x) => byId.get(x.id)!.expect !== x.verdict).map((x) => `${byId.get(x.id)!.label}: ${x.verdict}`);
    expect(wrong).toEqual([]);
    expect(r.rows).toHaveLength(ROWS.length);
  });

  const hcReady = !!frameworkRoot() && pythonCan(['psycopg2']);
  (hcReady ? it : it.skip)('gives every row exactly the verdict framework/core/human_clicks.py gives it', async () => {
    const framework = runPythonJson<Record<string, string>>(`
import json, os
import psycopg2
from framework.core import human_clicks as h
spec = h.resolve_spec({"time_col": "clicked_at", "referer_col": "source_page",
                       "country_col": "country", "bot_flag_col": "is_bot"}, config={})
sql, params = h.build_breakdown_query(spec, table="outbound_clicks", window_days=30, group_col="id")
conn = psycopg2.connect(os.environ["KIT_HC_DSN"])
with conn.cursor() as cur:
    cur.execute(sql, params)
    print(json.dumps({str(grp): verdict for verdict, grp, n in cur.fetchall()}))
`, { KIT_HC_DSN: db.url });
    const view = Object.fromEntries((await pool.query('SELECT id::text AS id, verdict FROM outbound_clicks_verdicts')).rows.map((x) => [x.id, x.verdict]));
    expect(Object.keys(framework).length).toBe(ROWS.length);
    const differ = Object.keys(view).filter((id) => view[id] !== framework[id]).map((id) => `${byId.get(id)!.label}: view=${view[id]} human_clicks=${framework[id]}`);
    expect(differ).toEqual([]);
  }, 60_000);

  it('outbound_clicks_verified_human is the human beacon rows; outbound_clicks_human every human row', async () => {
    const humanIds = [...byId.entries()].filter(([, r]) => r.expect === 'human').map(([id]) => id).sort();
    const beaconIds = [...byId.entries()].filter(([, r]) => r.expect === 'human' && r.via === 'beacon').map(([id]) => id).sort();
    const human = (await pool.query('SELECT id::text AS id FROM outbound_clicks_human')).rows.map((x) => x.id).sort();
    const verified = (await pool.query('SELECT id::text AS id FROM outbound_clicks_verified_human')).rows.map((x) => x.id).sort();
    expect(human).toEqual(humanIds);
    expect(verified).toEqual(beaconIds);
    const perIp = await pool.query(`SELECT DISTINCT per_ip_day FROM outbound_clicks_verified_human WHERE ip_address = '98.2.2.2'`);
    expect(perIp.rows).toEqual([{ per_ip_day: '20' }]);
  });

  it('amazon_clicks_verified_human_daily counts verified Amazon clicks per America/New_York day', async () => {
    const want = new Map<string, { clicks: number; ips: Set<string>; product: number }>();
    for (const r of byId.values()) {
      if (r.expect !== 'human' || r.via !== 'beacon' || r.target !== 'amazon') continue;
      const day = new Date(r.clicked_at!).toLocaleDateString('en-CA', { timeZone: 'America/New_York' });
      const d = want.get(day) ?? { clicks: 0, ips: new Set<string>(), product: 0 };
      d.clicks += 1;
      if (r.ip_address) d.ips.add(r.ip_address);
      if (r.source_kind === 'product') d.product += 1;
      want.set(day, d);
    }
    const got = await pool.query(`SELECT to_char(day, 'YYYY-MM-DD') AS day, clicks::int, people::int, product_page_clicks::int FROM amazon_clicks_verified_human_daily ORDER BY day`);
    expect(got.rows).toEqual([...want.entries()].sort().map(([day, d]) => ({ day, clicks: d.clicks, people: d.ips.size, product_page_clicks: d.product })));
  });

  it('kit_click_row_verdict is usable on its own (another click table, ad-hoc checks)', async () => {
    const v = async (args: [boolean | null, string | null, string | null, string | null, string | null]) =>
      (await pool.query('SELECT kit_click_row_verdict($1, $2, $3, $4, $5) AS v', args)).rows[0].v;
    expect(await v([false, FRESH, '73.1.1.1', 'US', '/guides/x'])).toBeNull();
    expect(await v([null, FRESH, '73.1.1.1/32', 'US', '/guides/x'])).toBeNull();
    expect(await v([false, FRESH, '13.1.1.1/32', 'US', '/guides/x'])).toBe('datacenter-ip');
    expect(await v([false, chrome(CHROME_MIN - 1), '73.1.1.1', 'US', '/x'])).toBe('stale-browser');
  });

  it('a click_day filter reaches the table scan (below the velocity window); clicked_at alone cannot', async () => {
    // count(*) FILTER (verdict) keeps the WindowAgg in the plan.
    const plan = async (where: string) => JSON.stringify((await pool.query(
      `EXPLAIN (FORMAT JSON) SELECT count(*) FILTER (WHERE verdict = 'human') FROM outbound_clicks_verdicts WHERE ${where}`)).rows[0]['QUERY PLAN']);
    const scanFilter = (p: string) => {
      const scans: string[] = [];
      const walk = (n: any) => {
        if (n && typeof n === 'object') {
          if (n['Relation Name'] === 'outbound_clicks') scans.push(String(n.Filter ?? ''));
          for (const v of Object.values(n)) walk(v);
        }
      };
      walk(JSON.parse(p));
      return scans.join(' | ');
    };
    const byDay = await plan(`click_day > (now() AT TIME ZONE 'UTC')::date - 30`);
    expect(byDay).toMatch(/"Node Type":\s*"WindowAgg"/);
    expect(scanFilter(byDay)).toMatch(/AT TIME ZONE/);
    const byTime = await plan(`clicked_at > now() - interval '30 days'`);
    expect(byTime).toMatch(/"Node Type":\s*"WindowAgg"/);
    expect(scanFilter(byTime)).not.toMatch(/clicked_at > /);
  });

  it('the per-row verdict function is inlined into the view (no per-row function call)', async () => {
    const r = await pool.query(`EXPLAIN (VERBOSE, FORMAT TEXT) SELECT verdict FROM outbound_clicks_verdicts`);
    const text = r.rows.map((x) => x['QUERY PLAN']).join('\n');
    expect(text).not.toMatch(/kit_click_row_verdict\(/);
    expect(text).toMatch(/site-flagged-bot/);
  });

  it('documents what the view cannot see (storage overrides) in its header', () => {
    expect(VIEWS_SQL).toMatch(/storage config\/human-click-filter-config\.json/);
  });
});
