/**
 * The bridge from kit-compliance to the framework's content-policy scanner
 * (F6a's framework/cli/content_policy_scan.py): it finds the checkout, hands
 * over the rendered pages as files, runs offline (--no-storage) with the
 * site's rule sets and maps the report back to page paths. Runs the real
 * scanner on the fixture site's rule set; no network, no LLM.
 */
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { frameworkDirFor, hasScanner, pageFileName, runContentPolicyScan } from '../ui/lib/contentPolicyScan';

const KIT = path.resolve(__dirname, '..', '..');
const FRAMEWORK = frameworkDirFor(null, {});
const CONFIG = path.join(KIT, 'fixture-site', 'content-policy.json');
const page = (body: string): string => `<!doctype html><html><head><title>t</title></head><body><main><h1>Guide</h1>${body}</main></body></html>`;

const work: string[] = [];
const workDir = (): string => {
  const d = fs.mkdtempSync(path.join(os.tmpdir(), 'kit-cps-'));
  work.push(d);
  return d;
};
afterAll(() => { for (const d of work) fs.rmSync(d, { recursive: true, force: true }); });

describe('frameworkDirFor', () => {
  it('finds the reusable-agents checkout the kit lives in', () => {
    expect(FRAMEWORK).not.toBeNull();
    expect(hasScanner(FRAMEWORK)).toBe(true);
    expect(path.resolve(KIT, '..', '..')).toBe(FRAMEWORK);
  });

  it('prefers the configured dir, then KIT_GATE_FRAMEWORK_DIR, and ignores dirs without the scanner', () => {
    const empty = workDir();
    expect(frameworkDirFor(empty, { KIT_GATE_FRAMEWORK_DIR: FRAMEWORK as string })).toBe(FRAMEWORK);
    expect(frameworkDirFor(FRAMEWORK, {})).toBe(FRAMEWORK);
    expect(frameworkDirFor(null, {}, empty)).toBeNull();
  });
});

describe('pageFileName', () => {
  it('makes a readable, unique, traversal-free file name', () => {
    expect(pageFileName(0, '/')).toBe('0000-home.html');
    expect(pageFileName(7, '/guides/choosing-a-widget?x=1')).toBe('0007-guides-choosing-a-widget-x-1.html');
    expect(pageFileName(1, '/../../etc/passwd')).toBe('0001-..-..-etc-passwd.html');
    expect(pageFileName(1, '/../../etc/passwd')).not.toContain('/');
  });
});

const describeScanner = FRAMEWORK ? describe : describe.skip;

describeScanner('runContentPolicyScan (the real scanner)', () => {
  const base = { ruleSets: ['kit-fixture'], configFile: CONFIG, frameworkDir: FRAMEWORK as string };

  it('passes pages that keep the rules, including a negated safety line', async () => {
    const r = await runContentPolicyScan([
      { name: '/guides/safe', html: page('<p>Never backfeed a house through an outlet. A transfer switch must be installed by a licensed electrician.</p>') },
      { name: '/about', html: page('<p>About us.</p>') },
    ], { ...base, workDir: workDir() });
    expect([r.exitCode, r.problems, r.scanned]).toEqual([0, [], 2]);
  }, 60_000);

  it('reports each violation by page path and rule', async () => {
    const r = await runContentPolicyScan([
      { name: '/guides/unsafe', html: page('<p>You can backfeed the house through the dryer outlet.</p>') },
      { name: '/guides/switch', html: page('<p>Wire in a transfer switch yourself.</p>') },
      { name: '/guides/fine', html: page('<p>Store it half charged.</p>') },
    ], { ...base, workDir: workDir() });
    expect(r.exitCode).toBe(1);
    expect(r.scanned).toBe(3);
    expect(r.problems).toEqual([
      expect.stringMatching(/^\/guides\/unsafe: \[kit-fixture\.fixture-backfeed\] backfeeding a home through an outlet: "You can backfeed/),
      expect.stringMatching(/^\/guides\/switch: \[kit-fixture\.fixture-electrician-notice\] missing the licensed-electrician notice; triggered by "Wire in a transfer switch/),
    ]);
  }, 60_000);

  it('turns a config error into a failure with the scanner\'s message', async () => {
    const r = await runContentPolicyScan([{ name: '/x', html: page('<p>x</p>') }], { ...base, ruleSets: ['no-such-rule-set'], workDir: workDir() });
    expect(r.exitCode).toBe(2);
    expect(r.problems.join('\n')).toMatch(/content_policy_scan exited 2/);
  }, 60_000);

  it('reports a missing interpreter instead of passing', async () => {
    const r = await runContentPolicyScan([{ name: '/x', html: page('<p>x</p>') }], { ...base, python: '/nonexistent/python3', workDir: workDir() });
    expect(r.exitCode).toBe(-1);
    expect(r.problems).toHaveLength(1);
  }, 60_000);
});
