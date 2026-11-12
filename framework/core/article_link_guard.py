"""Inline-link guard for the article-author → implementer pipeline.

Articles ship value to the site only when they actually link to other
pages on the site — catalog products, recipes, related articles. Without
inline links the article is a dead end: no internal-link equity, no
on-site session expansion, no conversion path.

The article-author already carries `expected_recipe_slugs` and
`expected_kitchen_slugs` on every proposal. This module enforces that
the written body actually contains inline links to a minimum number of
those slugs. Two operations:

  1. `inject_link_directive(prompt, proposal, *, min_recipes, min_kits)` —
     adds a hard requirement block to the implementer's LLM prompt so
     the model knows up-front it must produce N links.
  2. `verify_body(body_md, proposal, *, min_recipes, min_kits)` →
     `LinkAuditResult` — counts the inline links and returns a verdict
     the wrapper uses to accept / defer / extend the article.

Defaults: min_recipes=5, min_kits=2 (aisleprompt food articles); easy
to override per-site via the implementer's invocation.

Slug-pattern matching is lenient — it accepts:
  - `[Anchor](/recipes/<slug>)` (recommended absolute path)
  - `[Anchor](/recipes/<slug>-<id>)` (live aisleprompt URL shape)
  - `[Anchor](https://aisleprompt.com/recipes/<slug>...)` (also live)
  - same shapes under `/k/`, `/product/`, `/reviews/`
  - crawlable kitchen pages: `/kitchen/category/<slug>[/<sub>]` and
    `/kitchen/<product-slug>` (count as kitchen links when the site's
    `kitchen_roots` knob lists them)

It rejects:
  - `[Anchor](recipes/<slug>)` (no leading slash → renders inside /blog)
  - bare `<slug>` mentions without a markdown link

Direct buy links are counted separately (`buy_link_patterns`, default the
`/k/<slug>` click-out): a crawlable product-page link names a product but is
not a buy button, so a monetized bucket can require N of them on top of the
kitchen minimum (`min_buy_links`, `min_buy_links_by_bucket`,
`min_buy_links_by_title` — see `min_buy_links_for`). A body that names
products through their pages (`product_page_patterns`) needs buy links too,
whatever its bucket (`min_buy_links_if_product_pages`). A buy-shaped link to
a category slug is not a buy link: it is dropped when the proposal or the
site config names the slug as a category, or when a catalog check
(`product_slug_checker_from_db`) finds no active product with that slug.

Two deterministic repairs run on bodies already in the table
(framework/cli/article_link_guard.py sweep): `repair_buy_links` adds the
missing buy link after a product's first link, and `repair_anchor_brackets`
turns brackets inside link text into parentheses (a `[… [klein] …](url)`
citation rendered as raw markdown).

The guard is a thin layer — it doesn't try to resolve slug ↔ catalog
ids (that's `resolve-article-links.py`'s job). It just measures
"does the article actually link out, yes/no, how many."
"""
from __future__ import annotations

import dataclasses
import re
from typing import Callable, Iterable


# Match markdown link `[text](url)` where url is `/recipes/...` or full
# host. Captures the slug (with optional trailing -<digits>). Slug is
# case-insensitive so uppercase ASINs (`/product/B08W8BFG9B`) match, and
# an optional query string / fragment is tolerated after the slug so
# `?tag=specpicks-articles-20` variants still count.
_LINK_RE = re.compile(
    r"\[[^\]]+\]"
    r"\((?:https?://[^/)]*)?"
    r"(?P<root>/(?:recipes|k|product|reviews|blog|kitchen/category|kitchen)/)"
    r"(?P<slug>[A-Za-z0-9][A-Za-z0-9-]*(?:/[A-Za-z0-9][A-Za-z0-9-]*)?)"
    r"(?:\?[^)]*)?"
    r"(?:#[^)]*)?"
    r"\)"
)

# Path roots that count toward `kitchen_links` when a site sets no
# `kitchen_roots` knob. `/k/` is the affiliate click-out (a 302 to the
# retailer, robots-blocked on purpose), so a site that wants crawlable
# internal links configures its category/product page roots instead — see
# config/article-link-guard-config.json.
DEFAULT_KITCHEN_ROOTS: tuple[str, ...] = ("/k/",)

# Legacy link shape used when a proposal carries only the unsplit
# `expected_kitchen_slugs` list (categories and products mixed): `/k/`
# resolves both, so it never 404s.
DEFAULT_KITCHEN_LINK_TEMPLATE = "{site_root}/k/{slug}"

# What counts as a DIRECT buy link when a site sets no `buy_link_patterns`:
# the `/k/<slug>` affiliate click-out. Patterns are regexes searched against
# the link target — the path for a relative or own-host link, the full URL for
# any other host — so a site whose buy button is a retailer URL can list
# e.g. r"amazon\.[a-z.]+/dp/" without a code change.
DEFAULT_BUY_LINK_PATTERNS: tuple[str, ...] = (r"^/k/[A-Za-z0-9][A-Za-z0-9-]*",)

# Any markdown link (not an image), tolerating one level of brackets inside
# the anchor text ("[FLUX.2 [klein] model card](https://...)").
_MD_LINK_RE = re.compile(
    r"(?<!!)\[(?:[^\[\]\n]|\[[^\[\]\n]*\])+\]"
    r"\((?P<url>[^)\s]+)(?:\s+\"[^\"]*\")?\)"
)


@dataclasses.dataclass
class LinkAuditResult:
    recipe_links: int
    kitchen_links: int
    product_links: int
    blog_links: int
    matched_expected_recipes: int
    matched_expected_kitchen: int
    expected_recipe_total: int
    expected_kitchen_total: int
    min_recipes: int
    min_kits: int
    min_products: int = 0
    buy_links: int = 0
    min_buy_links: int = 0
    # distinct products the body names through their product pages
    # (`product_page_patterns`); informational, and the trigger for
    # `min_buy_links_if_product_pages`
    product_page_links: int = 0

    @property
    def total_internal_links(self) -> int:
        return (self.recipe_links + self.kitchen_links
                + self.product_links + self.blog_links)

    @property
    def passes(self) -> bool:
        return (self.recipe_links >= self.min_recipes
                and self.kitchen_links >= self.min_kits
                and self.product_links >= self.min_products
                and self.buy_links >= self.min_buy_links)

    def failure_reason(self) -> str:
        if self.recipe_links < self.min_recipes:
            return (f"only {self.recipe_links} /recipes/ links "
                    f"(min {self.min_recipes})")
        if self.kitchen_links < self.min_kits:
            return (f"only {self.kitchen_links} kitchen links "
                    f"(min {self.min_kits})")
        if self.product_links < self.min_products:
            return (f"only {self.product_links} /product/ links "
                    f"(min {self.min_products})")
        if self.buy_links < self.min_buy_links:
            named = (f"; the body names {self.product_page_links} product(s) through "
                     f"their product pages" if self.product_page_links else "")
            return (f"only {self.buy_links} direct buy links "
                    f"(min {self.min_buy_links}) — every named product needs "
                    f"its own buy link, a product-page link is not one, and a "
                    f"buy-shaped link to a category is not one either{named}")
        return ""


def _count_by_root(body_md: str) -> dict[str, set[str]]:
    """Returns root → set of slugs linked."""
    found: dict[str, set[str]] = {
        "/recipes/": set(), "/k/": set(),
        "/product/": set(), "/reviews/": set(), "/blog/": set(),
        "/kitchen/category/": set(), "/kitchen/": set(),
    }
    for m in _LINK_RE.finditer(body_md or ""):
        root = m.group("root"); slug = m.group("slug")
        found.setdefault(root, set()).add(slug)
    return found


