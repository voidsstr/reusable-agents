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
    client = AmazonCreatorsClient(CreatorsConfig.from_env())
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
        pages = shelf.crawl(origin, max_depth=depth,
                            per_depth=tuple(cfg.get("per_depth",
                                                    [None, 400, 500, 500])),
                            seeds=cfg.get("seeds") or ())
        asins = {a for m in pages.values() for a in m["asins"]}
        self.decide("observation",
                    "crawled %d pages to depth %d; %d pages carried products; "
                    "%d distinct ASINs in HTML"
                    % (len(pages), depth,
                       sum(1 for m in pages.values() if m["asins"]), len(asins)))
        api_slugs: dict[str, dict] = {}
        eps = cfg.get("api_endpoints") or []
        if eps:
            api_slugs = shelf.crawl_api_surface(eps)
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

    def run(self) -> RunResult:
        cfg = self._cfg()
        self.status("crawling the shelf", progress=0.1, current_action="crawl")
        asins = self._discover(cfg)
        slugs = sorted(self._api_slugs.keys())
        if not asins and not slugs:
            return RunResult(status="failure",
                             summary="shelf discovery found no products — "
                                     "crawl or api_endpoints config is wrong")

        self.status("loading shelf rows from db", progress=0.3,
                    current_action="db")
        rows = self._shelf_rows(cfg, asins, slugs)
        by_asin = {r["asin"]: r for r in rows if r.get("asin")}
        targets = sorted(by_asin)
        cap = int(cfg.get("max_verify_per_run", 400))
        if len(targets) > cap:
            # Oldest-priced first so successive runs sweep the whole shelf.
            targets.sort(key=lambda a: str(by_asin[a].get("price_updated_at") or ""))
            self.decide("observation",
                        "shelf has %d verifiable products; capping this run at "
                        "%d (stalest first)" % (len(targets), cap))
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

        recs = self._to_recs(findings, aerrs, cfg)
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
        self._save_artifact("shelf-findings.json",
                            {"crawled_pages": len(self._pages),
                             "surfaced_asins": len(asins),
                             "api_products": len(self._api_slugs),
                             "verified": len(truth), "amazon_errors": aerrs,
                             "findings": findings})
        return RunResult(
            status="success",
            summary="shelf: %d pages, %d products verified, %d disagree with "
                    "Amazon (%d recs)" % (len(self._pages), len(truth),
                                          len(findings), len(recs)),
            metrics={"pages_crawled": len(self._pages),
                     "products_verified": len(truth),
                     "products_with_issues": len(findings),
                     "amazon_unreachable": len(aerrs)})

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
