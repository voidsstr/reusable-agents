#!/usr/bin/env python3
"""Turn user feedback into fixes.

Reads a site's feedback table, decides what is actionable, and hands the
actionable defects to the implementer via the backlog dispatcher (which is what
actually edits code and deploys). Everything it touches gets a status, so the
same report is never picked up twice.

The status machine is the point. Feedback arrives as `new`; this agent CLAIMS a
row atomically before looking at it, so two ticks — or two agents — cannot both
start on the same report:

    new ──claim──> triaging ──actionable──> in_progress ──implementer──> resolved
                       │
                       ├── not a defect ──> wont_fix
                       └── too vague ─────> needs_info

Only `new` is ever selected. A row in `in_progress` is someone's work in flight
and is left alone.

Site-agnostic by config: aisleprompt and specpicks disagree on table name AND
column names (`Feedback."createdAt"` vs `feedback.created_at`), so the mapping
lives in site.yaml rather than in branches here.
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
from triage import build_recommendation, classify, normalise  # noqa: E402

MAX_PER_RUN = int(os.environ.get("FEEDBACK_MAX_PER_RUN", "20"))


def _cfg() -> dict:
    p = os.environ.get("FEEDBACK_TRIAGE_CONFIG")
    if not p:
        raise SystemExit("set FEEDBACK_TRIAGE_CONFIG to a site.yaml")
    return yaml.safe_load(Path(p).read_text())


class FeedbackTriageAgent(AgentBase):
    agent_id = os.environ.get("AGENT_ID", "feedback-triage-agent")

    def __init__(self, *a, **kw):
        self.cfg = _cfg()
        self.site = self.cfg.get("site") or "unknown"
        super().__init__(*a, **kw)

    def _conn(self):
        dsn = os.environ.get(self.cfg["db_env"]) or os.environ.get("DATABASE_URL")
        if not dsn:
            raise SystemExit(f"{self.cfg['db_env']} not set")
        return psycopg2.connect(dsn)

    def _pending(self, conn, src) -> int:
        c = src["columns"]
        with conn.cursor() as cur:
            cur.execute(f'SELECT count(*) FROM {src["table"]} WHERE {c["status"]} = %s',
                        (src["statuses"]["new"],))
            return int(cur.fetchone()[0])

    def signals(self):
        try:
            with self._conn() as conn:
                total = sum(self._pending(conn, s) for s in self.cfg["sources"])
        except Exception:
            return None
        # Never short-circuit while a user is waiting on an unread report.
        return None if total else {"pending": 0}

    def run(self) -> RunResult:
        conn = self._conn()
        claimed: list[dict] = []
        counts = {"investigate": 0, "needs_info": 0, "not_a_defect": 0, "ignore": 0}
        try:
            for src in self.cfg["sources"]:
                c, st = src["columns"], src["statuses"]
                with conn.cursor() as cur:
                    cur.execute(
                        f'SELECT {c["id"]} FROM {src["table"]} WHERE {c["status"]} = %s '
                        f'ORDER BY {c["created"]} ASC LIMIT %s',
                        (st["new"], MAX_PER_RUN - len(claimed)))
                    ids = [r[0] for r in cur.fetchall()]

                for fid in ids:
                    # Atomic claim: the WHERE re-checks the status, so a second
                    # worker that reads the same id loses the race and moves on.
                    cols = ", ".join(f'{c[k]}' for k in ("id", "message", "kind", "severity", "url")
                                     if c.get(k))
                    with conn.cursor() as cur:
                        cur.execute(
                            f'UPDATE {src["table"]} SET {c["status"]} = %s '
                            f'WHERE {c["id"]} = %s AND {c["status"]} = %s RETURNING {cols}',
                            (st["claimed"], fid, st["new"]))
                        row = cur.fetchone()
                    conn.commit()
                    if not row:
                        continue
                    keys = [k for k in ("id", "message", "kind", "severity", "url") if c.get(k)]
                    fb = dict(zip(keys, row))
                    cls = classify(fb)
                    counts[cls["action"]] = counts.get(cls["action"], 0) + 1
                    claimed.append({"src": src, "fb": fb, "cls": cls})

            recs = []
            now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            for i, item in enumerate(claimed, 1):
                src, fb, cls = item["src"], item["fb"], item["cls"]
                c, st = src["columns"], src["statuses"]
                if cls["action"] == "investigate":
                    rec = build_recommendation(fb, cls, f"fb-{now}-{i:03d}", self.site)
                    recs.append(rec)
                    new_status, note = st["working"], (
                        f"[triage] actionable (confidence {cls['confidence']}). "
                        f"Queued for the implementer as {rec['id']}.")
                    self.decide("action", f"queued feedback {fb['id']}: {str(fb.get('message'))[:80]}")
                elif cls["action"] == "needs_info":
                    new_status, note = st["needs_info"], f"[triage] {cls['reason']}"
                elif cls["action"] == "ignore":
                    new_status, note = st["rejected"], f"[triage] {cls['reason']}"
                else:
                    new_status, note = st["rejected"], f"[triage] {cls['reason']}"

                sets = [f'{c["status"]} = %s']
                vals = [new_status]
                if c.get("note"):
                    sets.append(f'{c["note"]} = %s')
                    vals.append(note)
                if c.get("updated"):
                    sets.append(f'{c["updated"]} = NOW()')
                vals.append(fb["id"])
                with conn.cursor() as cur:
                    cur.execute(f'UPDATE {src["table"]} SET {", ".join(sets)} WHERE {c["id"]} = %s', vals)
                conn.commit()
        finally:
            conn.close()

        if recs:
            # The dispatcher walks producer run-dirs for recommendations.json and
            # hands them to the implementer, which is the only thing that edits
            # code and deploys. Writing here is how feedback reaches a deploy.
            doc = {"run_ts": now, "mode": "feedback", "site": self.site,
                   "recommendations": recs}
            key = f"agents/{self.agent_id}/runs/{self.run_ts}/recommendations.json"
            self.storage.write_json(key, doc)
            try:
                # AgentBase provides run_ts but no run_dir; a local copy is only
                # a convenience for debugging - the blob is what the dispatcher reads.
                d = Path(os.path.expanduser("~/.reusable-agents")) / self.agent_id / "runs" / self.run_ts
                d.mkdir(parents=True, exist_ok=True)
                (d / "recommendations.json").write_text(json.dumps(doc, indent=2))
            except Exception:
                pass

        return RunResult(
            status="success",
            summary=(f"{len(claimed)} claimed — {counts['investigate']} queued for the implementer, "
                     f"{counts['needs_info']} need info, "
                     f"{counts['not_a_defect'] + counts['ignore']} not defects"),
            metrics={f"feedback_{k}": float(v) for k, v in counts.items()} |
                    {"feedback_claimed": float(len(claimed))},
        )


if __name__ == "__main__":
    FeedbackTriageAgent().run_once()
