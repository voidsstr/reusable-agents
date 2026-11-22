"""Model matcher: does an Amazon listing name exactly one of OUR model records?

A site keeps its own model catalog (maker, model name, aliases, model number,
kind, a few rated specs, kit components). An affiliate listing is useful to it
only once it is linked to the right model: the buy button, the picks and the
year-1 blocks read the model, never the listing. This primitive decides that
link from two Amazon fields read in memory, ``itemInfo.byLineInfo.brand`` and
``itemInfo.title``, and returns ONLY our derived outcome: a model id (or None),
a rule id, a confidence and a reason code. It never returns, stores or logs any
part of the title; callers persist the outcome and drop the listing.

Generic: no site, niche or maker names. Makers, aliases, kinds and specs come
from the caller's model rows and config.

THE RULES ("search and confirm")
--------------------------------
1. **Brand.** Amazon's brand field must resolve, alias-aware, to the maker of
   the model (``brand_aliases``: canonical maker -> other spellings, plus the
   model rows' own ``brand_aliases``). A missing or unknown brand never
   matches, and brand words in the title are never used instead.
2. **Designation.** One of the model's designations (name, aliases, model
   number) must appear in the title as a whole token sequence. Case,
   punctuation, unicode and spacing variants fold together ('DELTA3' =
   'DELTA 3' = 'delta-3'), but a token is never split: 'AC180' is not inside
   'AC180P' and 'C1000' is not inside 'C1000X'.
3. **Nesting resolves to the longest.** 'DELTA 3' inside 'DELTA 3 Max Plus'
   is the longer model, not the shorter one.
4. **Unknown variants are not the base model.** A designation followed by a
   variant word ('Plus', 'Pro', 'Max', 'v2', 'Gen 2', a bare number, a single
   letter, ...) that is not itself a known designation names a model we do
   not carry: no match.
5. **Compatibility context.** Designations after 'for', 'compatible',
   'fits', 'works with', 'replacement' name another product (an accessory's
   station): they are ignored.
6. **Ambiguity.** Two different models left after nesting, or one designation
   shared by two models: no match.
7. **Bundles.** A station designation plus solar-panel wording or a panel
   wattage, or 'with (extra) battery', is a bundle: it maps only to a kit
   model whose station is that station and whose panels / batteries agree
   with the title, never to the bare station. No such kit, or several: no
   match.
8. **Contradictions.** A capacity (Wh) or wattage (W) stated in the title that
   contradicts the matched model's rated spec: no match.
9. **What the listing is.** The item noun of the designation's own phrase
   decides what is sold: an accessory or part noun after the designation
   ('DELTA 2 Max Waterproof Bag', 'AC180 Replacement Cable', 'Yeti 1500X Roll
   Cart'), or a noun of another kind ('DELTA 3 Max Smart Extra Battery' for a
   station), is no match. A designation that only follows 'with' / '+' names
   a bundled part, never the listing.
10. **Condition and quantity.** Renewed, refurbished, used or open-box wording
    is no match. A unit count above one ('2 Pack', 'x2', 'Set of 2') on a
    single-unit model is no match; in a bundle every count must be one the
    kit check used.
11. **'+' glued to a name** reads 'plus' ('Max+' = 'Max Plus', 'Nomad 100+'),
    so the base model never takes it.

Never on brand alone. Everything here is a pure function of its inputs.
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Sequence

# -- roles: what a model kind means to the matcher ---------------------------

ROLE_STATION = "station"
ROLE_KIT = "kit"
ROLE_PANEL = "panel"
ROLE_BATTERY = "battery"
ROLE_OTHER = "other"
ROLES = (ROLE_STATION, ROLE_KIT, ROLE_PANEL, ROLE_BATTERY, ROLE_OTHER)

#: Model ``kind`` -> role. A caller with other kind names passes ``kind_roles``.
DEFAULT_KIND_ROLES: Mapping[str, str] = {
    "station": ROLE_STATION, "power-station": ROLE_STATION,
    "kit": ROLE_KIT, "bundle": ROLE_KIT,
    "panel": ROLE_PANEL, "solar-panel": ROLE_PANEL,
    "battery": ROLE_BATTERY, "expansion": ROLE_BATTERY, "expansion-battery": ROLE_BATTERY,
    "expansion_battery": ROLE_BATTERY,
}

# -- rule ids and reason codes (stable: they are stored and reported) --------

RULE_MODEL_NUMBER = "model-number"
RULE_DESIGNATION = "designation"
RULE_BUNDLE_KIT = "bundle-kit"
RULE_CONFIDENCE: Mapping[str, float] = {
    RULE_MODEL_NUMBER: 0.97,
    RULE_DESIGNATION: 0.95,
    RULE_BUNDLE_KIT: 0.92,
}

REASON_MATCHED = "matched"
REASON_NO_TITLE = "no-title"
REASON_BRAND_MISSING = "brand-missing"
REASON_BRAND_UNKNOWN = "brand-unknown"
REASON_NO_DESIGNATION = "no-designation"
REASON_UNKNOWN_VARIANT = "unknown-variant"
REASON_AMBIGUOUS = "ambiguous"
REASON_BUNDLE_NO_KIT = "bundle-no-kit"
REASON_BUNDLE_AMBIGUOUS = "bundle-ambiguous"
REASON_BUNDLE_UNVERIFIED = "bundle-unverified"
REASON_CAPACITY = "capacity-mismatch"
REASON_WATTAGE = "wattage-mismatch"
REASON_CONDITION = "condition"            # renewed / refurbished / used / open box
REASON_ACCESSORY = "accessory"            # 'DELTA 2 Max Bag', 'AC180 Replacement Cable'
REASON_ITEM_KIND = "item-kind-mismatch"   # 'DELTA 3 Max Smart Extra Battery' is not the station
REASON_MULTI_PACK = "multi-pack"          # '2 Pack', 'x2', 'Set of 2' of a single-unit model
REASON_BUNDLE_COMPONENT = "bundle-component"  # the designation is the part after 'with' / '+'
#: Reason codes that mean "more than one reading": reported as ambiguous.
AMBIGUOUS_REASONS = frozenset({REASON_AMBIGUOUS, REASON_BUNDLE_AMBIGUOUS, REASON_BUNDLE_UNVERIFIED})

DEFAULT_CAPACITY_TOLERANCE = 0.03

# -- normalisation -------------------------------------------------------------

_FOLD = str.maketrans({
    "×": "x", "✕": "x", "✖": "x", "＋": "+", "–": "-", "—": "-", "‐": "-", "‑": "-",
    "‒": "-", "−": "-", "*": "x", "’": "'", "‘": "'", "“": '"', "”": '"', " ": " ",
})
_WS_RE = re.compile(r"\s+")
_NON_WORD_RE = re.compile(r"[\W_]+", re.UNICODE)
_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
#: Characters that end a phrase: a designation never spans one.
_HARD_CHARS = frozenset(",;:()[]{}|/+&")
_SOFT_DASH_RE = re.compile(r"\s-|-\s")


def normalize_text(text: Any) -> str:
    """NFKC, case-folded, dashes / multiplication signs folded, single spaces."""
    s = unicodedata.normalize("NFKC", str(text or "")).casefold().translate(_FOLD)
    return _WS_RE.sub(" ", s).strip()


def compact(text: Any) -> str:
    """Letters and digits only: 'EF-DL-H02-3M' -> 'efdlh023m', 'DELTA 3' -> 'delta3'."""
    return _NON_WORD_RE.sub("", normalize_text(text))


@dataclass(frozen=True)
class Token:
    text: str            # compact form of one word
    hard_before: bool    # a phrase break (',', '(', '+', ' - ', ...) precedes it
    start: int = 0       # character span in normalize_text(...)
    end: int = 0
    synthetic: bool = False   # the 'plus' read from a '+' glued to the word before ('Max+')


#: A '+' glued to the end of a word ('Max+', 'Nomad 100+') is part of the name: 'plus'.
_GLUED_PLUS_RE = re.compile(r"(?<=[^\W_])\+(?![^\W_])")


def tokenize(text: Any) -> list[Token]:
    s = normalize_text(text)
    out: list[Token] = []
    prev_end = 0
    for m in _TOKEN_RE.finditer(s):
        gap = s[prev_end:m.start()]
        hard = bool(out) and (any(c in _HARD_CHARS for c in gap) or bool(_SOFT_DASH_RE.search(gap)))
        out.append(Token(_NON_WORD_RE.sub("", m.group(0)), hard, m.start(), m.end()))
        prev_end = m.end()
        if _GLUED_PLUS_RE.match(s, m.end()):
            # 'DELTA 3 Max+' reads 'DELTA 3 Max plus'; the word after the '+'
            # still starts a new phrase (the '+' stays in its gap).
            out.append(Token("plus", False, m.end(), m.end() + 1, synthetic=True))
    return out


# -- designations ----------------------------------------------------------------

_MEASURE_WORD_RE = re.compile(r"^\d+(?:[.,]\d+)?(?:w|wh|kwh|v|a|ah|mah|watts?)$")
_PAREN_RE = re.compile(r"\(([^()]*)\)")
_MODEL_NO_PREFIX_RE = re.compile(r"^\s*(?:model\s*(?:no\.?|number|#)\s*[:.]?\s*)", re.I)
#: Words that cannot make a designation on their own.
GENERIC_WORDS = frozenset("""
    a an and the with for new portable power station stations solar generator generators battery
    batteries extra expansion smart kit kits bundle bundles foldable folding lightweight suitcase
    pack module panel panels charger charging car extension input output dc ac cable cables
    adapter adapters connector connectors series
    original edition black white grey gray plus pro max mini air lite ultra home backup outdoor
    camping current listing version only
    """.split())
#: Words after a leading model code that still describe THAT product ('PS200 Bifacial
#: Portable Solar Panel' is the PS200), as opposed to an accessory for it ('F3000LFP Cascade Cable').
_PRODUCT_NOUNS = frozenset({"panel", "panels", "station", "generator", "battery", "module", "blanket",
                            "suitcase"})
_ACCESSORY_NOUNS = frozenset({"cable", "cables", "adapter", "adapters", "charger", "connector",
                              "connectors", "case", "bag", "cover", "kit", "hub", "box", "cart",
                              "trolley", "switch", "inlet", "cord", "extension"})
_CUE_WORDS = frozenset({"for", "compatible", "compatibility", "fits", "replacement", "replaces"})
#: Common connector / chemistry codes that look like model numbers but are not.
NUMBER_STOPWORDS = frozenset({"lifepo4", "lifepo", "dc8020", "dc7909", "dc5521", "xt60", "xt60i", "xt90",
                              "mc4", "usbc", "typec", "sae", "tt30", "l1430", "l530", "nema"})


def model_number_like(text: Any) -> bool:
    """A model code: >= 4 letters/digits with >= 2 digits and a letter, not a
    measure ('100W') and not a connector or chemistry code ('LiFePO4')."""
    c = compact(text)
    if len(c) < 4 or c in NUMBER_STOPWORDS or _MEASURE_WORD_RE.match(c):
        return False
    digits = sum(ch.isdigit() for ch in c)
    return digits >= 2 and any(ch.isalpha() for ch in c)


def _valid_designation(words: Sequence[str]) -> bool:
    c = compact(" ".join(words))
    if len(c) < 2:
        return False
    if all(compact(w) in GENERIC_WORDS for w in words):
        return False
    return any(ch.isdigit() for ch in c) or sum(ch.isalpha() for ch in c) >= 4


def designations_of(texts: Iterable[str], *, brand_prefixes: Iterable[str] = (),
                    codes_from_parens: bool = True) -> tuple[set[str], set[str]]:
    """(names, model numbers) as compact strings, from a model's name and aliases.

    Parenthesised parts are dropped from the name ('DELTA 3 Max (2048Wh)
    (EF-DL-H02-3M)' -> 'DELTA 3 Max'), but model codes inside them are kept as
    model numbers. 'A / B' names two designations. A leading maker name or
    alias is stripped. Trailing descriptors after a measure are cut when the
    words before it already carry a digit ('Flash300 286Wh Fast Charging ...'
    -> 'Flash300', but 'SolarSaga 200W' stays whole). Codes after 'for' name
    another product ('Cascade Cable for EP3800-48V') and are never taken; a
    kit passes ``codes_from_parens=False`` (its parentheses list components).
    """
    prefixes = sorted({p for p in (compact(b) for b in brand_prefixes) if p}, key=len, reverse=True)
    names: set[str] = set()
    numbers: set[str] = set()
    for raw in texts:
        text = _GLUED_PLUS_RE.sub(" plus", normalize_text(raw))   # 'Nomad 100+' -> 'nomad100plus'
        if not text:
            continue
        for inner in (_PAREN_RE.findall(text) if codes_from_parens else ()):
            toks = [t for t in re.split(r"[\s,;/]+", inner) if t]
            if any(compact(t) in _CUE_WORDS for t in toks):
                continue
            for tok in toks:
                if model_number_like(tok):
                    numbers.add(compact(tok))
        main = _PAREN_RE.sub(" ", text)
        main = re.split(r"\s-\s|:", main)[0]
        for part in re.split(r"\s/\s", main):
            words = part.split()
            # strip a leading maker name / alias (it may span several words)
            stripped = True
            while stripped and words:
                stripped = False
                for k in range(min(4, len(words)), 0, -1):
                    if compact(" ".join(words[:k])) in prefixes and k < len(words):
                        words = words[k:]
                        stripped = True
                        break
            for i, w in enumerate(words):
                if i and compact(w) in _CUE_WORDS:  # 'MJ32 Cascade Cable for EP3800-48V'
                    words = words[:i]
                    break
            for i, w in enumerate(words):
                if i and _MEASURE_WORD_RE.match(compact(w)) and any(
                        ch.isdigit() for ch in compact(" ".join(words[:i]))):
                    words = words[:i]
                    break
            if words and _valid_designation(words):
                names.add(compact(" ".join(words)))
            rest = {compact(w) for w in words[1:]}
            if (len(words) > 2 and model_number_like(words[0]) and rest & _PRODUCT_NOUNS
                    and not rest & (_ACCESSORY_NOUNS | _CUE_WORDS)
                    and not any(_VARIANT_WORD_RE.match(w) for w in rest)):
                # 'PS200 Bifacial Portable Solar Panel': the code alone names it too
                names.add(compact(words[0]))
            for w in words:
                if compact(w) in _CUE_WORDS:
                    break
                if "-" in w and model_number_like(w):
                    numbers.add(compact(w))
    return names, numbers


def model_numbers_of(texts: Iterable[Any]) -> set[str]:
    out: set[str] = set()
    for raw in texts:
        text = _MODEL_NO_PREFIX_RE.sub("", normalize_text(raw))
        for tok in re.split(r"[\s,;/()]+", text):
            if model_number_like(tok):
                out.add(compact(tok))
    return out


# -- model records ------------------------------------------------------------------

def _num(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and out not in (float("inf"), float("-inf")) else None


def _texts(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    return tuple(str(v) for v in value if v is not None and str(v).strip())


@dataclass(frozen=True)
class ModelRecord:
    """One of OUR models, as the matcher needs it (no Amazon data)."""

    model_id: Any
    brand: str
    kind: str
    name: str = ""
    aliases: tuple[str, ...] = ()
    model_numbers: tuple[str, ...] = ()
    brand_aliases: tuple[str, ...] = ()
    capacity_wh: Optional[float] = None
    power_w: Optional[float] = None
    components: tuple[tuple[Any, int], ...] = ()

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "ModelRecord":
        """From a row shaped like a site's ``models_sql``: ``id`` (or
        ``model_id``), ``brand``, ``model_name`` (or ``name``), ``kind`` and the
        optional ``aliases``, ``model_number``, ``brand_aliases``,
        ``capacity_wh``, ``power_w``, ``components`` (component model ids) and
        ``component_qty`` (same order)."""
        mid = row.get("id", row.get("model_id"))
        comps = list(row.get("components") or [])
        qtys = list(row.get("component_qty") or [])
        pairs = []
        for i, c in enumerate(comps):
            if c is None:
                continue
            try:
                q = int(qtys[i]) if i < len(qtys) and qtys[i] is not None else 1
            except (TypeError, ValueError):
                q = 1
            pairs.append((c, max(1, q)))
        return cls(model_id=mid, brand=str(row.get("brand") or ""), kind=str(row.get("kind") or ""),
                   name=str(row.get("model_name") or row.get("name") or ""),
                   aliases=_texts(row.get("aliases")),
                   model_numbers=_texts(row.get("model_number")),
                   brand_aliases=_texts(row.get("brand_aliases")),
                   capacity_wh=_num(row.get("capacity_wh")), power_w=_num(row.get("power_w")),
                   components=tuple(pairs))


@dataclass(frozen=True)
class MatchResult:
    """The derived outcome; never carries listing text."""

    model_id: Any
    confidence: float
    rule: str
    reason_code: str

    @property
    def matched(self) -> bool:
        return self.model_id is not None

    @property
    def ambiguous(self) -> bool:
        return self.reason_code in AMBIGUOUS_REASONS

    def to_dict(self) -> dict[str, Any]:
        return {"model_id": self.model_id, "confidence": self.confidence, "rule": self.rule,
                "reason_code": self.reason_code}


def _no(reason: str) -> MatchResult:
    return MatchResult(None, 0.0, "", reason)


# -- title evidence --------------------------------------------------------------------

#: Words that start a compatibility context ('for DELTA 3', 'compatible with ...').
_CUE_SINGLE = frozenset({"for", "compatible", "compatibility", "fits", "replacement", "replaces"})
_CUE_PAIRS = frozenset({("works", "with"), ("work", "with"), ("use", "with"), ("pairs", "with"),
                        ("fit", "for")})

_CONNECTOR = r"(?:with|w/|\+|plus|and|&|includ(?:es|ing|ed)?|bundled?|combo)"
#: Words just before a connector that make it a compatibility statement, not a bundle.
_NOT_BUNDLE_BEFORE_RE = re.compile(
    r"\b(?:compatible|compatibility|work|works|use|used|pair|pairs|paired|support|supports|"
    r"charge|charges|charging|connect|connects|connecting|recharge|rechargeable)\W*$")
_EXCLUDED_CLAUSE_RE = re.compile(r"optional|not included|sold separately|without|\bno (?:solar )?panel")
_CLAUSE_SPLIT_RE = re.compile(r"[,;()\[\]|]")
_PANEL_NOUN = r"(?:solar\s+)?(?:panels?|modules?|blankets?|suitcases?)\b"
_PANEL_WATTS_RE = re.compile(
    _CONNECTOR + r"\s*(?:(?P<q1>\d{1,2})\s*(?:x|pcs?|pieces?|packs?|sets?|units?)\s*)?"
    r"(?:[a-z][\w-]*\s+){0,2}?(?<!\d)(?P<w>\d{2,3})\s?w(?:atts?)?\b(?:\s*x\s*(?P<q2>\d{1,2})\b)?"
    r"(?:\s+[a-z][\w-]*){0,3}?\s+(?:solar\s+)?(?P<noun>panels?|modules?|blankets?|suitcases?)\b")
_PANEL_WORDING_RE = re.compile(
    _CONNECTOR + r"\s+(?:[\w-]+\s+){0,4}?" + _PANEL_NOUN
    + r"|solar generator kit|solar (?:panel )?kit\b|panels? included")
_BATTERY_WORDING_RE = re.compile(
    _CONNECTOR + r"\s+(?:(?:\d\s*(?:x|pcs?)?)\s*)?(?:[\w-]+\s+){0,4}?"
    r"(?:(?:extra|expansion|additional|external)\s+batter(?:y|ies)\b"
    r"|batter(?:y|ies)\s+(?:packs?|expansion)\b|expansion\s+packs?\b)")
_KIT_PANEL_WATTS_RE = re.compile(
    r"(?:(?P<q1>\d{1,2})\s*(?:x|pcs?|pieces?)\s*)?(?<!\d)(?P<w>\d{2,3})\s?w(?:atts?)?\b(?:\s*x\s*(?P<q2>\d{1,2})\b)?")
_KIT_BATTERY_RE = re.compile(r"extra batter|expansion batter|battery pack|smart extra|"
                             r"(?:\+|with|and)\s*(?:\d\s*x?\s*)?batter(?:y|ies)\b")
#: After a wattage in a kit's name: the station's own rating, not a panel's.
_NOT_PANEL_AFTER_RE = re.compile(r"^\s*(?:solar\s+)?(?:generator|output|ac\b|inverter|station|surge|peak)")

_WH_RE = re.compile(r"(?<![\d.,])(\d{1,2},\d{3}(?:\.\d+)?|\d+(?:\.\d+)?)\s?(kwh|wh|watt[- ]?hours?)\b")
_W_RE = re.compile(r"(?<![\d.,])(\d{1,2},\d{3}|\d{2,5})\s?(?:w|watts?)\b")
_EXPAND_RE = re.compile(r"(?:expand(?:able|ed|s)?|extend(?:able|ed)?|scalable|up)\s*(?:up\s*)?to\s*[\d.,]+\s?k?wh")
_W_SKIP_AFTER_RE = re.compile(
    r"^\s*(?:max\s+|ac\s+)?(?:solar|pv|charg|input|surge|peak|boost|car\b|usb|pd\b|type|wireless|"
    r"fast|lifting|dc\b|x\b)")
_W_SKIP_BEFORE_RE = re.compile(
    r"(?:surge|peak|boost|up to|lifting|input|solar|pv|usb[- ]?c?|pd|charging|charge|car|dc|max)\W*$")


def _wh_values(text: str, *, exclude: Sequence[tuple[int, int]] = ()) -> list[float]:
    t = _EXPAND_RE.sub(lambda m: " " * len(m.group(0)), text)  # same length: spans stay valid
    out = []
    for m in _WH_RE.finditer(t):
        if any(a <= m.start() < b for a, b in exclude):
            continue
        raw, unit = m.group(1), m.group(2)
        if re.fullmatch(r"\d{1,2},\d{3}(?:\.\d+)?", raw):
            value = float(raw.replace(",", ""))
        elif unit.startswith("wh") or unit.startswith("watt"):
            value = float(raw.replace(".", "")) if re.fullmatch(r"\d\.\d{3}", raw) else float(raw)
        else:
            value = float(raw)
        if unit == "kwh":
            value *= 1000.0
        out.append(value)
    return out


def _w_values(text: str, *, exclude: Sequence[tuple[int, int]] = ()) -> list[float]:
    out = []
    for m in _W_RE.finditer(text):
        if any(a <= m.start() < b for a, b in exclude):
            continue
        if _W_SKIP_AFTER_RE.match(text[m.end():m.end() + 16]):
            continue
        if _W_SKIP_BEFORE_RE.search(text[max(0, m.start() - 14):m.start()]):
            continue
        out.append(float(m.group(1).replace(",", "")))
    return out


def _close(a: float, b: float, tol: float) -> bool:
    return abs(a - b) <= max(tol * max(abs(a), abs(b)), 1.0)


@dataclass
class _Occurrence:
    start: int
    end: int
    model_ids: frozenset
    number: bool          # found through a model number
    variant_after: bool   # an unknown variant word follows it


@dataclass(frozen=True)
class _KitInfo:
    station_id: Any
    panels: Mapping[Any, int]       # panel key (watts, or 'm:<id>') -> qty
    battery: bool
    battery_wh: float               # 0 when unknown
    battery_ids: frozenset = frozenset()   # the kit's battery components (model ids)


@dataclass
class _Brand:
    key: str
    name: str
    designations: dict[str, set] = field(default_factory=dict)   # compact -> model ids
    numbers: dict[str, set] = field(default_factory=dict)        # compact -> model ids
    prefixes: set = field(default_factory=set)                   # every prefix of a key above
    kits: list = field(default_factory=list)
    #: Suffixes that, in this maker's own catalog, turn one model into another
    #: ('C300' -> 'C300 DC', 'Yeti 1000' -> 'Yeti 1000 Core'): a variant word here.
    variant_words: set = field(default_factory=set)


_VARIANT_WORD_RE = re.compile(r"^(?:plus|pro|max|ultra|mini|air|lite|elite|classic|se|v\d+|gen\d*|mk\d+|"
                              r"wireless|ups|dc|[a-z])$")
_SUFFIX_WORD_RE = re.compile(r"^[a-z]+\d{0,2}$")
_UNIT_WORDS = frozenset({"w", "wh", "kwh", "watt", "watts", "v", "a", "ah", "mah", "ft", "mm", "in",
                         "lb", "lbs", "kg", "pcs", "pc", "pack", "x", "hz"})


class ModelIndex:
    """The caller's models, indexed for :func:`match` (build once per run)."""

    def __init__(self, records: Iterable[ModelRecord], *,
                 brand_aliases: Optional[Mapping[str, Iterable[str]]] = None,
                 kind_roles: Optional[Mapping[str, str]] = None,
                 capacity_tolerance: float = DEFAULT_CAPACITY_TOLERANCE,
                 variant_words: Optional[Iterable[str]] = None) -> None:
        self.kind_roles = {str(k).lower(): v for k, v in (kind_roles or DEFAULT_KIND_ROLES).items()}
        self.capacity_tolerance = float(capacity_tolerance)
        self._variant_words = frozenset(compact(w) for w in variant_words) if variant_words else None
        self.records: dict[Any, ModelRecord] = {}
        self.roles: dict[Any, str] = {}
        self.brands: dict[str, _Brand] = {}
        self._alias_to_brands: dict[str, set[str]] = {}
        #: Configuration problems found while indexing (texts name ids and config keys only).
        self.problems: list[str] = []
        recs = [r for r in records if r.model_id is not None and compact(r.brand)]
        for r in recs:
            self.records[r.model_id] = r
            self.roles[r.model_id] = self.kind_roles.get(str(r.kind).lower(), ROLE_OTHER)
            key = compact(r.brand)
            self.brands.setdefault(key, _Brand(key=key, name=r.brand))
            self._add_alias(key, key)
            for a in r.brand_aliases:
                self._add_alias(compact(a), key)
        for canonical, aliases in (brand_aliases or {}).items():
            key = compact(canonical)
            if key not in self.brands:
                continue  # a configured maker with no model yet: nothing to match
            for a in _texts(aliases):
                self._add_alias(compact(a), key)
        for alias, keys in self._alias_to_brands.items():
            if len(keys) > 1:
                self.problems.append(f"brand alias {alias!r} names {len(keys)} makers; it resolves to none")
        prefixes: dict[str, set[str]] = {k: {a for a, ks in self._alias_to_brands.items() if k in ks}
                                         for k in self.brands}
        for r in recs:
            b = self.brands[compact(r.brand)]
            is_kit = self.roles[r.model_id] == ROLE_KIT
            names, numbers = designations_of((r.name, *r.aliases), brand_prefixes=prefixes[b.key],
                                             codes_from_parens=not is_kit)
            if is_kit:
                numbers = set()  # a kit's name lists its components' codes, not its own
            numbers |= model_numbers_of(r.model_numbers)
            if self.roles[r.model_id] != ROLE_KIT:
                for d in names:
                    b.designations.setdefault(d, set()).add(r.model_id)
            for n in numbers:
                b.numbers.setdefault(n, set()).add(r.model_id)
        for r in recs:
            if self.roles[r.model_id] == ROLE_KIT:
                self.brands[compact(r.brand)].kits.append(r.model_id)
        for b in self.brands.values():
            for key in (*b.designations, *b.numbers):
                b.prefixes.update(key[:i] for i in range(1, len(key) + 1))
            names = sorted(b.designations)
            for short in names:
                for long in names:
                    rest = long[len(short):]
                    if (long != short and long.startswith(short) and short[-1:].isdigit() != rest[:1].isdigit()
                            and _SUFFIX_WORD_RE.match(rest) and not _MEASURE_WORD_RE.match(rest)
                            and (rest not in GENERIC_WORDS or _VARIANT_WORD_RE.match(rest))):
                        b.variant_words.add(rest)
                        alpha = re.match(r"[a-z]+", rest).group(0)
                        if alpha != rest:
                            b.variant_words.add(alpha)          # 'gen2' -> 'gen' ('Gen 2')
        self._kits: dict[Any, Optional[_KitInfo]] = {}
        for b in self.brands.values():
            for kid in b.kits:
                self._kits[kid] = self._kit_info(b, self.records[kid])

    # -- brands --------------------------------------------------------------------------

    def _add_alias(self, alias: str, key: str) -> None:
        if alias:
            self._alias_to_brands.setdefault(alias, set()).add(key)

    def resolve_brand(self, item_brand: Any) -> Optional[str]:
        """The maker key Amazon's brand field names, or None (unknown / ambiguous)."""
        keys = self._alias_to_brands.get(compact(item_brand)) or set()
        return next(iter(keys)) if len(keys) == 1 else None

    def brand_name(self, key: str) -> str:
        return self.brands[key].name if key in self.brands else ""

    def record(self, model_id: Any) -> Optional[ModelRecord]:
        return self.records.get(model_id)

    def role(self, model_id: Any) -> str:
        return self.roles.get(model_id, ROLE_OTHER)

    # -- occurrences ---------------------------------------------------------------------------

    def _is_variant_word(self, tokens: Sequence[Token], j: int, brand: Optional[_Brand] = None) -> bool:
        t = tokens[j].text
        if brand is not None and t in brand.variant_words:
            return True
        if self._variant_words is not None:
            if t in self._variant_words:
                return True
        elif _VARIANT_WORD_RE.match(t):
            nxt = tokens[j + 1].text if j + 1 < len(tokens) else ""
            # 'x 2' is a quantity, not a model letter
            return not (t == "x" and nxt.isdigit())
        if t.isdigit() and len(t) <= 5:
            nxt = tokens[j + 1] if j + 1 < len(tokens) else None
            return not (nxt is not None and not nxt.hard_before and nxt.text in _UNIT_WORDS)
        return False

    def occurrences(self, brand_key: str, tokens: Sequence[Token]) -> list[_Occurrence]:
        """Every designation / model number of the maker's models in ``tokens``."""
        b = self.brands.get(brand_key)
        if b is None or not tokens:
            return []
        found: dict[tuple[int, int], _Occurrence] = {}
        n = len(tokens)
        for table, is_number in ((b.designations, False), (b.numbers, True)):
            if not table:
                continue
            for i in range(n):
                s = ""
                for j in range(i, n):
                    if j > i and tokens[j].hard_before:
                        break
                    s += tokens[j].text
                    if s not in b.prefixes:
                        break
                    if s in table:
                        span = (i, j + 1)
                        ids = frozenset(table[s])
                        prev = found.get(span)
                        if prev is None:
                            after = j + 1 < n and not tokens[j + 1].hard_before and \
                                self._is_variant_word(tokens, j + 1, b)
                            found[span] = _Occurrence(i, j + 1, ids, is_number, after)
                        else:
                            prev.model_ids = prev.model_ids | ids
                            prev.number = prev.number or is_number
        return sorted(found.values(), key=lambda o: (o.start, -o.end))

    # -- kits ----------------------------------------------------------------------------------

    def _kit_info(self, b: _Brand, kit: ModelRecord) -> Optional[_KitInfo]:
        station = None
        panels: Counter = Counter()
        battery = False
        battery_wh = 0.0
        battery_ids: set = set()
        panel_unknown = False
        for cid, qty in kit.components:
            role = self.roles.get(cid)
            comp = self.records.get(cid)
            if role == ROLE_STATION and station is None:
                station = cid
            elif role == ROLE_PANEL:
                if comp is not None and comp.power_w:
                    panels[int(round(comp.power_w))] += qty
                else:
                    panel_unknown = True
            elif role == ROLE_BATTERY:
                battery = True
                battery_ids.add(cid)
                if comp is not None and comp.capacity_wh:
                    battery_wh += comp.capacity_wh * qty
        text = normalize_text(kit.name)
        tokens = tokenize(kit.name)
        occ = _resolve_nesting(self.occurrences(b.key, tokens))
        if station is None:
            stations = {m for o in occ for m in o.model_ids if self.roles.get(m) == ROLE_STATION}
            if len(stations) == 1:
                station = next(iter(stations))
        if not panels or panel_unknown:
            # Components lack panel ratings: read the kit's own name instead,
            # after the station's designation ('Kit 600W (R600 + 100W Panel)':
            # the 600W is the station's).
            from_name: Counter = Counter()
            station_end = max((tokens[o.end - 1].end for o in occ if station in o.model_ids), default=0)
            for o in occ:
                ids = [m for m in o.model_ids if self.roles.get(m) == ROLE_PANEL]
                if len(ids) == 1 and self.records[ids[0]].power_w:
                    from_name[int(round(self.records[ids[0]].power_w))] += max(
                        _qty_after(tokens, o.end)[0], _qty_before(tokens, o.start)[0])
            for m in _KIT_PANEL_WATTS_RE.finditer(text):
                w = int(m.group("w"))
                if m.start() < station_end or _NOT_PANEL_AFTER_RE.match(text[m.end():m.end() + 24]):
                    continue
                if any(int(round(self.records[x].power_w or 0)) == w for o in occ for x in o.model_ids
                       if self.roles.get(x) == ROLE_PANEL):
                    continue  # already counted through the panel's designation
                q = int(m.group("q1") or m.group("q2") or 1)
                from_name[w] += q
            if from_name:
                panels = from_name
            elif panel_unknown:
                panels = Counter({f"m:{cid}": qty for cid, qty in kit.components
                                  if self.roles.get(cid) == ROLE_PANEL})
        if not battery and (_KIT_BATTERY_RE.search(text) or any(
                self.roles.get(m) == ROLE_BATTERY for o in occ for m in o.model_ids)):
            battery = True
        if station is None:
            return None
        return _KitInfo(station_id=station, panels=dict(panels), battery=battery, battery_wh=battery_wh,
                        battery_ids=frozenset(battery_ids))

    def kit_info(self, kit_id: Any) -> Optional[_KitInfo]:
        return self._kits.get(kit_id)


