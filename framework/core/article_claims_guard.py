"""Honesty guard for article bodies: invented testing, copied star ratings,
point prices.

Why (2026-09-25 AI-visibility audit, F119 / F075 / F100 / F108 / F120): the
kitchen buying guides AI assistants fetch and quote carried

  * first-hand testing that never happened ("we ran five sealers through the
    same four-part protocol", "4,000 cracked eggs", "survived 50 dishwasher
    cycles", "in our test kitchen", a Wolf range and a calibrated Fluke
    thermometer), sometimes attributed to a real, named person;
  * Amazon star ratings and review counts copied into prose and tables
    ("4.6 (4,318)") — not obtained from the Product Advertising API and
    stale the day after publication;
  * point prices in table "Price" columns and "Street price: $449.99"
    lines, which the page's own live "Check price on Amazon" button
    contradicted.

The writer prompts were fixed at the source; this module is the deterministic
backstop. It DETECTS — it never rewrites prose (editorial prose is Opus-only).

  check(body_md, *, subtitle, excerpt, bucket, site_hint) -> ClaimsAudit

The implementer's article-insert step refuses the INSERT when `passes` is
False and re-queues the rec with `failure_reason()` as the addendum, the same
path the inline-link guard uses. `framework.cli.article_claims_guard` sweeps
recently written rows (the article-author often INSERTs itself) and reports.

Policy is data, not code: `config/article-claims-guard-config.json` (repo
default; the storage copy at the same key overrides). Per site, by substring
of the rec's agent_id / site hint:

  first_hand_claims: "reject" | "warn" | "off"
  star_ratings:      "reject" | "warn" | "off"
  point_prices:      "reject" | "warn" | "off"   (only for `price_buckets`)
  price_buckets:     buckets/categories where a product point price is banned
                     (buying guides — never recipe clusters, where
                     "≈$2.50/serving" is the point).

Precision over recall: third-party attributions ("owners report…",
"America's Test Kitchen found…"), explicit disclaimers ("we did not test"),
price bands ("under $50", "$50–$100") and per-serving costs all pass.

2026-10-01 AI-reach audit additions (issue ids internal-numeric-
contradictions, price-claims-contradict-tracked-prices, stale-or-misleading-
price-anchors, false-in-stock-claims, cpu-deal-pick-reseller-asin):

  stock_claims:        "in stock" / "sells out" assertions — stock is a
                       render-time fact the body cannot keep true (warn)
  stale_claims:        a known-stale claim list, starting with "October
                       purchases get January returns" (reject — clearly wrong)
  numeric_consistency: a stated "$N cheaper / extra $N" that matches no gap
                       between two prices stated in the same body, and
                       "N× … at every <x>" universal ratio claims (warn)
  price_consistency:   with a caller-supplied live price map (`prices=`,
                       keyed by the body's own link paths): a Budget pick
                       that is not the cheapest priced pick, "priciest /
                       cheapest on this list" that live prices contradict,
                       and "above/below its $N MSRP" sentences the linked
                       product's current price contradicts (warn)

Keys missing from the config take `NEW_CHECK_DEFAULTS` (so the storage
config only has to name a key to change it). An explicit `policy=` dict
passed to `check()` is used as-is: a missing key there means "off".
"""
from __future__ import annotations

import dataclasses
import json as _json
import pathlib as _pathlib
import re
import sys as _sys
from typing import Iterable

_CONFIG_KEY = "config/article-claims-guard-config.json"
_REPO_CONFIG = _pathlib.Path(__file__).resolve().parents[2] / "config" / "article-claims-guard-config.json"

_FP = r"\b(?:we|we've|we\s+have|i|i've|i\s+have|our\s+team|our\s+testers?)\b"

