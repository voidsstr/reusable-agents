"""The MATCH phase: link catalog products to the site's own models.

Runs after seeds and search in the same run (same Creators consumer, budget
and run lock), when ``model_matching.enabled``. It applies the operator's
"search and confirm" rule through ``framework.core.model_matcher``: an ASIN is
linked to a model only when Amazon's brand field resolves to the model's maker
(alias-aware) AND the exact model designation appears in the listing title as
a whole token sequence; anything ambiguous stays unlinked.

TWO PASSES, ONE ALLOWANCE
-------------------------
1. **ASIN-first.** Products from ``candidates_sql`` (e.g. active, no model),
   in keyset pages of ``asin_first.batch`` (<= 10), one ``getItems`` call per
   page asking only for ``itemInfo.title`` + ``itemInfo.byLineInfo``. A title
   that hits a global reject pattern is never linked (``global-reject:<id>``,
   reported), nor is one no active lane accepts by its title (``lane:<reason>``;
   the lane's kind must also fit the model's). Every other item goes through
   the matcher; a match at or above
   ``confidence`` whose
   model kind fits the product kind is written with ``link_sql`` (which must
   keep ``model_id IS NULL`` in its WHERE: an existing link is never
   overwritten). The cursor persists, so the next run continues; once every
   candidate was checked the pass starts over after ``recheck_hours``.
2. **Model-first.** Models from ``models_sql`` that have no linked active
   product and may be searched, in ``priority`` order: one ``searchItems``
   per model with our own words ('<maker> <model name>'). Each listing first
   passes the pricing gate (new ASINs only), then the global rejects and lane
   rules, then the matcher. A known product is linked; a new one is inserted
   with its lane (``insert_sql``) and linked. At most one product per model
   per run, unless the model documents variants (``variants``). A model
   searched without a link waits ``retry_hours``.

The allowance is ``min(model_matching.max_calls_per_run, the consumer's
remaining share after search, the protected reserve)``; ASIN-first may be
capped with ``asin_first.max_calls`` so model-first always gets the rest. A
429, a spent budget or the allowance stops the phase cleanly; the next run
resumes from the saved cursor and search times.

WHAT NEVER LEAVES MEMORY
------------------------
Titles, brands and every other Amazon field are read here and dropped. The
report rows, the counters, the state and the DB writes carry only the ASIN,
our model id, the matcher's rule / reason id, our confidence and the time.
Nothing here logs. An unexpected error is reported by its class name only
(``failure``), never its text, because a matcher bug could quote a title.
"""
from __future__ import annotations

import datetime as dt
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from framework.core import amazon_creators, amazon_discovery
from framework.core import model_matcher as mm

from .catalog import Catalog, CatalogRow
from .config import LINK_PARAMS, DiscoveryConfig
from .lanes import LaneClassifier, normalize_title

#: getItems resources: the two fields the matcher reads, nothing else.
MATCH_RESOURCES = ("itemInfo.title", "itemInfo.byLineInfo")
REPORT_ROW_CAP = 500
SERVER_ERROR_STREAK_STOP = 3
TRANSIENT_STATUSES = frozenset({408}) | frozenset(range(500, 600))

STOP_MAX_CALLS = "max-calls"
STOP_BUDGET = "budget-exhausted"
STOP_THROTTLED = "throttled"
STOP_UNAVAILABLE = "creators-unavailable"
STOP_NO_ALLOWANCE = "no-allowance"
STOP_SERVER_ERRORS = "server-errors"
STOP_DONE = "done"

# Reason codes the phase adds to the matcher's own.
R_NOT_ACCESSIBLE = "not-accessible"
R_NOT_RETURNED = "not-returned"
R_INVALID_ASIN = "invalid-asin"
R_BELOW_CONFIDENCE = "below-confidence"
R_KIND_MISMATCH = "kind-mismatch"
R_MODEL_HAS_PRODUCT = "model-has-product"
R_MATCHER_ERROR = "matcher-error"
R_MAX_NEW = "max-new"


