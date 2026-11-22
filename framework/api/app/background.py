"""Switches for the framework API's background jobs.

The API runs its housekeeping loops (status snapshot, ghost/batch reapers,
dashboard cache warmers) in every process that serves it. A deployment often
runs two copies: one in the cloud next to the storage account, where reads
are free, and one on the fleet host, where every storage read is billed as
internet egress. On 2026-11-21 the two copies' loops read about 130 GB a day,
roughly $340 a month, while nobody had the dashboard open.

Set ``FRAMEWORK_API_BACKGROUND_JOBS=0`` on the copy that should only answer
requests. Its endpoints still compute on demand behind their caches; the copy
that keeps the loops (default ``1``) keeps the shared snapshot fresh for both.
"""
from __future__ import annotations

import os


def enabled() -> bool:
    """True unless FRAMEWORK_API_BACKGROUND_JOBS is set to 0/false/no/off."""
    v = os.getenv("FRAMEWORK_API_BACKGROUND_JOBS", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def interval(name: str, default: float) -> float:
    """A loop interval in seconds from env ``name``, falling back to default."""
    try:
        return max(1.0, float(os.getenv(name, "") or default))
    except ValueError:
        return default