# First-person narration of a test protocol, a measurement or a result.
FIRST_HAND = re.compile(r"""(
    """ + _FP + r"""[^.!?\n]{0,40}?\b(?:tested|re-tested|ran|timed|measured|clocked|weighed\s+(?:each|every|the)|tracked|froze|microwaved|thawed|dropped|abused|tortured|scrubbed|sealed|submerged|put\s+(?:it|them|each|a\s+test\s+set))\b
  | \bin\s+our\s+(?:own\s+)?(?:test(?:ing)?|test[- ]kitchen|editorial\s+kitchen|kitchens?)\b(?!\s+(?:catalog|shop|store))
  | \bin\s+our\s+[\w°\s"“”'-]{0,30}?\b(?:tests?|trial)\b
  | \bour\s+(?:own\s+)?(?:test[- ]kitchen|test\s+bench|bench\s+tests?|test\s+rotation|test\s+results|testers?|measurements?|kitchen\s+log|editorial\s+kitchen)\b
  | \bour\s+(?:standard\s+)?\d+-(?:day|week|month|cycle)\s+\w*\s*(?:test|trial|log|rotation)\b
  | \bour\s+[\w-]+\s+(?:test|tests|testing|trial)\b
  | \b(?:from|in)\s+our\s+(?:test|testing|bench)\b
  | \b(?:after|over|across)\s+(?:a\s+)?(?:\d+|one|two|three|four|five|six|twelve|eighteen|several|a)\s*\+?\s*(?:full\s+)?(?:months?|weeks?|years?|days?)(?:\s+of)?\s+(?:daily\s+|in-kitchen\s+|hands-on\s+|side-by-side\s+)?testing
  | \b(?:\d+|one|two|three|four|five|six|twelve|eighteen|several)\s+(?:months?|weeks?|years?|days?)\s+of\s+(?:daily\s+|in-kitchen\s+|hands-on\s+|side-by-side\s+)?testing
  | \bsurvived\s+(?:a\s+)?\d+[^.]{0,30}(?:cycles|sautés|drops|simulation)
  | \b(?:five|four|six|\d+)\s+tested\s+(?:picks|sets|machines|models|pans|products|knives)\b
  | \bhand-tested\b | \btest[- ]kitchen\s+(?:picks|notes)\b
  | \bthermal\s+camera\b | \bK-type\s+thermocouple\b
  | \bhow\s+we\s+tested\b | \bwhat\s+we\s+tested\b
)""", re.I | re.X)

# Explicit disclaimers are the honest form — never flag them.
_NEGATED = re.compile(
    r"\b(?:not|never|no|didn't|did\s+not|isn't|aren't|weren't|wasn't)\b[^.!?]{0,40}"
    r"\b(?:our\s+own|tested|test|measur\w*|hands-on|bench|lab|individually)", re.I)

STAR_RATING = re.compile(r"""(
    \b[1-5]\.\d\s*(?:★|stars?\b|-star\b|/\s*5\b|out\s+of\s+5)
  | ★\s*[1-5]\.\d | \b[1-5]\.\d★
  | \b[1-5]\.\d\s*\(\s*[\d,]{2,}\+?\s*\)
  | \b[\d,]{3,}\+?\s*(?:verified\s+|amazon\s+|customer\s+|owner\s+)?(?:ratings|reviews|reviewers)\b
)""", re.I | re.X)

_PRICE = r"\$\d[\d,]*(?:\.\d{1,2})?"
_POINT_PRICE_LABEL = re.compile(
    r"\*\*\s*(?:street\s+|approximate\s+|approx\.?\s+|typical\s+)?price[^*:]{0,20}:?\s*\*\*:?\s*~?(?:about\s+|around\s+)?" + _PRICE, re.I)
# "(about $52)" and the dated form "(about $52 as of September 2026)".
_PAREN_PRICE = re.compile(r"\((?:about|around|roughly|approx\.?|~)?\s*~?" + _PRICE +
                          r"(?:\s+(?:as\s+of|at\s+last\s+check|in|on)\b[^)]{0,40})?\)", re.I)