_QTY_SUFFIX_RE = re.compile(r"(\d{1,2})(?:x|pcs?|pieces?|packs?|pk)")
_QTY_WORDS = frozenset({"x", "pcs", "pc", "pieces", "piece", "pack", "packs", "pk"})
#: Words between a panel's designation and its count ('PS200 Solar Panel x2').
_PANEL_FILLER = frozenset({"solar", "panel", "panels", "module", "modules", "bifacial", "portable", "foldable",
                           "folding", "rigid", "flexible", "monocrystalline", "mono", "blanket", "blankets"})
_Span = Optional[tuple]


def _tok_span(tokens: Sequence[Token], i: int, j: int) -> tuple[int, int]:
    return tokens[i].start, tokens[j].end


def _qty_before(tokens: Sequence[Token], start: int) -> tuple[int, _Span]:
    """'2pcs SP039', '2 x PS100', '2x PS100': the quantity written before a part
    (and its character span, so a bundle can tell which counts it used)."""
    if start >= 1:
        m = re.fullmatch(r"(\d{1,2})(?:x|pcs?|pieces?)", tokens[start - 1].text)
        if m:
            return max(1, min(20, int(m.group(1)))), _tok_span(tokens, start - 1, start - 1)
    if start >= 2 and tokens[start - 1].text in ("x", "pcs", "pc", "pieces") and tokens[start - 2].text.isdigit():
        return max(1, min(20, int(tokens[start - 2].text))), _tok_span(tokens, start - 2, start - 1)
    if start >= 2 and re.fullmatch(r"[1-9]", tokens[start - 1].text) and tokens[start - 2].text in (
            "with", "and", "plus", "includes", "including"):
        return int(tokens[start - 1].text), _tok_span(tokens, start - 1, start - 1)  # 'with 2 SolarSaga 100 Air'
    return 1, None


