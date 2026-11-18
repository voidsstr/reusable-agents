/**
 * Calls into the reusable-agents framework (Python) from kit tests, so the
 * kit's SQL is checked against the framework's own definitions rather than a
 * copy of them: framework/core/human_clicks.py (the verified-human verdicts)
 * and framework/cli/article_publish_gate.py (the publisher's promote).
 *
 * The framework is found at KIT_FRAMEWORK_ROOT, else two levels above the kit
 * (reusable-agents/kits/affiliate-site). A kit vendored into a site has no
 * framework next to it, so these checks skip there. Connection strings travel
 * in the environment, never on the command line.
 */
import { spawnSync } from 'child_process';
import * as fs from 'fs';
import * as path from 'path';

const KIT = path.resolve(__dirname, '..', '..', '..');

/** The reusable-agents checkout holding framework/, or '' when there is none. */
export function frameworkRoot(): string {
  const candidates = [process.env.KIT_FRAMEWORK_ROOT || '', path.resolve(KIT, '..', '..')].filter(Boolean);
  for (const c of candidates) {
    if (fs.existsSync(path.join(c, 'framework', 'core', 'human_clicks.py'))) return c;
  }
  return '';
}

const importable = new Map<string, boolean>();

/** True when python3 runs and imports every module in `modules`. */
export function pythonCan(modules: string[] = []): boolean {
  const key = modules.join(',');
  if (!importable.has(key)) {
    const code = modules.map((m) => `import ${m}`).join('; ') || 'pass';
    const r = spawnSync('python3', ['-c', code], { encoding: 'utf8', timeout: 20_000 });
    importable.set(key, r.status === 0);
  }
  return importable.get(key) as boolean;
}

/**
 * Run `code` with python3 and parse its stdout as JSON. The framework root is
 * first on sys.path (`import framework...` works); `env` is added to the
 * environment.
 */
export function runPythonJson<T>(code: string, env: Record<string, string> = {}): T {
  const root = frameworkRoot();
  if (!root) throw new Error('no reusable-agents framework checkout found (set KIT_FRAMEWORK_ROOT)');
  const prelude = `import sys\nsys.path.insert(0, ${JSON.stringify(root)})\n`;
  const r = spawnSync('python3', ['-c', prelude + code], {
    encoding: 'utf8',
    timeout: 60_000,
    env: { ...process.env, ...env },
    cwd: root,
    maxBuffer: 32 * 1024 * 1024,
  });
  if (r.status !== 0) {
    throw new Error(`python3 exited ${r.status}: ${(r.stderr || '').slice(-2000)}`);
  }
  return JSON.parse(r.stdout) as T;
}
