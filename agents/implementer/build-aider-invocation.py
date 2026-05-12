#!/usr/bin/env python3
"""Build a focused per-rec aider invocation: concrete prompt + preload files.

Replaces the old approach of pointing aider at the full AGENT-AIDER.md
runbook. That gave aider too much latitude — abstract tasks ("read
recommendations.json and apply selected recs") produced 74-token
acknowledgements with no edits. This script does the planning HERE on
the Python side so aider gets concrete, file-scoped work like:

    Edit Customer Applications/aisleprompt/v1.0/frontend/src/pages/MarketingPage.tsx
    to remove the "(2026)" from the H1 to match current year.

Inputs (env or CLI):
  --recs <path>         path to run-dir/recommendations.json
  --rec-ids <csv>       comma-separated rec ids to apply
  --repo-path <dir>     implementer repo root (where aider runs)
  --site <name>         site id, used to map URLs → repo paths
  --site-config <path>  YAML site config (for repo_path / url_map hints)
  --out-prompt <path>   write the focused prompt here
  --out-files <path>    write one repo-relative file path per line here
                        (these become aider --file args)

Behaviour:
  * Reads + parses recommendations.json (handles both schema variants).
  * Filters to the requested rec ids.
  * For each rec, builds an "Apply this change" block with:
      - Title + rationale (1-2 lines, no fluff).
      - Concrete approach (from implementation_outline.approach OR .notes).
      - Acceptance signals (evidence snippets the rec wants gone / added).
      - Best-guess target file paths derived from evidence[].url + .data_refs.
  * Aggregates files across recs into a unique set; writes one path per
    line to --out-files. The bash wrapper passes each via --file so
    aider has them open in chat context BEFORE it sees the prompt.
  * Writes the focused prompt (no full runbook) to --out-prompt. The
    prompt explicitly tells aider: "edit ONLY these files, make ONLY
    these changes, do NOT explore the rest of the repo."

Exit codes:
  0  → wrote prompt + files (even if files list is empty — aider will
       use /run grep to find them)
  2  → invalid input (missing file, no matching recs)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional

# Make framework/ imports work whether this script is run via run.sh
# (which sets PYTHONPATH=REPO_ROOT) or by hand. The implementer dir is
# `<repo>/agents/implementer/`, so REPO_ROOT is two levels up.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from framework.core.implementer_scope import ScopePolicy  # noqa: E402


def _load_recs(path: Path) -> list[dict]:
    """Load recommendations.json, tolerating both top-level shapes."""
    raw = json.loads(path.read_text())
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        recs = raw.get("recommendations")
        if isinstance(recs, list):
            return recs
        # nested {recommendations: {recommendations: [...]}}
        if isinstance(recs, dict):
            inner = recs.get("recommendations")
            if isinstance(inner, list):
                return inner
    return []


def _filter(recs: list[dict], wanted_ids: list[str]) -> list[dict]:
    by_id = {r.get("id"): r for r in recs if isinstance(r, dict)}
    out = []
    for rid in wanted_ids:
        rid = rid.strip().lstrip(",")
        if not rid:
            continue
        r = by_id.get(rid)
        if r:
            out.append(r)
    return out


def _outline_text(rec: dict) -> str:
    """Pull the most concrete textual instruction out of the rec."""
    io = rec.get("implementation_outline") or {}
    if isinstance(io, str):
        return io
    if not isinstance(io, dict):
        return ""
    parts = []
    for key in ("approach", "notes", "steps", "title_template"):
        v = io.get(key)
        if isinstance(v, str) and v.strip():
            parts.append(f"{key}: {v.strip()}")
        elif isinstance(v, list) and v:
            parts.append(f"{key}:\n" + "\n".join(f"  - {x}" for x in v))
    if io.get("json_ld"):
        parts.append("MUST emit JSON-LD as specified in the rec's "
                     "implementation_outline.json_ld block.")
    if io.get("expected_word_count_min"):
        parts.append(
            f"Expected min word count: {io['expected_word_count_min']}.")
    if io.get("expected_internal_links_min"):
        parts.append(
            f"Expected min internal links: {io['expected_internal_links_min']}.")
    return "\n".join(parts)


_URL_IN_TEXT = re.compile(r"https?://\S+")


def _urls_from_rec(rec: dict) -> list[tuple[str, str]]:
    """Collect (url, snippet) pairs from both the evidence array AND
    URLs embedded in the title/rationale. SEO recs sometimes leave
    evidence=null and inline the URL in the title, e.g.

        rec.title = 'Rewrite snippet for "tacos on https://...-2397" ...'

    Without grabbing those, the slug_id hint never makes it into the
    prompt and the LLM has to guess the recipe id."""
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()

    ev = rec.get("evidence")
    if isinstance(ev, list):
        for e in ev:
            if isinstance(e, dict) and e.get("url"):
                u = e["url"].rstrip(".,;:)")
                if u not in seen:
                    seen.add(u)
                    pairs.append((u, e.get("snippet", "")))

    for field in ("title", "rationale"):
        text = rec.get(field) or ""
        for m in _URL_IN_TEXT.finditer(text):
            u = m.group(0).rstrip('".,;:)')
            if u not in seen:
                seen.add(u)
                pairs.append((u, ""))
    return pairs


def _evidence_text(rec: dict) -> str:
    pairs = _urls_from_rec(rec)
    if not pairs:
        return ""
    out = []
    for url, snippet in pairs[:5]:
        if url:
            line = f"  - {url}"
            # Many sites encode the entity primary key as the trailing
            # numeric segment of the slug (e.g. /recipes/foo-bar-43784 →
            # recipe id 43784, /products/B0G8XVV3GN, etc.). Surface the
            # extracted hint inline so the LLM can use it directly in
            # SQL UPDATE statements without guessing.
            slug_id_hint = _slug_id_hint(url)
            if slug_id_hint:
                line += f"  [slug_id={slug_id_hint}]"
            if snippet:
                line += f"  (offending text: {snippet!r})"
            out.append(line)
    return "\n".join(out)


_SLUG_TAIL_NUMERIC = re.compile(r"-(\d{2,12})/?$")
_SLUG_TAIL_ASIN = re.compile(r"/(B0[A-Z0-9]{8,})/?$")


def _slug_id_hint(url: str) -> str:
    """Best-effort extraction of an entity primary key from a URL.

    Examples:
      /recipes/traditional-mexican-tacos-al-pastor-43784  → '43784'
      /product/B0G8XVV3GN                                  → 'B0G8XVV3GN'
      /reviews/why-the-radeon-hd-3850-still-works          → '' (no id)
    """
    if not isinstance(url, str):
        return ""
    m = _SLUG_TAIL_NUMERIC.search(url)
    if m:
        return m.group(1)
    m = _SLUG_TAIL_ASIN.search(url)
    if m:
        return m.group(1)
    return ""


def _explicit_files(rec: dict) -> list[str]:
    """Collect paths the rec explicitly names. Schemas vary."""
    out: list[str] = []
    for k in ("target_files", "files", "target_paths"):
        v = rec.get(k)
        if isinstance(v, list):
            out.extend(str(x) for x in v if isinstance(x, str))
    io = rec.get("implementation_outline") or {}
    if isinstance(io, dict):
        for k in ("target_files", "files", "files_to_edit"):
            v = io.get(k)
            if isinstance(v, list):
                out.extend(str(x) for x in v if isinstance(x, str))
    return out


def _slug_from_url(url: str) -> str:
    """https://aisleprompt.com/vs/mealime → vs/mealime"""
    m = re.match(r"^https?://[^/]+(/.*)$", url.strip())
    if not m:
        return ""
    return m.group(1).strip("/")


