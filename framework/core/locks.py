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
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional


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
        deadline = time.monotonic() + (timeout_s if timeout_s is not None else self.timeout_s)
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
                return
            except OSError as e:
                if e.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"could not acquire {self.name} within {self.timeout_s}s")
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


def _safe(s: str) -> str:
    """Sanitize a string for use in a filesystem path."""
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in s)
