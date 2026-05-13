#!/usr/bin/env python3
"""Auto-queue drainer — long-running daemon.

Architecture intent (2026-05-08): the responder handles email replies
ONLY. Producer agents that want their recs implemented either call
`framework.core.dispatch.dispatch_now()` directly OR write to the
auto-queue (legacy producers, the requeue-deferred + consolidator
scripts). This daemon continuously drains the auto-queue so the
implementer picks up work without waiting for a 15-min cron tick.

Reuses `responder.drain_auto_queue()` so the priority-sort, tier-aware
ordering, and dispatch logic stay in one place. The responder.py
function is also still callable in one-shot mode if anyone needs to
flush manually.

Loop:
  • check auto-queue every `interval_s` seconds (default 15s)
  • when work exists: drain, then check again immediately
  • when idle: sleep `interval_s` seconds; on prolonged idle (>5 min)
    back off to 60s to reduce wakeups

Usage:
  python3 -m framework.cli.auto_queue_drainer [--interval 15] [--once]

  Set RESPONDER_CONFIG to point at the responder config.yaml (default:
  ~/.reusable-agents/responder/config.yaml).
"""
from __future__ import annotations

import argparse
import importlib.util as _ilu
import os
import signal
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _load_responder():
    p = _REPO_ROOT / "agents" / "responder-agent" / "responder.py"
    spec = _ilu.spec_from_file_location("_responder_for_drainer", str(p))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load responder at {p}")
    m = _ilu.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=15,
                    help="seconds between drain ticks when idle (default 15)")
    ap.add_argument("--idle-backoff", type=int, default=60,
                    help="seconds between ticks after prolonged idle (default 60)")
    ap.add_argument("--idle-threshold", type=int, default=5 * 60,
                    help="seconds of idle before backoff kicks in (default 300)")
    ap.add_argument("--once", action="store_true",
                    help="drain once and exit")
    args = ap.parse_args()

    responder = _load_responder()
    cfg_path = Path(os.environ.get(
        "RESPONDER_CONFIG",
        str(Path.home() / ".reusable-agents" / "responder" / "config.yaml"),
    ))
    cfg = responder.load_config(cfg_path)

    if args.once:
        n = responder.drain_auto_queue(cfg)
        print(f"[auto-queue-drainer] one-shot drain dispatched {n}", flush=True)
        return 0

    print(f"[auto-queue-drainer] starting daemon "
          f"(interval={args.interval}s idle_backoff={args.idle_backoff}s)",
          flush=True)

    stop = {"flag": False}
    def _sig(*_):
        stop["flag"] = True
        print("[auto-queue-drainer] received signal, exiting after current drain",
              flush=True)
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    last_work_at = time.monotonic()
    while not stop["flag"]:
        try:
            n = responder.drain_auto_queue(cfg)
        except Exception as e:
            print(f"[auto-queue-drainer] drain raised: {e}", flush=True)
            n = 0
        now = time.monotonic()
        if n:
            print(f"[auto-queue-drainer] dispatched {n} item(s)", flush=True)
            last_work_at = now
            sleep = args.interval        # active — fast cadence
        else:
            idle_for = now - last_work_at
            if idle_for >= args.idle_threshold:
                sleep = args.idle_backoff   # quiet — slow cadence
            else:
                sleep = args.interval
        # Interruptible sleep so SIGTERM doesn't have to wait the full interval
        end = time.monotonic() + sleep
        while time.monotonic() < end and not stop["flag"]:
            time.sleep(min(2.0, end - time.monotonic()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