def _qty_after(tokens: Sequence[Token], end: int) -> tuple[int, _Span]:
    """'x 2', 'x2', '2pcs', '(2 Pack)' after a part, past panel words ('PS200 Solar Panel x2')."""
    k = end
    while k < len(tokens) and k - end < 4 and tokens[k].text in _PANEL_FILLER and (
            k == end or not tokens[k].hard_before):
        k += 1
    for at in ((k, end) if k != end else (end,)):
        if at + 1 < len(tokens) and tokens[at].text == "x" and tokens[at + 1].text.isdigit() \
                and len(tokens[at + 1].text) <= 2:
            return max(1, min(20, int(tokens[at + 1].text))), _tok_span(tokens, at, at + 1)
        if at < len(tokens) and re.fullmatch(r"x\d{1,2}", tokens[at].text):
            return max(1, min(20, int(tokens[at].text[1:]))), _tok_span(tokens, at, at)
        if at < len(tokens):
            m = _QTY_SUFFIX_RE.fullmatch(tokens[at].text)
            if m and not tokens[at].text.endswith("x"):
                return max(1, min(20, int(m.group(1)))), _tok_span(tokens, at, at)
        if at + 1 < len(tokens) and tokens[at].text.isdigit() and len(tokens[at].text) <= 2 \
                and tokens[at + 1].text in _QTY_WORDS and tokens[at + 1].text != "x":
            return max(1, min(20, int(tokens[at].text))), _tok_span(tokens, at, at + 1)
    return 1, None