def _derive_files_from_url(url: str, repo_path: Path,
                           site: str) -> list[str]:
    """Best-effort URL → repo-file mapping. Returns repo-relative paths
    that exist in the working tree.

    Heuristics (cheap, high-signal):
      - last URL slug → grep for it in *.tsx/*.ts/*.py to find a
        page/component matching the slug.
      - vs/<x> URLs → look for VsPage / Vs<x>.tsx components
      - product/<asin> → product detail templates
    """
    slug = _slug_from_url(url)
    if not slug:
        return []
    last = slug.rsplit("/", 1)[-1]
    if not last or len(last) < 3:
        return []

    # Find candidate files. Limit scope to typical web roots to avoid
    # walking node_modules etc.
    candidates: list[Path] = []
    web_roots = [
        repo_path / "frontend" / "src",
        repo_path / "src",
        repo_path / "Customer Applications",
        repo_path / "agents",
        repo_path / "pages",
    ]
    web_roots = [p for p in web_roots if p.is_dir()]

    # Pattern: filename contains the last URL segment (case-insensitive),
    # OR file content references the slug as a route/path.
    fname_re = re.compile(re.escape(last), re.IGNORECASE)
    slug_re = re.compile(r"['\"`]/" + re.escape(slug) + r"['\"`]")
    seen: set[Path] = set()

    for root in web_roots:
        for p in root.rglob("*.tsx"):
            if any(part.startswith((".", "node_modules", "dist", "build"))
                   for part in p.parts):
                continue
            if p in seen:
                continue
            try:
                if fname_re.search(p.name):
                    candidates.append(p)
                    seen.add(p)
                    continue
                if p.stat().st_size > 200_000:
                    continue
                txt = p.read_text(errors="ignore")[:200_000]
                if slug_re.search(txt):
                    candidates.append(p)
                    seen.add(p)
            except OSError:
                pass
        # Also probe .ts / .py / .md briefly
        for ext in ("*.ts", "*.py"):
            for p in root.rglob(ext):
                if any(part.startswith((".", "node_modules", "dist", "build"))
                       for part in p.parts):
                    continue
                if p in seen:
                    continue
                if fname_re.search(p.name):
                    candidates.append(p)
                    seen.add(p)
        if len(candidates) > 6:
            break

    # Cap so a single rec doesn't preload 50 files into aider's context.
    return [str(p.relative_to(repo_path)) for p in candidates[:6]]


def _detect_framework_reference_pages(repo_path: Path) -> list[str]:
    """Find one or two existing static pages that the LLM can use as
    a CONVENTION REFERENCE when generating new pages. Critical to
    prevent the Next.js-on-Vite hallucination class of bug — without
    a reference file the LLM picks whichever convention its training
    data biased toward.

    Heuristic: prefer 'AboutPage', 'FAQPage', 'BlogPage', 'MarketingPage'
    in that order — those are usually small, simple, and demonstrate
    the project's component shape + routing import + meta-tag pattern."""
    candidates_pref = (
        "AboutPage.tsx", "FAQPage.tsx", "BlogPage.tsx",
        "MarketingPage.tsx", "AboutPage.jsx", "FAQPage.jsx",
    )
    search_roots = [
        repo_path / "frontend" / "src" / "pages",
        repo_path / "src" / "pages",
        repo_path / "pages",
        repo_path / "app",
    ]
    found: list[str] = []
    for root in search_roots:
        if not root.is_dir():
            continue
        for name in candidates_pref:
            p = root / name
            if p.is_file():
                try:
                    found.append(str(p.relative_to(repo_path)))
                except ValueError:
                    pass
                if len(found) >= 2:
                    return found
    return found


def _resolve_files(rec: dict, repo_path: Path, site: str) -> list[str]:
    """Pull explicit files first, then derive from evidence URLs.

    Special case: top5-target-page recs auto-preload a reference page
    so the LLM mirrors the project's actual conventions (Vite vs Next.js,
    React Router vs file-based routing, etc.)."""
    rec_type = (rec.get("type") or "").lower()
    if rec_type == "top5-target-page":
        # The convention reference is the most important file to load.
        # Without it the LLM hallucinates the wrong framework's idiom.
        ref = _detect_framework_reference_pages(repo_path)
        if ref:
            return ref

    out: list[str] = []
    for f in _explicit_files(rec):
        # Tolerate absolute paths and paths starting with repo_path.
        p = Path(f)
        if p.is_absolute():
            try:
                rel = p.relative_to(repo_path)
                out.append(str(rel))
            except ValueError:
                # Outside repo — skip.
                pass
        else:
            if (repo_path / p).exists():
                out.append(f)
    if out:
        return out

    # Derive from evidence URLs.
    urls: list[str] = []
    for e in (rec.get("evidence") or []):
        if isinstance(e, dict) and e.get("url"):
            urls.append(e["url"])
    for u in urls[:3]:  # only first 3 evidence URLs
        out.extend(_derive_files_from_url(u, repo_path, site))
        if len(out) > 8:
            break
    # Dedup, preserve order.
    seen: set[str] = set()
    deduped = []
    for f in out:
        if f not in seen:
            seen.add(f)
            deduped.append(f)
    return deduped[:8]


