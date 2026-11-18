/**
 * Shared harness for the deploy-template tests (unit/deploy/*.test.ts).
 *
 * Every script runs with a sandboxed PATH: symlinks to the coreutils and python3
 * the scripts need, plus recording stubs for az, aws, docker, psql, curl and git.
 * A stub never runs the real tool. It appends one JSON line per call to
 * calls.jsonl with its argv, the mode and sha256 of any file an argument points
 * at (`name=@file`, `@file`, `file://...`), the sha256 of PGPASSWORD /
 * DATABASE_URL when they are in its environment, and its stdin; it answers from
 * a responses file (first substring match on the joined argv: stdout, stderr,
 * exit code, or `stdouts`, one answer per matching call with the last
 * repeated), else prints nothing and exits 0. The fixture names every cloud
 * resource with a deliberately nonexistent name as a second line of defence.
 */
import { spawnSync } from 'child_process';
import * as crypto from 'crypto';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';

export const KIT = path.resolve(__dirname, '../..');
export const DEPLOY = path.join(KIT, 'deploy');
export const FIXTURES = path.join(__dirname, 'fixtures');
export const TOOL = path.join(DEPLOY, 'lib/deploytool.py');

/** The fake secret values in fixtures/secrets.env (none may ever appear in output or argv). */
export const FAKE_SECRET_VALUES = [
  'FakeAppPassw0rdSentinel1',
  'FakeOwnerPassw0rdSentinel2',
  'FakeAgentsPassw0rdSentinel3',
  'FakePublisherPassw0rdSentinel4',
  'FakeAdminPassw0rdSentinel5',
  'FakeRdsPassw0rdSentinel6',
];

export const STUBBED_TOOLS = ['az', 'aws', 'docker', 'psql', 'curl', 'git'] as const;
const REAL_TOOLS = [
  'bash', 'env', 'cat', 'chmod', 'mkdir', 'mktemp', 'rm', 'rmdir', 'date', 'sed', 'tr', 'cut', 'head', 'tail',
  'sort', 'uniq', 'wc', 'stat', 'id', 'dirname', 'basename', 'readlink', 'realpath', 'sleep', 'touch', 'ls',
  'python3', 'sha1sum', 'sha256sum', 'xargs', 'find', 'grep',
];

export function which(tool: string): string | null {
  const r = spawnSync('bash', ['-c', `type -P ${tool}`], { encoding: 'utf8' });
  const p = r.stdout.trim();
  return r.status === 0 && p ? p : null;
}

export const BASH = which('bash') ?? '/bin/bash';

const STUB = `#!/usr/bin/env python3
import hashlib, json, os, sys
tool = os.path.basename(sys.argv[0])
args = sys.argv[1:]
files = []
for a in args:
    cands = [a]
    if "=" in a:
        cands.append(a.split("=", 1)[1])
    for c in cands:
        p = c[1:] if c.startswith("@") else (c[7:] if c.startswith("file://") else None)
        if p and os.path.isfile(p):
            data = open(p, "rb").read()
            files.append({"arg": a, "path": p, "mode": oct(os.stat(p).st_mode & 0o777),
                          "dirMode": oct(os.stat(os.path.dirname(p)).st_mode & 0o777),
                          "sha256": hashlib.sha256(data).hexdigest()})
env = {k: hashlib.sha256(os.environ[k].encode()).hexdigest()
       for k in ("PGPASSWORD", "DATABASE_URL") if os.environ.get(k)}
stdin = ""
if tool in ("psql", "docker", "az", "aws") and not sys.stdin.isatty():
    try:
        stdin = sys.stdin.read()
    except Exception:
        stdin = ""
with open(os.environ["KIT_STUB_LOG"], "a") as fh:
    fh.write(json.dumps({"tool": tool, "argv": args, "files": files, "env": env, "stdin": stdin}) + "\\n")
out, err, code = "", "", 0
resp = os.environ.get("KIT_STUB_RESPONSES")
if resp and os.path.exists(resp):
    joined = tool + " " + " ".join(args)
    for i, rule in enumerate(json.load(open(resp))):
        if rule["match"] in joined:
            out, err, code = rule.get("stdout", ""), rule.get("stderr", ""), rule.get("exit", 0)
            if "stdouts" in rule:
                state_file = os.environ["KIT_STUB_LOG"] + ".seq.json"
                state = json.load(open(state_file)) if os.path.exists(state_file) else {}
                n = state.get(str(i), 0)
                state[str(i)] = n + 1
                json.dump(state, open(state_file, "w"))
                out = rule["stdouts"][min(n, len(rule["stdouts"]) - 1)]
            break
sys.stdout.write(out)
sys.stderr.write(err)
sys.exit(code)
`;