# "list $629", "list price of $629", "below its $629 list price", "~$500 at
# last check", "$52 as of September 2026": a dated or list anchor is still a
# point price — the stale "list $629" came from an inactive catalog row.
_MONTHS = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*"
_ANCHOR_PRICE = re.compile(
    r"\blist(?:\s+price)?(?:\s+of)?\s*:?\s*~?" + _PRICE +
    r"|" + _PRICE + r"\s+list(?:\s+price)?\b"
    r"|~?" + _PRICE + r"\s+(?:at\s+last\s+check|as\s+of\s+(?:" + _MONTHS + r"|\d{4}|\d{1,2}/))", re.I)
_HEADING_PRICE = re.compile(r"^#{2,4}\s+.+?(?:\s[—–-]|,)\s*~?" + _PRICE + r"\s*$", re.M)
_TABLE_PRICE_HEADER = re.compile(r"\b(?:price|street\s+price|approx\.?\s+price|typical\s+price)\b", re.I)
_TABLE_NOT_POINT = re.compile(r"tier|band|range|per[- ]|/\s*year|cost\s+per|total|lifespan", re.I)
_BAND = re.compile(r"(?:under|below|up\s+to|less\s+than|over)\s*\$|" + _PRICE + r"\s*[-–]\s*\$?\d", re.I)

_ABBR = re.compile(r"\b(?:e\.g|i\.e|vs|St|Dr|Mr|Mrs|approx|U\.S|etc|Inc|Co|Jr|Sr)\.$", re.I)
_SENT_END = re.compile(r"[.!?][*_\"”')\]]*\s+(?=[A-Z0-9\[*\"“(\$])")


def _sentences(text: str) -> Iterable[str]:
    """Table rows whole; prose split into sentences (abbreviation-aware)."""
    for line in (text or "").split("\n"):
        if not line.strip():
            continue
        if line.strip().startswith("|"):
            yield line.strip()
            continue
        start = 0
        for m in _SENT_END.finditer(line):
            head = line[start:m.end()].strip()
            if _ABBR.search(line[start:m.start() + 1]):
                continue
            if head:
                yield head
            start = m.end()
        tail = line[start:].strip()
        if tail:
            yield tail


def find_first_hand_claims(text: str) -> list[str]:
    """Sentences that claim first-hand testing, measurement or long-term use."""
    out = []
    for s in _sentences(text):
        if FIRST_HAND.search(s) and not _NEGATED.search(s):
            out.append(s[:240])
    return out


def find_star_ratings(text: str) -> list[str]:
    """Copied star ratings / review counts ("4.6 (4,318)", "12,000+ reviews")."""
    out = []
    for s in _sentences(text):
        if STAR_RATING.search(s):
            out.append(s[:240])
    return out


def find_point_prices(body_md: str) -> list[str]:
    """Point prices in the structured places a reader and an AI lift them from:
    table price columns, "Street price: $X" labels, "(about $X)" tags and
    "— $X" pick-heading suffixes. Price bands and per-serving costs pass."""
    hits: list[str] = []
    lines = (body_md or "").split("\n")
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.strip().startswith("|") and i + 1 < len(lines) and re.match(r"^\s*\|\s*:?-{2,}", lines[i + 1]):
            header = [c.strip() for c in ln.strip().strip("|").split("|")]
            cols = [k for k, h in enumerate(header)
                    if _TABLE_PRICE_HEADER.search(h) and not _TABLE_NOT_POINT.search(h)]
            j = i + 2
            while j < len(lines) and lines[j].strip().startswith("|"):
                cells = [c.strip() for c in lines[j].strip().strip("|").split("|")]
                for k in cols:
                    if k < len(cells) and re.search(_PRICE, cells[k]) and not _BAND.search(cells[k]):
                        hits.append(lines[j].strip()[:200])
                        break
                j += 1
            i = j
            continue
        if _POINT_PRICE_LABEL.search(ln) or _HEADING_PRICE.search(ln):
            hits.append(ln.strip()[:200])
        else:
            m = _PAREN_PRICE.search(ln) or _ANCHOR_PRICE.search(ln)
            if m:
                hits.append(ln[max(0, m.start() - 80):m.end() + 20].strip())
        i += 1
    return hits


