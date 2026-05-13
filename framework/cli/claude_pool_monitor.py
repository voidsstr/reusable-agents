#!/usr/bin/env python3
"""Claude-pool usage monitor — warns when profiles approach quota.

Each Claude Max profile has a soft session limit per 5h sliding window.
We don't know the exact ceiling Anthropic sets, but empirically pools
hit 5xx / 'too many active' errors after ~50-150 sessions in a window.
This monitor watches the per-profile session counter, computes
sessions-in-last-5h, and warns when any profile crosses thresholds.

Three modes:
  • --once       Report current state and exit. Suitable for cron.
  • --watch      Long-running daemon: poll state.json every minute,
                 emit warnings as state changes.
  • --reset-zombies
                 One-shot: reset in_use counters to 0 on profiles whose
                 last_used_at is > 30 minutes ago. Useful after a
                 worker crash leaves dangling counters.

Output:
  • Plain text to stdout
  • Status JSON written to agents/claude-pool-monitor/state/latest.json
    so the dashboard can surface it (consumes the same shape the
    Knowledge tab understands)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

POOL_STATE = Path.home() / ".reusable-agents/claude-pool/state.json"

# Empirical thresholds — adjust as we learn the actual quota shape.
# total_uses is monotonic since profile-discovered, NOT a 5h window.
# We approximate "recent activity" as total_uses growth since the
# previous monitor tick. A real 5h window would require keeping a
# log of session-start timestamps; that's overkill for a warning.
TOTAL_WARN  = 1000   # total_uses ≥ 1000 → start watching closely
TOTAL_HIGH  = 2000   # ≥ 2000 → high priority warning
GROWTH_BURST = 30    # +30 sessions in one tick → bursty usage warning


def read_pool() -> dict:
    if not POOL_STATE.exists():
        raise SystemExit(f"pool state not found at {POOL_STATE}")
    return json.loads(POOL_STATE.read_text())


def write_pool(d: dict) -> None:
    POOL_STATE.write_text(json.dumps(d, indent=2))


def _fmt_idle(last_used: str, now: datetime) -> str:
    if not last_used:
        return "(never)"
    try:
        dt = datetime.fromisoformat(last_used)
        delta = now - dt
        m = int(delta.total_seconds() / 60)
        if m < 60:  return f"{m}m ago"
        if m < 1440: return f"{m//60}h {m%60}m ago"
        return f"{m//1440}d ago"
    except Exception:
        return "?"


def report(d: dict, *, prev: dict | None = None) -> dict:
    """Print + return a structured summary.

    Returns dict with keys:
      profiles: list of per-profile entries
      warnings: list of warning strings
      summary:  one-line status
    """
    now = datetime.now(timezone.utc)
    profiles = []
    warnings = []
    authed = 0
    for pid, prof in sorted(d.items()):
        total = int(prof.get("total_uses", 0))
        in_use = int(prof.get("in_use", 0))
        last = prof.get("last_used_at", "") or ""
        is_authed = bool(prof.get("authenticated"))
        if is_authed: authed += 1
        prev_total = int((prev or {}).get(pid, {}).get("total_uses", 0))
        delta = max(0, total - prev_total)
        idle = _fmt_idle(last, now)
        zombie = in_use > 0 and last and (now - datetime.fromisoformat(last)
                                          ).total_seconds() > 30 * 60
        line = (f"  {pid:<10} in_use={in_use:<2} total={total:<6} "
                f"authed={is_authed!s:<5} last={idle:<14}"
                f"{' Δ+'+str(delta) if delta else ''}"
                f"{' [ZOMBIE]' if zombie else ''}")
        if not is_authed:
            line += " [NOT AUTHED]"
        print(line, flush=True)
        profile_entry = {
            "id": pid, "in_use": in_use, "total_uses": total,
            "authenticated": is_authed, "last_used_at": last,
            "delta_since_prev": delta, "zombie": zombie,
        }
        profiles.append(profile_entry)

        if zombie:
            warnings.append(f"{pid}: zombie counter (in_use={in_use}, idle {idle}). Run --reset-zombies.")
        if not is_authed:
            warnings.append(f"{pid}: not authenticated. Capacity unused.")
        if total >= TOTAL_HIGH:
            warnings.append(f"{pid}: HIGH usage ({total}). Likely throttled soon.")
        elif total >= TOTAL_WARN:
            warnings.append(f"{pid}: heavy usage ({total}). Watch for rate limits.")
        if delta >= GROWTH_BURST:
            warnings.append(f"{pid}: bursty (+{delta} sessions since last tick).")

    summary = (f"{len(profiles)} profiles | {authed} authed | "
               f"{sum(1 for p in profiles if p['zombie'])} zombies | "
               f"{len(warnings)} warning(s)")

    if warnings:
        print("\nwarnings:", flush=True)
        for w in warnings:
            print(f"  ⚠ {w}", flush=True)
    print(f"\n{summary}", flush=True)
    return {"profiles": profiles, "warnings": warnings, "summary": summary,
            "checked_at": now.isoformat(timespec="seconds")}


def reset_zombies() -> int:
    d = read_pool()
    now = datetime.now(timezone.utc)
    n = 0
    for pid, prof in d.items():
        if int(prof.get("in_use", 0)) <= 0: continue
        last = prof.get("last_used_at", "")
        if not last:
            prof["in_use"] = 0; n += 1; continue
        try:
            dt = datetime.fromisoformat(last)
            if (now - dt).total_seconds() > 30 * 60:
                prof["in_use"] = 0; n += 1
        except Exception:
            prof["in_use"] = 0; n += 1
    if n:
        write_pool(d)
    print(f"reset {n} zombie counter(s)", flush=True)
    return n


def write_dashboard_status(payload: dict) -> None:
    """Write the latest report into framework storage so the dashboard's
    Knowledge tab can surface it under agents/claude-pool-monitor/."""
    try:
        from framework.core.storage import get_storage
        s = get_storage()
        s.write_json("agents/claude-pool-monitor/state/latest.json", payload)
    except Exception as e:
        print(f"  (warn: dashboard write failed: {e})", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--once", action="store_true", default=True)
    g.add_argument("--watch", action="store_true")
    g.add_argument("--reset-zombies", action="store_true",
                   dest="reset_zombies")
    ap.add_argument("--interval", type=int, default=60,
                    help="seconds between watch-mode polls (default 60)")
    ap.add_argument("--no-dashboard", action="store_true",
                    help="skip the framework-storage status write")
    args = ap.parse_args()

    if args.reset_zombies:
        reset_zombies()
        return 0

    d = read_pool()
    if args.watch:
        prev = None
        while True:
            payload = report(d, prev=prev)
            if not args.no_dashboard:
                write_dashboard_status(payload)
            prev = d
            time.sleep(args.interval)
            d = read_pool()
    else:
        payload = report(d)
        if not args.no_dashboard:
            write_dashboard_status(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
