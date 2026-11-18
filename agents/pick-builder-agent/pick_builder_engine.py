"""Deterministic pick selection: ladder slots, best lists, substitutes.

Pure functions over rows the store loaded; no I/O. The same inputs always
give the same picks: every ordering ends with price, then ASIN, then
product id, and input row order never matters (duplicate compat rows for one
pair are merged by an explicit precedence, not by scan order).

POLICY NEVER LOOSENS
--------------------
Candidates come only from the eligibility pool (the site's eligibility view,
re-checked against the engine's integrity floor). A slot, list position or
substitute that no candidate satisfies stays EMPTY and says why ("no
eligible candidate: usable_wh >= 2400"); no filter is ever dropped or
relaxed to fill it, and a filter that cannot be evaluated (a missing tier
parameter, an unknown subject attribute) empties the slot too.

WHAT IS STORED
--------------
Reasons and the eligibility evidence are written to the site's tables and
outlive the 24h Amazon display window, so they never carry a value that came
from the Amazon API (price, list price, currency, or an attribute the site
lists under ``eligibility.never_store``) nor a number derived from one (a
remaining budget). They carry no candidate counts either, so a row changes
only when the pick or the reason for it changes. The counted trace ("18
eligible; kind = station -> 9; ...") exists only in memory, for ``--plan``
and the run summary.

SELECTORS
---------
filters      [{attr, op, value | expr}]; op in eq ne gt gte lt lte in not_in
             truthy falsy is_null not_null. ``value`` may be a template
             ("{tier.required_wh}", "{list.max_price|500}") resolved from the
             tier / ladder / list parameters or an earlier slot's pick
             ("{slot.station.cont_w}"); ``expr`` is an arithmetic expression
             over the candidate's own attributes ("price * 3"). A candidate
             whose attribute is NULL fails every comparison.
compat       {relation, with: <earlier role>, side: a|b, verdicts,
             include_via_in_budget}: the candidate's model must have a compat
             edge with that slot's pick whose verdict is accepted, that works
             with ONE unit (a slot holds one unit: an edge whose only working
             configurations need several units in series / parallel never
             qualifies, whatever its label), and that carries none of the
             excluded flags (default: ``computed-check-unknown``, a safety
             check that could not run). With include_via_in_budget, a
             candidate whose edge needs an adapter must fit the budget
             together with the cheapest eligible listing of that adapter (and
             is skipped when none is eligible); that adapter money is then
             RESERVED until the via_of slot that buys it, so no slot in
             between can spend it: a kit is never left needing an adapter it
             cannot afford.
via_of       {relation, a_role, b_role}: the candidate's model must be the
             exact cable/adapter (via_model_id) of the edge between two
             earlier picks; a direct connection leaves the slot empty on
             purpose, with status ``not-needed`` (a complete kit, not a gap).
sort         [{key: <attribute or expression>, dir: asc|desc}], NULL last.
count        an int or a template; max_per [{attr, count}] caps repeats;
             one_per_model keeps one listing per model; max_budget_share
             caps one slot's share of the tier budget; budget: kit (default:
             the slot spends the tier's remaining budget) | own (an
             alternative: judged against the tier budget alone, spends
             nothing of the kit's).
reason       a template over the pick's own attributes and the parameters
             (never price / list_price / currency / never_store attributes).
"""
from __future__ import annotations

import bisect
import collections
import functools
import math
import re
import string
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from framework.core import compat_graph as cg

#: Eligibility columns that come from the Amazon API (24h display limit):
#: selectors may filter and sort on them, nothing stored may name them.
AMAZON_FIELDS = frozenset({"price", "list_price", "currency"})

#: Evidence statuses of a slot row.
STATUS_FILLED = "filled"
STATUS_EMPTY = "empty"
STATUS_NOT_NEEDED = "not-needed"

DIRECT_CONNECTION = "direct connection: no adapter needed"

#: A slot is one unit: the compat verdicts that can describe one unit.
SINGLE_UNIT_VERDICTS = (cg.FITS, cg.FITS_WITH_ADAPTER)
MULTI_UNIT_VERDICTS = frozenset({cg.FITS_IN_SERIES, cg.FITS_IN_PARALLEL})
FAIL_VERDICTS = frozenset({cg.OVER_LIMIT, cg.NO_FIT})
#: Most conservative first (the compat_graph order for disagreeing statements).
CONSERVATIVE_ORDER = (cg.NO_FIT, cg.OVER_LIMIT, cg.UNKNOWN, cg.FITS_IN_PARALLEL,
                      cg.FITS_IN_SERIES, cg.FITS_WITH_ADAPTER, cg.FITS)
#: Who decided an edge: an operator outranks a manufacturer outranks the rules.
CONFIDENCE_RANK = {cg.CONF_MANUAL: 0, cg.CONF_MANUFACTURER: 1, cg.CONF_COMPUTED: 2}
DEFAULT_EXCLUDE_FLAGS = ("computed-check-unknown",)

# ---------------------------------------------------------------------------
# Expressions
# ---------------------------------------------------------------------------


class ExprError(ValueError):
    pass


_TOKEN_RE = re.compile(r"\s*(?:(?P<num>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)|(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
                       r"|(?P<op>[-+*/()]))")


