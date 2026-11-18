/**
 * Types shared by the kit's HTTP layer.
 *
 * Every database touch goes through KitDb, an injected interface: production
 * passes createPgKitDb(pool) (pgKitDb.ts), tests pass an in-memory fake. Row
 * shapes are camelCase mirrors of SpecPicks' outbound_clicks and
 * ai_traffic_log columns (kit migrations kit_0001 / kit_0002 keep those
 * column contracts, so the framework's human_clicks, placement_watch and
 * ai_traffic readers work unchanged).
 */
import type { AiTrafficKind, IpVerdict } from '../aiCrawlerVerify';

/** One outbound_clicks row (the kit writes via='beacon' and via='redirect'). */
export interface OutboundClickRow {
  readonly siteId: string;
  /** '' when a beacon could not name one (the column is NOT NULL, as upstream). */
  readonly asin: string;
  readonly trackingId: string | null;
  /** Path of the page the click came from ('' when unknown). */
  readonly sourcePage: string;
  readonly sourceKind: string;
  readonly target: 'amazon';
  readonly ipAddress: string | null;
  readonly userAgent: string | null;
  readonly isBot: boolean;
  readonly botReason: string | null;
  readonly country: string | null;
  readonly deviceType: string | null;
  readonly via: 'beacon' | 'redirect';
  readonly acceptLanguage: string | null;
  readonly sessionPageviews: number | null;
  readonly msOnPage: number | null;
  readonly placement: string | null;
}

/** One ai_traffic_log row. */
export interface AiTrafficRow {
  readonly kind: AiTrafficKind;
  readonly source: string;
  readonly path: string;
  /** HTTP status sent; 499 when the client went away before the response finished. */
  readonly statusCode: number;
  readonly userAgent: string | null;
  readonly referer: string | null;
  readonly ipAddress: string | null;
  readonly siteId: string;
  readonly durationMs: number;
  readonly ipVerified: boolean | null;
  readonly ipCheck: string | null;
  /** The raw X-Forwarded-For, only when it held more than the address that connected. */
  readonly forwardedFor: string | null;
}

/** Query for llms.txt's "Pages assistants cite" section. */
export interface AiCitedPagesQuery {
  /** Referral sources counted (classifyAiTraffic names: 'chatgpt', 'perplexity', ...). */
  readonly sources: readonly string[];
  readonly days: number;
  /** Only paths under these prefixes. */
  readonly pathPrefixes: readonly string[];
  readonly minReferrals: number;
  readonly limit: number;
  /**
   * Path prefix of editorial article pages ('/guides/'): a path under it is
   * listed only while editorial_articles holds that slug as published, and
   * takes the article's title.
   */
  readonly articlePathPrefix: string | null;
  /** Only rows written by this site id (ai_traffic_log.site_id); null for all. */
  readonly siteId: string | null;
}

export interface AiCitedPage {
  readonly path: string;
  readonly referrals: number;
  readonly title: string | null;
}

/** Query for llms.txt's "Latest articles" section. */
export interface LatestArticlesQuery {
  readonly days: number;
  readonly limit: number;
}

export interface LatestArticle {
  readonly slug: string;
  readonly title: string;
  /** Publish day, YYYY-MM-DD (UTC). */
  readonly day: string;
}

/**
 * Everything the HTTP layer reads from or writes to the database. The two
 * inserts are fire-and-forget (BackgroundWriter): a rejection is logged,
 * never shown to a reader. The reads are optional: without them llms.txt
 * leaves its data-driven sections out.
 */
export interface KitDb {
  insertOutboundClick(row: OutboundClickRow): Promise<void>;
  insertAiTraffic(row: AiTrafficRow): Promise<void>;
  aiCitedPages?(q: AiCitedPagesQuery): Promise<readonly AiCitedPage[]>;
  latestArticles?(q: LatestArticlesQuery): Promise<readonly LatestArticle[]>;
}

/** What the /r/:asin redirect needs to know about one catalog product. */
export interface RedirectProduct {
  readonly asin: string;
  /** False for a retired / deactivated row. */
  readonly isActive: boolean;
  /** quality_flags (array or JSON text); inactiveReason() reads it. */
  readonly qualityFlags?: unknown;
  /**
   * Required, and the recall gate of /r/: true only when the site vouches
   * that a reader may be sent to buy this product right now (the recall
   * gate does not block it: no v_recall_buy_blocks row for its product or
   * its model; and no policy hold). Anything else, including a
   * value missing at runtime, sends /r/ to the product's own page, which
   * says why. There is no default: a lookup that forgot the recall check
   * must not fail open.
   */
  readonly buyable: boolean;
  /**
   * A Creators detailPageURL fetched within the price window (the site's
   * fresh-cache view), re-tagged and host-checked here; it keeps Amazon's
   * variation parameters. Null falls back to the plain /dp/ link.
   */
  readonly detailPageUrl?: string | null;
  /** Our model name or a fresh cached title, and the brand: used only for the search fallback. */
  readonly title?: string | null;
  readonly brand?: string | null;
}

/** Catalog lookup for /r/:asin; null when the ASIN is not ours. */
export type RedirectLookup = (asin: string) => Promise<RedirectProduct | null>;

/** The part of AiCrawlerVerifier the logger uses (tests pass a stub). */
export interface CrawlerVerifierLike {
  verify(kind: string, source: string, ip: string): IpVerdict;
}

/** Operational log lines (default: console). */
export interface KitLogger {
  info(msg: string): void;
  error(msg: string): void;
}
