import { spawn } from 'child_process';
import * as fs from 'fs';
import * as path from 'path';

/**
 * Runs the framework's content-policy scanner (framework/cli/content_policy_scan.py,
 * build item F6a) over rendered pages: the gate's backstop for the electrical
 * and medical content rules, after the publish gate that keeps a violating
 * article from going live at all.
 *
 * Hermetic by construction: --no-storage (no blob config, no network), the
 * rule sets named explicitly (a site's gate config lists them), an optional
 * --config overlay, and the pages handed over as files.
 */

export const SCANNER_MODULE = 'framework.cli.content_policy_scan';
const SCANNER_FILE = path.join('framework', 'cli', 'content_policy_scan.py');

export interface ScanPage {
  /** The page's site path (how problems are reported). */
  name: string;
  html: string;
}

export interface ScanOptions {
  ruleSets: readonly string[];
  frameworkDir: string;
  /** Scratch directory for the page files and the JSON report. */
  workDir: string;
  configFile?: string;
  site?: string;
  python?: string;
  timeoutMs?: number;
}

export interface ScanResult {
  /** The scanner's exit code: 0 clean, 1 violation, 2 usage/config error, 3 nothing scanned; -1 did not run. */
  exitCode: number;
  /** One line per reject-level violation (or per page error), naming the page. */
  problems: string[];
  /** Pages the scanner reports as scanned. */
  scanned: number;
  /** The scanner's stderr tail (its summary line, config errors). */
  stderr: string;
}

/** True when `dir` holds the scanner. */
export function hasScanner(dir: string | undefined | null): dir is string {
  return !!dir && fs.existsSync(path.join(dir, SCANNER_FILE));
}

/**
 * The reusable-agents checkout to run the scanner from: the configured dir,
 * KIT_GATE_FRAMEWORK_DIR, REUSABLE_AGENTS_DIR, then the checkout this kit
 * lives in (kits/affiliate-site inside reusable-agents). null when none has it.
 */
export function frameworkDirFor(configured?: string | null, env: NodeJS.ProcessEnv = process.env, kitDir = path.resolve(__dirname, '..', '..', '..')): string | null {
  for (const c of [configured, env.KIT_GATE_FRAMEWORK_DIR, env.REUSABLE_AGENTS_DIR]) {
    if (c && hasScanner(path.resolve(c))) return path.resolve(c);
  }
  let at = kitDir;
  for (let i = 0; i < 6; i++) {
    if (hasScanner(at)) return at;
    const up = path.dirname(at);
    if (up === at) break;
    at = up;
  }
  return null;
}

/** A file name for a page path: readable, unique, and never a path traversal. */
export function pageFileName(index: number, name: string): string {
  const slug = name.replace(/^\/+/, '').replace(/[^A-Za-z0-9._-]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 80) || 'home';
  return `${String(index).padStart(4, '0')}-${slug}.html`;
}

interface ReportViolation { rule_id?: string; label?: string; excerpt?: string; kind?: string }
interface ReportPage { url?: string; error?: string | null; rejects?: ReportViolation[] }

export async function runContentPolicyScan(pages: readonly ScanPage[], opts: ScanOptions): Promise<ScanResult> {
  const pagesDir = path.join(opts.workDir, 'pages');
  fs.mkdirSync(pagesDir, { recursive: true });
  const byFile = new Map<string, string>();
  pages.forEach((p, i) => {
    const file = path.join(pagesDir, pageFileName(i, p.name));
    fs.writeFileSync(file, p.html, 'utf8');
    byFile.set(path.resolve(file), p.name);
  });
  const report = path.join(opts.workDir, 'content-policy-report.json');
  const args = ['-m', SCANNER_MODULE, '--no-storage', '--quiet', '--report-json', report];
  for (const r of opts.ruleSets) args.push('--rule-set', r);
  if (opts.configFile) args.push('--config', path.resolve(opts.configFile));
  if (opts.site) args.push('--site', opts.site);
  args.push(pagesDir);

  const { code, stderr } = await new Promise<{ code: number; stderr: string }>((resolve) => {
    const child = spawn(opts.python ?? 'python3', args, {
      cwd: opts.frameworkDir,
      env: { ...process.env, PYTHONPATH: opts.frameworkDir, PYTHONDONTWRITEBYTECODE: '1' },
      stdio: ['ignore', 'ignore', 'pipe'],
    });
    let err = '';
    child.stderr.on('data', (d: Buffer) => { err = (err + d.toString('utf8')).slice(-8000); });
    const timer = setTimeout(() => child.kill('SIGKILL'), opts.timeoutMs ?? 120_000);
    child.on('error', (e) => { clearTimeout(timer); resolve({ code: -1, stderr: `${err}\n${e.message}` }); });
    child.on('close', (c) => { clearTimeout(timer); resolve({ code: c ?? -1, stderr: err }); });
  });

  const problems: string[] = [];
  let scanned = 0;
  if (fs.existsSync(report)) {
    try {
      const data = JSON.parse(fs.readFileSync(report, 'utf8')) as { pages?: ReportPage[]; pages_scanned?: number };
      scanned = Number(data.pages_scanned ?? 0);
      for (const pg of data.pages ?? []) {
        const name = byFile.get(path.resolve(String(pg.url ?? ''))) ?? String(pg.url ?? '?');
        if (pg.error) problems.push(`${name}: could not be scanned: ${pg.error}`);
        for (const v of pg.rejects ?? []) {
          const what = v.kind === 'notice' ? `missing ${v.label}; triggered by` : `${v.label}:`;
          problems.push(`${name}: [${v.rule_id}] ${what} "${String(v.excerpt ?? '').slice(0, 160)}"`);
        }
      }
    } catch (e) {
      problems.push(`the scanner's report did not parse: ${e instanceof Error ? e.message : String(e)}`);
    }
  }
  if (code !== 0 && !problems.length) problems.push(`content_policy_scan exited ${code}: ${stderr.trim().split('\n').slice(-3).join(' | ')}`);
  return { exitCode: code, problems, scanned, stderr };
}