class Expr:
    """``usable_wh / price``, ``price * 3``, ``(list_price - price) / list_price``:
    numbers, attribute names, + - * /, parentheses and unary minus. No
    function calls, no attribute access: it is parsed, never eval'd. A
    NULL (or non-numeric) operand, or a division by zero, yields None. A
    bare attribute name returns the raw value (so text attributes sort)."""

    def __init__(self, text: str):
        self.text = str(text or "").strip()
        if not self.text:
            raise ExprError("empty expression")
        self._toks = self._tokenize(self.text)
        self._i = 0
        self.ast = self._expr()
        if self._i != len(self._toks):
            raise ExprError(f"unexpected {self._toks[self._i][1]!r} in {self.text!r}")
        self.names: set[str] = set()
        self._collect(self.ast)

    @staticmethod
    def _tokenize(text: str) -> list[tuple[str, str]]:
        out, pos = [], 0
        while pos < len(text):
            m = _TOKEN_RE.match(text, pos)
            if not m or m.end() == pos:
                if text[pos:].strip() == "":
                    break
                raise ExprError(f"cannot parse {text!r} at {text[pos:pos + 10]!r}")
            pos = m.end()
            for kind in ("num", "name", "op"):
                if m.group(kind) is not None:
                    out.append((kind, m.group(kind)))
                    break
        return out

    def _peek(self) -> Optional[tuple[str, str]]:
        return self._toks[self._i] if self._i < len(self._toks) else None

    def _take(self) -> tuple[str, str]:
        tok = self._peek()
        if tok is None:
            raise ExprError(f"unexpected end of {self.text!r}")
        self._i += 1
        return tok

    def _expr(self):
        node = self._term()
        while (t := self._peek()) and t[0] == "op" and t[1] in "+-":
            self._take()
            node = ("bin", t[1], node, self._term())
        return node

    def _term(self):
        node = self._factor()
        while (t := self._peek()) and t[0] == "op" and t[1] in "*/":
            self._take()
            node = ("bin", t[1], node, self._factor())
        return node

    def _factor(self):
        kind, val = self._take()
        if kind == "num":
            return ("num", float(val))
        if kind == "name":
            return ("name", val)
        if val == "-":
            return ("neg", self._factor())
        if val == "(":
            node = self._expr()
            if self._take() != ("op", ")"):
                raise ExprError(f"missing ')' in {self.text!r}")
            return node
        raise ExprError(f"unexpected {val!r} in {self.text!r}")

    def _collect(self, node) -> None:
        if node[0] == "name":
            self.names.add(node[1])
        elif node[0] == "neg":
            self._collect(node[1])
        elif node[0] == "bin":
            self._collect(node[2])
            self._collect(node[3])

    @property
    def is_name(self) -> bool:
        return self.ast[0] == "name"

    def value(self, lookup: Callable[[str], Any]) -> Any:
        if self.ast[0] == "name":
            return lookup(self.ast[1])
        return self._num(self.ast, lookup)

    def _num(self, node, lookup) -> Optional[float]:
        k = node[0]
        if k == "num":
            return node[1]
        if k == "name":
            return as_number(lookup(node[1]))
        if k == "neg":
            v = self._num(node[1], lookup)
            return None if v is None else -v
        a, b = self._num(node[2], lookup), self._num(node[3], lookup)
        if a is None or b is None:
            return None
        op = node[1]
        if op == "+":
            return a + b
        if op == "-":
            return a - b
        if op == "*":
            return a * b
        return None if b == 0 else a / b


@functools.lru_cache(maxsize=512)
def compile_expr(text: str) -> Expr:
    return Expr(text)


def as_number(v: Any) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None if v is None else float(v)
    if isinstance(v, (int, float)):
        f = float(v)
        return None if math.isnan(f) else f
    try:
        f = float(str(v))
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

_TEMPLATE_RE = re.compile(r"^\{([a-z_]+(?:\.[a-z0-9_]+)+)(?:\|([^{}]*))?\}$")


class MissingParam(Exception):
    def __init__(self, name: str):
        super().__init__(name)
        self.name = name


def template_names(value: Any) -> list[str]:
    vals = value if isinstance(value, list) else [value]
    out = []
    for v in vals:
        if isinstance(v, str):
            m = _TEMPLATE_RE.match(v.strip())
            if m:
                out.append(m.group(1))
    return out


def reason_names(template: Any) -> list[str]:
    """The ``{name}`` / ``{ns.key}`` fields a reason template formats.
    Raises ValueError for a template string.Formatter cannot parse."""
    if not template:
        return []
    return [f for _lit, f, _spec, _conv in string.Formatter().parse(str(template)) if f]


def hidden_name(name: str, hidden: Iterable[str]) -> bool:
    """True when a template field names a value that is never stored
    (``price``, ``slot.station.price``, ``subject.list_price``, ...)."""
    return str(name).split(".")[-1] in set(hidden)


def _lookup_path(ctx: Mapping[str, Any], path: str) -> Any:
    cur: Any = ctx
    for part in path.split("."):
        if isinstance(cur, Product):
            cur = cur.get(part)
        elif isinstance(cur, Mapping):
            cur = cur.get(part)
        else:
            return None
        if cur is None:
            return None
    return cur


def _literal(text: str) -> Any:
    t = text.strip()
    n = as_number(t)
    return n if n is not None else t


def resolve(value: Any, ctx: Mapping[str, Any]) -> Any:
    """A literal, or a whole-string template "{ns.key}" / "{ns.key|default}"
    looked up in ``ctx``. Raises MissingParam when it has no value."""
    if isinstance(value, list):
        return [resolve(v, ctx) for v in value]
    if not isinstance(value, str):
        return value
    m = _TEMPLATE_RE.match(value.strip())
    if not m:
        return value
    got = _lookup_path(ctx, m.group(1))
    if got is None:
        if m.group(2) is not None:
            return _literal(m.group(2))
        raise MissingParam(m.group(1))
    return got


