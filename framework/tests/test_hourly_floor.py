from framework.core.hourly_floor import evaluate_floor


def _series(per_hour, hours=7 * 24 + 12):
    return [per_hour] * hours


def test_steady_series_is_ok():
    r = evaluate_floor(_series(5))
    assert r["status"] == "ok"
    assert r["window_hours"] == 3
    assert r["current"] == 15 and r["baseline_median"] == 15


def test_step_drop_breaches():
    s = _series(5)
    s[-3:] = [1, 0, 1]
    r = evaluate_floor(s)
    assert r["status"] == "breached"
    assert r["current"] == 2 and r["floor"] == 7.5


def test_baseline_uses_same_clock_window():
    # Busy days (8/h), quiet nights (1/h). The series ends on the third quiet
    # hour: the current window is quiet, and so is the same window on every
    # prior day, so it must not read as a breach against the daytime level.
    day = [8] * 12 + [1] * 12
    s = (day * 9)[: 8 * 24][: 7 * 24 + 15]
    assert s[-3:] == [1, 1, 1]
    r = evaluate_floor(s, max_window_hours=3, min_baseline=3)
    assert r["status"] == "ok" and r["baseline_median"] == 3


def test_sparse_series_widens_then_gives_up():
    r = evaluate_floor([1, 0] * (4 * 24 + 6))  # 12h = 6 < 8
    assert r["status"] == "insufficient-baseline"
    assert r["window_hours"] == 12


def test_sparse_series_widens_to_a_usable_window():
    r = evaluate_floor(_series(2))  # 3h=6 < 8, 6h=12 >= 8
    assert r["status"] == "ok" and r["window_hours"] == 6


def test_short_series():
    assert evaluate_floor([5] * 10)["status"] == "insufficient-data"
