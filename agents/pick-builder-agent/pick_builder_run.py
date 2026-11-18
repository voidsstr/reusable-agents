"""One pick-builder run.

 1. Open one REPEATABLE READ snapshot (read-only for --plan) and load: the
    eligibility pool (the site's view, re-checked against the integrity
    floor), the compat edges a ladder plan depends on, active ladders /
    tiers / lists, the substitute subjects, and the current outputs.
 2. Short-circuit: when the inputs hash (pool rows, compat edges,
    definitions, subjects, config) and the outputs hash both match the last
    run, the last publish succeeded and storage still holds that list,
    nothing is recomputed or written (short_circuit.should_skip; it honours
    AGENT_FORCE_RUN).
 3. Otherwise fill every tier of every active ladder from its kind's plan,
    every active list from its kind's selector, and every subject's
    substitutes. A slot no eligible product satisfies is written EMPTY with
    the reason (a slot a direct connection makes unnecessary is written
    ``not-needed``). Ladders / lists without a plan / selector are only
    swept: ineligible products are cleared, nothing new is chosen.
 4. Write only the groups (tier, list, subject) whose rows changed, in the
    same transaction, and commit.
 5. Publish the pick-ASIN list (ladder picks, then list items, then, when
    configured, substitutes; picks dropped this run are kept for
    ``keep_dropped_runs`` runs so the refresh re-checks them) to the storage
    key price_refresh_priority reads as a ``storage_list`` group, and read it
    back. A failed or unverified publish leaves ``publish_pending`` in the
    state, so the next run cannot short-circuit: it publishes again.
 6. Verify in a fresh transaction: every live pick must still be in the
    eligibility pool (ineligible_in_picks, goal 0).
"""
from __future__ import annotations

import collections
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional, Sequence

from framework.core.short_circuit import should_skip, stable_hash

import pick_builder_engine as eng
from pick_builder_settings import Settings
from pick_builder_store import PickStore

Say = Callable[..., None]
Decide = Callable[..., None]


@dataclass
class Report:
    status: str = "success"
    summary: str = ""
    summary_md: str = ""
    metrics: dict = field(default_factory=dict)
    state: dict = field(default_factory=dict)
    error_text: str = ""
    short_circuited: bool = False
    plan: dict = field(default_factory=dict)      # --plan output (dry run)


def _canon(v: Any) -> str:
    return json.dumps(v, sort_keys=True, default=str, separators=(",", ":"))


def _pid(v: Any) -> Optional[str]:
    return None if v is None else str(v)


def _status_of(row: Mapping[str, Any]) -> str:
    """A stored slot's evidence status (filled | empty | not-needed)."""
    ev = row.get("eligibility")
    if isinstance(ev, str):
        try:
            ev = json.loads(ev)
        except ValueError:
            ev = None
    if isinstance(ev, Mapping) and ev.get("status"):
        return str(ev["status"])
    if _pid(row.get("product_id")) is not None:
        return eng.STATUS_FILLED
    return eng.STATUS_NOT_NEEDED if row.get("reason") == eng.DIRECT_CONNECTION else eng.STATUS_EMPTY


