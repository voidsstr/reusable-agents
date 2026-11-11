#!/usr/bin/env python3
"""Shelf Audit Agent — verify what the SITE SHOWS against what AMAZON SAYS.

Every other catalog agent picks its targets from the database. This one picks
them from the *rendered site*: it crawls the homepage and 3 levels down (plus
any JSON endpoints that back client-rendered shelves), collects the products a
visitor can actually reach, and checks each against the Amazon Product API.

Why that distinction matters (measured 2026-08-26):

  * specpicks' price refresher approximates "user-visible" as
    `is_featured OR asin = ANY(article.related_product_asins)`. Of 974 ASINs
    the crawler actually found on the site, 464 matched NEITHER — they sit in
    the refresher's tier-3 tail, and 408 of those had prices over a week old
    or never set at all.
  * aisleprompt has no Amazon price-verification agent whatsoever; 533 of
    1367 surfaced products had drifted prices, worst case $48.02 shown for a
    product Amazon sells at $14.88.
  * The stale price is also what goes into the product page's JSON-LD offer,
    so Google indexes it.

Checks per surfaced product: price drift, image is genuinely the same
picture, availability, and required fields. Findings become standard
recommendations.json entries for the implementer pipeline.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_FW = Path(os.environ.get("REUSABLE_AGENTS_REPO",
                          "/home/voidsstr/development/reusable-agents"))
if str(_FW) not in sys.path:
    sys.path.insert(0, str(_FW))
sys.path.insert(0, str(Path(__file__).parent))

from framework.core.agent_base import AgentBase, RunResult  # noqa: E402
from framework.core.amazon_creators import (  # noqa: E402
    AmazonCreatorsClient, CreatorsConfig, parse_item, CreatorsThrottled)
import shelf  # noqa: E402
import answer_picks  # noqa: E402

SEV = {"price_drift": "high", "image_mismatch": "high",
       "out_of_stock_on_amazon": "medium", "site_no_price": "medium",
       "image_missing": "medium", "amazon_no_price": "low",
       "missing_title": "high", "missing_brand": "low"}


def _amazon_lookup(asins: list[str], batch: int = 10, log=print) -> tuple[dict, dict]:
    """Resolve ASINs through the Amazon Creators API.

    NB: get_items returns (items, errors) — BOTH halves matter. An ASIN in
    `errors` is inaccessible (delisted / regional / no offer) and should be
    surfaced for deactivation, not silently dropped.
    """
    import time
    client = AmazonCreatorsClient(CreatorsConfig.from_env(), consumer="shelf-audit")
    items: dict[str, dict] = {}
    errors: dict[str, str] = {}
    for i in range(0, len(asins), batch):
        chunk = asins[i:i + batch]
        for attempt in range(4):
            try:
                got, aerrs = client.get_items(chunk)
                for a, raw in (got or {}).items():
                    try:
                        items[a] = parse_item(raw)
                    except Exception as e:
                        errors[a] = "parse:%s" % e
                for a, msg in (aerrs or {}).items():
                    errors.setdefault(a, "amazon:%s" % str(msg)[:60])
                for a in chunk:
                    if a not in (got or {}) and a not in (aerrs or {}):
                        errors.setdefault(a, "not_returned")
                break
            except CreatorsThrottled:
                time.sleep(2 ** attempt * 2)
            except Exception as e:
                if attempt == 3:
                    for a in chunk:
                        errors.setdefault(a, "api:%s" % str(e)[:70])
                else:
                    time.sleep(2 ** attempt)
        time.sleep(float(os.environ.get("SHELF_AUDIT_SLEEP", "1.0")))
    return items, errors


class ShelfAuditAgent(AgentBase):
    agent_id = os.environ.get("AGENT_ID", "shelf-audit-agent")

    def _cfg(self) -> dict:
        p = os.environ.get("SHELF_AUDIT_CONFIG")
        if not p:
            raise SystemExit("SHELF_AUDIT_CONFIG must point at a site yaml")
        import yaml
        return yaml.safe_load(Path(p).read_text())

    # ---------- shelf discovery ----------
    def _discover(self, cfg) -> list[str]:
        """Return the ASINs a visitor can actually reach."""
        origin = cfg["origin"]
        depth = int(cfg.get("max_depth", 3))
        # crawl_skip_path_regex: never enqueue the site's click-out route
        # (it logs a click and 302s to a tagged Amazon URL); probe_headers:
        # a header the site can use to skip click logging for our requests.
        pages = shelf.crawl(origin, max_depth=depth,
                            per_depth=tuple(cfg.get("per_depth",
                                                    [None, 400, 500, 500])),
                            seeds=cfg.get("seeds") or (),
                            skip_path_regex=cfg.get("crawl_skip_path_regex") or None,
                            headers=cfg.get("probe_headers") or None)
        asins = {a for m in pages.values() for a in m["asins"]}
        self.decide("observation",
                    "crawled %d pages to depth %d; %d pages carried products; "
                    "%d distinct ASINs in HTML"
                    % (len(pages), depth,
                       sum(1 for m in pages.values() if m["asins"]), len(asins)))
        api_slugs: dict[str, dict] = {}
        eps = cfg.get("api_endpoints") or []
        if eps:
            api_slugs = shelf.crawl_api_surface(eps, headers=cfg.get("probe_headers") or None)
            self.decide("observation",
                        "collected %d products from %d API shelf endpoints"
                        % (len(api_slugs), len(eps)))
        self._api_slugs = api_slugs
        self._pages = pages
        return sorted(asins)

    # ---------- db side ----------
    def _shelf_rows(self, cfg, asins: list[str], slugs: list[str]) -> list[dict]:
        import psycopg2
        dsn = os.environ[cfg["dsn_env"]]
        cn = psycopg2.connect(dsn)
        cur = cn.cursor()
        q = cfg["shelf_query"]
        # Sites differ in how a shelf row is addressed: specpicks matches on
        # ASIN alone, aisleprompt needs ASIN *or* slug (its shelf comes from
        # the API surface). Bind by however many placeholders the query has.
        nparams = q.count("%s")
        params = (asins, slugs)[:nparams] if nparams <= 2 else (asins,)
        cur.execute(q, params)
        cols = [c[0] for c in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        cn.close()
        return rows

    def signals(self) -> dict | None:
        return {"ready": True}

    # ---------- answer-pick click-outs (opt-in: `answer_pick_check`) ----------
    def _answer_pick_check(self, cfg) -> tuple[list[dict], dict]:
        """Walk the top-click pages' answer picks; see answer_picks.py."""
        apc = cfg.get("answer_pick_check") or {}
        if not apc.get("enabled"):
            return [], {}
        from datetime import datetime, timezone
        import urllib.request
        conn = None
        try:
            import psycopg2
            conn = psycopg2.connect(os.environ[cfg["dsn_env"]], connect_timeout=15)
            with conn.cursor() as cur:
                cur.execute("SET statement_timeout = 60000")
        except Exception as e:
            conn = None
            self.decide("observation", "answer-pick DB connect failed: %s"
                        % str(e)[:120])
        # Click pages, then AI-landed pages, then extra_pages (answer_picks.audit_pages).
        pages = answer_picks.audit_pages(
            conn, apc, warn=lambda m: self.decide("observation", m))

        hdrs = answer_picks.probe_headers(cfg)

        def fetch_json(url):
            try:
                with urllib.request.urlopen(urllib.request.Request(
                        url, headers=hdrs), timeout=25) as r:
                    return json.loads(r.read().decode("utf-8", "replace"))
            except Exception:
                return {}

        lookup = None
        if conn is not None and apc.get("replacement_query"):
            def lookup(slug):
                with conn.cursor() as cur:
                    cur.execute(apc["replacement_query"], {"slug": slug})
                    cols = [c[0] for c in cur.description]
                    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
                conn.rollback()
                own = next((r for r in rows if r.get("role") == "self"), None)
                return own, [r for r in rows if r.get("role") != "self"]

        swap_lookup = None
        swap_q = (apc.get("swap") or {}).get("query")
        if conn is not None and (apc.get("swap") or {}).get("enabled") and swap_q:
            def swap_lookup(slug, page):
                with conn.cursor() as cur:
                    cur.execute(swap_q, {"slug": slug, "page": page})
                    cols = [c[0] for c in cur.description]
                    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
                conn.rollback()
                return rows

        # A held page (framework page_hold) is still checked and reported, but
        # its fixes go to the person-review rec, never the dispatched one.
        from framework.core import page_hold
        held = page_hold.held_paths(page_hold.holds_from_site_config(cfg))

        state_key = f"agents/{self.agent_id}/answer-pick-strikes.json"
        try:
            state = self.storage.read_json(state_key) or {}
        except Exception:
            state = {}
        now = datetime.now(timezone.utc)
        try:
            res = answer_picks.run_check(
                apc, origin=cfg["origin"], pages=pages, state=state,
                now=now, fetch_json=fetch_json,
                probe_fn=lambda u: answer_picks.probe(u, headers=hdrs),
                lookup=lookup, same_picture=shelf.same_picture,
                swap_lookup=swap_lookup)
        finally:
            if conn is not None:
                conn.close()
        # Person-review picks that sat past escalate_after_hours/runs go to
        # KTLO once (marked in the strike state before it is saved).
        esc = answer_picks.escalations(res, apc, now, held_pages=held)
        self.storage.write_json(state_key, res["state"])
        if esc:
            self.decide("escalation",
                        "%d dead answer pick(s) still wait on a person past %sh: %s" % (
                            len(esc), apc.get("escalate_after_hours", 24),
                            "; ".join("%s on %s (%sh)" % (e["href"], ", ".join(e["pages"] or []),
                                                         e["age_h"]) for e in esc)[:900]))
            self._save_artifact("answer-pick-escalations.json",
                                {"run_ts": self.run_ts, "escalations": esc})
            try:
                self.storage.write_json(
                    f"agents/{self.agent_id}/escalations/answer-picks.json",
                    {"run_ts": self.run_ts, "escalations": esc})
            except Exception as e:
                self.decide("observation", "escalation write failed: %s" % str(e)[:120])
        self.decide("observation",
                    "answer picks: %d checked on %d pages; %d broken now, %d "
                    "confirmed (second strike, or first strike in a fast window)" % (
                        res["picks_checked"], len(pages), len(res["broken_now"]),
                        len(res["confirmed"])))
        summary = {k: res[k] for k in ("pages", "picks_checked", "broken_now",
                                       "confirmed", "pages_without_picks")}
        checked = res["picks_checked"]
        summary["slot_health_pct"] = round(
            100.0 * (checked - len(res["broken_now"])) / checked, 1) if checked else None
        summary["review_max_age_h"] = answer_picks.review_max_age_h(res, now, held_pages=held)
        summary["swaps"] = sum(1 for r in res["confirmed"] if r.get("action") == "swap")
        summary["escalated"] = len(esc)
        self._save_artifact("answer-picks.json", summary)
        return answer_picks.to_recs(res, self.run_ts, held_pages=held,
                                    dispatch_kind=apc.get("rec_dispatch_kind") or ""), summary

    def run(self) -> RunResult:
        cfg = self._cfg()
        # Runs first and on its own: it reads the top pages, not the crawl, so
        # a failed shelf discovery must not skip it.
        pick_recs, pick_summary = [], {}
        try:
            pick_recs, pick_summary = self._answer_pick_check(cfg)
        except Exception as e:
            self.decide("observation", "answer-pick check failed: %s" % str(e)[:160])
        self.status("crawling the shelf", progress=0.1, current_action="crawl")
        asins = self._discover(cfg)
        slugs = sorted(self._api_slugs.keys())
        if not asins and not slugs:
            return RunResult(status="failure",
                             summary="shelf discovery found no products — "
                                     "crawl or api_endpoints config is wrong")

        self.status("loading shelf rows from db", progress=0.3,
                    current_action="db")
        # verify_priority ASINs are looked up even when the crawl did not
        # surface them (a clicked product off the category grids).
        priority = self._verify_priority(cfg)
        rows = self._shelf_rows(cfg, sorted(set(asins) | priority), slugs)
        by_asin = {r["asin"]: r for r in rows if r.get("asin")}
        targets = sorted(by_asin)
        cap = int(cfg.get("max_verify_per_run", 400))
        if len(targets) > cap or priority:
            # Priority ASINs (verify_priority: clicked products, money-guide
            # picks) first, then oldest-priced first so successive runs sweep
            # the whole shelf.
            targets.sort(key=lambda a: (a not in priority,
                                        str(by_asin[a].get("price_updated_at") or "")))
            if len(targets) > cap:
                self.decide("observation",
                            "shelf has %d verifiable products; capping this run at "
                            "%d (%d priority ASINs first, then stalest)"
                            % (len(targets), cap, len(priority & set(targets))))
            targets = targets[:cap]

        self.status("verifying against the amazon api", progress=0.45,
                    current_action="amazon")
        truth, aerrs = _amazon_lookup(targets)

        self.status("comparing site vs amazon", progress=0.75,
                    current_action="compare")
        check_images = str(cfg.get("check_images", True)).lower() != "false"
        findings = []
        for a in targets:
            az = truth.get(a)
            if not az:
                continue
            issues = shelf.compare_row(by_asin[a], az, check_images=check_images)
            if issues:
                findings.append({"asin": a, "slug": by_asin[a].get("slug"),
                                 "title": (by_asin[a].get("title") or "")[:90],
                                 "issues": issues})

        # Price drift / out-of-stock on a priority ASIN is a re-price job, not
        # a code rec: publish those ASINs for the price refresher's
        # storage_list tier (verify_priority.publish_key) and keep them out of
        # the catalog recs.
        repriced = self._publish_priority_findings(cfg, priority, findings)
        rec_findings = findings
        if repriced:
            rec_findings = [
                f if f["asin"] not in repriced else {
                    **f, "issues": [i for i in f["issues"]
                                    if not i.startswith(("price_drift", "out_of_stock"))]}
                for f in findings]
            rec_findings = [f for f in rec_findings if f["issues"]]
        recs = pick_recs + self._to_recs(rec_findings, aerrs, cfg)
        # NO producer-history dedup here, deliberately. That filter exists so a
        # PROPOSAL agent does not pitch the same article twice; this is a
        # MONITOR, and its rec titles ("N shelf products: image mismatch") have
        # a constant signature run to run. Applying it silently suppressed 5 of
        # 6 buckets on the second run — 257 real findings collapsed to 1 rec —
        # which would have made the agent look clean while the shelf rotted.
        doc = {"schema_version": "1", "site": cfg.get("site_id", ""),
               "agent": self.agent_id, "agent_id": self.agent_id,
               "run_ts": self.run_ts,
               "summary": "%d of %d shelf products disagree with Amazon"
                          % (len(findings), len(targets)),
               "recommendations": recs}
        self._save_artifact("recommendations.json", doc)
        dispatched, dnote = self._maybe_dispatch(
            [r for r in recs if r not in pick_recs], cfg)
        pnote = self._maybe_dispatch_picks(pick_recs, cfg)
        self._save_artifact("shelf-findings.json",
                            {"crawled_pages": len(self._pages),
                             "surfaced_asins": len(asins),
                             "api_products": len(self._api_slugs),
                             "verified": len(truth), "amazon_errors": aerrs,
                             "findings": findings})
        return RunResult(
            status="success",
            summary="shelf: %d pages, %d products verified, %d disagree with "
                    "Amazon (%d recs; %s)%s" % (
                        len(self._pages), len(truth), len(findings), len(recs), dnote,
                        ("; answer picks: %d checked, %d confirmed dead (%s)" % (
                            pick_summary.get("picks_checked", 0),
                            len(pick_summary.get("confirmed") or []), pnote))
                        if pick_summary else ""),
            metrics={"pages_crawled": len(self._pages),
                     "products_verified": len(truth),
                     "products_with_issues": len(findings),
                     "amazon_unreachable": len(aerrs),
                     "answer_picks_checked": pick_summary.get("picks_checked", 0),
                     "answer_picks_broken_now": len(pick_summary.get("broken_now") or {}),
                     "answer_picks_confirmed_dead": len(pick_summary.get("confirmed") or []),
                     "answer_picks_swapped": pick_summary.get("swaps", 0),
                     "answer_picks_escalated": pick_summary.get("escalated", 0),
                     "answer_picks_review_max_age_h": pick_summary.get("review_max_age_h", 0.0),
                     "money_guide_slot_health_pct": pick_summary.get("slot_health_pct")
                     if pick_summary.get("slot_health_pct") is not None else 0.0,
                     "priority_asins_verified": len(priority & set(targets)),
                     "priority_findings_to_reprice": len(repriced)})

    def _verify_priority(self, cfg) -> set[str]:
        """ASINs to verify before the stalest-first sweep (opt-in
        `verify_priority.enabled`), from either or both of:
          demand: a framework price_refresh_priority site config (groups of
                  kind human_clicks / sql / ...); every member ASIN counts, so
                  "clicked by a verified human in 30 days" means what the
                  goal tracker means by it.
          query:  a site SELECT whose first column is an ASIN.
        Any failure is logged and contributes nothing."""
        vp = cfg.get("verify_priority") or {}
        if not vp.get("enabled") or not (vp.get("query") or vp.get("demand")):
            return set()
        out: set[str] = set()
        try:
            import psycopg2
            cn = psycopg2.connect(os.environ[cfg["dsn_env"]], connect_timeout=15)
        except Exception as e:
            self.decide("observation", "verify_priority DB connect failed: %s" % str(e)[:160])
            return set()
        try:
            if vp.get("demand"):
                try:
                    from framework.core import price_refresh_priority as prp
                    pcfg = prp.resolve_config(agent_id=self.agent_id,
                                              site=cfg.get("site_id", ""),
                                              site_cfg={**vp["demand"], "enabled": True},
                                              storage=self.storage)
                    tiers = prp.collect_tiers(cn, pcfg, storage=self.storage)
                    for name, err in tiers.errors.items():
                        self.decide("observation", "verify_priority group %s empty: %s"
                                    % (name, err[:160]))
                    out |= set(tiers.members)
                except Exception as e:
                    self.decide("observation", "verify_priority demand failed: %s" % str(e)[:160])
                try:
                    cn.rollback()
                except Exception:
                    pass
            if vp.get("query"):
                try:
                    with cn.cursor() as cur:
                        cur.execute("SET statement_timeout = 60000")
                        cur.execute(vp["query"])
                        out |= {str(r[0]) for r in cur.fetchall() if r and r[0]}
                except Exception as e:
                    self.decide("observation", "verify_priority query failed: %s" % str(e)[:160])
        finally:
            cn.close()
        self.decide("observation", "verify_priority: %d ASINs go first" % len(out))
        return out

    def _publish_priority_findings(self, cfg, priority: set, findings: list) -> set[str]:
        """Priority ASINs with price drift or out of stock on Amazon, written
        as {"asins": [...]} to `verify_priority.publish_key` (a
        price_refresh_priority storage_list group). Returns the ASINs only
        when published, so they leave the code recs only when the refresher
        can see them."""
        key = (cfg.get("verify_priority") or {}).get("publish_key")
        if not (priority and key):
            return set()
        hits = sorted({f["asin"] for f in findings if f["asin"] in priority and any(
            i.startswith(("price_drift", "out_of_stock")) for i in f["issues"])})
        try:
            self.storage.write_json(key, {"asins": hits, "run_ts": self.run_ts,
                                          "source": self.agent_id})
        except Exception as e:
            self.decide("observation", "verify_priority publish failed: %s" % str(e)[:120])
            return set()
        return set(hits)

    def _to_recs(self, findings, aerrs, cfg) -> list[dict]:
        recs = []
        buckets: dict[str, list] = {}
        for f in findings:
            for i in f["issues"]:
                buckets.setdefault(i.split(":")[0], []).append(f)
        for kind, items in sorted(buckets.items(),
                                  key=lambda kv: -len(kv[1])):
            ex = "; ".join("%s (%s)" % (x["asin"], x["title"][:40])
                           for x in items[:5])
            recs.append({
                "id": "shelf-%s-%s" % (self.run_ts, kind.replace("_", "-")),
                "title": "%d shelf products: %s" % (len(items), kind.replace("_", " ")),
                "severity": SEV.get(kind, "medium"),
                "priority": "high" if SEV.get(kind) == "high" else "medium",
                "tier": "lever" if SEV.get(kind) == "high" else "smaller",
                "rationale": "Found by crawling the live site %d levels deep and "
                             "checking each surfaced product against the Amazon "
                             "Product API. Examples: %s"
                             % (int(cfg.get("max_depth", 3)), ex),
                "expected_impact": "Correct prices/images on pages users and "
                                   "Google actually see (the stale price also "
                                   "lands in the page's JSON-LD offer).",
                "affected_asins": [x["asin"] for x in items][:200],
            })
        if aerrs:
            recs.append({
                "id": "shelf-%s-unreachable" % self.run_ts,
                "title": "%d shelf products are not retrievable from Amazon" % len(aerrs),
                "severity": "medium", "priority": "medium", "tier": "smaller",
                "rationale": "These ASINs are surfaced on the site but Amazon "
                             "will not return them (delisted / regional / no "
                             "offer). Candidates for deactivation.",
                "expected_impact": "Stops showing products nobody can buy.",
                "affected_asins": sorted(aerrs)[:200],
            })
        return recs


    def _maybe_dispatch(self, recs, cfg):
        """Route findings to the implementer — OFF unless the site opts in.

        The sibling catalog-audit-agent dispatches through the same call, and
        gated_dispatch_now() already honours `auto_implement` as its approval
        gate. A second, agent-local switch exists on top of it because the
        remedy here writes to LIVE CATALOG DATA: the drifts this agent finds
        span -95% to +279%, so a bug in the comparison could rewrite real
        prices at scale. `dispatch_findings` therefore defaults to FALSE and
        the operator turns it on deliberately, per site.
        """
        if not str(cfg.get("dispatch_findings", "false")).lower() == "true":
            return 0, "dispatch off (dispatch_findings=false)"
        tiers = {t.strip() for t in
                 str(cfg.get("dispatch_tiers", "lever")).split(",") if t.strip()}
        cap = int(cfg.get("max_dispatch", 3))
        eligible = [r for r in recs if r.get("tier") in tiers][:cap]
        if not eligible:
            return 0, "no recs in dispatch tiers %s" % sorted(tiers)
        try:
            from framework.core import dispatch as _d
            handle = _d.gated_dispatch_now(
                agent=self, agent_id=self.agent_id, run_dir=str(self._rundir),
                rec_ids=[r["id"] for r in eligible], action="implement",
                site=cfg.get("site_id", ""), subject_tag="shelf-audit")
            if handle is None:
                return 0, "awaiting approval (auto_implement=false)"
            return len(eligible), "dispatched %d of %d eligible (cap %d)" % (
                len(eligible), len([r for r in recs if r.get("tier") in tiers]), cap)
        except Exception as e:
            self.decide("observation", "shelf dispatch failed: %s" % str(e)[:120])
            return 0, "dispatch failed: %s" % str(e)[:60]

    def _maybe_dispatch_picks(self, pick_recs, cfg) -> str:
        """Dead answer picks go to the implementer only with
        `answer_pick_check.dispatch: true`, and only the lever rec (relink to a
        provably-same product, or unlink). The review rec never dispatches.
        Separate from `dispatch_findings`: this remedy edits a link in an
        article, it does not rewrite catalog prices."""
        apc = cfg.get("answer_pick_check") or {}
        if not pick_recs:
            return "nothing to dispatch"
        if not apc.get("dispatch"):
            return "dispatch off"
        eligible = [r for r in pick_recs if r.get("tier") == "lever"]
        if not eligible:
            return "only person-review picks"
        run_dir = self._rundir
        if any(r.get("dispatch_kind") for r in eligible):
            # The implementer derives the batch's dispatch kind from EVERY rec
            # in the run dir's recommendations.json (run.sh / dispatch
            # .dispatch_kind_for), so the shelf's catalog recs would turn an
            # article-refresh batch back into a deploying "seo" one. The pick
            # recs dispatch from their own run dir.
            run_dir = self._rundir.parent / ("%s-answer-picks" % self.run_ts)
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "recommendations.json").write_text(json.dumps({
                "schema_version": "1", "site": cfg.get("site_id", ""),
                "agent": self.agent_id, "agent_id": self.agent_id,
                "run_ts": self.run_ts, "summary": "dead answer picks",
                "recommendations": eligible}, indent=2))
        try:
            from framework.core import dispatch as _d
            handle = _d.gated_dispatch_now(
                agent=self, agent_id=self.agent_id, run_dir=str(run_dir),
                rec_ids=[r["id"] for r in eligible], action="implement",
                site=cfg.get("site_id", ""), subject_tag="shelf-audit")
            return "dispatched" if handle is not None else "awaiting approval"
        except Exception as e:
            self.decide("observation", "answer-pick dispatch failed: %s" % str(e)[:120])
            return "dispatch failed"

    @property
    def _rundir(self) -> Path:
        # AgentBase does not provide run_dir; agents that want local artifacts
        # derive their own (catalog-audit uses its own cfg helper).
        d = Path(os.path.expanduser("~/.reusable-agents")) / self.agent_id / \
            "runs" / self.run_ts
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _save_artifact(self, name: str, content) -> None:
        key = f"agents/{self.agent_id}/runs/{self.run_ts}/{name}"
        disk = self._rundir / name
        if isinstance(content, (dict, list)):
            disk.write_text(json.dumps(content, indent=2))
            self.storage.write_json(key, content)
        else:
            disk.write_text(str(content))
            self.storage.write_text(key, str(content))


if __name__ == "__main__":
    ShelfAuditAgent().run_once()
