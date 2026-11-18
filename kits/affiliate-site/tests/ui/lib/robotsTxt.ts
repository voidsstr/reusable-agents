/**
 * robots.txt as crawlers read it (RFC 9309), and the kit's crawler policy
 * checked against it: search engines and assistants' answer-time fetchers
 * must be able to read every page that earns money; training crawlers are
 * blocked unless a site says why not; the site's private paths are closed to
 * everyone; the sitemap is announced.
 *
 * Group rules follow RFC 9309: a crawler obeys the group(s) naming its
 * product token (case-insensitive), else the `*` group; within the rules the
 * longest matching path wins and Allow wins a tie; `*` and a trailing `$`
 * are wildcards. ai-fetcher-access.spec.ts (ported from SpecPicks) reads the
 * same parse.
 */

export interface RobotsRule {
  allow: boolean;
  path: string;
}

export interface RobotsGroup {
  agents: string[];
  rules: RobotsRule[];
}

export interface ParsedRobots {
  groups: RobotsGroup[];
  sitemaps: string[];
}

export function parseRobots(text: string): ParsedRobots {
  const groups: RobotsGroup[] = [];
  const sitemaps: string[] = [];
  let cur: RobotsGroup | null = null;
  let lastWasAgent = false;
  for (const raw of String(text).split(/\r?\n/)) {
    const line = raw.replace(/#.*/, '').trim();
    if (!line) continue;
    const m = line.match(/^([A-Za-z-]+)\s*:\s*(.*)$/);
    if (!m) continue;
    const key = (m[1] as string).toLowerCase();
    const value = (m[2] as string).trim();
    if (key === 'user-agent') {
      if (!cur || !lastWasAgent) {
        cur = { agents: [], rules: [] };
        groups.push(cur);
      }
      cur.agents.push(value);
      lastWasAgent = true;
      continue;
    }
    if (key === 'sitemap') {
      sitemaps.push(value);
      continue;
    }
    lastWasAgent = false;
    if ((key === 'allow' || key === 'disallow') && cur) {
      // "Disallow:" with no path allows everything: it adds no rule.
      if (value) cur.rules.push({ allow: key === 'allow', path: value });
    }
  }
  return { groups, sitemaps };
}

/** The rules a crawler with `token` obeys, and which group they came from. */
export function rulesFor(parsed: ParsedRobots, token: string): { from: 'own' | '*' | 'none'; rules: RobotsRule[] } {
  const t = token.toLowerCase();
  const own = parsed.groups.filter((g) => g.agents.some((a) => a.toLowerCase() === t));
  if (own.length) return { from: 'own', rules: own.flatMap((g) => g.rules) };
  const star = parsed.groups.filter((g) => g.agents.some((a) => a === '*'));
  if (star.length) return { from: '*', rules: star.flatMap((g) => g.rules) };
  return { from: 'none', rules: [] };
}

function patternRe(path: string): RegExp {
  const anchored = path.endsWith('$');
  const body = (anchored ? path.slice(0, -1) : path).split('*').map((s) => s.replace(/[.+?^${}()|[\]\\]/g, '\\$&')).join('.*');
  return new RegExp(`^${body}${anchored ? '$' : ''}`);
}

/** Whether `path` may be fetched under `rules` (no matching rule: allowed). */
export function isAllowed(rules: readonly RobotsRule[], path: string): boolean {
  let best: RobotsRule | null = null;
  for (const r of rules) {
    if (!patternRe(r.path).test(path)) continue;
    if (!best || r.path.length > best.path.length || (r.path.length === best.path.length && r.allow && !best.allow)) best = r;
  }
  return best ? best.allow : true;
}

export interface RobotsPolicy {
  /** Tokens that must be able to read keyPaths. */
  mustAllow: readonly string[];
  /** Tokens that must be blocked from the whole site (empty when a site lets training crawlers in). */
  mustBlock: readonly string[];
  /** Paths every allowed crawler must be able to read. */
  keyPaths: readonly string[];
  /** Paths closed to everyone (site.json crawlers.disallowPaths). */
  disallowPaths: readonly string[];
  /** The sitemap URL robots.txt must announce. */
  sitemapUrl: string;
}

export function robotsProblems(text: string, policy: RobotsPolicy): string[] {
  const out: string[] = [];
  const parsed = parseRobots(text);
  if (!parsed.groups.some((g) => g.agents.includes('*'))) out.push('robots.txt has no User-agent: * group');
  for (const token of policy.mustAllow) {
    const { from, rules } = rulesFor(parsed, token);
    const blocked = policy.keyPaths.filter((p) => !isAllowed(rules, p));
    if (blocked.length) out.push(`${token} (${from === 'own' ? 'its own group' : 'the * group'}) is blocked from ${blocked.slice(0, 5).join(', ')}`);
  }
  for (const token of policy.mustBlock) {
    const { from, rules } = rulesFor(parsed, token);
    if (isAllowed(rules, '/')) out.push(`training crawler ${token} is not blocked (${from === 'own' ? 'its own group allows /' : 'no group of its own; the * group lets it in'})`);
  }
  // The private paths (the click redirect, the API) are closed to everyone. Groups do
  // not inherit from '*', so every group an allowed crawler obeys must close them too.
  const star = rulesFor(parsed, '*');
  const probes = policy.disallowPaths.map((p) => ({ p, probe: p.endsWith('/') ? `${p}x` : p }));
  for (const { p, probe } of probes) {
    if (star.from !== 'none' && isAllowed(star.rules, probe)) out.push(`${p} is not disallowed for User-agent: *`);
  }
  for (const token of policy.mustAllow) {
    const { from, rules } = rulesFor(parsed, token);
    if (from !== 'own') continue;
    const open = probes.filter(({ probe }) => isAllowed(rules, probe)).map(({ p }) => p);
    if (open.length) out.push(`${open.join(', ')} not disallowed for ${token} (its own group does not inherit the * group's Disallow lines)`);
  }
  if (!parsed.sitemaps.includes(policy.sitemapUrl)) {
    out.push(`no "Sitemap: ${policy.sitemapUrl}" line (found ${parsed.sitemaps.length ? parsed.sitemaps.join(', ') : 'none'})`);
  }
  return out;
}

/** The Disallow lines of the group that names `token` (null when it has no group of its own). SpecPicks' robotsGroup(). */
export function robotsGroup(text: string, token: string): string[] | null {
  const parsed = parseRobots(text);
  const own = parsed.groups.filter((g) => g.agents.some((a) => a.toLowerCase() === token.toLowerCase()));
  return own.length ? own.flatMap((g) => g.rules.filter((r) => !r.allow).map((r) => r.path)) : null;
}

export type CrawlerClassName = 'search' | 'agent' | 'training';
const CRAWLER_CLASS_NAMES: readonly CrawlerClassName[] = ['search', 'agent', 'training'];

export interface CrawlerFloor {
  /** Every token the gate holds the site to, by the class it is checked as. */
  byClass: Record<CrawlerClassName, string[]>;
  /** Kit tokens the site lists under another class ("Amazonbot: training -> search"), for the run's annotations. */
  reclassified: string[];
}

/**
 * The crawler tokens robots.txt is checked against: the kit's default lists
 * united with the site's (site.json crawlers.userAgents). A site may add
 * tokens, and may move a kit token to another class by listing it there (the
 * move is reported), but it cannot drop one: a kit training crawler the site
 * leaves out of every list is still checked as a training crawler, so leaving
 * GPTBot out of the list cannot also take it out of the check.
 */
export function crawlerFloor(
  site: Readonly<Record<CrawlerClassName, readonly string[]>>,
  kit: Readonly<Record<CrawlerClassName, readonly string[]>>,
): CrawlerFloor {
  const siteClass = new Map<string, CrawlerClassName>();
  for (const cls of CRAWLER_CLASS_NAMES) for (const t of site[cls] ?? []) siteClass.set(t.toLowerCase(), cls);
  const chosen = new Map<string, { token: string; cls: CrawlerClassName }>();
  const reclassified: string[] = [];
  for (const cls of CRAWLER_CLASS_NAMES) {
    for (const t of kit[cls] ?? []) {
      const own = siteClass.get(t.toLowerCase());
      if (own && own !== cls) reclassified.push(`${t}: ${cls} -> ${own}`);
      chosen.set(t.toLowerCase(), { token: t, cls: own ?? cls });
    }
  }
  for (const cls of CRAWLER_CLASS_NAMES) for (const t of site[cls] ?? []) chosen.set(t.toLowerCase(), { token: t, cls });
  const byClass: Record<CrawlerClassName, string[]> = { search: [], agent: [], training: [] };
  for (const { token, cls } of chosen.values()) byClass[cls].push(token);
  return { byClass, reclassified };
}
