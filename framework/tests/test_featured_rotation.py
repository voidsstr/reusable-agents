"""Tests for framework.core.featured_rotation (plan A02 + A15)."""
import datetime as dt

from framework.core import featured_rotation as fr

IDLE_DAY = dt.date(2026, 11, 11)   # tech: nothing NOW/IMMINENT (Thanksgiving 15d out)
BF_DAY = dt.date(2026, 11, 13)     # tech: black-friday + thanksgiving IMMINENT


def _art(slug, tags=(), featured=False, status="published", day=1):
    return {"id": abs(hash(slug)) % 10 ** 6, "slug": slug, "tags": list(tags),
            "featured": featured, "status": status,
            "published_at": dt.datetime(2026, 11, day, tzinfo=dt.timezone.utc)}


class _Cur:
    def __init__(self, conn):
        self.conn = conn
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.conn.queries.append((sql, params))
        s = sql.lstrip()
        if s.startswith("SELECT id, slug"):
            self.rows = [dict(a) for a in self.conn.articles if a["status"] == "published"]
        elif "WITH w AS (" in sql:
            self.rows = list(self.conn.click_rows)
        elif s.startswith("UPDATE"):
            flag = "= TRUE" in sql
            for a in self.conn.articles:
                if a["slug"] in params[0] and (not flag or a["status"] == "published"):
                    a["featured"] = flag
            self.conn.updates.append((flag, sorted(params[0])))
            self.rows = []
        else:
            raise AssertionError(sql)

    def fetchall(self):
        return self.rows


