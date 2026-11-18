/**
 * kit_0004 against framework/core/human_clicks.py, without a database: every
 * constant the view's per-row verdict and velocity rule embed (UA terms,
 * datacenter prefixes, scanner regex, stale-browser anchors and exemptions,
 * excluded countries, the per-IP-per-day cut, the order of the rules) must
 * equal what human_clicks.py derives from its DEFAULT_SPEC for the
 * outbound_clicks column spec. A change to the framework's lists that is not
 * carried into a new kit migration fails here. The constants are read from the
 * latest kit migration (by name) that defines kit_click_row_verdict, and the
 * velocity rule from the latest that defines outbound_clicks_verdicts, so a
 * kit_NNNN that replaces them is what is checked; kit_0004 is never edited.
 *
 * Needs python3 and the reusable-agents checkout the kit lives in (or
 * KIT_FRAMEWORK_ROOT); skipped otherwise (a kit vendored into a site). The
 * row-by-row comparison on a real database is views.pg.test.ts.
 */
import fs from 'fs';
import path from 'path';
import { frameworkRoot, pythonCan, runPythonJson } from './helpers/python';

const MIGRATIONS = path.resolve(__dirname, '..', '..', 'migrations');
const KIT_FILES = fs.readdirSync(MIGRATIONS).filter((f) => /^kit_\d{4}_.*\.sql$/.test(f)).sort();

/** The text of the latest kit migration that matches `defines` (the definition in force). */
function latestDefining(defines: RegExp): { file: string; sql: string } {
  for (const file of [...KIT_FILES].reverse()) {
    const sql = fs.readFileSync(path.join(MIGRATIONS, file), 'utf8');
    if (defines.test(sql)) return { file, sql };
  }
  throw new Error(`no kit migration matches ${defines}`);
}

