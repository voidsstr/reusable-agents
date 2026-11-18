import * as path from 'path';
import { CHECK_IDS, type GateConfig, type PageFamily } from './manifestTypes';

/**
 * Loads the site's gate config (KIT_GATE_CONFIG: its tests/ui/gate.config.ts,
 * exporting `config` or a default). tests/playwright.base.ts sets the variable
 * from the site's playwright.config.ts, so the kit's specs run unchanged in
 * every site. The module is validated once per worker: a malformed family is a
 * gate failure with a message, not a silently skipped check.
 */

let cached: GateConfig | null = null;

export class GateConfigError extends Error {
  constructor(message: string) {
    super(`gate config: ${message}`);
    this.name = 'GateConfigError';
  }
}

/** The problems with a gate config; [] when it is usable. */
export function gateConfigProblems(cfg: unknown): string[] {
  const out: string[] = [];
  if (!cfg || typeof cfg !== 'object') return ['the module exports no config object'];
  const c = cfg as Partial<GateConfig>;
  if (!Array.isArray(c.families) || c.families.length === 0) return ['families: at least one page family is required'];
  const ids = new Set<string>();
  c.families.forEach((f: PageFamily, i) => {
    const where = `families[${i}]${f && typeof f.id === 'string' ? ` (${f.id})` : ''}`;
    if (!f || typeof f !== 'object') { out.push(`${where}: not an object`); return; }
    if (!/^[a-z0-9][a-z0-9-]*$/.test(String(f.id))) out.push(`${where}: id must be kebab-case`);
    if (ids.has(f.id)) out.push(`${where}: duplicate id`);
    ids.add(f.id);
    if (!Array.isArray(f.routes)) out.push(`${where}: routes must be an array`);
    if (typeof f.source !== 'function') out.push(`${where}: source must be a function`);
    if (!f.sourceLabel) out.push(`${where}: sourceLabel is required`);
    if (!Number.isInteger(f.sample) || f.sample < 1) out.push(`${where}: sample must be a positive integer`);
    if (f.shards !== undefined && (!Number.isInteger(f.shards) || f.shards < 1 || f.shards > f.sample)) {
      out.push(`${where}: shards must be 1..sample`);
    }
    if (f.robots !== 'index' && f.robots !== 'noindex') out.push(`${where}: robots must be 'index' or 'noindex'`);
    if (typeof f.monetized !== 'boolean' && !(f.monetized && typeof f.monetized === 'object' && f.monetized.most)) {
      out.push(`${where}: monetized must be a boolean or { most: reason }`);
    }
    for (const [check, reason] of Object.entries(f.exceptions ?? {})) {
      if (!(CHECK_IDS as readonly string[]).includes(check)) out.push(`${where}: exception for unknown check '${check}'`);
      if (!reason || !String(reason).trim()) out.push(`${where}: exception '${check}' needs a reason`);
    }
  });
  if (c.routes && typeof c.routes.declared !== 'function') out.push('routes.declared must be a function');
  out.push(...complianceSourceProblems(c.compliance));
  return out;
}

const reasonGiven = (v: unknown): boolean => typeof v === 'string' && v.trim().length > 0;

/**
 * The recall gate and the content gate are never cut (binding amendment 11):
 * a site names its recall source and its content-policy rule sets, or states
 * why it has none (recallsWaived / contentPolicyWaived). Missing both fails
 * the config, so the gate cannot pass with those checks quietly skipped.
 */
function complianceSourceProblems(cc: GateConfig['compliance']): string[] {
  const out: string[] = [];
  const c = cc ?? {};
  const recalls = c.recalledProducts;
  if (recalls !== undefined && typeof recalls !== 'function') out.push('compliance.recalledProducts must be a function returning the recalled SKUs');
  if (c.recallsWaived !== undefined && !reasonGiven(c.recallsWaived)) out.push('compliance.recallsWaived must be the reason, in words');
  if (typeof recalls === 'function' && c.recallsWaived !== undefined) out.push('compliance: set recalledProducts or recallsWaived, not both');
  if (recalls === undefined && !reasonGiven(c.recallsWaived)) {
    out.push('compliance.recalledProducts is required (the recall gate is never cut): read the recalled SKUs from the fixture DB, or set compliance.recallsWaived to the reason the site has none');
  }
  const cp = c.contentPolicy;
  if (cp && (!Array.isArray(cp.ruleSets) || cp.ruleSets.some((r) => !/^[A-Za-z0-9_.-]+$/.test(String(r))))) {
    out.push('compliance.contentPolicy.ruleSets must be a list of rule-set names');
  }
  const hasRuleSets = !!cp && Array.isArray(cp.ruleSets) && cp.ruleSets.length > 0;
  if (c.contentPolicyWaived !== undefined && !reasonGiven(c.contentPolicyWaived)) out.push('compliance.contentPolicyWaived must be the reason, in words');
  if (hasRuleSets && c.contentPolicyWaived !== undefined) out.push('compliance: set contentPolicy or contentPolicyWaived, not both');
  if (!hasRuleSets && !reasonGiven(c.contentPolicyWaived)) {
    out.push('compliance.contentPolicy.ruleSets is required (the content gate is never cut): name the rule sets the rendered pages must pass, or set compliance.contentPolicyWaived to the reason');
  }
  return out;
}

/** The site's gate config. Throws GateConfigError with every problem at once. */
export function loadGateConfig(): GateConfig {
  if (cached) return cached;
  const file = process.env.KIT_GATE_CONFIG;
  if (!file) {
    throw new GateConfigError('KIT_GATE_CONFIG is not set: run the gate through a playwright.config.ts built by kitPlaywrightConfig() (tests/playwright.base.ts)');
  }
  const abs = path.resolve(file);
  // Playwright's loader compiles the site's TypeScript on require, as it does a spec's imports.
  // eslint-disable-next-line @typescript-eslint/no-var-requires
  const mod = require(abs) as { config?: GateConfig; default?: GateConfig };
  const cfg = mod.config ?? mod.default;
  const problems = gateConfigProblems(cfg);
  if (problems.length) throw new GateConfigError(`${abs}:\n  ${problems.join('\n  ')}`);
  cached = cfg as GateConfig;
  return cached;
}
