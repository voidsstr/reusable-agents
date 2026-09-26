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
    r"\b\d(\.\d)?\s?(/\s?5|out of (5|five))\b|\b\d(\.\d)?[- ]stars?\b|★"
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
    """A number inside a price clause that is still a fact, not a price figure."""
    n, before, after = m.group(), clause[: m.start()], clause[m.end():]
    if re.match(r"[A-Za-wyz]|x[A-Za-z0-9]", after) or re.search(r"[A-Za-z]$", before):
        return True  # glued to letters: 5600X, 8GB, 1440p, R23
    if "," not in n and "." not in n and re.search(r"\b[A-Z][A-Za-z]*\s$", before):
        return True  # model name: "RTX 3060", "Pi 5", "Cyberpunk 2077"
    if re.fullmatch(r"(?:19|20)\d\d", n):
        return True
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
    t = source
    # Numbers in a clause that talks about price are price-derived ("12,357
    # PassMark points per $100 against 8,109", "the 2.48x price gap", "46% over
    # its $329 MSRP") unless they name a model, a year or, outside a per-dollar
    # clause, carry a unit ("115 fps", "24 GB", "2.3x faster").
    for c0, c1 in _clauses(source):
        clause = source[c0:c1]
        per_dollar = bool(PER_DOLLAR.search(clause))
        if not (per_dollar or PRICE_CLAUSE.search(clause)):
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


def check_rewrite(source: str, output: str) -> GuardResult:
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
    new = sorted(outn - allnums)
    if missing:
        reasons.append("missing_numbers")
    if new:
        reasons.append("new_numbers")
    return GuardResult(not reasons, reasons, missing, new)
