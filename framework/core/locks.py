"""Cross-process file locks for the framework.

Used by `framework.core.dispatch` to serialize implementer dispatches
*per site* — aisleprompt and specpicks can dispatch in parallel without
sharing the same Anthropic IP-rate-limit bucket, but two dispatches
against the same site queue up.

Backed by `fcntl.flock` so we play nicely with crashed processes (the
kernel releases the lock on process exit, no manual cleanup needed).

Usage:
    with site_dispatch_lock("specpicks"):
        # at most one dispatch at a time for specpicks
        run_implementer(...)

    # or non-blocking:
    lock = site_dispatch_lock("specpicks")
    if not lock.try_acquire():
        return  # another dispatch is running, skip
    try:
        run_implementer(...)
    finally:
        lock.release()
"""
from __future__ import annotations

import errno
import fcntl
import logging
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


_LOCK_DIR = Path(os.path.expanduser(
    os.environ.get("FRAMEWORK_LOCK_DIR", "/tmp/agent-framework-locks")
))
_LOCK_DIR.mkdir(parents=True, exist_ok=True)


class FileLock:
    """fcntl.flock-backed lock with a clear name + waiting semantics.

    Holds a file descriptor; the kernel releases the lock when the fd
    closes, which happens on .release() OR on process exit. Crashes
    don't leave stale locks.
    """

    def __init__(self, name: str, timeout_s: int = 1800):
        self.name = name
        self.timeout_s = timeout_s
        self.path = _LOCK_DIR / f"{name}.lock"
        self._fd: Optional[int] = None

    def acquire(self, *, timeout_s: Optional[int] = None) -> None:
        """Block until the lock is acquired or `timeout_s` elapses.
        Raises TimeoutError if the timeout fires."""
        effective_timeout = timeout_s if timeout_s is not None else self.timeout_s
        deadline = time.monotonic() + effective_timeout
        start = time.monotonic()
        last_heartbeat = start  # first heartbeat fires 60s into wait, not immediately
        if self._fd is None:
            self._fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        while True:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                # Record holder PID for debugging — visible via cat <lockfile>
                try:
                    os.lseek(self._fd, 0, os.SEEK_SET)
                    os.ftruncate(self._fd, 0)
                    os.write(self._fd, f"pid={os.getpid()} acquired={time.time():.0f}\n".encode())
                except Exception:
                    pass
                waited = time.monotonic() - start
                if waited > 5.0:
                    logger.info("lock %s acquired after %.0fs wait (pid=%d)", self.name, waited, os.getpid())
                return
            except OSError as e:
                if e.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                    raise
                now = time.monotonic()
                if now >= deadline:
                    # Surface current holder content for triage
                    holder = ""
                    try:
                        with open(self.path, "rb") as f:
                            holder = f.read(120).decode("utf-8", "replace").strip()
                    except Exception:
                        pass
                    raise TimeoutError(
                        f"could not acquire {self.name} within {effective_timeout}s; current holder: {holder!r}"
                    )
                # Heartbeat every 60s so wedged holders are visible in logs
                if now - last_heartbeat >= 60.0:
                    last_heartbeat = now
                    holder = ""
                    try:
                        with open(self.path, "rb") as f:
                            holder = f.read(120).decode("utf-8", "replace").strip()
                    except Exception:
                        pass
                    logger.info(
                        "lock %s waiting %.0fs (deadline in %.0fs) holder=%r",
                        self.name, now - start, deadline - now, holder,
                    )
                time.sleep(1.0)

    def try_acquire(self) -> bool:
        """Non-blocking acquire. Returns True if got the lock, False otherwise."""
        if self._fd is None:
            self._fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                os.lseek(self._fd, 0, os.SEEK_SET)
                os.ftruncate(self._fd, 0)
                os.write(self._fd, f"pid={os.getpid()} acquired={time.time():.0f}\n".encode())
            except Exception:
                pass
            return True
        except OSError as e:
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return False
            raise

    def release(self) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                try:
                    os.close(self._fd)
                except Exception:
                    pass
                self._fd = None

    def __enter__(self) -> "FileLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.release()


@contextmanager
def responder_drain_lock(*, timeout_s: int = 600, blocking: bool = False):
    """Global lock to prevent two responder processes from draining the
    auto-queue at the same time.

    The responder agent is a single registered .service so systemd
    typically serializes cron triggers. But a manual `/trigger` from
    the dashboard can fire while cron is running — both would LIST
    the same auto-queue keys and dispatch the same recs, resulting
    in double-shipped work + duplicate Anthropic API spend.

    Usage:
        from framework.core.locks import responder_drain_lock
        with responder_drain_lock(blocking=False) as got:
            if not got:
                return 0  # another responder is draining; skip this tick
            drain_auto_queue(cfg)

    blocking=False (default): non-blocking — yields False if the lock
    is held, so the caller can soft-skip. This is the right choice for
    drain ticks; the next tick will catch up.

    blocking=True: waits up to timeout_s for the lock. Use when you
    NEED the drain to happen this call (e.g. operator manually invoked
    a flush).
    """
    lock = FileLock("responder-drain", timeout_s=timeout_s)
    if blocking:
        lock.acquire(timeout_s=timeout_s)
        try:
            yield True
        finally:
            lock.release()
    else:
        got = lock.try_acquire()
        try:
            yield got
        finally:
            if got:
                lock.release()


