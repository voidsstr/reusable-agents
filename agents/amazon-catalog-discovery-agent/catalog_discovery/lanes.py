"""Lane classifier: which of the site's own lanes a listing belongs to.

A *lane* is our product class (a site's category, e.g. 'station' or 'filter').
Discovery persists the ASIN with that label and nothing Amazon wrote; the
title, price and browse nodes below are read in memory, once, to decide it.

THE RULES (ported from the sizing run's proven ``classify_lane``)
------------------------------------------------------------------
1. A global reject pattern anywhere in the title rejects the listing.
2. Lanes are tried in config order, which is precedence. For each lane:

   * positive evidence, per ``match``: the title rules (every ``include_all``
     and at least one ``include_any``), a browse-node hit (the listing's
     nodes intersect the lane's ``browse_nodes``), both, or either;
   * then ``exclude`` patterns, ``context_required``, ``min_price`` and the
     ``measures`` ranges (a number read from the title, e.g. a capacity).

   Failing any of these moves the listing on to the NEXT lane, so a $15
   'solar panel extension cable' still lands in a cable lane after a panel
   lane's price floor turned it away.
3. A listing no lane accepts is 'off-lane'. A listing whose lane is held back
   (inactive phase, or disabled) is reported as 'lane-held-back': precedence
   still sees held-back lanes, so a phase-2 product is never mislabelled as a
   launch lane's look-alike.

The browse-node evidence also sets the label's confidence: title and node
agree > title only > node only (values from the config).

Nothing returned here carries the title: decisions name rule ids and lane
ids only, so they can be logged and stored.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Optional

from .config import DiscoveryConfig, Lane, MeasureRule, MeasureSpec, PatternRule

OK = "ok"
GLOBAL_REJECT = "global-reject"
OFF_LANE = "off-lane"
HELD_BACK = "lane-held-back"

_WS_RE = re.compile(r"\s+")
_COMMA_THOUSANDS_RE = re.compile(r"^\d{1,3}(,\d{3})+(\.\d+)?$")
_DOT_THOUSANDS_RE = re.compile(r"^\d\.\d{3}$")


def normalize_title(text: Optional[str]) -> str:
    """NFKC (folds non-breaking spaces, full-width digits) + single spaces."""
    return _WS_RE.sub(" ", unicodedata.normalize("NFKC", text or "")).strip()


def _number(raw: str, *, dot_thousands: bool) -> Optional[float]:
    text = raw.strip()
    if _COMMA_THOUSANDS_RE.match(text):
        text = text.replace(",", "")
    elif dot_thousands and _DOT_THOUSANDS_RE.match(text):
        text = text.replace(".", "")
    else:
        text = text.replace(",", ".") if text.count(",") == 1 and "." not in text else text
    try:
        return float(text)
    except ValueError:
        return None


def read_measure(title: str, spec: MeasureSpec) -> Optional[float]:
    """The measure's value in ``title``: group 1 of its patterns times their
    scale, after ``strip`` patterns are removed; ``pick`` chooses among
    several matches (leftmost by default). None when the title states none.

    The dot-thousands reading ('1.024' = 1024) is a per-pattern option, as in
    the sizing run's ``capacity_wh``: it applies to the Wh pattern, never to
    the kWh one ('3.072kWh' is 3072 Wh, not 3,072,000)."""
    text = title
    for rx in spec.strip:
        text = rx.sub(" ", text)
    found: list[tuple[int, float]] = []
    for pattern in spec.patterns:
        for m in pattern.regex.finditer(text):
            value = _number(m.group(1) or "", dot_thousands=pattern.dot_thousands)
            if value is not None:
                found.append((m.start(), value * pattern.scale))
    if not found:
        return None
    if spec.pick == "max":
        return max(v for _, v in found)
    if spec.pick == "min":
        return min(v for _, v in found)
    return min(found)[1]


@dataclass(frozen=True)
class LaneDecision:
    """The classifier's verdict for one listing.

    ``lane`` is the accepted lane (None = do not persist). ``matched_lane`` is
    the lane the rules picked even when it is held back; precision metrics
    compare it with golden labels. ``reason`` is OK, GLOBAL_REJECT, OFF_LANE
    or HELD_BACK; ``rule`` names the global rule that fired; ``trace`` lists
    why earlier lanes passed the listing on ('<lane>:<check>[:<rule id>]').
    """

    lane: Optional[str]
    matched_lane: Optional[str]
    reason: str
    rule: Optional[str] = None
    node_confirmed: bool = False
    title_matched: bool = False
    confidence: Optional[float] = None
    trace: tuple[str, ...] = ()

    @property
    def accepted(self) -> bool:
        return self.lane is not None

    @property
    def predicted_label(self) -> str:
        """What the rules say this listing is: a lane id, or 'reject'."""
        return self.matched_lane or "reject"


class LaneClassifier:
    """Stateless classifier over a :class:`DiscoveryConfig`."""

    def __init__(self, cfg: DiscoveryConfig) -> None:
        self.cfg = cfg

    def classify(self, title: Optional[str], *, price: Optional[float] = None,
                 browse_nodes: Iterable[object] = ()) -> LaneDecision:
        text = normalize_title(title)
        nodes = {str(n).strip() for n in browse_nodes if n is not None and str(n).strip()}
        for rule in self.cfg.reject_rules:
            if rule.search(text):
                return LaneDecision(None, None, GLOBAL_REJECT, rule=rule.id)
        trace: list[str] = []
        for lane in self.cfg.lanes:
            verdict = self._lane_verdict(lane, text, price, nodes)
            if isinstance(verdict, str):
                if verdict:
                    trace.append(f"{lane.id}:{verdict}")
                continue
            title_ok, node_ok = verdict
            confidence = self._confidence(title_ok, node_ok)
            if not lane.active:
                return LaneDecision(None, lane.id, HELD_BACK, node_confirmed=node_ok,
                                    title_matched=title_ok, confidence=confidence,
                                    trace=tuple(trace))
            return LaneDecision(lane.id, lane.id, OK, node_confirmed=node_ok,
                                title_matched=title_ok, confidence=confidence,
                                trace=tuple(trace))
        return LaneDecision(None, None, OFF_LANE, trace=tuple(trace))

    def lane_exclusion(self, lane_id: str, title: Optional[str]) -> Optional[str]:
        """The id of the first ``exclude`` rule of lane ``lane_id`` the title
        hits, or None. Used to screen a labelled seed against its own lane."""
        lane = self.cfg.lane(lane_id)
        if lane is None:
            return None
        text = normalize_title(title)
        hit: Optional[PatternRule] = next((r for r in lane.exclude if r.search(text)), None)
        return hit.id if hit is not None else None

    # -- internals ---------------------------------------------------------------

    def _confidence(self, title_ok: bool, node_ok: bool) -> float:
        c = self.cfg.confidence
        if title_ok and node_ok:
            return c.node_and_title
        return c.title_only if title_ok else c.node_only

    def _lane_verdict(self, lane: Lane, text: str, price: Optional[float],
                      nodes: set[str]) -> tuple[bool, bool] | str:
        """(title_ok, node_ok) when the lane takes the listing, else the
        failed check ('' = no positive evidence at all, not worth a trace)."""
        title_ok = lane.has_title_rules and self._title_rules(lane, text)
        node_ok = bool(lane.browse_nodes and nodes & lane.browse_nodes)
        positive = {
            "title": title_ok,
            "node": node_ok,
            "title_and_node": title_ok and node_ok,
            "title_or_node": title_ok or node_ok,
        }[lane.match]
        if not positive:
            # Note a title hit that lacked the required node (and vice versa):
            # that is the interesting case when tuning a lane.
            if lane.match == "title_and_node" and (title_ok or node_ok):
                return "no-node" if title_ok else "no-title"
            return ""
        for rule in lane.exclude:
            if rule.search(text):
                return f"excluded:{rule.id}"
        if lane.context_required and not any(r.search(text) for r in lane.context_required):
            return "no-context"
        if price is not None and lane.min_price is not None and price < lane.min_price:
            return "below-min-price"
        for rule in lane.measure_rules:
            failed = self._measure_check(rule, text, price)
            if failed:
                return failed
        return title_ok, node_ok

    @staticmethod
    def _title_rules(lane: Lane, text: str) -> bool:
        if not all(r.search(text) for r in lane.include_all):
            return False
        return not lane.include_any or any(r.search(text) for r in lane.include_any)

    def _measure_check(self, rule: MeasureRule, text: str, price: Optional[float]) -> str:
        spec = self.cfg.measures.get(rule.measure)
        value = read_measure(text, spec) if spec is not None else None
        if value is None:
            if rule.required:
                return f"no-{rule.measure}"
            if (rule.missing_min_price is not None and price is not None
                    and price < rule.missing_min_price):
                return f"no-{rule.measure}-and-cheap"
            return ""
        if rule.min is not None and value < rule.min:
            return f"{rule.measure}-below-min"
        if rule.max is not None and value > rule.max:
            return f"{rule.measure}-above-max"
        return ""
