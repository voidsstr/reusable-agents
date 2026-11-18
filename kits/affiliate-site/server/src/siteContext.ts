/**
 * SiteContext: the typed site configuration every kit module reads.
 *
 * A site keeps one site.json (schema: schema/site.schema.json). It carries
 * everything SpecPicks hard-codes in its source: brand, domain and canonical
 * origin, the Associates partner tag and per-placement segment tags, the
 * Amazon Site named in notices, the earnings disclosure, the byline, theme
 * tokens, the robots / AI-crawler policy, extra static crawler ranges, the
 * CDN in front of the app (edge), the IndexNow key, the HTML cache limits and
 * the price freshness window.
 *
 * loadSiteContext() reads it, validates it against the schema (SITE_SCHEMA is
 * an embedded copy of schema/site.schema.json, kept identical by
 * unit/siteContext.test.ts so the check works however the kit is compiled or
 * bundled), applies kit defaults, runs the cross-field checks a schema cannot
 * express, derives the values the rules need (allowed tags, the quotable price
 * age) and returns a deep-frozen object. Any problem throws SiteContextError
 * listing every issue at once: a site must not boot on a config the kit
 * cannot vouch for.
 */
import * as fs from 'fs';
import {
  AI_CRAWLER_SOURCES,
  CDN_EDGE_RANGES,
  KIT_DEFAULT_STATIC_RANGES,
  MIN_STATIC_RANGE_PREFIX,
  MIN_TRUSTED_PROXY_PREFIX,
  cidrPolicyIssue,
} from './aiCrawlerVerify';

// ─── types ─────────────────────────────────────────────────────────────────

export type CrawlerClass = 'search' | 'agent' | 'training';
export const CRAWLER_CLASSES: readonly CrawlerClass[] = deepFreeze<readonly CrawlerClass[]>(['search', 'agent', 'training']);
export type CrawlerPolicy = 'allow' | 'block';
/** A proxying CDN site.json edge.cdn can name (its ranges: CDN_EDGE_RANGES). */
export type CdnName = 'cloudflare';
/** What site.json edge.cdn accepts. */
export const EDGE_CDN_CHOICES: ReadonlyArray<CdnName | 'none'> = deepFreeze<ReadonlyArray<CdnName | 'none'>>(['none', 'cloudflare']);

export interface ThemePalette {
  readonly bg: string;
  readonly surface: string;
  readonly text: string;
  readonly muted: string;
  readonly border: string;
  readonly link: string;
  readonly accent: string;
  readonly ctaBg: string;
  readonly ctaText: string;
  readonly focus: string;
}

export interface StaticCrawlerRange {
  readonly id: string;
  /** classifyAiTraffic() source names this range proves (e.g. 'claude-user'). */
  readonly sources: readonly string[];
  readonly cidrs: readonly string[];
  /** Where the vendor publishes the range. */
  readonly evidenceUrl: string;
}

export interface SourceRule {
  readonly kind: string;
  readonly exact: readonly string[];
  readonly prefixes: readonly string[];
}

export interface SiteContext {
  readonly schemaVersion: 1;
  readonly site: {
    /** Written to outbound_clicks.site_id / ai_traffic_log.site_id. */
    readonly id: string;
    readonly brandName: string;
    readonly domain: string;
    /** https origin, no trailing slash. */
    readonly canonicalOrigin: string;
    readonly language: string;
  };
  readonly routes: {
    /** Product page path template, e.g. '/product/{asin}'. */
    readonly product: string;
  };
  readonly amazon: {
    readonly partnerTag: string;
    readonly marketplaceHost: 'www.amazon.com';
    /** The Amazon Site named in the 2(i) disclaimer and in stamps. */
    readonly siteName: 'Amazon.com';
    readonly currency: 'USD';
    /** placement (data-cta-placement) -> tracking ID. Null-prototype record. */
    readonly placementTags: Readonly<Record<string, string>>;
    /** source kind (classifySource) -> tracking ID. Null-prototype record. */
    readonly sourceTags: Readonly<Record<string, string>>;
    readonly acceptSegmentTagParam: boolean;
    readonly ctaLabel: string;
    readonly searchCtaLabel: string;
    /** Derived: partnerTag plus every placement and source tag, unique, sorted. */
    readonly allowedTags: readonly string[];
  };
  readonly sources: readonly SourceRule[];
  readonly disclosure: { readonly earnings: string; readonly pagePath: string };
  readonly byline: { readonly name: string; readonly profilePath: string | null; readonly jobTitle: string | null };
  readonly theme: { readonly fontFamily: string; readonly light: ThemePalette; readonly dark: ThemePalette };
  readonly crawlers: {
    readonly policy: Readonly<Record<CrawlerClass, CrawlerPolicy>>;
    readonly userAgents: Readonly<Record<CrawlerClass, readonly string[]>>;
    readonly disallowPaths: readonly string[];
  };
  readonly aiCrawlers: {
    /** Kit defaults (Anthropic's outbound ranges) followed by site extras. */
    readonly staticRanges: readonly StaticCrawlerRange[];
  };
  readonly edge: {
    /** The proxying CDN in front of the app; 'none' when the container app is the first hop. */
    readonly cdn: CdnName | 'none';
    /**
     * Derived: the CDN's published edge ranges, then edge.extraTrustedProxies.
     * clientIp() skips one X-Forwarded-For hop inside them (siteClientIp).
     */
    readonly trustedProxyCidrs: readonly string[];
  };
  readonly indexNow: { readonly key: string };
  readonly cache: {
    readonly htmlSMaxAgeSeconds: number;
    readonly htmlMaxAgeSeconds: number;
    readonly ssrCacheSeconds: number;
    /** Derived: the longest any rendered HTML can be served after render. */
    readonly maxHtmlCacheAgeSeconds: number;
  };
  readonly prices: {
    readonly freshnessHours: number;
    /** Derived: freshnessHours*3600 - maxHtmlCacheAgeSeconds. Older prices are never quoted. */
    readonly quotableMaxAgeSeconds: number;
  };
  readonly meta: { readonly loadedFrom: string | null };
}

