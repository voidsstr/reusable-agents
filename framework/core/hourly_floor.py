"""Hourly floor check for a counted series (clicks, sign-ups, orders).

Asks one question: is the last ``window_hours`` of a count far below what the
same clock window held on each of the previous ``baseline_days`` days?

The baseline is the median of the same clock window (for example 13:00-16:00
UTC) on each prior day, so a quiet overnight window is compared with other
overnight windows, not with the afternoon peak. When the series is too sparse
for a 3-hour window to mean anything (baseline median under
``min_baseline``), the window widens in ``window_hours`` steps up to
``max_window_hours``. If even that is too sparse, the check reports
``insufficient-baseline`` and never breaches, because a floor on a series that
averages one event an hour is mostly Poisson noise.

Written for specpicks' verified-click alarm (growth-20261123T104200Z-02):
verified-human Amazon clicks fell about 70% at 06:00Z on 2026-11-20, and the
weekly d7-vs-prev7 watch took three days to surface it. Pure function, no
I/O, so every site can run it on whatever query feeds its own series.
"""
from __future__ import annotations

from statistics import median
from typing import Sequence


def evaluate_floor(
    hourly: Sequence[int],
    *,
    window_hours: int = 3,
    baseline_days: int = 7,
    ratio: float = 0.5,
    min_baseline: float = 8,
    max_window_hours: int = 12,
) -> dict:
    """Evaluate the floor on ``hourly`` (complete hours, oldest first).

    Needs ``baseline_days * 24 + max_window_hours`` points for the widest
    window; with fewer, windows that do not fit are skipped.

    Returns ``{"status": "ok" | "breached" | "insufficient-baseline" |
    "insufficient-data", "window_hours", "current", "baseline_median",
    "floor", "baseline_samples", "ratio_of_baseline"}``.
    """
    counts = [max(0, int(c)) for c in hourly]
    w = max(1, int(window_hours))
    step = w
    last: dict | None = None
    while w <= max(max_window_hours, window_hours):
        if len(counts) < baseline_days * 24 + w:
            break
        current = sum(counts[-w:])
        samples = []
        for d in range(1, baseline_days + 1):
            end = len(counts) - d * 24
            samples.append(sum(counts[end - w:end]))
        base = float(median(samples))
        last = {
            "window_hours": w,
            "current": current,
            "baseline_median": base,
            "floor": round(base * ratio, 2),
            "baseline_samples": samples,
            "ratio_of_baseline": round(current / base, 3) if base > 0 else None,
        }
        if base >= min_baseline:
            last["status"] = "breached" if current < base * ratio else "ok"
            return last
        w += step
    if last is None:
        return {"status": "insufficient-data", "window_hours": window_hours,
                "current": None, "baseline_median": None, "floor": None,
                "baseline_samples": [], "ratio_of_baseline": None}
    last["status"] = "insufficient-baseline"
    return last
