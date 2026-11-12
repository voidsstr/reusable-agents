"""framework.core.migration_ledger + the implementer hooks that keep a site's
migrations dir, git and its applied-migrations table in agreement.

2026-11-11: a server started from a live checkout applied 57 untracked
catalog-audit migrations to production. They were untracked because a gate
discarded their runs with `git reset --hard` (which keeps untracked files),
and the gate fired because the run's "new files" set-diff misread a stray
dotfile under en_US collation."""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from framework.core import migration_ledger as ml

_REPO = Path(__file__).resolve().parents[2]


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


def _write(mig, name, age_s, text="select 1;\n"):
    p = mig / name
    p.write_text(text)
    t = time.time() - age_s
    os.utime(p, (t, t))
    return p


CFG = {**ml.DEFAULTS, "enabled": True, "min_age_minutes": 30, "quarantine_after_hours": 24}


def test_plan_sorts_untracked_files():
    applied = {"a_old.sql", "b_young.sql"}
    ages = {"a_old.sql": 7200, "b_young.sql": 60, "c_stranded_old.sql": 3 * 86400,
            "d_stranded_mid.sql": 7200, "e_young.sql": 60}
    p = ml.plan(ages, applied, ages, CFG)
    assert p["commit"] == ["a_old.sql"] and p["young"] == ["b_young.sql", "e_young.sql"]
    assert p["stranded"] == ["c_stranded_old.sql", "d_stranded_mid.sql"] and not p["quarantine"]
    p = ml.plan(ages, applied, ages, {**CFG, "quarantine_stranded": True})
    assert p["quarantine"] == ["c_stranded_old.sql"] and p["stranded"] == ["d_stranded_mid.sql"]
    p = ml.plan(ages, applied, ages, {**CFG, "commit_applied": False, "name_regex": r"^a_|^c_"})
    assert p["applied_uncommitted"] == ["a_old.sql"] and p["commit"] == []
    assert p["skipped"] == ["b_young.sql", "d_stranded_mid.sql", "e_young.sql"]


def test_load_config_layers():
    c = ml.load_config("s", file_cfg={"defaults": {"min_age_minutes": 5},
                                      "by_site": {"s": {"enabled": True}}})
    assert (c["enabled"], c["min_age_minutes"], c["dir"]) == (True, 5, "db/migrations")
    assert ml.load_config("other", file_cfg={"by_site": {"s": {"enabled": True}}})["enabled"] is False


def test_reconcile_commits_applied_files_only(site, tmp_path):
    repo, mig = site
    _write(mig, "20261020T150001Z_catalog-audit-rec002-x.sql", 3 * 86400)   # applied, old
    _write(mig, "20261112T220001Z_catalog-audit-rec001-y.sql", 3 * 86400)   # stranded, old
    _write(mig, "20261112T230000Z_catalog-audit-rec001-z.sql", 60)          # applied, young
    # unrelated work another run has staged must not ride along
    (repo / "notes.md").write_text("wip\n")
    _git(repo, "add", "notes.md")
    applied = {"20261001T000000Z_base.sql", "20261020T150001Z_catalog-audit-rec002-x.sql",
               "20261112T230000Z_catalog-audit-rec001-z.sql"}
    assert ml.untracked_sql(str(repo), mig) == [
        "20261020T150001Z_catalog-audit-rec002-x.sql",
        "20261112T220001Z_catalog-audit-rec001-y.sql",
        "20261112T230000Z_catalog-audit-rec001-z.sql"]
    dry = ml.reconcile(str(repo), "s", applied, CFG, dry_run=True)
    assert dry["plan"]["commit"] == ["20261020T150001Z_catalog-audit-rec002-x.sql"]
    assert len(ml.untracked_sql(str(repo), mig)) == 3  # dry run changed nothing

    out = ml.reconcile(str(repo), "s", applied, {**CFG, "quarantine_dir": str(tmp_path / "q")})
    assert out["errors"] == [] and out["commit_sha"]
    shown = _git(repo, "show", "--name-only", "--format=%s", "HEAD").splitlines()
    assert shown[0].startswith("migrations: record 1 migration(s)")
    assert shown[-1] == "db/migrations/20261020T150001Z_catalog-audit-rec002-x.sql"
    assert "notes.md" in _git(repo, "diff", "--cached", "--name-only")  # still staged, uncommitted
    assert ml.untracked_sql(str(repo), mig) == [
        "20261112T220001Z_catalog-audit-rec001-y.sql",
        "20261112T230000Z_catalog-audit-rec001-z.sql"]
    assert out["plan"]["stranded"] == ["20261112T220001Z_catalog-audit-rec001-y.sql"]
    assert not (tmp_path / "q").exists()  # quarantine is opt-in


def test_reconcile_quarantines_stranded_files_when_enabled(site, tmp_path):
    repo, mig = site
    _write(mig, "20261112T220001Z_catalog-audit-rec001-y.sql", 3 * 86400, "UPDATE p SET x=1;\n")
    q = tmp_path / "quarantine" / "{site}"
    out = ml.reconcile(str(repo), "s", {"20261001T000000Z_base.sql"},
                       {**CFG, "quarantine_stranded": True, "quarantine_dir": str(q)})
    dest = tmp_path / "quarantine" / "s"
    assert out["errors"] == [] and [x["name"] for x in out["quarantined"]] == [
        "20261112T220001Z_catalog-audit-rec001-y.sql"]
    assert not (mig / "20261112T220001Z_catalog-audit-rec001-y.sql").exists()
    assert (dest / "20261112T220001Z_catalog-audit-rec001-y.sql").read_text() == "UPDATE p SET x=1;\n"
    assert "untracked and not applied" in (dest / "MANIFEST.jsonl").read_text()
    assert "commit_sha" not in out