/** The raw site.json shape (what the schema accepts). */
export interface SiteJson {
  $schema?: string;
  schemaVersion: 1;
  site: { id: string; brandName: string; domain: string; canonicalOrigin: string; language?: string };
  routes?: { product?: string };
  amazon: {
    partnerTag: string;
    marketplaceHost?: 'www.amazon.com';
    siteName?: 'Amazon.com';
    currency?: 'USD';
    placementTags?: Record<string, string>;
    sourceTags?: Record<string, string>;
    acceptSegmentTagParam?: boolean;
    ctaLabel?: string;
    searchCtaLabel?: string;
  };
  sources?: Array<{ kind: string; exact?: string[]; prefixes?: string[] }>;
  disclosure: { earnings: string; pagePath?: string };
  byline: { name: string; profilePath?: string; jobTitle?: string };
  theme: { fontFamily?: string; light: ThemePalette; dark: ThemePalette };
  crawlers?: {
    policy?: Partial<Record<CrawlerClass, CrawlerPolicy>>;
    userAgents?: Partial<Record<CrawlerClass, string[]>>;
    disallowPaths?: string[];
  };
  aiCrawlers?: { extraStaticRanges?: StaticCrawlerRange[] };
  edge?: { cdn?: CdnName | 'none'; extraTrustedProxies?: string[] };
  indexNow: { key: string };
  cache?: { htmlSMaxAgeSeconds?: number; htmlMaxAgeSeconds?: number; ssrCacheSeconds?: number };
  prices?: { freshnessHours?: number };
}

// ─── kit defaults ──────────────────────────────────────────────────────────

/** robots.txt tokens per crawler class when site.json does not list its own. */
export const KIT_DEFAULT_CRAWLER_USER_AGENTS: Readonly<Record<CrawlerClass, readonly string[]>> = deepFreeze({
  // Search indexes that send readers to the page. Amzn-SearchBot (Alexa /
  // Rufus search) "does not crawl content for generative AI model training".
  search: ['Googlebot', 'Bingbot', 'OAI-SearchBot', 'Claude-SearchBot', 'PerplexityBot', 'Applebot', 'DuckDuckBot', 'Amzn-SearchBot'],
  // Fetches an assistant makes while answering a person (answer-time reads).
  agent: ['ChatGPT-User', 'Claude-User', 'Perplexity-User', 'MistralAI-User', 'DuckAssistBot', 'Meta-ExternalFetcher', 'Amzn-User'],
  // Model-training crawlers and opt-out tokens (Google-Extended and
  // Applebot-Extended are robots.txt tokens, not user agents). Amazonbot is
  // here because Amazon says it "may be used to train Amazon AI models"; a
  // site that wants it lists its own crawlers.userAgents.
  training: [
    'GPTBot', 'ClaudeBot', 'anthropic-ai', 'CCBot', 'Google-Extended', 'Applebot-Extended', 'Bytespider',
    'Meta-ExternalAgent', 'FacebookBot', 'cohere-training-data-crawler', 'ai2bot', 'Diffbot', 'Amazonbot', 'PanguBot',
  ],
});

/** Referer-path rules when site.json has no `sources` (generic affiliate-site families). */
export const KIT_DEFAULT_SOURCES: readonly SourceRule[] = deepFreeze<readonly SourceRule[]>([
  { kind: 'product', exact: [], prefixes: ['/product/', '/products/'] },
  { kind: 'category', exact: ['/categories'], prefixes: ['/category/'] },
  { kind: 'brand', exact: ['/brands'], prefixes: ['/brand/'] },
  { kind: 'article', exact: [], prefixes: ['/guides/', '/articles/', '/reviews/'] },
  { kind: 'deal', exact: ['/deals'], prefixes: ['/deals/'] },
  { kind: 'search', exact: [], prefixes: ['/search'] },
]);

/** Source kinds classifySource() computes itself; rules may tag them but not define them. */
export const RESERVED_SOURCE_KINDS: readonly string[] = deepFreeze(['home', 'direct', 'other']);

export const KIT_DEFAULTS = deepFreeze({
  language: 'en-US',
  productRoute: '/product/{asin}',
  marketplaceHost: 'www.amazon.com',
  siteName: 'Amazon.com',
  currency: 'USD',
  acceptSegmentTagParam: false,
  ctaLabel: 'View current price on Amazon',
  searchCtaLabel: 'Search Amazon for this item',
  disclosurePagePath: '/disclosure',
  fontFamily: 'system-ui, -apple-system, "Segoe UI", Roboto, sans-serif',
  crawlerPolicy: { search: 'allow', agent: 'allow', training: 'block' } as Readonly<Record<CrawlerClass, CrawlerPolicy>>,
  disallowPaths: ['/api/', '/r/'] as readonly string[],
  cdn: 'none' as CdnName | 'none',
  htmlSMaxAgeSeconds: 600,
  htmlMaxAgeSeconds: 60,
  ssrCacheSeconds: 0,
  freshnessHours: 24,
} as const);

/** IP License 2(h): non-image Product Advertising Content may be cached for at most 24 hours. */
export const MAX_FRESHNESS_HOURS = 24;
/** A config whose quotable window drops below this is a mistake, not a policy. */
export const MIN_QUOTABLE_WINDOW_SECONDS = 3600;
/** WCAG 2.x AA contrast for text, and for non-text UI parts. */
export const MIN_TEXT_CONTRAST = 4.5;
export const MIN_UI_CONTRAST = 3;

// ─── schema (embedded copy of schema/site.schema.json) ─────────────────────

export type SchemaNode = boolean | { readonly [keyword: string]: unknown };