/** The per-row verdict function: every constant but the velocity rule. */
const FN = latestDefining(/^CREATE OR REPLACE FUNCTION kit_click_row_verdict\(/m);
/** The verdicts view: the velocity cut, the UTC day and the IP key. */
const VIEW = latestDefining(/^CREATE OR REPLACE VIEW outbound_clicks_verdicts AS/m);
const SQL = FN.sql;

interface SpecDump {
  ua_regex: string;
  ip_regex: string;
  scanner_regex: string;
  exempt_regex: string;
  engine_regex: Record<string, string>;
  anchors: Record<string, [number, string]>;
  slack_majors: number;
  cadence_days: number;
  min_versions: Record<string, string>;
  exclude_countries: string[];
  max_clicks_per_ip_day: number;
  require_referer: boolean;
  self_referer_regex: string | null;
  verdict_order: string[];
}

const DUMP = `
import json, re
from framework.core import human_clicks as h
spec = h.resolve_spec({"time_col": "clicked_at", "referer_col": "source_page",
                       "country_col": "country", "bot_flag_col": "is_bot"}, config={})
sb = spec["stale_browser"]
case, _ = h._reason_case(spec)
print(json.dumps({
    "ua_regex": h._ua_regex(spec["bot_ua_terms"]),
    "ip_regex": h._ip_regex(spec["datacenter_ip_prefixes"]),
    "scanner_regex": spec["referer_exclude_regex"],
    "exempt_regex": sb["exempt_ua_regex"],
    "engine_regex": {**h._ENGINE_UA_PATTERN, **(sb.get("engine_ua_regex") or {})},
    "anchors": sb["anchors"],
    "slack_majors": sb["slack_majors"],
    "cadence_days": sb["cadence_days"],
    "min_versions": sb.get("min_versions") or {},
    "exclude_countries": [str(c).upper() for c in spec["exclude_countries"]],
    "max_clicks_per_ip_day": spec["max_clicks_per_ip_day"],
    "require_referer": spec["require_referer"],
    "self_referer_regex": spec["self_referer_regex"],
    "verdict_order": re.findall(r"THEN '([a-z-]+)'", case),
}))
`;

/** The single $re$-quoted constant right before `THEN '<verdict>'` (or before `context`). */
function constantBefore(context: string): string {
  const at = SQL.indexOf(context);
  if (at < 0) throw new Error(`kit_0004 has no ${context}`);
  const head = SQL.slice(0, at);
  const end = head.lastIndexOf('$re$');
  const start = head.lastIndexOf('$re$', end - 1);
  if (start < 0 || end < 0) throw new Error(`no $re$ constant before ${context}`);
  return head.slice(start + 4, end);
}

const ready = !!frameworkRoot() && pythonCan();
const describeFw = ready ? describe : describe.skip;

describeFw('kit_0004 matches framework/core/human_clicks.py (DEFAULT_SPEC, outbound_clicks columns)', () => {
  let spec: SpecDump;
  beforeAll(() => { spec = runPythonJson<SpecDump>(DUMP); });

  it('reads the latest definitions (kit_0004 until a later kit migration replaces them)', () => {
    expect(KIT_FILES[0]).toBe('kit_0001_outbound_clicks.sql');
    expect(FN.file >= 'kit_0004_views.sql').toBe(true);
    expect(VIEW.file >= 'kit_0004_views.sql').toBe(true);
  });

  it('checks the per-row rules in the framework\'s order, then velocity, then human', () => {
    const start = SQL.indexOf('CREATE OR REPLACE FUNCTION kit_click_row_verdict');
    const fn = SQL.slice(start, SQL.indexOf('$fn$;', start));
    const order = [...fn.matchAll(/THEN '([a-z-]+)'/g)].map((m) => m[1]);
    expect(order).toEqual(spec.verdict_order);
    expect(VIEW.sql).toMatch(/WHEN v\.row_verdict IS NOT NULL THEN v\.row_verdict\s+WHEN v\.per_ip_day > (\d+) THEN 'velocity'\s+ELSE 'human'/);
  });

  it('uses the same bot user-agent alternation', () => {
    expect(constantBefore("THEN 'ua-bot'")).toBe(spec.ua_regex);
  });

  it('uses the same datacenter prefixes', () => {
    expect(constantBefore("THEN 'datacenter-ip'")).toBe(spec.ip_regex);
  });

  it('uses the same scanner-referer regex', () => {
    expect(constantBefore("THEN 'scanner-referer'")).toBe(spec.scanner_regex);
  });

  it('uses the same stale-browser exemptions, engine patterns, anchors, cadence and slack', () => {
    expect(constantBefore(")\n    THEN 'stale-browser'")).toBe(spec.exempt_regex);
    const engines = [...SQL.matchAll(/COALESCE\(substring\(p_user_agent from \$re\$(.+?)\$re\$\)::int, 2147483647\)\s+< (\d+) \+ GREATEST\(0, \(now\(\) AT TIME ZONE 'UTC'\)::date - DATE '(\d{4}-\d{2}-\d{2})'\) \/ (\d+) - (\d+)/g)]
      .map((m) => ({ pattern: m[1], major: Number(m[2]), date: m[3], cadence: Number(m[4]), slack: Number(m[5]) }));
    expect(engines).toEqual(Object.entries(spec.anchors).map(([engine, [major, date]]) => ({
      pattern: spec.engine_regex[engine], major, date, cadence: spec.cadence_days, slack: spec.slack_majors,
    })));
  });

  it('has no rule the framework leaves off by default (Safari floor, self-referer) and requires a referer', () => {
    expect(spec.min_versions).toEqual({});
    expect(spec.self_referer_regex).toBeNull();
    expect(SQL).not.toMatch(/'self-referer'/);
    expect(spec.require_referer).toBe(true);
    expect(SQL).toContain("WHEN COALESCE(TRIM(p_referer), '') = '' THEN 'no-referer'");
  });

  it('excludes the same countries and cuts at the same clicks per IP per UTC day', () => {
    const countries = /upper\(COALESCE\(p_country, ''\)\) = ANY \(ARRAY\[([^\]]*)\]\)/.exec(SQL)?.[1] ?? '';
    expect(countries.split(',').map((c) => c.trim().replace(/^'|'$/g, '')).filter(Boolean)).toEqual(spec.exclude_countries);
    expect(Number(/WHEN v\.per_ip_day > (\d+) THEN 'velocity'/.exec(VIEW.sql)?.[1])).toBe(spec.max_clicks_per_ip_day);
    expect(VIEW.sql).toContain("(o.clicked_at AT TIME ZONE 'UTC')::date AS click_day");
    expect(VIEW.sql).toContain("split_part(CAST(o.ip_address AS text), '/', 1) AS ip_key");
  });
});
