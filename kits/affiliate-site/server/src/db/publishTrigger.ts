/**
 * Renders migrations/templates/publish_trigger.sql.tmpl: the editorial_articles
 * publish guard a content_policy site installs as one of its own migrations
 * (affiliate-site kit design, binding amendment 1; build item K4).
 *
 * A write by any role other than the publisher can never make a row published:
 * its INSERTs become drafts with published_at NULL (the gate stamps the first
 * go-live time), an UPDATE that asks for 'published' on a row that was not
 * published keeps the status it had (a queued draft stays queued, a held row
 * such as 'archived' stays held until a write asks for the draft status), and
 * an edit to a published row's content columns sends it back to draft so the
 * publish gate re-checks it. No such UPDATE clears a published_at once set.
 * Housekeeping writes to a published row (view_count on every page view, hero
 * swaps, featured rotation, trend scores) leave it published.
 *
 * Two triggers, both ENABLE ALWAYS so replica mode cannot skip them: the BEFORE
 * guard applies those rules, and an AFTER recheck raises when the row as
 * written breaks them anyway (a BEFORE trigger that sorts after the guard
 * changed it, such as one a role holding the TRIGGER privilege created, or
 * swapped in for the guard, to publish its own writes), when a write by the
 * publisher changed published content, and when the publisher writes while any
 * trigger on the table runs a function that neither the table owner (or a
 * member of it) nor a superuser owns: code a TRIGGER holder plants runs as the
 * publisher during that write, so nothing is published until it is gone. The
 * recheck is a NOT DEFERRABLE constraint trigger, which CREATE OR REPLACE
 * TRIGGER (all the TRIGGER privilege allows) cannot swap; the role contract
 * (template header) still forbids the privilege, which can replace the guard
 * and so stop every write the guard would have drafted. The template's header
 * states the rules and the role requirements in full; installing it warns
 * about later-sorting BEFORE triggers, TRIGGER holders and
 * session_replication_role setters.
 *
 * The template's placeholders are a contract: framework/tests/
 * test_article_publish_gate.py renders it itself with exactly the six names
 * publishTriggerValues() returns, so a new placeholder breaks that test.
 *
 * Usage (a site's scaffold or S2). draftStatus must be the publish gate's
 * publish_gate.draft_status, whose default is 'pending' (this module's is
 * 'draft'); the gate refuses to write against a guard rendered for another:
 *
 *   import { renderPublishTrigger } from '../vendor/affiliate-kit/server/src/db';
 *   fs.writeFileSync('db/migrations/0009_publish_guard.sql',
 *                    renderPublishTrigger({ publisherRole: 'site_publisher', draftStatus: 'pending' }));
 *
 * or `node <kit>/dist/db/publishTrigger.js --publisher-role site_publisher --draft-status pending --out <file>`.
 *
 * Every value is validated before it is substituted (identifiers, statuses),
 * so the template's string literals can never be broken out of.
 */
import fs from 'fs';
import path from 'path';
import { describeCliArg, kitMigrationsDir } from './migrate';

/** The template, relative to the kit's migrations directory. */
export const PUBLISH_TRIGGER_TEMPLATE = path.join('templates', 'publish_trigger.sql.tmpl');

/**
 * Names the rendered migration creates. The publish gate verifies the guard by
 * its function name (publish_gate.guard_function, default
 * '{table}_publish_guard').
 */
export const PUBLISH_GUARD_FUNCTION = 'editorial_articles_publish_guard';
export const PUBLISH_GUARD_TRIGGER = 'editorial_articles_zzz_publish_guard_trg';
export const PUBLISH_RECHECK_FUNCTION = 'editorial_articles_publish_recheck';
export const PUBLISH_RECHECK_TRIGGER = 'editorial_articles_zzz_publish_recheck_trg';

export const DEFAULT_DRAFT_STATUS = 'draft';
export const DEFAULT_PUBLISHED_STATUS = 'published';

/**
 * Every column the publish gate's checks read: the text (title, subtitle,
 * excerpt, body_md, faqs), outbound_citations, and category, which selects the
 * claims guard's and the link guard's per-bucket policy (the gate reads
 * `bucket`, else `category`; a site that adds a `bucket` column lists it too).
 * A change to any of them on a published row returns it to the gate. The
 * kit's updated_at trigger treats the text columns, not category, as content.
 */
export const DEFAULT_CONTENT_COLUMNS: readonly string[] = Object.freeze([
  'title', 'subtitle', 'excerpt', 'body_md', 'faqs', 'outbound_citations', 'category',
]);

