"""Prompts for the one model call, the strict-JSON parser, and the canned
fake model the dry run uses by default.

The system prompt holds the engine's generic rules (strict JSON, numbers only
from FACTS, tables only from placeholders, links only to FACTS paths, no
price talk, the holiday:<id> tag rule) plus the site's own rules and voice
from config. The user message holds the topic chosen in code, the active
occasion (built from ``seasonal_calendar.ActiveOccasion`` fields only: id,
label, window, date, keywords; never an occasion's deal guidance text), the
tags the code requires, and the facts pack. No trends, forum or Amazon text
is ever placed in either message.
"""
from __future__ import annotations

import json
import re
from typing import Any, Mapping, Optional, Sequence

from framework.core.article_link_classes import render_link_class_directive

FACTS_BEGIN = "<<<FACTS_JSON"
FACTS_END = "FACTS_JSON>>>"
OUTPUT_KEYS = ("title", "slug", "subtitle", "excerpt", "body_md", "faqs", "tags", "buystrip_asins",
               "expected_links")


def system_prompt(settings, *, slug: str) -> str:
    pr = settings.prompt or {}
    kinds = settings.placeholder_kinds
    ph_lines = [f"  {{{{{k}:<key>}}}} — {v.get('describe') or 'rendered from the site database'}"
                for k, v in sorted(kinds.items())]
    site_rules = [f"  - {r}" for r in (pr.get("rules") or [])]
    link_dir = render_link_class_directive(settings.link_classes, "")
    budget = ", ".join(f"${int(x):,}" for x in (settings.claims_block.get("budget_labels") or []))
    return "\n".join(filter(None, [
        f"You write ONE guide for {pr.get('brand') or settings.site}: {pr.get('site_description') or ''}".strip(),
        (f"Voice: {pr['voice']}" if pr.get("voice") else ""),
        "",
        "OUTPUT: return exactly one JSON object and nothing else (no text before or after it, no code fence).",
        "Keys:",
        "  title: string, at most 110 characters, the question the guide answers",
        f"  slug: string, exactly \"{slug}\"",
        "  subtitle: string, at most 200 characters",
        "  excerpt: string, 120-160 characters: the answer in one sentence, with one FACTS figure",
        "  body_md: GitHub-flavoured markdown (## headings, paragraphs, short lists)",
        "  faqs: array of 3-6 objects {\"question\": string, \"answer\": string}",
        "  tags: array of strings (see TAGS)",
        f"  buystrip_asins: array of up to {int(settings.buystrip.get('max') or 6)} ASINs, taken only from FACTS "
        "rows of ranked picks, best first",
        "  expected_links: array of every site path you linked in body_md",
        "",
        "HARD RULES (a draft that breaks one is rejected by code after you answer):",
        "1. NUMBERS. Every figure with a unit (W, kW, Wh, kWh, hours, V, A) is copied from FACTS: the same value "
        "in the same unit (Wh may be written as kWh), rounded to no fewer decimals than you show. Never add, "
        "subtract, average, multiply or convert figures yourself, and never estimate a value FACTS does not give: "
        "say the maker does not publish it. A runtime is always given with its range and its basis from FACTS. "
        "Counts ('3 of the 27 stations') are counted from FACTS rows.",
        "2. TABLES. Never write a markdown table. Every table on the page comes from a placeholder, written alone "
        "on its own line between paragraphs (never in a heading, list item or quote):",
        *ph_lines,
        "   Use only keys that appear in FACTS or in TOPIC. Write every placeholder TOPIC lists as required.",
        "3. LINKS. Only markdown links to site paths that appear in FACTS (columns ending in path) or in TOPIC, "
        "written as site-relative paths ([anchor](/product/B0XXXXXXXX)). No other website, no bare URLs, no "
        "Amazon link of any kind, no images, no HTML.",
        link_dir.rstrip(),
        "4. PRICES. Never write a price, a price range, a discount, a deal, a sale, 'lowest', 'cheapest ever', "
        "'price drop', 'since <date>', a price history or a stock status. Prices render live, stamped, beside the "
        "text." + (f" A budget label ({budget}) may appear only as 'under $N' or 'the $N kit'." if budget else ""),
        "5. TAGS. tags must contain every tag listed under TAGS, exactly. holiday:<id> tag rule: when OCCASION is "
        "given, the guide is written for it and tags include holiday:<id> exactly as given, so the homepage "
        "rotation can feature it while the occasion is current. Never invent another holiday: or lane: tag.",
        "6. ANSWER FIRST. The first sentence of body_md answers the topic's main question with FACTS figures and "
        "states its assumption (for example the usable-capacity basis). Answer every TOPIC question; pages under "
        "'link instead of repeating' are linked, never restated.",
        "7. HONESTY. Nothing was tested by us: no first-hand testing, no star ratings, no reviews, no Amazon text, "
        "no invented people. Facts only from FACTS.",
        *(["", "SITE RULES (binding):", *site_rules] if site_rules else []),
    ]))


def occasion_block(active) -> str:
    """OCCASION lines from an ActiveOccasion (seasonal_calendar), fields only."""
    if active is None:
        return ""
    o = active.occasion
    kw = ", ".join(list(o.recipe_keywords)[:8])
    return (f"OCCASION: holiday:{o.id} — {o.label}; window {active.window.upper()}, starts "
            f"{active.when.isoformat()}" + (f"; reader keywords: {kw}" if kw else ""))