# ── Stock / availability claims ─────────────────────────────────────────────
# Stock is a render-time fact: a body that says "every pick below is an
# in-stock listing" is false the day a listing sells out (2026-10-01 audit:
# 5 picks of two kitchen guides were in_stock=false while the copy said so).
# Forecasts ("can sell through in November") are unsourced predictions.
STOCK_CLAIM = re.compile(r"""(
    \bin[- ]stock\b
  | \b(?:currently|still|now)\s+(?:available|shipping|in\s+stock)\b
  | \bavailable\s+(?:now|today|right\s+now)\b
  | \b(?:sells?|sold|selling)\s+(?:out|through)\b
  | \bsell-?outs?\b
  | \bout[- ]of[- ]stock\b
  | \b(?:low|limited)\s+stock\b
  | \bonly\s+\d+\s+left\b
  | \bback\s+in\s+stock\b
)""", re.I | re.X)
# Telling the reader to CHECK stock is honest, as is a renderer note.
_STOCK_OK = re.compile(
    r"\b(?:check|verify|confirm|whether|varies|vary|live\s+(?:price|button|listing)|"
    r"at\s+the\s+time\s+of\s+(?:writing|selection))\b", re.I)


def find_stock_claims(text: str) -> list[str]:
    """Sentences asserting current stock / availability or forecasting a
    sell-out. "Check stock on the live listing" style sentences pass."""
    out = []
    for s in _sentences(text):
        if STOCK_CLAIM.search(s) and not _STOCK_OK.search(s):
            out.append(s[:240])
    return out


# ── Known-stale claims ──────────────────────────────────────────────────────
# Claims an audit proved wrong that writers keep reproducing. Each entry:
# id, pattern (regex, one sentence), unless (regex; the sentence is the
# corrected form), why (shown in the re-queue addendum). Config key
# `stale_claims` (a list of the same shape) replaces this default.
DEFAULT_STALE_CLAIMS: list[dict] = [
    {
        "id": "october-purchases-january-returns",
        # One sentence naming an October sale, a return and January, any order.
        "pattern": (r"^(?=.*(?:\boctober\b|prime\s+big\s+deal\s+days|\bfall\s+prime\b|"
                    r"\bprime\s+day\b))(?=.*\breturn(?:s|ed|able|ing)?\b)"
                    r"(?=.*\bjan(?:uary|\.)?\b)"),
        "unless": (r"\b(?:standard|30-day|30\s+days|not\s+(?:covered|eligible|extended)|"
                   r"does\s*n[o']t|isn't|is\s+not|aren't|are\s+not|only\s+(?:covers?|applies)|"
                   r"nov(?:ember|\.)?\s+1)\b"),
        "why": ("Amazon's extended holiday return window has covered purchases made "
                "Nov 1-Dec 31 (returnable until Jan 31); October / Prime Big Deal Days "
                "orders get the standard window (usually 30 days)."),
    },
]


def find_stale_claims(text: str, claims: list[dict] | None = None) -> list[str]:
    """Sentences matching a known-stale claim; each hit is "<id>: <sentence>"."""
    compiled = []
    for c in (claims if claims is not None else DEFAULT_STALE_CLAIMS):
        try:
            compiled.append((str(c.get("id") or "stale"), re.compile(c["pattern"], re.I),
                             re.compile(c["unless"], re.I) if c.get("unless") else None))
        except (KeyError, re.error, TypeError):
            continue
    out = []
    for s in _sentences(text):
        for cid, pat, unless in compiled:
            if pat.search(s) and not (unless and unless.search(s)):
                out.append(f"{cid}: {s[:220]}")
                break
    return out


