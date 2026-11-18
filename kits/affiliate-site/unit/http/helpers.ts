/**
 * Shared fakes for the HTTP-layer tests: a SiteContext from the kit's example
 * site.json, an in-memory KitDb, a stub crawler verifier and a capturing
 * logger. Nothing here touches the network or a real database.
 */
import * as fs from 'fs';
import * as path from 'path';
import { parseSiteContext, type SiteContext } from '../../server/src/siteContext';
import type {
  AiCitedPage, AiCitedPagesQuery, AiTrafficRow, CrawlerVerifierLike, KitDb, KitLogger,
  LatestArticle, LatestArticlesQuery, OutboundClickRow,
} from '../../server/src/http/types';
import type { IpVerdict } from '../../server/src/aiCrawlerVerify';

export const KIT_DIR = path.resolve(__dirname, '../..');

export function exampleSiteJson(): Record<string, any> {
  return JSON.parse(fs.readFileSync(path.join(KIT_DIR, 'fixtures/site.example.json'), 'utf8'));
}

/** The example SiteContext, optionally with site.json edits applied first. */
export function exampleCtx(edit?: (json: Record<string, any>) => void): SiteContext {
  const json = exampleSiteJson();
  if (edit) edit(json);
  return parseSiteContext(json, { env: {}, loadedFrom: 'fixtures/site.example.json' });
}

export class FakeKitDb implements KitDb {
  readonly clicks: OutboundClickRow[] = [];
  readonly ai: AiTrafficRow[] = [];
  readonly citedQueries: AiCitedPagesQuery[] = [];
  readonly latestQueries: LatestArticlesQuery[] = [];
  citedRows: AiCitedPage[] = [];
  latestRows: LatestArticle[] = [];
  /** Make the next N writes reject. */
  failWrites = 0;
  /** Make the reads reject. */
  failReads = false;
  /** Delay every write (ms). */
  writeDelayMs = 0;
  private gate: Promise<void> | null = null;
  private openGate: (() => void) | null = null;

  /** Hold every write until release(): a database that has stopped answering. */
  hold(): void {
    if (this.gate) return;
    this.gate = new Promise<void>((r) => { this.openGate = r; });
  }

  release(): void {
    this.openGate?.();
    this.gate = null;
    this.openGate = null;
  }

  private async write(): Promise<void> {
    if (this.gate) await this.gate;
    if (this.writeDelayMs) await new Promise((r) => setTimeout(r, this.writeDelayMs));
    if (this.failWrites > 0) {
      this.failWrites--;
      throw new Error('db down');
    }
  }

  async insertOutboundClick(row: OutboundClickRow): Promise<void> {
    await this.write();
    this.clicks.push(row);
  }

  async insertAiTraffic(row: AiTrafficRow): Promise<void> {
    await this.write();
    this.ai.push(row);
  }

  async aiCitedPages(q: AiCitedPagesQuery): Promise<AiCitedPage[]> {
    this.citedQueries.push(q);
    if (this.failReads) throw new Error('read failed');
    return this.citedRows;
  }

  async latestArticles(q: LatestArticlesQuery): Promise<LatestArticle[]> {
    this.latestQueries.push(q);
    if (this.failReads) throw new Error('read failed');
    return this.latestRows;
  }
}

/** A verifier that records its calls and answers with a fixed verdict. */
export function stubVerifier(verdict: IpVerdict = { verified: null, check: 'list-unavailable' }): CrawlerVerifierLike & { calls: Array<[string, string, string]> } {
  const calls: Array<[string, string, string]> = [];
  return {
    calls,
    verify(kind: string, source: string, ip: string): IpVerdict {
      calls.push([kind, source, ip]);
      return kind === 'crawler' ? verdict : { verified: null, check: null };
    },
  };
}

export function captureLogger(): KitLogger & { infos: string[]; errors: string[] } {
  const infos: string[] = [];
  const errors: string[] = [];
  return { infos, errors, info: (m) => { infos.push(m); }, error: (m) => { errors.push(m); } };
}

/** Poll until `cond` holds (or fail after `timeoutMs`). */
export async function waitFor(cond: () => boolean, timeoutMs = 3000, what = 'condition'): Promise<void> {
  const until = Date.now() + timeoutMs;
  while (!cond()) {
    if (Date.now() > until) throw new Error(`timed out waiting for ${what}`);
    await new Promise((r) => setTimeout(r, 10));
  }
}

export const CHROME_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36';
export const IPHONE_UA = 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Mobile/15E148 Safari/604.1';
export const CLAUDE_USER_UA = 'Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; Claude-User/1.0; +Claude-User@anthropic.com)';
export const GPTBOT_UA = 'Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; GPTBot/1.2; +https://openai.com/gptbot)';
export const OAI_SEARCHBOT_UA = 'Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; OAI-SearchBot/1.0; +https://openai.com/searchbot';

/** Headers a real Chrome tab sends on a same-origin beacon or link click. */
export const HUMAN_HEADERS: Readonly<Record<string, string>> = Object.freeze({
  'User-Agent': CHROME_UA,
  'Accept-Language': 'en-US,en;q=0.9',
  'Sec-Fetch-Site': 'same-origin',
  'Sec-CH-UA': '"Chromium";v="140", "Google Chrome";v="140"',
});

/** A residential-looking client address (not in any datacenter prefix). */
export const HOME_IP = '73.12.1.9';
/** An address inside Anthropic's published outbound range 160.79.104.0/21. */
export const ANTHROPIC_IP = '160.79.104.10';
/** A Cloudflare edge address (172.64.0.0/13). */
export const CF_EDGE_IP = '172.70.1.1';
/** A cross-zone Cloudflare Worker's egress (inside Cloudflare's 2a06:98c0::/29). */
export const CF_WORKER_IP = '2a06:98c0:3600::103';

/** The example site with Cloudflare proxying it (site.json edge.cdn = 'cloudflare'). */
export function cloudflareCtx(): SiteContext {
  return exampleCtx((j) => { j.edge = { cdn: 'cloudflare' }; });
}
