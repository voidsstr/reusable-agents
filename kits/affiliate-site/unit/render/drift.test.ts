/**
 * Drift between the render helpers and the SpecPicks declarations they were
 * ported from (binding amendment 4: a drift test hashes the ported functions
 * at the commit the port is taken from).
 *
 * fixtures/ported-from-render.json holds the entries in PORTED_FROM.json's
 * exact format and hash unit (portedHash.ts reimplements unit/drift.test.ts's
 * extractor; the parity test below proves they agree on K1's own entries), so
 * they can be appended to PORTED_FROM.json verbatim; until then this file
 * checks them.
 *
 * Same knobs as unit/drift.test.ts:
 *   KIT_DRIFT_UPSTREAM=<dir>   upstream checkout (default: upstream.localPath)
 *   KIT_DRIFT_REF=<commit>     hash `git show <commit>:<file>` instead of the working tree
 *   KIT_DRIFT_REPORT=<file>    write the computed hashes as JSON (the file gets a .render suffix)
 *   KIT_DRIFT_STRICT=1         fail on any drift
 */
import { execFileSync } from 'child_process';
import * as fs from 'fs';
import * as path from 'path';
import { extractSymbolText, hashText, type PortedManifest } from './portedHash';

const KIT = path.resolve(__dirname, '../..');
const MANIFEST: PortedManifest = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures/ported-from-render.json'), 'utf8'));
const K1: PortedManifest = JSON.parse(fs.readFileSync(path.join(KIT, 'PORTED_FROM.json'), 'utf8'));

const UPSTREAM = process.env.KIT_DRIFT_UPSTREAM || MANIFEST.upstream.localPath;
const REF = process.env.KIT_DRIFT_REF || '';
const upstreamPresent = fs.existsSync(path.join(UPSTREAM, 'src'));

function gitShow(commit: string, rel: string): string | null {
  try {
    return execFileSync('git', ['-C', UPSTREAM, 'show', `${commit}:${rel}`], { encoding: 'utf8', maxBuffer: 64 * 1024 * 1024, stdio: ['ignore', 'pipe', 'ignore'] });
  } catch {
    return null;
  }
}

function commitAvailable(commit: string): boolean {
  try {
    execFileSync('git', ['-C', UPSTREAM, 'cat-file', '-e', `${commit}^{commit}`], { stdio: 'ignore' });
    return true;
  } catch {
    return false;
  }
}

describe('fixtures/ported-from-render.json', () => {
  it('uses PORTED_FROM.json\'s upstream, hash unit and entry shape', () => {
    expect(MANIFEST.upstream.repo).toBe(K1.upstream.repo);
    expect(MANIFEST.upstream.commit).toMatch(/^[0-9a-f]{40}$/);
    expect(MANIFEST.hash.algorithm).toBe('sha256');
    expect(MANIFEST.hash.normalization).toBe(K1.hash.normalization);
    expect(MANIFEST.entries.length).toBeGreaterThan(40);
    for (const e of MANIFEST.entries) {
      expect(Object.keys(e).sort()).toEqual(expect.arrayContaining(['kitFile', 'kitSymbol', 'sha256', 'symbol', 'upstreamFile']));
      expect(e.sha256).toMatch(/^[0-9a-f]{64}$/);
      expect(e.kitFile.startsWith('server/src/render/')).toBe(true);
    }
  });

  it('has one entry per upstream symbol, none already in PORTED_FROM.json', () => {
    const keys = MANIFEST.entries.map((e) => `${e.upstreamFile}#${e.symbol}`);
    expect(new Set(keys).size).toBe(keys.length);
    const k1 = new Set(K1.entries.map((e) => `${e.upstreamFile}#${e.symbol}`));
    expect(keys.filter((k) => k1.has(k))).toEqual([]);
  });

  it('points every entry at a kit file that still holds its kit symbol', () => {
    const missing = MANIFEST.entries.filter((e) => {
      const file = path.join(KIT, e.kitFile);
      return !fs.existsSync(file) || !new RegExp(`\\b${e.kitSymbol}\\b`).test(fs.readFileSync(file, 'utf8'));
    }).map((e) => `${e.kitFile}#${e.kitSymbol}`);
    expect(missing).toEqual([]);
  });
});

