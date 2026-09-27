"""Deterministic guard for rewrites that strip prices and ratings.

Why (2026-09-26 local-model bake-off): product FAQ answers and comparison
verdicts quote generation-day prices ("$759 vs $479.99") and scraped review
data ("4.6 out of 5 from 929 reviewers"), which go stale the day after they
are written and ship in FAQPage/Product JSON-LD. A model can rewrite them
price-free, but the bake-off showed every model sometimes leaves a price or
rating in, drops a benchmark number, invents one, or leaks prompt scaffolding.
This module catches those mechanically, so a pipeline can accept a cheap
local rewrite only when it is clean and route everything else to a stronger
model.

It DETECTS; it never rewrites.

  check_rewrite(source, output) -> GuardResult
  has_price_or_rating(text) -> bool
  is_price_or_review_question(question) -> bool
  SYSTEM_PROMPT  - the editing instructions every rewriter gets

"Required" numbers are every number in the source except the ones that ARE
a price, a price-derived figure (a percentage within 60 chars of a dollar
amount, "54 fps per $100", "4.2 cents", "4x the money") or rating/review
data. They must all survive; any number in the output that is not in the
source is a fabrication.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .article_claims_guard import find_star_ratings

SYSTEM_PROMPT = """You edit product-comparison verdicts and product FAQ answers for a shopping site.

Rewrite the text you are given so that it contains:
- NO prices and NO price comparisons: no dollar amounts, no "costs X% more", no "$N cheaper", no "at current street prices".
- NO ratings or review data: no stars, no "x/5" or "out of 5", no review or rating counts.

Everything else must survive exactly:
- Keep every benchmark result, spec, capacity, speed, percentage that is not about price, model name and unit, character for character.
- Do not add any fact, number, product, or claim that is not in the original.
- If a sentence only made sense because of a price, rephrase it around value, budget or use case without numbers (for example "the cheaper option" or "check the current listing for today's price").
- Keep the same voice, point of view and recommendation, and roughly the same length.