def _resolve_nesting(occ: Sequence[_Occurrence]) -> list[_Occurrence]:
    """Drop every occurrence whose span lies inside a longer one."""
    out = []
    for o in occ:
        if any(p is not o and p.start <= o.start and o.end <= p.end and (p.end - p.start) > (o.end - o.start)
               for p in occ):
            continue
        out.append(o)
    return out


def _head_end(tokens: Sequence[Token]) -> int:
    for k, t in enumerate(tokens):
        if t.text in _CUE_SINGLE:
            return k
        if k + 1 < len(tokens) and (t.text, tokens[k + 1].text) in _CUE_PAIRS:
            return k
    return len(tokens)


def _cue_mask(tokens: Sequence[Token], occ: Sequence[_Occurrence]) -> list[bool]:
    """True for every token inside a compatibility context ('for DELTA 3 ...').

    A cue covers the rest of the title, except when its own phrase names none
    of the maker's designations ('Solar Generator for Home Use, AC180 ...'):
    then it ends at that phrase's hard break."""
    n = len(tokens)
    mask = [False] * n
    starts = {o.start for o in occ}
    k = 0
    while k < n:
        t = tokens[k].text
        if not (t in _CUE_SINGLE or (k + 1 < n and (t, tokens[k + 1].text) in _CUE_PAIRS)):
            k += 1
            continue
        h = next((j for j in range(k + 1, n) if tokens[j].hard_before), n)
        if h < n and not any(k < s < h for s in starts):
            for j in range(k, h):
                mask[j] = True
            k = h
            continue
        for j in range(k, n):
            mask[j] = True
        break
    return mask


