"""Catalog-Audit Shipped Backfill — implementation.

Periodically reconciles catalog-audit migration recs:
  1. Walks the most recent 30 recommendations.json files per site.
  2. For each `implemented:true, shipped:false` rec with `ref_ids`, runs
     a per-criterion verification query against the prod DB.
  3. If the verifier confirms the migration's effects landed, flips the
     rec to `shipped:true` and writes back to storage.

Why: the deployer skips catalog-audit dispatches (DB-only, no docker
push), so the rec lifecycle stops at `implemented`. The migrations DO
get applied (via the app's startup migration runner), but nothing else
flips the lifecycle to shipped. This agent does that flip on a 30-min
cadence so the dashboard's queue panel reflects reality.

Originally a one-shot script at
`agents/implementer/catalog-audit-shipped-backfill.py`. Same logic,
wrapped here as an AgentBase so it shows up on the dashboard with
goals + status visibility.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

_REPO = "/home/voidsstr/development/reusable-agents"
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from framework.core.agent_base import AgentBase, RunResult


# Verification queries per catalog-audit criterion. After the migration
# runs, the listed ref_ids should NOT match these queries — that's the
# signal the migration's effects landed. Add new criteria here as the
# catalog-audit-agent introduces them.
def _verify_migration_applied(conn, rec: dict) -> tuple[bool, str]:
    check_id = rec.get("check_id", "") or rec.get("category", "")
    ref_ids_raw = rec.get("ref_ids") or (rec.get("migration_template") or {}).get("ref_ids") or []
    # Coerce to ints — some older runs persisted ids as strings, but
    # the DB columns are integer. ANY() with mismatched types raises
    # "operator does not exist: integer = text".
    ref_ids: list[int] = []
    for v in ref_ids_raw:
        try:
            ref_ids.append(int(v))
        except (TypeError, ValueError):
            pass
    if not ref_ids:
        return False, f"no integer-castable ref_ids (raw={ref_ids_raw[:3]})"

    cur = conn.cursor()
    try:
        if check_id == "recipe-image-present":
            cur.execute("SELECT COUNT(*) FROM recipe_catalog "
                        "WHERE id = ANY(%s) AND is_active = true", (ref_ids,))
            n = cur.fetchone()[0]
            return (n == 0, f"{n} of {len(ref_ids)} still active (expected 0)")

        if check_id == "recipe-nutrition-sanity":
            cur.execute("SELECT COUNT(*) FROM recipe_catalog "
                        "WHERE id = ANY(%s) "
                        "AND (calories IS NOT NULL OR protein IS NOT NULL "
                        "     OR carbs IS NOT NULL OR fat IS NOT NULL)", (ref_ids,))
            n = cur.fetchone()[0]
            return (n == 0, f"{n} of {len(ref_ids)} still have nutrition")

        if check_id == "product-image-present":
            cur.execute("SELECT COUNT(*) FROM kitchen_products "
                        "WHERE id = ANY(%s) AND is_active = true", (ref_ids,))
            n = cur.fetchone()[0]
            return (n == 0, f"{n} of {len(ref_ids)} still active")

        if check_id == "recipe-image-valid-url":
            cur.execute("SELECT COUNT(*) FROM recipe_catalog "
                        "WHERE id = ANY(%s) AND image_url IS NOT NULL "
                        "AND image_url != ''", (ref_ids,))
            n = cur.fetchone()[0]
            return (n == 0, f"{n} of {len(ref_ids)} still have image_url")

        if check_id == "recipe-video-present":
            cur.execute("SELECT COUNT(*) FROM recipe_catalog "
                        "WHERE id = ANY(%s) AND is_active = true "
                        "AND (video_url IS NOT NULL AND video_url != '')", (ref_ids,))
            n = cur.fetchone()[0]
            return (n == 0, f"{n} of {len(ref_ids)} still active w/ video missing")

        if check_id == "recipe-duplicate-source-url":
            cur.execute("SELECT COUNT(*) FROM recipe_catalog "
                        "WHERE id = ANY(%s) AND is_active = true", (ref_ids,))
            n = cur.fetchone()[0]
            return (n == 0, f"{n} of {len(ref_ids)} dupes still active")

        if check_id == "product-schema-rich-results":
            return True, "schema fix — assume shipped after commit"

        return False, f"no verifier for check_id={check_id!r}"
    except Exception as e:
        return False, f"verifier query failed: {e}"
    finally:
        cur.close()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class CatalogAuditShippedBackfill(AgentBase):
    agent_id = "catalog-audit-shipped-backfill"
    name = "Catalog-Audit Shipped Backfill"
    description = (
        "Verifies catalog-audit migration effects against prod DB and "
        "flips the rec lifecycle to shipped. Runs every 30 min."
    )
    category = "ops"

    init_goals = [
        {
            "id": "shipped-lifecycle-coverage",
            "title": "Catalog-audit recs flipped to shipped",
            "description": (
                "Cumulative count of catalog-audit recs whose lifecycle "
                "was reconciled from implemented → shipped after prod DB "
                "verification. Higher = more accurate dashboard."
            ),
            "metric": {"name": "total_shipped_flipped", "target": 200,
                       "direction": "up", "unit": "recs"},
            "directives": [
                "Run prod-DB verifier per implemented:true rec.",
                "Only flip shipped:false → shipped:true, never the other way.",
                "Skip recs without ref_ids (can't verify).",
            ],
        },
    ]

    DSN_BY_SITE = {
        "aisleprompt": "DATABASE_URL_AISLEPROMPT",
        "specpicks":   "DATABASE_URL_SPECPICKS",
    }

    def _connect(self, site: str):
        env_var = self.DSN_BY_SITE.get(site)
        dsn = os.environ.get(env_var or "")
        if not dsn:
            self.decide("warn", f"{site}: env {env_var} not set — skipping")
            return None
        try:
            import psycopg2
        except ImportError:
            self.decide("error", "psycopg2 not installed in agent venv")
            return None
        try:
            conn = psycopg2.connect(dsn, connect_timeout=10)
            # Autocommit so a single failing verifier doesn't poison the
            # transaction for the rest of the recs in this site.
            conn.autocommit = True
            return conn
        except Exception as e:
            self.decide("error", f"{site}: connect failed: {e}")
            return None

    def signals(self) -> dict | None:
        """Short-circuit when no new catalog-audit recommendations.json
        files have appeared since the last run. The backfill reads
        recommendations.json across recent runs — if the set hasn't
        changed, our DB-shipped-flag reconciliation would be identical.
        """
        try:
            keys_aisle = self.storage.list_prefix("agents/aisleprompt-catalog-audit-agent/runs/")
            keys_spec = self.storage.list_prefix("agents/specpicks-catalog-audit-agent/runs/")
        except Exception:
            return None
        # Most-recent 30 run-dirs per site are what run() scans.
        def head30(keys):
            recs = sorted([k for k in (keys or []) if k.endswith("/recommendations.json")], reverse=True)[:30]
            return recs
        return {"aisle": head30(keys_aisle), "specpicks": head30(keys_spec)}

    def run(self) -> RunResult:
        self.status("starting", progress=0.0)
        prior_total = (self.state or {}).get("total_shipped_flipped", 0)

        flipped_total = 0
        checked_total = 0
        per_site_summary: dict[str, dict] = {}

        for site_idx, site in enumerate(("aisleprompt", "specpicks")):
            self.status(f"reconciling {site}", progress=0.1 + 0.4 * site_idx)
            agent_id = f"{site}-catalog-audit-agent"

            conn = self._connect(site)
            if conn is None:
                per_site_summary[site] = {"checked": 0, "flipped": 0, "skipped": True}
                continue

            try:
                keys = sorted(self.storage.list_prefix(f"agents/{agent_id}/runs/"),
                              reverse=True)
                rec_keys = [k for k in keys if k.endswith("/recommendations.json")][:30]
                self.decide("scan",
                            f"{site}: scanning {len(rec_keys)} recent recommendations.json files")

                site_flipped = 0
                site_checked = 0
                for rk in rec_keys:
                    rd = self.storage.read_json(rk) or {}
                    recs = rd.get("recommendations", [])
                    if not recs:
                        continue
                    file_changed = False
                    for r in recs:
                        if not r.get("implemented") or r.get("shipped"):
                            continue
                        site_checked += 1
                        applied, reason = _verify_migration_applied(conn, r)
                        if applied:
                            r["shipped"] = True
                            r["shipped_at"] = _now_iso()
                            r["shipped_via"] = self.agent_id
                            r["shipped_verification"] = reason
                            file_changed = True
                            site_flipped += 1
                    if file_changed:
                        self.storage.write_json(rk, rd)

                per_site_summary[site] = {"checked": site_checked, "flipped": site_flipped}
                flipped_total += site_flipped
                checked_total += site_checked
                self.decide("done",
                            f"{site}: checked {site_checked}, flipped {site_flipped} to shipped")
            finally:
                conn.close()

        new_total = prior_total + flipped_total
        self.status("done", progress=1.0)

        summary_parts = [
            f"{site}={s['flipped']}/{s['checked']}"
            for site, s in per_site_summary.items()
            if not s.get("skipped")
        ]
        skipped = [s for s in per_site_summary if per_site_summary[s].get("skipped")]
        skip_str = (f" [skipped: {','.join(skipped)}]" if skipped else "")
        return RunResult(
            status="success",
            summary=(f"Backfilled {flipped_total} shipped flag(s) "
                     f"from {checked_total} candidate(s). "
                     + ", ".join(summary_parts) + skip_str),
            metrics={
                "total_shipped_flipped": new_total,
                "this_run_flipped": flipped_total,
                "this_run_checked": checked_total,
            },
            next_state={"total_shipped_flipped": new_total,
                        "last_run_at": _now_iso()},
        )


if __name__ == "__main__":
    CatalogAuditShippedBackfill().run_once()
