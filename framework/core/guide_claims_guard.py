"""Claims guard for data-backed guides: every number from the facts pack,
no price talk, no banned topic, no off-site or Amazon links, placeholders
that the site can render.

Why: a data site's guide is only worth publishing if every runtime, wattage
and watt-hour figure in it is the site's own sourced number. The author gets
those numbers as a facts pack (``framework.core.facts_pack``); this guard
checks the prose against the SAME pack after the model returns, so a number
the model invented, rounded into a promise or remembered from a forum never
reaches the INSERT. It also refuses the wording an Amazon Associate may not
use (Participation Requirements 6(y): price history, price drops, "lowest
ever", "since <date>"), point prices outside a budget label, topics the
site's safety boundary bans outright (config), Amazon URLs and image hosts,
links to other sites, raw HTML, inline images, and (when the site says so)
hand-written markdown tables: tables come from render-time placeholders.

It DETECTS; it never rewrites prose (editorial prose is Opus-only). The
caller re-prompts once with :meth:`ClaimsAudit.addendum`, then holds.

Policy is data (the site's YAML ``claims`` block, see :func:`parse_policy`);
the engine-level defaults below hold only generic English and Amazon-policy
wording. Niche words (a site's banned topics) are always config.

Numbers. A number followed by a checked unit (W, kW, Wh, kWh, h / hours,
and V / A when configured) must equal a facts-pack value in the same base
unit **within rounding**: the pack value rounded to the decimals the prose
wrote ("8.5 hours" for 8.46; "2.1 kWh" for 2,132 Wh), or within the
configured tolerance. A range ("8 to 12 hours", "75-100 W") checks both ends.
Product and model names from the pack are blanked first, so "Explorer 1000
Plus" is never read as 1000 of anything. Spelled-out numbers ("two hundred
forty volts", "three full days") are read as figures; days and weeks are
checked as hours; temperatures (C, F converted) are checked when the site
lists C as a unit.

Whose number. A sentence that names a model (whole-word match, longest name
first, so "DELTA 3 Max Plus" never borrows DELTA 3's figures) may use only
that model's numbers plus the numbers of rows about no model. A sentence that
names no model inherits the subject of the previous sentence in the same
paragraph ("The Yeti 500 is the smallest. It runs ..."). A name followed by a
model suffix the pack does not know ("DELTA 3 Max") is an unknown subject:
only subject-free numbers pass. Hours are stricter: an hour figure about no
model must be a subject-free value (a load preset, a tier goal), and the
question's own parameters (``scenario``) and ``always_allowed`` hours pass
only when written as the scenario ("a 24-hour outage", "an outage of 72
hours"), never as a runtime ("runs it for 72 hours").

Row rules (optional, config): ``verdicts`` (a sentence that names both sides
of a pair whose verdict is not a fit may not call it a fit) and ``statuses``
(a certification sentence must match the row's status and date).
"""
from __future__ import annotations

import dataclasses
import re
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import urlparse

from framework.core.facts_pack import AMAZON_RE, BASE_UNITS, NumericFacts, canonical_unit

# ---------------------------------------------------------------------------
# Generic defaults (Amazon policy wording and price talk; no niche words)
# ---------------------------------------------------------------------------