const describeUpstream = upstreamPresent ? describe : describe.skip;

describeUpstream(`hashes against the SpecPicks checkout (${UPSTREAM})`, () => {
  it('portedHash.ts reproduces unit/drift.test.ts\'s hashes for K1\'s entries at their commit', () => {
    if (!commitAvailable(K1.upstream.commit)) return;
    const sample = K1.entries.filter((e) => ['quotablePrice', 'affiliateOfferLd', 'esc', 'cta', 'classifySource', 'CRAWLER_RANGE_LISTS'].includes(e.symbol));
    expect(sample.length).toBeGreaterThanOrEqual(4);
    for (const e of sample) {
      const src = gitShow(K1.upstream.commit, e.upstreamFile);
      expect(src).not.toBeNull();
      expect(hashText(extractSymbolText(src as string, e.symbol, e.upstreamFile) as string)).toBe(e.sha256);
    }
  });

  it('every recorded hash is the upstream declaration at the recorded commit', () => {
    if (!commitAvailable(MANIFEST.upstream.commit)) return;
    const wrong = MANIFEST.entries.filter((e) => {
      const src = gitShow(MANIFEST.upstream.commit, e.upstreamFile);
      const text = src == null ? null : extractSymbolText(src, e.symbol, e.upstreamFile);
      return text == null || hashText(text) !== e.sha256;
    }).map((e) => `${e.upstreamFile}#${e.symbol}`);
    expect(wrong).toEqual([]);
  });

  it('reports drift since the port (fails only with KIT_DRIFT_STRICT=1)', () => {
    const cache = new Map<string, string | null>();
    const source = (rel: string): string | null => {
      if (!cache.has(rel)) {
        let text: string | null = null;
        try {
          text = REF ? gitShow(REF, rel) : fs.readFileSync(path.join(UPSTREAM, rel), 'utf8');
        } catch {
          text = null;
        }
        cache.set(rel, text);
      }
      return cache.get(rel) as string | null;
    };
    const results = MANIFEST.entries.map((e) => {
      const src = source(e.upstreamFile);
      if (src == null) return { e, status: 'missing-file', now: null as string | null };
      const text = extractSymbolText(src, e.symbol, e.upstreamFile);
      if (text == null) return { e, status: 'missing-symbol', now: null };
      const now = hashText(text);
      return { e, status: now === e.sha256 ? 'same' : 'changed', now };
    });
    if (process.env.KIT_DRIFT_REPORT) {
      fs.writeFileSync(`${process.env.KIT_DRIFT_REPORT}.render`, `${JSON.stringify({
        upstream: UPSTREAM,
        ref: REF || 'working-tree',
        entries: results.map((r) => ({ upstreamFile: r.e.upstreamFile, symbol: r.e.symbol, status: r.status, sha256: r.now })),
      }, null, 2)}\n`);
    }
    const drifted = results.filter((r) => r.status !== 'same');
    if (drifted.length) {
      // eslint-disable-next-line no-console
      console.warn([
        `affiliate-site kit (render): ${drifted.length} of ${results.length} ported SpecPicks declarations changed since ${MANIFEST.upstream.commit.slice(0, 8)}.`,
        'Review them and port the fix, or record the new hash in fixtures/ported-from-render.json:',
        ...drifted.map((r) => `  ${r.status.padEnd(14)} ${r.e.upstreamFile}#${r.e.symbol} -> kit ${r.e.kitFile}#${r.e.kitSymbol}`),
      ].join('\n'));
    }
    if (process.env.KIT_DRIFT_STRICT === '1') {
      expect(drifted.map((r) => `${r.e.upstreamFile}#${r.e.symbol}: ${r.status}`)).toEqual([]);
    }
  });
});
