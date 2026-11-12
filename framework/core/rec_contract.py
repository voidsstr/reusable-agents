"""Rec contracts: a rec its producer wrote under an older contract is held.
It is not dispatched and not implemented until the producer writes it again
under the current contract, or a person releases it.

WHY THIS EXISTS
---------------
When a producer changes what its recs mean (a new triage step, a new guard,
a new field the implementer relies on), the change reaches only the recs it
writes from then on. The recs it wrote before stay in storage, and the
backlog dispatcher replays every open rec it finds in a producer's run-dirs.

2026-11-12: 200 open catalog-audit image/name recs, written in 35 runs
before the image/name rule existed, were still being replayed. Each one told
the implementer to "deactivate the product (UPDATE products SET
is_active=false WHERE slug=...)". None of them had been through the rule's
brand/identity triage or the protect set: both run only inside the
producer's run(), on fresh findings. They also named a slug, and that
site's slugs are not unique. 15 of them named active products, and 7 of
those recs (6 products) were in the protect set (article-linked, featured
or clicked). They stranded only because an unrelated bug made the
implementer discard those runs. Fixing that bug would have shipped them.

A contract is data. Each rule names the recs it covers (`match`, optionally
`producers`) and the fields a covered rec must carry (`require_any`, at least
one of them truthy). A covered rec that carries none of them is HELD.

WHERE IT IS ENFORCED
--------------------
  backlog dispatcher   skips held recs when it picks candidates
                       (agents/backlog-dispatcher-agent).
  implementer          defers held recs before any LLM path
                       (agents/implementer/run.sh). It also marks them
                       review_required with a contract_hold block in the run's
                       recommendations.json. That file is synced back, so the
                       recs become review items and are no longer offered.
  sweep                `python3 -m framework.cli.rec_contract sweep` marks
                       every open held rec in the named producers' run-dirs
                       the same way, in one pass. It is a dry run unless
                       --apply is given.

RELEASING A HELD REC
--------------------
Re-run the producer: its new recs satisfy the contract. Or set
`contract_override: true` on the rec, which records that a person decided.

CONFIG
------
DEFAULT_CONFIG, then storage `config/rec-contract-config.json`:

    {"schema_version": "1",
     "rules": [...],              # replaces the default rule list
     "extra_rules": [...],        # appended to it
     "disabled_rules": ["<id>"]}  # rule ids to drop

A rule is:

    {"id": "...",
     "producers": ["*-catalog-audit-agent"],  # fnmatch globs; default ["*"]
     "match": {"check_id": "..."},            # field -> value or list of values
     "require_any": ["identity_rule"],
     "why": "...", "release": "..."}

If the storage config cannot be read, the defaults apply. A gate that fails
open would replay exactly the recs it exists to hold.
"""
from __future__ import annotations

import fnmatch
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

CONFIG_KEY = "config/rec-contract-config.json"

DEFAULT_CONFIG: dict = {
    "schema_version": "1",
    "rules": [
        {
            "id": "image-name-identity-rule",
            # Matched on the check alone, so a batch whose producer is not
            # known is still held. Only the catalog audit emits this check.
            "match": {"check_id": "product-image-matches-name"},
            "require_any": ["identity_rule"],
            "since": "2026-11-12",
            "why": ("written before the catalog audit's image/name rule. It never went "
                    "through brand/identity triage or the protect set, and its outline "
                    "deactivates by slug, which may name several products"),
            "release": ("re-run the catalog audit (its new recs carry identity_rule), or set "
                        "contract_override: true on the rec after checking it by hand"),
        },
    ],
    "extra_rules": [],
    "disabled_rules": [],
}

#: rec flags under which the rec is closed (the backlog dispatcher's list)
CLOSED_FLAGS = ("shipped", "implemented", "deferred", "duplicate", "skipped")


def load_config(*, storage=None, file_cfg: Any = None) -> dict:
    """DEFAULT_CONFIG <- storage config. `file_cfg` skips the storage read
    (tests). An unreadable or malformed storage config yields the defaults."""
    if file_cfg is None:
        try:
            from framework.core.storage import get_storage
            file_cfg = (storage or get_storage()).read_json(CONFIG_KEY) or {}
        except Exception:  # noqa: BLE001 -- unreadable config: the defaults hold
            file_cfg = {}
    if not isinstance(file_cfg, Mapping):
        file_cfg = {}
    out = json.loads(json.dumps(DEFAULT_CONFIG))
    if isinstance(file_cfg.get("rules"), list):
        out["rules"] = [r for r in file_cfg["rules"] if isinstance(r, Mapping)]
    if isinstance(file_cfg.get("extra_rules"), list):
        out["rules"] += [r for r in file_cfg["extra_rules"] if isinstance(r, Mapping)]
    disabled = set(str(x) for x in file_cfg.get("disabled_rules") or [])
    out["rules"] = [dict(r) for r in out["rules"] if str(r.get("id") or "") not in disabled]
    out["disabled_rules"] = sorted(disabled)
    return out