# Rec types the framework code-editor chain handles RELIABLY today.
# Anything outside this allowlist is deferred.
#
# Promotion policy as of 2026-05-08: every rec_type emitted by an
# auto-queueing agent is allowlisted. The implementer's per-type code
# paths handle the well-known shapes (DB UPDATE, template edit,
# article INSERT, h2h commentary); types without a dedicated path
# fall through to the generic "edit the relevant files" branch with
# a pre-prompt warning to the LLM. We accept some LLM hallucination
# risk on rarely-seen types in exchange for zero false-deferrals on
# the recommendations that drive site-side metrics (the north star).
#
# To revert to the conservative allowlist, set
# IMPLEMENTER_STRICT_ALLOWLIST=1 in the env — `_strict_allowlist()`
# below switches to the original 8-type set.
#
# Promotion log:
# - 2026-05-04: ctr-fix, internal-link, top5-target-page (opt-in),
#   article-author proposals (write-then-insert)
# - 2026-05-04: onpage-title-length, onpage-low-internal-linking,
#   article-orphan-boost
# - 2026-05-08: full fleet sweep — every emitted rec_type allowlisted.
#   Drove this from the storage corpus + the seo-opportunity-agent's
#   LLM-audit catalog at lib/analyzer/llm_audit.py (~120 types).
TRUSTED_REC_TYPES: set[str] = {
    # ── Core (validated, dedicated implementer paths) ───────────────
    "ctr-fix",                          # SQL UPDATE migrations on existing rows
    "outdated-content",                 # SQL UPDATE on a known row (slug_id)
    "snippet-rewrite",                  # SQL UPDATE on a known row (slug_id)
    "internal-link",                    # add anchor to existing template file
    "onpage-title-length",              # static template title tweak
    "onpage-low-internal-linking",      # add anchor to existing template
    "article-orphan-boost",             # sitemap + cross-link
    "catalog-audit-criterion",          # DB-only fix: SQL migration OR DEFERRED summary
    "article-author-proposal",          # write-markdown-then-insert (special path)
    "top5-target-page",                 # opt-in template change

    # ── SEO opportunity agent — analyzer.py (deterministic) ─────────
    "onpage-meta-description",
    "onpage-canonical-missing",
    "onpage-canonical-issue",
    "onpage-h1-issue",
    "onpage-og-incomplete",
    "onpage-twitter-card-missing",
    "onpage-thin-content",
    "broken-internal-link",
    "broken-page",
    "new-page-buying-guide",
    "new-page-brand",
    "new-page-comparison",
    "new-page-use-case",
    "new-page-troubleshooting",
    "gsc-coverage-discovered",
    "gsc-coverage-unknown",
    "ssr-fix",
    "indexing-fix",
    "schema-markup",
    "performance",
    "accessibility",
    "layout-issue",
    "missing-content",
    "content-expansion",
    "content-error",
    "duplicate-content",
    "incorrect-categorization",
    "image-name-mismatch",

    # ── SEO opportunity agent — llm_audit.py catalog (~120 types) ───
    # Article / E-E-A-T
    "article-author-credentials-missing",
    "article-cited-sources-missing",
    "article-datemodified-missing",
    "article-publish-update-dates",
    "article-wordcount-schema-missing",
    "body-internal-links-thin",
    "body-visible-date-missing",
    "content-freshness-low",
    "content-keyword-stuffing",
    "content-thin",
    "eeat-about-missing",
    "eeat-author-bio",
    "eeat-author-missing",
    "eeat-citations-missing",
    "eeat-outbound-citation-count",
    "eeat-policy-missing",
    "eeat-publish-date-missing",
    "eeat-update-date-missing",

    # Conversion / CTA
    "cta-missing",
    "cta-position",
    "cta-weak",
    "conversion-path",

    # Core Web Vitals / mobile
    "cwv-font-no-display",
    "cwv-image-format",
    "cwv-image-no-dimensions",
    "cwv-image-no-lazy",
    "cwv-large-dom",
    "cwv-render-blocking",
    "cwv-ttfb-slow",
    "cwv-ttfb-very-slow",
    "mobile-font-small",
    "mobile-horizontal-scroll",
    "mobile-tap-targets",
    "mobile-viewport-missing",

    # FAQ / GEO / LLM-search readiness
    "faq-quality-thin",
    "feature-conversational-content",
    "featured-product-pdp-improve",
    "feature-internal-link-cluster",
    "footer-trust-links-missing",
    "geo-author-credentials",
    "geo-direct-answer-missing",
    "geo-faq-missing",
    "geo-listicle-no-summary",
    "geo-llms-txt-missing",
    "geo-statistics-missing",
    "llm-search-citation-readiness",
    "llm-search-direct-answer-missing",
    "llm-search-faq-format",
    "llm-search-quote-extractability",

    # Headings
    "h1-mismatch",
    "h1-missing",
    "h1-multiple",
    "heading-hierarchy",

    # Images
    "image-alt-empty-content",
    "image-alt-keyword-stuffing",
    "image-alt-missing",
    "image-filename-non-descriptive",

    # Indexing
    "indexing-breadcrumb-parity",
    "indexing-canonical-non-2xx",
    "indexing-canonical-self",
    "indexing-hreflang-asymmetric",
    "indexing-hreflang-missing",
    "indexing-noindex-conflict",
    "indexing-pagination-rel",
    "indexing-robots-blocked",
    "indexing-sitemap-404",
    "indexing-sitemap-shrank",
    "indexing-soft-404",

    # Internal linking
    "internal-link-graph-regression",
    "internal-link-to-featured",
    "link-anchor-generic",
    "link-anchor-keyword",
    "link-broken",
    "link-nofollow-internal",
    "link-orphan",
    "link-redirect-chain",

    # Meta
    "meta-description-cta",
    "meta-description-duplicate",
    "meta-description-keyword",
    "meta-description-length",
    "meta-description-missing",
    "meta-title-brand",
    "meta-title-duplicate",
    "meta-title-keyword",
    "meta-title-length",

    # Product
    "product-affiliate-cta-position",
    "product-affiliate-tag-missing",
    "product-aggregate-rating-thin",
    "product-comparison-link-missing",
    "product-image-count-thin",
    "product-pros-cons-missing",
    "product-schema-incomplete",
    "product-specs-table-missing",
    "product-image-present",
    "product-image-valid-url",
    "product-brand-present",
    "product-schema-rich-results",

    # Recipe
    "recipe-conversational-intro-missing",
    "recipe-internal-links-thin",
    "recipe-schema-incomplete",
    "recipe-image-present",
    "recipe-image-valid-url",
    "recipe-video-present",
    "recipe-nutrition-sanity",
    "recipe-cuisine-assigned",
    "recipe-duplicate-source-url",
    "recipe-category-in-allowlist",
    "recipe-category-assigned",

    # Review
    "review-buyer-persona-segmentation-missing",
    "review-citations-section-missing",
    "review-specs-source-attribution-missing",
    "review-template-incomplete",

    # Schema (JSON-LD)
    "schema-article-missing",
    "schema-breadcrumblist-missing",
    "schema-deprecated",
    "schema-faqpage-missing",
    "schema-howto-missing",
    "schema-incomplete",
    "schema-invalid",
    "schema-organization-missing",
    "schema-product-missing",
    "schema-website-searchaction-missing",

    # Trust / topical
    "topical-cluster-orphan",
    "trust-signal-density-thin",
    "trust-signals-missing",

    # URL hygiene
    "url-deep",
    "url-non-descriptive",
    "url-trailing-slash",
    "url-uppercase",

    # ── Head-to-head agent (specpicks) ──────────────────────────────
    "h2h-comparison-format-readiness",
    "h2h-quote-worthy-verdict-missing",
    "h2h-stale-pricing",
    "h2h-stale-pricing-product",

    # ── Competitor research agent (parity / advantage / UX gaps) ────
    "parity-feature",
    "competitive-advantage",
    "ux-improvement",
    "content-gap",
    "monetization",
    "marketing-positioning",
    "integrations",

    # ── Consolidation primitive (install/consolidate-queued.py) ─────
    "page-cleanup",                     # mega-rec: N issues on one URL → one LLM call

    # ── Generic / catch-all ─────────────────────────────────────────
    "other",
}


def _strict_allowlist() -> set[str]:
    """Conservative pre-2026-05-08 allowlist. Only these types ran in
    production; the implementer reverts to this set when
    IMPLEMENTER_STRICT_ALLOWLIST=1 is set."""
    return {
        "ctr-fix", "outdated-content", "snippet-rewrite",
        "internal-link", "onpage-title-length",
        "onpage-low-internal-linking", "article-orphan-boost",
        "catalog-audit-criterion",
    }


# Apply the strict-mode env override at import time so a flip doesn't
# require a code change.
import os as _os_strict
if _os_strict.environ.get("IMPLEMENTER_STRICT_ALLOWLIST") == "1":
    TRUSTED_REC_TYPES = _strict_allowlist()


