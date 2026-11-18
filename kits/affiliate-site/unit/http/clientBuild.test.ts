/**
 * scripts/build-client.mjs: the browser bundle builds for the browser
 * platform (nothing Node-only can slip in), stays small, is deterministic,
 * and its metadata is right. Runs the real script into a temp directory.
 */
import { execFileSync, spawnSync } from 'child_process';
import * as crypto from 'crypto';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { kitClientBundleFromSource } from '../../server/src/http/clientAsset';
import { BEACON_PATH, SESSION_PAGEVIEWS_KEY, STICKY_HEIGHT_VAR } from '../../server/src/http/clientContract';
import { KIT_DIR } from './helpers';

const SCRIPT = path.join(KIT_DIR, 'scripts/build-client.mjs');

function build(outdir: string, extra: string[] = []): string {
  return execFileSync(process.execPath, [SCRIPT, '--outdir', outdir, ...extra], { cwd: KIT_DIR, encoding: 'utf8' });
}

describe('scripts/build-client.mjs', () => {
  let dir: string;
  let js: string;
  let meta: Record<string, unknown>;

  beforeAll(() => {
    dir = fs.mkdtempSync(path.join(os.tmpdir(), 'kit-client-build-'));
    const out = build(dir);
    expect(out).toMatch(/build-client: wrote .*kit-client\.js \(\d+ bytes, sha256 [0-9a-f]{12}\)/);
    js = fs.readFileSync(path.join(dir, 'kit-client.js'), 'utf8');
    meta = JSON.parse(fs.readFileSync(path.join(dir, 'kit-client.meta.json'), 'utf8'));
  }, 30_000);

  afterAll(() => {
    fs.rmSync(dir, { recursive: true, force: true });
  });

  it('writes one self-contained, minified IIFE under the size budget', () => {
    expect(js.startsWith('"use strict";(()=>{')).toBe(true);
    expect(js.trimEnd().endsWith('})();')).toBe(true);
    expect(Buffer.byteLength(js)).toBeLessThan(8 * 1024);
    expect(js).not.toMatch(/\brequire\(|\bimport\s*[({"']|\bexport\b|process\.|Buffer\b|__dirname/);
    expect(js.split('\n').length).toBeLessThanOrEqual(2);
  });

  it('carries the contract values the server expects', () => {
    expect(js).toContain(JSON.stringify(BEACON_PATH));
    expect(js).toContain(JSON.stringify(SESSION_PAGEVIEWS_KEY));
    expect(js).toContain(JSON.stringify(STICKY_HEIGHT_VAR));
    expect(js).toContain('sendBeacon');
    expect(js).toContain('"data-cta-placement"');
  });

  it('describes itself accurately in the metadata', () => {
    expect(meta).toEqual({
      file: 'kit-client.js',
      entry: 'client/src/index.ts',
      bytes: Buffer.byteLength(js),
      sha256: crypto.createHash('sha256').update(js).digest('hex'),
      integrity: `sha384-${crypto.createHash('sha384').update(js).digest('base64')}`,
      format: 'iife',
      target: ['es2018'],
      esbuild: expect.stringMatching(/^\d+\.\d+\.\d+$/),
    });
    expect(kitClientBundleFromSource(js).sha256).toBe(meta.sha256);
  });

  it('is deterministic', () => {
    const again = fs.mkdtempSync(path.join(os.tmpdir(), 'kit-client-build2-'));
    try {
      build(again);
      expect(fs.readFileSync(path.join(again, 'kit-client.js'), 'utf8')).toBe(js);
    } finally {
      fs.rmSync(again, { recursive: true, force: true });
    }
  }, 30_000);

  it('--check builds without writing', () => {
    const empty = fs.mkdtempSync(path.join(os.tmpdir(), 'kit-client-check-'));
    try {
      expect(build(path.join(empty, 'out'), ['--check'])).toMatch(/^build-client: checked \(\d+ bytes/);
      expect(fs.existsSync(path.join(empty, 'out'))).toBe(false);
    } finally {
      fs.rmSync(empty, { recursive: true, force: true });
    }
  }, 30_000);

  it('rejects unknown arguments with exit code 2', () => {
    const r = spawnSync(process.execPath, [SCRIPT, '--nope'], { cwd: KIT_DIR, encoding: 'utf8' });
    expect(r.status).toBe(2);
    expect(r.stderr).toContain('unknown argument --nope');
  });
});