def test_cli_is_off_unless_enabled(monkeypatch, capsys, site):
    from framework.cli import migration_ledger as cli
    monkeypatch.setattr(ml, "load_config", lambda site, **k: dict(ml.DEFAULTS))
    assert cli.main(["--repo", str(site[0]), "--site", "s"]) == 0
    assert "disabled" in capsys.readouterr().out
    monkeypatch.setattr(ml, "load_config", lambda site, **k: {**ml.DEFAULTS, "enabled": True})
    monkeypatch.delenv("NO_SUCH_DSN", raising=False)
    assert cli.main(["--repo", str(site[0]), "--site", "s", "--dsn-env", "NO_SUCH_DSN"]) == 0
    assert "nothing checked" in capsys.readouterr().out
    assert cli.main(["--repo", str(site[0]), "--site", "s", "--dsn-env", "NO_SUCH_DSN",
                     "--strict"]) == 1


def test_cli_prefers_the_sites_own_dsn(monkeypatch, capsys, site):
    from framework.cli import migration_ledger as cli
    monkeypatch.setattr(ml, "load_config", lambda site, **k: {**ml.DEFAULTS, "enabled": True})
    seen = {}

    def applied(dsn, sql, **k):
        seen["dsn"] = dsn
        return {"20261001T000000Z_base.sql"}

    monkeypatch.setattr(ml, "applied_names", applied)
    monkeypatch.setenv("DATABASE_URL", "postgresql://other-site")
    monkeypatch.setenv("DATABASE_URL_MY_SITE", "postgresql://my-site")
    assert cli.main(["--repo", str(site[0]), "--site", "my-site", "--dry-run"]) == 0
    assert seen["dsn"] == "postgresql://my-site"
    monkeypatch.delenv("DATABASE_URL_MY_SITE")
    assert cli.main(["--repo", str(site[0]), "--site", "my-site", "--dry-run"]) == 0
    assert seen["dsn"] == "postgresql://other-site"


def test_reconcile_refuses_an_applied_table_from_another_database(site):
    repo, mig = site
    for i in range(10):
        (mig / ("2026100%dT000000Z_tracked.sql" % i)).write_text("select 1;\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "more")
    _write(mig, "20261020T150001Z_catalog-audit-rec002-x.sql", 3 * 86400)
    cfg = {**CFG, "quarantine_stranded": True, "quarantine_after_hours": 1}
    for applied in (set(), {"some_other_sites_migration.sql"}):
        out = ml.reconcile(str(repo), "s", applied, cfg)
        assert out["errors"] and out["errors"][0].startswith("not reconciling")
        assert "plan" not in out
    assert (mig / "20261020T150001Z_catalog-audit-rec002-x.sql").exists()
    assert ml.applied_table_mismatch(["a.sql", "b.sql"], {"a.sql"}) == ""  # a few missing is fine


# ── implementer run.sh hooks ────────────────────────────────────────────────

def _run_sh_function(name):
    text = (_REPO / "agents" / "implementer" / "run.sh").read_text()
    start = text.index(f"{name}() {{")
    end = text.index("\n}\n", start) + 3
    return text[start:end]


def test_new_dirty_paths_survives_a_dotfile_under_en_us_collation(site, tmp_path):
    """The pre/post set-diff that decides which files an editor created. A
    stray untracked dotfile used to come back as "new", which made the
    orphan gate discard the run and strand its migration."""
    repo, mig = site
    (repo / ".q-tmp.js").write_text("// stray helper\n")
    for i in range(30):
        (mig / ("20261020T%06dZ_catalog-audit-rec%03d-x.sql" % (i, i))).write_text("select 1;\n")
    (repo / "src").mkdir()
    (repo / "src" / "a.ts").write_text("x\n")
    fns = _run_sh_function("_dirty_paths") + _run_sh_function("_new_dirty_paths")
    pre, post = tmp_path / "pre.txt", tmp_path / "post.txt"
    script = (f"set -euo pipefail\n{fns}\ncd {repo}\n_dirty_paths > {pre}\n"
              "echo 'select 2;' > db/migrations/20261112T150004Z_catalog-audit-rec004-new.sql\n"
              f"_dirty_paths > {post}\n_new_dirty_paths {pre} {post}\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("LC_")}
    env["LANG"] = "en_US.UTF-8"
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == ["db/migrations/20261112T150004Z_catalog-audit-rec004-new.sql"]
    assert "not in sorted order" not in r.stderr


def test_run_sh_runs_the_ledger_on_every_exit_before_the_push():
    text = (_REPO / "agents" / "implementer" / "run.sh").read_text()
    # the last EXIT trap is the one in force when the run ends
    trap = [l for l in text.splitlines() if l.startswith("trap ") and l.endswith("EXIT")][-1]
    assert trap.index("_migration_ledger") < trap.index("_push_implementer_repo")
    fn = _run_sh_function("_migration_ledger")
    assert "framework.cli.migration_ledger" in fn and "|| true" in fn
    # the set-diff no longer depends on the host locale
    assert "comm -23 \"$CE_POST_DIRTY\" \"$CE_PRE_DIRTY\"" not in text
    assert text.count("_dirty_paths >") == 2