class _SafeFormatter(string.Formatter):
    """Formats only ``{name}`` / ``{ns.key}`` with a format spec; no
    indexing or attribute access beyond the namespace path."""

    def __init__(self, ctx: Mapping[str, Any]):
        super().__init__()
        self.ctx = ctx

    def get_field(self, field_name, args, kwargs):
        if "[" in field_name or not re.fullmatch(r"[a-z_][a-z0-9_]*(?:\.[a-z0-9_]+)*", field_name):
            raise KeyError(field_name)
        v = _lookup_path(self.ctx, field_name)
        if v is None:
            raise KeyError(field_name)
        return v, field_name

    def format_field(self, value, format_spec):
        try:
            return format(value, format_spec)
        except (TypeError, ValueError):
            return format(str(value), "")


def format_reason(template: Optional[str], ctx: Mapping[str, Any]) -> Optional[str]:
    """The filled template, or None when it names a value that is missing
    (a value the context leaves out on purpose, such as a price, is missing)."""
    if not template:
        return None
    try:
        return _SafeFormatter(ctx).format(str(template))[:500]
    except (KeyError, ValueError, IndexError):
        return None


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------

BASE_FIELDS = ("product_id", "asin", "model_id", "kind", "price", "list_price", "currency",
               "category_confidence")