# -- what the listing is: the item noun of the designation's phrase -------------------------

ITEM_ACCESSORY = "accessory"
_ACCESSORY_ITEM_WORDS = frozenset("""
    bag bags case cases cover covers sleeve sleeves pouch pouches cable cables cord cords wire wires harness
    adapter adapters adaptor adaptors connector connectors extension extensions cart carts trolley trolleys
    wheel wheels handle handles remote hub hubs mount mounts bracket brackets stand stands holder holders
    strap straps protector protectors skin skins sticker stickers decal decals accessory accessories part
    parts fuse fuses inlet switch switches charger chargers kit kits replacement replacements organizer
    tray shelf plug plugs dolly
    """.split())
_STATION_ITEM_WORDS = frozenset({"station", "stations", "generator", "generators", "powerhouse", "bank",
                                 "backup"})
_PANEL_ITEM_WORDS = frozenset({"panel", "panels", "module", "modules", "blanket", "blankets", "briefcase",
                               "suitcase"})
_BATTERY_QUALIFIERS = frozenset({"extra", "expansion", "additional", "external", "expandable", "addon"})
#: Words that end the designation's own phrase: the start of a bundled part.
_BUNDLE_CONNECTOR_WORDS = frozenset({"with", "including", "includes", "included"})
_REPLACEMENT_CUES = frozenset({"replacement", "replaces"})
_COMPAT_CUES = frozenset({"compatible", "compatibility", "fits"})


