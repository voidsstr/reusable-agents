import * as fs from 'fs';
import * as path from 'path';
import { test, expect } from '@playwright/test';
import { BASE_URL, gateSite } from '../../vendor/affiliate-kit/tests/ui/lib/site';
import { pageRouteEntries } from '../../src/routes/table';

// This site's own gate checks (Playwright project 'site'); the kit's specs run
// as project 'kit'. Niche specs (product pages, the niche's safety rules) go
// next to this file. Read-only GETs, so they are safe against production too.

const KIT_VERSION = fs.readFileSync(path.resolve(__dirname, '..', '..', 'vendor', 'affiliate-kit', 'KIT_VERSION'), 'utf8').trim();

/** Text of an HTML attribute or element, with the entities the kit's esc() writes decoded. */
function decode(s: string): string {
  return s.replace(/&(amp|lt|gt|quot|#39);/g, (_m, e: string) => ({ amp: '&', lt: '<', gt: '>', quot: '"', '#39': "'" } as Record<string, string>)[e] ?? _m);
}

test.describe('site basics @prod-safe', () => {
  test('/health names this site and the vendored kit version', async ({ request }) => {
    const r = await request.get(`${BASE_URL}/health`);
    expect(r.status()).toBe(200);
    const body = (await r.json()) as { status?: string; siteId?: string; kitVersion?: string };
    expect(body.status).toBe('ok');
    expect(body.siteId).toBe(gateSite().ctx.site.id);
    expect(body.kitVersion, 'the image serves the vendored kit').toBe(KIT_VERSION);
  });

  test('every static page answers 200 with the brand in its title and the earnings disclosure', async ({ request }) => {
    const { ctx } = gateSite();
    const staticPaths = pageRouteEntries().map((r) => r.pattern).filter((p) => !p.includes(':'));
    expect(staticPaths.length).toBeGreaterThan(0);
    const problems: string[] = [];
    for (const p of staticPaths) {
      const r = await request.get(`${BASE_URL}${p}`);
      if (r.status() !== 200) { problems.push(`${p}: HTTP ${r.status()}`); continue; }
      const html = await r.text();
      const title = /<title>([^<]*)<\/title>/.exec(html)?.[1] ?? '';
      if (!decode(title).includes(ctx.site.brandName)) problems.push(`${p}: <title> "${title}" does not name the brand`);
      if (!html.includes('data-earnings-disclosure')) problems.push(`${p}: no earnings disclosure`);
    }
    expect(problems, problems.join('\n')).toEqual([]);
  });
});