export interface StubCall {
  tool: string;
  argv: string[];
  files: { arg: string; path: string; mode: string; dirMode: string; sha256: string }[];
  env: Record<string, string>;
  stdin: string;
}

export interface StubRule {
  match: string;
  stdout?: string;
  /** One answer per matching call (the last one repeats); overrides stdout. */
  stdouts?: string[];
  stderr?: string;
  exit?: number;
}

export interface Sandbox {
  root: string;
  bin: string;
  home: string;
  tmp: string;
  /** Copy of unit/deploy/fixtures (writable). */
  fx: string;
  /** deploy.env inside fx. */
  envFile: string;
  log: string;
  responses: string;
}

export interface RunResult {
  status: number;
  stdout: string;
  stderr: string;
  /** stdout + stderr */
  all: string;
  calls: StubCall[];
}

/**
 * A fresh sandbox; `real` lists stubbed tools to link to the real binary instead
 * (left out entirely when it is not installed).
 */
export function makeSandbox(opts: { real?: string[]; renderDockerfile?: boolean } = {}): Sandbox {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'kit-deploy-test-'));
  const bin = path.join(root, 'bin');
  const home = path.join(root, 'home');
  const tmp = path.join(root, 'tmp');
  const fx = path.join(root, 'fx');
  for (const d of [bin, home, tmp]) fs.mkdirSync(d, { recursive: true });
  fs.cpSync(FIXTURES, fx, { recursive: true });
  for (const t of REAL_TOOLS) {
    const p = which(t);
    if (p) fs.symlinkSync(p, path.join(bin, t));
  }
  for (const t of STUBBED_TOOLS) {
    if (opts.real?.includes(t)) {
      // The real tool, or nothing when it is not installed (never a stub).
      const real = which(t);
      if (real) fs.symlinkSync(real, path.join(bin, t));
    } else {
      fs.writeFileSync(path.join(bin, t), STUB, { mode: 0o755 });
    }
  }
  const sb: Sandbox = {
    root, bin, home, tmp, fx,
    envFile: path.join(fx, 'deploy.env'),
    log: path.join(root, 'calls.jsonl'),
    responses: path.join(root, 'responses.json'),
  };
  if (opts.renderDockerfile !== false) {
    const r = runScript(sb, 'render-dockerfile.sh', ['--env', sb.envFile, '--write']);
    if (r.status !== 0) throw new Error(`render failed: ${r.all}`);
    fs.writeFileSync(path.join(fx, 'site/.dockerignore'), fs.readFileSync(path.join(DEPLOY, 'dockerignore.example')));
  }
  return sb;
}

export function removeSandbox(sb: Sandbox): void {
  fs.rmSync(sb.root, { recursive: true, force: true });
}

export function setResponses(sb: Sandbox, rules: StubRule[]): void {
  fs.writeFileSync(sb.responses, JSON.stringify(rules));
}

export function readCalls(sb: Sandbox): StubCall[] {
  if (!fs.existsSync(sb.log)) return [];
  return fs.readFileSync(sb.log, 'utf8').split('\n').filter(Boolean).map((l) => JSON.parse(l) as StubCall);
}