def _item_word(tokens: Sequence[Token], k: int) -> Optional[str]:
    """What tokens[k] says the listing is: a role, ITEM_ACCESSORY, or None."""
    t = tokens[k].text
    prev = tokens[k - 1].text if k > 0 and not tokens[k].hard_before else ""
    nxt = tokens[k + 1].text if k + 1 < len(tokens) and not tokens[k + 1].hard_before else ""
    if t in ("battery", "batteries"):
        if prev in _BATTERY_QUALIFIERS or nxt in ("pack", "packs", "expansion"):
            return ROLE_BATTERY
        return None   # 'LiFePO4 Battery Backup': a station's chemistry, not what is sold
    if t in ("pack", "packs") and prev in ("battery", "batteries", "expansion"):
        return ROLE_BATTERY
    if t == "expansion" and nxt not in ("battery", "batteries", "port", "ports"):
        return ROLE_BATTERY          # 'Battery Pack 1000 Plus Expansion'
    if t in _STATION_ITEM_WORDS:
        return ROLE_STATION
    if t in _PANEL_ITEM_WORDS:
        return ROLE_PANEL
    if t in ("charger", "chargers") and prev == "solar":
        return ROLE_PANEL            # 'Foldable Solar Charger' is a panel
    if t in ("kit", "kits") and prev in ("solar", "generator", "generators", "panel", "panels"):
        return None                  # 'Solar Generator Kit': the bundle check decides
    if t in ("case", "cases") and prev == "in":
        return None                  # 'in case of an outage'
    if t in _ACCESSORY_ITEM_WORDS:
        return ITEM_ACCESSORY
    return None


def _starts_part(tokens: Sequence[Token], title: str, k: int) -> bool:
    """tokens[k] begins a bundled part: after '+' / '&', or it is 'with', 'w/', 'including'."""
    t = tokens[k]
    if t.text in _BUNDLE_CONNECTOR_WORDS or (t.text == "w" and title[t.end:t.end + 1] == "/"):
        return True
    if k > 0 and not t.synthetic:
        gap = title[tokens[k - 1].end:t.start]
        return "+" in gap or "&" in gap
    return False


def _first_part_start(tokens: Sequence[Token], title: str, in_cue: Sequence[bool]) -> int:
    return next((k for k in range(1, len(tokens)) if not in_cue[k] and _starts_part(tokens, title, k)),
                len(tokens))


def _item_kind(tokens: Sequence[Token], title: str, o: _Occurrence, in_cue: Sequence[bool]) -> Optional[str]:
    """What the listing is, read from the designation's own phrase (its last
    item noun); failing that, the nearest phrase after it, then before it,
    never across a bundled part ('with', '+') and never from a compatibility
    context ('for ...')."""
    n = len(tokens)

    def last_kind(ks: Iterable[int]) -> Optional[str]:
        out = None
        for k in ks:
            if not in_cue[k]:
                kind = _item_word(tokens, k)
                # Our own model's name says what it is ('... Solar Panel Suitcase Kit'):
                # an accessory word inside the designation is not evidence.
                if kind is not None and not (kind == ITEM_ACCESSORY and o.start <= k < o.end):
                    out = kind
        return out

    # Phrases: split at hard breaks and at the start of a bundled part.
    bounds = [0] + [k for k in range(1, n) if tokens[k].hard_before or _starts_part(tokens, title, k)] + [n]
    phrases = [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1)]
    own = next(i for i, (a, b) in enumerate(phrases) if a <= o.start < b)
    a, b = phrases[own]
    hi = b
    for k in range(o.end, b):
        if in_cue[k]:
            hi = k
            break
    kind = last_kind(range(a, hi))
    if hi < b:   # a cue inside the designation's phrase
        if tokens[hi].text in _REPLACEMENT_CUES:
            return ITEM_ACCESSORY    # 'AC180 Replacement Cable'
        if tokens[hi].text in _COMPAT_CUES and kind is None:
            return ITEM_ACCESSORY    # 'AC180 Compatible'
    if kind is not None:
        return kind
    if hi < b:
        return None
    for i in range(own + 1, len(phrases)):
        a, b = phrases[i]
        if in_cue[a] or _starts_part(tokens, title, a):
            break
        kind = last_kind(range(a, b))
        if kind is not None:
            return kind
    for i in range(own - 1, -1, -1):
        if _starts_part(tokens, title, phrases[i + 1][0]):
            break
        kind = last_kind(range(*phrases[i]))
        if kind is not None:
            return kind
    return None


#: Item kinds a model of each role may be sold as (None = no item noun at all).
_ITEM_FITS: Mapping[str, frozenset] = {
    ROLE_STATION: frozenset({None, ROLE_STATION}),
    ROLE_KIT: frozenset({None, ROLE_STATION}),
    ROLE_BATTERY: frozenset({None, ROLE_BATTERY, ROLE_STATION}),
    ROLE_PANEL: frozenset({None, ROLE_PANEL}),
}