def _iso(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(text: Any) -> Optional[dt.datetime]:
    if not isinstance(text, str):
        return None
    try:
        return dt.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def _bool(value: Any, default: bool) -> bool:
    return default if value is None else bool(value)


@dataclass
class ModelMeta:
    model_id: Any
    linked: bool
    priority: Optional[float]
    searchable: bool
    variants: int

    @property
    def per_run_cap(self) -> int:
        return max(1, self.variants)


@dataclass
class MatchReport:
    """Counters and outcome rows; no Amazon value anywhere."""

    enabled: bool = True
    allowance: int = 0
    stopped_reason: str = ""
    calls: int = 0
    asin_first_calls: int = 0
    model_first_calls: int = 0
    checked: int = 0
    matched: int = 0
    linked: int = 0
    linked_asin_first: int = 0
    linked_model_first: int = 0
    inserted: int = 0
    link_noop: int = 0
    ambiguous: int = 0
    unmatched: int = 0
    rejected: int = 0
    errors: int = 0
    error_samples: list[str] = field(default_factory=list)
    request_errors: int = 0
    transient_errors: int = 0
    throttled: int = 0
    reasons: Counter = field(default_factory=Counter)
    rows: list[dict[str, Any]] = field(default_factory=list)
    rows_total: int = 0
    models_total: int = 0
    models_linked_before: int = 0
    models_newly_linked: set = field(default_factory=set)
    models_searched: int = 0
    models_waiting: int = 0
    asin_first_state: str = ""          # 'cooldown' | 'cycle-complete' | 'continues' | 'off'
    stats: Optional[dict[str, int]] = None
    index_problems: list[str] = field(default_factory=list)
    failure: str = ""

    @property
    def models_with_product(self) -> int:
        return self.models_linked_before + len(self.models_newly_linked)

    def row(self, asin: str, model_id: Any, rule: str, confidence: float) -> None:
        self.rows_total += 1
        if len(self.rows) < REPORT_ROW_CAP:
            self.rows.append({"asin": asin, "model_id": model_id, "rule": rule,
                              "confidence": round(float(confidence or 0.0), 4)})

    def products_model_matched_pct(self, *, dry_run: bool) -> Optional[float]:
        if not self.stats:
            return None
        active = int(self.stats.get("active_products") or 0)
        matched = int(self.stats.get("matched_products") or 0)
        if dry_run:  # nothing was written: add what this run would have
            active += self.inserted
            matched += self.linked
        return round(100.0 * matched / active, 2) if active else 0.0

    def to_dict(self, *, dry_run: bool = False) -> dict[str, Any]:
        return {
            "enabled": self.enabled, "allowance": self.allowance, "stopped_reason": self.stopped_reason,
            "counts": {"checked": self.checked, "matched": self.matched, "linked": self.linked,
                       "inserted": self.inserted, "ambiguous": self.ambiguous,
                       "unmatched": self.unmatched, "rejected": self.rejected, "calls": self.calls,
                       "link_noop": self.link_noop, "errors": self.errors},
            "asin_first": {"calls": self.asin_first_calls, "linked": self.linked_asin_first,
                           "state": self.asin_first_state},
            "model_first": {"calls": self.model_first_calls, "linked": self.linked_model_first,
                            "models_searched": self.models_searched,
                            "models_waiting": self.models_waiting},
            "models": {"total": self.models_total, "with_product": self.models_with_product,
                       "newly_linked": len(self.models_newly_linked)},
            "request_errors": self.request_errors, "transient_errors": self.transient_errors,
            "throttled": self.throttled, "reasons": dict(self.reasons.most_common()),
            "error_samples": list(self.error_samples),
            "stats": dict(self.stats) if self.stats else None,
            "products_model_matched_pct": self.products_model_matched_pct(dry_run=dry_run),
            "index_problems": list(self.index_problems)[:20],
            "rows": list(self.rows), "rows_total": self.rows_total,
            "failure": self.failure,
        }


class MatchStore:
    """The site's SQL for this phase, over the catalog's connection."""

    def __init__(self, catalog: Catalog, cfg: DiscoveryConfig, *, run_ts: str) -> None:
        self.catalog = catalog
        self.mcfg = cfg.matching
        self.run_ts = run_ts

    def _select(self, sql: str, params: Optional[Mapping[str, Any]] = None) -> list[dict[str, Any]]:
        with self.catalog.conn.cursor() as cur:
            cur.execute(sql, dict(params) if params is not None else None)
            cols = [str(c[0]).lower() for c in (cur.description or [])]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def models(self) -> list[dict[str, Any]]:
        return self._select(self.mcfg.plain(self.mcfg.models_sql))

    def candidates(self, *, after: str, limit: int) -> list[dict[str, Any]]:
        rows = self._select(self.mcfg.paged(self.mcfg.candidates_sql), {"after": after, "limit": int(limit)})
        if rows and "asin" not in rows[0]:
            raise ValueError("model_matching.candidates_sql returns no `asin` column")
        return rows

    def stats(self) -> Optional[dict[str, int]]:
        if not self.mcfg.stats_sql:
            return None
        rows = self._select(self.mcfg.plain(self.mcfg.stats_sql))
        if not rows:
            return None
        return {"active_products": int(rows[0].get("active_products") or 0),
                "matched_products": int(rows[0].get("matched_products") or 0)}

    def link(self, asin: str, model_id: Any, *, confidence: float, rule: str) -> tuple[str, str]:
        """('linked' | 'noop' | 'error', error text). Text = class + SQLSTATE only."""
        if self.catalog.dry_run:
            return "linked", ""
        params = {"asin": asin, "model_id": model_id, "confidence": round(float(confidence), 4),
                  "rule": rule, "run_ts": self.run_ts}
        assert set(params) == set(LINK_PARAMS)
        with self.catalog.conn.cursor() as cur:
            cur.execute("SAVEPOINT model_match_link")
            try:
                cur.execute(self.mcfg.link_sql, params)
                count = cur.rowcount
            except Exception as exc:  # noqa: BLE001 -- one bad row never aborts the batch
                cur.execute("ROLLBACK TO SAVEPOINT model_match_link")
                code = getattr(exc, "pgcode", None)
                return "error", f"{asin}: {type(exc).__name__}" + (f" ({code})" if code else "")
            cur.execute("RELEASE SAVEPOINT model_match_link")
        return ("linked" if count and count > 0 else "noop"), ""


class MatchPhase:
    def __init__(self, cfg: DiscoveryConfig, *, now: dt.datetime, run_ts: str,
                 classifier: Optional[LaneClassifier] = None) -> None:
        self.cfg = cfg
        self.mcfg = cfg.matching
        self.now = now
        self.run_ts = run_ts
        self.classifier = classifier or LaneClassifier(cfg)

    # -- entry ---------------------------------------------------------------------------

    def run(self, client: Any, catalog: Catalog, *, state: dict[str, Any], allowed_calls: int,
            max_new: Optional[int] = None) -> MatchReport:
        """Mutates ``state['match']`` (cursor, search times). Never raises for
        a Creators or matcher problem: ``report.failure`` names its class."""
        rep = MatchReport(allowance=max(0, int(allowed_calls)))
        st = state.get("match")
        if not isinstance(st, dict):
            st = {}
        state["match"] = st
        store = MatchStore(catalog, self.cfg, run_ts=self.run_ts)
        failure = ""
        try:
            self._run(client, catalog, store, rep, st, max_new=max_new)
        except amazon_creators.CreatorsUnavailable:
            rep.stopped_reason = STOP_UNAVAILABLE
            failure = "Creators unavailable during model matching (auth / network)"
        except Exception as exc:  # noqa: BLE001 -- the text could quote a listing: class only
            failure = f"model matching stopped by {type(exc).__name__}"
        if failure:
            rep.failure = failure
            try:
                catalog.rollback()
            except Exception:  # noqa: BLE001
                pass
        try:
            rep.stats = store.stats()
        except Exception as exc:  # noqa: BLE001
            rep.index_problems.append(f"stats_sql failed: {type(exc).__name__}")
            try:
                catalog.rollback()
            except Exception:  # noqa: BLE001
                pass
        return rep

    def _run(self, client: Any, catalog: Catalog, store: MatchStore, rep: MatchReport,
             st: dict[str, Any], *, max_new: Optional[int]) -> None:
        rows = store.models()
        records, metas = [], {}
        for r in rows:
            rec = mm.ModelRecord.from_row(r)
            if rec.model_id is None:
                continue
            records.append(rec)
            try:
                prio = None if r.get("priority") is None else float(r["priority"])
            except (TypeError, ValueError):
                prio = None
            try:
                variants = int(r.get("variants") or 0)
            except (TypeError, ValueError):
                variants = 0
            metas[rec.model_id] = ModelMeta(rec.model_id, bool(r.get("linked")), prio,
                                            _bool(r.get("searchable"), True), variants)
        index = mm.ModelIndex(records, brand_aliases=self.mcfg.brand_aliases,
                              kind_roles=self.mcfg.kind_roles,
                              capacity_tolerance=self.mcfg.capacity_tolerance)
        rep.index_problems.extend(index.problems)
        rep.models_total = len(metas)
        rep.models_linked_before = sum(1 for m in metas.values() if m.linked)
        self._index, self._metas = index, metas
        self._links_this_run: Counter = Counter()
        self._seen: set[str] = set()
        self._new_left = None if max_new is None else max(0, int(max_new))
        if rep.allowance <= 0:
            rep.stopped_reason = STOP_NO_ALLOWANCE
            return
        if self.mcfg.asin_first.enabled:
            self._asin_first(client, catalog, store, rep, st)
        else:
            rep.asin_first_state = "off"
        if not rep.stopped_reason and self.mcfg.model_first.enabled:
            self._model_first(client, catalog, store, rep, st)
        if not rep.stopped_reason:
            rep.stopped_reason = STOP_MAX_CALLS if rep.calls >= rep.allowance else STOP_DONE

    # -- budget --------------------------------------------------------------------------

    def _can_call(self, client: Any, rep: MatchReport) -> bool:
        if rep.calls >= rep.allowance:
            rep.stopped_reason = STOP_MAX_CALLS
            return False
        fn = getattr(client, "budget_ok", None)
        if fn is not None and not fn():
            rep.stopped_reason = STOP_BUDGET
            return False
        return True

    # -- ASIN-first ----------------------------------------------------------------------

    def _asin_first(self, client: Any, catalog: Catalog, store: MatchStore, rep: MatchReport,
                    st: dict[str, Any]) -> None:
        af = self.mcfg.asin_first
        cursor = str(st.get("asin_cursor") or "")
        done_at = _parse(st.get("asin_cycle_done_at"))
        if not cursor and done_at is not None and self.now < done_at + dt.timedelta(hours=af.recheck_hours):
            rep.asin_first_state = "cooldown"
            return
        budget = rep.allowance if af.max_calls is None else min(rep.allowance, af.max_calls)
        asin_re = self.cfg.catalog.asin_re
        rep.asin_first_state = "continues"
        while rep.asin_first_calls < budget:
            batch = store.candidates(after=cursor, limit=af.batch)
            if not batch:
                st["asin_cursor"] = ""
                st["asin_cycle_done_at"] = _iso(self.now)
                rep.asin_first_state = "cycle-complete"
                return
            if not self._can_call(client, rep):
                return
            asins, kinds = [], {}
            for row in batch:
                asin = str(row.get("asin") or "").strip().upper()
                if asin_re.match(asin):
                    asins.append(asin)
                    kinds[asin] = None if row.get("kind") is None else str(row.get("kind"))
                else:
                    rep.rejected += 1
                    rep.reasons[R_INVALID_ASIN] += 1
            last = str(batch[-1].get("asin") or "")
            if not asins:
                cursor = last
                st["asin_cursor"] = cursor
                continue
            rep.calls += 1
            rep.asin_first_calls += 1
            try:
                items, errors = client.get_items(asins, resources=MATCH_RESOURCES)
            except amazon_creators.CreatorsRequestError as exc:
                status = int(getattr(exc, "status", 0) or 0)
                if status in TRANSIENT_STATUSES:
                    rep.transient_errors += 1
                    rep.stopped_reason = STOP_SERVER_ERRORS   # the page is retried next run
                    return
                rep.request_errors += 1                       # a refused page: move past it
                cursor = last
                st["asin_cursor"] = cursor
                continue
            if getattr(client, "throttled_remainder", None):
                rep.throttled += 1
                rep.stopped_reason = STOP_THROTTLED           # the same page next run
                return
            by_asin = {str(k).upper(): v for k, v in (items or {}).items()}
            failed = {str(k).upper() for k in (errors or {})}
            for asin in asins:
                item = by_asin.get(asin)
                if item is None:
                    reason = R_NOT_ACCESSIBLE if asin in failed else R_NOT_RETURNED
                    rep.unmatched += 1
                    rep.reasons[reason] += 1
                    rep.row(asin, None, reason, 0.0)
                    continue
                parsed = amazon_creators.parse_item(item)
                # The year-1 boundary first: a catalog row (an untitled seed label,
                # say) whose title hits a global reject is never linked, so its
                # confidence is never raised.
                rule = self._global_reject(parsed.get("title"))
                if rule:
                    self._reject(rep, asin, f"global-reject:{rule}")
                    continue
                # Then the lane rules, as model-first applies them: an untitled seed
                # never went through them, and a case, bag or cable named after a
                # station must not be linked to it (getItems asks for no price or
                # browse nodes, so the title rules decide).
                decision = self.classifier.classify(parsed.get("title"), price=None, browse_nodes=())
                if decision.lane is None:
                    self._reject(rep, asin, f"lane:{decision.reason}")
                    continue
                lane = self.cfg.lane(decision.lane)
                res = self._match(parsed, rep)
                del parsed, item
                self._decide(store, rep, asin, res, product_kind=kinds.get(asin), source="asin-first",
                             lane_kind=lane.kind if lane else None)
            catalog.commit()
            cursor = last
            st["asin_cursor"] = cursor

    # -- model-first ---------------------------------------------------------------------

    def _work_list(self, st: dict[str, Any], rep: MatchReport) -> list[ModelMeta]:
        mf = self.mcfg.model_first
        queried = st.get("models_queried")
        if not isinstance(queried, dict):
            queried = {}
        known_keys = {str(m) for m in self._metas}
        queried = {k: v for k, v in queried.items() if k in known_keys}   # prune retired models
        st["models_queried"] = queried
        out = []
        for meta in self._metas.values():
            if not meta.searchable or (meta.linked and meta.variants <= 0):
                continue
            last = _parse(queried.get(str(meta.model_id)))
            if last is not None and self.now < last + dt.timedelta(hours=mf.retry_hours):
                rep.models_waiting += 1
                continue
            out.append(meta)
        out.sort(key=lambda m: (m.priority is None, m.priority if m.priority is not None else 0.0,
                                str(m.model_id)))
        return out

    def _model_first(self, client: Any, catalog: Catalog, store: MatchStore, rep: MatchReport,
                     st: dict[str, Any]) -> None:
        mf = self.mcfg.model_first
        work = self._work_list(st, rep)
        index_name = mf.search_index or self.cfg.creators.search_index
        streak = 0
        for meta in work:
            if rep.model_first_calls >= mf.queries_per_run:
                break
            if self._links_this_run[meta.model_id] >= meta.per_run_cap:
                continue
            if not self._can_call(client, rep):
                return
            keywords = mm.search_keywords(self._index, meta.model_id)
            if not keywords:
                continue
            rep.calls += 1
            rep.model_first_calls += 1
            try:
                raw = client.search_items(keywords, search_index=index_name, item_count=mf.item_count)
            except amazon_creators.CreatorsRequestError as exc:
                status = int(getattr(exc, "status", 0) or 0)
                if status in TRANSIENT_STATUSES:
                    rep.transient_errors += 1
                    streak += 1
                    if streak >= SERVER_ERROR_STREAK_STOP:
                        rep.stopped_reason = STOP_SERVER_ERRORS
                        return
                    continue                                   # searched again next run
                rep.request_errors += 1
                st["models_queried"][str(meta.model_id)] = _iso(self.now)
                continue
            streak = 0
            if (getattr(client, "last_search_meta", None) or {}).get("throttled"):
                rep.throttled += 1
                rep.stopped_reason = STOP_THROTTLED
                return
            rep.models_searched += 1
            parsed = [amazon_creators.parse_item(r) for r in raw or []]
            asins = [str(p.get("asin") or "").upper() for p in parsed]
            valid = [a for a in asins if self.cfg.catalog.asin_re.match(a)]
            known = catalog.known(valid) if valid else set()
            for p, asin in zip(parsed, asins):
                self._listing(store, catalog, rep, p, asin, known=asin in known)
            del parsed
            catalog.commit()
            # Only once its listings are committed: a failure above rolls them back
            # and the model is searched again next run, not after retry_hours.
            st["models_queried"][str(meta.model_id)] = _iso(self.now)

    def _listing(self, store: MatchStore, catalog: Catalog, rep: MatchReport, p: dict[str, Any],
                 asin: str, *, known: bool) -> None:
        if not asin or asin in self._seen:
            return
        self._seen.add(asin)
        if not self.cfg.catalog.asin_re.match(asin):
            self._reject(rep, asin, R_INVALID_ASIN)
            return
        if not known:
            gate = amazon_discovery.listing_rejection(
                p, min_price=self.cfg.pricing.min_price, currencies=self.cfg.pricing.currencies,
                require_new=self.cfg.pricing.require_new_condition)
            if gate is not None:
                self._reject(rep, asin, f"pricing:{gate}")
                return
        nodes = [str(n.get("id")) for n in p.get("browse_nodes") or [] if n.get("id") is not None]
        price = p.get("price")
        decision = self.classifier.classify(p.get("title"), price=price, browse_nodes=nodes)
        if decision.lane is None:
            self._reject(rep, asin, f"{decision.reason}:{decision.rule}" if decision.rule else decision.reason)
            return
        lane = self.cfg.lane(decision.lane)
        res = self._match(p, rep)
        self._decide(store, rep, asin, res, product_kind=lane.kind if lane else None, source="model-first",
                     insert=None if known else (lane, decision.confidence), catalog=catalog)

    # -- shared ----------------------------------------------------------------------------

    def _match(self, parsed: Mapping[str, Any], rep: MatchReport) -> mm.MatchResult:
        try:
            return mm.match(parsed.get("brand"), parsed.get("title"), self._index)
        except Exception as exc:  # noqa: BLE001 -- counted by class; the text never leaves
            rep.errors += 1
            if len(rep.error_samples) < 5:
                rep.error_samples.append(f"matcher: {type(exc).__name__}")
            return mm.MatchResult(None, 0.0, "", R_MATCHER_ERROR)

    def _global_reject(self, title: Any) -> str:
        text = normalize_title(title)
        return next((r.id for r in self.cfg.reject_rules if r.search(text)), "")

    def _reject(self, rep: MatchReport, asin: str, reason: str) -> None:
        rep.rejected += 1
        rep.reasons[reason] += 1
        rep.row(asin, None, reason, 0.0)

    def _kind_ok(self, product_kind: Optional[str], model_kind: str) -> bool:
        table = self.mcfg.compatible_kinds
        if table is None or product_kind is None:
            return True
        allowed = table.get(product_kind)
        return model_kind == product_kind if allowed is None else model_kind in allowed

    def _decide(self, store: MatchStore, rep: MatchReport, asin: str, res: mm.MatchResult, *,
                product_kind: Optional[str], source: str, lane_kind: Optional[str] = None,
                insert: Optional[tuple[Any, Optional[float]]] = None,
                catalog: Optional[Catalog] = None) -> None:
        rep.checked += 1
        if not res.matched:
            if res.ambiguous:
                rep.ambiguous += 1
            else:
                rep.unmatched += 1
            rep.reasons[res.reason_code] += 1
            rep.row(asin, None, res.reason_code, res.confidence)
            return
        if res.confidence < self.mcfg.confidence:
            rep.unmatched += 1
            rep.reasons[R_BELOW_CONFIDENCE] += 1
            rep.row(asin, None, R_BELOW_CONFIDENCE, res.confidence)
            return
        record = self._index.record(res.model_id)
        model_kind = record.kind if record else ""
        if not self._kind_ok(product_kind, model_kind) or (
                lane_kind is not None and not self._kind_ok(lane_kind, model_kind)):
            self._reject(rep, asin, R_KIND_MISMATCH)
            return
        rep.matched += 1
        meta = self._metas.get(res.model_id)
        if source == "model-first" and meta is not None:
            already = meta.linked and meta.variants <= 0
            if already or self._links_this_run[res.model_id] >= meta.per_run_cap:
                rep.reasons[R_MODEL_HAS_PRODUCT] += 1
                rep.row(asin, None, R_MODEL_HAS_PRODUCT, res.confidence)
                return
        if insert is not None:
            lane, lane_conf = insert
            if self._new_left is not None and self._new_left <= 0:
                rep.reasons[R_MAX_NEW] += 1
                rep.row(asin, None, R_MAX_NEW, res.confidence)
                return
            row = CatalogRow(asin=asin, lane=lane.id, lane_kind=lane.kind, lane_category=lane.category,
                             confidence=lane_conf, via=f"match:{lane.id}/model-first",
                             seed=f"model:{res.model_id}")
            outcome = catalog.insert([row])
            if outcome.errors:
                rep.errors += 1
                if len(rep.error_samples) < 5:
                    rep.error_samples.append(f"{asin}: insert failed")
                return
            if not outcome.inserted:
                rep.link_noop += 1    # it appeared meanwhile; the link below decides
            else:
                rep.inserted += 1
                if self._new_left is not None:
                    self._new_left -= 1
        outcome, err = store.link(asin, res.model_id, confidence=self.mcfg.confidence, rule=res.rule)
        if outcome == "error":
            rep.errors += 1
            if len(rep.error_samples) < 5:
                rep.error_samples.append(err)
            return
        if outcome == "noop":
            rep.link_noop += 1
            rep.reasons["already-linked"] += 1
            rep.row(asin, None, "already-linked", res.confidence)
            return
        rep.linked += 1
        if source == "asin-first":
            rep.linked_asin_first += 1
        else:
            rep.linked_model_first += 1
        self._links_this_run[res.model_id] += 1
        if meta is not None and not meta.linked:
            rep.models_newly_linked.add(res.model_id)
        rep.reasons[res.rule] += 1
        rep.row(asin, res.model_id, res.rule, res.confidence)