_MONTHS = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
DEFAULT_BANNED: tuple[dict, ...] = (
    {"id": "price-history", "pattern": r"\bprice[- ]?(?:history|histories|charts?|trackers?|tracking|alerts?|drops?|"
                                       r"dropped|cuts?|watch|changes?|increases?|hikes?)\b|\b(?:price|prices) (?:has|have) "
                                       r"(?:dropped|fallen|risen|gone (?:up|down))",
     "why": "Price history, price changes and price alerts are banned (Associates Participation 6(y))."},
    # 'lowest' is a price claim only next to price words: "the lowest charging
    # temperature" and "the lowest capacity ceiling" are specs (first live
    # drafts, 2026-11-22); "record-low temperatures" are weather.
    {"id": "lowest-ever", "pattern": r"\blowest(?:[- ]ever\b|[- ]priced?\b|\s+(?:\w+\s+){0,2}(?:price|prices|pricing|cost|costs)\b)"
                                     r"|\blowest\s+(?:it|they|this|that)\s+(?:has|have|had|is|was|'s|\u2019s)\b|\bat\s+(?:its|their)\s+lowest\b"
                                     r"|\blowest\s+(?:yet|so\s+far|this\s+(?:year|month|week|season))\b"
                                     r"|\b(?:all[- ]time[- ]low|cheapest[- ]ever|best[- ]price|never been cheaper|"
                                     r"rock[- ]bottom)\b|\brecord[- ]low\s+(?:price|prices|cost)\b",
     "why": "'Lowest' / 'all-time low' claims are price-history claims (6(y))."},
    {"id": "deal-talk", "pattern": r"\b(?:deals?|doorbusters?|on sale|sale prices?|discount(?:s|ed)?|markdowns?|"
                                   r"clearance|price cuts?|limited[- ]time)\b|\d\s?% off\b|\bsav(?:e|ings?)\s+(?:up to\s+)?\$|"
                                   r"\bsales?\b(?![- ]tax)",
     "why": "A guide never talks about deals, sales or discounts: prices render live, stamped, beside the text."},
    {"id": "since-date", "pattern": rf"\bsince\s+(?:{_MONTHS}(?:\s+\d{{1,2}})?|\d{{1,2}}/\d{{1,2}}|(?:19|20)\d\d|last\s+"
                                    r"(?:week|month|year|black friday|cyber monday|prime day)|black friday|"
                                    r"cyber monday|prime day)\b",
     "why": "'Since <date>' compares a price or availability over time (6(y))."},
    {"id": "was-now", "pattern": r"\bwas\s+\$\s?\d|\bnow\s+(?:only\s+|just\s+)?\$\s?\d|\b(?:list|msrp|retail)\s+(?:price\s+)?"
                                 r"(?:of\s+)?\$\s?\d",
     "why": "Was/now and list-price anchors are price comparisons."},
    {"id": "money-words", "pattern": r"\b(?:bucks|dollars?|cents|pennies|quid|usd)\b|\b\d[\d,.]*\s?(?:grand|k)\b(?!\s?(?:wh|w)\b)",
     "why": "A guide never states an amount of money, in any words: prices render live, stamped, beside the text."},
    {"id": "list-price", "pattern": r"\b\d+(?:\.\d+)?\s?(?:%|percent|per ?cent)\s+(?:off|below|under|less|cheaper|lower|savings?|"
                                    r"discount)|\b(?:list|retail|regular|original|sticker|full|normal|usual|typical|street) "
                                    r"prices?\b|\bmsrp\b|\bmarked (?:down|up)\b",
     "why": "Percent-off and list-price anchors are price comparisons (6(y))."},
    {"id": "price-compare", "pattern": r"\b(?:cheap|cheaper|cheapest|pricier|priciest|less expensive|more expensive|"
                                       r"costlier|bargains?|(?:best|great|good|better) value|value for (?:the )?money|"
                                       r"(?:wh|watt[- ]hours?|kwh) per dollar|bang for (?:the|your) buck)\b",
     "why": "Cheaper/pricier and value-for-money claims compare prices that change; the page ranks value live."},
    {"id": "promo-talk", "pattern": r"\b(?:coupons?|promo(?:tional)?(?: codes?)?|promotions?|vouchers?|rebates?|"
                                    r"cash ?back|checkout|price[- ]?match\w*|free shipping|subscribe (?:&|and) save|"
                                    r"lightning)\b",
     "why": "Coupons, promotions and checkout talk are deal talk."},
    {"id": "retail-event", "pattern": r"\b(?:black friday|cyber (?:monday|week)|prime (?:big deal )?days?|"
                                      r"prime early access|boxing day|singles'? day|(?:after|post)[- ]christmas)\b",
     "why": "Retail events are deal hooks; an occasion is carried by the holiday: tag the code sets, never by "
            "a shopping-event hook in the text."},
    {"id": "buy-urgency", "pattern": r"\b(?:time|week|day|moment|month) to (?:order|buy|shop)\b|\b(?:buy|order|grab) "
                                     r"(?:it |one |yours |them )?(?:now|today|early|soon|while)\b|\bshipping "
                                     r"(?:delays?|cut-?offs?|times?|deadlines?)\b|\bbefore (?:the )?(?:prices?|"
                                     r"stock|it sells|they sell)\b",
     "why": "Buy-now and wait-for-it timing is a price/availability claim the text cannot keep true."},
    {"id": "guarantee", "pattern": r"\bguarantee[sd]?\b|\bpromis(?:e|es|ed)\b|\bno matter (?:what|how)\b|"
                                   r"\b(?:any|every|all) (?:\w+ )?(?:storms?|outages?|blackouts?|emergenc(?:y|ies))\b|"
                                   r"\bthrough the (?:whole|entire|full) (?:\w+ )?(?:storm|outage|blackout|winter|night|"
                                   r"event)\b|\bnever (?:run out|lose power|go dark|fail|let you down)\b|"
                                   r"\b(?:worry|risk|trouble)[- ]free\b|\bpeace of mind\b|\b(?:keeps?|keeping|stay|"
                                   r"stays) (?:your |the |you )?(?:family|kids|children|household|home|loved ones|"
                                   r"everyone|you) (?:\w+ )?safe\b",
     "why": "A guide never guarantees an outcome or safety: runtimes are estimates with a range."},
    {"id": "stock-claim", "pattern": r"\b(?:in stock|out of stock|sells? out|selling fast|while supplies last|"
                                     r"back in stock)\b",
     "why": "Stock is a render-time fact the text cannot keep true."},
)
#: "$N" forms that are a budget LABEL, not a price ("under $500", "the $1,000 kit").
DEFAULT_PRICE_LABELS: tuple[str, ...] = (
    r"\b(?:under|below|less than|up to|at most)\s+\$(?P<n>\d{1,3}(?:,\d{3})*|\d+)(?![\d.,]*\d)",
    r"\$(?P<n>\d{1,3}(?:,\d{3})*|\d+)\s+(?:tier|kit|ladder|budget|level|rung|step|slot)s?\b",
)
#: A sentence that states what something costs: a budget amount in it is a price.
PRICE_VERB_RE = re.compile(r"\b(?:costs?|costing|priced|prices?|sells?|selling|sold|retails?|pay|paid|spend|"
                           r"for (?:only|just)|goes for|listed at|marked|msrp)\b", re.I)
_DOLLAR_N = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+|\d+)(?![\d.])")
MONEY_ANY = re.compile(r"[$€£¥₹]\s?\d|\b\d[\d,]*(?:\.\d+)?\s?(?:dollars|usd)\b", re.I)

UNIT_TOKEN = (r"(?:kWh|KWH|kwh|kilowatt[- ]hours?|Wh|WH|wh|watt[- ]hours?|kW|KW|kilowatts?|W|watts?|"
              r"hours?|hrs?|h|days?|weeks?|V|volts?|A|amps?)")
NUM = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_ADJ_WORDS = r"(?:full|whole|straight|solid|entire|more)"
_ADJ = rf"(?:{_ADJ_WORDS}\s+)?"
#: "3 full days", "24 whole hours": the figure is claimed as a minimum.
_STRICT_ADJ = re.compile(r"\b(?:full|whole|straight|solid|entire)\b", re.I)
RANGE_RE = re.compile(rf"(?<![\w.,/-])(?P<a>{NUM})\s*(?:-|–|—|to|and)\s*(?P<b>{NUM})\s*-?\s*{_ADJ}(?P<u>{UNIT_TOKEN})"
                      r"(?![A-Za-z0-9])")
SINGLE_RE = re.compile(rf"(?<![\w.,/-])(?P<n>{NUM})\s*-?\s*{_ADJ}(?P<u>{UNIT_TOKEN})(?![A-Za-z0-9])")
TEMP_RE = re.compile(rf"(?<![\w.,/])(?P<sign>[-−–]\s?|minus\s+)?(?P<n>{NUM})\s*(?:°\s*|degrees?\s+)?"
                     r"(?P<u>Celsius|Fahrenheit|C|F)(?![A-Za-z0-9])")
_UNIT_WORDS = {"kilowatt-hour": "kWh", "kilowatt-hours": "kWh", "kilowatt hour": "kWh", "kilowatt hours": "kWh",
               "watt-hour": "Wh", "watt-hours": "Wh", "watt hour": "Wh", "watt hours": "Wh",
               "kilowatt": "kW", "kilowatts": "kW", "watt": "W", "watts": "W", "kwh": "kWh", "wh": "Wh",
               "kw": "kW", "hour": "h", "hours": "h", "hr": "h", "hrs": "h", "volt": "V", "volts": "V",
               "amp": "A", "amps": "A", "day": "d", "days": "d", "week": "wk", "weeks": "wk"}

# Spelled-out numbers directly before a unit ("two hundred forty volts").
_NUM_WORDS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
    "seventeen eighteen nineteen".split())}