def _expected_kitchen(proposal: dict) -> list[str]:
    """Every kitchen slug the proposal expects: the split lists (set by a
    proposer that resolved slugs against its catalog) plus the legacy
    unsplit `expected_kitchen_slugs`."""
    out: list[str] = []
    for key in ("expected_kitchen_category_slugs", "expected_kitchen_product_slugs",
                "expected_kitchen_slugs"):
        for s in proposal.get(key) or []:
            if isinstance(s, str) and s and s not in out:
                out.append(s)
    return out


def _compile_patterns(patterns) -> list:
    """Regexes from config: a bare string is one pattern; an invalid one is
    skipped rather than taking the guard down."""
    if isinstance(patterns, str):     # one pattern given bare in config
        patterns = [patterns]
    out = []
    for p in patterns or ():
        try:
            if p:
                out.append(re.compile(p, re.I))
        except re.error:
            continue
    return out


def _link_target(url: str, hosts: set[str]) -> str:
    """What a link pattern sees: the path (+query) of a relative link or of
    an absolute one on an own host, else the full URL — so another site's
    `/k/` path never reads as ours."""
    hm = re.match(r"^https?://([^/?#]+)(.*)$", url, re.I)
    if hm:
        return (hm.group(2) or "/") if hm.group(1).lower() in hosts else url
    return url


def _target_key(target: str) -> str:
    """A target without its query / fragment / trailing slash, lower-cased:
    "Check price" and the product name pointing at one click-out are one link."""
    return re.split(r"[?#]", target, maxsplit=1)[0].rstrip("/").lower()


def _slug_of(key: str) -> str:
    """The last path segment of an own-host target key ("" for other hosts)."""
    return key.rsplit("/", 1)[-1] if key.startswith("/") else ""


def _iter_link_targets(body_md: str, own_hosts: Iterable[str] | None = None):
    """(match, target) for every markdown link (not an image), in body order."""
    hosts = {str(h).lower() for h in (own_hosts or [])}
    for m in _MD_LINK_RE.finditer(body_md or ""):
        yield m, _link_target(m.group("url").strip("<>"), hosts)


def _matches(pats: list, target: str) -> bool:
    bare = re.split(r"[?#]", target, maxsplit=1)[0]
    return any(p.search(target) or p.search(bare) for p in pats)


def link_targets(body_md: str, patterns, own_hosts: Iterable[str] | None = None) -> set[str]:
    """Distinct targets (`_target_key`) of the markdown links whose target
    matches any of `patterns`. A pattern is tried on the target with and
    without its query, so `…/?$`-anchored shapes match a link that carries
    one. No pattern, no match."""
    pats = _compile_patterns(patterns)
    if not pats:
        return set()
    return {_target_key(t) for _m, t in _iter_link_targets(body_md, own_hosts) if _matches(pats, t)}


def buy_link_targets(body_md: str,
                     patterns: Iterable[str] | None = None,
                     own_hosts: Iterable[str] | None = None) -> set[str]:
    """Distinct direct buy links in `body_md`.

    A link's target is matched against `patterns` (default
    `DEFAULT_BUY_LINK_PATTERNS`): a relative link, or an absolute one on a host
    in `own_hosts`, is matched by its path; any other link by its full URL, so
    another site's `/k/` path never counts. Distinct by target without its
    query or fragment — "Check price" and the product name pointing at the
    same click-out are one buy link. An invalid pattern is skipped.

    Shape only: a buy-shaped link to a category slug is still in this set.
    `filter_buy_targets` drops those (see `verify_body_with_config`).
    """
    return link_targets(body_md, patterns if patterns else DEFAULT_BUY_LINK_PATTERNS, own_hosts)


def product_page_targets(body_md: str, patterns: Iterable[str] | None = None,
                         own_hosts: Iterable[str] | None = None) -> set[str]:
    """Distinct links to product pages (`product_page_patterns`, matched like
    the buy patterns). No default: a site that sets no pattern names no
    product pages, and the product-page trigger stays off."""
    return link_targets(body_md, patterns or (), own_hosts)


def non_product_slugs(cfg: dict | None) -> set[str]:
    """Slugs the site's guard config names as something other than a product:
    `kitchen_slug_check.static_slugs` (subcategories), both sides of
    `subcategory_parents` (subcategories and their top-level categories) and
    `reserved_slugs`. A buy-shaped link to one of them is a category link
    (a site's /k/<category> click-out 301s to the category page), so it is not a
    buy link. The catalog check covers categories this list does not name."""
    ks = (cfg or {}).get("kitchen_slug_check") or {}
    out: set[str] = set()
    for s in list(ks.get("static_slugs") or []) + list(ks.get("reserved_slugs") or []):
        if isinstance(s, str) and s:
            out.add(s.lower())
    parents = ks.get("subcategory_parents")
    for k, v in (parents.items() if isinstance(parents, dict) else []):
        for s in (k, v):
            if isinstance(s, str) and s:
                out.add(s.lower())
    return out


def filter_buy_targets(targets: Iterable[str], exclude: Iterable[str] | None = None,
                       slug_ok: "Callable[[str], bool] | None" = None) -> set[str]:
    """Drop own-host buy targets that are not a product's buy link: the slug
    (last path segment) is in `exclude` (category slugs from the proposal or
    `non_product_slugs`), or `slug_ok(slug)` is false (no active catalog
    product — `product_slug_checker_from_db`). A target on another host (a
    retailer URL) carries no site slug and is kept. This assumes an own-host
    buy link ends in the product's catalog slug (`/k/<slug>`); a site whose
    own buy links are keyed otherwise should not set
    `kitchen_slug_check.product_table`, which turns the catalog check on."""
    ex = {str(s).lower() for s in (exclude or []) if s}
    out: set[str] = set()
    for t in targets or ():
        slug = _slug_of(t)
        if slug and (slug in ex or (slug_ok is not None and not slug_ok(slug))):
            continue
        out.add(t)
    return out


def product_page_slugs(body_md: str, cfg: dict | None, own_hosts: Iterable[str] | None = None,
                       slug_ok: "Callable[[str], bool] | None" = None) -> set[str]:
    """Slugs of the products `body_md` names through their product pages
    (`product_page_patterns`); with `slug_ok`, only those it accepts (active
    catalog products)."""
    cfg = cfg or {}
    hosts = own_hosts if own_hosts is not None else site_hosts_from_root(cfg.get("site_root") or "")
    out: set[str] = set()
    for t in product_page_targets(body_md, cfg.get("product_page_patterns"), hosts):
        slug = _slug_of(t) or t
        if slug_ok is not None and _slug_of(t) and not slug_ok(slug):
            continue
        out.add(slug)
    return out


def _as_int(v) -> int:
    try:
        return max(int(v or 0), 0)
    except (TypeError, ValueError):
        return 0


def min_buy_links_for(cfg: dict | None, *, bucket: "str | Iterable[str]" = "",
                      title: str = "", body_md: str | None = None,
                      own_hosts: Iterable[str] | None = None,
                      product_pages: int | None = None,
                      slug_ok: "Callable[[str], bool] | None" = None) -> int:
    """The direct-buy-link minimum for one article, from the site's guard
    config (`resolve_minima()`):

      min_buy_links            — the site's default (0 when unset);
      min_buy_links_by_bucket  — {bucket: n}; a listed bucket REPLACES the
                                 default (so 0 exempts a bucket);
      min_buy_links_by_title   — {title regex: n}; every pattern that matches
                                 the title RAISES the minimum to at least n.
                                 Gift and checklist pages ride several
                                 buckets, so they are caught by title.
      min_buy_links_if_product_pages — k; a body that names N products
                                 through their pages (`product_page_patterns`)
                                 needs at least min(k, N), whatever its bucket
                                 or title (2026-11-12 review: a seasonal
                                 cookie article and a diet article each linked
                                 three product pages and no buy link).

    `bucket` may be several names (a row's `bucket` and `category` can
    differ: a gift guide proposed as seasonal-occasion was stored with
    category buying-guide); the highest listed minimum among them applies.
    Bucket keys and title patterns are case-insensitive. A bad regex in the
    config is skipped rather than taking the guard down.

    The product-page count comes from `product_pages` when given (the
    directive passes the proposal's product-slug count), else from `body_md`
    (`product_page_slugs`, filtered by `slug_ok` when given).
    """
    cfg = cfg or {}
    n = _min_buy_by_bucket_and_title(cfg, bucket, title)
    knob = _as_int(cfg.get("min_buy_links_if_product_pages"))
    if knob:
        if product_pages is None:
            product_pages = (len(product_page_slugs(body_md, cfg, own_hosts, slug_ok))
                             if body_md else 0)
        n = max(n, min(knob, _as_int(product_pages)))
    return n


