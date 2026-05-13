"""oauth-heartbeat-agent — keeps Google OAuth refresh tokens alive.

Why this exists
---------------
Google OAuth apps in "Testing" status get refresh_tokens that expire
after 7 days of non-use. Every successful refresh resets the 7-day
clock. The seo-opportunity-agent normally exercises the token every
2 hours, so this isn't a problem during steady-state operation.

BUT the moment the agent is paused for >7 days (claude pool outage,
debugging session, OAuth-related fix, holiday), the token silently
expires and Google returns HTTP 400 on next refresh. Recovery
requires re-running the consent flow with a browser, which is
disruptive.

This heartbeat solves it: a tiny daily agent that does NOTHING but
call refresh-token.py for each registered oauth file. The successful
mint resets the 7-day clock. If the seo-opportunity-agent is paused
for weeks, the heartbeat keeps the token alive in the background.

What it monitors
----------------
For each oauth file:
  - Mints a fresh access token (proves refresh_token is still valid)
  - Records age of last successful mint
  - Emits operator-alert email if a refresh fails (early warning
    before the next agent run hits it)

Add an oauth file to its watch list by setting:
  OAUTH_HEARTBEAT_FILES=/path/to/oauth1.json:/path/to/oauth2.json
(default watches just the seo-opportunity-agent's file)
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve()
_REPO_ROOT = _HERE.parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from framework.core.agent_base import AgentBase, RunResult  # noqa: E402


DEFAULT_OAUTH_FILES = [
    os.path.expanduser("~/.reusable-agents/seo/.oauth.json"),
]
REFRESH_SCRIPT = (
    _REPO_ROOT
    / "agents/seo-opportunity-agent/lib/collector/refresh-token.py"
)


class OAuthHeartbeatAgent(AgentBase):
    agent_id = "oauth-heartbeat-agent"
    category = "ops"
    # Status writes go to status.json; this is a low-frequency low-risk
    # agent so the default cadence is fine.

    def signals(self) -> dict | None:
        """No short-circuit — the whole point is to fire daily even when
        nothing else moves. Returning None opts out of auto-skip."""
        return None

    def run(self) -> RunResult:
        # Resolve the watchlist
        env_list = os.environ.get("OAUTH_HEARTBEAT_FILES", "").strip()
        if env_list:
            files = [p.strip() for p in env_list.split(":") if p.strip()]
        else:
            files = DEFAULT_OAUTH_FILES

        self.status(f"refreshing {len(files)} oauth token(s)", progress=0.1)

        results: list[dict] = []
        failed: list[str] = []
        now_unix = int(time.time())

        for oauth_path in files:
            entry = {"oauth_file": oauth_path, "ok": False}
            p = Path(oauth_path)
            if not p.is_file():
                entry["error"] = "file does not exist"
                results.append(entry)
                failed.append(oauth_path)
                continue
            entry["mtime"] = int(p.stat().st_mtime)
            entry["age_days"] = round((now_unix - entry["mtime"]) / 86400, 2)

            try:
                proc = subprocess.run(
                    [
                        sys.executable, str(REFRESH_SCRIPT),
                        "--oauth-file", oauth_path,
                    ],
                    capture_output=True, text=True,
                    timeout=int(os.environ.get("OAUTH_REFRESH_TIMEOUT_S", "60")),
                )
                if proc.returncode == 0 and proc.stdout.strip():
                    entry["ok"] = True
                    entry["token_len"] = len(proc.stdout.strip())
                else:
                    entry["error"] = (proc.stderr or proc.stdout or "")[-300:]
                    failed.append(oauth_path)
            except subprocess.TimeoutExpired as e:
                entry["error"] = f"timeout after {e.timeout}s"
                failed.append(oauth_path)
            except Exception as e:
                entry["error"] = f"{type(e).__name__}: {e}"
                failed.append(oauth_path)

            results.append(entry)
            self.decide(
                "observation" if entry["ok"] else "warning",
                f"{oauth_path}: {'ok' if entry['ok'] else 'FAILED — ' + (entry.get('error') or '?')[:100]}",
            )

        ok_count = sum(1 for r in results if r.get("ok"))

        # Save artifact
        try:
            self.storage.write_json(
                f"agents/{self.agent_id}/runs/{self.run_ts}/results.json",
                {"results": results, "ok_count": ok_count, "total": len(results)},
            )
        except Exception:
            pass

        # If any failed → operator alert (early warning before SEO agent breaks)
        if failed:
            try:
                from framework.core.resilience import notify_operator
                err = RuntimeError(
                    f"{len(failed)} OAuth refresh_token(s) failed to mint: {failed}"
                )
                notify_operator(
                    agent_id=self.agent_id,
                    error=err,
                    context={
                        "failed_files": failed,
                        "results": results,
                        "recovery": (
                            "Run install/reauth-google-oauth.sh from a graphical "
                            "session to re-issue the refresh_token. The expired "
                            "client_id + client_secret stay valid — only the "
                            "refresh_token needs renewal."
                        ),
                    },
                    severity="high",
                    cooldown_s=86400,  # 1 alert per day max — heartbeat fires daily
                )
            except Exception as e:
                self.decide("warning", f"notify_operator failed: {e}")

        metrics = {
            "oauth_files_watched": float(len(files)),
            "oauth_files_ok": float(ok_count),
            "oauth_files_failed": float(len(failed)),
            "success_pct": round(100.0 * ok_count / max(len(files), 1), 2),
        }

        if failed:
            return RunResult(
                status="failure",
                summary=(
                    f"OAuth heartbeat: {ok_count}/{len(files)} tokens ok, "
                    f"{len(failed)} FAILED — operator alerted"
                ),
                metrics=metrics,
            )

        return RunResult(
            status="success",
            summary=(
                f"OAuth heartbeat: all {ok_count} token(s) minted successfully "
                f"(7-day clock reset)"
            ),
            metrics=metrics,
        )


def main() -> int:
    return 0 if OAuthHeartbeatAgent().run_once().status == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