_NUM_WORDS.update({w: 10 * (i + 2) for i, w in enumerate("twenty thirty forty fifty sixty seventy eighty ninety".split())})
_NW = "|".join(sorted(list(_NUM_WORDS) + ["hundred", "thousand", "dozen"], key=len, reverse=True))
_NW_SEQ = re.compile(rf"\b(?:a\s+)?(?:{_NW})(?:(?:\s+and\s+|[\s-]+)(?:{_NW}))*\b", re.I)
_UNIT_AFTER = re.compile(rf"^\s*-?\s*{_ADJ}(?:{UNIT_TOKEN}|degrees?|°)(?![A-Za-z0-9])", re.I)
#: A model name followed by one of these is a DIFFERENT model the pack does not name.
_MODEL_SUFFIX = re.compile(r"^(?:\s*\+|\s+(?:max|plus|pro|ultra|mini|lite|air|se|gen\s*\d+|v\d+|mk\s*\d+)(?![\w]))",
                           re.I)
#: Words around a figure that make it the question's scenario, not a runtime.
_SCEN_AFTER = re.compile(r"^[\s,]*(?:(?:of|without|with no)\s+(?:an?\s+|the\s+)?)?(?:power\s+)?(?:outages?|storms?|"
                         r"blackouts?|cuts?|nights?|windows?|periods?|goals?|targets?|power\b)", re.I)
_SCEN_BEFORE = re.compile(r"(?:outages?|storms?|blackouts?|goals?|targets?)\s+(?:of|lasting|for)\s+(?:about\s+)?$", re.I)

