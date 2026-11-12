"""framework.core.migration_ledger: a file edited after the database applied it
is not committed as the record of what production ran.

The applied table (_migrations) stores a name and applied_at, no checksum.
Committing a file's current content as "what production ran" is only true if
the file has not changed since it was applied (review 2026-11-12)."""
from __future__ import annotations

import os
import subprocess
import time
from datetime import datetime, timedelta, timezone

import pytest

from framework.core import migration_ledger as ml


def _git(repo, *args):
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


@pytest.fixture
def site(tmp_path):
    repo = tmp_path / "site"
    mig = repo / "db" / "migrations"
    mig.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (mig / "20261001T000000Z_base.sql").write_text("select 1;\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    return repo, mig


def _write(mig, name, mtime):
    p = mig / name
    p.write_text("update t set x = 1;\n")
    os.utime(p, (mtime, mtime))


CFG = {**ml.DEFAULTS, "enabled": True, "min_age_minutes": 30}


def test_plan_keeps_edited_files_out_of_the_commit():
    ages = {"a.sql": 7200, "b.sql": 7200}
    p = ml.plan(ages, {"a.sql", "b.sql"}, ages, CFG, edited={"b.sql"})
    assert p["commit"] == ["a.sql"] and p["edited_after_apply"] == ["b.sql"]


def test_reconcile_commits_only_files_unchanged_since_they_were_applied(site):
    repo, mig = site
    now = time.time()
    applied_utc = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2)
    _write(mig, "20261111T143000Z_catalog-audit-rec001-a.sql", now - 3 * 3600)   # before apply
    _write(mig, "20261111T143001Z_catalog-audit-rec002-b.sql", now - 3600)       # edited after
    applied = {"20261001T000000Z_base.sql": None,
               "20261111T143000Z_catalog-audit-rec001-a.sql": applied_utc,      # naive = UTC
               "20261111T143001Z_catalog-audit-rec002-b.sql": applied_utc}
    out = ml.reconcile(str(repo), "s", applied, CFG, now=now)
    assert out["plan"]["commit"] == ["20261111T143000Z_catalog-audit-rec001-a.sql"]
    assert out["plan"]["edited_after_apply"] == ["20261111T143001Z_catalog-audit-rec002-b.sql"]
    assert "20261111T143001Z_catalog-audit-rec002-b.sql" in _git(
        repo, "ls-files", "--others", "--", "db/migrations")
    assert "20261111T143000Z_catalog-audit-rec001-a.sql" in _git(repo, "ls-files", "db/migrations")


def test_applied_at_forms_and_skew():
    t0 = datetime(2026, 11, 11, 14, 44, 31)
    aware = t0.replace(tzinfo=timezone.utc)
    m = {"f.sql": aware.timestamp() + 60}
    cfg = {**CFG, "applied_at_tolerance_s": 120}
    for v in (t0, aware, "2026-11-11T14:44:31Z", "2026-11-11 14:44:31"):
        assert ml.edited_after_apply(["f.sql"], {"f.sql": v}, m, cfg) == set()  # within skew
        assert ml.edited_after_apply(["f.sql"], {"f.sql": v}, {"f.sql": m["f.sql"] + 3600},
                                     cfg) == {"f.sql"}
    # no time (a one-column applied_sql) or an unreadable one: nothing is flagged
    assert ml.edited_after_apply(["f.sql"], {"f.sql": None}, m, cfg) == set()
    assert ml.edited_after_apply(["f.sql"], {"f.sql": "yesterday"}, m, cfg) == set()
    # a naive time read in another zone
    ny = {**cfg, "applied_at_tz": "America/New_York"}
    assert ml.edited_after_apply(["f.sql"], {"f.sql": t0}, {"f.sql": aware.timestamp() + 3600},
                                 ny) == set()   # 14:44 EST is 19:44 UTC, after the edit


def test_cli_asks_for_applied_times(monkeypatch, site):
    from framework.cli import migration_ledger as cli
    monkeypatch.setattr(ml, "load_config", lambda site, **k: {**ml.DEFAULTS, "enabled": True})
    seen = {}

    def applied(dsn, sql, **k):
        seen.update(k, sql=sql)
        return {"20261001T000000Z_base.sql": None}

    monkeypatch.setattr(ml, "applied_names", applied)
    monkeypatch.setenv("DATABASE_URL_S", "postgresql://s")
    assert cli.main(["--repo", str(site[0]), "--site", "s", "--dry-run"]) == 0
    assert seen["with_times"] is True and "applied_at" in seen["sql"]