# ── Numeric self-consistency ────────────────────────────────────────────────
_MONEY = re.compile(r"\$\s?(\d[\d,]*(?:\.\d{1,2})?)")
# "$700 cheaper", "$400 more than", "extra $200", "saves $50", "a $50 gap".
_DELTA = re.compile(
    r"(?:\b(?:extra|additional|another|saves?|saving|savings\s+of|premium\s+of|"
    r"gap\s+of|difference\s+of|step[- ]up\s+of|just|only)\s+(?:about\s+|roughly\s+|~)?"
    r"\$\s?(?P<a>\d[\d,]*(?:\.\d{1,2})?)\+?(?!\s*(?:/|per\b|a\s+month|a\s+year|each))"
    r"|\$\s?(?P<b>\d[\d,]*(?:\.\d{1,2})?)\+?\s+(?:cheaper|less\s+than|more\s+than|more\b|"
    r"extra\b|over\s+the|above\s+the|below\s+the|under\s+the|premium\b|gap\b|difference\b|"
    r"step[- ]up\b|savings?\b))", re.I)
_UNIVERSAL_RATIO = re.compile(
    r"\b\d+(?:\.\d+)?\s*[×x]\s+(?:the\s+|faster\s+than\s+|as\s+fast\s+as\s+)?[^.!?\n]{0,50}?"
    r"\b(?:at|across|on|in)\s+(?:every|all|each|any)\b", re.I)


def _money(v: str) -> float:
    try:
        return float(v.replace(",", ""))
    except ValueError:
        return 0.0


def find_numeric_inconsistencies(text: str) -> list[str]:
    """Two narrow, precision-first checks (warn mode by default):

    * a stated dollar delta ("$200 more", "extra $200", "$700 cheaper") that
      equals no gap between two prices stated in the same body (±$5 / 3%).
      SETUP 2026-09: anchors $1,899.99 vs $1,199.99 ($700 gap, $300 vs the
      $1,499.99 listing) next to "spend the extra $200".
    * a universal ratio claim ("about 3× the 4060 Ti at every context
      length"): one ratio rarely holds at every point the article cites
      (the page's own figures gave 3.02× at 4K and 2.26× at 128K).
    Deltas under $10 are ignored (cost per serving, shipping)."""
    body = text or ""
    # Anchor prices = every dollar figure that is not itself a stated delta.
    delta_spans = [m.span("a") if m.group("a") else m.span("b") for m in _DELTA.finditer(body)]
    prices = sorted({_money(m.group(1)) for m in _MONEY.finditer(body)
                     if not any(a <= m.start(1) < b for a, b in delta_spans)} - {0.0})
    gaps = {round(abs(a - b), 2) for i, a in enumerate(prices) for b in prices[i + 1:]}
    out: list[str] = []
    for s in _sentences(body):
        if len(prices) >= 2:
            for m in _DELTA.finditer(s):
                d = _money(m.group("a") or m.group("b") or "")
                if d < 10:
                    continue
                tol = max(5.0, 0.03 * d)
                if not any(abs(d - g) <= tol for g in gaps):
                    out.append(f"${d:,.0f} delta matches no gap between the body's prices: {s[:200]}")
                    break
        if _UNIVERSAL_RATIO.search(s):
            out.append(f"universal ratio claim (check it at each cited figure): {s[:200]}")
    return out


# ── Price-role consistency against live prices ──────────────────────────────
_LINK_PATH = re.compile(r"\]\((?:https?://[^/)\s]+)?(/[^)\s?#]+)")
_HEADING = re.compile(r"^(#{2,4})\s+(.*)$")
_ROLE_BUDGET = re.compile(r"\bbudget(?:\s+pick)?\b", re.I)
_ROLE_ANY = re.compile(r"\b(?:best\s+(?:overall|value|premium|performance|for\s+\w+)|"
                       r"budget(?:\s+pick)?|premium\s+pick|runner[- ]up|upgrade\s+pick)\b", re.I)
