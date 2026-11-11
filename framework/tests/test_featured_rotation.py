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
    # clicks desc, then newest, then slug; then the hour rotation fills the rest
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