@contextmanager
def accumulator_lock(agent_id: str, *, timeout_s: int = 120):
    """Per-agent cross-process lock for the rec/proposal accumulator.

    Producer agents (competitor-research, crash-watcher, app-store-
    opportunity, …) accumulate proposed recs across runs at
    `agents/<agent_id>/proposals/active.json` (or .../state/accumulator.json
    for rec_memory). The pattern is load → mutate → save, which is
    NOT atomic — a producer mid-cycle can race with:

      * the responder agent applying an email reply's
        `implement rec-NNN` / `defer rec-NNN` / `skip rec-NNN`
      * the implementer shelling out to
        `framework.cli.mark_shipped_in_accumulator` after a rec ships
      * the backfill CLI re-shaping old runs
      * a second triggered run (cron + manual /trigger overlap)

    Without serialization the LATER `save_active()` overwrites the
    EARLIER state transition — so a rec the user shipped via email
    reply pops back to "open" on the next producer run, and the
    implementer ships it AGAIN.

    Wrap EVERY load_active → mutate → save_active sequence in this
    context. Timeout default is 120s — producer runs hold the lock
    only for the merge step (~1s), and responder/CLI transitions are
    sub-second. Hitting the timeout means something is wedged; surface
    it loudly so the operator can investigate.

    Per-agent lock key: different agents (specpicks-competitor-research
    vs aisleprompt-competitor-research) don't share the file, so they
    don't share the lock either — full parallelism across agents.
    """
    if not agent_id:
        raise ValueError("accumulator_lock requires non-empty agent_id")
    lock = FileLock(f"accumulator-{_safe(agent_id)}", timeout_s=timeout_s)
    lock.acquire()
    try:
        yield lock
    finally:
        lock.release()


@contextmanager
def site_dispatch_lock(site: str, *, timeout_s: int = 1800):
    """Site-keyed dispatch lock — at most one implementer dispatch per
    site at a time. Different sites run in parallel.

    timeout_s defaults to 30 min (long enough that an in-progress
    dispatch typically finishes; short enough to surface true deadlocks).
    """
    if not site:
        site = "_default"
    lock = FileLock(f"dispatch-{_safe(site)}", timeout_s=timeout_s)
    lock.acquire()
    try:
        yield lock
    finally:
        lock.release()


@contextmanager
def ollama_dispatch_lock(
    *,
    timeout_s: int = 1800,
    on_timeout: str = "raise",
):
    """Global ollama serialization lock — at most one ollama-using
    operation (chat AI provider, jcode-ollama edit, aider-ollama edit)
    runs at a time on this host.

    Why: ollama evicts loaded models when a new one arrives AND the new
    model doesn't fit alongside it. Concurrent ollama calls fight over
    VRAM and fail with HTTP 500 / "unable to load model". Serializing
    through this lock makes ollama swap deterministic — load A, use A,
    swap to B, use B.

    Timeout + deadlock posture:
      - Default 30 min (down from 60). Any single ollama call should
        complete inside that even with 70B models — if it doesn't, the
        runner is wedged and we want callers to fall through, not
        block forever.
      - `on_timeout="raise"` (default) → TimeoutError out of the
        context, caller catches + soft-fails to next backend.
      - `on_timeout="proceed"` → log + run the body WITHOUT serial
        guarantees. Useful for chat callers that prefer best-effort
        over hanging.
      - `on_timeout="skip"` → yield None instead of a lock; caller
        checks `if lock is None: <fallback>`.
      - fcntl.flock releases on process exit, so a crashed holder
        never leaves an orphan lock.

    Reentrancy: fcntl.flock is per-FD, not per-process. The same PID
    re-acquiring inside its own context is safe (kernel grants).

    Usage:
        with ollama_dispatch_lock() as lock:
            if lock is None:
                # on_timeout='skip' path — fall through
                return SOFT_FAIL
            ensure_ollama_model_loaded("devstral-small-2:24b")
            run_aider_or_jcode(...)
    """
    lock = FileLock("ollama-dispatch", timeout_s=timeout_s)
    acquired = False
    try:
        try:
            lock.acquire(timeout_s=timeout_s)
            acquired = True
            yield lock
        except TimeoutError:
            mode = (on_timeout or "raise").lower()
            if mode == "raise":
                raise
            if mode == "skip":
                # Caller checks `if lock is None` to fall through.
                yield None
                return
            # mode == "proceed" — run unguarded. Caller still gets the
            # lock object so its preflight calls work, but two callers
            # may overlap.
            yield lock
    finally:
        if acquired:
            try:
                lock.release()
            except Exception:
                pass


def _safe(s: str) -> str:
    """Sanitize a string for use in a filesystem path."""
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in s)