export const SITE_SCHEMA: { readonly [keyword: string]: unknown } = deepFreeze({
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "urn:reusable-agents:affiliate-site-kit:site:1",
  "title": "Affiliate site configuration (site.json)",
  "description": "Everything a site built on the affiliate-site kit used to hard-code: identity, Amazon tags, notices, byline, theme, crawler policy, the CDN in front of the app, IndexNow key, cache and price-freshness limits. Loaded and checked by server/src/siteContext.ts (loadSiteContext). Niche values live here, never in kit code.",
  "type": "object",
  "additionalProperties": false,
  "required": ["schemaVersion", "site", "amazon", "disclosure", "byline", "theme", "indexNow"],
  "properties": {
    "$schema": {
      "type": "string",
      "description": "Optional path or URL of this schema, for editors."
    },
    "schemaVersion": {
      "const": 1,
      "description": "site.json format version. The kit refuses versions it does not know."
    },
    "site": { "$ref": "#/$defs/site" },
    "routes": { "$ref": "#/$defs/routes" },
    "amazon": { "$ref": "#/$defs/amazon" },
    "sources": {
      "type": "array",
      "description": "Referer-path rules that name the page kind a click came from (classifySource). First match wins; '/' is 'home', no referer is 'direct', anything else unmatched is 'other'. Omit to use the kit defaults.",
      "items": { "$ref": "#/$defs/sourceRule" },
      "maxItems": 50
    },
    "disclosure": { "$ref": "#/$defs/disclosure" },
    "byline": { "$ref": "#/$defs/byline" },
    "theme": { "$ref": "#/$defs/theme" },
    "crawlers": { "$ref": "#/$defs/crawlers" },
    "aiCrawlers": { "$ref": "#/$defs/aiCrawlers" },
    "edge": { "$ref": "#/$defs/edge" },
    "indexNow": { "$ref": "#/$defs/indexNow" },
    "cache": { "$ref": "#/$defs/cache" },
    "prices": { "$ref": "#/$defs/prices" }
  },
  "$defs": {
    "site": {
      "type": "object",
      "additionalProperties": false,
      "required": ["id", "brandName", "domain", "canonicalOrigin"],
      "properties": {
        "id": {
          "type": "string",
          "pattern": "^[a-z][a-z0-9-]{1,62}$",
          "description": "Stable site id, written to outbound_clicks.site_id and ai_traffic_log.site_id."
        },
        "brandName": { "type": "string", "minLength": 1, "maxLength": 60 },
        "domain": {
          "type": "string",
          "pattern": "^(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\\.)+[a-z]{2,63}$",
          "description": "Registrable host without scheme, e.g. example.com."
        },
        "canonicalOrigin": {
          "type": "string",
          "pattern": "^https://(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\\.)+[a-z]{2,63}$",
          "description": "https origin with no path or trailing slash; its host must be the domain or www.<domain>."
        },
        "language": {
          "type": "string",
          "pattern": "^[a-z]{2,3}(?:-[A-Z]{2})?$",
          "default": "en-US"
        }
      }
    },
    "routes": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "product": {
          "type": "string",
          "pattern": "^/[a-z0-9/_-]*\\{asin\\}[a-z0-9/_-]*$",
          "default": "/product/{asin}",
          "description": "Path template of a product page; {asin} appears exactly once."
        }
      }
    },
    "amazon": {
      "type": "object",
      "additionalProperties": false,
      "required": ["partnerTag"],
      "properties": {
        "partnerTag": { "$ref": "#/$defs/trackingId" },
        "marketplaceHost": {
          "type": "string",
          "enum": ["www.amazon.com"],
          "default": "www.amazon.com",
          "description": "The kit quotes USD prices for the US store only."
        },
        "siteName": {
          "type": "string",
          "enum": ["Amazon.com"],
          "default": "Amazon.com",
          "description": "The Amazon Site named in the IP License 2(i) disclaimer and in price stamps."
        },
        "currency": { "const": "USD", "default": "USD" },
        "placementTags": {
          "type": "object",
          "description": "Segment tracking ID per CTA placement (data-cta-placement value). Unlisted placements use partnerTag.",
          "propertyNames": { "$ref": "#/$defs/placement" },
          "additionalProperties": { "$ref": "#/$defs/trackingId" },
          "default": {}
        },
        "sourceTags": {
          "type": "object",
          "description": "Tracking ID per source kind (pickTrackingId), used by the /r/ redirect. Unlisted kinds use partnerTag.",
          "propertyNames": { "$ref": "#/$defs/sourceKind" },
          "additionalProperties": { "$ref": "#/$defs/trackingId" },
          "default": {}
        },
        "acceptSegmentTagParam": {
          "type": "boolean",
          "default": false,
          "description": "Honour a ?tag= on internal links, but only when it is one of this site's own tracking IDs."
        },
        "ctaLabel": {
          "type": "string",
          "minLength": 1,
          "maxLength": 60,
          "default": "View current price on Amazon"
        },
        "searchCtaLabel": {
          "type": "string",
          "minLength": 1,
          "maxLength": 60,
          "default": "Search Amazon for this item"
        }
      }
    },
    "sourceRule": {
      "type": "object",
      "additionalProperties": false,
      "required": ["kind"],
      "properties": {
        "kind": { "$ref": "#/$defs/sourceKind" },
        "exact": {
          "type": "array",
          "items": { "$ref": "#/$defs/sitePath" },
          "uniqueItems": true,
          "default": []
        },
        "prefixes": {
          "type": "array",
          "items": { "$ref": "#/$defs/sitePath" },
          "uniqueItems": true,
          "default": []
        }
      }
    },
    "disclosure": {
      "type": "object",
      "additionalProperties": false,
      "required": ["earnings"],
      "properties": {
        "earnings": {
          "type": "string",
          "minLength": 20,
          "maxLength": 500,
          "description": "The Associates disclosure shown on every page with an affiliate link (Operating Agreement section 5)."
        },
        "pagePath": {
          "$ref": "#/$defs/sitePath",
          "default": "/disclosure"
        }
      }
    },
    "byline": {
      "type": "object",
      "additionalProperties": false,
      "required": ["name"],
      "properties": {
        "name": { "type": "string", "minLength": 1, "maxLength": 80 },
        "profilePath": { "$ref": "#/$defs/sitePath" },
        "jobTitle": { "type": "string", "minLength": 1, "maxLength": 80 }
      }
    },
    "theme": {
      "type": "object",
      "additionalProperties": false,
      "required": ["light", "dark"],
      "properties": {
        "fontFamily": {
          "type": "string",
          "pattern": "^[A-Za-z0-9 ,\"'._-]{1,200}$",
          "default": "system-ui, -apple-system, \"Segoe UI\", Roboto, sans-serif"
        },
        "light": { "$ref": "#/$defs/palette" },
        "dark": { "$ref": "#/$defs/palette" }
      }
    },
    "palette": {
      "type": "object",
      "additionalProperties": false,
      "required": ["bg", "surface", "text", "muted", "border", "link", "accent", "ctaBg", "ctaText", "focus"],
      "properties": {
        "bg": { "$ref": "#/$defs/color" },
        "surface": { "$ref": "#/$defs/color" },
        "text": { "$ref": "#/$defs/color" },
        "muted": { "$ref": "#/$defs/color" },
        "border": { "$ref": "#/$defs/color" },
        "link": { "$ref": "#/$defs/color" },
        "accent": { "$ref": "#/$defs/color" },
        "ctaBg": { "$ref": "#/$defs/color" },
        "ctaText": { "$ref": "#/$defs/color" },
        "focus": { "$ref": "#/$defs/color" }
      }
    },
    "crawlers": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "policy": {
          "type": "object",
          "additionalProperties": false,
          "description": "robots.txt policy per crawler class. Kit default: search allow, agent allow, training block.",
          "properties": {
            "search": { "$ref": "#/$defs/crawlerPolicy" },
            "agent": { "$ref": "#/$defs/crawlerPolicy" },
            "training": { "$ref": "#/$defs/crawlerPolicy" }
          }
        },
        "userAgents": {
          "type": "object",
          "additionalProperties": false,
          "description": "robots.txt user-agent tokens per class. A class given here replaces the kit default list for that class.",
          "properties": {
            "search": { "$ref": "#/$defs/userAgentList" },
            "agent": { "$ref": "#/$defs/userAgentList" },
            "training": { "$ref": "#/$defs/userAgentList" }
          }
        },
        "disallowPaths": {
          "type": "array",
          "items": { "$ref": "#/$defs/robotsPath" },
          "uniqueItems": true,
          "maxItems": 50,
          "default": ["/api/", "/r/"]
        }
      }
    },
    "aiCrawlers": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "extraStaticRanges": {
          "type": "array",
          "description": "Static vendor IP ranges on top of the kit defaults (which already carry Anthropic's published outbound ranges).",
          "items": { "$ref": "#/$defs/staticRange" },
          "maxItems": 20,
          "default": []
        }
      }
    },
    "staticRange": {
      "type": "object",
      "additionalProperties": false,
      "required": ["id", "sources", "cidrs", "evidenceUrl"],
      "properties": {
        "id": { "type": "string", "pattern": "^[a-z0-9][a-z0-9-]{1,47}$" },
        "sources": {
          "type": "array",
          "items": { "type": "string", "pattern": "^[a-z0-9][a-z0-9-]{0,39}$" },
          "minItems": 1,
          "uniqueItems": true
        },
        "cidrs": {
          "type": "array",
          "description": "Public address space only, no broader than /16 (IPv4) or /32 (IPv6): a range here verifies before the vendor lists do.",
          "items": { "$ref": "#/$defs/cidr" },
          "minItems": 1,
          "maxItems": 100,
          "uniqueItems": true
        },
        "evidenceUrl": { "type": "string", "pattern": "^https://[^\\s]+$" }
      }
    },
    "edge": {
      "type": "object",
      "additionalProperties": false,
      "description": "What sits in front of the app. The kit reads the visitor's address from X-Forwarded-For; behind a proxying CDN the right-most public hop is the CDN's edge, so name the CDN here and the kit skips exactly one hop inside its published ranges (siteClientIp). Default: none, the container app is the first hop.",
      "properties": {
        "cdn": {
          "type": "string",
          "enum": ["none", "cloudflare"],
          "default": "none",
          "description": "The proxying CDN, if any. Its edge ranges are kit data (CDN_EDGE_RANGES)."
        },
        "extraTrustedProxies": {
          "type": "array",
          "description": "Further proxy ranges in front of the app, treated like the CDN's edge (still one hop). Public address space only, no broader than /12 (IPv4) or /24 (IPv6).",
          "items": { "$ref": "#/$defs/cidr" },
          "uniqueItems": true,
          "maxItems": 50,
          "default": []
        }
      }
    },
    "indexNow": {
      "type": "object",
      "additionalProperties": false,
      "required": ["key"],
      "properties": {
        "key": {
          "type": "string",
          "pattern": "^[A-Za-z0-9-]{8,128}$",
          "description": "Public IndexNow key, served at /<key>.txt."
        }
      }
    },
    "cache": {
      "type": "object",
      "additionalProperties": false,
      "description": "Every layer that can hold rendered HTML. Their sum is subtracted from the price freshness window, so a cached page never shows a price older than the window.",
      "properties": {
        "htmlSMaxAgeSeconds": { "type": "integer", "minimum": 0, "maximum": 600, "default": 600 },
        "htmlMaxAgeSeconds": { "type": "integer", "minimum": 0, "maximum": 600, "default": 60 },
        "ssrCacheSeconds": { "type": "integer", "minimum": 0, "maximum": 900, "default": 0 }
      }
    },
    "prices": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "freshnessHours": {
          "type": "integer",
          "minimum": 1,
          "maximum": 24,
          "default": 24,
          "description": "Hours an Amazon price (or availability) may be shown after it was fetched, before cache layers are subtracted. IP License 2(h) caps it at 24."
        }
      }
    },
    "trackingId": {
      "type": "string",
      "pattern": "^[a-z0-9][a-z0-9-]{0,60}-[0-9]{2}$"
    },
    "placement": {
      "type": "string",
      "pattern": "^[a-z0-9][a-z0-9-]{0,39}$"
    },
    "sourceKind": {
      "type": "string",
      "pattern": "^[a-z][a-z0-9_]{0,49}$"
    },
    "sitePath": {
      "type": "string",
      "pattern": "^/[A-Za-z0-9._~!$&'()*+,;=:@%/-]*$"
    },
    "robotsPath": {
      "type": "string",
      "pattern": "^/[A-Za-z0-9._~!$&'()*+,;=:@%/?-]*$"
    },
    "color": {
      "type": "string",
      "pattern": "^#[0-9a-fA-F]{6}$"
    },
    "cidr": {
      "type": "string",
      "pattern": "^[0-9A-Fa-f:.]{2,45}/[0-9]{1,3}$"
    },
    "crawlerPolicy": {
      "type": "string",
      "enum": ["allow", "block"]
    },
    "userAgentList": {
      "type": "array",
      "items": { "type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$" },
      "uniqueItems": true,
      "maxItems": 100
    }
  }
});

