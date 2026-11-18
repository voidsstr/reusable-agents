/**
 * Drift between the kit gate engine and the SpecPicks gate files it was
 * ported from (tests/PORTED_FROM.json). When the SpecPicks checkout is
 * present, each upstream file is re-hashed and every one that changed since
 * the port is reported, so a fix SpecPicks makes to its gate (a new detector,
 * a runner exit code) is not silently missing here. Reported, not failed,
 * unless KIT_DRIFT_STRICT=1: SpecPicks agents edit those files on their own
 * schedule. KIT_DRIFT_UPSTREAM overrides the checkout; KIT_DRIFT_REF hashes
 * `git show <ref>:<file>` instead of the working tree.
 */
import { execFileSync } from 'child_process';
import * as crypto from 'crypto';
import * as fs from 'fs';
import * as path from 'path';

interface Entry { upstreamFile: string; kitFiles: string[]; upstreamLastChange: string; sha256: string }
interface Ported { upstream: { repo: string; localPath: string; commit: string }; entries: Entry[] }

const KIT = path.resolve(__dirname, '..', '..');
const PORTED: Ported = JSON.parse(fs.readFileSync(path.join(KIT, 'tests', 'PORTED_FROM.json'), 'utf8'));
const UPSTREAM = process.env.KIT_DRIFT_UPSTREAM || PORTED.upstream.localPath;
const REF = process.env.KIT_DRIFT_REF || '';

function normalizedSha256(text: string): string {
  const norm = text.replace(/\r\n/g, '\n').split('\n').map((l) => l.replace(/\s+$/, '')).join('\n');
  return crypto.createHash('sha256').update(norm, 'utf8').digest('hex');
}

function upstreamText(file: string): string | null {
  try {
    if (REF) return execFileSync('git', ['-C', UPSTREAM, 'show', `${REF}:${file}`], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] });
    return fs.readFileSync(path.join(UPSTREAM, file), 'utf8');
  } catch {
    return null;
  }
}

describe('PORTED_FROM.json', () => {
  it('names a commit and, for every upstream file, the kit files it became (which exist)', () => {
    expect(PORTED.upstream.commit).toMatch(/^[0-9a-f]{40}$/);
    expect(PORTED.entries.length).toBeGreaterThan(5);
    for (const e of PORTED.entries) {
      expect(e.sha256).toMatch(/^[0-9a-f]{64}$/);
      for (const k of e.kitFiles) expect([k, fs.existsSync(path.join(KIT, k))]).toEqual([k, true]);
    }
  });
});

const hasUpstream = fs.existsSync(path.join(UPSTREAM, '.git')) || fs.existsSync(path.join(UPSTREAM, 'tests'));
(hasUpstream ? describe : describe.skip)(`drift against ${UPSTREAM}${REF ? ` @ ${REF}` : ''}`, () => {
  it('reports every upstream gate file that changed since the port', () => {
    const drift: string[] = [];
    for (const e of PORTED.entries) {
      const text = upstreamText(e.upstreamFile);
      if (text === null) drift.push(`${e.upstreamFile}: missing upstream (ported into ${e.kitFiles.join(', ')})`);
      else if (normalizedSha256(text) !== e.sha256) drift.push(`${e.upstreamFile}: changed since ${PORTED.upstream.commit.slice(0, 8)} (ported into ${e.kitFiles.join(', ')})`);
    }
    if (drift.length) {
      // eslint-disable-next-line no-console
      console.warn(`kit gate engine drift (port the fixes, then refresh tests/PORTED_FROM.json):\n  ${drift.join('\n  ')}`);
    }
    if (process.env.KIT_DRIFT_STRICT === '1') expect(drift).toEqual([]);
  });
});
