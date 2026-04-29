#!/usr/bin/env python3
"""Write systemd --user timers/services for every registered agent that
declares cron_expr + entry_command.

Runs on the **host** (not in Docker), so it can actually write to
~/.config/systemd/user/ and run `systemctl --user daemon-reload`.
The framework API container can't do this — its writes go to the
container filesystem, never reaching the host.

Pulls the manifest from the framework API. Auth via FRAMEWORK_API_TOKEN
or via cookie (if you've signed in via OAuth in the same shell).

Usage:
    bash install/write-systemd-timers.py
    bash install/write-systemd-timers.py --agent-id <id>   # one agent
    bash install/write-systemd-timers.py --reload-only     # just daemon-reload

Requires the reusable-agents repo to be on disk at /home/voidsstr/development/
reusable-agents (same path the framework expects). Imports framework.core.scheduler
directly so unit content stays in lockstep with the API.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_DIR))

from framework.core import scheduler  # noqa: E402


def fetch_agents(api_url: str, token: str) -> list[dict]:
    """Fetch every agent's full detail (the list endpoint omits entry_command)."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    list_req = urllib.request.Request(f"{api_url.rstrip('/')}/api/agents", headers=headers)
    try:
        with urllib.request.urlopen(list_req, timeout=10) as r:
            summaries = json.loads(r.read())
    except urllib.error.HTTPError as e:
        sys.exit(f"API error: HTTP {e.code} {e.read().decode('utf-8','ignore')}")
    except Exception as e:
        sys.exit(f"API connect failed: {e}")
    full: list[dict] = []
    for s in summaries:
        try:
            req = urllib.request.Request(
                f"{api_url.rstrip('/')}/api/agents/{s['id']}", headers=headers,
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                full.append(json.loads(r.read()))
        except Exception as e:
            print(f"  warn: detail for {s['id']} failed: {e}", file=sys.stderr)
    return full


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--api-url", default=os.getenv("FRAMEWORK_API_URL", "http://localhost:8093"))
    p.add_argument("--token", default=os.getenv("FRAMEWORK_API_TOKEN", ""))
    p.add_argument("--agent-id", default=None,
                   help="Limit to one agent id (default: all)")
    p.add_argument("--reload-only", action="store_true",
                   help="Skip writing units; just daemon-reload + enable timers")
    args = p.parse_args()

    if not args.token:
        # Last-resort: try .env
        env_path = REPO_DIR / ".env"
        if env_path.is_file():
            for line in env_path.read_text().splitlines():
                if line.startswith("FRAMEWORK_API_TOKEN="):
                    args.token = line.split("=", 1)[1].strip()
                    break

    if args.reload_only:
        scheduler.systemctl_reload()
        print("daemon-reload done")
        return

    agents = fetch_agents(args.api_url, args.token)
    print(f"Got {len(agents)} agents from {args.api_url}")

    written = 0
    skipped = 0
    enabled = 0
    failed: list[str] = []

    # Forward storage env vars to the agent's systemd unit so dispatched
    # subprocesses (e.g. seo-implementer) inherit them.
    storage_env = {
        k: os.environ[k]
        for k in ("AZURE_STORAGE_CONNECTION_STRING", "AZURE_STORAGE_CONTAINER",
                   "STORAGE_BACKEND")
        if os.environ.get(k)
    }
    # Also fall back to .env values if not already in environment
    if not storage_env:
        env_path = REPO_DIR / ".env"
        if env_path.is_file():
            for line in env_path.read_text().splitlines():
                if "=" in line and not line.startswith("#"):
                    k, _, v = line.partition("=")
                    if k in ("AZURE_STORAGE_CONNECTION_STRING", "AZURE_STORAGE_CONTAINER",
                             "STORAGE_BACKEND") and v:
                        storage_env[k] = v.strip()

    for a in agents:
        if args.agent_id and a["id"] != args.agent_id:
            continue
        cron = a.get("cron_expr", "").strip()
        cmd = a.get("entry_command", "").strip()
        if not cron or not cmd:
            skipped += 1
            continue
        try:
            scheduler.write_systemd_units(
                agent_id=a["id"],
                cron_expr=cron,
                entry_command=cmd,
                working_directory=a.get("repo_dir") or os.path.expanduser("~"),
                timezone=a.get("timezone") or "UTC",
                extra_env=storage_env or None,
            )
            written += 1
        except Exception as e:
            failed.append(f"{a['id']}: {e}")

    print(f"Wrote {written} units, skipped {skipped} (no cron)")
    if failed:
        print("Failures:")
        for f in failed:
            print(f"  - {f}")

    # Reload + enable. Also STOP timers for disabled agents — without
    # this step, an agent that was previously enabled keeps running its
    # systemd timer indefinitely after the dashboard toggles it off.
    # (Found in flight: aisleprompt-kitchen-scraper had enabled=false
    # in the manifest but kept firing every 30 min for hours.)
    stopped = 0
    if scheduler.systemctl_reload():
        for a in agents:
            if args.agent_id and a["id"] != args.agent_id:
                continue
            if not a.get("cron_expr"):
                continue
            if a.get("enabled", True):
                if scheduler.systemctl_enable_and_start(a["id"]):
                    enabled += 1
            else:
                # Stop + disable any running timer for this agent
                import subprocess as _sp
                unit = f"agent-{a['id']}.timer"
                try:
                    _sp.run(["systemctl", "--user", "stop", unit],
                            capture_output=True, timeout=10)
                    r = _sp.run(["systemctl", "--user", "disable", unit],
                                capture_output=True, timeout=10)
                    if r.returncode == 0:
                        stopped += 1
                except Exception:
                    pass
        print(f"Enabled + started {enabled} timers · stopped {stopped} disabled-agent timers")


if __name__ == "__main__":
    main()