_SUPERLATIVE = re.compile(
    r"\b(?P<w>priciest|most\s+expensive|highest[- ]priced|cheapest|least\s+expensive|"
    r"lowest[- ]priced|most\s+affordable)\b[^.!?\n]{0,40}?\b(?:on|in|of)\s+(?:this|the|our)\s+"
    r"(?:list|roundup|guide|lineup|picks|shortlist|page)\b", re.I)
_MSRP_CLAIM = re.compile(
    r"\b(?P<dir>above|over|higher\s+than|below|under|less\s+than|beneath)\s+(?:its|the|a|their)\s+"
    r"(?:\w+\s+){0,2}?\$\s?(?P<n>\d[\d,]*(?:\.\d{1,2})?)\s+(?:launch\s+)?(?:MSRP|list\s+price|launch\s+price)\b",
    re.I)


def _norm_path(path: str) -> str:
    return (path or "").strip().rstrip("/").lower()


def _sections(body_md: str) -> list[tuple[str, str]]:
    """(heading, text) per ##/###/#### section; text excludes the heading."""
    out: list[tuple[str, str]] = []
    head, buf = "", []
    for ln in (body_md or "").split("\n"):
        m = _HEADING.match(ln)
        if m:
            out.append((head, "\n".join(buf)))
            head, buf = m.group(2).strip(), []
        else:
            buf.append(ln)
    out.append((head, "\n".join(buf)))
    return out


def _first_priced(text: str, prices: dict[str, float]) -> tuple[str, float] | None:
    for m in _LINK_PATH.finditer(text or ""):
        key = _norm_path(m.group(1))
        if key in prices:
            return key, prices[key]
    return None


def find_price_conflicts(body_md: str, prices: dict[str, float] | None) -> list[str]:
    """Pick roles and price words the live prices contradict.

    `prices` maps a link path the body uses ("/product/B0DKFMSMYK",
    "/kitchen/<slug>") to that product's CURRENT price, from the site's
    freshest valid listing (the caller applies its pricing-integrity
    filters; the guard never queries a DB). Checks:
      * a "Budget" pick section/heading whose product costs more than another
        role pick's product (>2%);
      * "priciest / cheapest … on this list" for a product that is not the
        max / min of the priced products the body links;
      * "above / below its $N MSRP" when the product linked in the same
        sentence or paragraph is priced on the other side of $N now."""
    if not prices:
        return []
    pm = {_norm_path(k): float(v) for k, v in prices.items() if v is not None and float(v) > 0}
    if not pm:
        return []
    out: list[str] = []
    roles: list[tuple[str, str, float]] = []   # (heading, key, price)
    for head, text in _sections(body_md):
        if head and _ROLE_ANY.search(head):
            hit = _first_priced(head, pm) or _first_priced(text, pm)
            if hit:
                roles.append((head, hit[0], hit[1]))
    # Comparison-table rows that carry a role or tier word and a priced link.
    for ln in (body_md or "").split("\n"):
        t = ln.strip()
        if t.startswith("|") and not re.match(r"^\|[\s:|-]+\|?$", t) and _ROLE_ANY.search(t):
            hit = _first_priced(t, pm)
            if hit:
                roles.append((t, hit[0], hit[1]))
    budget = [r for r in roles if _ROLE_BUDGET.search(r[0])]
    others = [r for r in roles if not _ROLE_BUDGET.search(r[0])]
    for bh, bk, bp in budget:
        cheaper = [o for o in others if o[1] != bk and o[2] * 1.02 < bp]
        if cheaper:
            o = min(cheaper, key=lambda r: r[2])
            out.append(f"budget pick {bk} (${bp:,.2f}) costs more than {o[0][:50]!r} "
                       f"{o[1]} (${o[2]:,.2f})")
    linked = {k: pm[k] for k in (_norm_path(m.group(1)) for m in _LINK_PATH.finditer(body_md or ""))
              if k in pm}
    if len(linked) >= 2:
        hi, lo = max(linked.values()), min(linked.values())
        for head, text in _sections(body_md):
            sec_hit = _first_priced(head, pm) or _first_priced(text, pm)
            for s in _sentences(text):
                m = _SUPERLATIVE.search(s)
                if not m:
                    continue
                hit = _first_priced(s, pm) or sec_hit
                if not hit:
                    continue
                w = m.group("w").lower()
                top = w.startswith(("priciest", "most exp", "highest"))
                if (top and hit[1] < hi * 0.98) or (not top and hit[1] > lo * 1.02):
                    out.append(f"{w!r} but {hit[0]} is ${hit[1]:,.2f} (range "
                               f"${lo:,.2f}-${hi:,.2f}): {s[:160]}")
    # MSRP claims bind to a product linked in the same sentence or paragraph
    # only — a section can discuss several products.
    for para in (body_md or "").split("\n"):
        para_hit = _first_priced(para, pm)
        for s in _sentences(para):
            for m in _MSRP_CLAIM.finditer(s):
                hit = _first_priced(s, pm) or para_hit
                if not hit:
                    continue
                n = _money(m.group("n"))
                above = m.group("dir").lower() in ("above", "over", "higher than")
                if (above and hit[1] < n) or (not above and hit[1] > n):
                    out.append(f"says {m.group('dir').lower()} the ${n:,.0f} MSRP but {hit[0]} "
                               f"is ${hit[1]:,.2f} now: {s[:160]}")
    return out


