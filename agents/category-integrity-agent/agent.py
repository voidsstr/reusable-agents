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

    def run(self) -> RunResult:
        domain, foreign = load_vocab(self.cfg)
        if not domain or not foreign:
            return RunResult(status="failure",
                             summary="site.yaml has no domain/foreign vocabulary — refusing to judge",
                             metrics={"decategorised": 0})

        conn = self._conn()
        decat = 0
        unscored_pct = 0.0
        try:
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

        return RunResult(
            status="success",
            summary=(f"{decat} off-domain products de-categorised; "
                     f"{unscored_pct*100:.1f}% of the catalogue was never scored"
                     + (f"; queued a pipeline fix for the implementer" if recs else "")),
            metrics={"decategorised": float(decat),
                     "unscored_pct": round(unscored_pct, 4),
                     "recs": float(len(recs))},
        )


if __name__ == "__main__":
    CategoryIntegrityAgent().run_once()
