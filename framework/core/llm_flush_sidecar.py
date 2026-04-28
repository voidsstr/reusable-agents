"""Sidecar that pushes the tail of a local log file to the framework's
live-LLM-output blob.

Use this from bash agents (e.g. the implementer's run.sh) that write
`claude --print` output directly to a local log file via a shell-level
redirect — they don't run inside Python's LLMStream context, so the
companion `live-llm-output.txt` blob would otherwise never get written.

The sidecar:
  1. Periodically reads the last ~256KB of `--watch <path>`.
  2. Writes it to `agents/<agent_id>/live-llm-output.txt` with the
     same `__META__: {...}` header the Python LLMStream uses, so the
     dashboard endpoint treats it identically.
  3. On SIGTERM/SIGINT (or stdin EOF in `--follow-stdin` mode), does
     one final flush with `is_active=false`.

Usage from run.sh:
  DISPATCH_LOG="/tmp/.../dispatch-implementer-${SITE}-${RUN_TS}.log"
  python3 -m framework.core.llm_flush_sidecar \\
      --agent-id "${RESPONDER_AGENT_ID:-implementer}" \\
      --run-ts   "${RUN_TS}" \\
      --watch    "$DISPATCH_LOG" \\
      --flush-interval 3 &
  SIDECAR_PID=$!
  trap "kill -TERM $SIDECAR_PID 2>/dev/null; wait $SIDECAR_PID 2>/dev/null" EXIT

  # ... existing claude invocation ...

The trap ensures one final flush with is_active=false on script exit.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_tail(path: Path, max_bytes: int) -> str:
    try:
        sz = path.stat().st_size
    except OSError:
        return ""
    try:
        with open(path, "rb") as f:
            if sz > max_bytes:
                f.seek(-max_bytes, 2)
                # Skip a possibly-partial first line.
                f.readline()
            data = f.read()
    except Exception:
        return ""
    return data.decode("utf-8", errors="replace")


def _flush(*, agent_id: str, run_ts: str, started_at: str,
           watch_path: Path, max_bytes: int, is_active: bool) -> None:
    try:
        # Lazy import — keeps the sidecar fast to start in tight loops.
        from framework.core import llm_stream
        from framework.core.storage import get_storage
    except Exception as e:
        print(f"[llm-flush-sidecar] storage import failed: {e}", file=sys.stderr)
        return
    s = get_storage()
    tail = _read_tail(watch_path, max_bytes)
    meta = {
        "agent_id":   agent_id,
        "run_ts":     run_ts,
        "started_at": started_at,
        "updated_at": _now_iso(),
        "is_active":  is_active,
        "source":     "sidecar",
        "watch_path": str(watch_path),
        "tail_bytes": len(tail.encode("utf-8")),
    }
    body = "__META__: " + json.dumps(meta) + "\n" + tail
    try:
        s.write_text(
            llm_stream.live_llm_tail_key(agent_id), body,
            cache_control="public, max-age=2",
        )
    except Exception as e:
        # Best-effort — never disrupt the agent run if blob is unreachable.
        print(f"[llm-flush-sidecar] write failed: {e}", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--agent-id", required=True,
                    help="agent_id whose live-tail blob to update")
    ap.add_argument("--run-ts", default="",
                    help="run timestamp (defaults to now in UTC, "
                         "format YYYYMMDDTHHMMSSZ)")
    ap.add_argument("--watch", required=True, type=Path,
                    help="local log path to tail")
    ap.add_argument("--flush-interval", type=float, default=3.0,
                    help="seconds between blob writes (default 3.0)")
    ap.add_argument("--max-bytes", type=int, default=262_144,
                    help="cap for the live-tail blob (default 256 KB)")
    ns = ap.parse_args()

    run_ts = ns.run_ts or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    started_at = _now_iso()

    # Quiesce flag set by signal handler — final flush + exit.
    stopping = {"v": False}

    def _on_signal(signum, frame):  # noqa: ARG001
        stopping["v"] = True

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    # Wait for the watched file to appear (up to 30s; the agent script
    # may not have started writing yet when the sidecar launches).
    deadline = time.time() + 30
    while not ns.watch.exists() and time.time() < deadline and not stopping["v"]:
        time.sleep(0.5)

    # Initial flush so the dashboard has SOMETHING immediately even if
    # the file is still empty.
    _flush(agent_id=ns.agent_id, run_ts=run_ts, started_at=started_at,
           watch_path=ns.watch, max_bytes=ns.max_bytes, is_active=True)

    try:
        while not stopping["v"]:
            # Sleep first; we already did an initial flush above.
            slept = 0.0
            while slept < ns.flush_interval and not stopping["v"]:
                time.sleep(0.25)
                slept += 0.25
            if stopping["v"]:
                break
            _flush(agent_id=ns.agent_id, run_ts=run_ts, started_at=started_at,
                   watch_path=ns.watch, max_bytes=ns.max_bytes, is_active=True)
    finally:
        # Final flush — flip is_active=False so the dashboard knows
        # the run ended.
        _flush(agent_id=ns.agent_id, run_ts=run_ts, started_at=started_at,
               watch_path=ns.watch, max_bytes=ns.max_bytes, is_active=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