def _min_buy_by_bucket_and_title(cfg: dict, bucket, title) -> int:
    n = _as_int(cfg.get("min_buy_links"))
    raw_bucket = cfg.get("min_buy_links_by_bucket")
    raw_title = cfg.get("min_buy_links_by_title")
    by_bucket = {str(k).strip().lower(): v
                 for k, v in (raw_bucket if isinstance(raw_bucket, dict) else {}).items()}
    names = [bucket] if isinstance(bucket, str) else list(bucket or [])
    listed = [by_bucket[b] for b in (str(x or "").strip().lower() for x in names)
              if b and b in by_bucket]
    if listed:
        n = max(_as_int(v) for v in listed)
    t = str(title or "")
    for pat, m in (raw_title if isinstance(raw_title, dict) else {}).items():
        try:
            if pat and t and re.search(pat, t, re.I):
                n = max(n, _as_int(m))
        except re.error:
            continue
    return n


def verify_body(body_md: str,
                proposal: dict,
                *,
                min_recipes: int = 5,
                min_kits: int = 2,
                min_products: int = 0,
                kitchen_roots: Iterable[str] | None = None,
                min_buy_links: int = 0,
                buy_link_patterns: Iterable[str] | None = None,
                own_hosts: Iterable[str] | None = None,
                buy_exclude_slugs: Iterable[str] | None = None,
                buy_slug_ok: "Callable[[str], bool] | None" = None) -> LinkAuditResult:
    """Audit `body_md` against the proposal's expected slug lists.

    `kitchen_roots` — path roots whose links count as kitchen links
    (default `DEFAULT_KITCHEN_ROOTS`). A site whose category/product pages
    are crawlable (e.g. `/kitchen/category/`, `/kitchen/`) lists them so an
    article linking the crawlable page satisfies the contract.

    `min_buy_links` / `buy_link_patterns` / `own_hosts` — the direct-buy-link
    minimum (see `buy_link_targets`, `min_buy_links_for`). 0 by default, so a
    caller that passes nothing keeps the old verdict. `buy_exclude_slugs` /
    `buy_slug_ok` drop buy-shaped links that are not a product's buy link
    (`filter_buy_targets`).
    """
    found = _count_by_root(body_md or "")
    recipes = found.get("/recipes/", set())
    roots = tuple(kitchen_roots) if kitchen_roots else DEFAULT_KITCHEN_ROOTS
    kitchens: set[str] = set()
    for root in roots:
        kitchens |= found.get(root, set())
    products = found.get("/product/", set())
    blogs = found.get("/blog/", set())

    expected_recipe = set(_strip_id(s) for s in
                           (proposal.get("expected_recipe_slugs") or []))
    expected_kit = set(_strip_id(s) for s in _expected_kitchen(proposal))

    matched_r = len(expected_recipe & set(_strip_id(s) for s in recipes))
    # `/kitchen/category/<parent>/<sub>` matches an expected `<sub>` too.
    kit_found = set()
    for s in kitchens:
        kit_found.add(_strip_id(s))
        kit_found.add(_strip_id(s.rsplit("/", 1)[-1]))
    matched_k = len(expected_kit & kit_found)

    return LinkAuditResult(
        recipe_links=len(recipes),
        kitchen_links=len(kitchens),
        product_links=len(products),
        blog_links=len(blogs),
        matched_expected_recipes=matched_r,
        matched_expected_kitchen=matched_k,
        expected_recipe_total=len(expected_recipe),
        expected_kitchen_total=len(expected_kit),
        min_recipes=min_recipes,
        min_kits=min_kits,
        min_products=min_products,
        buy_links=len(filter_buy_targets(buy_link_targets(body_md, buy_link_patterns, own_hosts),
                                         buy_exclude_slugs, buy_slug_ok)),
        min_buy_links=_as_int(min_buy_links),
    )


def verify_body_with_config(body_md: str,
                            proposal: dict | None,
                            cfg: dict | None,
                            *,
                            bucket: "str | Iterable[str] | None" = None,
                            title: str | None = None,
                            slug_ok: "Callable[[str], bool] | None" = None) -> LinkAuditResult:
    """`verify_body` with every minimum taken from the site's resolved guard
    config (`resolve_minima()`), so the implementer's INSERT gate, the
    author's pre-INSERT self-check and any sweep apply the same contract.

    `bucket` / `title` default to the proposal's `bucket` + `category` and
    `title`; with the products the body names through their pages they
    select the direct-buy-link minimum (`min_buy_links_for`).

    A buy-shaped link counts only when it is a product's buy link: its slug
    is not one of the proposal's `expected_kitchen_category_slugs` or the
    config's `non_product_slugs`, and, when the caller has a catalog check
    (`slug_ok`, from `product_slug_checker_from_db`), it is an active
    product. The same `slug_ok` limits the product-page count to active
    products.
    """
    cfg = cfg or {}
    proposal = proposal or {}
    b = bucket if bucket is not None else [str(proposal.get("bucket") or ""),
                                           str(proposal.get("category") or "")]
    t = title if title is not None else str(proposal.get("title") or "")
    hosts = site_hosts_from_root(cfg.get("site_root") or "")
    pages = product_page_slugs(body_md, cfg, hosts, slug_ok)
    exclude = non_product_slugs(cfg) | {
        s.lower() for s in (proposal.get("expected_kitchen_category_slugs") or [])
        if isinstance(s, str) and s}
    audit = verify_body(
        body_md, proposal,
        min_recipes=_as_int(cfg.get("min_recipes")),
        min_kits=_as_int(cfg.get("min_kits")),
        min_products=_as_int(cfg.get("min_products")),
        kitchen_roots=cfg.get("kitchen_roots"),
        min_buy_links=min_buy_links_for(cfg, bucket=b, title=t, product_pages=len(pages)),
        buy_link_patterns=cfg.get("buy_link_patterns"),
        own_hosts=hosts,
        buy_exclude_slugs=exclude,
        buy_slug_ok=slug_ok,
    )
    audit.product_page_links = len(pages)
    return audit


# ── Catalog check for buy links (2026-11-12 review) ─────────────────────
# A `/k/<slug>` link is a buy button only when <slug> is an active product:
# one site's /k/<category> 301s to the category page, and 235 of its
# published bodies link /k/cookware. The caller supplies the connection; the table
# and column names are the site's `kitchen_slug_check` values.

