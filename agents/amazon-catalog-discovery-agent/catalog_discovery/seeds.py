"""Seed-ASIN import: ASINs we already know belong in the catalog.

Two kinds of source, both configured under ``seed_asins``:

* **files** -- a JSON file of ASINs (e.g. a sizing run's in-lane set). Shapes
  accepted at ``json_path``: ``["B0..", ...]``, ``[{"asin": .., "lane": ..,
  "phase": .., ...}, ...]``, ``{"B0..": "<lane>"}`` and
  ``{"B0..": {"lane": .., ...}}``. ``where`` keeps object records whose
  fields hold the given values (all must match; a list means any of its
  values); ``where_not`` drops records where any listed field holds one of
  the given values. ``title_field`` names a title to screen with.
* **queries** -- a read-only SELECT against another database (e.g. a sister
  site's catalog) returning ``asin`` and optionally ``lane``, ``phase`` and
  ``title``. It is read in keyset pages of ``max_rows`` (``asin > cursor``),
  so a large source continues on the next run instead of being cut.

A source's own lane labels go through ``lane_map`` (e.g. a sizing run's
'expansion_battery' -> our 'expansion').

TRUST AND SCREENING (the year-1 boundary applies to seeds too)
--------------------------------------------------------------
* A record WITH a title (a query's ``title`` column, a file's
  ``title_field``) is screened by the lane rules before anything else: a
  global reject pattern skips it, and so does a phase-held-back lane.
  With ``classify_titles`` our classifier picks the lane, and a source label
  that disagrees skips the record (``label-disputed``). Without it the label
  (or ``default_lane``) is kept, unless the title hits an ``exclude`` rule of
  that lane (``lane-excluded``).
* A record WITHOUT a title cannot be screened, so a lane label it carries is
  used only when the source trusts that lane explicitly: the lane is in the
  source's ``lanes`` allow-list, or ``trust_labels: true``. Otherwise it is
  skipped as ``untrusted-label``. ``default_lane`` is the operator's own
  assertion and needs no further trust.

Every row carries only the ASIN and our labels; the title is transient
(classification input only) and never stored or logged. Nothing here talks
to Amazon.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Mapping, Optional, Sequence

from .catalog import CatalogRow
from .config import DiscoveryConfig, SeedFileSource, SeedQuerySource
from .lanes import GLOBAL_REJECT, HELD_BACK, LaneClassifier

UNTRUSTED_LABEL = "untrusted-label"
LABEL_DISPUTED = "label-disputed"
LANE_EXCLUDED = "lane-excluded"


class SeedSourceError(RuntimeError):
    """A seed source could not be read or has an unsupported shape."""


@dataclass(frozen=True)
class SeedRecord:
    asin: str
    lane: Optional[str] = None
    phase: Optional[str] = None
    # Transient classification input; never persisted, logged or reported.
    title: Optional[str] = field(default=None, repr=False)


@dataclass
class SeedPlan:
    rows: list[CatalogRow] = field(default_factory=list)
    skipped: Counter = field(default_factory=Counter)
    #: Informational counts about rows that WERE planned (e.g. a title that
    #: our rules would file elsewhere, kept because the label wins).
    notes: Counter = field(default_factory=Counter)


@dataclass(frozen=True)
class SeedQueryPage:
    """One keyset page of a seed query."""

    records: list[SeedRecord]
    more: bool                 # a full page came back: the source continues after ``last``
    last: str = ""             # the page's highest ASIN (the next cursor)


def _dig(doc: Any, json_path: Optional[str]) -> Any:
    node = doc
    for part in [p for p in (json_path or "").split(".") if p]:
        if isinstance(node, Mapping) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            raise SeedSourceError(f"json_path {json_path!r}: no {part!r}")
    return node


def _matches(value: Any, expected: Any) -> bool:
    if isinstance(expected, list):
        return value in expected
    return value == expected


def _where_ok(record: Mapping[str, Any], where: Mapping[str, Any]) -> bool:
    return all(_matches(record.get(key), expected) for key, expected in where.items())


def _where_not_hit(record: Mapping[str, Any], where_not: Mapping[str, Any]) -> bool:
    return any(_matches(record.get(key), expected) for key, expected in where_not.items())


def _text(value: Any) -> Optional[str]:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text or None


def records_from_document(doc: Any, src: SeedFileSource) -> Iterator[SeedRecord]:
    """Yield the seed records of a parsed seed file (see module docstring)."""
    container = _dig(doc, src.json_path)
    where = dict(src.where or {})
    where_not = dict(src.where_not or {})
    filtered = bool(where or where_not)

    def keep(record: Mapping[str, Any]) -> bool:
        if where and not _where_ok(record, where):
            return False
        return not (where_not and _where_not_hit(record, where_not))

    def from_record(asin: Any, record: Mapping[str, Any]) -> SeedRecord:
        return SeedRecord(asin=str(asin or ""), lane=_text(record.get(src.lane_field)),
                          phase=_text(record.get(src.phase_field)),
                          title=_text(record.get(src.title_field)) if src.title_field else None)

    if isinstance(container, list):
        for item in container:
            if isinstance(item, str):
                if filtered:
                    raise SeedSourceError("`where` / `where_not` need object records, "
                                          "the file lists bare ASINs")
                yield SeedRecord(asin=item)
            elif isinstance(item, Mapping):
                if keep(item):
                    yield from_record(item.get(src.asin_field), item)
            else:
                raise SeedSourceError(f"unsupported list item of type {type(item).__name__}")
    elif isinstance(container, Mapping):
        for key, value in container.items():
            if str(key).startswith("_"):
                continue
            if isinstance(value, Mapping):
                if keep(value):
                    yield from_record(key, value)
            elif filtered:
                raise SeedSourceError("`where` / `where_not` need object records, "
                                      "the file maps ASINs to scalars")
            elif value is False:
                continue
            elif isinstance(value, str):
                yield SeedRecord(asin=str(key), lane=_text(value))
            elif value is None or value is True:
                yield SeedRecord(asin=str(key))
            else:
                raise SeedSourceError(f"unsupported value of type {type(value).__name__} for {key!r}")
    else:
        raise SeedSourceError(f"expected a list or an object, got {type(container).__name__}")


def read_seed_file(src: SeedFileSource) -> tuple[list[SeedRecord], str]:
    """(records, sha256 of the file's bytes)."""
    try:
        data = src.path.read_bytes()
    except OSError as exc:
        raise SeedSourceError(f"cannot read {src.path}: {exc.strerror or exc}") from None
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise SeedSourceError(f"{src.path.name} is not JSON: {exc}") from None
    sha = hashlib.sha256(data).hexdigest()[:16]
    return list(records_from_document(doc, src)), sha


def read_seed_query(conn: Any, src: SeedQuerySource, *, after: str = "") -> SeedQueryPage:
    """One keyset page of the source's SELECT on ``conn`` (a READ ONLY
    session the caller opened): the rows with ``asin > after``, in ASIN
    order, at most ``max_rows`` of them."""
    try:
        with conn.cursor() as cur:
            cur.execute(src.paged_sql(), {"after": after or "", "limit": int(src.max_rows)})
            cols = [str(c[0]).lower() for c in (cur.description or [])]
            if "asin" not in cols:
                raise SeedSourceError("the query returns no `asin` column")
            rows = cur.fetchall()
    finally:
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001 -- best effort on a read-only session
            pass
    out = []
    for row in rows[:src.max_rows]:
        rec = dict(zip(cols, row))
        out.append(SeedRecord(asin=str(rec.get("asin") or ""), lane=_text(rec.get("lane")),
                              phase=_text(rec.get("phase")), title=_text(rec.get("title"))))
    # The cursor is the last row in the SERVER's order (ORDER BY asin), so the
    # next page's `asin > cursor` uses the same collation that sorted this one.
    last = out[-1].asin if out else (after or "")
    return SeedQueryPage(records=out, more=len(rows) >= src.max_rows, last=last)


def records_signature(records: Sequence[SeedRecord]) -> str:
    """Hash of the (asin, lane, phase) set, title excluded."""
    keys = sorted({(r.asin.strip().upper(), r.lane or "", r.phase or "") for r in records})
    return hashlib.sha256(json.dumps(keys).encode("utf-8")).hexdigest()[:16]


def plan_seed_rows(records: Iterable[SeedRecord], *, cfg: DiscoveryConfig,
                   classifier: LaneClassifier, source_name: str,
                   allowed_lanes: Optional[frozenset] = None,
                   default_lane: Optional[str] = None,
                   classify_titles: bool = False,
                   lane_map: Optional[Mapping[str, str]] = None,
                   trust_labels: bool = False,
                   confidence: Optional[float] = None) -> SeedPlan:
    """Resolve each record to a catalog row, or count why it was skipped
    (see TRUST AND SCREENING in the module docstring)."""
    plan = SeedPlan()
    asin_re = cfg.catalog.asin_re
    seen: set[str] = set()
    via = f"seed:{source_name}"
    lane_map = dict(lane_map or {})
    for rec in records:
        asin = (rec.asin or "").strip().upper()
        if not asin_re.match(asin):
            plan.skipped["invalid-asin"] += 1
            continue
        if asin in seen:
            plan.skipped["duplicate"] += 1
            continue
        seen.add(asin)
        if rec.phase and cfg.active_phases is not None and rec.phase not in cfg.active_phases:
            plan.skipped["phase-held-back"] += 1
            continue
        label = lane_map.get(rec.lane, rec.lane) if rec.lane else None
        if label is not None and cfg.lane(label) is None:
            plan.skipped["unknown-lane"] += 1  # a source label lane_map does not translate
            continue
        row_confidence = cfg.confidence.seed
        lane_id: Optional[str] = None
        if rec.title:
            decision = classifier.classify(rec.title)
            if decision.reason == GLOBAL_REJECT:
                plan.skipped["global-reject"] += 1
                continue
            if decision.reason == HELD_BACK:
                plan.skipped["lane-held-back"] += 1
                continue
            if classify_titles:
                if decision.lane is None:
                    plan.skipped["off-lane"] += 1
                    continue
                if label is not None and label != decision.lane:
                    plan.skipped[LABEL_DISPUTED] += 1
                    continue
                lane_id = decision.lane
                if label is None:
                    row_confidence = float(decision.confidence or row_confidence)
            else:
                lane_id = label or default_lane
                if lane_id is not None and classifier.lane_exclusion(lane_id, rec.title):
                    plan.skipped[LANE_EXCLUDED] += 1
                    continue
                if lane_id is not None and decision.lane != lane_id:
                    plan.notes["title-suggests-" + (decision.lane or "off-lane")] += 1
        elif label is not None:
            if not trust_labels and (allowed_lanes is None or label not in allowed_lanes):
                # No title to check the label against, and the source does
                # not vouch for this lane: never import it on faith.
                plan.skipped[UNTRUSTED_LABEL if allowed_lanes is None else "lane-not-allowed"] += 1
                continue
            lane_id = label
        else:
            lane_id = default_lane
        if lane_id is None:
            plan.skipped["no-lane"] += 1
            continue
        lane = cfg.lane(lane_id)
        if lane is None:
            plan.skipped["unknown-lane"] += 1
            continue
        if allowed_lanes is not None and lane.id not in allowed_lanes:
            plan.skipped["lane-not-allowed"] += 1
            continue
        if not lane.active:
            plan.skipped["lane-held-back"] += 1
            continue
        plan.rows.append(CatalogRow(asin=asin, lane=lane.id, lane_kind=lane.kind,
                                    lane_category=lane.category,
                                    confidence=row_confidence if confidence is None else confidence,
                                    via=via, seed=source_name))
    return plan


def plan_source(records: Iterable[SeedRecord], src: SeedFileSource | SeedQuerySource, *,
                cfg: DiscoveryConfig, classifier: LaneClassifier) -> SeedPlan:
    """:func:`plan_seed_rows` with a source's own options."""
    return plan_seed_rows(records, cfg=cfg, classifier=classifier, source_name=src.name,
                          allowed_lanes=src.lanes, default_lane=src.default_lane,
                          classify_titles=bool(getattr(src, "classify_titles", False)),
                          lane_map=src.lane_map, trust_labels=src.trust_labels,
                          confidence=src.confidence)