/** Run deploy/<script> with the sandbox PATH and a minimal environment. */
export function runScript(sb: Sandbox, script: string, args: string[], env: Record<string, string> = {}): RunResult {
  fs.rmSync(sb.log, { force: true });
  fs.rmSync(`${sb.log}.seq.json`, { force: true });
  const r = spawnSync(BASH, [path.join(DEPLOY, script), ...args], {
    cwd: sb.fx,
    encoding: 'utf8',
    timeout: 60_000,
    env: {
      PATH: sb.bin,
      HOME: sb.home,
      TMPDIR: sb.tmp,
      LANG: 'C.UTF-8',
      KIT_STUB_LOG: sb.log,
      KIT_STUB_RESPONSES: sb.responses,
      ...env,
    },
  });
  const stdout = r.stdout ?? '';
  const stderr = r.stderr ?? '';
  return { status: r.status ?? -1, stdout, stderr, all: stdout + stderr, calls: readCalls(sb) };
}

/** A copy of the sandbox deploy.env with `edit` applied; returns its path. */
export function editedEnv(sb: Sandbox, edit: (text: string) => string, name = `deploy-${Math.random().toString(36).slice(2)}.env`): string {
  const f = path.join(sb.fx, name);
  fs.writeFileSync(f, edit(fs.readFileSync(sb.envFile, 'utf8')));
  return f;
}

/** Run deploytool.py directly. */
export function runTool(args: string[], opts: { input?: string; env?: Record<string, string>; cwd?: string } = {}) {
  const r = spawnSync('python3', ['-I', TOOL, ...args], {
    encoding: 'utf8',
    input: opts.input,
    cwd: opts.cwd,
    timeout: 60_000,
    env: { PATH: process.env.PATH ?? '/usr/bin:/bin', LANG: 'C.UTF-8', ...(opts.env ?? {}) },
  });
  return { status: r.status ?? -1, stdout: r.stdout ?? '', stderr: r.stderr ?? '' };
}

/** Lines starting with `[dry-run] <prefix>` (the printed commands). */
export function dryCommands(out: string, prefix = ''): string[] {
  return out.split('\n').filter((l) => l.startsWith(`[dry-run] ${prefix}`));
}

/** The single printed command starting with `prefix`; throws unless exactly one. */
export function oneCommand(out: string, prefix: string): string {
  const hits = dryCommands(out, prefix).filter((l) => !l.startsWith('[dry-run] (probe)'));
  if (hits.length !== 1) throw new Error(`expected one "${prefix}" command, got ${hits.length}:\n${hits.join('\n')}`);
  return hits[0];
}

/** The SQL blocks printed by dry-run psql commands, in order. */
export function sqlBlocks(out: string): string[] {
  const blocks: string[] = [];
  const re = /<<'SQL'\n([\s\S]*?)\nSQL\n/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(out)) !== null) blocks.push(m[1]);
  return blocks;
}

/** The JSON document printed after `[dry-run] (file <...>/name)`. */
export function shownFile(out: string, name: string): string {
  const start = out.indexOf(`/${name})\n`);
  if (start < 0) throw new Error(`no shown file ${name}`);
  const rest = out.slice(start + name.length + 3);
  const end = rest.indexOf('\n[dry-run]');
  return end < 0 ? rest : rest.slice(0, end);
}

export function sha256(s: string): string {
  return crypto.createHash('sha256').update(s).digest('hex');
}

/** Read one variable from the fixture secrets file (single-quoted values). */
export function fixtureSecret(name: string): string {
  const text = fs.readFileSync(path.join(FIXTURES, 'secrets.env'), 'utf8');
  const m = new RegExp(`^${name}='([^']*)'$`, 'm').exec(text);
  if (!m) throw new Error(`no ${name} in fixtures/secrets.env`);
  return m[1];
}

export function expectNoSecrets(text: string): void {
  for (const s of FAKE_SECRET_VALUES) {
    if (text.includes(s)) throw new Error(`secret value ${s} leaked into:\n${text}`);
  }
}

/** Every cloud/docker/db stub call; dry-run must make none. */
export function cloudCalls(calls: StubCall[]): StubCall[] {
  return calls.filter((c) => c.tool !== 'git');
}