export interface PublishTriggerParams {
  /** The role the publish gate logs in as; the only role that can publish. */
  publisherRole: string;
  /** Status a forced write gets (default 'draft'; the gate's draft_status). */
  draftStatus?: string;
  /** The one status that renders publicly (default 'published'; the gate's published_status). */
  publishedStatus?: string;
  /** Columns whose change on a published row sends it back to draft (default DEFAULT_CONTENT_COLUMNS). */
  contentColumns?: readonly string[];
}

export interface RenderPublishTriggerOptions {
  /** Template text (default: read from the kit's migrations directory). */
  template?: string;
  /** The kit's migrations directory (default kitMigrationsDir()). */
  migrationsDir?: string;
  /** Written into the header (default: the kit's VERSION file). */
  kitVersion?: string;
}

/** Invalid parameters or template; `issues` lists every problem. */
export class PublishTriggerError extends Error {
  constructor(readonly issues: string[]) {
    super(`publish trigger: ${issues.join('; ')}`);
    this.name = 'PublishTriggerError';
  }
}

const ROLE_RE = /^[a-z_][a-z0-9_]{0,62}$/;
const COLUMN_RE = /^[a-z_][a-z0-9_]{0,62}$/;
/** editorial_articles.status is VARCHAR(20). */
const STATUS_RE = /^[a-z][a-z0-9_-]{0,19}$/;
/** Names a login role can never have, or that would make the guard meaningless. */
const RESERVED_ROLES = new Set(['public', 'none', 'current_user', 'current_role', 'session_user', 'user', 'all']);

/** Every problem with `params` (empty when they are valid). */
export function validatePublishTriggerParams(params: PublishTriggerParams): string[] {
  const issues: string[] = [];
  const role = params?.publisherRole;
  if (typeof role !== 'string' || !role) {
    issues.push('publisherRole is required');
  } else if (!ROLE_RE.test(role)) {
    issues.push(`publisherRole ${JSON.stringify(role)} must be a lower-case SQL identifier (a-z, 0-9, _; at most 63 characters)`);
  } else if (RESERVED_ROLES.has(role) || role.startsWith('pg_')) {
    issues.push(`publisherRole ${JSON.stringify(role)} is reserved`);
  }
  const draft = params?.draftStatus ?? DEFAULT_DRAFT_STATUS;
  const published = params?.publishedStatus ?? DEFAULT_PUBLISHED_STATUS;
  for (const [name, v] of [['draftStatus', draft], ['publishedStatus', published]] as const) {
    if (typeof v !== 'string' || !STATUS_RE.test(v)) {
      issues.push(`${name} ${JSON.stringify(v)} must be 1-20 characters of a-z, 0-9, '_' or '-', starting with a letter`);
    }
  }
  if (draft === published) issues.push('draftStatus and publishedStatus must differ');
  const cols = params?.contentColumns ?? DEFAULT_CONTENT_COLUMNS;
  if (!Array.isArray(cols) || cols.length === 0) {
    issues.push('contentColumns must list at least one column');
  } else {
    const seen = new Set<string>();
    for (const c of cols) {
      if (typeof c !== 'string' || !COLUMN_RE.test(c)) {
        issues.push(`content column ${JSON.stringify(c)} must be a lower-case SQL identifier`);
        continue;
      }
      if (c === 'status') issues.push("content column 'status' is the gated column itself");
      // The gate's promote sets published_at, and the recheck refuses a publisher write that changes content.
      if (c === 'published_at') issues.push("content column 'published_at' is the go-live time the publish gate stamps; as content it would make every promote fail");
      if (seen.has(c)) issues.push(`content column ${c} is listed twice`);
      seen.add(c);
    }
  }
  return issues;
}

/**
 * Replace every `{{ name }}` in `template` with `values[name]`. Throws
 * PublishTriggerError naming any placeholder without a value. Values are
 * inserted verbatim: validate them first.
 */
export function renderSqlTemplate(template: string, values: Readonly<Record<string, string>>): string {
  const missing = new Set<string>();
  const out = template.replace(/\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}/g, (_m, name: string) => {
    if (!Object.prototype.hasOwnProperty.call(values, name)) {
      missing.add(name);
      return '';
    }
    return values[name];
  });
  if (missing.size) throw new PublishTriggerError([...missing].sort().map((m) => `template placeholder {{${m}}} has no value`));
  if (/\{\{|\}\}/.test(out)) throw new PublishTriggerError(['template has a malformed {{ }} placeholder']);
  return out;
}

function readKitVersion(migrationsDir: string): string {
  try {
    const v = fs.readFileSync(path.join(migrationsDir, '..', 'VERSION'), 'utf8').trim();
    return /^\d+\.\d+\.\d+$/.test(v) ? v : 'unversioned';
  } catch {
    return 'unversioned';
  }
}