// ─── JSON Schema validator (the draft 2020-12 subset the kit schema uses) ──

export interface SchemaIssue {
  /** JSON pointer into the validated document ('' is the root). */
  readonly path: string;
  readonly message: string;
}

/**
 * Keywords this validator implements (assertions) or knowingly ignores
 * (annotations). unsupportedSchemaKeywords() fails a schema that uses anything
 * else, so a schema edit can never silently stop being enforced.
 */
export const SUPPORTED_SCHEMA_KEYWORDS: ReadonlySet<string> = new Set([
  '$schema', '$id', '$comment', 'title', 'description', 'default', 'examples',
  '$defs', '$ref',
  'type', 'const', 'enum',
  'properties', 'required', 'additionalProperties', 'propertyNames',
  'items', 'minItems', 'maxItems', 'uniqueItems',
  'minLength', 'maxLength', 'pattern',
  'minimum', 'maximum', 'exclusiveMinimum', 'exclusiveMaximum',
]);

type SchemaObject = { readonly [keyword: string]: unknown };

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

/** Every keyword in `schema` (recursively) the validator does not know. */
export function unsupportedSchemaKeywords(schema: SchemaNode): string[] {
  const out = new Set<string>();
  const walk = (node: unknown, where: string): void => {
    if (typeof node === 'boolean') return;
    if (!isPlainObject(node)) { out.add(`${where}: not a schema`); return; }
    for (const [k, v] of Object.entries(node)) {
      if (!SUPPORTED_SCHEMA_KEYWORDS.has(k)) out.add(`${where}/${k}`);
      if (k === 'properties' || k === '$defs') {
        if (isPlainObject(v)) for (const [name, sub] of Object.entries(v)) walk(sub, `${where}/${k}/${name}`);
      } else if (k === 'items' || k === 'additionalProperties' || k === 'propertyNames') {
        walk(v, `${where}/${k}`);
      }
    }
  };
  walk(schema, '#');
  return Array.from(out);
}