def active_product_slugs_from_db(conn, slugs: Iterable[str], check_cfg: dict) -> "set[str] | None":
    """The subset of `slugs` that are active rows of the site's product table
    (`kitchen_slug_check.product_table`, `product_active_column`). None when
    the config names no product table: the caller then cannot tell, and
    filters nothing. One query."""
    prod_t = str((check_cfg or {}).get("product_table") or "")
    active_c = str((check_cfg or {}).get("product_active_column") or "")
    if not prod_t:
        return None
    for ident in (prod_t, active_c):
        if ident and not _SQL_IDENT_RE.match(ident):
            raise ValueError(f"kitchen_slug_check: bad identifier {ident!r}")
    want = sorted({str(s).lower() for s in slugs or () if s})
    if not want:
        return set()
    where_active = f" AND {active_c} = TRUE" if active_c else ""
    with conn.cursor() as cur:
        cur.execute(f"SELECT slug FROM {prod_t} WHERE slug = ANY(%s){where_active}", (want,))
        return {str(r[0]).lower() for r in cur.fetchall()}


class _SlugSet:
    """`slug_ok` predicate over a set of active product slugs."""

    def __init__(self, active: set[str]):
        self.active = active

    def __call__(self, slug: str) -> bool:
        return str(slug or "").lower() in self.active


def product_slug_checker_from_db(conn, bodies, cfg: dict | None,
                                 own_hosts: Iterable[str] | None = None):
    """`slug_ok` for `verify_body_with_config` / `repair_buy_links`: True for
    a slug that is an active catalog product. One query covers every
    own-host buy-link and product-page slug in `bodies` (a body or a list of
    them). None when the site's config names no product table."""
    cfg = cfg or {}
    ks = cfg.get("kitchen_slug_check") or {}
    if not ks.get("product_table"):
        return None
    hosts = own_hosts if own_hosts is not None else site_hosts_from_root(cfg.get("site_root") or "")
    slugs: set[str] = set()
    for b in ([bodies] if isinstance(bodies, str) else list(bodies or [])):
        for t in (buy_link_targets(b, cfg.get("buy_link_patterns"), hosts)
                  | product_page_targets(b, cfg.get("product_page_patterns"), hosts)):
            if _slug_of(t):
                slugs.add(_slug_of(t))
    active = active_product_slugs_from_db(conn, slugs, ks)
    return None if active is None else _SlugSet(active)


# ── Deterministic body repairs (2026-11-12 review) ──────────────────────

_FENCE_RE = re.compile(r"^[ \t]{0,3}(```|~~~)", re.M)


def _fenced_spans(body: str) -> list[tuple[int, int]]:
    """(start, end) of every fenced code block; an unclosed fence runs to
    the end. Repairs never touch code."""
    spans, open_at = [], None
    for m in _FENCE_RE.finditer(body or ""):
        if open_at is None:
            open_at = m.start()
        else:
            spans.append((open_at, m.end()))
            open_at = None
    if open_at is not None:
        spans.append((open_at, len(body)))
    return spans


def _in_spans(pos: int, spans: list[tuple[int, int]]) -> bool:
    return any(a <= pos < b for a, b in spans)


def _on_heading_line(body: str, pos: int) -> bool:
    start = body.rfind("\n", 0, pos) + 1
    return bool(re.match(r"[ \t]{0,3}#{1,6}[ \t]", body[start:pos + 1]))


def repair_buy_links(body_md: str, cfg: dict | None, *,
                     product_ok: "Callable[[str], bool] | None" = None,
                     own_hosts: Iterable[str] | None = None,
                     max_add: int = 12) -> tuple[str, list[str]]:
    """Add a buy link after the first mention of each product the body links
    through its product page (`product_page_patterns`) but gives no buy link.

    The link is ` ([<buy_link_label>](<buy_link_template | kitchen_buy_template>))`,
    with `{site_root}` left empty so it stays a relative own-host link
    (e.g. ` ([Check price](/k/<slug>?source=amazon))`). Only products
    `product_ok` accepts get one (pass the catalog check: an inactive or
    unknown product has nothing to buy); a link in a heading or a code block
    is skipped in favour of a later mention. Prose is never removed, so the
    repair is idempotent: a product that has its buy link is left alone.
    Returns (body, notes); a site with no template or no product-page
    pattern gets the body back unchanged.
    """
    cfg = cfg or {}
    body = body_md or ""
    tpl = str(cfg.get("buy_link_template") or cfg.get("kitchen_buy_template") or "")
    pats = _compile_patterns(cfg.get("product_page_patterns"))
    if not body or "{slug}" not in tpl or not pats:
        return body, []
    hosts = own_hosts if own_hosts is not None else site_hosts_from_root(cfg.get("site_root") or "")
    label = str(cfg.get("buy_link_label") or "Check price")
    have = {_slug_of(t) for t in buy_link_targets(body, cfg.get("buy_link_patterns"), hosts)}
    fenced = _fenced_spans(body)
    done: set[str] = set()
    inserts: list[tuple[int, str]] = []
    for m, target in _iter_link_targets(body, hosts):
        if not _matches(pats, target):
            continue
        slug = _slug_of(_target_key(target))
        if not slug or slug in done or slug in have:
            continue
        if product_ok is not None and not product_ok(slug):
            done.add(slug)
            continue
        if _in_spans(m.start(), fenced) or _on_heading_line(body, m.start()):
            continue
        end = m.end()
        # "**[Name](url)**": the buy link goes after the emphasis, not inside it
        for mark in ("**", "__", "*", "_"):
            if body[m.start() - len(mark):m.start()] == mark and body[end:end + len(mark)] == mark:
                end += len(mark)
                break
        inserts.append((end, slug))
        done.add(slug)
        if len(inserts) >= max_add:
            break
    if not inserts:
        return body, []
    out, last, notes = [], 0, []
    for end, slug in inserts:
        try:
            href = tpl.format(site_root="", slug=slug)
        except (KeyError, IndexError, ValueError):
            return body, []
        out.append(body[last:end])
        out.append(f" ([{label}]({href}))")
        last = end
        notes.append(f"added buy link for {slug}")
    out.append(body[last:])
    return "".join(out), notes


# Link text holding one level of brackets, followed by its (url):
# "[Black Forest Labs — FLUX.2 [klein] 4B model card](https://…)". Renderers
# that match link text with [^\]]+ stop at the inner "]" and print the rest
# as raw markdown (4475, 2026-11-12 review).
_BRACKETED_ANCHOR_RE = re.compile(
    r"(?<!!)\[(?P<text>(?:[^\[\]\n]|\[[^\[\]\n]*\])*\[[^\[\]\n]*\](?:[^\[\]\n]|\[[^\[\]\n]*\])*)\]"
    r"(?=\([^)\s]+(?:\s+\"[^\"]*\")?\))"
)
_INNER_LINK_RE = re.compile(r"\[([^\[\]\n]*)\]\([^)\s]*\)")


def find_bracketed_anchors(body_md: str) -> list[str]:
    """Link texts that hold brackets (outside code blocks)."""
    body = body_md or ""
    fenced = _fenced_spans(body)
    return [m.group("text") for m in _BRACKETED_ANCHOR_RE.finditer(body)
            if not _in_spans(m.start(), fenced)]


def repair_anchor_brackets(body_md: str) -> tuple[str, list[str]]:
    """Make link text bracket-free: a link nested in the text becomes its
    words ("[Check the [Noctua](/product/X) on Amazon](u)" → "[Check the
    Noctua on Amazon](u)"), and other inner brackets become parentheses
    ("FLUX.2 [klein] 4B" → "FLUX.2 (klein) 4B"). Code blocks are untouched.
    Returns (body, notes)."""
    body = body_md or ""
    fenced = _fenced_spans(body)
    notes: list[str] = []

    def _fix(m: "re.Match[str]") -> str:
        if _in_spans(m.start(), fenced):
            return m.group(0)
        text = m.group("text")
        new = _INNER_LINK_RE.sub(lambda x: x.group(1), text).replace("[", "(").replace("]", ")")
        notes.append(f"[{text}] -> [{new}]")
        return f"[{new}]"

    return _BRACKETED_ANCHOR_RE.sub(_fix, body), notes