@dataclass(frozen=True)
class Product:
    """One eligible listing (an eligibility-view row)."""
    product_id: Any
    asin: str
    kind: str
    price: float
    model_id: Any = None
    list_price: Optional[float] = None
    currency: Optional[str] = None
    category_confidence: Optional[float] = None
    attrs: Mapping[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return str(self.product_id)

    @property
    def model_key(self) -> str:
        return "" if self.model_id is None else str(self.model_id)

    def get(self, name: str) -> Any:
        if name in BASE_FIELDS:
            return getattr(self, name)
        return self.attrs.get(name)

    def as_ctx(self) -> dict:
        """Every value (selection: filters may compare against an earlier
        pick's price). Never used for anything that is stored."""
        d = dict(self.attrs)
        for k in BASE_FIELDS:
            d[k] = getattr(self, k)
        return d

    def stored_ctx(self, hidden: Iterable[str] = AMAZON_FIELDS) -> dict:
        """The values a stored reason may show: no Amazon field and no
        ``never_store`` attribute."""
        hide = set(hidden) | AMAZON_FIELDS
        return {k: v for k, v in self.as_ctx().items() if k not in hide}


@dataclass(frozen=True)
class Subject:
    """A product that may need substitutes (eligible or not). ``attrs`` are
    the subjects relation's mapped attributes; a mapped attribute wins over
    the base columns of the same name."""
    product_id: Any
    kind: str
    asin: str = ""
    model_id: Any = None
    unbuyable: Optional[bool] = None
    attrs: Mapping[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return str(self.product_id)

    def get(self, name: str) -> Any:
        if name in self.attrs:
            return self.attrs.get(name)
        if name in ("product_id", "kind", "asin", "model_id"):
            return getattr(self, name)
        return None


def integrity_problem(p: Product, cfg: Mapping[str, Any]) -> str:
    """The pricing-integrity floor (reusable-agents CLAUDE.md): never pick a
    null / sub-$1 price, a foreign currency, a list price below the price,
    or a low category confidence. '' = the row passes. A check whose column
    the site did not map cannot run (settings warn about it)."""
    if p.price is None or not math.isfinite(p.price) or p.price < float(cfg.get("min_price", 1.0)):
        return "price"
    cur = cfg.get("currencies")
    if cur and p.currency is not None and str(p.currency).upper() not in {str(c).upper() for c in cur}:
        return "currency"
    if cfg.get("list_price_not_below_price", True) and p.list_price is not None and p.list_price < p.price:
        return "list-price-below-price"
    minc = cfg.get("min_category_confidence")
    if minc is not None and "category_confidence" in cfg.get("_mapped", ()) \
            and (p.category_confidence is None or p.category_confidence < float(minc)):
        return "category-confidence"
    rx = cfg.get("asin_regex")
    if rx and not re.match(str(rx), p.asin or ""):
        return "asin"
    return ""


# ---------------------------------------------------------------------------
# Compat index
# ---------------------------------------------------------------------------

def _int(v: Any) -> Optional[int]:
    if isinstance(v, bool) or v is None:
        return None
    try:
        i = int(v)
    except (TypeError, ValueError):
        return None
    return i if i >= 1 else None


def single_unit_works(verdict: str, config: Any) -> bool:
    """Does ONE unit of the B model work, per a stored compat_graph edge?

    ``fits`` says so by definition. ``fits_in_series`` / ``fits_in_parallel``
    say it does not. ``fits_with_adapter`` (the label wins over the
    series / parallel ones) works with one unit only when its evaluated
    configuration of 1 x 1 fits, or the configuration it stores is a single
    unit; with neither in its config it is taken at its word (a statement
    about one unit). Anything else is not a fit."""
    if verdict == cg.FITS:
        return True
    if verdict in MULTI_UNIT_VERDICTS or verdict not in cg.FIT_VERDICTS:
        return False
    cfg = config if isinstance(config, Mapping) else {}
    opts = cfg.get("options")
    if isinstance(opts, list):
        for o in opts:
            if isinstance(o, Mapping) and _int(o.get("series")) == 1 and _int(o.get("parallel")) == 1 \
                    and o.get("outcome") == cg.FITS:
                return True
    s, p = _int(cfg.get("series")), _int(cfg.get("parallel"))
    if s is not None or p is not None:
        return (s or 1) * (p or 1) == 1
    return not isinstance(opts, list)


@dataclass(frozen=True)
class CompatEdge:
    """One pair's edge as the selectors see it."""
    verdict: str
    via: Any = None
    single_unit: bool = False
    flags: tuple[str, ...] = ()
    confidence: str = ""
    rule_id: str = ""

    def precedence(self) -> tuple:
        """Smallest wins: any failure first (a safety floor no statement
        overrides), then an operator over a manufacturer over the rules,
        then the most conservative verdict, then rule id and via (ties)."""
        order = CONSERVATIVE_ORDER.index(self.verdict) if self.verdict in CONSERVATIVE_ORDER \
            else len(CONSERVATIVE_ORDER)
        return (0 if self.verdict in FAIL_VERDICTS else 1, CONFIDENCE_RANK.get(self.confidence, 3),
                order, self.rule_id, "" if self.via is None else str(self.via))


def edge_from_row(r: Mapping[str, Any]) -> CompatEdge:
    cfg = r.get("config")
    if not isinstance(cfg, Mapping):
        cfg = {}
    flags = cfg.get("flags")
    verdict = str(r.get("verdict") or "")
    return CompatEdge(verdict=verdict, via=r.get("via_model_id"),
                      single_unit=single_unit_works(verdict, cfg),
                      flags=tuple(sorted(str(f) for f in flags)) if isinstance(flags, list) else (),
                      confidence=str(r.get("confidence") or ""), rule_id=str(r.get("rule_id") or ""))


class CompatIndex:
    """compat_edges rows by (relation, a model, b model). Several rows of one
    pair (one per rule id: a computed row and an operator's, two rulesets)
    are merged by :meth:`CompatEdge.precedence`, so the result never depends
    on the order the rows were read in."""

    def __init__(self, rows: Iterable[Mapping[str, Any]] = ()):
        grouped: dict[tuple[str, str, str], list[CompatEdge]] = collections.defaultdict(list)
        for r in rows:
            key = (str(r.get("relation")), str(r.get("a_model_id")), str(r.get("b_model_id")))
            grouped[key].append(edge_from_row(r))
        self.edges: dict[tuple[str, str, str], CompatEdge] = {
            k: min(v, key=CompatEdge.precedence) for k, v in grouped.items()}
        self.duplicate_pairs = sum(1 for v in grouped.values() if len(v) > 1)

    def edge(self, relation: str, a: Any, b: Any) -> Optional[CompatEdge]:
        return self.edges.get((relation, str(a), str(b)))

    def verdict(self, relation: str, a: Any, b: Any) -> Optional[str]:
        e = self.edge(relation, a, b)
        return e.verdict if e else None

    def via(self, relation: str, a: Any, b: Any) -> tuple[bool, Any]:
        e = self.edge(relation, a, b)
        return (False, None) if e is None else (True, e.via)

    def signature(self) -> list:
        return sorted([list(k) + [e.verdict, None if e.via is None else str(e.via), e.single_unit,
                                  list(e.flags)] for k, e in self.edges.items()])


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

_OP_TEXT = {"eq": "=", "ne": "!=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<=", "in": "in",
            "not_in": "not in", "within_pct": "within ±{v}% of", "gte_subject": ">= subject",
            "lte_subject": "<= subject", "eq_subject": "= subject"}


def _cmp(op: str, left: Any, right: Any) -> bool:
    if op in ("truthy", "falsy"):
        t = bool(left) if not isinstance(left, str) else left.strip().lower() not in ("", "false", "0", "no")
        return t if op == "truthy" else (left is not None and not t)
    if op == "is_null":
        return left is None
    if op == "not_null":
        return left is not None
    if left is None:
        return False
    if op in ("in", "not_in"):
        vals = right if isinstance(right, (list, tuple, set)) else [right]
        hit = any(_eq(left, v) for v in vals)
        return hit if op == "in" else not hit
    if op in ("eq", "ne"):
        same = _eq(left, right)
        return same if op == "eq" else not same
    a, b = as_number(left), as_number(right)
    if a is None or b is None:
        return False
    eps = 1e-9
    return {"gt": a > b + eps, "gte": a >= b - eps, "lt": a < b - eps, "lte": a <= b + eps}[op]


def _eq(a: Any, b: Any) -> bool:
    na, nb = as_number(a), as_number(b)
    if na is not None and nb is not None and not isinstance(a, str) and not isinstance(b, str):
        return abs(na - nb) <= 1e-9
    return str(a) == str(b)


def describe_filter(f: Mapping[str, Any], resolved: Any, hidden: Iterable[str] = AMAZON_FIELDS) -> str:
    """'usable_wh >= 2400'. A value templated from a never-stored field
    (``{slot.station.price}``) is shown as the template, not the number."""
    op = str(f.get("op"))
    if op in ("truthy", "falsy", "is_null", "not_null"):
        return f"{f.get('attr')} {op.replace('_', ' ')}"
    if "expr" in f:
        rhs = str(f.get("expr"))
    else:
        raw = f.get("value")
        names = template_names(raw)
        if any(hidden_name(n, hidden) for n in names):
            rhs = _fmt(raw)
        else:
            rhs = _fmt(resolved)
    text = _OP_TEXT.get(op, op).replace("{v}", _fmt(f.get("value")))
    if op == "within_pct":
        return f"{f.get('attr')} {text} the subject's".strip()
    if op in ("gte_subject", "lte_subject", "eq_subject"):
        return f"{f.get('attr')} {text}".strip()
    return f"{f.get('attr')} {text} {rhs}".strip()


def _fmt(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:g}"
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_fmt(x) for x in v) + "]"
    return str(v)


def tie_key(p: Product) -> tuple:
    return (p.price if p.price is not None else float("inf"), p.asin or "", str(p.product_id))


def sort_products(products: Iterable[Product], sort: Sequence[Mapping[str, Any]],
                  first: Optional[Callable[[Product], Any]] = None) -> list[Product]:
    """Deterministic: the configured keys (NULL last in either direction),
    then price, ASIN, product id."""
    exprs = [(compile_expr(str(s.get("key"))), str(s.get("dir", "asc"))) for s in sort or []]

    def cmp(a: Product, b: Product) -> int:
        if first is not None:
            fa, fb = first(a), first(b)
            if fa != fb:
                return -1 if fa < fb else 1
        for e, d in exprs:
            va, vb = e.value(a.get), e.value(b.get)
            if va is None and vb is None:
                continue
            if va is None:
                return 1
            if vb is None:
                return -1
            if va == vb:
                continue
            try:
                less = va < vb
            except TypeError:
                less = str(va) < str(vb)
            r = -1 if less else 1
            return r if d == "asc" else -r
        ta, tb = tie_key(a), tie_key(b)
        return 0 if ta == tb else (-1 if ta < tb else 1)

    return sorted(products, key=functools.cmp_to_key(cmp))


@dataclass
class Selection:
    """``trace``: (check, candidates left) pairs, in memory only.
    ``empty_reason``: the stored reason when nothing was picked (no counts,
    no prices). ``status``: empty | not-needed. ``extra``: per picked
    product, the price of the adapter its compat edge needs."""
    picks: list[Product]
    trace: list[tuple[str, int]]
    empty_reason: str = ""
    status: str = STATUS_EMPTY
    extra: dict[str, float] = field(default_factory=dict)

    @property
    def checks(self) -> list[str]:
        """The checks a pick passed, without the counts."""
        return [d for d, _n in self.trace[1:]]

    @property
    def trace_text(self) -> str:
        return _trace_text(self.trace)


def _trace_text(trace: Sequence[tuple[str, int]]) -> str:
    if not trace:
        return ""
    head = f"{trace[0][1]} {trace[0][0]}"
    rest = "; ".join(f"{d} -> {n}" for d, n in trace[1:])
    return f"{head}; {rest}" if rest else head


def _stopped_at(trace: Sequence[tuple[str, int]]) -> str:
    """The first check that left no candidate (stable wording, no counts)."""
    if not trace or trace[0][1] == 0:
        return "nothing in the eligibility view"
    for desc, n in trace[1:]:
        if n == 0:
            return desc
    return "the diversity caps"


def _budget_text(share: Optional[float], with_adapter: bool, own: bool) -> str:
    what = "price (+ its adapter)" if with_adapter else "price"
    base = "the tier budget" if own else "the tier's remaining budget"
    if share is not None:
        return f"{what} within {_fmt(round(float(share) * 100, 4))}% of the tier budget" + \
            ("" if own else " and the remaining budget")
    return f"{what} within {base}"


def select(pool: Sequence[Product], spec: Mapping[str, Any], ctx: Mapping[str, Any], *,
           exclude: Iterable[str] = (), picks: Optional[Mapping[str, Product]] = None,
           compat: Optional[CompatIndex] = None, budget_left: Optional[float] = None,
           tier_budget: Optional[float] = None, count: int = 1,
           fit_verdicts: Sequence[str] = SINGLE_UNIT_VERDICTS,
           exclude_flags: Sequence[str] = DEFAULT_EXCLUDE_FLAGS,
           hidden: Iterable[str] = AMAZON_FIELDS, cumulative: bool = True) -> Selection:
    """Run one selector over the pool. Never relaxes a filter.
    ``cumulative`` False: every pick is judged against the budget alone
    (alternatives), not against what earlier picks of the slot spent."""
    picks = picks or {}
    hidden = set(hidden) | AMAZON_FIELDS
    excluded = set(exclude)
    cands = [p for p in pool if p.key not in excluded]
    trace: list[tuple[str, int]] = [("eligible", len(cands))]
    for f in spec.get("filters") or []:
        op = str(f.get("op"))
        try:
            rhs = None if "expr" in f else resolve(f.get("value"), ctx)
        except MissingParam as e:
            return Selection([], trace, f"missing parameter {e.name}: slot left empty")
        if "expr" in f:
            ex = compile_expr(str(f["expr"]))
            cands = [p for p in cands if _cmp(op, p.get(str(f["attr"])), ex.value(p.get))]
        else:
            cands = [p for p in cands if _cmp(op, p.get(str(f["attr"])), rhs)]
        trace.append((describe_filter(f, rhs, hidden), len(cands)))
    dep = spec.get("compat")
    extra: dict[str, float] = {}       # candidate key -> price of the adapter its edge needs
    if dep:
        role = str(dep.get("with"))
        anchor = picks.get(role)
        if anchor is None:
            return Selection([], trace, f"depends on the {role} slot, which is empty")
        rel = str(dep.get("relation"))
        allowed = set(dep.get("verdicts") or fit_verdicts)
        flags_out = set(exclude_flags)
        side = str(dep.get("side", "b"))
        idx = compat or CompatIndex()

        def ends(p: Product) -> tuple[Any, Any]:
            return (anchor.model_id, p.model_id) if side == "b" else (p.model_id, anchor.model_id)

        def fits(p: Product) -> bool:
            e = idx.edge(rel, *ends(p))
            return (e is not None and e.verdict in allowed and e.single_unit
                    and not (flags_out & set(e.flags)))

        cands = [p for p in cands if fits(p)]
        label = f"{rel} the {role} pick ({'/'.join(sorted(allowed))}, one unit"
        label += f", not {'/'.join(sorted(flags_out))})" if flags_out else ")"
        trace.append((label, len(cands)))
        if dep.get("include_via_in_budget") and budget_left is not None:
            # A pick whose connection needs an adapter is only complete with
            # that adapter: it must fit the budget together with the cheapest
            # eligible listing of it, and it cannot be picked when none is
            # eligible (the kit could not be bought whole).
            cheapest: dict[str, float] = {}
            for p in pool:
                if p.model_key and p.key not in excluded:
                    cheapest[p.model_key] = min(cheapest.get(p.model_key, float("inf")), p.price)
            kept = []
            for p in cands:
                _found, via = idx.via(rel, *ends(p))
                if via is None:
                    kept.append(p)
                elif str(via) in cheapest:
                    extra[p.key] = cheapest[str(via)]
                    kept.append(p)
            cands = kept
            trace.append(("its adapter, when one is needed, is eligible", len(cands)))
    via = spec.get("via_of")
    if via:
        a, b = picks.get(str(via.get("a_role"))), picks.get(str(via.get("b_role")))
        if a is None or b is None:
            missing = via.get("a_role") if a is None else via.get("b_role")
            return Selection([], trace, f"depends on the {missing} slot, which is empty")
        found, via_id = (compat or CompatIndex()).via(str(via.get("relation")), a.model_id, b.model_id)
        if not found:
            return Selection([], trace, f"no {via.get('relation')} edge between the "
                                        f"{via.get('a_role')} and {via.get('b_role')} picks")
        if via_id is None:
            return Selection([], trace, DIRECT_CONNECTION, status=STATUS_NOT_NEEDED)
        cands = [p for p in cands if str(p.model_id) == str(via_id)]
        trace.append((f"model {via_id} (the documented adapter)", len(cands)))
    limit = None
    if budget_left is not None:
        limit = budget_left
        share = spec.get("max_budget_share")
        if share is not None and tier_budget is not None:
            limit = min(limit, float(share) * float(tier_budget))
        cands = [p for p in cands if p.price + extra.get(p.key, 0.0) <= limit + 1e-9]
        trace.append((_budget_text(share if tier_budget is not None else None, bool(extra),
                                   not cumulative), len(cands)))
    ordered = sort_products(cands, spec.get("sort") or [])
    caps = [(str(m["attr"]), int(m["count"])) for m in spec.get("max_per") or []]
    one_model = bool(spec.get("one_per_model", True))
    seen_models: set[str] = set()
    cap_counts: dict[tuple[str, str], int] = collections.Counter()
    out: list[Product] = []
    running = limit
    for p in ordered:
        if len(out) >= count:
            break
        if one_model and p.model_key and p.model_key in seen_models:
            continue
        if any(cap_counts[(a, str(p.get(a)))] >= n for a, n in caps):
            continue
        cost = p.price + extra.get(p.key, 0.0)
        if running is not None and cost > running + 1e-9:
            continue
        out.append(p)
        if p.model_key:
            seen_models.add(p.model_key)
        for a, _n in caps:
            cap_counts[(a, str(p.get(a)))] += 1
        if running is not None and cumulative:
            running -= cost
    empty = "" if out else f"no eligible candidate: {_stopped_at(trace)}"
    return Selection(out, trace, empty, extra={p.key: extra[p.key] for p in out if p.key in extra})


def default_reason(spec: Mapping[str, Any], sel: Selection, rank: int) -> str:
    """Stable wording (no candidate counts, no prices): a reason that changed
    with every refresh would rewrite the row every run."""
    keys = [str(s.get("key")) for s in spec.get("sort") or []]
    by = f" by {', '.join(keys)}" if keys else " by price"
    return f"#{rank} eligible match{by}"


def filter_texts(sel: Selection) -> list[str]:
    """The checks a pick passed, without the counts."""
    return sel.checks


# ---------------------------------------------------------------------------
# Ladders, lists, substitutes
# ---------------------------------------------------------------------------

@dataclass
class Output:
    """One slot / list position / substitute: product None = left empty.
    ``trace`` (the counted trace) is for --plan only and never stored."""
    group: Any            # tier id | list id | subject product id
    role: str             # ladder role ('' for lists and substitutes)
    rank: int
    product: Optional[Product]
    reason: str
    evidence: dict = field(default_factory=dict)
    trace: str = ""

    def row(self) -> tuple:
        return (str(self.group), self.role, int(self.rank),
                None if self.product is None else str(self.product.product_id), self.reason or "")


def _count(spec: Mapping[str, Any], ctx: Mapping[str, Any], default: int) -> int:
    raw = spec.get("count", default)
    try:
        v = resolve(raw, ctx)
    except MissingParam:
        return default
    n = as_number(v)
    return max(0, int(n)) if n is not None else default


def _reason_ctx(ctx: Mapping[str, Any], p: Product, hidden: Iterable[str]) -> dict:
    """The context a stored reason is formatted with: parameters, the pick's
    and earlier picks' stored values; no Amazon field, no never_store one."""
    slots = {r: {k: v for k, v in dict(vals).items() if not hidden_name(k, hidden)}
             for r, vals in (ctx.get("slot") or {}).items()}
    out = {k: v for k, v in ctx.items() if k != "slot"}
    out["slot"] = slots
    out.update(p.stored_ctx(hidden))
    return out


def fill_tier(tier: Mapping[str, Any], ladder: Mapping[str, Any], plan: Sequence[Mapping[str, Any]],
              pool: Sequence[Product], *, compat: Optional[CompatIndex], enforce_budget: bool,
              fit_verdicts: Sequence[str] = SINGLE_UNIT_VERDICTS,
              exclude_flags: Sequence[str] = DEFAULT_EXCLUDE_FLAGS, view: str,
              hidden: Iterable[str] = AMAZON_FIELDS) -> list[Output]:
    """One tier from its plan, in plan order. Kit slots spend the tier's
    remaining budget; ``budget: own`` slots (alternatives) are judged against
    the tier budget alone. Adapter money a compat pick needs is reserved
    until the via_of slot of that edge runs (released to it), so a slot in
    between cannot spend it; an adapter no slot buys stays reserved."""
    hidden = set(hidden) | AMAZON_FIELDS
    params = dict(tier.get("params") or {})
    if tier.get("budget") is not None:
        params.setdefault("budget", tier.get("budget"))
    ctx: dict[str, Any] = {"tier": params, "ladder": dict(ladder), "slot": {}}
    budget = as_number(params.get("budget"))
    picks: dict[str, Product] = {}
    used: set[str] = set()
    spent = 0.0
    reserved: dict[tuple[str, str, str], float] = {}
    out: list[Output] = []
    for slot in plan:
        role = str(slot["role"])
        n = _count(slot, ctx, 1)
        own = str(slot.get("budget", "kit")) == "own"
        if enforce_budget and budget is None:
            for rank in range(1, n + 1):
                out.append(Output(tier["id"], role, rank, None, "the tier has no budget: slot left empty",
                                  {"status": STATUS_EMPTY, "view": view}))
            continue
        via = slot.get("via_of")
        via_key = (str(via.get("relation")), str(via.get("a_role")), str(via.get("b_role"))) \
            if isinstance(via, Mapping) else None
        released = reserved.pop(via_key, 0.0) if via_key is not None and not own else 0.0
        if not enforce_budget:
            left = None
        elif own:
            left = budget
        else:
            left = budget - spent - sum(reserved.values())
        sel = select(pool, slot, ctx, exclude=used, picks=picks, compat=compat, budget_left=left,
                     tier_budget=budget if enforce_budget else None, count=n, fit_verdicts=fit_verdicts,
                     exclude_flags=exclude_flags, hidden=hidden, cumulative=not own)
        if released and not sel.picks:
            spent += released          # the adapter is still needed: its money stays committed
        for rank in range(1, n + 1):
            if rank <= len(sel.picks):
                p = sel.picks[rank - 1]
                used.add(p.key)
                if not own:
                    spent += p.price
                    adapter = sel.extra.get(p.key)
                    if adapter:
                        dep = slot.get("compat") or {}
                        with_role = str(dep.get("with"))
                        key = ((str(dep.get("relation")), with_role, role) if str(dep.get("side", "b")) == "b"
                               else (str(dep.get("relation")), role, with_role))
                        if rank == 1:
                            reserved[key] = reserved.get(key, 0.0) + adapter
                        else:
                            spent += adapter
                if rank == 1:
                    picks[role] = p
                    ctx["slot"][role] = p.as_ctx()
                reason = format_reason(slot.get("reason"), _reason_ctx(ctx, p, hidden)) \
                    or default_reason(slot, sel, rank)
                out.append(Output(tier["id"], role, rank, p, reason,
                                  {"status": STATUS_FILLED, "view": view, "checks": sel.checks},
                                  trace=sel.trace_text))
            elif sel.status == STATUS_NOT_NEEDED:
                out.append(Output(tier["id"], role, rank, None, sel.empty_reason,
                                  {"status": STATUS_NOT_NEEDED, "view": view}, trace=sel.trace_text))
            else:
                why = sel.empty_reason or f"only {len(sel.picks)} of {n} slot(s) could be filled"
                out.append(Output(tier["id"], role, rank, None, why,
                                  {"status": STATUS_EMPTY, "view": view}, trace=sel.trace_text))
    return out


@dataclass
class ListFill:
    """A list's picks, the positions it asked for, and why it is short."""
    outputs: list[Output]
    size: int
    reason: str = ""
    trace: str = ""

    @property
    def short(self) -> bool:
        return len(self.outputs) < self.size


def fill_list(lst: Mapping[str, Any], spec: Mapping[str, Any], pool: Sequence[Product], *,
              default_size: int = 10, hidden: Iterable[str] = AMAZON_FIELDS) -> ListFill:
    hidden = set(hidden) | AMAZON_FIELDS
    params = dict(lst.get("params") or {})
    ctx = {"list": {**params, "slug": lst.get("slug"), "kind": lst.get("kind")}}
    n = _count(spec, ctx, int(as_number(params.get("size")) or default_size))
    sel = select(pool, spec, ctx, count=n, hidden=hidden)
    out = []
    for rank, p in enumerate(sel.picks, start=1):
        reason = format_reason(spec.get("reason"), {**ctx, **p.stored_ctx(hidden)}) \
            or default_reason(spec, sel, rank)
        out.append(Output(lst["id"], "", rank, p, reason))
    reason = ""
    if not sel.picks:
        reason = sel.empty_reason
    elif len(sel.picks) < n:
        reason = f"only {len(sel.picks)} of {n} position(s) could be filled"
    return ListFill(out, n, reason, sel.trace_text)


def list_size(lst: Mapping[str, Any], spec: Mapping[str, Any], default_size: int = 10) -> int:
    params = dict(lst.get("params") or {})
    ctx = {"list": {**params, "slug": lst.get("slug"), "kind": lst.get("kind")}}
    return _count(spec, ctx, int(as_number(params.get("size")) or default_size))


class SubstituteIndex:
    """Pool rows grouped by kind, with sorted views per numeric attribute so
    a within_pct constraint is a bisect, not a scan."""

    def __init__(self, pool: Sequence[Product]):
        self.by_kind: dict[str, list[Product]] = collections.defaultdict(list)
        for p in pool:
            self.by_kind[p.kind].append(p)
        self._sorted: dict[tuple[str, str], tuple[list[float], list[Product]]] = {}

    def window(self, kind: Optional[str], attr: str, lo: float, hi: float) -> list[Product]:
        key = (kind or "*", attr)
        if key not in self._sorted:
            rows = self.by_kind.get(kind, []) if kind is not None else [p for v in self.by_kind.values() for p in v]
            vals = sorted(((as_number(p.get(attr)), p) for p in rows if as_number(p.get(attr)) is not None),
                          key=lambda t: (t[0], tie_key(t[1])))
            self._sorted[key] = ([v for v, _ in vals], [p for _, p in vals])
        values, prods = self._sorted[key]
        return prods[bisect.bisect_left(values, lo - 1e-9):bisect.bisect_right(values, hi + 1e-9)]

    def all(self, kind: Optional[str]) -> list[Product]:
        if kind is None:
            return [p for v in self.by_kind.values() for p in v]
        return list(self.by_kind.get(kind, []))


def build_substitutes(subjects: Sequence[Subject], pool: Sequence[Product],
                      rules: Mapping[str, Mapping[str, Any]], *, max_per_subject: int = 3,
                      same_model: str = "prefer", one_per_model: bool = True,
                      hidden: Iterable[str] = AMAZON_FIELDS
                      ) -> tuple[dict[str, list[Output]], dict[str, str]]:
    """{subject id: substitutes} and {subject id: why none} for subjects of
    a kind with a rule. A constraint the subject cannot be measured against
    (its attribute is NULL) gives that subject no substitutes: nothing beats
    a near-miss."""
    hidden = set(hidden) | AMAZON_FIELDS
    idx = SubstituteIndex(pool)
    out: dict[str, list[Output]] = {}
    why_none: dict[str, str] = {}
    for subj in sorted(subjects, key=lambda s: s.key):
        rule = rules.get(subj.kind)
        if rule is None:
            continue
        kind = subj.kind if rule.get("match_kind", True) else None
        constraints = list(rule.get("constraints") or [])
        window_c = next((c for c in constraints if c.get("op") == "within_pct"), None)
        reason_missing = ""
        if window_c is not None:
            s_val = as_number(subj.get(str(window_c["attr"])))
            if s_val is None:
                why_none[subj.key] = f"subject has no {window_c['attr']}"
                out[subj.key] = []
                continue
            pct = float(window_c["value"]) / 100.0
            lo, hi = sorted((s_val * (1 - pct), s_val * (1 + pct)))
            cands = idx.window(kind, str(window_c["attr"]), lo, hi)
        else:
            cands = idx.all(kind)
        cands = [p for p in cands if p.key != subj.key]
        if same_model == "exclude" and subj.model_id is not None:
            cands = [p for p in cands if str(p.model_id) != str(subj.model_id)]
        ok: list[Product] = []
        for p in cands:
            good = True
            for c in constraints:
                op = str(c.get("op"))
                attr = str(c.get("attr"))
                if op == "within_pct":
                    s, v = as_number(subj.get(attr)), as_number(p.get(attr))
                    if s is None or v is None or abs(v - s) > abs(s) * float(c["value"]) / 100.0 + 1e-9:
                        good = False
                elif op in ("gte_subject", "lte_subject", "eq_subject"):
                    s, v = subj.get(attr), p.get(attr)
                    if s is None:
                        reason_missing = f"subject has no {attr}"
                        good = False
                    else:
                        good = _cmp({"gte_subject": "gte", "lte_subject": "lte", "eq_subject": "eq"}[op], v, s)
                else:
                    try:
                        rhs = None if "expr" in c else resolve(c.get("value"), {"subject": subj.attrs})
                    except MissingParam as e:
                        reason_missing = f"missing parameter {e.name}"
                        good = False
                        break
                    if "expr" in c:
                        rhs = compile_expr(str(c["expr"])).value(p.get)
                    good = _cmp(op, p.get(attr), rhs)
                if not good:
                    break
            if good:
                ok.append(p)
        if reason_missing and not ok:
            why_none[subj.key] = reason_missing
            out[subj.key] = []
            continue
        first = None
        if same_model == "prefer" and subj.model_id is not None:
            first = lambda p, m=str(subj.model_id): 0 if str(p.model_id) == m else 1  # noqa: E731
        ordered = sort_products(ok, rule.get("sort") or [{"key": "price", "dir": "asc"}], first=first)
        chosen: list[Product] = []
        models: set[str] = set()
        for p in ordered:
            if len(chosen) >= max_per_subject:
                break
            if one_per_model and p.model_key and p.model_key in models:
                continue
            chosen.append(p)
            if p.model_key:
                models.add(p.model_key)
        rows = []
        subject_ctx = {k: v for k, v in subj.attrs.items() if k not in hidden}
        for rank, p in enumerate(chosen, start=1):
            same = subj.model_id is not None and str(p.model_id) == str(subj.model_id)
            ctx = {**p.stored_ctx(hidden), "subject": subject_ctx}
            reason = ("same model, another listing" if same else
                      format_reason(rule.get("reason"), ctx) or
                      "; ".join(describe_filter(c, c.get("value"), hidden) for c in constraints) or "same kind")
            rows.append(Output(subj.product_id, "", rank, p, reason))
        out[subj.key] = rows
        if not rows:
            why_none[subj.key] = "no eligible listing matches"
    return out, why_none