# Patterns lifted from the leaked Claude Code execution/index.ts
# DANGEROUS_COMMANDS regex set. We don't run shell ourselves — but the
# rec body / outline / evidence is user-untrusted text that gets
# embedded in the aider prompt. If a rec contains one of these strings,
# the LLM might mirror it into a /run block. Scrub before send.
_DANGEROUS_COMMAND_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"\brm\s+-rf?\s+/(?:\s|$)"),         # rm -rf /
    re.compile(r"\brm\s+-rf?\s+/\*"),               # rm -rf /*
    re.compile(r"\brm\s+-rf?\s+~/?(?:\s|$)"),       # rm -rf ~
    re.compile(r"\bdd\s+if=.*of=/dev/(?:sd|nvme|hd|xvd)"),  # dd to disk
    re.compile(r"\bmkfs\.[a-z0-9]+\s+/dev/"),       # mkfs on raw device
    re.compile(r":\(\)\s*\{\s*:\|:&\s*\};:"),       # classic fork bomb
    re.compile(r"\bsudo\s+rm\s+-rf?\s+/"),          # sudo rm -rf /
    re.compile(r">\s*/dev/sd[a-z]"),                # redirect to raw disk
    re.compile(r"\bchmod\s+-R\s+0?777\s+/"),        # chmod -R 777 /
    re.compile(r"\bchown\s+-R\s+\S+\s+/(?:\s|$)"),  # chown -R x /
    re.compile(r"\bcurl\s+\S+\s*\|\s*(?:bash|sh)\b"),  # curl|sh pipe
    re.compile(r"\bwget\s+\S+\s*\|\s*(?:bash|sh)\b"),  # wget|sh pipe
)


def _scrub_dangerous_commands(text: str) -> tuple[str, list[str]]:
    """Replace dangerous shell command patterns with a redacted marker.

    Returns (scrubbed_text, list_of_matched_pattern_descriptions).
    Used on the assembled aider prompt right before it's written so
    untrusted rec text can't smuggle a destructive command into the
    LLM's context.
    """
    if not text:
        return text, []
    hits: list[str] = []
    out = text
    for pat in _DANGEROUS_COMMAND_PATTERNS:
        if pat.search(out):
            hits.append(pat.pattern)
            out = pat.sub("[REDACTED-DANGEROUS-COMMAND]", out)
    return out, hits


def _detect_repo_stack(repo_path: Path) -> str:
    """Detect web framework + SSR mode so the LLM uses correct file
    conventions. Returns a short multi-line hint string injected into
    the prompt.

    Catches the historical Next.js-on-Vite failure mode where the LLM
    wrote `pages/buying-guide/[slug].tsx` on a repo that uses
    react-router. Also flags SSR vs SPA so SEO recs land in the SSR
    render path instead of post-hydration client code.
    """
    hints: list[str] = []
    pkg_paths = [
        repo_path / "package.json",
        repo_path / "frontend" / "package.json",
    ]
    deps: dict[str, str] = {}
    pkg_seen: Path | None = None
    # Walk every candidate package.json and prefer the one that actually
    # declares a frontend framework (next/vite/remix). Top-level
    # package.json on monorepos often has only tooling deps, so don't
    # stop at the first one we find.
    framework_keys = {"next", "vite", "react-router", "react-router-dom"}
    for p in pkg_paths:
        if not p.exists():
            continue
        try:
            j = json.loads(p.read_text())
            local: dict[str, str] = {}
            for key in ("dependencies", "devDependencies"):
                d = j.get(key) or {}
                if isinstance(d, dict):
                    local.update({k: str(v) for k, v in d.items()})
            if not pkg_seen or any(k in local for k in framework_keys) or any(k.startswith("@remix-run/") for k in local):
                deps = local
                pkg_seen = p
                if any(k in local for k in framework_keys) or any(k.startswith("@remix-run/") for k in local):
                    break
        except Exception:
            continue
    if not deps:
        return ""

    has_next = "next" in deps
    has_vite = "vite" in deps
    has_remix = any(k.startswith("@remix-run/") for k in deps)
    has_react_router = any(k.startswith("react-router") for k in deps)
    has_helmet = "react-helmet-async" in deps or "react-helmet" in deps
    pkg_dir = pkg_seen.parent if pkg_seen else repo_path

    fw = "unknown"
    if has_next:
        fw = "Next.js"
    elif has_remix:
        fw = "Remix"
    elif has_vite and has_react_router:
        fw = "Vite + react-router (SPA-style)"
    elif has_vite:
        fw = "Vite"

    hints.append(f"Frontend framework: {fw} (detected from {pkg_seen.relative_to(repo_path) if pkg_seen else '?'})")

    if fw.startswith("Vite"):
        hints.append(
            "  ⚠ This is NOT a Next.js project. DO NOT create files under "
            "`pages/` or `app/`. Routes live in App.tsx / a router config; "
            "page components live under `src/pages/` (or `frontend/src/pages/`). "
            "Use the existing routing pattern — do not introduce Next.js "
            "file-based routing."
        )

    # SSR detection — look for an SSR entry file.
    ssr_paths = [
        repo_path / "src" / "services" / "ssrRender.ts",
        repo_path / "src" / "entry-server.ts",
        repo_path / "src" / "entry-server.tsx",
        repo_path / "frontend" / "src" / "entry-server.tsx",
        repo_path / "frontend" / "src" / "services" / "ssrRender.ts",
        repo_path / "server.ts",
        repo_path / "server.js",
        pkg_dir / "src" / "services" / "ssrRender.ts",
        pkg_dir / "src" / "entry-server.tsx",
    ]
    ssr_file: Path | None = None
    for sp in ssr_paths:
        if sp.exists():
            ssr_file = sp
            break

    if ssr_file:
        rel = ssr_file.relative_to(repo_path)
        # Look for an ssrHead-style sibling — same SSR layer where head
        # tags / JSON-LD are typically built. ssrRender handles the body
        # HTML; ssrHead handles <head> JSON-LD + meta tags. Both need the
        # same product/data filters to stay consistent.
        ssr_head = None
        for c in (ssr_file.parent / "ssrHead.ts", ssr_file.parent / "ssrHead.tsx"):
            if c.exists():
                ssr_head = c.relative_to(repo_path)
                break
        sibling = f" + `{ssr_head}`" if ssr_head else ""
        hints.append(
            f"SSR: this site server-renders pages via `{rel}`{sibling}. "
            "For any rec that affects what bots/crawlers see (JSON-LD, "
            "meta tags, canonical/og:* tags, structured data, robots, "
            "hreflang) OR what data-quality filters apply to product "
            "lists shown in indexed JSON-LD ItemList schemas (blocklists, "
            "rating gates, dedup rules), the change MUST land in the SSR "
            "file(s) above. Client-only React filters (a `useEffect` "
            "filter, a function in a page component) do NOT apply to the "
            "SSR'd HTML or the SSR'd JSON-LD. Search engines see only the "
            "server-rendered output — so a blocklist that lives in "
            "`SomePage.tsx` is invisible to Google. Whenever a rec edits "
            "a client-side filter / blocklist / quality gate, MIRROR it "
            f"into `{rel}`{(' and `' + str(ssr_head) + '`') if ssr_head else ''} "
            "in the same dispatch."
        )
    elif has_next:
        hints.append(
            "SSR: Next.js — use `getServerSideProps` / `getStaticProps` "
            "or the App Router's server components. Head tags via "
            "`<Head>` (pages dir) or `metadata` export (app dir)."
        )
    else:
        hints.append(
            "SSR: not detected — this is a client-rendered SPA. SEO recs "
            "may still need server-side meta-tag injection (check "
            "index.html template + any pre-render config like "
            "vite-plugin-ssr / vike before assuming pure client-side is OK)."
        )

    if has_helmet:
        hints.append("Head tags: `react-helmet-async` is available — use it for client-side <head> updates.")

    return "Repo stack hints (read before editing):\n" + "\n".join(hints)