def split_kitchen_slugs(slugs: Iterable[str],
                        category_slugs: Iterable[str],
                        product_slugs: Iterable[str]) -> dict[str, list[str]]:
    """Split a proposal's mixed kitchen slugs by what they really are.

    The caller (a site's proposer) resolves the two sets against its own
    catalog — the guard never queries a site DB. Returns
    {"categories": [...], "products": [...], "unknown": [...]}; unknown
    slugs (hallucinated or retired) should not be handed to the writer,
    because a link to them 404s.
    """
    cats = set(category_slugs or [])
    prods = set(product_slugs or [])
    out: dict[str, list[str]] = {"categories": [], "products": [], "unknown": []}
    for s in slugs or []:
        if not isinstance(s, str) or not s:
            continue
        bucket = ("categories" if s in cats else
                  "products" if s in prods else "unknown")
        if s not in out[bucket]:
            out[bucket].append(s)
    return out


# ── Kitchen slug repair (2026-09-25 audit F101/F118) ─────────────────────
# The guard counted kitchen links without checking that they resolve, so an
# invented slug (/k/thermoworks-thermapen-one, /k/kitchen-scales) counted
# toward min_kits and shipped as a 404: 53 dead links in 23 aisleprompt
# articles. The site-agnostic part lives here; the caller supplies the lookup
# (kitchen_slug_resolver_from_db reads table names from the site's
# `kitchen_slug_check` config — the guard never names a site's schema).

_KITCHEN_LINK_RE = re.compile(
    r"\[(?P<text>[^\]]+)\]"
    r"\((?P<host>https?://[^/)]*)?"
    r"(?P<root>/(?:kitchen/category|kitchen|k)/)"
    r"(?P<slug>[a-z0-9][a-z0-9-]*)(?P<rest>(?:/[a-z0-9-]+)?(?:[?#][^)]*)?)\)"
)
_SQL_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


def de_stutter_candidates(slug: str) -> list[str]:
    """Doubled / tripled leading-token forms of a slug ("acme-acme-pan" ->
    "acme-pan"), nearest first. A lookup keeps whichever exists."""
    out: list[str] = []
    frontier = [slug]
    for _ in range(3):
        nxt: list[str] = []
        for s in frontier:
            parts = s.split("-")
            for k in range(1, 5):
                if 2 * k > len(parts):
                    break
                if parts[:k] == parts[k:2 * k]:
                    c = "-".join(parts[k:])
                    if c and c != slug and c not in out:
                        out.append(c)
                        nxt.append(c)
        frontier = nxt
    return out


def kitchen_link_slugs(body_md: str) -> list[tuple[str, str]]:
    """(root, slug) of every kitchen markdown link, in body order."""
    return [(m.group("root"), m.group("slug")) for m in _KITCHEN_LINK_RE.finditer(body_md or "")]


def site_hosts_from_root(site_root: str) -> set[str]:
    """{"example.com", "www.example.com"} for a site_root like
    "https://example.com" — the hosts whose absolute links are the site's own."""
    host = re.sub(r"^https?://", "", str(site_root or "").strip().lower()).split("/")[0]
    if not host:
        return set()
    bare = host[4:] if host.startswith("www.") else host
    return {bare, f"www.{bare}"}


def repair_kitchen_links(body_md: str, resolve, own_hosts: Iterable[str] | None = None) -> tuple[str, list[str]]:
    """Canonicalize or unlink kitchen links that do not resolve.

    `resolve(root, slug)` returns "keep", ("rewrite", new_slug) or "unlink".
    An unlinked link keeps its anchor text as plain prose — nothing a reader
    sees is lost, and a 404 never ships. Returns (body, notes).

    Only the site's own links are judged: a relative link, or an absolute one
    whose host is in `own_hosts` (see site_hosts_from_root). A citation to
    another site whose path happens to start with /kitchen/ or /k/ is never
    touched — the site's catalog says nothing about it.
    """
    notes: list[str] = []
    hosts = {h.lower() for h in (own_hosts or [])}

    def _fix(m: "re.Match[str]") -> str:
        host = (m.group("host") or "").lower()
        if host and re.sub(r"^https?://", "", host) not in hosts:
            return m.group(0)
        root, slug = m.group("root"), m.group("slug")
        verdict = resolve(root, slug)
        if verdict == "keep" or verdict is None:
            return m.group(0)
        if isinstance(verdict, tuple) and verdict[0] == "rewrite":
            notes.append(f"{root}{slug} -> {root}{verdict[1]}")
            return f"[{m.group('text')}]({m.group('host') or ''}{root}{verdict[1]}{m.group('rest')})"
        notes.append(f"unlinked {root}{slug}")
        return m.group("text")

    body = _KITCHEN_LINK_RE.sub(_fix, body_md or "")
    policy = getattr(resolve, "route_policy", None)
    if policy:
        body, route_notes = repair_unknown_routes(
            body, policy.get("known_routes") or [], own_hosts=hosts,
            route_rewrites=policy.get("route_rewrites") or {})
        notes.extend(route_notes)
    return body, notes


def kitchen_slug_resolver_from_db(conn, body_md: str, check_cfg: dict):
    """Build a `resolve` for repair_kitchen_links from the site's catalog.

    `check_cfg` (the site's `kitchen_slug_check` guard config):
      product_table / product_active_column — the product rows (/k/<slug>,
          /kitchen/<slug>); only active rows count;
      category_table — top-level category slugs (/k/<slug>,
          /kitchen/category/<slug>);
      static_slugs — slugs the site resolves in code (e.g. subcategories);
      reserved_slugs — non-product pages under the product root
          (/kitchen/buying-guides), always kept;
      subcategory_parents — {sub: parent} for subcategories that only
          resolve UNDER their parent (/kitchen/category/<parent>/<sub>).
          A top-level /kitchen/category/<sub> link is rewritten to the
          nested path (2026-10-01 audit: /kitchen/category/pressure-cookers
          fell through to the soft-404 shell; the real page is
          /kitchen/category/small-appliances/pressure-cookers);
      known_routes / route_rewrites — see repair_unknown_routes(); when set
          the returned resolver carries them and repair_kitchen_links applies
          them to every other own-host link in the same pass.
    One query per table for all slugs in the body.
    """
    prod_t = str(check_cfg.get("product_table") or "")
    active_c = str(check_cfg.get("product_active_column") or "")
    cat_t = str(check_cfg.get("category_table") or "")
    for ident in (prod_t, active_c, cat_t):
        if ident and not _SQL_IDENT_RE.match(ident):
            raise ValueError(f"kitchen_slug_check: bad identifier {ident!r}")
    static = set(check_cfg.get("static_slugs") or [])
    reserved = set(check_cfg.get("reserved_slugs") or [])
    parents = {str(k): str(v) for k, v in (check_cfg.get("subcategory_parents") or {}).items()
               if k and v}
    pairs = kitchen_link_slugs(body_md)
    slugs = {s for _, s in pairs}
    cands = set(slugs)
    for s in slugs:
        cands.update(de_stutter_candidates(s))
    active: set[str] = set()
    cats: set[str] = set()
    if cands:
        with conn.cursor() as cur:
            if prod_t:
                where_active = f" AND {active_c} = TRUE" if active_c else ""
                cur.execute(f"SELECT slug FROM {prod_t} WHERE slug = ANY(%s){where_active}", (list(cands),))
                active = {r[0] for r in cur.fetchall()}
            if cat_t:
                cur.execute(f"SELECT slug FROM {cat_t} WHERE slug = ANY(%s)", (list(cands),))
                cats = {r[0] for r in cur.fetchall()}

    def resolve(root: str, slug: str):
        if slug in reserved:
            return "keep"
        if root == "/kitchen/category/":
            if slug in parents and slug not in cats:
                return ("rewrite", f"{parents[slug]}/{slug}")
            return "keep" if (slug in cats or slug in static) else "unlink"
        if slug in active or slug in cats or slug in static:
            return "keep"
        for c in de_stutter_candidates(slug):
            if c in active:
                return ("rewrite", c)
        return "unlink"

    if check_cfg.get("known_routes"):
        resolve.route_policy = {  # type: ignore[attr-defined]
            "known_routes": list(check_cfg.get("known_routes") or []),
            "route_rewrites": dict(check_cfg.get("route_rewrites") or {}),
        }
    return resolve


