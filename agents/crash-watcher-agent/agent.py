"""crash-watcher-agent — poll Sentry for new crashes, dispatch fixes via the
implementer chain.

Loop (every cron tick):
  1. GET /api/0/projects/<org>/<proj>/issues/?query=is:unresolved&statsPeriod=24h
  2. For each NEW issue (not in our accumulator):
       a. GET /api/0/issues/<id>/events/latest/ — pulls top frames + breadcrumbs
       b. Resolve top frame's source file via Sentry → grab ±50 lines of context
       c. Build a rec describing the crash + suggested fix surface
       d. dispatch_now() to the implementer with rec_type=crash-fix
       e. Record the Sentry issue_id in the accumulator so we don't re-dispatch
  3. For each issue WE dispatched whose status flipped to "resolved" in Sentry:
       a. Mark accumulator entry state=implemented
  4. For each issue we marked implemented BUT it's reappeared as unresolved:
       a. Re-dispatch with an annotation that the prior fix didn't hold

Env (set in manifest.metadata + .env):
  SENTRY_API_TOKEN  — Internal Integration auth token (sentry.io → Settings →
                      Integrations → Internal Integration → "Issues: Read &
                      Write" scope)
  SENTRY_ORG_SLUG   — your org slug (e.g., "aisleprompt")
  SENTRY_PROJECT_SLUG — the project slug (e.g., "aisleprompt-mobile")

Storage:
  agents/crash-watcher-agent/state/accumulator.json — per-issue tracking
    (uses framework.core.rec_memory schema so the Queue page lists them
    alongside other producer agents)
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from framework.core.agent_base import AgentBase, RunResult  # noqa: E402
from framework.core.rec_memory import (  # noqa: E402
    load_active, save_active, transition,
)
from framework.core import dispatch as fw_dispatch  # noqa: E402


SENTRY_BASE_DEFAULT = "https://sentry.io/api/0"


def _sentry_base() -> str:
    # SENTRY_API_BASE lets the same agent talk to either sentry.io or a
    # self-hosted GlitchTip (Sentry-API-compatible). See install/glitchtip/.
    return os.environ.get("SENTRY_API_BASE", SENTRY_BASE_DEFAULT).rstrip("/")


def _sentry_get(path: str, token: str) -> Any:
    url = f"{_sentry_base()}{path}"
    # Cloudflare in front of a self-hosted GlitchTip serves a 403 (error 1010)
    # for the default `Python-urllib/3.x` UA — it treats it as a bot. A
    # standard UA string passes the browser-integrity check.
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "User-Agent": "reusable-agents-crash-watcher/1.0",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return {"error": e.code, "body": e.read().decode()[:400]}
    except Exception as e:
        return {"error": "exception", "message": str(e)}


def _build_rec_for_issue(issue: dict, event: dict | None) -> dict:
    """Translate a Sentry issue + its latest event into a rec the implementer
    can act on. Includes the top frame's file + line so the LLM has a
    concrete entry point."""
    title = issue.get("title") or issue.get("metadata", {}).get("type", "Crash")
    culprit = issue.get("culprit", "")
    count = issue.get("count", 0)
    user_count = issue.get("userCount", 0)
    release = (issue.get("firstRelease") or {}).get("shortVersion", "")
    permalink = issue.get("permalink", "")
    # Pull top frame from latest event
    files_hint: list[str] = []
    top_frame_summary = ""
    if event and isinstance(event, dict):
        for entry in event.get("entries", []):
            if entry.get("type") == "exception":
                vals = (entry.get("data") or {}).get("values") or []
                for v in vals:
                    frames = ((v.get("stacktrace") or {}).get("frames") or [])
                    # Last frame = innermost (where the crash actually happened)
                    for f in reversed(frames):
                        if f.get("inApp"):
                            file = f.get("filename") or f.get("absPath") or ""
                            line = f.get("lineNo") or 0
                            fn = f.get("function") or ""
                            if file and file not in files_hint:
                                files_hint.append(file)
                            if not top_frame_summary and file:
                                top_frame_summary = f"{file}:{line} in {fn}()"
                                break
                    if top_frame_summary:
                        break
            if top_frame_summary:
                break
    return {
        "id": issue["id"],  # Sentry issue id (numeric)
        "title": f"[crash] {title}",
        "type": "crash-fix",
        "severity": "high" if user_count > 5 else "medium",
        "rationale": (
            f"Sentry reports {count} events affecting {user_count} user(s). "
            f"Culprit: {culprit}. Release: {release}. Top frame: "
            f"{top_frame_summary or '(no in-app frame parsed)'}. "
            f"See Sentry: {permalink}"
        ),
        "files": files_hint[:5],
        "sentry": {
            "issue_id": issue["id"],
            "permalink": permalink,
            "count": count,
            "user_count": user_count,
            "release": release,
        },
    }


class CrashWatcher(AgentBase):
    agent_id = "crash-watcher-agent"
    category = "ops"

    def signals(self) -> dict | None:
        """Auto short-circuit when the Sentry issue set hasn't changed.
        Most 10-min ticks find zero new issues for a stable app, so
        skipping the LLM-less-but-still-Azure-write loop is free perf."""
        token = os.environ.get("SENTRY_API_TOKEN", "").strip()
        org = os.environ.get("SENTRY_ORG_SLUG", "").strip()
        proj = os.environ.get("SENTRY_PROJECT_SLUG", "").strip()
        if not (token and org and proj):
            return None  # Let run() fail with the proper config error.
        head = _sentry_get(
            f"/projects/{org}/{proj}/issues/?query=is:unresolved&statsPeriod=24h&limit=50",
            token,
        )
        if not isinstance(head, list):
            return None  # API error → let run() handle it.
        # Hash just the issue IDs + their lastSeen timestamps. New issues
        # bust the hash; a re-emerging issue (lastSeen updated) busts it too.
        return {"issues": sorted([(i.get("id"), i.get("lastSeen")) for i in head])}

    def run(self) -> RunResult:
        token = os.environ.get("SENTRY_API_TOKEN", "").strip()
        org = os.environ.get("SENTRY_ORG_SLUG", "").strip()
        proj = os.environ.get("SENTRY_PROJECT_SLUG", "").strip()
        if not (token and org and proj):
            return RunResult(
                status="failure",
                summary="SENTRY_API_TOKEN / SENTRY_ORG_SLUG / SENTRY_PROJECT_SLUG not configured — see agents/crash-watcher-agent/README.md",
            )

        self.status("polling Sentry", progress=0.1)
        path = f"/projects/{org}/{proj}/issues/?query=is:unresolved&statsPeriod=24h&limit=50"
        issues = _sentry_get(path, token)
        if isinstance(issues, dict) and issues.get("error"):
            return RunResult(
                status="failure",
                summary=f"Sentry API {issues['error']}: {issues.get('body','')[:200]}",
            )
        if not isinstance(issues, list):
            return RunResult(
                status="failure",
                summary=f"Sentry returned unexpected shape: {type(issues).__name__}",
            )

        self.decide("observation", f"Sentry returned {len(issues)} unresolved issues")

        # Load our accumulator (skipped/implemented dedup)
        accum = load_active(self.storage, self.agent_id)
        seen_ids = {p.get("id") for p in accum.get("proposals", [])}

        new_recs: list[dict] = []
        re_emerged: list[dict] = []
        for issue in issues:
            iid = issue.get("id")
            if not iid:
                continue
            if iid in seen_ids:
                # Already tracked. Did it come back as unresolved after we
                # marked it implemented? That means the prior fix didn't hold.
                prior = next((p for p in accum["proposals"] if p.get("id") == iid), None)
                if prior and prior.get("state") == "implemented":
                    transition(accum, iid, to_state="open", run_ts=self.run_ts)
                    re_emerged.append(issue)
                continue
            # Fetch the latest event for frame info
            event = _sentry_get(f"/issues/{iid}/events/latest/", token)
            if isinstance(event, dict) and event.get("error"):
                event = None
            rec = _build_rec_for_issue(issue, event if isinstance(event, dict) else None)
            new_recs.append(rec)
            accum.setdefault("proposals", []).append({
                "id": iid,
                "title": rec["title"],
                "rec_type": "crash-fix",
                "severity": rec["severity"],
                "state": "open",
                "first_seen_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "last_seen_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "first_seen_run_ts": self.run_ts,
                "last_seen_run_ts": self.run_ts,
                "seen_count": 1,
                "body": rec,
            })

        save_active(self.storage, self.agent_id, accum)

        # Persist a per-run recommendations.json so the implementer's
        # dispatch_now() pipeline can read it.
        if new_recs or re_emerged:
            recs_for_dispatch = []
            for i, rec in enumerate(new_recs + [_build_rec_for_issue(x, None) for x in re_emerged], start=1):
                rec_copy = dict(rec)
                rec_copy["id"] = f"crash-{i:03d}"  # implementer-friendly id
                rec_copy["original_sentry_id"] = rec.get("id")
                recs_for_dispatch.append(rec_copy)
            self.storage.write_json(
                f"agents/{self.agent_id}/runs/{self.run_ts}/recommendations.json",
                {"recommendations": recs_for_dispatch},
            )

            # Dispatch to implementer for ONE site at a time (mobile crashes
            # → fixes land in aisleprompt repo by default; cross-site crash
            # detection would need a per-issue routing rule).
            try:
                fw_dispatch.dispatch_now(
                    source_agent_id=self.agent_id,
                    source_run_ts=self.run_ts,
                    site=os.environ.get("CRASH_WATCHER_TARGET_SITE", "aisleprompt"),
                    dispatch_kind="crash-fix",
                    rec_ids=",".join(r["id"] for r in recs_for_dispatch),
                    storage=self.storage,
                )
            except Exception as e:
                self.decide("error", f"dispatch_now failed: {e}")

        return RunResult(
            status="success",
            summary=(
                f"Sentry: {len(issues)} unresolved. New: {len(new_recs)}. "
                f"Re-emerged after prior fix: {len(re_emerged)}. "
                f"Accumulator: {len(accum.get('proposals', []))} total."
            ),
            metrics={
                "unresolved": len(issues),
                "new": len(new_recs),
                "re_emerged": len(re_emerged),
            },
        )


def main() -> int:
    return 0 if CrashWatcher().run_once().status == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