function pointerEscape(key: string): string {
  return key.replace(/~/g, '~0').replace(/\//g, '~1');
}

function jsonEqual(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (Array.isArray(a) || Array.isArray(b)) {
    if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length) return false;
    return a.every((x, i) => jsonEqual(x, b[i]));
  }
  if (isPlainObject(a) && isPlainObject(b)) {
    const ka = Object.keys(a);
    const kb = Object.keys(b);
    if (ka.length !== kb.length) return false;
    return ka.every((k) => Object.prototype.hasOwnProperty.call(b, k) && jsonEqual(a[k], b[k]));
  }
  return false;
}

const PATTERN_CACHE = new Map<string, RegExp>();
function compiledPattern(p: string): RegExp {
  let re = PATTERN_CACHE.get(p);
  if (!re) {
    re = new RegExp(p, 'u');
    PATTERN_CACHE.set(p, re);
  }
  return re;
}

function typeMatches(t: unknown, v: unknown): boolean {
  switch (t) {
    case 'null': return v === null;
    case 'boolean': return typeof v === 'boolean';
    case 'string': return typeof v === 'string';
    case 'number': return typeof v === 'number' && Number.isFinite(v);
    case 'integer': return typeof v === 'number' && Number.isInteger(v);
    case 'array': return Array.isArray(v);
    case 'object': return isPlainObject(v);
    default: throw new Error(`schema: unknown type ${String(t)}`);
  }
}

function resolveRef(root: SchemaObject, ref: string): SchemaNode {
  if (!ref.startsWith('#/')) throw new Error(`schema: only local $ref is supported (${ref})`);
  let node: unknown = root;
  for (const raw of ref.slice(2).split('/')) {
    const key = raw.replace(/~1/g, '/').replace(/~0/g, '~');
    if (!isPlainObject(node) || !Object.prototype.hasOwnProperty.call(node, key)) {
      throw new Error(`schema: unresolvable $ref ${ref}`);
    }
    node = node[key];
  }
  if (typeof node !== 'boolean' && !isPlainObject(node)) throw new Error(`schema: $ref ${ref} is not a schema`);
  return node as SchemaNode;
}

function num(node: SchemaObject, k: string): number | undefined {
  const v = node[k];
  return typeof v === 'number' ? v : undefined;
}

function validateNode(
  node: SchemaNode,
  value: unknown,
  path: string,
  root: SchemaObject,
  issues: SchemaIssue[],
  depth: number,
): void {
  if (depth > 64) throw new Error('schema: $ref nesting too deep');
  if (node === true) return;
  if (node === false) { issues.push({ path, message: 'is not allowed' }); return; }

  if (typeof node.$ref === 'string') validateNode(resolveRef(root, node.$ref), value, path, root, issues, depth + 1);

  if (Object.prototype.hasOwnProperty.call(node, 'const') && !jsonEqual(value, node.const)) {
    issues.push({ path, message: `must be ${JSON.stringify(node.const)}` });
  }
  if (Array.isArray(node.enum) && !node.enum.some((e) => jsonEqual(e, value))) {
    issues.push({ path, message: `must be one of ${node.enum.map((e) => JSON.stringify(e)).join(', ')}` });
  }
  if (node.type !== undefined) {
    const types = Array.isArray(node.type) ? node.type : [node.type];
    if (!types.some((t) => typeMatches(t, value))) {
      issues.push({ path, message: `must be ${types.join(' or ')}` });
      return;
    }
  }

  if (typeof value === 'string') {
    const len = Array.from(value).length;
    const minL = num(node, 'minLength');
    const maxL = num(node, 'maxLength');
    if (minL !== undefined && len < minL) issues.push({ path, message: `must be at least ${minL} characters` });
    if (maxL !== undefined && len > maxL) issues.push({ path, message: `must be at most ${maxL} characters` });
    if (typeof node.pattern === 'string' && !compiledPattern(node.pattern).test(value)) {
      issues.push({ path, message: `must match ${node.pattern}` });
    }
  }

  if (typeof value === 'number') {
    const min = num(node, 'minimum');
    const max = num(node, 'maximum');
    const xmin = num(node, 'exclusiveMinimum');
    const xmax = num(node, 'exclusiveMaximum');
    if (min !== undefined && value < min) issues.push({ path, message: `must be >= ${min}` });
    if (max !== undefined && value > max) issues.push({ path, message: `must be <= ${max}` });
    if (xmin !== undefined && value <= xmin) issues.push({ path, message: `must be > ${xmin}` });
    if (xmax !== undefined && value >= xmax) issues.push({ path, message: `must be < ${xmax}` });
  }

  if (Array.isArray(value)) {
    const minI = num(node, 'minItems');
    const maxI = num(node, 'maxItems');
    if (minI !== undefined && value.length < minI) issues.push({ path, message: `must have at least ${minI} items` });
    if (maxI !== undefined && value.length > maxI) issues.push({ path, message: `must have at most ${maxI} items` });
    if (node.uniqueItems === true) {
      for (let i = 0; i < value.length; i++) {
        for (let j = i + 1; j < value.length; j++) {
          if (jsonEqual(value[i], value[j])) issues.push({ path: `${path}/${j}`, message: `duplicates item ${i}` });
        }
      }
    }
    if (node.items !== undefined) {
      value.forEach((item, i) => validateNode(node.items as SchemaNode, item, `${path}/${i}`, root, issues, depth + 1));
    }
  }

  if (isPlainObject(value)) {
    const props = isPlainObject(node.properties) ? node.properties : {};
    if (Array.isArray(node.required)) {
      for (const k of node.required) {
        if (typeof k === 'string' && !Object.prototype.hasOwnProperty.call(value, k)) {
          issues.push({ path: `${path}/${pointerEscape(k)}`, message: 'is required' });
        }
      }
    }
    for (const [k, v] of Object.entries(value)) {
      const at = `${path}/${pointerEscape(k)}`;
      if (node.propertyNames !== undefined) {
        const sub: SchemaIssue[] = [];
        validateNode(node.propertyNames as SchemaNode, k, at, root, sub, depth + 1);
        for (const s of sub) issues.push({ path: at, message: `property name ${s.message}` });
      }
      if (Object.prototype.hasOwnProperty.call(props, k)) {
        validateNode(props[k] as SchemaNode, v, at, root, issues, depth + 1);
      } else if (node.additionalProperties === false) {
        issues.push({ path: at, message: 'is not an allowed property' });
      } else if (node.additionalProperties !== undefined && node.additionalProperties !== true) {
        validateNode(node.additionalProperties as SchemaNode, v, at, root, issues, depth + 1);
      }
    }
  }
}