# ── Unknown own-host routes (2026-10-01 audit) ───────────────────────────
# Article bodies linked /cuisine, /holidays/thanksgiving and
# /kitchen/category/pressure-cookers. None is a route: the SPA catch-all
# answered 200 with the homepage shell (a soft 404 with a canonical to /),
# so the link guard counted them and they shipped. The site lists its real
# route shapes in config (`known_routes`, regexes over the path); an
# own-host link matching none of them is rewritten via `route_rewrites`
# ({path_regex: replacement}) or unlinked with its anchor text kept.
_ANY_LINK_RE = re.compile(
    r"(?<!!)\[(?P<text>[^\]]+)\]\((?P<host>https?://[^/)\s]*)?(?P<path>/[^)\s?#]*)"
    r"(?P<rest>[?#][^)\s]*)?\)"
)


def repair_unknown_routes(body_md: str, known_routes: Iterable[str],
                          own_hosts: Iterable[str] | None = None,
                          route_rewrites: dict | None = None) -> tuple[str, list[str]]:
    """Rewrite or unlink own-host links whose path matches no known route.

    `known_routes`: regexes matched (fullmatch) against the link path, e.g.
    r"/recipes/cuisine/[a-z0-9-]+". `route_rewrites`: {regex: replacement}
    applied (fullmatch, re.sub semantics) before unlinking, so a known-wrong
    shape becomes the real page. An empty `known_routes` changes nothing.
    Returns (body, notes)."""
    pats = [re.compile(p) for p in (known_routes or []) if p]
    if not pats:
        return body_md or "", []
    rewrites = [(re.compile(k), v) for k, v in (route_rewrites or {}).items() if k]
    hosts = {h.lower() for h in (own_hosts or [])}
    notes: list[str] = []

    def _known(path: str) -> bool:
        return any(p.fullmatch(path) for p in pats)

    def _fix(m: "re.Match[str]") -> str:
        host = re.sub(r"^https?://", "", (m.group("host") or "").lower())
        if host and host not in hosts:
            return m.group(0)
        path = m.group("path")
        norm = path.rstrip("/") or "/"
        if _known(norm):
            return m.group(0)
        for rx, repl in rewrites:
            if rx.fullmatch(norm):
                new = rx.sub(repl, norm)
                if _known(new):
                    notes.append(f"{norm} -> {new}")
                    return f"[{m.group('text')}]({m.group('host') or ''}{new}{m.group('rest') or ''})"
        notes.append(f"unlinked unknown route {norm}")
        return m.group("text")

    return _ANY_LINK_RE.sub(_fix, body_md or ""), notes


def _strip_id(slug: str) -> str:
    """Normalize 'foo-bar-12345' → 'foo-bar'.

    Real aisleprompt recipe URLs end in `-<id>` but the proposal's
    expected slugs are bare. We compare the bare form so a slug with
    or without the id reads the same.
    """
    return re.sub(r"-\d+$", "", slug or "")


# ---------------------------------------------------------------------------
# Prompt-side enforcement — inject this BEFORE the LLM runs so the model
# knows up-front it must produce the links (rather than failing the
# verify step after the fact).
# ---------------------------------------------------------------------------