def _matches(rec: Mapping, match: Mapping) -> bool:
    for field, want in (match or {}).items():
        have = rec.get(field)
        wants = want if isinstance(want, (list, tuple, set)) else [want]
        if str(have if have is not None else "") not in {str(w) for w in wants}:
            return False
    return True


def _producer_ok(producer: str, globs: Optional[Iterable[str]]) -> bool:
    globs = list(globs or ["*"])
    return any(fnmatch.fnmatchcase(producer or "", g) for g in globs)


def hold_reason(rec: Any, *, producer: str = "", cfg: Optional[Mapping] = None) -> Optional[dict]:
    """The rule a rec breaks, as {"rule", "why", "release"}, or None when it
    may be dispatched. `producer` is the agent id whose run-dir holds the
    rec. A rec with `contract_override: true` is never held."""
    if not isinstance(rec, Mapping) or rec.get("contract_override") is True:
        return None
    cfg = cfg if cfg is not None else load_config()
    for rule in cfg.get("rules") or []:
        if not isinstance(rule, Mapping) or not rule.get("match"):
            continue  # a rule with no match would hold every rec; ignore it
        if not _producer_ok(producer, rule.get("producers")):
            continue
        if not _matches(rec, rule["match"]):
            continue
        if any(rec.get(f) for f in rule.get("require_any") or []):
            continue
        return {"rule": str(rule.get("id") or "?"),
                "why": str(rule.get("why") or "written under an older producer contract"),
                "release": str(rule.get("release") or "set contract_override: true")}
    return None


def is_open(rec: Mapping) -> bool:
    """Not shipped, implemented, deferred, duplicate or skipped."""
    return not any(rec.get(f) for f in CLOSED_FLAGS)


def is_dispatchable(rec: Mapping) -> bool:
    """What the backlog dispatcher would offer, ignoring contracts: open, and
    not a review item awaiting a person."""
    return is_open(rec) and not (rec.get("review_required")
                                 and not rec.get("confirmed_for_implementation"))


def mark_held(rec: dict, hold: Mapping, *, now: Optional[str] = None, by: str = "") -> bool:
    """Turn a held rec into a review item: review_required, plus a
    contract_hold block saying which rule held it and how to release it.
    Returns False when it already carries this rule's hold."""
    prior = rec.get("contract_hold") or {}
    if rec.get("review_required") and isinstance(prior, Mapping) and prior.get("rule") == hold["rule"]:
        return False
    rec["review_required"] = True
    rec["contract_hold"] = {
        "rule": hold["rule"], "why": hold["why"], "release": hold["release"],
        "at": now or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **({"by": by} if by else {}),
    }
    return True


def filter_batch(recs: Iterable[Any], rec_ids: Iterable[str], *, producer: str = "",
                 cfg: Optional[Mapping] = None) -> tuple[list[str], list[tuple[str, dict]]]:
    """Split a dispatched batch: -> (ids to keep, [(held id, hold)]). An id
    whose rec is not in `recs` is kept; this gate only holds what it can read."""
    cfg = cfg if cfg is not None else load_config()
    by_id = {}
    for r in recs or []:
        if isinstance(r, Mapping):
            rid = r.get("id") or r.get("rec_id") or r.get("rec_uid")
            if rid:
                by_id[str(rid)] = r
    keep: list[str] = []
    held: list[tuple[str, dict]] = []
    for rid in rec_ids:
        rid = str(rid).strip()
        if not rid:
            continue
        hold = hold_reason(by_id.get(rid) or {}, producer=producer, cfg=cfg)
        if hold:
            held.append((rid, hold))
        else:
            keep.append(rid)
    return keep, held


def filter_run_dir(run_dir: str, rec_ids: Iterable[str], *, producer: str = "",
                   cfg: Optional[Mapping] = None, mark: bool = True) -> tuple[list[str], list]:
    """filter_batch over <run_dir>/recommendations.json, for the implementer.

    For each held rec it writes changes/<id>.summary.md (DEFERRED: ...), and
    with `mark` it marks the rec held in recommendations.json. The implementer
    syncs that file back, so the producer's copy shows the rec as a review item
    and the dispatcher stops offering it."""
    rd = Path(run_dir)
    path = rd / "recommendations.json"
    doc = json.loads(path.read_text())
    recs = doc.get("recommendations") if isinstance(doc, Mapping) else doc
    recs = recs if isinstance(recs, list) else []
    if not producer and isinstance(doc, Mapping):
        producer = str(doc.get("agent_id") or doc.get("agent") or "")
    keep, held = filter_batch(recs, list(rec_ids), producer=producer, cfg=cfg)
    if not held:
        return keep, held
    (rd / "changes").mkdir(exist_ok=True)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    held_ids = {rid for rid, _ in held}
    for rid, hold in held:
        (rd / "changes" / f"{rid}.summary.md").write_text(
            "DEFERRED: held by rec contract %r: %s. Release: %s.\n"
            % (hold["rule"], hold["why"], hold["release"]))
    if mark:
        holds = dict(held)
        changed = 0
        for r in recs:
            rid = str(r.get("id") or r.get("rec_id") or "") if isinstance(r, dict) else ""
            if rid in held_ids:
                changed += mark_held(r, holds[rid], now=now, by="implementer")
        if changed:
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(doc, indent=2, default=str))
            os.replace(tmp, path)
    return keep, held


