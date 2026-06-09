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
# host. Captures the slug (with optional trailing -<digits>).
_LINK_RE = re.compile(
    r"\[[^\]]+\]"
    r"\((?:https?://[^/)]*)?"
    r"(?P<root>/(?:recipes|k|product|reviews|blog)/)"
    r"(?P<slug>[a-z0-9][a-z0-9-]*)"
    r"\)"
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

    @property
    def total_internal_links(self) -> int:
        return (self.recipe_links + self.kitchen_links
                + self.product_links + self.blog_links)

    @property
    def passes(self) -> bool:
        return (self.recipe_links >= self.min_recipes
                and self.kitchen_links >= self.min_kits)

    def failure_reason(self) -> str:
        if self.recipe_links < self.min_recipes:
            return (f"only {self.recipe_links} /recipes/ links "
                    f"(min {self.min_recipes})")
        if self.kitchen_links < self.min_kits:
            return (f"only {self.kitchen_links} /k/ links "
                    f"(min {self.min_kits})")
        return ""


def _count_by_root(body_md: str) -> dict[str, set[str]]:
    """Returns root → set of slugs linked."""
    found: dict[str, set[str]] = {
        "/recipes/": set(), "/k/": set(),
        "/product/": set(), "/reviews/": set(), "/blog/": set(),
    }
    for m in _LINK_RE.finditer(body_md or ""):
        root = m.group("root"); slug = m.group("slug")
        found.setdefault(root, set()).add(slug)
    return found


def verify_body(body_md: str,
                proposal: dict,
                *,
                min_recipes: int = 5,
                min_kits: int = 2) -> LinkAuditResult:
    """Audit `body_md` against the proposal's expected slug lists."""
    found = _count_by_root(body_md or "")
    recipes = found.get("/recipes/", set())
    kitchens = found.get("/k/", set())
    products = found.get("/product/", set())
    blogs = found.get("/blog/", set())

    expected_recipe = set(_strip_id(s) for s in
                           (proposal.get("expected_recipe_slugs") or []))
    expected_kit = set(_strip_id(s) for s in
                        (proposal.get("expected_kitchen_slugs") or []))

    matched_r = len(expected_recipe & set(_strip_id(s) for s in recipes))
    matched_k = len(expected_kit & set(_strip_id(s) for s in kitchens))

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
    )


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
                          site_root: str = "https://aisleprompt.com") -> str:
    """Returns a directive block the implementer can paste into its
    aider/claude prompt. The directive lists every expected slug and
    explains the inline-link contract.

    Sites that don't have one of the two link types (e.g. specpicks has
    no /recipes/) pass min_recipes=0 and the directive adapts.
    """
    recipes: list[str] = proposal.get("expected_recipe_slugs") or []
    kits: list[str] = proposal.get("expected_kitchen_slugs") or []
    lines: list[str] = [
        "INLINE-LINK CONTRACT (HARD REQUIREMENT — verified after exit):",
        f"  The wrapper counts /recipes/ + /k/ markdown links in your "
        f"output. It rejects the article (EDIT INCOMPLETE) if the body "
        f"contains fewer than {min_recipes} distinct /recipes/ links "
        f"and {min_kits} distinct /k/ links — the article will NOT be "
        f"inserted, the proposal will be re-queued, and you'll be asked "
        f"to do this work again. Better to put the links in once.",
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
    if kits:
        lines.append(f"  KITCHEN CATEGORIES TO LINK ({len(kits)} provided — "
                     f"use these slugs verbatim; pick at least {min_kits}):")
        for s in kits[:10]:
            lines.append(f"    [Category Name]({site_root}/k/{s})")
        lines.append("")
    lines.append(
        "  Use absolute paths (`/recipes/...`, `/k/...`) — relative paths "
        "resolve under /blog and break.")
    lines.append("")
    return "\n".join(lines)


def render_failure_addendum(audit: LinkAuditResult) -> str:
    """Used when the wrapper re-prompts the LLM after a failed verify.
    Explains what was missing so the model fixes the exact gap."""
    return (
        "INLINE-LINK CONTRACT — VIOLATED. You wrote "
        f"{audit.recipe_links}/{audit.min_recipes} required /recipes/ "
        f"links and {audit.kitchen_links}/{audit.min_kits} required "
        f"/k/ links. The article is NOT shippable in this state. "
        "Add additional inline links to the body using the expected "
        "slugs already provided; do not delete any existing prose. "
        "After adding the links, save the file again and exit cleanly."
    )