def render_link_directive(proposal: dict,
                          *,
                          min_recipes: int = 5,
                          min_kits: int = 2,
                          min_products: int = 0,
                          site_root: str = "https://aisleprompt.com",
                          link_cfg: dict | None = None,
                          min_buy_links: int | None = None) -> str:
    """Returns a directive block the implementer can paste into its
    aider/claude prompt. The directive lists every expected slug and
    explains the inline-link contract.

    Sites that don't have one of the two link types (e.g. specpicks has
    no /recipes/) pass min_recipes=0 and the directive adapts.
    Specpicks passes min_products>=3 so the LLM knows to wrap named
    hardware SKUs (ZOTAC RTX 3060 12GB, Ryzen 7 5800X, etc.) in
    /product/<ASIN> markdown links at first mention.

    `link_cfg` is the site's resolved guard config (`resolve_minima()`).
    Its kitchen link shapes, all `{site_root}`/`{slug}` templates:
      kitchen_category_template — category pages (crawlable)
      kitchen_product_template  — product pages (crawlable)
      kitchen_buy_template      — optional affiliate click-out for an
                                  explicit buy link (the site's renderer
                                  must mark it rel="sponsored nofollow");
                                  omit to not ask for one
      kitchen_link_template     — the legacy unsplit list (default /k/)

    `min_buy_links` — the direct-buy-link minimum; None derives it from
    `link_cfg`, the proposal's bucket + title and the number of catalog
    products it lists (`min_buy_links_for`, product-page trigger). When it is
    above 0 the directive adds a DIRECT BUY LINKS block in the shape of
    `buy_link_template` (falling back to `kitchen_buy_template`), filled only
    with product slugs: a split proposal's category slugs never become a
    "Check price" line, and a split proposal with no products is told to
    defer, because a buy-shaped link to a category is not a buy link.
    """
    cfg = link_cfg or {}
    recipes: list[str] = proposal.get("expected_recipe_slugs") or []
    kits: list[str] = proposal.get("expected_kitchen_slugs") or []
    products: list[str] = (proposal.get("expected_product_asins")
                             or proposal.get("expected_asins") or [])
    kit_cats: list[str] = proposal.get("expected_kitchen_category_slugs") or []
    kit_prods: list[str] = proposal.get("expected_kitchen_product_slugs") or []
    split = bool(kit_cats or kit_prods)
    min_buy = (_as_int(min_buy_links) if min_buy_links is not None else min_buy_links_for(
        cfg, bucket=[str(proposal.get("bucket") or ""), str(proposal.get("category") or "")],
        title=str(proposal.get("title") or ""),
        product_pages=len({s for s in kit_prods if isinstance(s, str) and s})))
    buy_clause = (f", and {min_buy} distinct direct buy links" if min_buy else "")
    lines: list[str] = [
        "INLINE-LINK CONTRACT (HARD REQUIREMENT — verified after exit):",
        f"  The wrapper counts /recipes/ + kitchen + /product/ markdown "
        f"links in your output. It rejects the article (EDIT INCOMPLETE) "
        f"if the body contains fewer than {min_recipes} distinct "
        f"/recipes/ links, {min_kits} distinct kitchen links, "
        + ("" if min_buy else "and ")
        + f"{min_products} distinct /product/ links{buy_clause} — the article will "
        f"NOT be inserted, the proposal will be re-queued, and you'll "
        f"be asked to do this work again. Better to put the links in "
        f"once.",
        "",
    ]
    if recipes:
        lines.append(f"  RECIPES TO LINK ({len(recipes)} provided — use these "
                     f"slugs verbatim; pick at least {min_recipes}):")
        for s in recipes[:20]:
            lines.append(f"    [Recipe Name]({site_root}/recipes/{s}-<id>)  "
                         f"← bare slug = {s}")
        lines.append("")
        lines.append(
            "  CRITICAL: pick a real recipe title for the anchor text. "
            "Don't use generic 'click here' or the slug itself. Render "
            "each link inline, woven into a sentence in the body — not "
            "as a bare list at the bottom.")
        lines.append("")

    def _tpl(key: str, default: str, slug: str) -> str:
        return (cfg.get(key) or default).format(site_root=site_root, slug=slug)

    if split:
        cat_tpl_default = cfg.get("kitchen_link_template") or DEFAULT_KITCHEN_LINK_TEMPLATE
        lines.append(f"  KITCHEN LINKS — at least {min_kits} distinct, from the "
                     f"slugs below verbatim (they were checked against the live "
                     f"catalog; do not invent others):")
        if kit_prods:
            lines.append(f"  Products ({len(kit_prods)}) — link the product NAME at "
                         f"first mention to its page:")
            for s in kit_prods[:10]:
                lines.append(f"    [Product Name]({_tpl('kitchen_product_template', cat_tpl_default, s)})")
            if cfg.get("kitchen_buy_template"):
                lines.append("    For an explicit buy/price link (\"Check price\"), use "
                             + _tpl("kitchen_buy_template", "", "<slug>")
                             + " — that is the affiliate click-out. Never write a "
                               "raw retailer URL.")
        if kit_cats:
            lines.append(f"  Categories ({len(kit_cats)}):")
            for s in kit_cats[:10]:
                lines.append(f"    [Category Name]({_tpl('kitchen_category_template', cat_tpl_default, s)})")
        lines.append("")
    elif kits:
        lines.append(f"  KITCHEN LINKS ({len(kits)} provided — "
                     f"use these slugs verbatim; pick at least {min_kits}):")
        for s in kits[:10]:
            lines.append(f"    [Name]({_tpl('kitchen_link_template', DEFAULT_KITCHEN_LINK_TEMPLATE, s)})")
        lines.append("")
    if min_buy > 0:
        # 2026-11-12 review: gift guides and a checklist named every product
        # through its product page and shipped with zero buy buttons.
        buy_tpl = cfg.get("buy_link_template") or cfg.get("kitchen_buy_template") or ""
        # Only product slugs: a split proposal's categories are never offered
        # as a buy link (/k/<category> redirects to the category page). The
        # legacy unsplit list cannot tell the two apart.
        buy_slugs = [s for s in (kit_prods if split else kits) if isinstance(s, str) and s][:10]
        lines.append(f"  DIRECT BUY LINKS — at least {min_buy} distinct (HARD REQUIREMENT "
                     f"for this article's bucket/title, or because it names products). "
                     f"Give EVERY product you name as a pick, gift or checklist item its "
                     f"own buy link, next to its name or in its row of the picks table:")
        if split and not buy_slugs:
            lines.append("    This proposal lists no catalog products, only categories. A "
                         "buy-shaped link to a category (/k/<category>) is NOT a buy link: it "
                         "redirects to the category page and the guard does not count it. "
                         "This minimum cannot be met from this proposal: write `DEFERRED: "
                         "link minimum unreachable (no catalog products)` to "
                         "changes/<rec-id>.summary.md and do not insert the article.")
        elif buy_tpl:
            for s in (buy_slugs or ["<product-slug>"]):
                lines.append(f"    [Check price]({buy_tpl.format(site_root=site_root, slug=s)})")
            if not split and buy_slugs:
                lines.append("    Use only the slugs above that are PRODUCTS: a buy link to a "
                             "category slug is not a buy link and is not counted.")
        else:
            pats = ", ".join(cfg.get("buy_link_patterns") or DEFAULT_BUY_LINK_PATTERNS)
            lines.append(f"    (a buy link is a link whose target matches: {pats})")
        if not (split and not buy_slugs):
            prod_tpl = cfg.get("kitchen_product_template") or ""
            page_shape = (prod_tpl.format(site_root=site_root, slug="<slug>")
                          if prod_tpl else "the product's own page")
            lines.append(f"    A product-page link ({page_shape}) names the product but is "
                         f"NOT a buy link: keep it on the name AND add the buy link. Use "
                         f"only the product slugs listed above, never a raw retailer URL.")
        lines.append("")
    if min_products > 0:
        lines.append(f"  PRODUCTS TO LINK — wrap named hardware SKUs "
                     f"(GPUs, CPUs, monitors, controllers, kits) in "
                     f"/product/<ASIN> markdown links at first mention. "
                     f"Minimum {min_products} distinct /product/ links "
                     f"required. Example anchor shape:")
        lines.append(f"    [ZOTAC RTX 3060 12GB]({site_root}/product/"
                     f"B08W8BFG9B?tag=specpicks-articles-20)")
        if products:
            lines.append("")
            lines.append(f"  Suggested ASINs ({len(products)} provided by "
                         f"the proposal — use these when their SKU is "
                         f"referenced in the body):")
            for a in products[:12]:
                lines.append(f"    /product/{a}?tag=specpicks-articles-20")
        lines.append("")
        lines.append(
            "  CRITICAL: only link ACTIVE catalog ASINs. Don't invent "
            "an ASIN. If a named product isn't in the suggested list "
            "and you're not sure of its ASIN, either (a) link to the "
            "matching /buying-guide/<slug> instead, or (b) omit that "
            "link — but hit the minimum with other named SKUs.")
        lines.append("")
    lines.append(
        "  Use absolute paths (`/recipes/...`, `/kitchen/...`, `/product/...`) "
        "— relative paths resolve under /blog and break.")
    lines.append("")
    return "\n".join(lines)


def render_failure_addendum(audit: LinkAuditResult) -> str:
    """Used when the wrapper re-prompts the LLM after a failed verify.
    Explains what was missing so the model fixes the exact gap."""
    buy = (f", and {audit.buy_links}/{audit.min_buy_links} required direct buy "
           f"links (one per named product — a product-page link is not one)"
           if audit.min_buy_links else "")
    return (
        "INLINE-LINK CONTRACT — VIOLATED. You wrote "
        f"{audit.recipe_links}/{audit.min_recipes} required /recipes/ "
        f"links, {audit.kitchen_links}/{audit.min_kits} required "
        f"kitchen links, " + ("" if buy else "and ")
        + f"{audit.product_links}/{audit.min_products} "
        f"required /product/ links{buy}. The article is NOT shippable in "
        f"this state. Add additional inline links to the body using "
        f"the expected slugs / ASINs already provided; do not delete "
        f"any existing prose. After adding the links, save the file "
        f"again and exit cleanly."
    )


# ── Per-site minima resolution ─────────────────────────────────────────────
# The minima are per-deployment VALUES, so they live in config, not code
# (framework-first). Resolution: repo default file, with the storage copy laid
# over it block by block → zeros when neither is readable (with a stderr
# warning, because silently-disabled enforcement is how the 881
# zero-product-link specpicks articles happened).
import json as _json
import sys as _sys
from pathlib import Path as _Path

_REPO_CONFIG = _Path(__file__).resolve().parents[2] / "config" / "article-link-guard-config.json"
_CONFIG_KEY = "config/article-link-guard-config.json"


def merge_guard_config(base: dict | None, over: dict | None) -> dict:
    """Lay `over` (the storage copy) on `base` (the repo default).

    Top-level keys replace, except `default` and `sites`, which merge per
    block: a site block in `over` updates only the keys it names. An older
    storage copy therefore cannot silently drop a knob added to the repo
    default since (2026-11-12: the live copy predated `kitchen_slug_check`,
    `known_routes` and the buy-link knobs). To switch a knob off from
    storage, set it to 0 / [] / null rather than deleting it.
    """
    base = base or {}
    out = dict(base)
    for k, v in (over or {}).items():
        if k == "default" and isinstance(v, dict):
            out["default"] = {**(base.get("default") or {}), **v}
        elif k == "sites" and isinstance(v, dict):
            sites = {sk: dict(sv or {}) for sk, sv in (base.get("sites") or {}).items()}
            for sk, sv in v.items():
                sites[sk] = {**sites.get(sk, {}), **(sv or {})}
            out["sites"] = sites
        else:
            out[k] = v
    return out


