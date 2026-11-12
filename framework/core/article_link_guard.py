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
`min_buy_links_by_title` — see `min_buy_links_for`).

The guard is a thin layer — it doesn't try to resolve slug ↔ catalog
ids (that's `resolve-article-links.py`'s job). It just measures
"does the article actually link out, yes/no, how many."
"""
from __future__ import annotations

import dataclasses
import re
from typing import Iterable


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
            return (f"only {self.buy_links} direct buy links "
                    f"(min {self.min_buy_links}) — every named product needs "
                    f"its own buy link, a product-page link is not one")
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
    """
    if isinstance(patterns, str):     # one pattern given bare in config
        patterns = [patterns]
    pats = []
    for p in (patterns if patterns else DEFAULT_BUY_LINK_PATTERNS):
        try:
            if p:
                pats.append(re.compile(p, re.I))
        except re.error:
            continue
    hosts = {str(h).lower() for h in (own_hosts or [])}
    found: set[str] = set()
    for m in _MD_LINK_RE.finditer(body_md or ""):
        url = m.group("url").strip("<>")
        hm = re.match(r"^https?://([^/?#]+)(.*)$", url, re.I)
        target = (hm.group(2) or "/") if (hm and hm.group(1).lower() in hosts) else url
        if any(p.search(target) for p in pats):
            found.add(re.split(r"[?#]", target, maxsplit=1)[0].rstrip("/").lower())
    return found


def _as_int(v) -> int:
    try:
        return max(int(v or 0), 0)
    except (TypeError, ValueError):
        return 0


def min_buy_links_for(cfg: dict | None, *, bucket: "str | Iterable[str]" = "",
                      title: str = "") -> int:
    """The direct-buy-link minimum for one article, from the site's guard
    config (`resolve_minima()`):

      min_buy_links            — the site's default (0 when unset);
      min_buy_links_by_bucket  — {bucket: n}; a listed bucket REPLACES the
                                 default (so 0 exempts a bucket);
      min_buy_links_by_title   — {title regex: n}; every pattern that matches
                                 the title RAISES the minimum to at least n.
                                 Gift and checklist pages ride several
                                 buckets, so they are caught by title.

    `bucket` may be several names (a row's `bucket` and `category` can
    differ: a gift guide proposed as seasonal-occasion was stored with
    category buying-guide); the highest listed minimum among them applies.
    Bucket keys and title patterns are case-insensitive. A bad regex in the
    config is skipped rather than taking the guard down.
    """
    cfg = cfg or {}
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
                own_hosts: Iterable[str] | None = None) -> LinkAuditResult:
    """Audit `body_md` against the proposal's expected slug lists.

    `kitchen_roots` — path roots whose links count as kitchen links
    (default `DEFAULT_KITCHEN_ROOTS`). A site whose category/product pages
    are crawlable (e.g. `/kitchen/category/`, `/kitchen/`) lists them so an
    article linking the crawlable page satisfies the contract.

    `min_buy_links` / `buy_link_patterns` / `own_hosts` — the direct-buy-link
    minimum (see `buy_link_targets`, `min_buy_links_for`). 0 by default, so a
    caller that passes nothing keeps the old verdict.
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
        buy_links=len(buy_link_targets(body_md, buy_link_patterns, own_hosts)),
        min_buy_links=_as_int(min_buy_links),
    )


def verify_body_with_config(body_md: str,
                            proposal: dict | None,
                            cfg: dict | None,
                            *,
                            bucket: "str | Iterable[str] | None" = None,
                            title: str | None = None) -> LinkAuditResult:
    """`verify_body` with every minimum taken from the site's resolved guard
    config (`resolve_minima()`), so the implementer's INSERT gate, the
    author's pre-INSERT self-check and any sweep apply the same contract.

    `bucket` / `title` default to the proposal's `bucket` + `category` and
    `title`; they select the direct-buy-link minimum (`min_buy_links_for`).
    """
    cfg = cfg or {}
    proposal = proposal or {}
    b = bucket if bucket is not None else [str(proposal.get("bucket") or ""),
                                           str(proposal.get("category") or "")]
    t = title if title is not None else str(proposal.get("title") or "")
    return verify_body(
        body_md, proposal,
        min_recipes=_as_int(cfg.get("min_recipes")),
        min_kits=_as_int(cfg.get("min_kits")),
        min_products=_as_int(cfg.get("min_products")),
        kitchen_roots=cfg.get("kitchen_roots"),
        min_buy_links=min_buy_links_for(cfg, bucket=b, title=t),
        buy_link_patterns=cfg.get("buy_link_patterns"),
        own_hosts=site_hosts_from_root(cfg.get("site_root") or ""),
    )


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
    `link_cfg` and the proposal's bucket + title (`min_buy_links_for`). When
    it is above 0 the directive adds a DIRECT BUY LINKS block in the shape of
    `buy_link_template` (falling back to `kitchen_buy_template`).
    """
    cfg = link_cfg or {}
    recipes: list[str] = proposal.get("expected_recipe_slugs") or []
    kits: list[str] = proposal.get("expected_kitchen_slugs") or []
    products: list[str] = (proposal.get("expected_product_asins")
                             or proposal.get("expected_asins") or [])
    min_buy = (_as_int(min_buy_links) if min_buy_links is not None else min_buy_links_for(
        cfg, bucket=[str(proposal.get("bucket") or ""), str(proposal.get("category") or "")],
        title=str(proposal.get("title") or "")))
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
    kit_cats: list[str] = proposal.get("expected_kitchen_category_slugs") or []
    kit_prods: list[str] = proposal.get("expected_kitchen_product_slugs") or []
    split = bool(kit_cats or kit_prods)

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
        buy_slugs = [s for s in (kit_prods or kits) if isinstance(s, str) and s][:10]
        lines.append(f"  DIRECT BUY LINKS — at least {min_buy} distinct (HARD REQUIREMENT "
                     f"for this article's bucket/title). Give EVERY product you name as "
                     f"a pick, gift or checklist item its own buy link, next to its name "
                     f"or in its row of the picks table:")
        if buy_tpl:
            for s in (buy_slugs or ["<product-slug>"]):
                lines.append(f"    [Check price]({buy_tpl.format(site_root=site_root, slug=s)})")
        else:
            pats = ", ".join(cfg.get("buy_link_patterns") or DEFAULT_BUY_LINK_PATTERNS)
            lines.append(f"    (a buy link is a link whose target matches: {pats})")
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
    buy_link_patterns, buy_link_template).

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
