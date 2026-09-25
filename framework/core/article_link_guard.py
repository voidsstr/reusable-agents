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

    @property
    def total_internal_links(self) -> int:
        return (self.recipe_links + self.kitchen_links
                + self.product_links + self.blog_links)

    @property
    def passes(self) -> bool:
        return (self.recipe_links >= self.min_recipes
                and self.kitchen_links >= self.min_kits
                and self.product_links >= self.min_products)

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


def verify_body(body_md: str,
                proposal: dict,
                *,
                min_recipes: int = 5,
                min_kits: int = 2,
                min_products: int = 0,
                kitchen_roots: Iterable[str] | None = None) -> LinkAuditResult:
    """Audit `body_md` against the proposal's expected slug lists.

    `kitchen_roots` — path roots whose links count as kitchen links
    (default `DEFAULT_KITCHEN_ROOTS`). A site whose category/product pages
    are crawlable (e.g. `/kitchen/category/`, `/kitchen/`) lists them so an
    article linking the crawlable page satisfies the contract.
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
                          link_cfg: dict | None = None) -> str:
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
    """
    cfg = link_cfg or {}
    recipes: list[str] = proposal.get("expected_recipe_slugs") or []
    kits: list[str] = proposal.get("expected_kitchen_slugs") or []
    products: list[str] = (proposal.get("expected_product_asins")
                             or proposal.get("expected_asins") or [])
    lines: list[str] = [
        "INLINE-LINK CONTRACT (HARD REQUIREMENT — verified after exit):",
        f"  The wrapper counts /recipes/ + kitchen + /product/ markdown "
        f"links in your output. It rejects the article (EDIT INCOMPLETE) "
        f"if the body contains fewer than {min_recipes} distinct "
        f"/recipes/ links, {min_kits} distinct kitchen links, and "
        f"{min_products} distinct /product/ links — the article will "
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
    return (
        "INLINE-LINK CONTRACT — VIOLATED. You wrote "
        f"{audit.recipe_links}/{audit.min_recipes} required /recipes/ "
        f"links, {audit.kitchen_links}/{audit.min_kits} required "
        f"kitchen links, and {audit.product_links}/{audit.min_products} "
        f"required /product/ links. The article is NOT shippable in "
        f"this state. Add additional inline links to the body using "
        f"the expected slugs / ASINs already provided; do not delete "
        f"any existing prose. After adding the links, save the file "
        f"again and exit cleanly."
    )


# ── Per-site minima resolution ─────────────────────────────────────────────
# The minima are per-deployment VALUES, so they live in config, not code
# (framework-first). Resolution: storage config → repo default file → zeros
# (with a stderr warning, because silently-disabled enforcement is how the
# 881 zero-product-link specpicks articles happened).
import json as _json
import sys as _sys
from pathlib import Path as _Path

_REPO_CONFIG = _Path(__file__).resolve().parents[2] / "config" / "article-link-guard-config.json"
_CONFIG_KEY = "config/article-link-guard-config.json"


def resolve_minima(site_hint: str, storage=None) -> dict:
    """Return the site's guard config: {min_recipes, min_kits, min_products,
    site_root} plus the optional kitchen knobs (kitchen_roots,
    kitchen_*_template) that `verify_body` / `render_link_directive` read.

    `site_hint` is whatever the caller has — a per-site agent_id
    ("specpicks-article-proposal-agent") or a bare site name; matching is
    by substring against the config's `sites` keys.
    """
    cfg = None
    if storage is not None:
        try:
            cfg = storage.read_json(_CONFIG_KEY) or None
        except Exception:
            cfg = None
    if cfg is None:
        try:
            cfg = _json.loads(_REPO_CONFIG.read_text())
        except Exception as e:
            _sys.stderr.write(f"[link-guard] config unreadable ({e}); minima default to 0 — FIX THIS\n")
            return {"min_recipes": 0, "min_kits": 0, "min_products": 0,
                    "site_root": ""}
    vals = dict(cfg.get("default") or {})
    hint = (site_hint or "").lower()
    for key, over in (cfg.get("sites") or {}).items():
        if key.lower() in hint:
            vals.update(over or {})
            break
    return vals
