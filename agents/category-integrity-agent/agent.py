#!/usr/bin/env python3
"""Keep products out of categories they do not belong in.

Why it exists (2026-08-30): /buying-guide/games-cartridges on specpicks listed a
Crock-Pot slow cooker, flea spray, headphones, batteries, an HP ink cartridge and
a flying ring. Nothing checked. The cause is upstream — 81,345 of 146,073 active
products (55.7%) carry `category_confidence IS NULL`, meaning they were filed by
a path that never ran the scoring categoriser.

WHAT IT DOES ITSELF: de-categorises products that are provably outside the
catalogue's domain AND were never scored. Setting category_id = NULL removes them
from buying guides immediately; the site's own `assign-categories` can place them
properly later. It never moves a product to a guessed category — a wrong home is
worse than no home — and never touches `category_validated = true`, which is
somebody's decision.

WHAT IT HANDS TO THE IMPLEMENTER: a code-level cause. Data repair is
deterministic and safe to automate; changing the categoriser is not, so that
goes out as a recommendation for the implementer, which is the only thing that
edits code and deploys.

DEMAND FIRST (2026-11-11, site config ``demand_scoring``, off by default):
before any of that, the products people actually clicked through to Amazon
(framework human_clicks) that fail the Top-Pick floor (confidence < 0.5) are
run through the site's own categoriser and given a score where its rules
confirm their category. See demand.py for what it will and will not write.
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
import yaml

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
_FW = Path(os.environ.get("REUSABLE_AGENTS_REPO", "/home/voidsstr/development/reusable-agents"))
sys.path.insert(0, str(_FW))

from framework.core.agent_base import AgentBase, RunResult  # noqa: E402
from domain import is_off_domain, load_vocab  # noqa: E402
import demand  # noqa: E402

MAX_FIX = int(os.environ.get("CATEGORY_MAX_FIX_PER_RUN", "500"))
# An unscored share above this is a pipeline failure, not a backlog: something
# is filing products without ever running the categoriser.
UNSCORED_ALERT = float(os.environ.get("CATEGORY_UNSCORED_ALERT", "0.25"))


def _cfg() -> dict:
    p = os.environ.get("CATEGORY_INTEGRITY_CONFIG")
    if not p:
        raise SystemExit("set CATEGORY_INTEGRITY_CONFIG to a site.yaml")
    return yaml.safe_load(Path(p).read_text())


class CategoryIntegrityAgent(AgentBase):
    agent_id = os.environ.get("AGENT_ID", "category-integrity-agent")

    def __init__(self, *a, **kw):
        self.cfg = _cfg()
        self.site = self.cfg.get("site") or "unknown"
        super().__init__(*a, **kw)

    def _conn(self):
        dsn = os.environ.get(self.cfg["db_env"]) or os.environ.get("DATABASE_URL")
        if not dsn:
            raise SystemExit(f"{self.cfg['db_env']} not set")
        return psycopg2.connect(dsn)

    def signals(self):
        return None      # catalogue churns constantly; the check is cheap

    # ── demand-first scoring ────────────────────────────────────────────────
    def _clicked_asins(self, conn, dcfg: dict) -> dict[str, int]:
        """{asin: verified-human clicks} over the window (framework human_clicks)."""
        from framework.core import human_clicks
        c = dcfg["clicks"]
        spec = human_clicks.resolve_spec(c.get("spec") or {},
                                         profile=c.get("profile") or self.agent_id)
        counts = human_clicks.human_counts_by(
            conn, spec, table=c["table"], group_col=c.get("group_col") or "asin",
            window_days=dcfg["window_days"], match=c.get("match") or {})
        conn.rollback()
        return {k: int(n) for k, n in counts.items() if k and n > 0}

    def _demand_scoring(self, conn) -> dict:
        """Score clicked low-confidence products with the site's categoriser
        (demand.py). Never raises: any failure is reported and nothing is
        written, and the de-categorisation pass still runs."""
        dcfg = demand.resolve(self.cfg.get("demand_scoring"))
        out: dict = {"enabled": bool(dcfg["enabled"]), "verdicts": {}, "written": 0,
                     "clicked_asins": 0, "candidates": 0, "error": "", "unresolved": [],
                     "pass_pct": None}
        if not dcfg["enabled"]:
            return out
        bad = demand.problems(dcfg)
        if bad:
            out["error"] = "; ".join(bad)
            return out
        try:
            clicks = self._clicked_asins(conn, dcfg)
            out["clicked_asins"] = len(clicks)
            if not clicks:
                return out
            with conn.cursor() as cur:
                cur.execute("""SELECT p.id, p.asin, p.title, p.brand, p.category_id, c.slug,
                                      p.category_confidence, COALESCE(p.category_validated, false)
                                 FROM products p LEFT JOIN categories c ON c.id = p.category_id
                                WHERE p.asin = ANY(%s) AND p.is_active
                                  AND COALESCE(p.category_confidence, 0) < %s""",
                            (list(clicks), demand.DISPLAY_FLOOR))
                rows = [dict(zip(("id", "asin", "title", "brand", "category_id", "slug",
                                  "confidence", "validated"), r)) for r in cur.fetchall()]
                cur.execute("SELECT slug, MIN(id) FROM categories WHERE is_active GROUP BY slug")
                slug_ids = dict(cur.fetchall())
            conn.rollback()
            # Most-clicked first; validated rows are counted, never scored.
            rows.sort(key=lambda r: (-clicks.get(r["asin"], 0), r["id"]))
            todo = [r for r in rows if not r["validated"]][:dcfg["max_per_run"]]
            todo_ids = {r["id"] for r in todo}
            out["candidates"] = len(todo)
            results = demand.run_classifier(dcfg, [
                {"asin": r["asin"], "title": r["title"] or "", "brand": r["brand"],
                 "hint": r["slug"]} for r in todo])
            updates, unresolved = [], []
            for r in rows:
                if r["id"] not in todo_ids and not r["validated"]:
                    continue                      # over this run's cap
                verdict, upd = demand.decide(
                    r, results.get(r["asin"]), min_score=dcfg["min_score"],
                    assign_uncategorised=bool(dcfg["assign_uncategorised"]),
                    slug_ids=slug_ids)
                out["verdicts"][verdict] = out["verdicts"].get(verdict, 0) + 1
                if upd:
                    updates.append(upd)
                elif verdict in ("unplaced", "conflict", "low-score", "unknown-slug"):
                    res = results.get(r["asin"]) or {}
                    unresolved.append({"asin": r["asin"], "clicks": clicks.get(r["asin"], 0),
                                       "verdict": verdict, "category": r["slug"],
                                       "rules_slug": res.get("slug"),
                                       "rules_confidence": res.get("confidence"),
                                       "title": (r["title"] or "")[:90]})
            if updates:
                with conn.cursor() as cur:
                    for u in updates:
                        # Only while the row is as we read it: still active,
                        # still below the floor, not validated, same category.
                        cur.execute("""UPDATE products
                                          SET category_id = %s, category_confidence = %s,
                                              updated_at = NOW()
                                        WHERE id = %s AND is_active
                                          AND COALESCE(category_confidence, 0) < %s
                                          AND NOT COALESCE(category_validated, false)
                                          AND category_id IS NOT DISTINCT FROM %s""",
                                    (u["category_id"], u["category_confidence"], u["id"],
                                     demand.DISPLAY_FLOOR, u["expect_category_id"]))
                        out["written"] += cur.rowcount
                conn.commit()
            out["unresolved"] = sorted(unresolved, key=lambda u: -u["clicks"])[:int(
                dcfg.get("report_limit") or 50)]
            with conn.cursor() as cur:
                cur.execute("""SELECT count(DISTINCT asin) FILTER (
                                        WHERE COALESCE(category_confidence, 0) >= %s),
                                      count(DISTINCT asin)
                                 FROM products WHERE asin = ANY(%s) AND is_active""",
                            (demand.DISPLAY_FLOOR, list(clicks)))
                passing, total = cur.fetchone()
            conn.rollback()
            out["pass_pct"] = round(100.0 * passing / total, 1) if total else None
        except Exception as e:  # noqa: BLE001 — optional phase
            try:
                conn.rollback()
            except Exception:
                pass
            out["error"] = f"{type(e).__name__}: {e}"[:300]
        return out

    def run(self) -> RunResult:
        domain, foreign = load_vocab(self.cfg)
        if not domain or not foreign:
            return RunResult(status="failure",
                             summary="site.yaml has no domain/foreign vocabulary — refusing to judge",
                             metrics={"decategorised": 0})

        conn = self._conn()
        decat = 0
        unscored_pct = 0.0
        dem: dict = {"enabled": False}
        try:
            # Demand first: score the clicked products before the unscored
            # share is measured and before any de-categorisation.
            self.status("scoring clicked low-confidence products", progress=0.1)
            dem = self._demand_scoring(conn)
            if dem.get("enabled"):
                self.decide("observation",
                            f"demand scoring: {dem['clicked_asins']} clicked ASINs, "
                            f"{dem['candidates']} low-confidence rows classified, "
                            f"{dem['written']} scored; verdicts {dem['verdicts']}"
                            + (f"; error: {dem['error']}" if dem.get("error") else ""))
                for u in dem.get("unresolved", [])[:5]:
                    self.decide("observation",
                                f"clicked but unconfirmed ({u['verdict']}, {u['clicks']} clicks) "
                                f"in {u['category']}: {u['title'][:70]}")
                if dem.get("unresolved") or dem.get("written"):
                    try:
                        self.storage.write_json(
                            f"agents/{self.agent_id}/runs/{self.run_ts}/demand-scoring.json",
                            {"schema_version": "1", "site": self.site, "run_ts": self.run_ts,
                             **{k: v for k, v in dem.items() if k != "enabled"}})
                    except Exception as e:  # noqa: BLE001 — report only
                        self.decide("observation", f"demand-scoring artifact not saved: {e}")
            with conn.cursor() as cur:
                cur.execute("""SELECT count(*) FILTER (WHERE category_confidence IS NULL),
                                      count(*)
                                 FROM products
                                WHERE is_active AND category_id IS NOT NULL""")
                unscored, total = cur.fetchone()
            unscored_pct = (unscored / total) if total else 0.0
            self.status(f"{total} categorised products, {unscored_pct*100:.1f}% never scored",
                        progress=0.2)

            # Only ever consider rows nobody has vouched for.
            with conn.cursor() as cur:
                cur.execute("""SELECT p.id, p.asin, p.title, c.slug
                                 FROM products p JOIN categories c ON c.id = p.category_id
                                WHERE p.is_active
                                  AND p.category_id IS NOT NULL
                                  AND p.category_confidence IS NULL
                                  AND NOT COALESCE(p.category_validated, false)""")
                rows = cur.fetchall()

            bad = []
            for pid, asin, title, slug in rows:
                off, words = is_off_domain(title or "", domain, foreign)
                if off:
                    bad.append((pid, asin, title, slug, words))

            self.status(f"{len(bad)} off-domain products found", progress=0.6)
            for pid, asin, title, slug, words in bad[:5]:
                self.decide("observation",
                            f"off-domain in {slug}: {str(title)[:70]} (foreign: {','.join(words)})")

            batch = bad[:MAX_FIX]
            if batch:
                with conn.cursor() as cur:
                    cur.execute("""UPDATE products SET category_id = NULL, updated_at = NOW()
                                    WHERE id = ANY(%s)""", ([b[0] for b in batch],))
                    decat = cur.rowcount
                conn.commit()
        finally:
            conn.close()

        # A high unscored share is a CODE problem: something files products
        # without running the categoriser. That is the implementer's to fix.
        recs = []
        if unscored_pct > UNSCORED_ALERT:
            now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            recs.append({
                "id": f"cat-{now}-001",
                "type": "categorisation-pipeline-gap",
                "priority": "high",
                "title": (f"{unscored_pct*100:.0f}% of categorised products were never scored "
                          f"by the categoriser"),
                "rationale": (
                    "products.category_confidence IS NULL means a category was assigned by a "
                    "path that never ran assign-categories' scoring rules. Those rows are how "
                    "a slow cooker and an ink cartridge ended up in a games-cartridges buying "
                    "guide. De-categorising them treats the symptom; the writer that skips "
                    "scoring is the defect."),
                "recommendation": (
                    "Find every code path that writes products.category_id without also "
                    "setting category_confidence, and make it run the same scoring rules "
                    "assign-categories uses (they are exported). A path that cannot score "
                    "should leave category_id NULL rather than guess. Add a regression test "
                    "asserting no INSERT/UPDATE sets category_id without a confidence."),
                "evidence": {"unscored_pct": round(unscored_pct, 4), "site": self.site},
                "effort": "unknown", "impact": "catalogue-wide",
            })
            doc = {"run_ts": now, "mode": "category-integrity", "site": self.site,
                   "recommendations": recs}
            self.storage.write_json(
                f"agents/{self.agent_id}/runs/{self.run_ts}/recommendations.json", doc)

        metrics = {"decategorised": float(decat),
                   "unscored_pct": round(unscored_pct, 4),
                   "recs": float(len(recs))}
        demand_note = ""
        if dem.get("enabled"):
            v = dem.get("verdicts") or {}
            metrics.update({
                "demand_clicked_asins": float(dem.get("clicked_asins") or 0),
                "demand_candidates": float(dem.get("candidates") or 0),
                "demand_scored": float(dem.get("written") or 0),
                "demand_confirmed": float(v.get("confirm", 0)),
                "demand_assigned": float(v.get("assign", 0)),
                "demand_unplaced": float(v.get("unplaced", 0)),
                "demand_conflicts": float(v.get("conflict", 0)),
                "demand_validated_skipped": float(v.get("validated", 0)),
                "demand_error": 1.0 if dem.get("error") else 0.0,
            })
            if dem.get("pass_pct") is not None:
                # Share of clicked active ASINs that clear the Top-Pick
                # confidence floor (>= 0.5) after this run.
                metrics["clicked_confidence_pass_pct"] = float(dem["pass_pct"])
            demand_note = (f"{int(dem.get('written') or 0)} clicked low-confidence products "
                           f"scored" + (f" ({dem['pass_pct']}% of clicked products now clear "
                                        f"the 0.5 floor)" if dem.get("pass_pct") is not None else "")
                           + (f" [demand phase error: {dem['error'][:120]}]" if dem.get("error") else "")
                           + "; ")
        return RunResult(
            status="success",
            summary=(demand_note + f"{decat} off-domain products de-categorised; "
                     f"{unscored_pct*100:.1f}% of the catalogue was never scored"
                     + (f"; queued a pipeline fix for the implementer" if recs else "")),
            metrics=metrics,
        )


if __name__ == "__main__":
    CategoryIntegrityAgent().run_once()