def user_prompt(entry, *, facts_json: str, tags: Sequence[str], active_occasion=None,
                placeholders_required: Sequence[str] = (), addendum: str = "") -> str:
    lines = ["TOPIC", f"  working title: {entry.working_title}", f"  slug: {entry.slug}"]
    if entry.format:
        lines.append(f"  format: {entry.format}")
    if entry.brief:
        lines.append(f"  brief: {entry.brief}")
    if entry.questions:
        lines.append("  questions this guide must answer (no single page on the site answers them):")
        lines += [f"    - {q}" for q in entry.questions]
    if entry.link_instead_of_repeating:
        lines.append("  link instead of repeating: " + ", ".join(entry.link_instead_of_repeating))
    if placeholders_required:
        lines.append("  required placeholders: " + " ".join(placeholders_required))
    if entry.scenario:
        lines.append("  the question's own parameters (may appear in prose): "
                     + "; ".join(f"{u}: {', '.join(f'{v:g}' for v in vals)}" for u, vals in entry.scenario.items()))
    occ = occasion_block(active_occasion)
    if occ:
        lines += ["", occ]
    lines += ["", "TAGS (all required): " + ", ".join(tags), "",
              "FACTS (JSON; the only source of figures, names and link paths):", FACTS_BEGIN, facts_json, FACTS_END]
    if addendum:
        lines += ["", "YOUR PREVIOUS DRAFT FAILED THESE CHECKS. Return the full corrected JSON object; fix every "
                  "item, change nothing else:", addendum]
    return "\n".join(lines)


class DraftParseError(ValueError):
    pass


def parse_draft(text: str) -> dict:
    """The model's JSON object (a code fence is tolerated, nothing else)."""
    t = (text or "").strip()
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", t, re.S)
    if m:
        t = m.group(1).strip()
    if not t.startswith("{"):
        raise DraftParseError("the reply is not a JSON object")
    try:
        obj = json.loads(t)
    except json.JSONDecodeError as e:
        raise DraftParseError(f"invalid JSON: {e}") from None
    if not isinstance(obj, dict):
        raise DraftParseError("the reply is not a JSON object")
    missing = [k for k in ("title", "slug", "excerpt", "body_md") if not isinstance(obj.get(k), str) or not obj[k].strip()]
    if missing:
        raise DraftParseError(f"missing or empty: {', '.join(missing)}")
    obj.setdefault("subtitle", "")
    for k in ("faqs", "tags", "buystrip_asins", "expected_links"):
        if not isinstance(obj.get(k), list):
            obj[k] = []
    obj["faqs"] = [{"question": str(f.get("question") or ""), "answer": str(f.get("answer") or "")}
                   for f in obj["faqs"] if isinstance(f, Mapping)]
    return obj


def facts_from_prompt(user_text: str) -> dict:
    m = re.search(re.escape(FACTS_BEGIN) + r"\n(.*)\n" + re.escape(FACTS_END), user_text or "", re.S)
    return json.loads(m.group(1)) if m else {}


class CannedModel:
    """The dry run's default model: builds a deterministic draft from the
    FACTS in the prompt (first five product paths, first two compat paths,
    the required placeholders, one runtime figure). It never calls a network.
    `override` (a JSON string or dict) replaces the draft verbatim (the
    hand-written red-team bodies)."""

    class provider:  # noqa: N801 - mimics AIClient.provider
        kind = "canned"
        name = "canned"

    def __init__(self, override: Any = None):
        self.override = override
        self.calls = 0

    def chat(self, messages, **_kw) -> str:
        self.calls += 1
        if self.override is not None:
            return self.override if isinstance(self.override, str) else json.dumps(self.override)
        # the first user message carries TOPIC, TAGS and FACTS; a re-prompt only adds the addendum
        user = next((m["content"] for m in messages if m.get("role") == "user" and FACTS_BEGIN in m["content"]), "")
        facts = facts_from_prompt(user)
        slug = (re.search(r"^  slug: (\S+)$", user, re.M) or [None, "draft"])[1]
        tags = [t.strip() for t in ((re.search(r"^TAGS \(all required\): (.*)$", user, re.M) or [None, ""])[1]).split(",")
                if t.strip()]
        phs = (re.search(r"^  required placeholders: (.*)$", user, re.M) or [None, ""])[1].split()
        paths = sorted({v for rows in facts.values() for r in rows for k, v in r.items()
                        if isinstance(v, str) and v.startswith("/") and k.endswith("path")})
        products = [p for p in paths if p.startswith("/product/")][:5]
        compats = [p for p in paths if p.startswith("/compat/")][:2]
        rt = next((r for r in facts.get("runtimes") or [] if r.get("hours_est") is not None), None)
        first = (f"On the {rt.get('model_name') or 'first station in our data'}, the estimate runs from "
                 f"{rt['hours_low']} to {rt['hours_high']} hours ({rt['hours_est']} hours expected), using rated "
                 "capacity times 0.842 as usable energy."
                 if rt else "Our data answers this from each maker's published specifications.")
        body = [first, "", "## What the numbers say", ""]
        body += [f"See [this station]({p}) for its full runtime card." for p in products]
        body += ["", "## Charging between outages", ""]
        body += [f"Check [what charges it]({p}) before buying panels." for p in compats]
        for ph in phs:
            body += ["", ph]
        body += ["", "See [how we calculate](/how-we-calculate) for the formula."]
        asins = [p.rsplit("/", 1)[-1] for p in products]
        return json.dumps({
            "title": "Canned draft: " + slug.replace("-", " ")[:90], "slug": slug,
            "subtitle": "A canned dry-run body built from the facts pack",
            "excerpt": "A canned dry-run draft that links the stations in the facts pack and renders every table "
                       "from the site's own data.",
            "body_md": "\n".join(body),
            "faqs": [{"question": "Where do these numbers come from?",
                      "answer": "From the makers' published specifications and our runtime estimates."}],
            "tags": tags, "buystrip_asins": asins[:3], "expected_links": products + compats})