class _Conn:
    def __init__(self, articles, click_rows=()):
        self.articles = articles
        self.click_rows = list(click_rows)
        self.queries, self.updates = [], []
        self.commits = self.rollbacks = 0
        self.closed = False

    def cursor(self, **kw):
        return _Cur(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


def _run(conn, **kw):
    kw.setdefault("audience", "tech")
    return fr.cycle_homepage_features("unused-dsn", conn=conn, **kw)


def test_stale_holiday_features_demoted_on_an_idle_day():
    arts = [_art("halloween-a", ["holiday:halloween"], featured=True),
            _art("pinned-guide", ["gpu"], featured=True),
            _art("odd-tag", ["holiday:none"], featured=True)]
    conn = _Conn(arts)
    stats = _run(conn, today=IDLE_DAY)
    assert stats["active_occasions"] == []
    assert sorted(stats["demoted"]) == ["halloween-a", "odd-tag"]
    assert stats["untouched_pinned"] == 1 and stats["promoted"] == []
    assert {a["slug"]: a["featured"] for a in arts} == {
        "halloween-a": False, "pinned-guide": True, "odd-tag": False}
    assert conn.commits == 1 and not conn.closed      # caller's connection stays open


def test_demote_when_idle_off_restores_the_early_return():
    conn = _Conn([_art("halloween-a", ["holiday:halloween"], featured=True)])
    stats = _run(conn, today=IDLE_DAY, demote_when_idle=False)
    assert conn.queries == [] and stats["demoted"] == []


def test_dry_run_reports_without_writing():
    arts = [_art("halloween-a", ["holiday:halloween"], featured=True)]
    conn = _Conn(arts)
    stats = _run(conn, today=IDLE_DAY, dry_run=True)
    assert stats["demoted"] == ["halloween-a"] and conn.updates == [] and arts[0]["featured"]


def test_alias_tags_are_candidates_for_the_calendar_occasion():
    arts = [_art(f"sept-bf-{i}", ["holiday:black-friday"], day=1) for i in range(7)]
    arts += [_art("nov-bf-gpu", ["holiday:black-friday-cyber-monday"], day=8),
             _art("nov-bf-prebuilt", ["deals", "holiday:black-friday-cyber-monday"], day=9),
             _art("nov-bf-mini", ["holiday:bfcm"], day=10)]
    stats = _run(_Conn(arts), today=BF_DAY, dry_run=True)
    assert stats["active_occasions"] == ["black-friday", "thanksgiving"]
    assert stats["candidates_by_occasion"] == {"thanksgiving": 0, "black-friday": 10}
    assert len(stats["promoted"]) == 6


def test_scores_feature_what_sells_first():
    arts = [_art(f"a-sept-{i}", ["holiday:black-friday"], day=1) for i in range(7)]
    arts += [_art("z-nov-prebuilt", ["holiday:black-friday-cyber-monday"], day=9),
             _art("y-nov-gpu", ["holiday:black-friday-cyber-monday"], day=8)]
    scores = {"z-nov-prebuilt": 7, "a-sept-3": 2, "y-nov-gpu": 2, "a-sept-0": 0}
    stats = _run(_Conn(arts), today=BF_DAY, max_features=4, scores=scores, dry_run=True)
    # clicks desc, then newest, then slug; zero-click candidates fill the rest
    assert stats["promoted"][:3] == ["z-nov-prebuilt", "y-nov-gpu", "a-sept-3"]
    assert len(stats["promoted"]) == 4 and stats["ranked_by"] == "verified clicks"
    assert stats["scores_of_picks"]["z-nov-prebuilt"] == 7.0


def test_without_scores_order_is_the_old_slug_rotation():
    arts = [_art(s, ["holiday:black-friday"]) for s in ("c", "a", "b")]
    stats = _run(_Conn(arts), today=BF_DAY, max_features=2, dry_run=True)
    cur = stats["rotate_hour_cursor"]
    order = ["a", "b", "c"]
    assert stats["promoted"] == [order[cur % 3], order[(cur + 1) % 3]]
    assert "ranked_by" not in stats


def test_score_source_reads_verified_clicks(storage):
    arts = [_art("a-bf", ["holiday:black-friday"]), _art("b-bf", ["holiday:black-friday"]),
            _art("c-bf", ["holiday:black-friday"])]
    rows = [("human", "/reviews/c-bf", 5), ("site-flagged-bot", "/reviews/a-bf", 99),
            ("human", "/product/B0X", 3)]
    src = {"table": "outbound_clicks", "group_col": "source_page",
           "spec": {"time_col": "clicked_at", "referer_col": "source_page"},
           "match": {"target": "amazon", "via": "beacon"},
           "window_days": 30, "path_prefixes": ["/reviews/"]}
    conn = _Conn(arts, click_rows=rows)
    stats = _run(conn, today=BF_DAY, max_features=1, score_source=src,
                 storage=storage, profile="site-a-article-proposal-agent", dry_run=True)
    assert stats["promoted"] == ["c-bf"] and stats["scores"].startswith("ok: 1")
    sql, params = next(q for q in conn.queries if "WITH w AS (" in q[0])
    assert "FROM outbound_clicks" in sql and params[-3:] == [30, "amazon", "beacon"]


def test_score_source_failure_falls_back_to_rotation():
    arts = [_art("a-bf", ["holiday:black-friday"])]
    stats = _run(_Conn(arts), today=BF_DAY, score_source={"table": None}, dry_run=True)
    assert stats["scores"].startswith("unavailable") and stats["promoted"] == ["a-bf"]


def test_evergreen_fallback_fills_and_later_releases_its_own_picks(storage):
    arts = [_art("strix-review", ["mini-pc"]), _art("monitors-guide", ["monitors"]),
            _art("pinned-guide", ["gpu"], featured=True), _art("quiet", ["ai"])]
    eg = {"enabled": True, "max_features": 3}
    prof = "site-a-article-proposal-agent"
    scores = {"strix-review": 16, "monitors-guide": 11, "pinned-guide": 9}
    conn = _Conn(arts)
    stats = _run(conn, today=IDLE_DAY, scores=scores, evergreen_fallback=eg,
                 storage=storage, profile=prof)
    # Three slots: two promoted, the operator pin counts but is never claimed.
    assert stats["evergreen"] == ["strix-review", "monitors-guide", "pinned-guide"]
    assert stats["promoted"] == ["strix-review", "monitors-guide"]
    assert storage.read_json(fr.STATE_KEY_TMPL.format(profile=prof))["evergreen"] == \
        ["strix-review", "monitors-guide"]
    # A day later the monitors guide stops selling: released; the pin stays.
    stats = _run(conn, today=IDLE_DAY, scores={"strix-review": 16, "pinned-guide": 9},
                 evergreen_fallback=eg, storage=storage, profile=prof)
    assert stats["demoted"] == ["monitors-guide"] and stats["promoted"] == []
    assert {a["slug"]: a["featured"] for a in arts} == {
        "strix-review": True, "monitors-guide": False, "pinned-guide": True, "quiet": False}
    assert storage.read_json(fr.STATE_KEY_TMPL.format(profile=prof))["evergreen"] == \
        ["strix-review"]


def test_holiday_candidates_take_slots_before_evergreen(storage):
    arts = [_art("bf-1", ["holiday:black-friday"]), _art("bf-2", ["holiday:bfcm"]),
            _art("strix-review", ["mini-pc"]), _art("monitors-guide", ["monitors"])]
    stats = _run(_Conn(arts), today=BF_DAY, max_features=3,
                 scores={"bf-1": 1, "strix-review": 16, "monitors-guide": 11},
                 evergreen_fallback={"enabled": True}, storage=storage,
                 profile="p", dry_run=True)
    assert stats["promoted"][:2] == ["bf-1", "bf-2"]
    assert stats["evergreen"] == ["strix-review"]


def test_evergreen_needs_scores_and_storage(storage):
    arts = [_art("strix-review", ["mini-pc"])]
    stats = _run(_Conn(arts), today=IDLE_DAY, evergreen_fallback={"enabled": True})
    assert stats["evergreen"].startswith("skipped") and stats["promoted"] == []
    stats = _run(_Conn(arts), today=IDLE_DAY, scores={"strix-review": 3},
                 evergreen_fallback={"enabled": True})
    assert stats["evergreen"].startswith("skipped: needs storage")


def test_cycle_from_config_passes_site_yaml_keys():
    arts = [_art("halloween-a", ["holiday:halloween"], featured=True)]
    assert fr.cycle_from_config("dsn", {"enabled": False}, audience="tech") is None
    assert fr.cycle_from_config("dsn", None, audience="tech") is None
    conn = _Conn(arts)
    stats = fr.cycle_from_config("dsn", {"enabled": True, "max_features": "6",
                                         "demote_when_idle": False},
                                 audience="tech", today=IDLE_DAY, conn=conn)
    assert stats["demoted"] == [] and conn.queries == []
    stats = fr.cycle_from_config("dsn", {"enabled": True, "dry_run": True},
                                 audience="tech", today=IDLE_DAY, conn=_Conn(arts))
    assert stats["dry_run"] is True and stats["demoted"] == ["halloween-a"]


def test_holiday_ids_from_tags_keeps_unknown_ids():
    from framework.core.seasonal_calendar import canonicalizer
    c = canonicalizer("tech", warn=False)
    assert fr.holiday_ids_from_tags(["holiday:bfcm", "holiday:none", "gpu", None], c) == \
        {"black-friday", "none"}
    assert fr.holiday_ids_from_tags(["holiday:bfcm"]) == {"bfcm"}


def test_unsafe_identifiers_are_refused():
    import pytest
    with pytest.raises(ValueError):
        _run(_Conn([]), today=BF_DAY, articles_table="x; drop table y")


# ── Review fixes: recency tail, evergreen ownership (2026-11-11) ──

class _FlakyStorage:
    """Wraps a storage; the first `fail_writes` write_json calls raise."""

    def __init__(self, inner, fail_writes=1):
        self.inner, self.fail_writes, self.writes = inner, fail_writes, 0

    def read_json(self, key):
        return self.inner.read_json(key)

    def write_json(self, key, obj, **kw):
        self.writes += 1
        if self.writes <= self.fail_writes:
            raise OSError("blob write failed")
        return self.inner.write_json(key, obj, **kw)


def _state(storage, prof):
    doc = storage.read_json(fr.STATE_KEY_TMPL.format(profile=prof))
    return None if doc is None else doc["evergreen"]


def _flags(arts):
    return {a["slug"]: a["featured"] for a in arts}


def test_zero_click_candidates_follow_recency_not_the_hour_cursor(storage):
    arts = [_art("a-old", ["holiday:black-friday"], day=1),
            _art("b-new", ["holiday:black-friday"], day=10),
            _art("c-mid", ["holiday:bfcm"], day=5),
            _art("d-sells", ["holiday:black-friday"], day=2),
            _art("e-one-click", ["holiday:black-friday"], day=3)]
    scores = {"d-sells": 5, "e-one-click": 1}
    for hours in (1, 5, 7):                       # different hour cursors
        stats = _run(_Conn(arts), today=BF_DAY, max_features=4, scores=scores,
                     rotate_every_hours=hours, dry_run=True)
        assert stats["promoted"] == ["d-sells", "e-one-click", "b-new", "c-mid"]
    # Below score_source.min_score a click count ranks as zero (by recency).
    src = {"table": "outbound_clicks", "group_col": "source_page",
           "spec": {"time_col": "clicked_at", "referer_col": "source_page"},
           "match": {"target": "amazon"}, "path_prefixes": ["/reviews/"], "min_score": 2}
    rows = [("human", "/reviews/d-sells", 5), ("human", "/reviews/e-one-click", 1)]
    stats = _run(_Conn(arts, click_rows=rows), today=BF_DAY, max_features=4,
                 score_source=src, storage=storage, profile="p", dry_run=True)
    assert stats["promoted"] == ["d-sells", "b-new", "c-mid", "e-one-click"]


def test_evergreen_claim_failure_keeps_new_picks_down(storage):
    # Reviewer scenario: the state write fails. Before, the picks went up
    # unrecorded and were pinned for good; now they are not promoted.
    prof = "site-a-article-proposal-agent"
    arts = [_art("strix-review", ["mini-pc"]), _art("monitors-guide", ["monitors"]),
            _art("pinned-guide", ["gpu"], featured=True),
            _art("halloween-a", ["holiday:halloween"], featured=True)]
    conn = _Conn(arts)
    eg = {"enabled": True, "max_features": 3}
    scores = {"strix-review": 16, "monitors-guide": 11}
    stats = _run(conn, today=IDLE_DAY, scores=scores, evergreen_fallback=eg,
                 storage=_FlakyStorage(storage), profile=prof)
    assert stats["promoted"] == [] and stats["evergreen_state"].startswith("claim not saved")
    assert stats["demoted"] == ["halloween-a"]          # holiday rotation still ran
    assert _flags(arts) == {"strix-review": False, "monitors-guide": False,
                            "pinned-guide": True, "halloween-a": False}
    assert _state(storage, prof) is None
    # Next run, the picks are no longer winners: nothing is stuck featured.
    stats = _run(conn, today=IDLE_DAY, scores={"pinned-guide": 2},
                 evergreen_fallback=eg, storage=storage, profile=prof)
    assert stats["demoted"] == [] and stats["untouched_pinned"] == 1
    # With storage healthy the claim is saved before the picks go up.
    stats = _run(conn, today=IDLE_DAY, scores=scores, evergreen_fallback=eg,
                 storage=storage, profile=prof)
    assert stats["promoted"] == ["strix-review", "monitors-guide"]
    assert _state(storage, prof) == ["strix-review", "monitors-guide"]


def test_evergreen_trim_failure_is_pruned_next_run(storage):
    prof = "p"
    arts = [_art("strix-review", ["mini-pc"]), _art("monitors-guide", ["monitors"])]
    conn = _Conn(arts)
    eg = {"enabled": True}
    _run(conn, today=IDLE_DAY, scores={"strix-review": 16, "monitors-guide": 11},
         evergreen_fallback=eg, storage=storage, profile=prof)
    assert _state(storage, prof) == ["strix-review", "monitors-guide"]
    # monitors-guide loses its slot; the trim write fails: a superset remains.
    stats = _run(conn, today=IDLE_DAY, scores={"strix-review": 16},
                 evergreen_fallback=eg, storage=_FlakyStorage(storage), profile=prof)
    assert stats["demoted"] == ["monitors-guide"]
    assert stats["evergreen_state"].startswith("state not trimmed")
    assert _state(storage, prof) == ["strix-review", "monitors-guide"]
    # The released slug is no longer featured, so the next run drops it.
    stats = _run(conn, today=IDLE_DAY, scores={"strix-review": 16},
                 evergreen_fallback=eg, storage=storage, profile=prof)
    assert stats["demoted"] == [] and stats["promoted"] == []
    assert _state(storage, prof) == ["strix-review"]


def test_disabling_evergreen_releases_its_own_picks_only(storage):
    prof = "p"
    for idle_kw in ({}, {"demote_when_idle": False}):
        storage.write_json(fr.STATE_KEY_TMPL.format(profile=prof), {"evergreen": []})
        arts = [_art("strix-review", ["mini-pc"]), _art("monitors-guide", ["monitors"]),
                _art("pinned-guide", ["gpu"], featured=True)]
        conn = _Conn(arts)
        _run(conn, today=IDLE_DAY, scores={"strix-review": 16, "monitors-guide": 11},
             evergreen_fallback={"enabled": True}, storage=storage, profile=prof)
        assert _state(storage, prof) == ["strix-review", "monitors-guide"]
        stats = _run(conn, today=IDLE_DAY, evergreen_fallback={"enabled": False},
                     storage=storage, profile=prof, **idle_kw)
        assert sorted(stats["demoted"]) == ["monitors-guide", "strix-review"]
        assert stats["evergreen"].startswith("off: releasing 2")
        assert stats["untouched_pinned"] == 1
        assert _flags(arts) == {"strix-review": False, "monitors-guide": False,
                                "pinned-guide": True}
        assert _state(storage, prof) == []
        # Nothing left to release: back to the cheap early return when idle.
        conn.queries.clear()
        _run(conn, today=IDLE_DAY, storage=storage, profile=prof, demote_when_idle=False)
        assert conn.queries == []


def test_unreadable_scores_hold_evergreen_picks(storage):
    prof = "p"
    arts = [_art("strix-review", ["mini-pc"]), _art("pinned-guide", ["gpu"], featured=True)]
    conn = _Conn(arts)
    eg = {"enabled": True}
    _run(conn, today=IDLE_DAY, scores={"strix-review": 16}, evergreen_fallback=eg,
         storage=storage, profile=prof)
    assert _state(storage, prof) == ["strix-review"]
    # The score source fails: hold, neither promote nor release.
    stats = _run(conn, today=IDLE_DAY, score_source={"table": None},
                 evergreen_fallback=eg, storage=storage, profile=prof)
    assert stats["scores"].startswith("unavailable")
    assert stats["evergreen"].startswith("held") and stats["demoted"] == []
    assert stats["untouched_pinned"] == 1 and _state(storage, prof) == ["strix-review"]
    assert _flags(arts)["strix-review"] is True
    # No score source configured at all: it can never run, so it releases.
    stats = _run(conn, today=IDLE_DAY, evergreen_fallback=eg, storage=storage, profile=prof)
    assert stats["evergreen"] == "skipped: no verified-click scores"
    assert stats["demoted"] == ["strix-review"] and _state(storage, prof) == []


def test_demote_when_idle_off_with_evergreen_keeps_stale_holiday_features(storage):
    arts = [_art("halloween-a", ["holiday:halloween"], featured=True),
            _art("strix-review", ["mini-pc"])]
    conn = _Conn(arts)
    stats = _run(conn, today=IDLE_DAY, demote_when_idle=False, scores={"strix-review": 5},
                 evergreen_fallback={"enabled": True}, storage=storage, profile="p")
    assert stats["demoted"] == [] and stats["promoted"] == ["strix-review"]
    assert _flags(arts) == {"halloween-a": True, "strix-review": True}


def test_evergreen_state_is_written_only_when_ownership_changes(storage):
    prof = "p"
    arts = [_art("strix-review", ["mini-pc"]), _art("monitors-guide", ["monitors"]),
            _art("desk-guide", ["desks"])]
    conn = _Conn(arts)
    eg = {"enabled": True, "max_features": 3}
    counting = _FlakyStorage(storage, fail_writes=0)
    _run(conn, today=IDLE_DAY, scores={"strix-review": 16}, evergreen_fallback=eg,
         storage=counting, profile=prof)
    assert counting.writes == 1                       # the claim, no trim
    # A new pick ranked first: one claim write; the order change alone is no write.
    _run(conn, today=IDLE_DAY, scores={"strix-review": 16, "monitors-guide": 20},
         evergreen_fallback=eg, storage=counting, profile=prof)
    assert counting.writes == 2
    assert sorted(_state(storage, prof)) == ["monitors-guide", "strix-review"]
    _run(conn, today=IDLE_DAY, scores={"strix-review": 30, "monitors-guide": 20},
         evergreen_fallback=eg, storage=counting, profile=prof)
    assert counting.writes == 2
    # A consumer that passes storage but never enables evergreen never writes.
    _run(_Conn([_art("halloween-a", ["holiday:halloween"], featured=True)]),
         today=IDLE_DAY, storage=counting, profile="other-agent")
    assert counting.writes == 2