@dataclasses.dataclass
class ClaimsAudit:
    first_hand: list[str]
    star_ratings: list[str]
    point_prices: list[str]
    policy: dict
    meta_first_hand: list[str] = dataclasses.field(default_factory=list)
    stock_claims: list[str] = dataclasses.field(default_factory=list)
    stale_claims: list[str] = dataclasses.field(default_factory=list)
    numeric: list[str] = dataclasses.field(default_factory=list)
    price_conflicts: list[str] = dataclasses.field(default_factory=list)

    def _findings(self) -> list[tuple[str, str, list[str]]]:
        """(policy key, label, hits) for every check."""
        return [
            ("first_hand_claims", "first-hand testing claims", self.first_hand + self.meta_first_hand),
            ("star_ratings", "copied star ratings / review counts", self.star_ratings),
            ("point_prices", "product point prices", self.point_prices),
            ("stock_claims", "stock / availability assertions", self.stock_claims),
            ("stale_claims", "known-stale claims", self.stale_claims),
            ("numeric_consistency", "numbers that contradict the body's own figures", self.numeric),
            ("price_consistency", "pick roles / price words the live prices contradict",
             self.price_conflicts),
        ]

    def _rejecting(self) -> list[tuple[str, list[str]]]:
        return [(label, hits) for key, label, hits in self._findings()
                if hits and self.policy.get(key) == "reject"]

    @property
    def passes(self) -> bool:
        return not self._rejecting()

    def failure_reason(self, max_examples: int = 4) -> str:
        parts = []
        for label, items in self._rejecting():
            ex = "; ".join(repr(x[:120]) for x in items[:max_examples])
            parts.append(f"{len(items)} {label} (e.g. {ex})")
        advice = (
            "Unless the proposal carries real test data, rewrite as a "
            "'How we picked' account of specs, cited third-party tests and what "
            "owners report; use dollar-defined price tiers ('under $50', "
            "'premium ($300+)') and let the live price button carry the price; drop "
            "star ratings and review counts; never assert current stock")
        stale = [c for c in (self.policy.get("stale_claims_list") or DEFAULT_STALE_CLAIMS)
                 if any(h.startswith(f"{c.get('id')}:") for h in self.stale_claims)]
        if stale:
            advice += "; correct the stale claim(s): " + " ".join(str(c.get("why") or "") for c in stale)
        return ("CLAIMS GUARD: " + " | ".join(parts) + ". " + advice + ".") if parts else ""

    def warnings(self) -> list[str]:
        return [f"{len(hits)} {label}" for key, label, hits in self._findings()
                if hits and self.policy.get(key) == "warn"]


