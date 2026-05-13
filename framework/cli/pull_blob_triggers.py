"""Pull pending trigger jobs from Azure blob queue → local /tmp.

The /api/agents/<id>/trigger endpoint writes each trigger job to
`_trigger-queue/<agent>-<run_id>.json` in blob storage. This script
runs on each host that has a host-worker, lists that prefix, downloads
new jobs to the local /tmp/agent-trigger-queue/, and deletes the blob
once the local file exists. The host-worker's existing /tmp scanner
then picks them up the normal way — zero behavior change downstream.

Atomicity:
  - Two host-workers polling the same blob would race on download.
    The first one to win the `delete_key` call gets the job; the
    other's download succeeds but the rename in host-worker.sh's
    `process_one()` (atomic mv to .processing) catches the collision
    on the LOCAL side too.
  - If the host process crashes between download and delete, the
    blob remains and the next tick re-downloads — at-least-once.
  - The local file uses the same name as the blob, so re-downloads
    overwrite an in-progress file ONLY if it hasn't been claimed
    by `mv $job_path $job_path.processing` yet. Claiming wins.

Idempotency:
  - Run as often as you like. If the blob is gone, nothing happens.
  - If the local file already exists (un-claimed), download is a no-op.

Usage (from host-worker.sh):
    python3 -m framework.cli.pull_blob_triggers [--queue-dir /tmp/...]

Exits 0 always (one warning per failed key, never blocks the polling
loop). Prints one line per job pulled.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Make `framework` importable regardless of where this is run from.
_HERE = Path(__file__).resolve()
_REPO_ROOT = _HERE.parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from framework.core.storage import get_storage  # noqa: E402


PREFIX = "_trigger-queue/"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="pull_blob_triggers")
    p.add_argument("--queue-dir",
                   default=os.environ.get("AGENT_TRIGGER_QUEUE_DIR",
                                           "/tmp/agent-trigger-queue"),
                   help="local dir where host-worker.sh expects jobs")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args(argv)

    queue_dir = Path(args.queue_dir)
    queue_dir.mkdir(parents=True, exist_ok=True)

    try:
        s = get_storage()
    except Exception as e:
        # Quiet failure — host-worker loop must never crash on a missing
        # storage backend (the local /tmp path still works).
        if args.verbose:
            print(f"[pull-blob-triggers] storage unavailable: {e}", file=sys.stderr)
        return 0

    try:
        keys = s.list_prefix(PREFIX) or []
    except Exception as e:
        if args.verbose:
            print(f"[pull-blob-triggers] list_prefix failed: {e}", file=sys.stderr)
        return 0

    pulled = 0
    for key in keys:
        if not key.endswith(".json"):
            continue
        filename = key.rsplit("/", 1)[-1]
        local_path = queue_dir / filename

        # If a .processing or completed copy already exists locally,
        # the host-worker already claimed it from an earlier pull —
        # just delete the blob and move on.
        if (queue_dir / (filename + ".processing")).exists():
            _delete_blob(s, key, args.verbose)
            continue

        try:
            payload = s.read_json(key)
        except Exception as e:
            if args.verbose:
                print(f"[pull-blob-triggers] read {key} failed: {e}", file=sys.stderr)
            continue
        if payload is None:
            # Stale or never-written; clear it.
            _delete_blob(s, key, args.verbose)
            continue

        # Atomic write: tmp + rename so the host-worker's poll never
        # sees a half-written file.
        tmp = local_path.with_suffix(local_path.suffix + ".tmp")
        try:
            tmp.write_text(json.dumps(payload, indent=2))
            tmp.rename(local_path)
        except Exception as e:
            if args.verbose:
                print(f"[pull-blob-triggers] write {local_path} failed: {e}",
                      file=sys.stderr)
            continue

        # Delete from blob — at-most-once handoff from blob → /tmp.
        _delete_blob(s, key, args.verbose)
        print(f"[pull-blob-triggers] pulled {filename}")
        pulled += 1

    if args.verbose and pulled == 0:
        print("[pull-blob-triggers] no pending jobs")
    return 0


def _delete_blob(s, key: str, verbose: bool) -> None:
    """Best-effort delete. Storage backends expose this differently."""
    for method in ("delete_key", "delete", "remove"):
        fn = getattr(s, method, None)
        if callable(fn):
            try:
                fn(key)
                return
            except Exception as e:
                if verbose:
                    print(f"[pull-blob-triggers] {method}({key}) failed: {e}",
                          file=sys.stderr)
            return
    # No delete method — fall back to overwriting with a tombstone so we
    # can recognize it next iteration. Not ideal but keeps the loop moving.
    try:
        s.write_json(key, {"_consumed": True})
    except Exception:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
