/**
 * The client address every K2 row carries (rt.clientIp) is K1's
 * siteClientIp(): the right-most public X-Forwarded-For entry, with exactly
 * one hop skipped inside the CDN site.json `edge` names. These tests pin the
 * wiring (the runtime reads ctx.edge and nothing else) and the cases the
 * logged rows depend on; K1's own tests cover the helper in depth.
 */
import { clientIp, siteClientIp } from '../../server/src/aiCrawlerVerify';
import { createKitRuntime, type KitRequestLike } from '../../server/src/http/runtime';
import type { SiteContext } from '../../server/src/siteContext';
import { ANTHROPIC_IP, captureLogger, CF_EDGE_IP, CF_WORKER_IP, exampleCtx, FakeKitDb, HOME_IP, stubVerifier } from './helpers';

const plain = exampleCtx();
const behindCloudflare = exampleCtx((j) => { j.edge = { cdn: 'cloudflare' }; });

function addressOf(ctx: SiteContext, xff: string | string[] | undefined, remote?: string, env: NodeJS.ProcessEnv = {}): string {
  const rt = createKitRuntime(ctx, { db: new FakeKitDb(), verifier: stubVerifier(), log: captureLogger(), env });
  const req: KitRequestLike = { headers: xff === undefined ? {} : { 'x-forwarded-for': xff }, socket: { remoteAddress: remote } };
  return rt.clientIp(req);
}

describe('rt.clientIp with no CDN in front (site.json edge.cdn none)', () => {
  it.each<[string, string | string[] | undefined, string | undefined]>([
    ['empty', undefined, undefined],
    ['peer only', undefined, '73.12.1.9'],
    ['ipv4-mapped peer', undefined, '::ffff:73.12.1.9'],
    ['edge-appended entry wins over the client claim', '203.0.113.9, 108.80.179.176', '10.0.0.4'],
    ['private hops are skipped', '73.12.1.9, 10.1.2.3', '100.100.0.7'],
    ['only private entries: the peer', '10.0.0.5', '10.0.0.9'],
    ['only private entries, no peer: last entry', '10.0.0.5, 192.168.1.1', undefined],
    ['garbage entries ignored', 'unknown, 73.12.1.9:443, [2001:db8::1]:80', undefined],
    ['header as array', ['203.0.113.9', '73.12.1.9'], undefined],
  ])('%s: SpecPicks\' rule (K1 clientIp)', (_label, xff, remote) => {
    expect(plain.edge.cdn).toBe('none');
    expect(addressOf(plain, xff, remote)).toBe(clientIp(xff, remote));
  });

  it('logs the Cloudflare edge when site.json does not say Cloudflare is in front', () => {
    expect(addressOf(plain, `${HOME_IP}, ${CF_EDGE_IP}`, '100.100.0.7')).toBe(CF_EDGE_IP);
  });

  it('has no second switch: KIT_TRUSTED_PROXIES in the environment changes nothing', () => {
    expect(addressOf(plain, `${HOME_IP}, ${CF_EDGE_IP}`, '100.100.0.7', { KIT_TRUSTED_PROXIES: 'cloudflare' })).toBe(CF_EDGE_IP);
  });
});

describe('rt.clientIp behind Cloudflare (site.json edge.cdn cloudflare)', () => {
  it('skips the Cloudflare edge hop the container platform appended', () => {
    // client -> Cloudflare (appends the client) -> Azure (appends the edge)
    expect(addressOf(behindCloudflare, `${HOME_IP}, ${CF_EDGE_IP}`, '100.100.0.7')).toBe(HOME_IP);
  });

  it('ignores whatever the client prepended', () => {
    expect(addressOf(behindCloudflare, `1.2.3.4, 5.6.7.8, ${HOME_IP}, ${CF_EDGE_IP}`, '10.0.0.1')).toBe(HOME_IP);
  });

  it('a client that bypasses Cloudflare cannot impersonate it', () => {
    // a forged edge address on the left is followed by the address Azure saw
    expect(addressOf(behindCloudflare, `1.2.3.4, ${CF_EDGE_IP}, ${HOME_IP}`, '10.0.0.1')).toBe(HOME_IP);
  });

  it('skips exactly one trusted hop: a Cloudflare Worker cannot choose the logged address', () => {
    // the Worker's author writes the first entry; Cloudflare appends the Worker's own egress; Azure appends the edge
    const xff = `${ANTHROPIC_IP}, ${CF_WORKER_IP}, ${CF_EDGE_IP}`;
    expect(addressOf(behindCloudflare, xff, '10.0.0.4')).toBe(CF_WORKER_IP);
  });

  it('handles IPv6 edges and clients', () => {
    expect(addressOf(behindCloudflare, '2001:db8:1::5, 2606:4700:10::6816:1', '10.0.0.1')).toBe('2001:db8:1::5');
  });

  it('when only the edge is public, logs the edge (the CDN itself asked), never a private ingress peer', () => {
    expect(addressOf(behindCloudflare, CF_EDGE_IP, '100.100.0.7')).toBe(CF_EDGE_IP);
    expect(addressOf(behindCloudflare, `10.0.0.5, ${CF_EDGE_IP}`, '100.100.0.7')).toBe(CF_EDGE_IP);
    expect(addressOf(behindCloudflare, undefined, undefined)).toBe('');
  });

  it('honours edge.extraTrustedProxies, still one hop', () => {
    const ctx = exampleCtx((j) => { j.edge = { cdn: 'none', extraTrustedProxies: ['198.51.100.0/24'] }; });
    expect(addressOf(ctx, `${HOME_IP}, 198.51.100.7`, '10.0.0.1')).toBe(HOME_IP);
    expect(addressOf(ctx, `${ANTHROPIC_IP}, 198.51.100.8, 198.51.100.7`, '10.0.0.1')).toBe('198.51.100.8');
  });

  it('is exactly siteClientIp(ctx, ...)', () => {
    for (const xff of [`${HOME_IP}, ${CF_EDGE_IP}`, `${ANTHROPIC_IP}, ${CF_WORKER_IP}, ${CF_EDGE_IP}`, CF_EDGE_IP, '']) {
      expect(addressOf(behindCloudflare, xff, '10.0.0.4')).toBe(siteClientIp(behindCloudflare, xff, '10.0.0.4'));
    }
  });
});