def run_keys(storage, agent_id: str) -> list[str]:
    """Every recommendations.json key in an agent's run-dirs: the run-index
    plus the run-dir listing (either may miss runs on its own)."""
    keys: set[str] = set()
    try:
        idx = storage.read_json(f"agents/{agent_id}/run-index.json") or {}
        for e in (idx.get("recent") or []) if isinstance(idx, Mapping) else []:
            if isinstance(e, Mapping) and e.get("run_ts"):
                keys.add(f"agents/{agent_id}/runs/{e['run_ts']}/recommendations.json")
    except Exception:  # noqa: BLE001
        pass
    prefix = f"agents/{agent_id}/runs/"
    try:
        for ts in storage.list_child_prefixes(prefix) or []:
            keys.add(f"{prefix}{ts}/recommendations.json")
    except Exception:  # noqa: BLE001
        for k in storage.list_prefix(prefix) or []:
            if k.endswith("/recommendations.json"):
                keys.add(k)
    return sorted(keys)


def sweep(storage, agent_ids: Iterable[str], *, cfg: Optional[Mapping] = None,
          apply: bool = False, now: Optional[str] = None) -> dict:
    """Find, and with `apply` mark, every open held rec in the agents'
    run-dirs. A dry run reads only.

    -> {"apply", "agents": {aid: {"runs_scanned", "runs_with_held",
        "held_dispatchable", "held_review", "marked"}}, "held": [{agent,
        run_ts, rec_id, rule, dispatchable, title, ref_ids}], "totals"}

    held_dispatchable counts recs the dispatcher would offer today, and
    held_review counts recs already waiting for a person."""
    cfg = cfg if cfg is not None else load_config(storage=storage)
    now = now or datetime.now(timezone.utc).isoformat(timespec="seconds")
    report: dict = {"apply": apply, "agents": {}, "held": [], "errors": []}
    for aid in agent_ids:
        st = {"runs_scanned": 0, "runs_with_held": 0, "held_dispatchable": 0,
              "held_review": 0, "marked": 0}
        for key in run_keys(storage, aid):
            try:
                doc = storage.read_json(key)
            except Exception as e:  # noqa: BLE001
                report["errors"].append("%s: %s" % (key, str(e)[:120]))
                continue
            if doc is None:
                continue
            st["runs_scanned"] += 1
            recs = doc.get("recommendations") if isinstance(doc, Mapping) else doc
            if not isinstance(recs, list):
                continue
            run_ts = key.rsplit("/runs/", 1)[-1].split("/", 1)[0]
            changed = 0
            any_held = False
            for r in recs:
                if not isinstance(r, dict) or not is_open(r):
                    continue
                hold = hold_reason(r, producer=aid, cfg=cfg)
                if not hold:
                    continue
                any_held = True
                disp = is_dispatchable(r)
                st["held_dispatchable" if disp else "held_review"] += 1
                report["held"].append({
                    "agent": aid, "run_ts": run_ts, "rec_id": str(r.get("id") or ""),
                    "rule": hold["rule"], "dispatchable": disp,
                    "title": str(r.get("title") or "")[:140],
                    "ref_ids": [str(x) for x in (r.get("ref_ids") or [])][:5]})
                if apply and mark_held(r, hold, now=now, by="rec-contract sweep"):
                    changed += 1
            st["runs_with_held"] += any_held
            if apply and changed:
                storage.write_json(key, doc)
                st["marked"] += changed
        report["agents"][aid] = st
    report["totals"] = {k: sum(a[k] for a in report["agents"].values())
                        for k in ("runs_scanned", "runs_with_held", "held_dispatchable",
                                  "held_review", "marked")}
    return report


__all__ = ["CLOSED_FLAGS", "CONFIG_KEY", "DEFAULT_CONFIG", "filter_batch", "filter_run_dir",
           "hold_reason", "is_dispatchable", "is_open", "load_config", "mark_held",
           "run_keys", "sweep"]
