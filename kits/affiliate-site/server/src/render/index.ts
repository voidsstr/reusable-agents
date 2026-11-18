/**
 * Kit render helpers (build item K3): markdown with GFM tables, JSON-LD
 * builders, SEO head helpers, sitemaps fed only indexable URLs, the page
 * shell carrying the theme tokens, the earnings disclosure and the 2(k)
 * notice, and the fragment sealer that keeps malformed content from hiding
 * that shell's notices.
 *
 * Import from here (vendor/affiliate-kit/server/src/render in a site, or the
 * kit barrel once it re-exports this module). The list is explicit so the
 * public surface is a decision, not an accident of what each file exports.
 */
export {
  AMAZON_IMAGE_HOST_SUFFIXES,
  isAmazonHostedUrl,
  isAmazonImageHost,
  isAmazonLinkHost,
  isAmazonOwnedHost,
} from './amazonHosts';

export { BASE_CSS, BASE_CSS_MIN } from './baseCss';

export { AMAZON_CONTENT_ATTR, amazonTextHtml, breadcrumbNavHtml, faqSectionHtml } from './blocks';
export type { AmazonTextTag, FaqSectionOptions } from './blocks';

export {
  ARTICLE_HEADLINE_MAX,
  BREADCRUMB_NAME_MAX,
  MAX_ADDITIONAL_PROPERTIES,
  PRODUCT_DESCRIPTION_MAX,
  PRODUCT_NAME_MAX,
  SCHEMA_ORG_CONTEXT,
  SPEC_VALUE_PLACEHOLDER,
  articleLd,
  breadcrumbListLd,
  breadcrumbTrail,
  cleanBrand,
  faqPageLd,
  gtinCheckDigitOk,
  gtinFields,
  isPublishableNode,
  isSimilarToLd,
  itemListLd,
  jsonLdScripts,
  normalizeFaq,
  organizationLd,
  organizationNodeId,
  productLd,
  productNodeId,
  propertyValuesLd,
  sanitizeMpn,
  serializeJsonLd,
  stripAggregateRatings,
  webSiteLd,
  webSiteNodeId,
} from './jsonld';
export type {
  ArticleLdInput,
  BreadcrumbItem,
  BreadcrumbOptions,
  FaqEntry,
  GtinFields,
  ItemListEntry,
  ItemListInput,
  JsonLdNode,
  OrganizationLdOptions,
  ProductLdInput,
  ProductOfferInput,
  PropertyValueInput,
  ResolvedCrumb,
  SimilarProductRef,
  WebSiteLdOptions,
} from './jsonld';

export {
  htmlHasAffiliateLinks,
  htmlShowsAmazonContent,
  needsAmazonNotice,
  renderPage,
  siteFooterHtml,
  siteHeaderHtml,
} from './layout';
export type { FooterColumn, FooterOptions, NavLink, PageOptions } from './layout';

export {
  DEFAULT_MD_PLACEMENT,
  MD_LINK_TEXT,
  MD_TABLE_CLASS,
  MD_TABLE_WRAP_CLASS,
  MD_UNLINKED_CLASS,
  enforceAffiliateLinks,
  headingId,
  mdImageRe,
  mdLinkOrImageRe,
  mdLinkRe,
  mdToHtml,
  productHrefRe,
  resolveNestedLink,
  underscoreEmphasis,
  unwrapNestedLinks,
} from './md';
export type { AffiliateLinkOptions, MdContext, MdOptions } from './md';

export { sealHtmlFragment } from './seal';

export {
  INDEXABLE_PAGE,
  MAX_META_DESCRIPTION_CHARS,
  MAX_TITLE_CHARS,
  clampTitle,
  collapseVsPrefix,
  compactTitleHead,
  headTags,
  isAllowedOgImage,
  matchupWithTopic,
  metaDescriptionFrom,
  noindexPage,
  pageTitle,
  resolveOgImage,
  robotsMetaContent,
} from './seo';
export type { HeadFields, MetaTag, OgImage, PageIndexing } from './seo';

export {
  SITEMAP_CACHE_CONTROL,
  SITEMAP_IMAGE_NS,
  SITEMAP_INDEX_PATH,
  SITEMAP_KIND_RE,
  SITEMAP_MAX_IMAGES_PER_URL,
  SITEMAP_NS,
  SITEMAP_PROTOCOL_MAX_URLS,
  SITEMAP_RETRY_AFTER_SECONDS,
  SITEMAP_SHARD_TARGET,
  SitemapBuildError,
  buildSitemapSet,
  escapeXml,
  indexableUrls,
  maxLastmod,
  robotsDisallows,
  shardFromPgHash,
  sitemapDate,
  sitemapFileResponse,
  sitemapIndexResponse,
  sitemapIndexXml,
  sitemapShardOf,
  sitemapUnavailableResponse,
  sitemapUrlsFrom,
  urlsetXml,
} from './sitemap';
export type {
  BuildSitemapOptions,
  Changefreq,
  SitemapCandidate,
  SitemapExclusion,
  SitemapExclusionReason,
  SitemapFile,
  SitemapHttpResponse,
  SitemapImage,
  SitemapKindInput,
  SitemapSet,
  SitemapUrl,
  SitemapUrlsResult,
} from './sitemap';

export {
  TRAILING_FUNCTION_WORDS,
  TRAILING_TITLE_JUNK,
  collapseWhitespace,
  dropDanglingModifier,
  dropQuestionStub,
  dropUnclosedParen,
  htmlLength,
  htmlToText,
  plainText,
  stripDanglingTail,
  trimNoEllipsis,
  truncateAtWord,
} from './text';
export type { TruncateOptions } from './text';

export { TRACKING_PARAM_RE, canonicalUrl, isSiteHost, publicImageUrl, siteUrl } from './urls';
export type { CanonicalOptions } from './urls';
