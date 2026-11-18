/**
 * Rules for the K2 code that the K1 checks do not reach (they scan only the
 * top level of server/src):
 *   - no site names in code (framework-first: niche values live in site.json)
 *   - every http module is in the http barrel, and the barrel's names never
 *     collide with the kit barrel's, so the kit barrel can re-export it
 *   - the browser bundle only imports browser-safe modules
 */
import * as fs from 'fs';
import * as path from 'path';
import * as kit from '../../server/src';
import * as http from '../../server/src/http';
import { KIT_DIR } from './helpers';

const HTTP_DIR = path.join(KIT_DIR, 'server/src/http');
const CLIENT_DIR = path.join(KIT_DIR, 'client/src');

function codeOnly(src: string): string {
  return src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:])\/\/.*$/gm, '$1');
}

function files(dir: string, ext: RegExp): string[] {
  return fs.readdirSync(dir).filter((n) => ext.test(n)).map((n) => path.join(dir, n));
}

describe('framework-first: no site literals in K2 code', () => {
  it('server/src/http, client/src and the build script never name a specific site', () => {
    const all = [
      ...files(HTTP_DIR, /\.ts$/),
      ...files(CLIENT_DIR, /\.ts$/),
      path.join(KIT_DIR, 'scripts/build-client.mjs'),
    ];
    const offenders = all.filter((f) => /specpicks|aisleprompt|runtimepicks/i.test(codeOnly(fs.readFileSync(f, 'utf8'))));
    expect(offenders.map((f) => path.relative(KIT_DIR, f))).toEqual([]);
  });
});

describe('the http barrel', () => {
  it('re-exports every module in server/src/http', () => {
    const barrel = fs.readFileSync(path.join(HTTP_DIR, 'index.ts'), 'utf8');
    const modules = fs.readdirSync(HTTP_DIR).filter((n) => n.endsWith('.ts') && n !== 'index.ts').map((n) => n.replace(/\.ts$/, ''));
    expect(modules.filter((m) => !barrel.includes(`export * from './${m}';`))).toEqual([]);
  });

  it('exports the public surface', () => {
    for (const name of [
      'mountKit', 'createKitRuntime', 'aiTrafficMiddleware', 'outboundClickHandlers', 'parseBeaconPayload',
      'redirectHandler', 'redirectDecision', 'renderRobotsTxt', 'robotsTxtHandler', 'renderLlmsTxt', 'llmsTxtHandler',
      'indexNowKeyHandler', 'indexNowKeyPath', 'healthHandler', 'loadKitClientBundle', 'kitClientHandler',
      'kitClientScriptTag', 'stickyBuyBarHtml', 'STICKY_BAR_CSS', 'createPgKitDb', 'pgRedirectLookup',
      'redirectProductFromRow', 'REDIRECT_BUYABLE_COLUMN', 'BackgroundWriter', 'defaultMaxSheddable', 'BEACON_PATH',
      'STICKY_ACTIVE_CLASS',
    ]) {
      expect((http as Record<string, unknown>)[name]).toBeDefined();
    }
  });

  it('has one client-address rule, K1\'s (site.json edge): no second resolver, CDN range list or env switch', () => {
    for (const gone of ['requestClientIp', 'trustedProxies', 'resolveTrustedProxies', 'TrustedProxies', 'CLOUDFLARE_IP_RANGES', 'TRUSTED_PROXIES_ENV']) {
      expect((http as Record<string, unknown>)[gone]).toBeUndefined();
    }
    const offenders = files(HTTP_DIR, /\.ts$/).filter((f) => {
      const code = codeOnly(fs.readFileSync(f, 'utf8'));
      // a copied CDN range (Cloudflare's 173.245.48.0/20 or 2400:cb00::/32) or an env switch for proxies
      return /173\.245\.48\.0|2400:cb00::|KIT_TRUSTED_PROXIES/.test(code);
    });
    expect(offenders.map((f) => path.basename(f))).toEqual([]);
    expect(fs.readFileSync(path.join(HTTP_DIR, 'runtime.ts'), 'utf8')).toMatch(/siteClientIp\(ctx, req\.headers\['x-forwarded-for'\], req\.socket\?\.remoteAddress\)/);
  });

  it('never imports the kit barrel at load time (it is meant to re-export this layer: a cycle would drop exports)', () => {
    const offenders = files(HTTP_DIR, /\.ts$/).filter((f) => /^\s*(import|export)\s[^;]*from\s+'\.\.(\/index)?';/m.test(fs.readFileSync(f, 'utf8')));
    expect(offenders.map((f) => path.basename(f))).toEqual([]);
  });

  it('shares no export name with the kit barrel (so the kit barrel can re-export it)', () => {
    const kitNames = new Set(Object.keys(kit));
    // names the kit barrel already re-exports from http (none today) are the same binding
    const clashes = Object.keys(http).filter((n) => kitNames.has(n) && (kit as Record<string, unknown>)[n] !== (http as Record<string, unknown>)[n]);
    expect(clashes).toEqual([]);
  });
});

describe('the browser bundle imports', () => {
  const BROWSER_SAFE = new Set([
    path.join(KIT_DIR, 'server/src/http/clientContract.ts'),
    path.join(KIT_DIR, 'server/src/links.ts'),
  ]);

  function importsOf(file: string): string[] {
    return Array.from(fs.readFileSync(file, 'utf8').matchAll(/^\s*import\s+(?:type\s+)?[^'"]*from\s+'([^']+)';/gm)).map((m) => m[1] as string);
  }

  it('client/src imports only client/src, clientContract.ts and links.ts', () => {
    const bad: string[] = [];
    for (const f of files(CLIENT_DIR, /\.ts$/)) {
      for (const spec of importsOf(f)) {
        if (!spec.startsWith('.')) { bad.push(`${path.basename(f)} -> ${spec}`); continue; }
        const target = path.resolve(path.dirname(f), `${spec}.ts`);
        if (!target.startsWith(CLIENT_DIR) && !BROWSER_SAFE.has(target)) bad.push(`${path.basename(f)} -> ${spec}`);
      }
    }
    expect(bad).toEqual([]);
  });

  it('clientContract.ts imports nothing, and links.ts only types', () => {
    expect(importsOf(path.join(HTTP_DIR, 'clientContract.ts'))).toEqual([]);
    const linksImports = fs.readFileSync(path.join(KIT_DIR, 'server/src/links.ts'), 'utf8').match(/^\s*import\s.*$/gm) ?? [];
    expect(linksImports.every((l) => /^\s*import\s+type\s/.test(l))).toBe(true);
  });
});