PLACEHOLDER_RE = re.compile(r"\{\{([^{}\n]{0,200})\}\}")
MD_LINK_RE = re.compile(r"(!?)\[([^\]]*)\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
BARE_URL_RE = re.compile(r"(?<![(\w])(?:https?://|www\.)[^\s)>\]]+", re.I)
RAW_HTML_RE = re.compile(r"<\s*(?:a|img|script|iframe|style|object|embed|form|link|meta|picture|source|video)\b", re.I)
TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$", re.M)


class ClaimsConfigError(ValueError):
    """The claims block does not compile. Callers fail closed."""


@dataclasses.dataclass(frozen=True)
class Pattern:
    id: str
    rx: re.Pattern
    why: str = ""
    fix: str = ""


@dataclasses.dataclass(frozen=True)
class VerdictRule:
    """A pair row (``rows`` query) whose ``verdict`` is not in ``positive`` (or
    whose ``flags`` column is non-empty) may not be called a fit in a sentence
    that names both ``subjects``."""
    rows: str
    subjects: tuple[str, str]
    verdict: str
    positive: tuple[str, ...]
    flags: str = ""
    claim_rx: re.Pattern = None  # type: ignore[assignment]
    negative_flags: tuple[str, ...] = ()      # empty = any flag makes the row a non-fit

    def is_fit(self, row: Mapping[str, Any]) -> bool:
        if str(row.get(self.verdict)) not in self.positive:
            return False
        fl = row.get(self.flags) if self.flags else None
        if not fl:
            return True
        fl = [str(x) for x in (fl if isinstance(fl, (list, tuple)) else [fl])]
        return not (set(fl) & set(self.negative_flags)) if self.negative_flags else False


@dataclasses.dataclass(frozen=True)
class StatusRule:
    """A sentence that names a subject, a ``key`` value of the ``rows`` query
    (e.g. a standard) and a claim word asserts the row's status: the subject
    needs a row with that key whose ``status`` is in ``positive``, and a date
    written in the sentence must be the row's ``date``."""
    rows: str
    subject: str
    key: str
    status: str
    positive: tuple[str, ...]
    date: str = ""
    claim_rx: re.Pattern = None  # type: ignore[assignment]


DEFAULT_FIT_CLAIM = (r"\b(?:fits?|fitting|compatible|works? (?:fine |well |safely )?with|safe to (?:use|connect|pair)|"
                     r"within (?:the |its )?(?:limits?|input|range|rating)|ok(?:ay)? to (?:use|connect|pair)|"
                     r"pairs? (?:well|fine|safely)|good match|handles? (?:it|them|that)|takes? (?:it|them|that))\b")
DEFAULT_STATUS_CLAIM = (r"\b(?:listing|listed|certified|certification|certificate|verified|carries|holds|passed|"
                        r"meets|complies|compliant|approved)\b")
_NEGATION = re.compile(r"\b(?:not|never|no|cannot|nor|neither|without|fails?|failed|exceeds?|over|above|too|"
                       r"unlisted)\b|n't\b", re.I)
_CLAUSE_BREAK = re.compile(r"[,;:()]|\b(?:but|yet|however|although|though|still|while|whereas|and so|so)\b", re.I)


@dataclasses.dataclass(frozen=True)
class ClaimsPolicy:
    units: tuple[str, ...] = ("W", "Wh", "h")          # base units checked against the facts pack
    rel_tol: float = 0.005
    abs_tol: Mapping[str, float] = dataclasses.field(default_factory=lambda: {"W": 0.5, "Wh": 0.5, "h": 0.05,
                                                                              "V": 0.05, "A": 0.05, "C": 0.05})
    always_allowed: Mapping[str, tuple[float, ...]] = dataclasses.field(default_factory=dict)
    banned: tuple[Pattern, ...] = ()
    topic_bans: tuple[Pattern, ...] = ()
    price_labels: tuple[re.Pattern, ...] = ()
    budget_labels: tuple[float, ...] = ()               # allowed N in a price label; empty = any
    budget_label_anywhere: bool = False                 # "$N" with N a budget label passes outside a price sentence
    own_hosts: frozenset = frozenset()
    external_hosts_allowed: tuple[str, ...] = ()
    allow_markdown_tables: bool = True
    allow_images: bool = False
    verdicts: tuple[VerdictRule, ...] = ()
    statuses: tuple[StatusRule, ...] = ()
    contexts: tuple["ContextRule", ...] = ()


@dataclasses.dataclass(frozen=True)
class ContextRule:
    """In a sentence matching ``rx``, a figure in base unit ``unit`` must come
    from one of ``columns`` (e.g. a temperature in a sentence about charging
    must be a charging limit, not a discharging one)."""
    rx: re.Pattern
    unit: str
    columns: tuple[str, ...]


def _compile(p: str, where: str) -> re.Pattern:
    try:
        return re.compile(p, re.I)
    except re.error as e:
        raise ClaimsConfigError(f"{where}: invalid regex: {e}") from None


def _patterns(items: Iterable[Any], where: str) -> tuple[Pattern, ...]:
    out = []
    for i, it in enumerate(items or ()):
        if not isinstance(it, Mapping) or not it.get("id") or not it.get("pattern"):
            raise ClaimsConfigError(f"{where}[{i}]: needs id and pattern")
        out.append(Pattern(str(it["id"]), _compile(str(it["pattern"]), f"{where}[{i}].pattern"),
                           str(it.get("why") or ""), str(it.get("fix") or "")))
    return tuple(out)


def _rules(items: Any, where: str, kind: str) -> tuple:
    out = []
    for i, it in enumerate(items or ()):
        w = f"{where}[{i}]"
        if not isinstance(it, Mapping):
            raise ClaimsConfigError(f"{w}: expected an object")
        rows = str(it.get("rows") or "")
        if not rows:
            raise ClaimsConfigError(f"{w}.rows: the facts query whose rows the rule reads")
        pos = tuple(str(x) for x in (it.get("positive") or ()))
        if not pos:
            raise ClaimsConfigError(f"{w}.positive: the values that make the claim true")
        if kind == "verdict":
            subj = tuple(str(x) for x in (it.get("subjects") or ()))
            if len(subj) != 2 or not it.get("verdict"):
                raise ClaimsConfigError(f"{w}: needs subjects (two columns) and verdict")
            out.append(VerdictRule(rows, (subj[0], subj[1]), str(it["verdict"]), pos, str(it.get("flags") or ""),
                                   _compile(str(it.get("claim_pattern") or DEFAULT_FIT_CLAIM), f"{w}.claim_pattern"),
                                   tuple(str(x) for x in (it.get("negative_flags") or ()))))
        else:
            if not it.get("subject") or not it.get("key") or not it.get("status"):
                raise ClaimsConfigError(f"{w}: needs subject, key and status")
            out.append(StatusRule(rows, str(it["subject"]), str(it["key"]), str(it["status"]), pos,
                                  str(it.get("date") or ""),
                                  _compile(str(it.get("claim_pattern") or DEFAULT_STATUS_CLAIM), f"{w}.claim_pattern")))
    return tuple(out)


def parse_policy(block: Optional[Mapping[str, Any]], *, own_hosts: Iterable[str] = ()) -> ClaimsPolicy:
    """A ClaimsPolicy from a site's ``claims`` block::

        claims:
          units: [W, Wh, h, V, A, C]
          rel_tol: 0.005
          always_allowed: {h: [24], V: [120]}
          banned_extra: [{id, pattern, why}]       # added to the generic defaults
          topic_bans: [{id, pattern, why, fix}]    # the site's safety boundary, any mention
          budget_labels: [300, 500, 1000]          # "$N" allowed only as "under $N" / "$N kit"
          budget_label_anywhere: false             # true: "$N" (N a budget label) also passes in a sentence
                                                   # that states no price ("kits at $500 and $1,000")
          external_hosts_allowed: [energystar.gov]
          allow_markdown_tables: false
          verdicts:                                # pair rows: a non-fit is never called a fit
            - {rows: compat, subjects: [station, other], verdict: verdict,
               positive: [fits, fits_in_series], flags: flags, negative_flags: [computed-check-unknown]}
          statuses:                                # dated status rows (certifications)
            - {rows: certifications, subject: model_name, key: standard, status: status,
               positive: [verified], date: verified_at}
          contexts:                                # what a figure measures, by sentence wording
            - {pattern: '\\bcharg', unit: C, columns: [charge_temp_min_c]}
    """
    b = dict(block or {})
    units = []
    for u in b.get("units") or ("W", "Wh", "h"):
        base = BASE_UNITS[canonical_unit(u)][0]
        if base not in units:
            units.append(base)
    always = {}
    for u, vals in (b.get("always_allowed") or {}).items():
        base, factor = BASE_UNITS[canonical_unit(u)]
        always.setdefault(base, [])
        always[base].extend(float(v) * factor for v in (vals or ()))
    tol = {"W": 0.5, "Wh": 0.5, "h": 0.05, "V": 0.05, "A": 0.05, "C": 0.05}
    tol.update({BASE_UNITS[canonical_unit(k)][0]: float(v) for k, v in (b.get("abs_tol") or {}).items()})
    return ClaimsPolicy(
        units=tuple(units), rel_tol=float(b.get("rel_tol", 0.005)), abs_tol=tol,
        always_allowed={k: tuple(v) for k, v in always.items()},
        banned=_patterns(DEFAULT_BANNED, "defaults") + _patterns(b.get("banned_extra") or (), "claims.banned_extra"),
        topic_bans=_patterns(b.get("topic_bans") or (), "claims.topic_bans"),
        price_labels=tuple(_compile(p, "claims.price_labels") for p in (b.get("price_labels") or DEFAULT_PRICE_LABELS)),
        budget_labels=tuple(float(x) for x in (b.get("budget_labels") or ())),
        budget_label_anywhere=bool(b.get("budget_label_anywhere", False)),
        own_hosts=frozenset(h.lower() for h in own_hosts if h),
        external_hosts_allowed=tuple(str(h).lower().lstrip(".") for h in (b.get("external_hosts_allowed") or ())),
        allow_markdown_tables=bool(b.get("allow_markdown_tables", True)),
        allow_images=bool(b.get("allow_images", False)),
        verdicts=_rules(b.get("verdicts"), "claims.verdicts", "verdict"),
        statuses=_rules(b.get("statuses"), "claims.statuses", "status"),
        contexts=_contexts(b.get("contexts")))


def _contexts(items: Any) -> tuple[ContextRule, ...]:
    out = []
    for i, it in enumerate(items or ()):
        w = f"claims.contexts[{i}]"
        if not isinstance(it, Mapping) or not it.get("pattern") or not it.get("unit") or not it.get("columns"):
            raise ClaimsConfigError(f"{w}: needs pattern, unit and columns")
        out.append(ContextRule(_compile(str(it["pattern"]), f"{w}.pattern"), BASE_UNITS[canonical_unit(it["unit"])][0],
                               tuple(str(c) for c in it["columns"])))
    return tuple(out)


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class ClaimViolation:
    rule: str
    field: str
    excerpt: str
    why: str = ""
    fix: str = ""

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class ClaimsAudit:
    violations: list[ClaimViolation]
    numbers_checked: int = 0

    @property
    def passes(self) -> bool:
        return not self.violations

    def rules(self) -> list[str]:
        return sorted({v.rule for v in self.violations})

    def summary(self) -> str:
        if self.passes:
            return f"claims guard: pass ({self.numbers_checked} number(s) checked)"
        return "claims guard: " + ", ".join(f"{r}x{sum(1 for v in self.violations if v.rule == r)}" for r in self.rules())

    def addendum(self, max_items: int = 12) -> str:
        if self.passes:
            return ""
        lines = ["CLAIMS GUARD — fix every item below in place, change nothing else:"]
        for i, v in enumerate(self.violations[:max_items], 1):
            lines.append(f"  {i}. [{v.rule}] ({v.field}) {v.excerpt[:200]!r}" + (f" — {v.why}" if v.why else ""))
            if v.fix:
                lines.append(f"     Fix: {v.fix}")
        if len(self.violations) > max_items:
            lines.append(f"  ... and {len(self.violations) - max_items} more of the same kinds")
        lines.append("  Numbers: write only figures that appear in FACTS (same unit, or kWh/kW for Wh/W), rounded "
                     "no further than the decimals you write; never add, average or convert a figure yourself.")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {"passes": self.passes, "numbers_checked": self.numbers_checked,
                "violations": [v.to_dict() for v in self.violations]}


def _fields(title: str, subtitle: str, excerpt: str, body_md: str, faqs: Any) -> list[tuple[str, str]]:
    out = [("title", title or ""), ("subtitle", subtitle or ""), ("excerpt", excerpt or ""), ("body", body_md or "")]
    for i, f in enumerate(faqs or []):
        if isinstance(f, Mapping):
            out.append((f"faqs[{i}]", f"{f.get('question') or ''}\n{f.get('answer') or ''}"))
    return out


def _snippet(text: str, start: int, end: int, pad: int = 60) -> str:
    return " ".join(text[max(0, start - pad):min(len(text), end + pad)].split())


def _blank(m: re.Match) -> str:
    return " " * len(m.group(0))


def _strip_names(text: str, names: Iterable[str]) -> str:
    """Pack names blanked with spaces (same length, so positions hold)."""
    for n in sorted({n for n in names if n and len(n) >= 3}, key=len, reverse=True):
        text = re.sub(re.escape(n), _blank, text, flags=re.I)
    return text


def _plain(text: str) -> str:
    """Markdown links replaced by their anchor text (padded to the same
    length is not needed: every later step works on this plain text)."""
    return MD_LINK_RE.sub(lambda m: f" {m.group(2)} ", text or "")


def _mask(plain: str, names: Iterable[str]) -> str:
    """The plain text with everything that is not a claim blanked IN PLACE
    (placeholders, URLs, stray site paths, pack names), so a figure's offset
    is its offset in `plain`."""
    t = PLACEHOLDER_RE.sub(_blank, plain or "")
    t = BARE_URL_RE.sub(_blank, t)
    t = re.sub(r"/[a-z]+/[A-Za-z0-9-]+", _blank, t)      # stray site paths
    return _strip_names(t, names)


def _prose(text: str, names: Iterable[str]) -> str:
    """Text whose numbers are claims: placeholders, link targets, URLs and
    pack names removed (link anchor text kept)."""
    return _mask(_plain(text), names)


_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\[*_(\"'])|\n+")


def split_claim_sentences(text: str) -> list[str]:
    """Sentences (and lines) of `text`; a decimal point never splits."""
    return [x for x in _SENT_SPLIT.split(text or "") if x and x.strip()]


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def _decimals(s: str) -> int:
    return len(s.split(".", 1)[1]) if "." in s else 0


def _unit(raw: str) -> str:
    if raw in ("W", "V", "A", "h"):
        return raw
    return _UNIT_WORDS.get(raw.lower().replace("  ", " "), None) or canonical_unit(raw)


def value_matches(written: str, unit: str, candidates: Iterable[float], policy: ClaimsPolicy) -> bool:
    base, factor = BASE_UNITS[unit]
    n = _num(written)
    dec = _decimals(written)
    tol = float(policy.abs_tol.get(base, 0.5))
    for v in candidates:
        in_unit = v / factor
        if round(in_unit, dec) == round(n, dec):
            return True
        if abs(n * factor - v) <= max(tol, policy.rel_tol * abs(v)):
            return True
    return False


def _words_value(seq: str) -> Optional[int]:
    total, cur, any_word = 0, 0, False
    for w in re.split(r"[\s-]+", seq.lower().strip()):
        if not w or w in ("and", "a"):
            continue
        any_word = True
        if w in _NUM_WORDS:
            cur += _NUM_WORDS[w]
        elif w == "dozen":
            cur = max(cur, 1) * 12
        elif w == "hundred":
            cur = max(cur, 1) * 100
        elif w == "thousand":
            total += max(cur, 1) * 1000
            cur = 0
        else:
            return None
    return total + cur if any_word else None


def spell_out_numbers(text: str) -> str:
    """Number words directly before a unit become digits ("two hundred forty
    volts" -> "240 volts", "three full days" -> "3 full days")."""
    def rep(m: re.Match) -> str:
        if not _UNIT_AFTER.match(text[m.end():m.end() + 40]):
            return m.group(0)
        v = _words_value(m.group(0))
        return m.group(0) if v is None else str(v).ljust(len(m.group(0)))    # same length: offsets hold
    return _NW_SEQ.sub(rep, text or "")


def _scenario_form(t: str, start: int, end: int) -> bool:
    """The figure at t[start:end] is written as the question's scenario ("a
    24-hour outage", "an outage of 72 hours"), not as a runtime."""
    if re.search(r"\d\s*-\s*[A-Za-z]", t[start:end]):
        return True
    return bool(_SCEN_AFTER.match(t[end:end + 40]) or _SCEN_BEFORE.search(t[max(0, start - 40):start]))


@dataclasses.dataclass(frozen=True)
class NumberClaim:
    written: str
    unit: str            # canonical unit; 'F' for Fahrenheit
    start: int
    end: int
    scenario: bool       # written as the question's scenario ("a 24-hour outage")
    strict: bool         # "3 full days": the figure is claimed as a minimum


def _claims_in(t: str) -> list[NumberClaim]:
    out, taken = [], []
    for m in RANGE_RE.finditer(t):
        try:
            u = _unit(m.group("u"))
        except Exception:
            continue
        form = _scenario_form(t, m.start(), m.end())
        strict = bool(_STRICT_ADJ.search(t[m.end("b"):m.start("u")]))
        out += [NumberClaim(m.group("a"), u, m.start(), m.end(), form, False),
                NumberClaim(m.group("b"), u, m.start(), m.end(), form, strict)]
        taken.append((m.start(), m.end()))
    for m in SINGLE_RE.finditer(t):
        if any(a <= m.start() < b for a, b in taken):
            continue
        try:
            u = _unit(m.group("u"))
        except Exception:
            continue
        out.append(NumberClaim(m.group("n"), u, m.start(), m.end(), _scenario_form(t, m.start(), m.end()),
                               bool(_STRICT_ADJ.search(t[m.end("n"):m.start("u")]))))
    for m in TEMP_RE.finditer(t):
        neg = "-" if m.group("sign") else ""
        u = "F" if m.group("u").lower().startswith("f") else "C"
        out.append(NumberClaim(neg + m.group("n"), u, m.start(), m.end(), False, False))
    return sorted(out, key=lambda c: c.start)


def find_number_claims(text: str, names: Iterable[str] = ()) -> list[tuple[str, str, int, int]]:
    """(written number, canonical unit, start, end) for every number+unit in
    the prose of `text` (ranges yield both ends; spelled-out numbers count;
    'F' temperatures are reported as unit 'F')."""
    return [(c.written, c.unit, c.start, c.end) for c in _claims_in(spell_out_numbers(_prose(text, names)))]


def mention_spans(text: str, entities: Iterable[str]) -> list[tuple[int, int, str]]:
    """(start, end, entity) for every entity named in `text`; entity '' marks
    an UNKNOWN variant. Whole-word, case-insensitive, longest name first; a
    matched span is masked so a shorter name inside it never matches. A name
    followed by a model suffix (Max, Plus, Pro, Mini, Gen 2, ...) the entity
    list does not hold is another model: it is reported as unknown, never as
    that entity."""
    low = (text or "").lower()
    spans: list[tuple[int, int, str]] = []
    for e in sorted({str(x).lower() for x in entities if x and len(str(x)) >= 3}, key=len, reverse=True):
        rx = re.compile(r"(?<![a-z0-9])" + re.escape(e) + r"(?![a-z0-9])")
        for m in rx.finditer(low):
            spans.append((m.start(), m.end(), "" if _MODEL_SUFFIX.match(low[m.end():m.end() + 24]) else e))
        low = rx.sub(_blank, low)
    return sorted(spans)


def mentions(text: str, entities: Iterable[str]) -> tuple[list[str], bool]:
    """(entities named in `text`, whether an UNKNOWN variant is named)."""
    spans = mention_spans(text, entities)
    return list(dict.fromkeys(e for _a, _b, e in spans if e)), any(not e for _a, _b, e in spans)


def _paragraphs(text: str) -> list[list[str]]:
    """Paragraphs (blank-line separated; a heading line is its own paragraph)
    as lists of sentences."""
    out = []
    for para in re.split(r"\n\s*\n", text or ""):
        cur: list[str] = []
        for line in para.splitlines():
            if re.match(r"^\s*#{1,6}\s", line):
                if cur:
                    out.append(cur)
                out.append([line])
                cur = []
                continue
            cur += [x for x in _SENT_SPLIT.split(line) if x and x.strip()]
        if cur:
            out.append(cur)
    return out


def _negated(sentence: str, at: int, window: int = 6) -> bool:
    """A negation between the last clause break before `at` and `at`, within
    `window` words."""
    before = sentence[:at]
    breaks = [m.end() for m in _CLAUSE_BREAK.finditer(before)]
    seg = before[breaks[-1]:] if breaks else before
    words = seg.split()[-window:]
    return bool(_NEGATION.search(" ".join(words)))


def _sentence_at(text: str, pos: int) -> str:
    start = max(text.rfind(". ", 0, pos), text.rfind("\n", 0, pos), text.rfind("? ", 0, pos), text.rfind("! ", 0, pos))
    ends = [i for i in (text.find(". ", pos), text.find("\n", pos), text.find("? ", pos), text.find("! ", pos)) if i >= 0]
    return text[start + 1:(min(ends) if ends else len(text))]


def _money_ok(text: str, m: re.Match, policy: ClaimsPolicy) -> bool:
    """A '$' at m is inside an allowed budget label."""
    if policy.budget_label_anywhere and policy.budget_labels:
        dm = _DOLLAR_N.match(text, m.start())
        if dm and _num(dm.group(1)) in policy.budget_labels and not PRICE_VERB_RE.search(_sentence_at(text, m.start())):
            return True
    for rx in policy.price_labels:
        for lm in rx.finditer(text):
            if lm.start() <= m.start() < lm.end():
                if not policy.budget_labels:
                    return True
                try:
                    n = _num(lm.group("n"))
                except (IndexError, ValueError):
                    return True
                return n in policy.budget_labels
    return False


def _host_ok(host: str, policy: ClaimsPolicy) -> bool:
    host = (host or "").lower().split(":")[0]
    if host in policy.own_hosts:
        return True
    return any(host == h or host.endswith("." + h) for h in policy.external_hosts_allowed)


def _subject_at(spans: list[tuple[int, int, str]], pos: int) -> Optional[str]:
    """The entity a figure at `pos` is about: the nearest name before it in
    the sentence, else the first name after it; None when the sentence names
    none ('' = an unknown variant)."""
    before = [sp for sp in spans if sp[1] <= pos]
    if before:
        return before[-1][2]
    after = [sp for sp in spans if sp[0] >= pos]
    return after[0][2] if after else None


def _candidates(base: str, subject: Optional[str], form: bool, facts: NumericFacts, policy: ClaimsPolicy,
                scenario: Optional[NumericFacts], columns: Optional[set[str]] = None) -> set[float]:
    if columns is not None:
        ents = [subject] if subject else []
        cands = facts.column_values(base, columns, ents) if subject != "" else facts.column_values(base, columns, [""])
    elif subject is not None:
        cands = set(facts.global_values.get(base, ()))
        if subject:
            cands |= set(facts.by_entity.get(subject, {}).get(base, ()))
    elif base == "h":
        cands = set(facts.global_values.get(base, ()))      # an hour figure about no model is never a runtime
    else:
        cands = set(facts.values.get(base, ()))
    if base != "h" or form:
        cands |= set(policy.always_allowed.get(base, ()))
        if scenario:
            cands |= set(scenario.values.get(base, ()))
    return cands


def _f_matches(written: str, cands: Iterable[float]) -> bool:
    c = (_num(written) - 32.0) * 5.0 / 9.0
    return any(abs(c - v) <= 1.0 for v in cands)


def _claim_matches(c: NumberClaim, cands: set[float], policy: ClaimsPolicy) -> bool:
    if c.unit == "F":
        return _f_matches(c.written, cands)
    base, factor = BASE_UNITS[c.unit]
    n = _num(c.written) * factor
    tol = float(policy.abs_tol.get(base, 0.5))
    if c.strict:                                       # "3 full days": at least that much
        cands = {v for v in cands if v >= n - tol}
    if factor > 1 and base == "h":                     # days / weeks: within 5 % of a FACTS hour figure
        return any(abs(v - n) <= max(tol, 0.05 * n) for v in cands)
    return value_matches(c.written, c.unit, cands, policy)


_CONTEXT_BREAK = re.compile(r"[;:]|\b(?:and|but|while|whereas|yet|or)\b", re.I)


def _context_columns(sentence: str, pos: int, base: str, policy: ClaimsPolicy) -> Optional[set[str]]:
    """The columns a figure at `pos` must come from: the contexts whose
    pattern occurs in the figure's clause (from the last 'and/but/;' before
    it), e.g. "charges down to -20 C" but not "charges at 0 C and runs down to
    -20 C" for the -20."""
    before = sentence[:pos]
    breaks = [m.end() for m in _CONTEXT_BREAK.finditer(before)]
    clause = before[breaks[-1]:] if breaks else before
    cols: Optional[set[str]] = None
    for ctx in policy.contexts:
        if ctx.unit == base and ctx.rx.search(clause):
            cols = (cols or set()) | set(ctx.columns)
    return cols


def _number_violations(field: str, text: str, facts: NumericFacts, policy: ClaimsPolicy,
                       scenario: Optional[NumericFacts], names: set[str]) -> tuple[list[ClaimViolation], int]:
    out: list[ClaimViolation] = []
    checked = 0
    for para in _paragraphs(text):
        carried: Optional[str] = None               # the last subject named earlier in the paragraph
        for sent in para:
            plain = _plain(sent)
            spans = mention_spans(plain, facts.by_entity)
            prose = spell_out_numbers(_mask(plain, names))
            for c in _claims_in(prose):
                base = "C" if c.unit == "F" else BASE_UNITS[c.unit][0]
                if base not in policy.units:
                    continue
                checked += 1
                subject = _subject_at(spans, c.start)
                inherited = subject is None and carried is not None
                if inherited:
                    subject = carried
                cols = _context_columns(plain, c.start, base, policy)
                cands = _candidates(base, subject, c.scenario, facts, policy, scenario, cols)
                if _claim_matches(c, cands, policy):
                    continue
                if subject:
                    about = f" (about {subject}" + (", carried from the previous sentence" if inherited else "") + ")"
                    why = "every figure must be a FACTS value (within rounding) of the model it is about"
                elif subject == "":
                    about = " (about a model FACTS does not name)"
                    why = "the sentence names a model variant FACTS does not hold"
                else:
                    about = ""
                    why = "every figure must be a FACTS value (within rounding)"
                    if base == "h":
                        why += ("; an hour figure about no model must be a FACTS figure about no model, or the "
                                "question's own scenario written as one ('a 24-hour outage')")
                if cols is not None:
                    why += f"; in this sentence only {', '.join(sorted(cols))} figures fit"
                if c.strict:
                    why += "; 'full/whole' claims at least that much"
                out.append(ClaimViolation(
                    "number-not-in-facts", field, f"{c.written} {c.unit}{about}: {_snippet(plain, 0, 0, 160)}"[:240],
                    why, "Replace it with the exact FACTS value for the model the sentence names (name it), or "
                         "drop the sentence."))
            if spans:
                carried = spans[-1][2]
    return out, checked


def _row_violations(field: str, text: str, rows: Mapping[str, list], facts: NumericFacts,
                    policy: ClaimsPolicy) -> list[ClaimViolation]:
    out: list[ClaimViolation] = []
    if not rows or not (policy.verdicts or policy.statuses):
        return out
    for para in _paragraphs(text):
        for sent in para:
            plain = MD_LINK_RE.sub(lambda m: f" {m.group(2)} ", sent)
            for r in policy.verdicts:
                prs = [x for x in rows.get(r.rows) or [] if isinstance(x, Mapping)]
                names = {str(x.get(c)).lower() for x in prs for c in r.subjects if x.get(c)}
                named, _unk = mentions(plain, names | set(facts.by_entity))
                named_set = set(named)
                bad = [x for x in prs if str(x.get(r.subjects[0]) or "").lower() in named_set
                       and str(x.get(r.subjects[1]) or "").lower() in named_set
                       and not r.is_fit(x)]
                if not bad:
                    continue
                for m in r.claim_rx.finditer(plain):
                    if not _negated(plain, m.start()):
                        x = bad[0]
                        out.append(ClaimViolation(
                            "verdict-contradicts-facts", field, _snippet(plain, m.start(), m.end(), 100),
                            f"FACTS gives {x.get(r.subjects[1])} on {x.get(r.subjects[0])} the verdict "
                            f"{x.get(r.verdict)!r}" + (f" with flags {x.get(r.flags)}" if r.flags and x.get(r.flags)
                                                       else "") + "; it is never called a fit",
                            "State the verdict FACTS gives (for example 'over the limit, so not a fit')."))
                        break
            for r in policy.statuses:
                prs = [x for x in rows.get(r.rows) or [] if isinstance(x, Mapping)]
                keys = {str(x.get(r.key)) for x in prs if x.get(r.key)}
                kmatch = [k for k in keys if re.search(r"(?<![A-Za-z0-9])" + re.escape(k) + r"(?![A-Za-z0-9])",
                                                        plain, re.I)]
                claims = [m for m in r.claim_rx.finditer(plain) if not _negated(plain, m.start())]
                if not kmatch or not claims:
                    continue
                subj_names = {str(x.get(r.subject)).lower() for x in prs if x.get(r.subject)}
                named, unknown = mentions(plain, subj_names | set(facts.by_entity))
                dates = set(re.findall(r"\b\d{4}-\d{2}-\d{2}\b", plain))
                problems = []
                if unknown:
                    problems.append("names a model variant FACTS does not hold")
                for ent in named:
                    for k in kmatch:
                        ok_rows = [x for x in prs if str(x.get(r.subject) or "").lower() == ent
                                   and str(x.get(r.key)).lower() == k.lower() and str(x.get(r.status)) in r.positive]
                        if not ok_rows:
                            have = [str(x.get(r.status)) for x in prs if str(x.get(r.subject) or "").lower() == ent
                                    and str(x.get(r.key)).lower() == k.lower()]
                            problems.append(f"{ent}: {k} is {have[0] if have else 'not in FACTS'}")
                        elif r.date and dates and not dates & {str(x.get(r.date))[:10] for x in ok_rows}:
                            problems.append(f"{ent}: {k} date is {str(ok_rows[0].get(r.date))[:10]} in FACTS")
                if problems:
                    out.append(ClaimViolation(
                        "status-contradicts-facts", field, _snippet(plain, 0, 0, 200)[:240],
                        "a status claim must match FACTS: " + "; ".join(problems[:4]),
                        "Write the status, standard, lab and date exactly as FACTS gives them for that model, "
                        "or drop the sentence."))
    return out


def _scan_text(text: str) -> str:
    """Text for wording rules: placeholders and link targets removed (anchor
    text kept), so a slug such as /best/cheapest-usable-kwh is not prose."""
    t = PLACEHOLDER_RE.sub(" ", text or "")
    return MD_LINK_RE.sub(lambda m: f"{m.group(1)}[{m.group(2)}]( )", t)


def check(*, title: str = "", subtitle: str = "", excerpt: str = "", body_md: str = "", faqs: Any = None,
          facts: NumericFacts, policy: ClaimsPolicy, scenario: Optional[NumericFacts] = None,
          rows: Optional[Mapping[str, list]] = None) -> ClaimsAudit:
    """Audit one draft against its facts pack (see module docstring). `rows`
    (the pack's rows by query) feeds the optional verdict and status rules."""
    out: list[ClaimViolation] = []
    names = set(facts.names) | (set(scenario.names) if scenario else set())
    checked = 0
    for field, text in _fields(title, subtitle, excerpt, body_md, faqs):
        if not text:
            continue
        v, n = _number_violations(field, text, facts, policy, scenario, names)
        out += v
        checked += n
        out += _row_violations(field, text, rows or {}, facts, policy)
        # banned wording, topics
        scan = _scan_text(text)
        for p in policy.banned + policy.topic_bans:
            for m in p.rx.finditer(scan):
                out.append(ClaimViolation(p.id, field, _snippet(scan, m.start(), m.end()), p.why,
                                          p.fix or "Remove the sentence; do not rephrase around the rule."))
        # money
        for m in MONEY_ANY.finditer(text):
            if not _money_ok(text, m, policy):
                out.append(ClaimViolation("point-price", field, _snippet(text, m.start(), m.end()),
                                          "a guide never states a price; the page renders live stamped prices",
                                          "Remove the amount (a budget label such as 'under $500' is the only "
                                          "allowed form)."))
        # Amazon anywhere
        for m in AMAZON_RE.finditer(text):
            out.append(ClaimViolation("amazon-url", field, _snippet(text, m.start(), m.end()),
                                      "no Amazon URL, short link or image host in authored text",
                                      "Link our own /product/<ASIN> page instead."))
        # links
        for m in MD_LINK_RE.finditer(text):
            img, url = m.group(1), m.group(3)
            if img and not policy.allow_images:
                out.append(ClaimViolation("inline-image", field, m.group(0)[:120], "no inline images in a guide"))
            if url.startswith(("http://", "https://", "//")):
                host = urlparse(url if not url.startswith("//") else "https:" + url).netloc
                if not _host_ok(host, policy):
                    out.append(ClaimViolation("external-link", field, url[:160],
                                              "links go to our own pages (or an allowed source host)",
                                              "Use a site-relative path from FACTS."))
            elif not url.startswith("/"):
                out.append(ClaimViolation("relative-link", field, url[:160], "links are absolute site paths",
                                          "Use a path from FACTS such as /product/<ASIN>."))
        for m in BARE_URL_RE.finditer(MD_LINK_RE.sub(" ", text)):
            host = urlparse(m.group(0) if "://" in m.group(0) else "https://" + m.group(0)).netloc
            if not _host_ok(host, policy):
                out.append(ClaimViolation("external-link", field, m.group(0)[:160], "bare URL to another site"))
        for m in RAW_HTML_RE.finditer(text):
            out.append(ClaimViolation("raw-html", field, _snippet(text, m.start(), m.end()),
                                      "markdown only: raw HTML bypasses the link and image checks"))
        if field == "body" and not policy.allow_markdown_tables and TABLE_SEP_RE.search(text):
            out.append(ClaimViolation("hand-written-table", field, TABLE_SEP_RE.search(text).group(0)[:80],
                                      "tables come from the site's placeholders, rendered from SQL",
                                      "Replace the table with the matching {{...}} placeholder."))
    return ClaimsAudit(out, checked)


# ---------------------------------------------------------------------------
# Placeholders (grammar and position; the caller resolves keys against data)
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Placeholder:
    raw: str
    kind: str          # "" = unparseable
    key: str
    line: int


_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_BAD_LINE_START = re.compile(r"^\s*(?:#{1,6}\s|[-*+]\s|\d+[.)]\s|\||>)")


def find_placeholders(body_md: str, kinds: Iterable[str]) -> tuple[list[Placeholder], list[str]]:
    """Placeholders in `body_md` and grammar problems (unknown kind, bad key,
    stray braces, a placeholder inside a heading, list item, table row or
    quote). `kinds` are the site's kinds, e.g. 'table:runtime', 'picks'."""
    kinds = set(kinds)
    found, problems = [], []
    for ln, line in enumerate((body_md or "").splitlines(), 1):
        for m in PLACEHOLDER_RE.finditer(line):
            inner = m.group(1).strip()
            parts = [p.strip() for p in inner.split(":")]
            kind, key = (":".join(parts[:-1]), parts[-1]) if len(parts) >= 2 else ("", "")
            if kind not in kinds or not _SLUG.match(key or ""):
                problems.append(f"line {ln}: unknown or malformed placeholder {m.group(0)!r}")
                kind, key = "", ""
            elif _BAD_LINE_START.match(line):
                problems.append(f"line {ln}: placeholder {m.group(0)!r} inside a heading, list, table or quote")
            found.append(Placeholder(m.group(0), kind, key, ln))
        rest = PLACEHOLDER_RE.sub("", line)
        if "{{" in rest or "}}" in rest:
            problems.append(f"line {ln}: stray '{{{{' or '}}}}'")
    return found, problems


__all__ = ["ClaimsPolicy", "ClaimsAudit", "ClaimViolation", "ClaimsConfigError", "Pattern", "VerdictRule",
           "StatusRule", "parse_policy", "check", "find_number_claims", "spell_out_numbers", "mentions",
           "value_matches", "find_placeholders", "Placeholder", "DEFAULT_BANNED",
           "DEFAULT_PRICE_LABELS", "PLACEHOLDER_RE", "MD_LINK_RE"]