# Checks added after the config file was first written. A config that does
# not name the key gets this mode; stale claims are clearly wrong by
# construction (each entry is a claim an audit disproved), the rest start in
# warn mode to measure false positives before anything is refused.
NEW_CHECK_DEFAULTS: dict = {
    "stock_claims": "warn",
    "stale_claims": "reject",
    "numeric_consistency": "warn",
    "price_consistency": "warn",
}


def load_config(storage=None) -> dict:
    """The guard's config: the storage copy at `config/article-claims-guard-
    config.json` when a storage backend is given and holds one, else the repo
    default. Callers that check many rows load it once and pass `config=`."""
    if storage is not None:
        try:
            cfg = storage.read_json(_CONFIG_KEY) or None
        except Exception:
            cfg = None
        if cfg:
            return cfg
    try:
        return _json.loads(_REPO_CONFIG.read_text())
    except Exception as e:  # pragma: no cover - config shipped with the repo
        _sys.stderr.write(f"[claims-guard] config unreadable ({e}); guard in warn mode\n")
        return {"default": {"first_hand_claims": "warn", "star_ratings": "warn", "point_prices": "off"}}


def resolve_policy(site_hint: str = "", bucket: str = "", storage=None, config: dict | None = None) -> dict:
    """The site's policy with `point_prices` forced to "off" unless `bucket` is
    one of the site's `price_buckets`. `config` (from load_config) wins over
    `storage`; with neither, the repo default applies."""
    cfg = config if config is not None else load_config(storage)
    vals = dict(NEW_CHECK_DEFAULTS)
    vals.update(cfg.get("default") or {})
    hint = (site_hint or "").lower()
    for key, over in (cfg.get("sites") or {}).items():
        if key.lower() in hint:
            vals.update(over or {})
            break
    if isinstance(cfg.get("stale_claims"), list):
        vals["stale_claims_list"] = cfg["stale_claims"]
    buckets = [b.lower() for b in (vals.get("price_buckets") or [])]
    if (bucket or "").lower() not in buckets:
        vals["point_prices"] = "off"
    return vals


def check(body_md: str, *, subtitle: str = "", excerpt: str = "", bucket: str = "",
          site_hint: str = "", policy: dict | None = None, storage=None,
          prices: dict[str, float] | None = None) -> ClaimsAudit:
    """Audit one article. `prices` (optional) maps the body's link paths to
    current prices for the price_consistency check; without it that check
    finds nothing."""
    pol = policy if policy is not None else resolve_policy(site_hint, bucket, storage)
    meta = " ".join(x for x in (subtitle, excerpt) if x)

    def _on(key: str) -> bool:
        return pol.get(key, "off") != "off"

    return ClaimsAudit(
        first_hand=find_first_hand_claims(body_md) if pol.get("first_hand_claims", "off") != "off" else [],
        star_ratings=(find_star_ratings(body_md) + (find_star_ratings(meta) if meta else []))
        if pol.get("star_ratings", "off") != "off" else [],
        point_prices=find_point_prices(body_md) if pol.get("point_prices", "off") != "off" else [],
        policy=pol,
        meta_first_hand=find_first_hand_claims(meta) if meta and pol.get("first_hand_claims", "off") != "off" else [],
        stock_claims=(find_stock_claims(body_md) + (find_stock_claims(meta) if meta else []))
        if _on("stock_claims") else [],
        stale_claims=(find_stale_claims(body_md, pol.get("stale_claims_list"))
                      + (find_stale_claims(meta, pol.get("stale_claims_list")) if meta else []))
        if _on("stale_claims") else [],
        numeric=find_numeric_inconsistencies(body_md) if _on("numeric_consistency") else [],
        price_conflicts=find_price_conflicts(body_md, prices) if _on("price_consistency") else [],
    )