class PickBuild:
    def __init__(self, settings: Settings, store: PickStore, *, storage, prior_state: Mapping[str, Any],
                 say: Say, decide: Decide, now: Optional[datetime] = None, run_ts: str = "",
                 dry_run: bool = False):
        self.s = settings
        self.b = settings.block
        self.store = store
        self.storage = storage
        self.prior = dict(prior_state or {})
        self.say = say
        self.decide = decide
        self.now = now or datetime.now(timezone.utc)
        self.run_ts = run_ts
        self.dry_run = dry_run
        self.hidden = settings.hidden
        self.m: dict[str, float] = collections.defaultdict(float)
        self.notes: list[str] = []
        self.warnings: list[str] = []
        self.state: dict[str, Any] = {}

    # ------------------------------------------------------------------
    def _slot_cols(self) -> Mapping[str, Optional[str]]:
        return self.b["ladders"]["slots"]["columns"]

    def _slot_tuple(self, r: Mapping[str, Any]) -> tuple:
        c = self._slot_cols()
        t = (str(r.get("role")), int(r.get("rank") or 0), _pid(r.get("product_id")),
             (r.get("reason") or "") if c.get("reason") else "")
        if c.get("eligibility"):
            t += (_canon(r.get("eligibility")),)
        return t

    def _item_tuple(self, r: Mapping[str, Any]) -> tuple:
        c = self.b["lists"]["items"]["columns"]
        return (int(r.get("rank") or 0), _pid(r.get("product_id")),
                (r.get("reason") or "") if c.get("reason") else "")

    def _sub_tuple(self, r: Mapping[str, Any]) -> tuple:
        c = self.b["substitutes"]["output"]["columns"]
        return (int(r.get("rank") or 0), _pid(r.get("substitute_id")),
                (r.get("reason") or "") if c.get("reason") else "")

    def _outputs_sig(self, slots, items, subs) -> str:
        """One hash of the stored outputs. Groups without rows are skipped:
        a list left empty is absent from the table and from this hash alike."""
        def part(groups, fn):
            return sorted((k, sorted(fn(r) for r in v)) for k, v in groups.items() if v)
        return stable_hash({"slots": part(slots, self._slot_tuple), "items": part(items, self._item_tuple),
                            "subs": part(subs, self._sub_tuple)})

    # ------------------------------------------------------------------
    def run(self) -> Report:
        rep = Report()
        b = self.b
        self.say("loading the eligibility pool", progress=0.05)
        self.store.begin_snapshot(read_only=self.dry_run)
        pool, rejected, dup = self.store.load_pool()
        self.m["pool_size"] = float(len(pool))
        self.m["pool_integrity_rejects"] = float(sum(rejected.values()))
        self.m["pool_duplicates"] = float(dup)
        if rejected:
            self.warnings.append(f"the eligibility view returned rows that fail the pricing-integrity "
                                 f"floor (never picked): {dict(sorted(rejected.items()))}")
        pool_ids = {p.key for p in pool}
        lad_on, lst_on, sub_on = (bool(b[k].get("enabled")) for k in ("ladders", "lists", "substitutes"))
        plans: Mapping[str, Sequence[Mapping[str, Any]]] = b["ladders"].get("plans") or {}
        relations = {str(s[k]["relation"]) for plan in plans.values() for s in plan
                     for k in ("compat", "via_of") if isinstance(s.get(k), Mapping)}
        compat = self.store.load_compat(relations) if lad_on else eng.CompatIndex()
        if compat.duplicate_pairs:
            self.m["compat_duplicate_pairs"] = float(compat.duplicate_pairs)
        ladders = self.store.load_ladders() if lad_on else []
        tiers = self.store.load_tiers([l["id"] for l in ladders]) if lad_on else []
        slots = self.store.load_slots([t["id"] for t in tiers]) if lad_on else {}
        lists = self.store.load_lists() if lst_on else []
        items = self.store.load_items([l["id"] for l in lists]) if lst_on else {}
        subjects = self.store.load_subjects() if sub_on else []
        subs = self.store.load_substitutes() if sub_on else {}

        inputs_sig = stable_hash({
            "pool": [[p.key, p.asin, p.kind, p.price, p.model_key, p.list_price, p.currency,
                      p.category_confidence, sorted(p.attrs.items())] for p in pool],
            "compat": compat.signature(),
            "ladders": [[str(l["id"]), l.get("slug"), l.get("kind")] for l in ladders],
            "tiers": [[str(t["id"]), str(t["ladder_id"]), t.get("budget"), sorted(t["params"].items())]
                      for t in tiers],
            "lists": [[str(l["id"]), l.get("slug"), l.get("kind"), sorted(l["params"].items())] for l in lists],
            "subjects": [[s.key, s.kind, s.asin, None if s.model_id is None else str(s.model_id), s.unbuyable,
                          sorted(s.attrs.items())] for s in subjects],
            "cfg": {k: b[k] for k in ("eligibility", "compat", "ladders", "lists", "substitutes", "publish")},
        }, float_precision=6)
        existing_sig = self._outputs_sig(slots, items, subs)
        found = self._ineligible(slots, items, subs, pool_ids)
        self.m["ineligible_found"] = float(found)

        if (not self.dry_run and should_skip(self.prior, "inputs_sig", inputs_sig)
                and self.prior.get("outputs_sig") == existing_sig and self._published_ok()):
            self.store.rollback()
            rep.short_circuited = True
            self.notes.append("inputs and outputs unchanged since the last run: nothing rebuilt")
            self._metrics_from_rows(slots, items, subs, subjects, lists, pool_ids)
            self.state = {**{k: v for k, v in self.prior.items()}, "inputs_sig": inputs_sig,
                          "outputs_sig": existing_sig}
            if self.b["publish"].get("enabled"):
                # verified just now: storage holds the list the last run published
                self.m["pick_publish_failed"] = 0.0
                self.m["pick_asins_published"] = float(len(self.prior.get("published") or []))
            rep.state = self.state
            self._verify(rep)
            return self._finish(rep)

        self.say("filling ladders, lists and substitutes", progress=0.3)
        desired_slots: dict[str, list[dict]] = {}
        slot_traces: dict[str, list[str]] = {}
        if lad_on:
            fit = list(b["compat"].get("fit_verdicts") or [])
            excl = list(b["compat"].get("exclude_flags") or [])
            view = str(b["eligibility"]["relation"])
            by_ladder: dict[str, list[dict]] = collections.defaultdict(list)
            for t in tiers:
                by_ladder[str(t["ladder_id"])].append(t)
            for lad in ladders:
                plan = plans.get(str(lad.get("kind"))) or plans.get("default")
                for t in by_ladder.get(str(lad["id"]), []):
                    if plan is None:
                        rows = self._sweep_slots(slots.get(str(t["id"]), []), pool_ids, lad)
                        desired_slots[str(t["id"])] = rows
                        continue
                    outs = eng.fill_tier(t, {"id": lad["id"], "slug": lad.get("slug"), "kind": lad.get("kind")},
                                         plan, pool, compat=compat,
                                         enforce_budget=bool(b["ladders"]["budget"].get("enforce", True)),
                                         fit_verdicts=fit, exclude_flags=excl, view=view, hidden=self.hidden)
                    desired_slots[str(t["id"])] = [
                        {"tier_id": t["id"], "role": o.role, "rank": o.rank,
                         "product_id": None if o.product is None else o.product.product_id,
                         "reason": o.reason, "eligibility": o.evidence} for o in outs]
                    slot_traces[str(t["id"])] = [o.trace for o in outs]
                if plan is None:
                    self.m["ladders_without_plan"] += 1
                    self.warnings.append(f"ladder {lad.get('slug')}: no plan for kind {lad.get('kind')!r} "
                                         "(its slots are only swept for eligibility)")
        desired_items: dict[str, list[dict]] = {}
        list_sizes: dict[str, int] = {}
        list_notes: dict[str, dict] = {}
        if lst_on:
            kinds = b["lists"].get("kinds") or {}
            for lst in lists:
                spec = kinds.get(str(lst.get("kind")))
                if spec is None:
                    self.m["lists_without_selector"] += 1
                    self.warnings.append(f"list {lst.get('slug')}: no selector for kind {lst.get('kind')!r} "
                                         "(its items are only swept for eligibility)")
                    desired_items[str(lst["id"])] = [dict(r) for r in items.get(str(lst["id"]), [])
                                                     if _pid(r.get("product_id")) in pool_ids]
                    continue
                lf = eng.fill_list(lst, spec, pool, hidden=self.hidden)
                list_sizes[str(lst["id"])] = lf.size
                desired_items[str(lst["id"])] = [
                    {"list_id": lst["id"], "rank": o.rank, "product_id": o.product.product_id,
                     "reason": o.reason} for o in lf.outputs if o.product is not None]
                if lf.short:
                    self.m["lists_short"] += 1
                    if not lf.outputs:
                        self.m["lists_empty"] += 1
                    list_notes[str(lst["id"])] = {"slug": lst.get("slug"), "filled": len(lf.outputs),
                                                  "size": lf.size, "reason": lf.reason, "trace": lf.trace}
                    self.warnings.append(f"list {lst.get('slug')}: {len(lf.outputs)}/{lf.size} filled: "
                                         f"{lf.reason}"[:300])
        desired_subs: dict[str, list[dict]] = {}
        why_none: dict[str, str] = {}
        if sub_on:
            sb = b["substitutes"]
            built, why_none = eng.build_substitutes(
                subjects, pool, sb.get("rules") or {}, max_per_subject=int(sb.get("max_per_subject", 3)),
                same_model=str(sb.get("same_model", "prefer")), one_per_model=bool(sb.get("one_per_model", True)),
                hidden=self.hidden)
            for sid, outs in built.items():
                desired_subs[sid] = [{"subject_id": o.group, "rank": o.rank,
                                      "substitute_id": o.product.product_id, "reason": o.reason}
                                     for o in outs if o.product is not None]
            for sid in subs:
                desired_subs.setdefault(sid, [])     # subjects no longer covered lose their rows

        # -- diff and write --------------------------------------------
        slot_changes = {k: v for k, v in desired_slots.items()
                        if sorted(self._slot_tuple(r) for r in v)
                        != sorted(self._slot_tuple(r) for r in slots.get(k, []))}
        item_changes = {k: v for k, v in desired_items.items()
                        if sorted(self._item_tuple(r) for r in v)
                        != sorted(self._item_tuple(r) for r in items.get(k, []))}
        sub_changes = {k: v for k, v in desired_subs.items()
                       if sorted(self._sub_tuple(r) for r in v)
                       != sorted(self._sub_tuple(r) for r in subs.get(k, []))}
        new_slots = {**slots, **desired_slots}
        new_items = {**items, **desired_items}
        new_subs = {k: v for k, v in {**subs, **desired_subs}.items() if v}
        self._metrics_from_rows(new_slots, new_items, new_subs, subjects, lists, pool_ids,
                                list_sizes=list_sizes)
        if why_none:
            self.m["substitute_subjects_without"] = float(sum(1 for v in why_none.values() if v))
        rep.plan = {"slots": {k: v for k, v in desired_slots.items()}, "slot_traces": slot_traces,
                    "items": desired_items, "lists": list_notes,
                    "substitutes": desired_subs, "changes": {"tiers": len(slot_changes), "lists": len(item_changes),
                                                             "subjects": len(sub_changes)}}
        if self.dry_run:
            self.store.rollback()
            self.notes.append(f"[dry-run] would rewrite {len(slot_changes)} tier(s), {len(item_changes)} "
                              f"list(s), {len(sub_changes)} subject(s)")
            return self._finish(rep)
        try:
            ins = dels = 0
            if slot_changes:
                sb = b["ladders"]["slots"]
                i, d = self.store.replace_groups(sb["table"], sb["columns"], "tier_id", slot_changes)
                ins, dels = ins + i, dels + d
            if item_changes:
                ib = b["lists"]["items"]
                i, d = self.store.replace_groups(ib["table"], ib["columns"], "list_id", item_changes)
                ins, dels = ins + i, dels + d
            if sub_changes:
                ob = b["substitutes"]["output"]
                i, d = self.store.replace_groups(ob["table"], ob["columns"], "subject_id", sub_changes)
                ins, dels = ins + i, dels + d
            self.store.commit()
        except Exception:
            self.store.rollback()
            raise
        self.m["groups_written"] = float(len(slot_changes) + len(item_changes) + len(sub_changes))
        self.m["rows_inserted"] = float(ins)
        self.m["rows_deleted"] = float(dels)
        out_sig = self._outputs_sig(new_slots, new_items, new_subs)
        # The outputs are committed: from here on a failure must not lose
        # them, and until the list is published and read back the next run
        # must not short-circuit (publish_pending).
        self.state = {**{k: v for k, v in self.prior.items()
                         if k in ("published", "published_current", "dropped")},
                      "inputs_sig": inputs_sig, "outputs_sig": out_sig,
                      "publish_pending": bool(b["publish"].get("enabled"))}
        rep.state = self.state
        self._publish(rep, ladders, tiers, plans, new_slots, lists, new_items, new_subs, pool)
        self._verify(rep)
        return self._finish(rep)

    # ------------------------------------------------------------------
    def _sweep_slots(self, rows: Sequence[Mapping[str, Any]], pool_ids: set[str], lad: Mapping[str, Any]
                     ) -> list[dict]:
        out = []
        for r in rows:
            r = dict(r)
            if _pid(r.get("product_id")) is not None and _pid(r.get("product_id")) not in pool_ids:
                r["product_id"] = None
                r["reason"] = f"no longer eligible (ladder kind {lad.get('kind')!r} has no plan)"
                r["eligibility"] = {"status": eng.STATUS_EMPTY, "view": self.b["eligibility"]["relation"]}
            out.append(r)
        return out

    def _ineligible(self, slots, items, subs, pool_ids: set[str]) -> int:
        n = 0
        for rows in slots.values():
            n += sum(1 for r in rows if _pid(r.get("product_id")) is not None and _pid(r.get("product_id")) not in pool_ids)
        for rows in items.values():
            n += sum(1 for r in rows if _pid(r.get("product_id")) is not None and _pid(r.get("product_id")) not in pool_ids)
        for rows in subs.values():
            n += sum(1 for r in rows if _pid(r.get("substitute_id")) is not None
                     and _pid(r.get("substitute_id")) not in pool_ids)
        return n

    def _metrics_from_rows(self, slots, items, subs, subjects, lists, pool_ids, *,
                           list_sizes: Optional[Mapping[str, int]] = None) -> None:
        """Fill rates. A slot a direct connection makes unnecessary
        (``not-needed``) is a complete kit, not a gap: it is left out of the
        denominators."""
        all_slots = [r for rows in slots.values() for r in rows]
        filled = sum(1 for r in all_slots if _pid(r.get("product_id")) is not None)
        not_needed = sum(1 for r in all_slots
                         if _pid(r.get("product_id")) is None and _status_of(r) == eng.STATUS_NOT_NEEDED)
        needed = len(all_slots) - not_needed
        self.m["ladder_slots_total"] = float(len(all_slots))
        self.m["ladder_slots_filled"] = float(filled)
        self.m["ladder_slots_not_needed"] = float(not_needed)
        self.m["ladder_slots_empty"] = float(needed - filled)
        kinds = self.b["lists"].get("kinds") or {}
        if list_sizes is None:
            list_sizes = {str(l["id"]): eng.list_size(l, kinds[str(l.get("kind"))])
                          for l in lists if str(l.get("kind")) in kinds}
        positions = sum(list_sizes.values())
        list_filled = sum(min(len(items.get(k, [])), n) for k, n in list_sizes.items())
        self.m["list_positions_total"] = float(positions)
        self.m["list_items_filled"] = float(list_filled)
        total = needed + positions
        self.m["slots_filled_pct"] = round(100.0 * (filled + list_filled) / total, 2) if total else 0.0
        if self.b["ladders"].get("enabled"):
            self.m["ladder_slots_filled_pct"] = (round(100.0 * filled / needed, 2) if needed else 0.0)
        rules = self.b["substitutes"].get("rules") or {}
        covered = [s for s in subjects if s.kind in rules]
        with_sub = [s for s in covered if subs.get(s.key)]
        unbuyable = [s for s in covered if s.unbuyable]
        unbuyable_with = [s for s in unbuyable if subs.get(s.key)]
        self.m["substitute_subjects"] = float(len(covered))
        self.m["substitute_subjects_with"] = float(len(with_sub))
        self.m["unbuyable_subjects"] = float(len(unbuyable))
        self.m["unbuyable_with_substitute"] = float(len(unbuyable_with))
        if self.b["substitutes"].get("enabled"):
            self.m["unbuyable_with_substitute_pct"] = (round(100.0 * len(unbuyable_with) / len(unbuyable), 2)
                                                       if unbuyable else 100.0)

    def _published_ok(self) -> bool:
        """The last publish succeeded and storage still holds that list."""
        if not self.b["publish"].get("enabled"):
            return True
        if self.prior.get("publish_pending") or self.prior.get("published") is None:
            return False
        try:
            doc = self.storage.read_json(self.s.publish_key)
        except Exception:  # noqa: BLE001
            return False
        return isinstance(doc, Mapping) and doc.get("asins") == self.prior.get("published")

    def _publish(self, rep: Report, ladders, tiers, plans, slots, lists, items, subs,
                 pool: Sequence[eng.Product]) -> None:
        pub = self.b["publish"]
        if not pub.get("enabled"):
            return
        asin_of = {p.key: p.asin for p in pool}
        include = set(pub.get("include") or [])
        order: list[str] = []
        if "ladders" in include:
            role_pos: dict[tuple[str, str], int] = {}
            for lad in ladders:
                plan = plans.get(str(lad.get("kind"))) or plans.get("default") or []
                for i, s in enumerate(plan):
                    role_pos[(str(lad["id"]), str(s["role"]))] = i
            tier_ladder = {str(t["id"]): str(t["ladder_id"]) for t in tiers}
            tier_pos = {str(t["id"]): i for i, t in enumerate(tiers)}
            rows = [r for k, v in slots.items() for r in v if _pid(r.get("product_id")) is not None]
            rows.sort(key=lambda r: (tier_pos.get(str(r["tier_id"]), 10 ** 6),
                                     role_pos.get((tier_ladder.get(str(r["tier_id"]), ""), str(r["role"])), 99),
                                     int(r.get("rank") or 0)))
            order += [asin_of.get(_pid(r["product_id"]), "") for r in rows]
        if "lists" in include:
            for lst in lists:
                order += [asin_of.get(_pid(r["product_id"]), "")
                          for r in sorted(items.get(str(lst["id"]), []), key=lambda r: int(r.get("rank") or 0))]
        if "substitutes" in include:
            for sid in sorted(subs):
                order += [asin_of.get(_pid(r["substitute_id"]), "")
                          for r in sorted(subs[sid], key=lambda r: int(r.get("rank") or 0))]
        current = list(dict.fromkeys(a for a in order if a))
        keep = int(pub.get("keep_dropped_runs", 1))
        prev = [a for a in (self.prior.get("published_current") or []) if a]
        dropped: dict[str, int] = {}
        if keep > 0:
            for a, n in (self.prior.get("dropped") or {}).items():
                if a not in current and int(n) - 1 > 0:
                    dropped[a] = int(n) - 1
            for a in prev:
                if a not in current:
                    dropped[a] = max(dropped.get(a, 0), keep)
        published = current + sorted(a for a in dropped if a not in current)
        doc = {"asins": published, "run_ts": self.run_ts, "source": self.s.agent_id,
               "generated_at": self.now.astimezone(timezone.utc).isoformat(timespec="seconds"),
               "counts": {"current": len(current), "recently_dropped": len(published) - len(current)}}
        try:
            self.storage.write_json(self.s.publish_key, doc)
            back = self.storage.read_json(self.s.publish_key)
            if not isinstance(back, Mapping) or back.get("asins") != published:
                raise RuntimeError("the list read back from storage differs from the list written")
        except Exception as e:  # noqa: BLE001 - the refresh keeps reading the previous list
            self.m["pick_publish_failed"] = 1.0
            self.m["pick_asins_published"] = 0.0
            self.warnings.append(f"publishing the pick-ASIN list failed (the next run publishes again): "
                                 f"{type(e).__name__}: {e}"[:240])
            # The previous successful publish stays the reference; the
            # pending flag (set at commit) keeps the next run from
            # short-circuiting.
            self.state["publish_pending"] = True
            return
        self.m["pick_asins_published"] = float(len(published))
        self.m["pick_publish_failed"] = 0.0
        # explicit False: the agent merges this state over the prior one
        self.state.update({"published": published, "published_current": current, "dropped": dropped,
                           "publish_pending": False})

    def _verify(self, rep: Report) -> None:
        """Fresh transaction: live picks that are no longer eligible."""
        if self.dry_run:
            return
        try:
            self.store.rollback()
            pool, _rej, _dup = self.store.load_pool()
            ids = {p.key for p in pool}
            b = self.b
            slots = items = subs = {}
            if b["ladders"].get("enabled"):
                ladders = self.store.load_ladders()
                tiers = self.store.load_tiers([l["id"] for l in ladders])
                slots = self.store.load_slots([t["id"] for t in tiers])
            if b["lists"].get("enabled"):
                items = self.store.load_items([l["id"] for l in self.store.load_lists()])
            if b["substitutes"].get("enabled"):
                subs = self.store.load_substitutes()
            self.store.rollback()
            self.m["ineligible_in_picks"] = float(self._ineligible(slots, items, subs, ids))
        except Exception as e:  # noqa: BLE001
            self.warnings.append(f"post-run eligibility check failed: {type(e).__name__}: {e}"[:200])
            self.store.rollback()

    def _finish(self, rep: Report) -> Report:
        g = self.m.get
        rep.metrics = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in sorted(self.m.items())}
        if g("ineligible_in_picks", 0) > 0:
            # Normally a price that aged out of the freshness window in the
            # seconds between the build and the check; the goal tracks it.
            self.warnings.append(f"{int(g('ineligible_in_picks'))} live pick(s) are outside the "
                                 "eligibility pool after the run (the next run replaces them)")
            self.decide("warning", self.warnings[-1])
        if g("pick_publish_failed", 0) > 0:
            self.decide("warning", "the pick-ASIN list was not published; the next run publishes again")
        head = "short-circuited: inputs unchanged | " if rep.short_circuited else ""
        parts = []
        if self.b["ladders"].get("enabled"):
            nn = int(g("ladder_slots_not_needed", 0))
            parts.append(f"ladder slots {int(g('ladder_slots_filled', 0))}/"
                         f"{int(g('ladder_slots_total', 0)) - nn} filled"
                         + (f" ({nn} not needed)" if nn else ""))
        if self.b["lists"].get("enabled"):
            parts.append(f"list positions {int(g('list_items_filled', 0))}/{int(g('list_positions_total', 0))}")
        if self.b["substitutes"].get("enabled"):
            parts.append(f"unbuyable with a substitute {int(g('unbuyable_with_substitute', 0))}/"
                         f"{int(g('unbuyable_subjects', 0))}")
        parts.append(f"{int(g('pool_size', 0))} eligible")
        parts.append(f"{int(g('ineligible_in_picks', 0))} ineligible in picks")
        if self.b["publish"].get("enabled"):
            if g("pick_publish_failed", 0) > 0:
                parts.append("pick ASIN list NOT published (retried next run)")
            else:
                parts.append(f"{int(g('pick_asins_published', 0))} pick ASIN(s) published")
        rep.summary = (head + "; ".join(parts))[:500]
        lines = [f"## pick-builder run ({rep.status})", "", rep.summary, ""]
        if self.notes:
            lines += ["### Notes", *[f"- {n}" for n in self.notes], ""]
        if self.warnings:
            lines += ["### Warnings", *[f"- {w}" for w in self.warnings[:30]], ""]
        lines += ["### Metrics", *[f"- {k}: {v:g}" for k, v in rep.metrics.items()]]
        rep.summary_md = "\n".join(lines)
        return rep