def _item_reason(role: str, item: Optional[str]) -> str:
    fits = _ITEM_FITS.get(role)
    if fits is None or item in fits:
        return ""
    return REASON_ACCESSORY if item == ITEM_ACCESSORY else REASON_ITEM_KIND


# -- condition and unit counts -------------------------------------------------------------

_CONDITION_RE = re.compile(
    r"\b(?:renewed|refurbished|refurb|reconditioned|remanufactured|pre-?owned|open[- ]box)\b"
    r"|\(\s*used\b|\bused\s*[-:,]?\s*(?:like new|very good|good|acceptable)\b|\bused\s*\)")
_NUM_WORDS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "twin": 2, "double": 2, "dual": 2,
              "pair": 2}
_COUNT_RES = (
    re.compile(r"(?<![\w.])(?P<n>\d{1,2})\s*-?\s*(?:packs?|pk|pcs?|pieces?|units?|count|ct|sets?)\b"),
    re.compile(r"\b(?:set|pack|box|lot|bundle) of (?P<n>\d{1,2}|two|three|four|five|six)\b"),
    re.compile(r"\b(?P<n>twin|double|two|three|four)[- ]?packs?\b"),
    re.compile(r"\b(?P<n>pair) of\b"),
    re.compile(r"(?<![\w.])x\s?(?P<n>\d{1,2})\b(?!\s?(?:w|wh|v|a|ah|kwh)\b)"),
    re.compile(r"(?<![\w.])(?P<n>\d{1,2})\s?x\s?(?=[a-z])(?!(?:ac|usb|dc|outlets?|ports?|output|type)\b)"),
    re.compile(r"(?<![\w.])(?P<n>\d{1,2})\s?x\s?\d{2,3}\s?w(?:atts?)?\b"),
)
_EXPAND_CLAUSE_RE = re.compile(r"expand\w*\s+(?:up\s+)?(?:to|with)\s+[^,;()]*")


def _count_markers(title: str, *, skip: Sequence[tuple[int, int]] = ()) -> list[tuple[int, int, int]]:
    """(start, end, count) for every unit count above one in ``title``."""
    t = _EXPAND_CLAUSE_RE.sub(lambda m: " " * len(m.group(0)), title)
    out = []
    for rx in _COUNT_RES:
        for m in rx.finditer(t):
            raw = m.group("n")
            n = int(raw) if raw.isdigit() else _NUM_WORDS.get(raw, 0)
            if n < 2 or any(a <= m.start() < b for a, b in skip):
                continue
            out.append((m.start(), m.end(), n))
    return out


def _cue_spans(tokens: Sequence[Token], in_cue: Sequence[bool], length: int) -> list[tuple[int, int]]:
    spans = []
    for k, flag in enumerate(in_cue):
        if flag:
            spans.append((tokens[k].start, tokens[k + 1].start if k + 1 < len(tokens) else length))
    return spans


@dataclass
class _BundleEvidence:
    wording: bool = False
    panels: dict = field(default_factory=dict)   # key -> qty or None (unstated)
    battery: bool = False
    battery_ids: set = field(default_factory=set)   # battery models named in the title
    spans: list = field(default_factory=list)    # char spans of panel wattages (not station watts)
    used: list = field(default_factory=list)     # char spans of the counts the evidence read


def _clauses(text: str) -> list[tuple[int, str]]:
    out, pos = [], 0
    for part in _CLAUSE_SPLIT_RE.split(text):
        out.append((pos, part))
        pos += len(part) + 1
    return out


def _bundle_evidence(text: str) -> _BundleEvidence:
    ev = _BundleEvidence()
    for offset, clause in _clauses(text):
        if _EXCLUDED_CLAUSE_RE.search(clause):
            continue
        for m in _PANEL_WATTS_RE.finditer(clause):
            if _NOT_BUNDLE_BEFORE_RE.search(clause[:m.start()]):
                continue
            w = int(m.group("w"))
            q = m.group("q1") or m.group("q2")
            # No count written: 'panel' is one, 'panels' is any number.
            ev.panels[w] = int(q) if q else (None if m.group("noun").endswith("s") else 1)
            ev.spans.append((offset + m.start(), offset + m.end()))
            ev.used.append((offset + m.start(), offset + m.end()))
            ev.wording = True
        for m in _PANEL_WORDING_RE.finditer(clause):
            if not _NOT_BUNDLE_BEFORE_RE.search(clause[:m.start()]):
                ev.wording = True
        for m in _BATTERY_WORDING_RE.finditer(clause):
            if not _NOT_BUNDLE_BEFORE_RE.search(clause[:m.start()]):
                ev.battery = True
                ev.used.append((offset + m.start(), offset + m.end()))
    return ev


def _blank(text: str, spans: Iterable[tuple[int, int]]) -> str:
    """``text`` with ``spans`` replaced by spaces (same length: other spans stay valid)."""
    chars = list(text)
    for a, b in spans:
        for i in range(max(0, a), min(len(chars), b)):
            chars[i] = " "
    return "".join(chars)


# -- matching ------------------------------------------------------------------------------------