/** Validate `value` against a schema (default: the site schema). Returns every issue. */
export function validateAgainstSchema(value: unknown, schema: SchemaObject = SITE_SCHEMA): SchemaIssue[] {
  const issues: SchemaIssue[] = [];
  validateNode(schema, value, '', schema, issues, 0);
  return issues;
}

/** Schema-only check of a parsed site.json. */
export function validateSiteJson(value: unknown): SchemaIssue[] {
  return validateAgainstSchema(value, SITE_SCHEMA);
}

// ─── theme ─────────────────────────────────────────────────────────────────

function channel(hex: string, offset: number): number {
  const c = parseInt(hex.slice(offset, offset + 2), 16) / 255;
  return c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
}

/** WCAG relative luminance of a #rrggbb colour. */
export function relativeLuminance(hex: string): number {
  if (!/^#[0-9a-fA-F]{6}$/.test(hex)) throw new Error(`not a #rrggbb colour: ${hex}`);
  return 0.2126 * channel(hex, 1) + 0.7152 * channel(hex, 3) + 0.0722 * channel(hex, 5);
}

/** WCAG contrast ratio between two #rrggbb colours (1..21). */
export function contrastRatio(a: string, b: string): number {
  const la = relativeLuminance(a);
  const lb = relativeLuminance(b);
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
}

/**
 * Foreground/background pairs a palette must keep legible, with the minimum
 * ratio. Cards render on `surface`, so links, accents and focus rings are
 * checked against it as well as against `bg`.
 */
export const THEME_CONTRAST_PAIRS: ReadonlyArray<readonly [keyof ThemePalette, keyof ThemePalette, number]> = deepFreeze<ReadonlyArray<readonly [keyof ThemePalette, keyof ThemePalette, number]>>([
  ['text', 'bg', MIN_TEXT_CONTRAST],
  ['text', 'surface', MIN_TEXT_CONTRAST],
  ['muted', 'bg', MIN_TEXT_CONTRAST],
  ['muted', 'surface', MIN_TEXT_CONTRAST],
  ['link', 'bg', MIN_TEXT_CONTRAST],
  ['link', 'surface', MIN_TEXT_CONTRAST],
  ['ctaText', 'ctaBg', MIN_TEXT_CONTRAST],
  ['accent', 'bg', MIN_UI_CONTRAST],
  ['accent', 'surface', MIN_UI_CONTRAST],
  ['focus', 'bg', MIN_UI_CONTRAST],
  ['focus', 'surface', MIN_UI_CONTRAST],
]);

/** CSS custom property per palette token (the names kit CSS reads). */
export const THEME_TOKEN_VARS: Readonly<Record<keyof ThemePalette, string>> = deepFreeze({
  bg: '--kit-bg',
  surface: '--kit-surface',
  text: '--kit-text',
  muted: '--kit-muted',
  border: '--kit-border',
  link: '--kit-link',
  accent: '--kit-accent',
  ctaBg: '--kit-cta-bg',
  ctaText: '--kit-cta-text',
  focus: '--kit-focus',
});
export const THEME_FONT_VAR = '--kit-font';

function paletteDecls(p: ThemePalette): string {
  return (Object.keys(THEME_TOKEN_VARS) as Array<keyof ThemePalette>).map((k) => `${THEME_TOKEN_VARS[k]}:${p[k]}`).join(';');
}

/** :root custom properties for the light palette, the dark palette under prefers-color-scheme. */
export function themeCss(theme: SiteContext['theme']): string {
  return `:root{color-scheme:light dark;${paletteDecls(theme.light)};${THEME_FONT_VAR}:${theme.fontFamily}}`
    + `@media (prefers-color-scheme: dark){:root{${paletteDecls(theme.dark)}}}`;
}

// ─── loading ───────────────────────────────────────────────────────────────

export class SiteContextError extends Error {
  readonly issues: readonly string[];
  readonly loadedFrom: string | null;

  constructor(loadedFrom: string | null, issues: readonly string[]) {
    super(`invalid site config${loadedFrom ? ` (${loadedFrom})` : ''}:\n  - ${issues.join('\n  - ')}`);
    this.name = 'SiteContextError';
    this.issues = issues;
    this.loadedFrom = loadedFrom;
  }
}

export interface ParseSiteContextOptions {
  /**
   * Environment for the ops override PRICE_FRESH_HOURS: whole hours, 1..24.
   * It can only shorten the window, and is honoured even below the minimum
   * window site.json itself must keep (hiding prices sooner is always safe).
   */
  env?: NodeJS.ProcessEnv;
  /** For error messages and ctx.meta. */
  loadedFrom?: string | null;
}

function nullProtoRecord(src: Record<string, string> | undefined): Readonly<Record<string, string>> {
  const out = Object.create(null) as Record<string, string>;
  for (const [k, v] of Object.entries(src || {})) out[k] = v;
  return out;
}

function deepFreeze<T>(v: T): T {
  if (v && typeof v === 'object' && !Object.isFrozen(v)) {
    Object.freeze(v);
    for (const k of Object.keys(v as object)) deepFreeze((v as Record<string, unknown>)[k]);
  }
  return v;
}

/** The tighten-only PRICE_FRESH_HOURS override; undefined when unset or not a whole number of hours in 1..24. */
function envFreshHours(env: NodeJS.ProcessEnv): number | undefined {
  const raw = env.PRICE_FRESH_HOURS;
  if (raw === undefined || !/^\s*\d+\s*$/.test(raw)) return undefined;
  const n = parseInt(raw, 10);
  return n >= 1 && n <= MAX_FRESHNESS_HOURS ? n : undefined;
}

/** Validate and resolve a parsed site.json. Throws SiteContextError listing every problem. */
export function parseSiteContext(raw: unknown, opts: ParseSiteContextOptions = {}): SiteContext {
  const loadedFrom = opts.loadedFrom ?? null;
  const schemaIssues = validateSiteJson(raw);
  if (schemaIssues.length) {
    throw new SiteContextError(loadedFrom, schemaIssues.map((i) => `${i.path || '/'} ${i.message}`));
  }
  const input = raw as SiteJson;
  const issues: string[] = [];

  // site
  const origin = input.site.canonicalOrigin;
  const host = new URL(origin).hostname;
  if (host !== input.site.domain && host !== `www.${input.site.domain}`) {
    issues.push(`/site/canonicalOrigin host ${host} is neither ${input.site.domain} nor www.${input.site.domain}`);
  }

  // routes
  const productRoute = input.routes?.product ?? KIT_DEFAULTS.productRoute;
  if (productRoute.split('{asin}').length !== 2) issues.push('/routes/product must contain {asin} exactly once');

  // sources
  const sources: SourceRule[] = (input.sources ?? KIT_DEFAULT_SOURCES).map((r) => ({
    kind: r.kind,
    exact: [...(r.exact ?? [])],
    prefixes: [...(r.prefixes ?? [])],
  }));
  const kinds = new Set<string>();
  const seenExact = new Map<string, string>();
  const seenPrefix = new Map<string, string>();
  sources.forEach((r, i) => {
    if (RESERVED_SOURCE_KINDS.includes(r.kind)) issues.push(`/sources/${i}/kind '${r.kind}' is computed by classifySource and cannot be a rule`);
    if (kinds.has(r.kind)) issues.push(`/sources/${i}/kind '${r.kind}' is defined twice`);
    kinds.add(r.kind);
    if (!r.exact.length && !r.prefixes.length) issues.push(`/sources/${i} needs at least one exact path or prefix`);
    for (const p of r.exact) {
      const prev = seenExact.get(p);
      if (prev) issues.push(`/sources/${i} exact path ${p} is already claimed by '${prev}'`);
      else seenExact.set(p, r.kind);
    }
    for (const p of r.prefixes) {
      const prev = seenPrefix.get(p);
      if (prev) issues.push(`/sources/${i} prefix ${p} is already claimed by '${prev}'`);
      else seenPrefix.set(p, r.kind);
    }
  });

  // amazon
  const a = input.amazon;
  const placementTags = nullProtoRecord(a.placementTags);
  const sourceTags = nullProtoRecord(a.sourceTags);
  for (const k of Object.keys(sourceTags)) {
    if (!kinds.has(k) && !RESERVED_SOURCE_KINDS.includes(k)) {
      issues.push(`/amazon/sourceTags/${k} names a source kind no rule defines (known: ${[...kinds, ...RESERVED_SOURCE_KINDS].join(', ')})`);
    }
  }
  const allowedTags = Array.from(new Set([a.partnerTag, ...Object.values(placementTags), ...Object.values(sourceTags)])).sort();

  // disclosure: Operating Agreement section 5 ("As an Amazon Associate I earn
  // from qualifying purchases", or a substantially similar statement).
  const earnings = input.disclosure.earnings;
  if (!earnings.includes('As an Amazon Associate') || !earnings.includes('from qualifying purchases')) {
    issues.push("/disclosure/earnings must state 'As an Amazon Associate ... from qualifying purchases' (Operating Agreement section 5)");
  }

  // theme
  for (const mode of ['light', 'dark'] as const) {
    const p = input.theme[mode];
    for (const [fg, bg, min] of THEME_CONTRAST_PAIRS) {
      const ratio = contrastRatio(p[fg], p[bg]);
      if (ratio < min) issues.push(`/theme/${mode} ${fg} on ${bg} contrast ${ratio.toFixed(2)} < ${min}`);
    }
  }

  // crawlers
  const policy: Record<CrawlerClass, CrawlerPolicy> = { ...KIT_DEFAULTS.crawlerPolicy, ...(input.crawlers?.policy ?? {}) };
  const userAgents = {} as Record<CrawlerClass, readonly string[]>;
  const uaOwner = new Map<string, CrawlerClass>();
  for (const cls of CRAWLER_CLASSES) {
    const list = [...(input.crawlers?.userAgents?.[cls] ?? KIT_DEFAULT_CRAWLER_USER_AGENTS[cls])];
    userAgents[cls] = list;
    for (const ua of list) {
      const prev = uaOwner.get(ua.toLowerCase());
      if (prev && prev !== cls) issues.push(`/crawlers/userAgents: ${ua} is listed under both ${prev} and ${cls}`);
      uaOwner.set(ua.toLowerCase(), cls);
    }
  }
  const disallowPaths = [...(input.crawlers?.disallowPaths ?? KIT_DEFAULTS.disallowPaths)];

  // AI-crawler static ranges: kit defaults, then site extras
  const extras = input.aiCrawlers?.extraStaticRanges ?? [];
  const rangeIds = new Set(KIT_DEFAULT_STATIC_RANGES.map((r) => r.id));
  const known = new Set(AI_CRAWLER_SOURCES);
  extras.forEach((r, i) => {
    if (rangeIds.has(r.id)) issues.push(`/aiCrawlers/extraStaticRanges/${i}/id '${r.id}' is already defined`);
    rangeIds.add(r.id);
    for (const s of r.sources) {
      if (!known.has(s)) issues.push(`/aiCrawlers/extraStaticRanges/${i}/sources '${s}' is not a crawler source classifyAiTraffic() reports`);
    }
    for (const c of r.cidrs) {
      const issue = cidrPolicyIssue(c, MIN_STATIC_RANGE_PREFIX);
      if (issue) issues.push(`/aiCrawlers/extraStaticRanges/${i}/cidrs '${c}' ${issue}`);
    }
  });
  const staticRanges: StaticCrawlerRange[] = [...KIT_DEFAULT_STATIC_RANGES, ...extras].map((r) => ({
    id: r.id,
    sources: [...r.sources],
    cidrs: [...r.cidrs],
    evidenceUrl: r.evidenceUrl,
  }));

  // edge: the CDN whose one X-Forwarded-For hop clientIp() skips
  const cdn = input.edge?.cdn ?? KIT_DEFAULTS.cdn;
  const extraProxies = input.edge?.extraTrustedProxies ?? [];
  extraProxies.forEach((c, i) => {
    const issue = cidrPolicyIssue(c, MIN_TRUSTED_PROXY_PREFIX);
    if (issue) issues.push(`/edge/extraTrustedProxies/${i} '${c}' ${issue}`);
  });
  const trustedProxyCidrs = Array.from(new Set([...(cdn === 'none' ? [] : CDN_EDGE_RANGES[cdn].cidrs), ...extraProxies]));

  // cache and prices
  const htmlSMaxAgeSeconds = input.cache?.htmlSMaxAgeSeconds ?? KIT_DEFAULTS.htmlSMaxAgeSeconds;
  const htmlMaxAgeSeconds = input.cache?.htmlMaxAgeSeconds ?? KIT_DEFAULTS.htmlMaxAgeSeconds;
  const ssrCacheSeconds = input.cache?.ssrCacheSeconds ?? KIT_DEFAULTS.ssrCacheSeconds;
  const maxHtmlCacheAgeSeconds = htmlSMaxAgeSeconds + htmlMaxAgeSeconds + ssrCacheSeconds;
  const configuredHours = input.prices?.freshnessHours ?? KIT_DEFAULTS.freshnessHours;
  // The minimum window is a check on site.json, where a short one is a mistake.
  const configuredWindow = configuredHours * 3600 - maxHtmlCacheAgeSeconds;
  if (configuredWindow < MIN_QUOTABLE_WINDOW_SECONDS) {
    issues.push(`/prices/freshnessHours ${configuredHours}h minus ${maxHtmlCacheAgeSeconds}s of HTML caching leaves ${configuredWindow}s; at least ${MIN_QUOTABLE_WINDOW_SECONDS}s is required`);
  }
  // PRICE_FRESH_HOURS is an ops decision to hide prices sooner: honour it, never
  // refuse to boot over it. Whole hours >= 1 always outlast the cache layers
  // (at most 2100 s); the clamp only guards against a future schema change.
  const envHours = envFreshHours(opts.env ?? process.env);
  const freshnessHours = envHours !== undefined ? Math.min(configuredHours, envHours) : configuredHours;
  const quotableMaxAgeSeconds = Math.max(0, freshnessHours * 3600 - maxHtmlCacheAgeSeconds);

  if (issues.length) throw new SiteContextError(loadedFrom, issues);

  const ctx: SiteContext = {
    schemaVersion: 1,
    site: {
      id: input.site.id,
      brandName: input.site.brandName,
      domain: input.site.domain,
      canonicalOrigin: origin,
      language: input.site.language ?? KIT_DEFAULTS.language,
    },
    routes: { product: productRoute },
    amazon: {
      partnerTag: a.partnerTag,
      marketplaceHost: KIT_DEFAULTS.marketplaceHost,
      siteName: KIT_DEFAULTS.siteName,
      currency: KIT_DEFAULTS.currency,
      placementTags,
      sourceTags,
      acceptSegmentTagParam: a.acceptSegmentTagParam ?? KIT_DEFAULTS.acceptSegmentTagParam,
      ctaLabel: a.ctaLabel ?? KIT_DEFAULTS.ctaLabel,
      searchCtaLabel: a.searchCtaLabel ?? KIT_DEFAULTS.searchCtaLabel,
      allowedTags,
    },
    sources,
    disclosure: { earnings, pagePath: input.disclosure.pagePath ?? KIT_DEFAULTS.disclosurePagePath },
    byline: {
      name: input.byline.name,
      profilePath: input.byline.profilePath ?? null,
      jobTitle: input.byline.jobTitle ?? null,
    },
    theme: {
      fontFamily: input.theme.fontFamily ?? KIT_DEFAULTS.fontFamily,
      light: { ...input.theme.light },
      dark: { ...input.theme.dark },
    },
    crawlers: { policy, userAgents, disallowPaths },
    aiCrawlers: { staticRanges },
    edge: { cdn, trustedProxyCidrs },
    indexNow: { key: input.indexNow.key },
    cache: { htmlSMaxAgeSeconds, htmlMaxAgeSeconds, ssrCacheSeconds, maxHtmlCacheAgeSeconds },
    prices: { freshnessHours, quotableMaxAgeSeconds },
    meta: { loadedFrom },
  };
  return deepFreeze(ctx);
}

/** Read, validate and resolve a site.json file. */
export function loadSiteContext(file: string, opts: Omit<ParseSiteContextOptions, 'loadedFrom'> = {}): SiteContext {
  let raw: unknown;
  try {
    raw = JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch (e) {
    throw new SiteContextError(file, [`cannot read JSON: ${e instanceof Error ? e.message : String(e)}`]);
  }
  return parseSiteContext(raw, { ...opts, loadedFrom: file });
}

/**
 * The Cache-Control header for SSR HTML, derived from the same numbers that
 * bound price quotability, so a header and the price rule cannot drift apart.
 * Never stale-while-revalidate: that would stretch the age past the window.
 */
export function htmlCacheControl(ctx: Pick<SiteContext, 'cache'>): string {
  return `public, max-age=${ctx.cache.htmlMaxAgeSeconds}, s-maxage=${ctx.cache.htmlSMaxAgeSeconds}`;
}
