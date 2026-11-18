#!/usr/bin/env python3
"""Amazon Catalog Discovery: grow a site's catalog from Creators searchItems.

An AgentBase wrapper on ``framework.core.amazon_discovery.discover()``. Each
run, for the site named by its config (``CATALOG_DISCOVERY_CONFIG``):

1. SEEDS   -- imports seed ASINs (JSON files, read-only SQL on another DB),
              no Amazon calls; unchanged sources are skipped.
2. SEARCH  -- walks the config's lane seeds (x brands x price bands) one
              itemPage at a time, breadth first and resumed across runs,
              within the run's call allowance; classifies each new listing
              into the site's own lanes and inserts the in-lane ASINs.
3. REPORT  -- metrics for the declared goals, a per-run report of ASINs and
              our labels, and the rotation state for the next run.

It persists ONLY the ASIN and our own labels (lane, kind/category, confidence,
provenance). Titles, prices, images and every other Amazon field are read in
memory to decide, then dropped: not stored, not logged, not in the report.

Per-site instances are config-only directories in the site repos whose
manifest runs this file with ``AGENT_ID`` and ``CATALOG_DISCOVERY_CONFIG`` set.
See AGENT.md.

CLI
---
    python3 agent.py --config site.yaml --validate     # config only; no DB, no API
    python3 agent.py --config site.yaml --dry-run      # REAL searchItems calls (budget-charged),
                                                       # no DB writes, no state or run record
    python3 agent.py --config site.yaml --seeds-only   # a recorded run of the seed import only
    python3 agent.py --config site.yaml                # a full recorded run
Dry-run extras: --max-calls N (can only lower the cap), --no-seeds,
--show-titles (prints titles to the terminal only; never stored).
"""
from __future__ import annotations

import argparse
import copy
from collections import Counter
import datetime as dt
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Callable, Optional

_HERE = Path(__file__).resolve().parent
_FW = Path(os.environ.get("REUSABLE_AGENTS_REPO", str(_HERE.parents[1])))
if str(_FW) not in sys.path:
    sys.path.insert(0, str(_FW))
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from framework.core import amazon_creators  # noqa: E402
from framework.core.agent_base import AgentBase, RunResult  # noqa: E402
from framework.core.guardrails import declare  # noqa: E402

from catalog_discovery import catalog as catalog_mod  # noqa: E402
from catalog_discovery.catalog import Catalog, resolve_dsn  # noqa: E402
from catalog_discovery.config import (CONFIG_ENV, ConfigError,  # noqa: E402
                                      DiscoveryConfig, SeedQuerySource, load_config)
from catalog_discovery.engine import (STOP_UNAVAILABLE, BudgetDecision,  # noqa: E402
                                      DiscoveryEngine, SearchReport, SeedReport,
                                      budget_allowance, golden_metrics,
                                      update_golden_window)
from catalog_discovery.seeds import SeedSourceError  # noqa: E402