def match(item_brand: Any, item_title: Any,
          candidates: "ModelIndex | Iterable[ModelRecord]", *,
          brand_aliases: Optional[Mapping[str, Iterable[str]]] = None) -> MatchResult:
    """Link one listing to one model, or say why not (see the module rules).

    ``candidates`` is a :class:`ModelIndex` (build it once per run) or the
    model records themselves. The result names a model id, a rule id, a
    confidence and a reason code, never any listing text.
    """
    index = candidates if isinstance(candidates, ModelIndex) else ModelIndex(
        candidates, brand_aliases=brand_aliases)
    title = normalize_text(item_title)
    if not title:
        return _no(REASON_NO_TITLE)
    if not compact(item_brand):
        return _no(REASON_BRAND_MISSING)
    brand = index.resolve_brand(item_brand)
    if brand is None:
        return _no(REASON_BRAND_UNKNOWN)
    tokens = tokenize(title)
    all_occ = index.occurrences(brand, tokens)
    in_cue = _cue_mask(tokens, all_occ)
    occ = _resolve_nesting([o for o in all_occ if not any(in_cue[o.start:o.end])])
    if not occ:
        return _no(REASON_NO_DESIGNATION)
    if _CONDITION_RE.search(title):
        return _no(REASON_CONDITION)
    usable = [o for o in occ if not o.variant_after]
    if not usable:
        return _no(REASON_UNKNOWN_VARIANT)
    if any(len(o.model_ids) > 1 for o in usable):
        return _no(REASON_AMBIGUOUS)
    for o in usable:
        # 'Explorer 1000+ SolarSaga 100W': the '+' may be 'Plus' or a connector.
        if tokens[o.end - 1].synthetic and any(p.start == o.end for p in all_occ):
            return _no(REASON_AMBIGUOUS)
    ids = {next(iter(o.model_ids)) for o in usable}
    by_role: dict[str, set] = {}
    for m in ids:
        by_role.setdefault(index.role(m), set()).add(m)
    stations = by_role.get(ROLE_STATION, set())
    ev = _bundle_evidence(title)
    # Panel / battery designations anywhere outside a compatibility context
    # are bundle evidence too ('+ SolarSaga 100W', '+ Battery Pack 2000 Plus').
    for o in _resolve_nesting(all_occ):
        if len(o.model_ids) != 1 or o.variant_after:
            continue
        mid = next(iter(o.model_ids))
        role = index.role(mid)
        if role not in (ROLE_PANEL, ROLE_BATTERY) or (in_cue[o.start] and _cue_before(tokens, o.start)):
            continue
        q_after, span_after = _qty_after(tokens, o.end)
        q_before, span_before = _qty_before(tokens, o.start)
        ev.used.extend(s for s in (span_after, span_before) if s)
        if role == ROLE_PANEL:
            rec = index.record(mid)
            key = int(round(rec.power_w)) if rec and rec.power_w else f"m:{mid}"
            q = max(q_after, q_before)
            plural = any(t.text in ("panels", "modules") for t in tokens[o.end:o.end + 2])
            ev.panels[key] = q if q > 1 else (ev.panels.get(key, None if plural else 1) if key in ev.panels
                                              else (None if plural else 1))
            ev.wording = True
        else:
            ev.battery = True
            ev.battery_ids.add(mid)
    part_start = _first_part_start(tokens, title, in_cue)
    cue_spans = _cue_spans(tokens, in_cue, len(title))
    if len(stations) == 1 and (ev.wording or ev.battery or ev.panels):
        sid = next(iter(stations))
        own = [o for o in usable if sid in o.model_ids]
        if all(o.start > part_start for o in own):
            return _no(REASON_BUNDLE_COMPONENT)
        reason = _item_reason(ROLE_STATION, _item_kind(tokens, title, own[0], in_cue))
        if reason:
            return _no(reason)
        named = [(tokens[o.start].start, tokens[o.end - 1].end) for o in usable]
        # Every unit count in the title must be one the kit check read.
        if any(not any(a <= s < b for a, b in ev.used)
               for s, _e, _n in _count_markers(title, skip=[*cue_spans, *named])):
            return _no(REASON_BUNDLE_UNVERIFIED)
        return _match_bundle(index, brand, sid, ev, title, named=named)
    if len(ids) > 1:
        return _no(REASON_AMBIGUOUS)
    mid = next(iter(ids))
    if all(o.start > part_start for o in usable):
        return _no(REASON_BUNDLE_COMPONENT)   # 'Solar Generator 1000 Plus with 2 x SolarSaga 100 Air'
    role = index.role(mid)
    reason = _item_reason(role, _item_kind(tokens, title, usable[0], in_cue))
    if reason:
        return _no(reason)
    rule = RULE_MODEL_NUMBER if all(o.number for o in usable) else RULE_DESIGNATION
    rec = index.record(mid)
    # A number inside the matched designation ('300W Portable Power Station')
    # is the name, not a claim about the spec.
    named = [(tokens[o.start].start, tokens[o.end - 1].end) for o in usable]
    if role != ROLE_OTHER and _count_markers(title, skip=[*cue_spans, *named]):
        return _no(REASON_MULTI_PACK)
    reason = _contradiction(index, rec, _blank(title, named), exclude=ev.spans)
    if reason:
        return _no(reason)
    return MatchResult(mid, RULE_CONFIDENCE[rule], rule, REASON_MATCHED)


def _cue_before(tokens: Sequence[Token], start: int) -> bool:
    for k in range(max(0, start - 3), start):
        if tokens[k].text in _CUE_SINGLE or tokens[k].text in ("supports", "support", "pair", "pairs",
                                                               "compatible", "works"):
            return True
    return False


def _match_bundle(index: ModelIndex, brand: str, station_id: Any, ev: _BundleEvidence,
                  title: str, *, named: Sequence[tuple[int, int]] = ()) -> MatchResult:
    kits = [k for k in index.brands[brand].kits
            if (info := index.kit_info(k)) is not None and info.station_id == station_id]
    if not kits:
        return _no(REASON_BUNDLE_NO_KIT)
    if ev.wording and not ev.panels:
        # Panel wording without a wattage or a panel model: which kit is unknowable.
        if any(index.kit_info(k).panels for k in kits):
            return _no(REASON_BUNDLE_UNVERIFIED)
    fits = [k for k in kits if _kit_consistent(index.kit_info(k), ev)]
    if not fits:
        return _no(REASON_BUNDLE_NO_KIT)
    station = index.record(station_id)
    plain = _blank(title, named)
    stated = _wh_values(plain)

    def caps(kid: Any) -> list[float]:
        info = index.kit_info(kid)
        out = []
        if station is not None and station.capacity_wh:
            out.append(station.capacity_wh)
            if info.battery_wh:
                out.append(station.capacity_wh + info.battery_wh)
        return out

    if len(fits) > 1 and stated:
        # Two kits on the same station: a stated total capacity may tell them apart.
        fits = [k for k in fits if any(_close(v, c, index.capacity_tolerance) for v in stated for c in caps(k))]
        if not fits:
            return _no(REASON_CAPACITY)
    if len(fits) > 1:
        return _no(REASON_BUNDLE_AMBIGUOUS)
    kid = fits[0]
    kit_caps = caps(kid)
    if kit_caps and stated and not any(_close(v, c, index.capacity_tolerance) for v in stated for c in kit_caps):
        return _no(REASON_CAPACITY)
    if station is not None and station.power_w:
        watts = _w_values(plain, exclude=ev.spans)
        if watts and not any(_close(v, station.power_w, index.capacity_tolerance) for v in watts):
            return _no(REASON_WATTAGE)
    return MatchResult(kid, RULE_CONFIDENCE[RULE_BUNDLE_KIT], RULE_BUNDLE_KIT, REASON_MATCHED)


def _kit_consistent(info: _KitInfo, ev: _BundleEvidence) -> bool:
    if ev.panels:
        if set(ev.panels) != set(info.panels):
            return False
        for key, q in ev.panels.items():
            if q is not None and info.panels.get(key) != q:
                return False
    elif info.panels and not ev.wording:
        return False
    elif ev.wording and not info.panels:
        return False
    if ev.battery_ids and info.battery_ids and not ev.battery_ids <= info.battery_ids:
        return False     # 'with EB300' is not the kit that ships the EB600
    return ev.battery == info.battery


def _contradiction(index: ModelIndex, rec: Optional[ModelRecord], title: str, *,
                   exclude: Sequence[tuple[int, int]] = ()) -> str:
    """``title`` arrives with the matched designation blanked, so a name ending
    in 'Max' ('DELTA 2 Max 1200W') is never read as 'max 1200W input'."""
    if rec is None:
        return ""
    tol = index.capacity_tolerance
    role = index.role(rec.model_id)
    if rec.capacity_wh and role in (ROLE_STATION, ROLE_BATTERY):
        stated = _wh_values(title, exclude=exclude)
        if stated and not any(_close(v, rec.capacity_wh, tol) for v in stated):
            return REASON_CAPACITY
    if rec.power_w and role in (ROLE_STATION, ROLE_PANEL):
        watts = _w_values(title, exclude=exclude) if role == ROLE_STATION else \
            [float(m.group(1).replace(",", "")) for m in _W_RE.finditer(title)
             if not any(a <= m.start() < b for a, b in exclude)]
        if watts and not any(_close(v, rec.power_w, tol) for v in watts):
            return REASON_WATTAGE
    return ""


def search_keywords(index: ModelIndex, model_id: Any, *, max_len: int = 120) -> str:
    """'<maker> <model name>' for a model-first search: our own words only."""
    rec = index.record(model_id)
    if rec is None:
        return ""
    name = _WS_RE.sub(" ", unicodedata.normalize("NFKC", rec.name or "")).strip()
    name = re.sub(r"[()\[\]{}:,;|+]", " ", name)
    brand_words = _WS_RE.sub(" ", unicodedata.normalize("NFKC", rec.brand or "")).strip()
    if name.casefold().startswith(brand_words.casefold() + " "):
        name = name[len(brand_words) + 1:]
    text = f"{rec.brand} {_WS_RE.sub(' ', name).strip()}".strip()
    return text[:max_len].rstrip()