def build_prompt(recs: list[dict], repo_path: Path, site: str,
                 dispatch_kind: str,
                 pre_dirty_list: list[str],
                 ) -> tuple[str, list[str], list[dict]]:
    """Returns (prompt_text, unique_files_list_repo_relative, deferred).

    `deferred` is a list of {rec_id, type, reason} for recs filtered out
    because the chain can't handle them safely. The wrapper logs these
    so the dashboard can surface "deferred until claude-pool reset" per
    rec, separate from "applied".
    """
    all_files: list[str] = []
    seen_files: set[str] = set()
    deferred: list[dict] = []

    # Filter recs by trusted-type allowlist BEFORE building the prompt.
    # An unrecognized type defers. Validation history is the only way
    # to add a type; never widen this filter speculatively.
    eligible_recs: list[dict] = []
    article_recs: list[dict] = []   # special handling — see article block
    for r in recs:
        # progressive-improvement-agent emits rec-type under `category`
        # (its long-standing field name); SEO + catalog-audit emit it under
        # `type`. Treat them interchangeably so the TRUSTED allowlist
        # gate doesn't silently defer 100% of PI's output. If both are
        # set, `type` wins. Persist back to `r["type"]` so downstream
        # logging + dispatch records show the resolved value.
        rt = (r.get("type") or r.get("category") or "").lower()
        if rt and not r.get("type"):
            r["type"] = rt
        # Article-author proposals carry a `proposal` block. We route
        # them through the write-body-then-insert pattern: the LLM
        # writes only Markdown to a file (something it's good at), and
        # the wrapper post-step does the verified DB INSERT itself.
        # This avoids the hallucinated-psql failure mode where the LLM
        # claimed it ran psql but never did.
        proposal = r.get("proposal") or r.get("article_proposal")
        if isinstance(proposal, dict) and proposal.get("slug"):
            article_recs.append(r)
            eligible_recs.append(r)
            continue
        if rt and rt in TRUSTED_REC_TYPES:
            eligible_recs.append(r)
            continue
        # Catalog-audit recs ship with empty `type`. Detect by the
        # canonical "Catalog audit found ... criterion" rationale and
        # promote them as if they were `catalog-audit-criterion` recs
        # so the dedicated prompt block kicks in.
        rationale_lc = (r.get("rationale") or "").lower()
        if rationale_lc.startswith("catalog audit found"):
            r["type"] = "catalog-audit-criterion"
            eligible_recs.append(r)
            continue
        # Unknown / not-yet-validated rec types defer cleanly.
        deferred.append({
            "rec_id": r.get("id"),
            "type": rt or "(no type)",
            "reason": (
                f"rec-type {rt!r} not in TRUSTED_REC_TYPES allowlist; "
                "either claude can't be relied on to follow project "
                "conventions for this work-shape (e.g. top5-target-page "
                "got Next.js convention on a Vite repo), or the rec "
                "needs research the LLM can't do"
            ),
        })

    blocks: list[str] = []
    for r in eligible_recs:
        rid = r.get("id", "?")
        title = (r.get("title") or "").strip()
        rationale = (r.get("rationale") or "").strip()
        outline = _outline_text(r)
        evidence = _evidence_text(r)
        files = _resolve_files(r, repo_path, site)

        for f in files:
            if f not in seen_files:
                seen_files.add(f)
                all_files.append(f)

        block = [f"### {rid} — {title}"]
        if rationale:
            block.append(f"Rationale: {rationale}")
        if outline:
            block.append(f"Approach:\n{outline}")
        if evidence:
            block.append(f"Evidence (these are the symptoms — fix them):\n"
                         f"{evidence}")
        # Heuristic: recs that target SERP snippets / titles / metas
        # for content URLs (e.g. /recipes/foo-1234, /reviews/bar-99) are
        # almost always backed by a database table — the page is
        # server-rendered from a row, not a static file. Tell the LLM
        # to write a SQL migration instead of hunting for a per-page
        # source file that doesn't exist.
        rec_type = (r.get("type") or "").lower()
        is_db_snippet_rec = (
            rec_type in {"ctr-fix", "outdated-content", "snippet-rewrite"}
            and any("/recipes/" in u or "/product/" in u or "/reviews/" in u
                    or "/articles/" in u
                    for u, _ in _urls_from_rec(r))
        )
        # Catalog-audit recs: type often empty (the agent emits them
        # without a `type` field), distinguished by rationale wording
        # "Catalog audit found N rows failing the '<criterion>' criterion".
        # The fix shape is ALWAYS a SQL migration (or a noop+defer-to-
        # other-job summary). Aider's default mode treats them like code
        # recs and goes hunting for non-existent source files; surface
        # the criterion + evidence rows + a pointer to the audit script
        # so the model has enough to write a real migration.
        is_catalog_audit_rec = (
            rec_type == "catalog-audit-criterion"
            or rationale.lower().startswith("catalog audit found")
        )
        catalog_criterion = ""
        if is_catalog_audit_rec:
            import re as _re
            m = _re.search(r"'([a-z][a-z0-9-]+)'", rationale)
            if m:
                catalog_criterion = m.group(1)
        # Article-author / catalog-audit dispatches: the rec carries a
        # `proposal` (article-author) or `audit_findings` (catalog-audit)
        # block that the LLM should turn into a real `INSERT` against
        # editorial_articles / kitchen_products etc. Surface that block
        # explicitly so the LLM doesn't have to guess at structure.
        proposal = r.get("proposal") or r.get("article_proposal")
        if isinstance(proposal, dict) and proposal.get("slug"):
            slug = proposal.get("slug")
            outline_list = proposal.get("outline") or []
            outline_text = "\n".join(f"  {i+1}. {h}"
                                     for i, h in enumerate(outline_list))
            secondary = proposal.get("secondary_keywords") or []
            block.append(
                "ARTICLE PROPOSAL — write the article body to a Markdown "
                "FILE. Do NOT run psql. The wrapper handles the DB "
                "INSERT after you exit (this avoids the hallucinated-"
                "psql failure mode where the LLM claimed success without "
                "actually inserting).\n\n"
                f"Proposal:\n```json\n{json.dumps(proposal, indent=2)[:2500]}\n```\n\n"
                "Required outputs — TWO files have been pre-created and "
                f"added to your chat context:\n"
                f"  1. `changes/{slug}.body.md` — REPLACE the entire "
                "file contents with the article body, 1500-3000 words. "
                f"Use this outline (H2 headers VERBATIM):\n{outline_text}\n"
                "     • H1 = the proposal `title` field exactly\n"
                "     • Body = ~200-400 words per section, plain prose\n"
                "     • Reference recipe slugs from "
                "`expected_recipe_slugs` as inline links\n"
                f"     • Hit the primary keyword "
                f"('{proposal.get('primary_keyword','')}') in H1 + "
                "first paragraph + at least one H2\n"
                f"     • Weave in secondary keywords {secondary[:3]} "
                "naturally\n"
                f"  2. `changes/{slug}.meta.json` — UPDATE the meta with "
                "subtitle (1 sentence) + excerpt (200-300 chars from "
                "the intro). Keep all other fields as pre-filled.\n\n"
                # Anti-hallucination contract — pattern lifted from the\n
                # leaked Claude Code system prompt: \"if you can't verify,\n
                # say so explicitly rather than claiming success.\"\n
                "VERIFICATION CONTRACT (mandatory before exit):\n"
                f"  a. After writing `changes/{slug}.body.md`, run "
                f"`/run wc -w changes/{slug}.body.md` and confirm "
                "word count is between 1500 and 3000. If under 1500, "
                "EXTEND the body — do not exit short.\n"
                "  b. If you cannot meet the word-count target after "
                "one extension attempt, write `EDIT INCOMPLETE: under "
                "target` as the LAST line of the body file. The "
                "wrapper will see it and skip the DB INSERT instead of "
                "shipping a thin article.\n"
                "  c. Do NOT write a `summary.md` claiming success. "
                "The wrapper writes the summary based on its own "
                "INSERT verification — it doesn't trust your claim.\n"
                "  d. Do NOT run psql. Do NOT modify code files outside "
                "`changes/`. The wrapper handles the DB INSERT.\n")
        elif is_catalog_audit_rec:
            # Surface the evidence rows + criterion verbatim. The audit
            # script (`scripts/catalog-quality-audit.ts`) is the source
            # of truth for what the criterion checks — preload it so
            # the model can read the SQL the criterion actually runs.
            # Compact prompt: when the rec carries a migration_template
            # (the catalog-audit agent attaches one for known
            # criteria), we ship the LLM the EXACT SQL to write — it
            # just needs to copy it into a new migration file with the
            # right timestamp prefix. This trims the prompt from
            # ~11k chars (including audit-script preload) down to
            # ~1k chars, lets local-LLM tier converge, AND is more
            # deterministic across all backends.
            tmpl = r.get("migration_template") or {}
            ref_ids = r.get("ref_ids") or [
                e.get("ref_id") for e in (r.get("evidence") or []) if e.get("ref_id")
            ]
            if tmpl.get("sql_with_ids"):
                block.append(
                    f"CATALOG-AUDIT REC — criterion `{catalog_criterion or '?'}`. "
                    f"DB-only fix.\n\n"
                    f"Action: {tmpl.get('action','')}\n"
                    f"Target ids: {', '.join(ref_ids[:30])}\n\n"
                    f"REQUIRED OUTPUT: write a NEW SQL migration file at "
                    f"`db/migrations/<UTC-ts>_{tmpl.get('filename','catalog-audit')}.sql` "
                    f"containing EXACTLY this SQL plus a 2-line "
                    f"`-- Criterion: {catalog_criterion}` / "
                    f"`-- Action: {tmpl.get('action','')}` header and a "
                    f"trailing `-- VERIFY: ...` comment showing how to "
                    f"re-run the audit on these ids:\n\n"
                    f"```sql\n{tmpl['sql_with_ids']}\n```\n\n"
                    f"Then write `changes/{rid}.summary.md` with `# {rid} "
                    f"— {catalog_criterion}` / `**Status:** SHIPPED — "
                    f"<filename>` (no DEFERRED). The migration runs on "
                    f"deploy; do NOT execute it via psql.")
            else:
                # Unknown criterion — fall back to the verbose path
                # (preloads audit script for the LLM to reverse-engineer).
                evidence_rows = r.get("evidence") or []
                ev_text = "\n".join(
                    f"  - id={e.get('ref_id','?')}  "
                    f"{(e.get('snippet') or '')[:90]}"
                    for e in evidence_rows[:30]
                )
                audit_script_path = "scripts/catalog-quality-audit.ts"
                audit_script_full = repo_path / audit_script_path
                if audit_script_full.is_file():
                    rel = str(audit_script_full.relative_to(repo_path))
                    if rel not in seen_files:
                        seen_files.add(rel)
                        all_files.append(rel)
                        files = files + [rel]
                block.append(
                    f"CATALOG-AUDIT REC — criterion `{catalog_criterion or '?'}` "
                    f"(no template registered — fallback path). DB-only fix.\n\n"
                    f"Concrete evidence rows:\n{ev_text or '  (no evidence rows)'}\n\n"
                    f"Reference: `{audit_script_path}` (preloaded). Read the "
                    f"criterion's `check` SQL, then write a SQL migration at "
                    f"`db/migrations/<UTC-ts>_catalog-audit-{catalog_criterion or 'fix'}.sql` "
                    f"that UPDATE/DELETEs the listed ref_ids per the criterion's "
                    f"intent (deactivate / null+backfill / canonical mapping). "
                    f"Use a defensive WHERE clause that's safe to re-run.\n\n"
                    f"Write `changes/{rid}.summary.md` with `# {rid} — "
                    f"{catalog_criterion}` / `**Status:** SHIPPED — <filename>`. "
                    f"Do NOT execute via psql; deployer runs migrations.")
        elif is_db_snippet_rec:
            block.append(
                "DB-backed page: the URL above is rendered from a "
                "database row, not a static file. Write a NEW SQL "
                "migration file under `db/migrations/` (or "
                "`supabase/migrations/`, whichever the repo already "
                "uses — `/run ls db/ supabase/` to check) that updates "
                "the appropriate row(s) by primary key (the `slug_id` "
                "above is the row id). Use `ALTER TABLE ... ADD "
                "COLUMN IF NOT EXISTS` defensively for any new column "
                "you reference. Do NOT modify React/SSR templates for "
                "this rec — the change must persist through the DB.")
        elif rec_type == "top5-target-page" and files:
            # Convention-driven new-page rec. The preloaded files ARE
            # the convention references (AboutPage.tsx etc.) — tell
            # the LLM to mirror their shape exactly when creating the
            # new page, instead of inventing.
            block.append(
                "NEW PAGE — create a NEW file alongside the existing "
                f"reference page(s) shown below. MIRROR THE CONVENTION "
                "of the reference file EXACTLY:\n" +
                "\n".join(f"  - {f}  (CONVENTION REFERENCE — read this "
                          "first; same imports, same component shape, "
                          "same export style)" for f in files) +
                "\n\nDo NOT introduce a different framework (no Next.js "
                "`app/` directory, no `metadata` exports, no "
                "`use client` directives) unless the reference file "
                "uses them. The reference file IS the source of truth "
                "for what 'a page' looks like in this repo. After you "
                "create the new page file, also `/run grep -rn "
                "'react-router\\|<Route ' --include=*.tsx -l | head` "
                "to find where routes are registered, and add a route "
                "entry there so the page is reachable.")
        elif files:
            block.append("Likely files to edit (already preloaded into "
                         "your chat):\n" +
                         "\n".join(f"  - {f}" for f in files))
        else:
            block.append("Files to edit: NOT auto-resolved — use `/run grep "
                         "-rn ...` against the repo to locate them, then "
                         "`/add <path>` before editing.")
        blocks.append("\n".join(block))

    pre_dirty_section = ""
    if pre_dirty_list:
        head = "\n".join(f"  - {f}" for f in pre_dirty_list[:60])
        more = (f"\n  ... ({len(pre_dirty_list) - 60} more)"
                if len(pre_dirty_list) > 60 else "")
        pre_dirty_section = (
            "\n\nWorking tree was already dirty before this run on these "
            "files — they may contain in-progress work from a parallel "
            "session. PREFER editing other files when possible. You MAY "
            "edit these if a rec genuinely requires it (the wrapper will "
            "commit content changes), but stay narrowly within the rec's "
            "scope and do NOT casually rewrite or reformat them:\n"
            + head + more)

    stack_hint = _detect_repo_stack(repo_path)
    stack_section = ("\n\n" + stack_hint) if stack_hint else ""

    prompt = f"""You are the implementer agent running in aider fallback mode.
Your job: apply the {len(recs)} recommendation(s) below to the repo at
`{repo_path}` (your CWD). Each rec is concrete, scoped, and self-contained.

Rules:
  1. Edit ONLY the files listed under each rec, OR files you discover
     are required after `/run grep`. Do NOT explore unrelated code.
  2. Do NOT run lengthy build commands, test suites, or migrations
     unless a rec explicitly requires it. The wrapper handles commits.
  3. After applying all recs, EXIT cleanly. Do not ask follow-up
     questions — `--yes-always` is set, and there's no human to answer.
  4. Use `/run` for shell, `/web` for URL fetches, `/add` to bring more
     files into context if needed. Standard aider tools.
  5. PREFER ACTION over deferral. The recs you receive are pre-filtered
     by an allowlist that only lets through types where the framework
     has validated the LLM can ship safe edits. So when you receive a
     rec, the CORRECT default is to make the edit. Defer ONLY when the
     rec text explicitly says no action is required (e.g. evidence
     proves the issue is handled elsewhere) — never because the work
     looks large or speculative. If the file list is unclear, use
     `/run grep` to locate the right files; do not skip. The deployer's
     test/build/smoke gates are what protect prod, not your reluctance.
  6. CONTEXT BUDGET: edit AT MOST 8 files TOTAL across all recs in this
     batch. If recs collectively touch more, prioritize the highest-
     severity ones and skip the rest — they will be re-dispatched on the
     next tick. Going over the budget breaks the LLM's context window
     and the whole batch fails.
  7. SCOPE EACH REC NARROWLY: most recs are template tweaks (single
     file). Only widen to multi-file edits when the rec EXPLICITLY says
     "across these N pages" or "every product page". When a rec lists
     "Likely files to edit", treat that list as a HARD CAP — do not
     auto-expand to sibling files just because they look similar.
  8. END-TO-END COMPLETION (NO DEAD CODE). The rec is "done" only when
     a user-visible, automatically-verifiable change ships to the page
     or API the rec targeted. CONCRETELY:
       a) If you create a NEW component / module / utility (e.g.
          `JsonLdScripts.tsx`, `useFooHook.ts`, `helpers/bar.ts`), you
          MUST also add at least one IMPORT + USE site in an existing
          page/route/component. A new file with zero callers is a
          PARTIAL COMPLETION — not acceptable.
       b) If you create a new schema/type/fixture, you MUST also wire
          it into the data-flow path that triggered the rec
          (e.g. an API route that returns it, a page that renders it).
       c) Before exiting, run `/run grep -rn '<NewSymbolName>' --include='*.ts*'`
          for every new top-level symbol you introduced. If grep
          returns ONLY the file that defines it, you have not
          completed the rec — go add the import + call site.
       d) For multi-page recs ("across all comparison pages", "every
          product page"), add the integration to AT LEAST 2 distinct
          pages so the LLM doesn't wave-its-hands by editing only one.
       e) If the integration would require touching files outside the
          rec's "Likely files to edit" list AND you don't see a clear
          import path, prefer EXTENDING an existing file over leaving
          the new code unused. If truly impossible, write a one-line
          DEFER summary explaining why — never ship orphan scaffolding.

     Reason: production smoke tests verify URLs respond — they do NOT
     check that a new component file is imported by anything. A rec
     that creates `lib/jsonld/productSchema.ts` but doesn't add it to
     `pages/buying-guide/[slug].tsx` will pass smoke (the file exists,
     the build compiles, the page returns 200) but produce ZERO user-
     visible change. That has happened in past dispatches and the
     follow-up cleanup is more expensive than doing it right the first
     time.
{pre_dirty_section}{stack_section}

Site: {site}
Dispatch kind: {dispatch_kind}
Total recs: {len(recs)}
Total preloaded files: {len(all_files)}

────────────────────────────────────────────────────────────────────────
{"\n\n".join(blocks)}
────────────────────────────────────────────────────────────────────────

Now apply these changes. Make minimal, targeted edits. Exit when done.
"""
    return prompt, all_files, deferred


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--recs", required=True)
    ap.add_argument("--rec-ids", required=True)
    ap.add_argument("--repo-path", required=True)
    ap.add_argument("--site", default="")
    ap.add_argument("--dispatch-kind", default="recommendation")
    ap.add_argument("--pre-dirty-file", default="")
    ap.add_argument("--out-prompt", required=True)
    ap.add_argument("--out-files", required=True)
    ap.add_argument("--out-deferred", default="",
                    help="Path to write JSON list of recs deferred by the "
                         "trusted-rec-types allowlist.")
    ap.add_argument("--allow-rec-types", default="",
                    help="Comma-separated additional rec types to add to "
                         "TRUSTED_REC_TYPES for this run only (used for "
                         "supervised one-off validation).")
    ap.add_argument("--site-config", default="",
                    help="Path to the site.yaml. If set, the implementer."
                         "{allowed,excluded}_paths block is enforced — recs "
                         "whose target_files fall outside the policy are "
                         "deferred with reason 'out-of-scope per site policy' "
                         "and never reach the LLM. Use to keep web-content "
                         "agents away from mobile/, ios-extensions/, etc.")
    args = ap.parse_args()

    recs_path = Path(args.recs)
    if not recs_path.is_file():
        print(f"build-aider-invocation: recs file not found: {recs_path}",
              file=sys.stderr)
        return 2
    repo_path = Path(args.repo_path).resolve()
    if not repo_path.is_dir():
        print(f"build-aider-invocation: repo path not a dir: {repo_path}",
              file=sys.stderr)
        return 2

    rec_ids = [s for s in (args.rec_ids or "").split(",") if s.strip()]
    if not rec_ids:
        print("build-aider-invocation: no rec ids supplied", file=sys.stderr)
        return 2

    all_recs = _load_recs(recs_path)
    selected = _filter(all_recs, rec_ids)
    if not selected:
        print(f"build-aider-invocation: 0 of {len(rec_ids)} requested rec "
              f"ids matched (have {len(all_recs)} recs total)",
              file=sys.stderr)
        return 2

    pre_dirty: list[str] = []
    if args.pre_dirty_file and Path(args.pre_dirty_file).is_file():
        try:
            pre_dirty = [
                ln.strip() for ln in
                Path(args.pre_dirty_file).read_text().splitlines()
                if ln.strip()
            ]
        except OSError:
            pass

    # Per-run trusted-types extension (supervised override).
    if args.allow_rec_types:
        extra = {t.strip().lower() for t in args.allow_rec_types.split(",")
                 if t.strip()}
        TRUSTED_REC_TYPES.update(extra)

    # ---- Site path-scope policy ----
    # Optional. When the site.yaml has an implementer.allowed_paths or
    # implementer.excluded_paths block, defer any rec whose target files
    # land outside it. This is what keeps SEO/PI agents away from
    # mobile/ on aisleprompt — see the framework's
    # framework/core/implementer_scope.py docstring for the full spec.
    policy = ScopePolicy()
    if args.site_config and Path(args.site_config).is_file():
        try:
            import yaml  # type: ignore
            cfg = yaml.safe_load(Path(args.site_config).read_text()) or {}
            # Pass dispatch_kind so the crash-watcher (dispatch_kind=
            # "crash-fix") gets a scope that allows mobile/ while
            # SEO / PI / catalog-audit etc. stay in their default lane.
            policy = ScopePolicy.from_site_config(cfg, dispatch_kind=args.dispatch_kind)
            if policy.allowed_paths or policy.excluded_paths:
                print(
                    f"build-aider-invocation: scope policy active "
                    f"(dispatch_kind={args.dispatch_kind!r}, "
                    f"allowed={list(policy.allowed_paths)}, "
                    f"excluded={list(policy.excluded_paths)})",
                    file=sys.stderr,
                )
        except Exception as e:  # noqa: BLE001
            print(f"build-aider-invocation: site-config load failed ({e}) — "
                  f"running without scope policy", file=sys.stderr)

    out_of_scope: list[dict] = []
    if policy.allowed_paths or policy.excluded_paths:
        in_scope_recs: list[dict] = []
        for r in selected:
            rec_files: list[str] = []
            for k in ("target_files", "files", "target_paths", "files_to_edit"):
                v = r.get(k)
                if isinstance(v, list):
                    rec_files.extend(str(x) for x in v if x)
            ok, denied = policy.is_rec_in_scope(rec_files)
            if not ok:
                out_of_scope.append({
                    "rec_id": r.get("id"),
                    "type": (r.get("type") or r.get("category") or "").lower(),
                    "reason": (
                        f"out-of-scope per site policy: target files "
                        f"{denied} not permitted by "
                        f"implementer.allowed_paths / "
                        f"implementer.excluded_paths in site.yaml"
                    ),
                })
            else:
                in_scope_recs.append(r)
        if out_of_scope:
            print(
                f"build-aider-invocation: {len(out_of_scope)}/{len(selected)} "
                f"recs deferred by site path-scope policy",
                file=sys.stderr,
            )
            for d in out_of_scope:
                print(f"  scope-defer {d['rec_id']} ({d['type']}): "
                      f"{d['reason'][:160]}", file=sys.stderr)
        selected = in_scope_recs
        if not selected:
            # All recs scope-deferred — write deferred.json + exit rc=3
            # the same way the trusted-types allowlist does. The wrapper
            # already handles rc=3 by recording the run as "deferred".
            if args.out_deferred:
                Path(args.out_deferred).write_text(
                    json.dumps(
                        {"deferred": out_of_scope,
                         "deferred_count": len(out_of_scope),
                         "applied_count": 0,
                         "reason_class": "site-scope-policy"},
                        indent=2,
                    )
                )
            print("build-aider-invocation: ALL recs out-of-scope — exiting "
                  "without dispatch", file=sys.stderr)
            return 3

    prompt, files, deferred = build_prompt(
        selected, repo_path, args.site or "", args.dispatch_kind, pre_dirty)
    # Merge scope-policy defers into the deferred list so downstream
    # logging shows both classes uniformly.
    deferred = out_of_scope + deferred

    # Article-author stub creation: aider can only write to files in
    # its chat context (`--file` args). For article recs, pre-create
    # `<repo>/changes/<slug>.body.md` and `.meta.json` as empty stubs
    # and add them to `files` so aider opens + fills them. Without this
    # the LLM exits in ~3s without producing output (no file to edit).
    #
    # FILENAMES KEYED BY SLUG, NOT REC_ID. rec_ids (art-001..art-008)
    # are scoped to a single run; two runs both proposing different
    # articles under "art-007" would race on changes/art-007.body.md
    # and the wrapper's INSERT step (which reads body file then INSERTs
    # with proposal.slug from the per-run recommendations.json) could
    # ship Run A's body under Run B's title — the
    # how-nvidia-killed-3dfx article that actually contained the
    # GeForce-4-Ti build content was caused by exactly this collision.
    # Slug-keyed filenames are stable per-article: any run proposing
    # the same slug writes the same file (idempotent, last-write-wins
    # is fine because both runs intend the same article); different
    # slugs naturally land in different files.
    #
    # ALWAYS OVERWRITE the stub. The previous `if not body_p.exists()`
    # guard left stale content from earlier runs in place and the LLM
    # frequently treated it as "already done" and skipped rewriting.
    repo_changes = repo_path / "changes"
    for r in selected:
        proposal = r.get("proposal") or r.get("article_proposal")
        if not (isinstance(proposal, dict) and proposal.get("slug")):
            continue
        rid = r.get("id", "")
        if not rid:
            continue
        slug = (proposal.get("slug") or "").strip()
        if not slug:
            continue
        repo_changes.mkdir(parents=True, exist_ok=True)
        body_p = repo_changes / f"{slug}.body.md"
        meta_p = repo_changes / f"{slug}.meta.json"
        # Force-overwrite: clean stub every run, no stale-content leakage.
        body_p.write_text(
            f"<!-- {rid} | slug={slug} | EVERY RUN STARTS FRESH HERE.\n"
            f"     REPLACE this entire file with the article body.\n"
            f"     H1 (first line, must match): # {proposal.get('title','')}\n"
            f"     Length: 1500-3000 words.\n"
            f"     Topic: {(proposal.get('primary_keyword') or '')[:80]}\n"
            f"-->\n"
        )
        meta_p.write_text(json.dumps({
            "subtitle": "",
            "excerpt": "",
            "_slug_for_safety_check": slug,  # wrapper verifies before INSERT
        }, indent=2) + "\n")
        # Carry the slug-named files into the file list aider opens.
        rel_body = str(body_p.relative_to(repo_path))
        rel_meta = str(meta_p.relative_to(repo_path))
        if rel_body not in files:
            files.append(rel_body)
        if rel_meta not in files:
            files.append(rel_meta)

    prompt, scrubbed = _scrub_dangerous_commands(prompt)
    if scrubbed:
        print(f"build-aider-invocation: scrubbed {len(scrubbed)} dangerous "
              f"shell pattern(s) from prompt: {scrubbed}", file=sys.stderr)
    Path(args.out_prompt).write_text(prompt)
    Path(args.out_files).write_text("\n".join(files) + ("\n" if files else ""))
    if args.out_deferred:
        Path(args.out_deferred).write_text(
            json.dumps({"deferred": deferred,
                        "deferred_count": len(deferred),
                        "applied_count": len(selected) - len(deferred)},
                       indent=2))

    print(f"build-aider-invocation: wrote prompt={args.out_prompt} "
          f"({len(prompt)} chars), files={args.out_files} "
          f"({len(files)} preload files), recs={len(selected)}/"
          f"{len(rec_ids)} matched, "
          f"applied={len(selected) - len(deferred)}, "
          f"deferred-by-allowlist={len(deferred)}",
          file=sys.stderr)
    if deferred:
        for d in deferred:
            print(f"  deferred {d['rec_id']} ({d['type']}): {d['reason'][:120]}",
                  file=sys.stderr)
    # If EVERY rec was deferred, the prompt has no work blocks — exit
    # with rc=3 so the wrapper writes a deferred.json instead of
    # spinning the chain on an empty prompt.
    if len(deferred) == len(selected):
        print("build-aider-invocation: every rec deferred by allowlist — "
              "nothing for the framework chain to do",
              file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