/** The placeholder values for valid params (throws PublishTriggerError otherwise). */
export function publishTriggerValues(params: PublishTriggerParams, kitVersion = 'unversioned'): Record<string, string> {
  const issues = validatePublishTriggerParams(params);
  if (issues.length) throw new PublishTriggerError(issues);
  const cols = [...(params.contentColumns ?? DEFAULT_CONTENT_COLUMNS)];
  return {
    kit_version: /^[0-9A-Za-z.+-]{1,40}$/.test(kitVersion) ? kitVersion : 'unversioned',
    publisher_role: params.publisherRole,
    draft_status: params.draftStatus ?? DEFAULT_DRAFT_STATUS,
    published_status: params.publishedStatus ?? DEFAULT_PUBLISHED_STATUS,
    content_columns: cols.map((c) => `'${c}'`).join(', '),
    content_columns_plain: cols.join(', '),
  };
}

/** The template's path in the kit. */
export function publishTriggerTemplatePath(migrationsDir: string = kitMigrationsDir()): string {
  return path.join(migrationsDir, PUBLISH_TRIGGER_TEMPLATE);
}

/** The publish guard migration for `params`, ready to save as a site migration. */
export function renderPublishTrigger(params: PublishTriggerParams, opts: RenderPublishTriggerOptions = {}): string {
  const needsDir = opts.template === undefined || opts.kitVersion === undefined;
  const dir = needsDir ? (opts.migrationsDir ?? kitMigrationsDir()) : '';
  const template = opts.template ?? fs.readFileSync(publishTriggerTemplatePath(dir), 'utf8');
  const version = opts.kitVersion ?? readKitVersion(dir);
  return renderSqlTemplate(template, publishTriggerValues(params, version));
}

// ─── Command line ──────────────────────────────────────────────────────────

export const PUBLISH_TRIGGER_USAGE = `usage: publishTrigger --publisher-role <role> [options]

Render the editorial_articles publish guard (migrations/templates/
publish_trigger.sql.tmpl) for a content_policy site. Save the output as a NEW
site migration; never edit an applied one.

  --publisher-role <role>      the role the publish gate logs in as (required)
  --draft-status <status>      default: draft
  --published-status <status>  default: published
  --content-columns <a,b,...>  default: ${DEFAULT_CONTENT_COLUMNS.join(',')}
  --out <file>                 write here instead of stdout (refuses to overwrite)
  -h, --help                   this text`;

export interface PublishTriggerCliDeps {
  cwd?: string;
  stdout?: (text: string) => void;
  stderr?: (line: string) => void;
  render?: (params: PublishTriggerParams) => string;
}

/** `publishTrigger` command line. Returns the exit code (0 ok, 2 usage or invalid parameters). */
export function publishTriggerCli(argv: string[], deps: PublishTriggerCliDeps = {}): number {
  const out = deps.stdout ?? ((text: string) => process.stdout.write(text));
  const err = deps.stderr ?? ((line: string) => console.error(line));
  const params: PublishTriggerParams = { publisherRole: '' };
  let outFile: string | undefined;
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '-h' || a === '--help') {
      out(`${PUBLISH_TRIGGER_USAGE}\n`);
      return 0;
    }
    const v = argv[i + 1];
    const takes = ['--publisher-role', '--draft-status', '--published-status', '--content-columns', '--out'];
    if (!takes.includes(a)) {
      err(`publish trigger: unknown argument ${describeCliArg(a, i)}`);
      err(PUBLISH_TRIGGER_USAGE);
      return 2;
    }
    if (v === undefined || v.startsWith('--')) {
      err(`publish trigger: ${a} needs a value`);
      return 2;
    }
    i++;
    if (a === '--publisher-role') params.publisherRole = v;
    else if (a === '--draft-status') params.draftStatus = v;
    else if (a === '--published-status') params.publishedStatus = v;
    else if (a === '--content-columns') params.contentColumns = v.split(',').map((c) => c.trim()).filter(Boolean);
    else outFile = v;
  }
  let sql: string;
  try {
    sql = (deps.render ?? ((p: PublishTriggerParams) => renderPublishTrigger(p)))(params);
  } catch (e: any) {
    err(String(e?.message || e));
    return 2;
  }
  if (outFile) {
    const target = path.resolve(deps.cwd ?? process.cwd(), outFile);
    try {
      fs.writeFileSync(target, sql, { flag: 'wx' });
    } catch (e: any) {
      err(`publish trigger: cannot write ${target}: ${e?.code === 'EEXIST' ? 'it already exists' : String(e?.message || e)}`);
      return 2;
    }
    err(`wrote ${target}`);
    return 0;
  }
  out(sql);
  return 0;
}

if (require.main === module) {
  process.exit(publishTriggerCli(process.argv.slice(2)));
}