REPORT_NAME = "discovery-report.json"
MODES = ("full", "seeds-only", "no-seeds")


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class AmazonCatalogDiscoveryAgent(AgentBase):
    agent_id = "amazon-catalog-discovery-agent"
    name = "Amazon Catalog Discovery"
    description = ("Grows a site's catalog from Amazon Creators searchItems over config-driven "
                   "lanes, persisting only the ASIN and our lane label.")
    category = "seo"
    capabilities = [
        declare("search_amazon", "Creators searchItems within the site's consumer budget share",
                risk_level="low", affects=["amazon-creators-quota"]),
        declare("insert_catalog_asins",
                "INSERT new ASINs with our lane label into the site's catalog (no Amazon content)",
                risk_level="medium", affects=["production-db"]),
        declare("read_seed_sources", "Read seed ASIN files and run read-only seed SELECTs",
                risk_level="low"),
    ]

    #: Set by the CLI before run_once()/execute().
    mode: str = "full"
    dry_run: bool = False
    max_calls_override: Optional[int] = None
    config_path: Optional[str] = None
    title_sink: Optional[Callable[..., None]] = None

    # -- hooks (tests replace these) ------------------------------------------------

    def _connect(self, dsn: str, **kw: Any) -> Any:
        return catalog_mod.connect(dsn, **kw)

    def _make_client(self, cfg: DiscoveryConfig) -> Any:
        tag = cfg.creators.partner_tag or os.environ.get(cfg.creators.partner_tag_env, "")
        return amazon_creators.client_from_env(consumer=cfg.consumer, storage=self.storage,
                                               partner_tag=tag)

    def _now(self) -> dt.datetime:
        return _utcnow()

    # -- lifecycle ----------------------------------------------------------------------

    def signals(self) -> dict | None:
        # Opted out on purpose: the rotation advances every run, so a tick
        # with unchanged inputs still has new slots to search. A run with
        # nothing to do (disabled, cooldown, no allowance) returns in
        # milliseconds without an API call.
        return None

    def run(self) -> RunResult:
        result, report = self.execute()
        if not self.dry_run and report is not None:
            try:
                self.storage.write_json(
                    f"agents/{self.agent_id}/runs/{self.run_ts}/{REPORT_NAME}", report)
            except Exception as exc:  # noqa: BLE001 -- the report is a convenience
                self._note("warning", f"run report not written: {type(exc).__name__}")
        return result

    # -- the work ---------------------------------------------------------------------------

    def _note(self, category: str, message: str, **kw: Any) -> None:
        if self.dry_run:
            print(f"[{category}] {message}")
        else:
            self.decide(category, message, **kw)

    def _progress(self, message: str, progress: float) -> None:
        if self.dry_run:
            print(f"[status] {message}")
        else:
            self.status(message, progress=progress)

    def execute(self) -> tuple[RunResult, Optional[dict[str, Any]]]:
        """One run. Returns the RunResult and the run-report document."""
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        prior = copy.deepcopy(self.state) if isinstance(self.state, dict) else {}
        for key in ("rotation", "seed_sources", "golden"):
            if key in prior and not isinstance(prior[key], dict):
                prior.pop(key)  # malformed state piece: start it afresh, keep the rest
        self._progress("loading config", 0.02)
        try:
            cfg = load_config(self.config_path)
        except ConfigError as exc:
            first = exc.problems[0]
            more = f" (+{len(exc.problems) - 1} more)" if len(exc.problems) > 1 else ""
            self._note("warning", f"config rejected: {first}{more}")
            return RunResult(status="failure", summary=f"Config invalid: {first}{more}",
                             error_text=str(exc)[:4000], next_state=prior), None
        if not cfg.enabled:
            return RunResult(status="success",
                             summary="Discovery is disabled in its config; nothing to do.",
                             metrics={"discovery_enabled": 0}, next_state=prior), None

        state = copy.deepcopy(prior)
        now = self._now()
        engine = DiscoveryEngine(cfg, now=now, log=print, title_sink=self.title_sink)
        rotation = engine.rotation(state.get("rotation"))
        describe = cfg.describe()
        self._note("plan", (f"site={cfg.site or '-'} consumer={cfg.consumer} mode={self.mode}"
                            f"{' DRY-RUN' if self.dry_run else ''}: {len(cfg.active_lanes)} of "
                            f"{len(cfg.lanes)} lanes active, {describe['queries']} queries, "
                            f"cycle {rotation.cycle}"),
                   evidence={"config": cfg.fingerprint,
                             "lanes": [lane["id"] for lane in describe["lanes"] if lane["active"]]})

        dsn = resolve_dsn(dsn_env=cfg.catalog.dsn_env, site=cfg.site, storage=self.storage)
        if not dsn.ok:
            name = dsn.env_name or "(none: set catalog.dsn_env or site)"
            return RunResult(status="failure", summary=f"Catalog DSN not set: {name}",
                             error_text=f"catalog DSN env var {name} is empty ({dsn.source})",
                             next_state=prior), None
        try:
            conn = self._connect(dsn.value, application_name=self.agent_id,
                                 statement_timeout_ms=cfg.catalog.statement_timeout_ms,
                                 connect_timeout_s=cfg.catalog.connect_timeout_s)
        except Exception as exc:  # noqa: BLE001 -- message never carries the DSN value
            msg = f"{type(exc).__name__}: {str(exc).splitlines()[0][:200] if str(exc) else ''}"
            return RunResult(status="failure", summary=f"Catalog DB unreachable via ${dsn.env_name}",
                             error_text=msg, next_state=prior), None
        catalog = Catalog(conn, cfg.catalog, run_ts=self.run_ts, dry_run=self.dry_run)

        seeds: Optional[SeedReport] = None
        search: Optional[SearchReport] = None
        budget: Optional[BudgetDecision] = None
        failure: list[str] = []
        error_text = ""
        try:
            if self.mode != "no-seeds" and (cfg.seed_files or cfg.seed_queries):
                self._progress("importing seed ASINs", 0.1)
                seeds = engine.import_seeds(catalog, state=state,
                                            connect_seed=self._seed_connector(cfg))
            if self.mode != "seeds-only":
                client = self._make_client(cfg)
                if client is None:
                    failure.append("Amazon Creators API not configured (AMAZON_CREATORS_*)")
                else:
                    budget = budget_allowance(cfg, client, storage=self.storage,
                                              max_calls=self.max_calls_override)
                    self._note("observation",
                               f"call allowance {budget.allowed_calls} (cap {budget.max_calls_per_run}, "
                               f"consumer left {budget.own_remaining}, reserved for "
                               f"{', '.join(budget.protected) or 'nobody'}: {budget.protected_reserve})",
                               evidence=budget.to_dict())
                    self._progress(f"searching (allowance {budget.allowed_calls} calls)", 0.3)
                    search = engine.run_search(client, catalog, rotation=rotation,
                                               allowed_calls=budget.allowed_calls)
                    if search.stopped_reason == STOP_UNAVAILABLE:
                        failure.append(f"Creators unavailable: {search.creators_error}")
        except Exception as exc:  # noqa: BLE001 -- keep the progress made so far
            failure.append(f"{type(exc).__name__}: {str(exc)[:300]}")
            error_text = traceback.format_exc()[-4000:]
            try:
                catalog.rollback()
            except Exception:  # noqa: BLE001
                pass
        finally:
            catalog.close()

        state["rotation"] = rotation.to_state()
        state["config_fingerprint"] = cfg.fingerprint
        if search is not None and search.golden:
            state["golden"] = update_golden_window(state.get("golden"), search.golden,
                                                   run_ts=self.run_ts, size=cfg.qa.window)
        golden = golden_metrics(state.get("golden") or {}, min_observations=cfg.qa.min_observations)
        metrics = self._metrics(cfg, seeds, search, budget, rotation, golden)
        state["last_run"] = {"run_ts": self.run_ts,
                             "new_in_lane_asins": metrics.get("new_in_lane_asins", 0),
                             "seed_asins_imported": metrics.get("seed_asins_imported", 0),
                             "api_calls": metrics.get("api_calls", 0),
                             "stopped_reason": search.stopped_reason if search else ""}
        self._log_outcome(cfg, seeds, search, rotation)
        summary = self._summary(cfg, seeds, search, budget, rotation, failure)
        report = {
            "schema_version": "1", "agent_id": self.agent_id, "run_ts": self.run_ts,
            "dry_run": self.dry_run, "mode": self.mode, "config": describe,
            "budget": budget.to_dict() if budget else None,
            "seeds": seeds.to_dict() if seeds else None,
            "search": search.to_dict() if search else None,
            "rotation": {"cycle": rotation.cycle, "cycle_progress": rotation.cycle_progress(),
                         "cycle_restarted": bool(rotation.cycles_started_this_run),
                         "cooldown_until": rotation.cooldown_until(),
                         "saturated": rotation.saturated_keys(),
                         "ended_with_errors": rotation.ended_with_errors()},
            "golden": golden, "metrics": metrics, "failures": failure,
        }
        status = "failure" if failure else "success"
        if failure and not error_text:
            error_text = "; ".join(failure)
        return RunResult(status=status, summary=summary, metrics=metrics, next_state=state,
                         error_text=error_text), report

    def _seed_connector(self, cfg: DiscoveryConfig) -> Callable[[SeedQuerySource], Any]:
        def open_seed_db(src: SeedQuerySource) -> Any:
            res = resolve_dsn(dsn_env=src.dsn_env, site=src.site, storage=self.storage)
            if not res.ok:
                raise SeedSourceError(f"seed DSN env {res.env_name or '(unset)'} is empty")
            return self._connect(res.value, application_name=f"{self.agent_id}:seed",
                                 statement_timeout_ms=src.statement_timeout_ms,
                                 connect_timeout_s=cfg.catalog.connect_timeout_s,
                                 readonly=True)
        return open_seed_db

    # -- reporting ---------------------------------------------------------------------------

    @staticmethod
    def _metrics(cfg: DiscoveryConfig, seeds: Optional[SeedReport], search: Optional[SearchReport],
                 budget: Optional[BudgetDecision], rotation: Any,
                 golden: dict[str, float]) -> dict[str, Any]:
        m: dict[str, Any] = {"discovery_enabled": 1}
        if seeds is not None:
            m["seed_asins_imported"] = len(seeds.imported)
            m["seed_asins_skipped"] = sum(seeds.skipped.values())
            m["seed_source_errors"] = len(seeds.errors)
        else:
            m["seed_asins_imported"] = 0
        if search is not None:
            new = len(search.inserted)
            m.update({
                "new_in_lane_asins": new,
                "api_calls": search.api_calls,
                "pages_fetched": search.pages,
                "throttled": search.throttled,
                "request_errors": search.request_errors,
                "candidates": search.candidates,
                "known_seen": search.known_seen,
                "rejected_lane": search.rejected_total,
                "rejected_global": search.rejected.get("global-reject", 0),
                "rejected_off_lane": search.rejected.get("off-lane", 0),
                "rejected_held_back": search.rejected.get("lane-held-back", 0),
                "rejected_pricing": sum(search.pricing_rejected.values()),
                "cross_lane": search.cross_lane,
                "node_confirmed": search.node_confirmed,
                "deferred_max_new": search.deferred_max_new,
                "insert_errors": search.insert_errors,
                "transient_errors": search.transient_errors,
                "short_pages_continued": search.short_pages_continued,
                "queries_ended": search.queries_ended,
                "partition_price_checked": search.partition_checked,
                "partition_price_misses": search.partition_misses,
                "known_now_rejected": search.known_rejected_total,
            })
            if search.api_calls:
                m["calls_per_new_asin"] = round(search.api_calls / max(1, new), 3)
        if budget is not None:
            m["budget_allowed_calls"] = budget.allowed_calls
            m["budget_overrun_calls"] = max(0, (search.api_calls if search else 0) - budget.allowed_calls)
        m["cycle"] = rotation.cycle
        m["cycle_progress"] = rotation.cycle_progress()
        m["saturated_queries"] = len(rotation.saturated_keys())
        m["query_errors"] = len(rotation.ended_with_errors())
        m["low_yield_queries"] = len(rotation.ended_low_yield())
        m.update(golden)
        return m

    def _log_outcome(self, cfg: DiscoveryConfig, seeds: Optional[SeedReport],
                     search: Optional[SearchReport], rotation: Any) -> None:
        if seeds is not None:
            for name, err in seeds.errors.items():
                self._note("warning", f"seed source {name} skipped: {err}")
            if seeds.imported:
                self._note("result", f"imported {len(seeds.imported)} seed ASIN(s): "
                                     f"{dict(Counter(r.lane for r in seeds.imported).most_common())}")
            for name, count in seeds.untrusted.items():
                self._note("warning",
                           f"seed source {name}: {count} labelled record(s) without a title were "
                           f"not imported: list the lanes you trust under `lanes` (or set "
                           f"`trust_labels: true`), or give the source a title to screen with")
            for name in seeds.continued:
                self._note("observation",
                           f"seed source {name} returned a full page of max_rows rows; the next "
                           f"run continues after its last ASIN")
            for name in seeds.truncated:
                self._note("observation",
                           f"seed source {name} hit max_seed_asins_per_run; the rest follows "
                           f"next run")
        if search is None:
            return
        if search.known_rejected_total:
            sample = ", ".join(f"{k['asin']} ({k['rule']})" for k in search.known_rejected[:10])
            self._note("warning",
                       f"{search.known_rejected_total} catalog ASIN(s) now hit a global reject "
                       f"pattern by their current title (remove or relabel them): {sample}")
        if search.inserted:
            self._note("result", f"inserted {len(search.inserted)} new in-lane ASIN(s): "
                                 f"{search.lane_counts()}")
        saturated = rotation.saturated_keys()
        if saturated:
            self._note("observation",
                       f"{len(saturated)} quer(ies) report more results than 10 pages reach; "
                       f"split them with price_partitions, brands or browse nodes: "
                       f"{', '.join(saturated[:10])}")
        errored = rotation.ended_with_errors()
        if errored:
            self._note("warning", f"{len(errored)} quer(ies) ended on a request error this cycle: "
                                  f"{', '.join(errored[:10])}")
        if search.transient_errors:
            self._note("observation",
                       f"{search.transient_errors} server error(s) from Creators; those slots are "
                       f"retried by the next run (stopped: {search.stopped_reason or '-'})")
        if search.partition_unit_suspect:
            self._note("warning",
                       f"{search.partition_misses} of {search.partition_checked} listings fell outside "
                       f"their price band: check creators.price_filter_unit "
                       f"(now {cfg.creators.price_filter_unit!r})")
        if search.insert_errors:
            self._note("warning", f"{search.insert_errors} insert(s) failed: "
                                  f"{'; '.join(search.insert_error_samples[:3])}")

    @staticmethod
    def _summary(cfg: DiscoveryConfig, seeds: Optional[SeedReport], search: Optional[SearchReport],
                 budget: Optional[BudgetDecision], rotation: Any, failure: list[str]) -> str:
        parts: list[str] = []
        if failure:
            parts.append("FAILED: " + "; ".join(failure)[:300] + ".")
        if search is not None:
            new = len(search.inserted)
            lanes = ", ".join(f"{k} {v}" for k, v in search.lane_counts().items())
            reject_bits = ", ".join(f"{k} {v}" for k, v in search.rejected.most_common(4))
            head = (f"{new} new in-lane ASIN(s)" + (f" ({lanes})" if lanes else "")
                    if new else "no new in-lane ASINs")
            parts.append(
                f"Searched {search.pages} page(s): {head}; "
                f"{search.rejected_total} rejected by lane rules"
                + (f" ({reject_bits})" if reject_bits else "")
                + f", {sum(search.pricing_rejected.values())} by the pricing gate, "
                f"{search.known_seen} already carried. "
                f"{search.api_calls}/{budget.allowed_calls if budget else 0} Creators calls; "
                f"cycle {rotation.cycle} at {round(rotation.cycle_progress() * 100)}%"
                + (f" (stopped: {search.stopped_reason}"
                   + (f" until {rotation.cooldown_until()}"
                      if search.stopped_reason == "cooldown" and rotation.cooldown_until() else "")
                   + ")" if search.stopped_reason else "") + ".")
            if search.deferred_max_new:
                parts.append(f"{search.deferred_max_new} in-lane listing(s) over the per-run cap "
                             f"wait for the next run.")
        if seeds is not None:
            bits = f"Seeds: {len(seeds.imported)} imported"
            if seeds.unchanged:
                bits += f", {len(seeds.unchanged)} source(s) unchanged"
            if seeds.errors:
                bits += f", {len(seeds.errors)} source error(s)"
            parts.append(bits + ".")
        return " ".join(parts) or "Nothing to do."


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_titles(asin: str, title: Optional[str], decision: Any, key: str) -> None:
    label = decision.lane or f"-{decision.reason}"
    print(f"  {asin} {label:<22} {key[:40]:<40} {(title or '')[:90]}")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", help=f"config file (default: ${CONFIG_ENV})")
    ap.add_argument("--validate", action="store_true", help="validate the config and exit")
    ap.add_argument("--dry-run", action="store_true",
                    help="real searchItems calls, no DB writes, no state or run record")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--seeds-only", action="store_true", help="only the seed import")
    mode.add_argument("--no-seeds", action="store_true", help="skip the seed import")
    ap.add_argument("--max-calls", type=int, help="lower the run's call cap")
    ap.add_argument("--show-titles", action="store_true",
                    help="dry run only: print listing titles to this terminal (never stored)")
    args = ap.parse_args(argv)

    if args.validate:
        try:
            cfg = load_config(args.config)
        except ConfigError as exc:
            print(json.dumps({"ok": False, "problems": exc.problems}, indent=2))
            return 2
        print(json.dumps({"ok": True, **cfg.describe()}, indent=2, default=str))
        return 0
    if args.show_titles and not args.dry_run:
        ap.error("--show-titles is only allowed with --dry-run")

    agent = AmazonCatalogDiscoveryAgent()
    agent.config_path = args.config
    agent.mode = "seeds-only" if args.seeds_only else ("no-seeds" if args.no_seeds else "full")
    agent.max_calls_override = args.max_calls
    if args.dry_run:
        # Outside run_once: no run record, no state write, no DB writes. The
        # rotation starts from the stored state so the preview is realistic.
        agent.dry_run = True
        latest = agent.storage.read_json(f"agents/{agent.agent_id}/state/latest.json") or {}
        agent.state = latest.get("state") or {}
        if args.show_titles:
            agent.title_sink = _print_titles
        result, report = agent.execute()
        print(json.dumps({"status": result.status, "summary": result.summary,
                          "report": report}, indent=2, default=str))
        return 0 if result.status == "success" else 1
    result = agent.run_once()
    return 0 if result.status == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