def load_guard_config(storage=None) -> dict | None:
    """The repo default overlaid with the storage copy (`merge_guard_config`);
    None when neither is readable."""
    repo = stored = None
    try:
        repo = _json.loads(_REPO_CONFIG.read_text())
    except Exception as e:
        _sys.stderr.write(f"[link-guard] repo default unreadable ({e})\n")
    if storage is not None:
        try:
            stored = storage.read_json(_CONFIG_KEY) or None
        except Exception:
            stored = None
    if repo is None and stored is None:
        return None
    return merge_guard_config(repo, stored)


def resolve_minima(site_hint: str, storage=None) -> dict:
    """Return the site's guard config: {min_recipes, min_kits, min_products,
    site_root} plus the optional knobs that `verify_body` /
    `verify_body_with_config` / `render_link_directive` read: kitchen
    (kitchen_roots, kitchen_*_template, kitchen_slug_check) and direct buy
    links (min_buy_links, min_buy_links_by_bucket, min_buy_links_by_title,
    min_buy_links_if_product_pages, product_page_patterns, buy_link_patterns,
    buy_link_template, buy_link_label).

    `site_hint` is whatever the caller has — a per-site agent_id
    ("specpicks-article-proposal-agent") or a bare site name; matching is
    by substring against the config's `sites` keys. `storage` (optional)
    supplies the storage copy, laid over the repo default.
    """
    cfg = load_guard_config(storage)
    if cfg is None:
        _sys.stderr.write("[link-guard] config unreadable; minima default to 0 — FIX THIS\n")
        return {"min_recipes": 0, "min_kits": 0, "min_products": 0,
                "site_root": ""}
    vals = dict(cfg.get("default") or {})
    hint = (site_hint or "").lower()
    for key, over in (cfg.get("sites") or {}).items():
        if key.lower() in hint:
            vals.update(over or {})
            break
    return vals


# ── Outbound-citation quality (2026-10-01 audit, thin-outbound-citations) ──
# Proposals cited homepage roots (instantpot.com/, baratza.com/,
# nchfp.uga.edu/), Shopify storefronts (*.myshopify.com) and mostly
# Wikipedia. A root or a storefront supports no specific claim, so an answer
# engine gains nothing from it. The proposer filters its
# `outbound_citations` through `audit_citations` before the writer copies
# them; per-site knobs live under `citations` in the guard config.
DEFAULT_CITATION_POLICY: dict = {
    "min_qualifying": 3,          # citations that pass every rule
    "min_non_wikipedia": 2,       # of those, how many must not be Wikipedia
    # host regexes that never qualify (storefronts, aggregators, social)
    "reject_hosts": [r"(^|\.)myshopify\.com$", r"(^|\.)amazon\.[a-z.]+$", r"(^|\.)amzn\.to$",
                     r"(^|\.)ebay\.[a-z.]+$", r"(^|\.)walmart\.com$", r"(^|\.)pinterest\.[a-z.]+$",
                     r"(^|\.)facebook\.com$", r"(^|\.)instagram\.com$", r"(^|\.)tiktok\.com$"],
    # path regexes (fullmatch, after stripping a trailing slash) that are
    # hubs, not pages that state a fact
    "hub_paths": [r"", r"/(?:blog|news|shop|store|products?|collections?|recipes|reviews|research)",
                  r"/[a-z]{2}(?:-[a-z]{2})?"],
}


@dataclasses.dataclass
class CitationAudit:
    kept: list[str]
    rejected: list[tuple[str, str]]
    min_qualifying: int
    min_non_wikipedia: int

    @property
    def non_wikipedia(self) -> int:
        return sum(1 for u in self.kept if "wikipedia.org" not in _host(u))

    @property
    def passes(self) -> bool:
        return (len(self.kept) >= self.min_qualifying
                and self.non_wikipedia >= min(self.min_non_wikipedia, self.min_qualifying))

    def failure_reason(self) -> str:
        if self.passes:
            return ""
        why = "; ".join(f"{u} ({r})" for u, r in self.rejected[:4])
        return (f"{len(self.kept)} qualifying citation(s) (min {self.min_qualifying}, "
                f"{self.non_wikipedia} non-Wikipedia, min {self.min_non_wikipedia})"
                + (f"; rejected: {why}" if why else ""))


def _host(url: str) -> str:
    return re.sub(r"^https?://", "", str(url or "").strip().lower()).split("/")[0].split("?")[0]


def audit_citations(urls: Iterable[str], policy: dict | None = None,
                    own_hosts: Iterable[str] | None = None) -> CitationAudit:
    """Split outbound citations into qualifying deep pages and rejects
    (bare roots, hub paths, storefronts, the site's own host, non-http,
    duplicates). `policy` overrides DEFAULT_CITATION_POLICY keys."""
    pol = dict(DEFAULT_CITATION_POLICY)
    pol.update(policy or {})
    bad_hosts = [re.compile(p) for p in pol.get("reject_hosts") or []]
    hubs = [re.compile(p, re.I) for p in pol.get("hub_paths") or []]
    own = {h.lower() for h in (own_hosts or [])}
    kept: list[str] = []
    rejected: list[tuple[str, str]] = []
    seen: set[str] = set()
    for u in urls or []:
        url = str(u or "").strip()
        if not re.match(r"^https?://", url, re.I):
            rejected.append((url, "not an http(s) URL"))
            continue
        host = _host(url)
        bare = host[4:] if host.startswith("www.") else host
        path = re.sub(r"^https?://[^/]+", "", url, flags=re.I).split("?")[0].split("#")[0]
        path = path.rstrip("/")
        key = f"{bare}{path}".lower()
        if key in seen:
            rejected.append((url, "duplicate"))
            continue
        seen.add(key)
        if host in own or bare in own:
            rejected.append((url, "own site"))
        elif any(rx.search(bare) for rx in bad_hosts):
            rejected.append((url, "storefront / aggregator host"))
        elif any(rx.fullmatch(path) for rx in hubs):
            rejected.append((url, "homepage or hub, not a page that states the fact"))
        else:
            kept.append(url)
    return CitationAudit(kept=kept, rejected=rejected,
                         min_qualifying=int(pol.get("min_qualifying") or 0),
                         min_non_wikipedia=int(pol.get("min_non_wikipedia") or 0))


def resolve_citation_policy(site_hint: str, storage=None) -> dict:
    """The site's `citations` block from the guard config (storage copy
    overrides the repo default), merged over DEFAULT_CITATION_POLICY."""
    pol = dict(DEFAULT_CITATION_POLICY)
    pol.update((resolve_minima(site_hint, storage) or {}).get("citations") or {})
    return pol


def filter_proposal_citations(proposals: list[dict], policy: dict | None,
                              own_hosts: Iterable[str],
                              on_note=None, key: str = "outbound_citations") -> int:
    """Proposer-side use of audit_citations: trim each proposal's citation
    list to qualifying deep pages, in place (the writer copies the list
    verbatim and links every entry, so a homepage root here ships as a
    homepage citation). Returns how many proposals end below the minimum;
    `on_note(str)` receives what was dropped and which proposals fell short."""
    short = 0
    for p in proposals or []:
        cites = p.get(key) or []
        if not cites:
            continue
        a = audit_citations(cites, policy, own_hosts=own_hosts)
        if a.rejected and on_note:
            on_note(f"{p.get('id')}: dropped citation(s) "
                    + "; ".join(f"{u} ({r})" for u, r in a.rejected[:5]))
        p[key] = a.kept
        if not a.passes:
            short += 1
            if on_note:
                on_note(f"{p.get('id')}: citations below minimum — {a.failure_reason()}")
    return short