Output only the rewritten text. No preamble, no notes, no labels, no quotation marks around it.
"""

PRICE = re.compile(r"\$\s?\d|\b\d[\d,.]*\s?(usd|dollars|bucks|cents?)\b|costs? (only )?\$?\d", re.I)
RATING = re.compile(
    r"\b\d(\.\d{1,2})?\s?(/\s?5|out of (5|five))\b|\b\d(\.\d{1,2})?[- ]stars?\b|★"
    r"|\b(?!(?:19|20)\d\d\b)[\d,]+\s+(customer\s+|verified\s+)?"
    r"(reviews|ratings|reviewers|shoppers|buyers|owners|customers)\b", re.I)
DOLLARS = re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?(?:\s?[-–]\s?\$?\d[\d,]*(?:\.\d+)?)?[kKmM]?")
PRICE_PCT = re.compile(
    r"(?:costs?|priced|price|pay(?:ing)?|spend(?:ing)?)\s+(?:only\s+|about\s+|roughly\s+|just\s+)?"
    r"\d[\d.]*\s?%\s+(?:more|less)|\d[\d.]*\s?%\s+(?:cheaper|pricier|more expensive|less expensive)"
    r"|\d[\d.]*\s?%\s+(?:less|more)\s+(?:money|cash)", re.I)
PRICE_DERIVED = re.compile(
    r"\d[\d.,]*\s?cents?\b|\d[\d.,]*\s*[a-z/%]*\s+per\s+\$\s?\d[\d,]*"
    r"|\d+(\.\d+)?x\s+the\s+(money|price|cost)", re.I)
RATING_NEAR = re.compile(r"\b\d\.\d\b")
PRICE_MULT = re.compile(r"\b\d+(?:\.\d+)?\s?x\s+(?:as much|the (?:price|money|cost)|more expensive|the street price)", re.I)
NUM = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")
# Prompt scaffolding a model echoes back instead of just the answer
# (gemma4:26b did this on 2 of 50 FAQ items in the bake-off).
SCAFFOLD = re.compile(
    r"^\s*(question|answer|rewritten( answer| text| verdict)?|answer to rewrite|verdict to rewrite"
    r"|here('s| is) (the )?(rewritten|revised|updated))\s*[:\-]", re.I | re.M)
# Pure price or review LOOKUPS only. Value questions ("Is it worth it over the
# original console?", "any good for the price?", "HDR on a monitor this
# cheap?") are rewritten, because a feature-based answer still helps; a
# price lookup has no price-free answer.
PRICE_OR_REVIEW_QUESTION = re.compile(
    r"\b(how much|what does (it|this|that|the [\w\s-]{1,60}) cost|what('s| is) the (current )?price"
    r"|current price|price (of|for) (it|this|the)|on sale|discount|coupon"
    r"|how (well|highly) (rated|reviewed)|how (well|highly|good) (is|are|was) ([\w\s-]{1,60} )?(rated|reviewed)|how (is|are) ([\w\s-]{1,40} )?(rated|reviewed)"
    r"|how (do|does|would) ([\w\s-]{1,40} )?(buyers|owners|customers|reviewers|users|people|shoppers) rate|well[- ](reviewed|rated)|(rated|reviewed) by|reviews? say|review score|star rating|how many stars|ratings?\b"
    r"|what do (people|users|owners|buyers|customers|reviewers) (say|think))",
    re.I)


PER_DOLLAR = re.compile(r"\bper\s+(?:\$|dollar\b)", re.I)
PRICE_CLAUSE = re.compile(
    r"\$\s?\d|\bMSRP\b|\bprices?d?\b|\bcosts?\b|\blistings?\b|\bpay\b|\bas much\b|\bexpensive\b|\bcheaper\b", re.I)
RATING_CLAUSE = re.compile(
    r"\brat(?:ing|ings|ed)\b|\bstars?\b|\breviews?\b|\breviewers?\b|/\s?5\b|\bout of (?:5|five)\b", re.I)
ISO_DATE = re.compile(r"\b(?:19|20)\d\d-\d\d-\d\d\b")
# Capitalised words that start sentences or clauses, so a number after them is
# not a model name ("At 2026 street prices", "In 2025 the card...").
_NOT_A_NAME = frozenset(
    "a an and as at both but by each for from if in into its it's of on only or over per so than that "
    "the their these this those to under until when where while with".split())
CLAUSE_BREAK = re.compile(r"(?<=[.!?])\s+|;\s+|,\s+(?:but|while|whereas|which|so)\s+|\s+(?:but|while|whereas)\s+|\s+[—–]\s+")
UNIT_AFTER = re.compile(
    r"\s?(?:fps|gb|tb|mb|gib|w|watts?|mhz|ghz|hz|ms|mm|nm|°c|cores?|threads?|tok(?:ens)?/s|points?|pts"
    r"|geekbench|passmark|cinebench|x\s+(?:faster|slower|quicker|the (?:speed|performance)))\b", re.I)


def _clauses(text: str) -> list[tuple[int, int]]:
    out, start = [], 0
    for m in CLAUSE_BREAK.finditer(text):
        out.append((start, m.start()))
        start = m.end()
    out.append((start, len(text)))
    return out


def _protected_number(clause: str, m: re.Match, allow_units: bool) -> bool:
    """A number inside a price or rating clause that is still a fact, not a
    price or rating figure. Years are not protected here: in such a clause they
    date the price snapshot ("At 2026 street prices")."""
    n, before, after = m.group(), clause[: m.start()], clause[m.end():]
    if re.match(r"[A-Za-wyz]|x[A-Za-z0-9]", after) or re.search(r"[A-Za-z]$", before):
        return True  # glued to letters: 5600X, 8GB, 1440p, R23
    w = re.search(r"\b([A-Z][A-Za-z]*)\s$", before)
    if "," not in n and "." not in n and w and w.group(1).lower() not in _NOT_A_NAME:
        return True  # model name: "RTX 3060", "Pi 5", "Cyberpunk 2077"
    return allow_units and bool(UNIT_AFTER.match(after))


def _blank(s: str, start: int, end: int) -> str:
    return s[:start] + " " * (end - start) + s[end:]


def _norm(n: str) -> str:
    return n.replace(",", "").rstrip(".")


def numbers(text: str) -> set[str]:
    return {_norm(m.group()) for m in NUM.finditer(text or "")}


def has_price(text: str) -> bool:
    return bool(PRICE.search(text or ""))


def has_rating(text: str) -> bool:
    return bool(RATING.search(text or "")) or bool(find_star_ratings(text or ""))


def has_price_or_rating(text: str) -> bool:
    return has_price(text) or has_rating(text)


def is_price_or_review_question(question: str) -> bool:
    """A question whose whole point is the price or the reviews ("How much does
    it cost?", "What do reviews say?"). Rewriting its answer price-free leaves
    nothing useful, so pipelines should drop the pair instead."""
    return bool(PRICE_OR_REVIEW_QUESTION.search(question or ""))


_Q_AMOUNT = r"\$\s?\d[\d,]*(?:\.\d+)?(?:\s?[-–]\s?\$?\d[\d,]*(?:\.\d+)?)?"
_Q_AT_FOR = re.compile(r"\s(at|for)\s+(?:around\s+|about\s+|roughly\s+|just\s+|only\s+|under\s+)?" + _Q_AMOUNT, re.I)
_Q_WORTH = re.compile(r"\bworth\s+(?:around\s+|about\s+)?" + _Q_AMOUNT, re.I)
_Q_ANY = re.compile(r"(?:around\s+|about\s+|roughly\s+|just\s+|only\s+|under\s+)?" + _Q_AMOUNT, re.I)


def strip_price_from_question(question: str) -> str:
    """Deterministically replace a quoted price in a FAQ QUESTION with a
    price-free phrase ("Is it worth $31.99 over X?" -> "Is it worth the price
    over X?"). Questions are short labels, not editorial prose, so this is a
    mechanical edit; the answer is rewritten separately."""
    q = question or ""
    if not re.search(_Q_AMOUNT, q):
        return q
    q = _Q_WORTH.sub("worth the price", q)
    q = _Q_AT_FOR.sub(lambda m: f" {m.group(1)} {'its' if m.group(1).lower() == 'at' else 'the'} price", q)
    q = _Q_ANY.sub("the price", q)
    return re.sub(r"\s{2,}", " ", q).strip()


def required_numbers(source: str) -> tuple[set[str], set[str]]:
    """(numbers that must survive, every number in the source)."""
    allnums = numbers(source)
    t = ISO_DATE.sub(lambda m: " " * len(m.group()), source)  # scrape timestamps
    # Numbers in a clause that talks about price or ratings are price- or
    # rating-derived ("12,357 PassMark points per $100 against 8,109", "the
    # 2.48x price gap", "46% over its $329 MSRP", "4.30 across 2673 reviews")
    # unless they name a model or, outside a per-dollar clause, carry a unit
    # ("115 fps", "24 GB", "2.3x faster").
    for c0, c1 in _clauses(source):
        clause = source[c0:c1]
        per_dollar = bool(PER_DOLLAR.search(clause))
        if not (per_dollar or PRICE_CLAUSE.search(clause) or RATING_CLAUSE.search(clause)):
            continue
        for m in NUM.finditer(clause):
            if not _protected_number(clause, m, allow_units=not per_dollar):
                t = _blank(t, c0 + m.start(), c0 + m.end())
    t = PRICE_DERIVED.sub(lambda m: " " * len(m.group()), t)
    t = RATING.sub(lambda m: " " * len(m.group()), t)
    spans = [m.span() for m in DOLLARS.finditer(t)]
    for m in re.finditer(r"\d[\d.]*\s?%", t):
        if any(abs(m.start() - b) < 60 or abs(a - m.end()) < 60 for a, b in spans):
            t = _blank(t, m.start(), m.end())
    t = PRICE_MULT.sub(lambda m: " " * len(m.group()), t)
    t = PRICE_PCT.sub(lambda m: " " * len(m.group()), t)
    t = DOLLARS.sub(lambda m: " " * len(m.group()), t)
    for m in RATING_NEAR.finditer(t):
        ctx = source[max(0, m.start() - 40): m.end() + 40].lower()
        if re.search(r"rating|rated|stars?|reviews?|reviewers", ctx):
            t = _blank(t, m.start(), m.end())
    return numbers(t), allnums


@dataclass
class GuardResult:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    missing_numbers: list[str] = field(default_factory=list)
    new_numbers: list[str] = field(default_factory=list)


def check_rewrite(source: str, output: str, context: str = "") -> GuardResult:
    """`context` is other text from the same record (sibling fields the model
    saw). A number found there was moved, not invented."""
    out = (output or "").strip()
    reasons: list[str] = []
    if not out:
        return GuardResult(False, ["empty"])
    if has_price(out):
        reasons.append("price_left")
    if has_rating(out):
        reasons.append("rating_left")
    if SCAFFOLD.search(out):
        reasons.append("scaffold_leak")
    req, allnums = required_numbers(source or "")
    outn = numbers(out)
    missing = sorted(req - outn)
    new = sorted(outn - allnums - numbers(context))
    if missing:
        reasons.append("missing_numbers")
    if new:
        reasons.append("new_numbers")
    return GuardResult(not reasons, reasons, missing, new)


# ---------------------------------------------------------------------------
# Second opinion for rewrites the number check rejects.
#
# Why (2026-09-27): number matching cannot tell "33% better value" (price-
# derived, fine to drop) from "a 30% lead in Cinebench" (a fact) when both sit
# in a sentence about price. Labelling all 767 verdict-scrub rejections found
# 749 false positives. Four rule-based redesigns reached 91-97% on those labels,
# but adversarial red-teams showed each let MORE dropped benchmarks through
# than the baseline. So check_rewrite stays the fast first pass, and a rewrite
# it rejects only on number or rating grounds goes to a model that reads the
# text. price_left, scaffold_leak and empty are never overridden: a "$" left in
# the output is unambiguous.
JUDGEABLE_REASONS = frozenset({"missing_numbers", "new_numbers", "rating_left"})

JUDGE_PROMPT = """You audit edits of product-comparison text for a shopping site. Each REWRITE was supposed to remove every price, price comparison, star rating and review count from its SOURCE and keep every other fact.

For each item, compare SOURCE and REWRITE and report:
- lost_facts: non-price facts that are in SOURCE but missing or changed in REWRITE. Facts include benchmark scores, fps, test results, specs, capacities, core counts, product and model names, launch or release dates, and percentages or multiples that describe performance. These are NOT facts to keep: prices, per-dollar or per-$100 figures, value percentages or multiples computed from prices ("33% better value", "a 2.6x advantage" per dollar), discounts, MSRP comparisons, ratings, review counts, and dates that only timestamp a price. Dropping part of a product name while the product stays clearly identified is fine.
- invented: numbers or claims in REWRITE that appear in neither SOURCE nor ALSO_IN_RECORD.
- price_or_rating_left: true only if REWRITE still states a price amount, a quantified price comparison (a number, percentage or exact multiple or fraction of a price, including in words such as "costs twice as much" or "less than half the price"), a rating value or a review count. Qualitative value language is REQUIRED by the edit and is NOT a leftover price: "the cheaper card", "costs more", "a modest premium", "better value", "for the money", "launch pricing", "check the current listing for today's price", "top-reviewed", "well reviewed".

Be strict: if you are unsure whether a dropped number was a fact, list it in lost_facts.

Return ONLY a JSON array with one object per item, in order:
{"id": "<id>", "lost_facts": ["..."], "invented": ["..."], "price_or_rating_left": false}"""


@dataclass
class JudgeVerdict:
    ok: bool
    lost_facts: list[str] = field(default_factory=list)
    invented: list[str] = field(default_factory=list)
    price_or_rating_left: bool = False
    error: str = ""


def judgeable(result: GuardResult) -> bool:
    """True when every reason check_rewrite gave can be overridden by the judge."""
    return bool(result.reasons) and set(result.reasons) <= JUDGEABLE_REASONS


def judge_rewrites(client, items: list[dict], batch: int = 12, timeout: int = 900) -> dict[str, JudgeVerdict]:
    """Ask `client` (framework ai client: chat(messages, ...) -> str) whether each
    rewrite lost a fact, invented one, or kept a price/rating.

    items: [{"id": str, "source": str, "rewrite": str, "context": str (optional)}].
    A number the judge calls invented but that appears in the item's context is
    dropped from `invented` (it was moved from elsewhere in the record).
    An item the judge does not answer comes back ok=False with `error` set.
    """
    from .llm_json import extract_json_array

    out: dict[str, JudgeVerdict] = {}
    for b0 in range(0, len(items), batch):
        chunk = items[b0: b0 + batch]
        parts = []
        for it in chunk:
            parts.append(f"### id {it['id']}\nSOURCE:\n{it['source']}\n\nREWRITE:\n{it['rewrite']}\n")
            also = sorted(numbers(it["rewrite"]) & numbers(it.get("context", "")) - numbers(it["source"]))
            if also:
                parts.append(f"ALSO_IN_RECORD (numbers stated elsewhere in the same record): {', '.join(also)}\n")
        try:
            raw = client.chat([{"role": "system", "content": JUDGE_PROMPT},
                               {"role": "user", "content": "\n".join(parts)}],
                              temperature=0.0, max_tokens=8000, timeout=timeout)
            answers = {str(a.get("id")): a for a in extract_json_array(raw) if isinstance(a, dict)}
        except Exception as e:  # noqa: BLE001 - any failure means "not verified"
            answers, err = {}, f"judge call failed: {e}"[:200]
        else:
            err = "judge gave no verdict for this item"
        for it in chunk:
            a = answers.get(str(it["id"]))
            if a is None:
                out[str(it["id"])] = JudgeVerdict(False, error=err)
                continue
            ctx_nums = numbers(it.get("context", ""))
            lost = [str(x) for x in a.get("lost_facts") or [] if str(x).strip()]
            invented = [str(x) for x in a.get("invented") or []
                        if str(x).strip() and not (numbers(str(x)) and numbers(str(x)) <= ctx_nums)]
            left = bool(a.get("price_or_rating_left"))
            out[str(it["id"])] = JudgeVerdict(not (lost or invented or left), lost, invented, left)
    return out
