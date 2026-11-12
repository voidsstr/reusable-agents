"""framework/cli/article_link_guard.py `check` — the pre-INSERT self-check the
article author runs (ARTICLE_AUTHOR.md). The Opus author INSERTs its own rows,
so the wrapper's INSERT-time guard never saw the seven guides that shipped
with zero buy links (2026-11-12 review); this is the same contract, run by the
author before its INSERT. And `sweep`, run by run.sh after every article-author
/ article-refresh dispatch, with its deterministic repairs. No network, no
real DB: repo default config only; the DB paths run against a fake."""
from __future__ import annotations

import json

import pytest

from framework.cli import article_link_guard as cli


@pytest.fixture(autouse=True)
def _no_session_db(monkeypatch):
    """`check` reads $DATABASE_URL for its catalog check; keep every test off
    a real database unless it installs the fake below."""
    monkeypatch.delenv("DATABASE_URL", raising=False)

PRODUCT_PAGES_ONLY = (
    "# Gifts for Bakers 2026\n\n"
    + " ".join(f"Try [recipe {i}](/recipes/cookie-{i}-{100 + i})." for i in range(6))
    + " Our pick is the [OXO scale](/kitchen/oxo-good-grips-11-pound-scale) and the "
      "[Nordic Ware sheet](/kitchen/nordic-ware-naturals-half-sheet).\n"
)


def _run_dir(tmp_path, proposal, agent_id="aisleprompt-article-proposal-agent"):
    (tmp_path / "recommendations.json").write_text(json.dumps({"recommendations": [
        {"id": "art-001", "agent_id": agent_id, "proposal": proposal}]}))
    return tmp_path


def test_check_fails_a_gift_guide_without_buy_links_and_prints_the_fix(tmp_path, capsys):
    prop = {"slug": "gifts-for-bakers", "title": "Gifts for Bakers 2026", "bucket": "seasonal-occasion",
            "expected_kitchen_product_slugs": ["oxo-good-grips-11-pound-scale",
                                               "nordic-ware-naturals-half-sheet"]}
    rd = _run_dir(tmp_path, prop)
    body = tmp_path / "gifts-for-bakers.body.md"
    body.write_text(PRODUCT_PAGES_ONLY)
    rc = cli.main(["check", "--run-dir", str(rd), "--rec-id", "art-001", "--body", str(body),
                   "--no-storage"])
    out = capsys.readouterr().out
    assert rc == 1
    assert out.startswith("FAIL only 0 direct buy links (min 2)")
    assert "0/2 required direct buy links" in out
    assert "DIRECT BUY LINKS — at least 2 distinct" in out
    assert "/k/oxo-good-grips-11-pound-scale?source=amazon" in out

    body.write_text(PRODUCT_PAGES_ONLY
                    + "[Check price](https://aisleprompt.com/k/oxo-good-grips-11-pound-scale?source=amazon) "
                      "[Check price](/k/nordic-ware-naturals-half-sheet?source=amazon)\n")
    rc = cli.main(["check", "--run-dir", str(rd), "--rec-id", "art-001", "--body", str(body),
                   "--no-storage"])
    assert rc == 0
    assert capsys.readouterr().out.startswith("PASS ")


def test_check_without_a_monetized_bucket_or_title_needs_no_buy_link(tmp_path, capsys):
    # Updated 2026-11-12 (re-review): still no buy link for a recipe-cluster
    # body that names no product; but a body naming products through their
    # /kitchen/ pages now needs buy links whatever its bucket or title (757
    # and 763 passed the first version of the gate with three product pages
    # and no buy link). The first expectation is kept, the second is new.
    body = tmp_path / "b.md"
    body.write_text(PRODUCT_PAGES_ONLY.split(" Our pick")[0]
                    + " Browse [bakeware](/kitchen/category/bakeware) and [utensils](/kitchen/category/utensils).\n")
    rc = cli.main(["check", "--site", "aisleprompt", "--bucket", "recipe-cluster",
                   "--title", "Diwali Recipes: 20 Sweets", "--body", str(body), "--no-storage"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert out.startswith("PASS ") and "buy=0/0" in out
    body.write_text(PRODUCT_PAGES_ONLY)
    rc = cli.main(["check", "--site", "aisleprompt", "--bucket", "recipe-cluster",
                   "--title", "Diwali Recipes: 20 Sweets", "--body", str(body), "--no-storage"])
    out = capsys.readouterr().out
    assert rc == 1, out
    assert out.startswith("FAIL only 0 direct buy links (min 2)")
    assert "names 2 product(s) through their product pages" in out
    assert "/k/oxo-good-grips-11-pound-scale" not in out    # no proposal: no slugs to offer


def test_check_bad_input_is_exit_2(tmp_path, capsys):
    rd = _run_dir(tmp_path, {"slug": "x"})
    assert cli.main(["check", "--run-dir", str(rd), "--rec-id", "art-404",
                     "--body", str(tmp_path / "missing.md"), "--no-storage"]) == 2
    assert cli.main(["check", "--site", "aisleprompt", "--body", str(tmp_path / "missing.md"),
                     "--no-storage"]) == 2
    assert "cannot read" in capsys.readouterr().err


# ── brackets, category-only proposals, refresh rows, sweep repairs ────────────

RECIPES = (" ".join(f"Try [recipe {i}](/recipes/cookie-{i}-{100 + i})." for i in range(6))
           + " Browse [bakeware](/kitchen/category/bakeware) and [utensils](/kitchen/category/utensils).")


def test_check_fails_link_text_holding_brackets(tmp_path, capsys):
    body = tmp_path / "b.md"
    body.write_text(RECIPES + " Source: [Black Forest Labs — FLUX.2 [klein] 4B model card, Hugging Face]"
                              "(https://huggingface.co/black-forest-labs/FLUX.2-klein-4B).\n")
    rc = cli.main(["check", "--site", "aisleprompt", "--bucket", "recipe-cluster", "--title", "Diwali",
                   "--body", str(body), "--no-storage"])
    out = capsys.readouterr().out
    assert rc == 1
    assert out.startswith("FAIL link text holds brackets (1 link(s))")
    assert "FLUX.2 (klein) 4B" in out and "[Black Forest Labs — FLUX.2 [klein] 4B" in out


def test_check_never_counts_or_offers_a_category_buy_link(tmp_path, capsys):
    # The reviewer's reproduction: a categories-only checklist proposal. The
    # old directive printed "[Check price](/k/roasting-pans?source=amazon)" and
    # a body with two such links then passed buy=2/2.
    prop = {"slug": "thanksgiving-kitchen-gear-checklist", "bucket": "seasonal-occasion",
            "title": "Thanksgiving Kitchen Gear Checklist: The Tools for Turkey, Sides and Pie",
            "expected_kitchen_category_slugs": ["roasting-pans", "meat-thermometers"]}
    rd = _run_dir(tmp_path, prop)
    body = tmp_path / "b.md"
    body.write_text(RECIPES + " Browse [roasting pans](/kitchen/category/roasting-pans) and "
                              "[thermometers](/kitchen/category/meat-thermometers).\n")
    args = ["check", "--run-dir", str(rd), "--rec-id", "art-001", "--body", str(body), "--no-storage"]
    assert cli.main(args) == 1
    out = capsys.readouterr().out
    assert "lists no catalog products" in out and "DEFERRED: link minimum unreachable" in out
    assert "/k/roasting-pans" not in out
    body.write_text(body.read_text() + "[Check price](/k/roasting-pans?source=amazon) "
                                       "[Check price](/k/meat-thermometers?source=amazon)\n")
    assert cli.main(args) == 1
    assert "buy=0/2" in capsys.readouterr().out


class _FakeCur:
    def __init__(self, db):
        self.db, self._rows = db, []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.db.sql.append(s)
        if s.startswith("SET ") or s.startswith("UPDATE "):
            self._rows = []
            if s.startswith("UPDATE "):
                self.db.updates.append((s, params))
                # the conditional UPDATE matches only the body that was read
                current = {r[0]: r[1] for r in self.db.rows}
                self.rowcount = int(current.get(params[1]) == params[2])
        elif "information_schema.columns" in s:
            self._rows = [("slug",), ("body_md",), ("title",), ("bucket",), ("category",)]
        elif s.startswith("SELECT slug FROM kitchen_products"):
            self._rows = [(x,) for x in params[0] if x in self.db.active]
        elif s.startswith("SELECT title, bucket, category FROM editorial_articles"):
            self._rows = [self.db.meta[params[0]]] if params[0] in self.db.meta else []
        elif s.startswith("SELECT slug, body_md"):
            self._rows = list(self.db.rows)
        else:
            raise AssertionError(f"unexpected SQL: {s}")

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeDB:
    def __init__(self, rows=(), active=(), meta=None):
        self.rows, self.active, self.meta = list(rows), set(active), dict(meta or {})
        self.sql, self.updates, self.connects = [], [], []
        self.committed = self.closed = False

    def connect(self, dsn, readonly=True):
        self.connects.append((dsn, readonly))
        return self

    def cursor(self):
        return _FakeCur(self)

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True


def test_check_reads_a_refresh_rows_bucket_and_counts_only_active_products(tmp_path, capsys, monkeypatch):
    # article-refresh recs carry article_slug, no proposal (recs/ap-a17-*.json)
    (tmp_path / "recommendations.json").write_text(json.dumps({"agent_id": "aisleprompt-user-growth-strategist",
        "recommendations": [{"id": "a17-refresh-carving-knives", "type": "article-refresh", "site": "aisleprompt",
                             "article_slug": "best-carving-knives"}]}))
    db = _FakeDB(active={"victorinox-fibrox-12-inch-slicing-knife"},
                 meta={"best-carving-knives": ("Best Carving Knives for 2026", "kitchen-buying-guide",
                                               "buying-guide")})
    monkeypatch.setattr(cli, "_connect", db.connect)
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake/aisleprompt")
    body = tmp_path / "b.md"
    body.write_text(RECIPES + " The [Victorinox](/kitchen/victorinox-fibrox-12-inch-slicing-knife) "
                              "([Check price](/k/victorinox-fibrox-12-inch-slicing-knife?source=amazon)) and the "
                              "[Wusthof](/kitchen/wusthof-classic-8-inch-chef-knife) "
                              "([Check price](/k/wusthof-classic-8-inch-chef-knife?source=amazon)).\n")
    args = ["check", "--run-dir", str(tmp_path), "--rec-id", "a17-refresh-carving-knives",
            "--body", str(body), "--no-storage"]
    assert cli.main(args) == 1
    out = capsys.readouterr().out
    # the row's bucket set the minimum; the inactive Wusthof neither counts
    # as a buy link nor as a named product
    assert out.startswith("FAIL only 1 direct buy links (min 2)") and "product_pages=1" in out
    assert db.connects == [("postgresql://fake/aisleprompt", True)] and db.closed
    db.active.add("wusthof-classic-8-inch-chef-knife")
    assert cli.main(args) == 0
    assert capsys.readouterr().out.startswith("PASS ")
    assert cli.main(args + ["--no-db"]) == 0 and len(db.connects) == 2   # --no-db: no connection


GIFTS = (RECIPES + " Our pick is the [OXO scale](/kitchen/oxo-good-grips-11-pound-scale) and the "
         "[Nordic Ware sheet](https://aisleprompt.com/kitchen/nordic-ware-naturals-half-sheet), plus a "
         "[retired mixer](/kitchen/retired-mixer). Sources: [FLUX.2 [klein] card](https://hf.co/x).\n")
PASSING = RECIPES + " [OXO](/kitchen/oxo-good-grips-11-pound-scale) ([Check price](/k/oxo-good-grips-11-pound-scale)).\n"


def _sweep_db():
    return _FakeDB(rows=[("gifts-for-bakers", GIFTS, "Gifts for Bakers 2026", "seasonal-occasion", "buying-guide"),
                         ("fine-article", PASSING, "Holiday Baking", "seasonal-occasion", "seasonal-occasion")],
                   active={"oxo-good-grips-11-pound-scale", "nordic-ware-naturals-half-sheet"})


def test_sweep_dry_run_repairs_nothing_and_reports(tmp_path, capsys, monkeypatch):
    db = _sweep_db()
    monkeypatch.setattr(cli, "_connect", db.connect)
    monkeypatch.setenv("DATABASE_URL_AISLEPROMPT", "postgresql://fake/ap")
    rc = cli.main(["sweep", "--site", "aisleprompt", "--since-hours", "6", "--no-storage"])
    cap = capsys.readouterr()
    assert rc == 0 and db.updates == [] and db.connects == [("postgresql://fake/ap", True)]
    assert cap.out.startswith("gifts-for-bakers: only 0 direct buy links (min 2)")
    assert "1/2 row(s) fail the link contract" in cap.err
    report = tmp_path / "r.json"
    rc = cli.main(["sweep", "--site", "aisleprompt", "--since-hours", "6", "--no-storage",
                   "--repair-buy-links", "--repair-anchor-brackets", "--report-json", str(report)])
    cap = capsys.readouterr()
    assert rc == 0 and db.updates == [] and not db.committed and db.connects[-1][1] is True
    assert "gifts-for-bakers: would repair — " in cap.out and "dry run" in cap.err
    rep = json.loads(report.read_text())
    assert rep["committed"] is False and rep["catalog_check"] is True and rep["rows"] == 2
    assert [r["slug"] for r in rep["repaired"]] == ["gifts-for-bakers"] and rep["failed"] == []


def test_sweep_commit_writes_the_repaired_body_only(tmp_path, capsys, monkeypatch):
    db = _sweep_db()
    monkeypatch.setattr(cli, "_connect", db.connect)
    monkeypatch.setenv("DATABASE_URL_AISLEPROMPT", "postgresql://fake/ap")
    report = tmp_path / "run" / "link-guard.json"
    rc = cli.main(["sweep", "--site", "aisleprompt-article-proposal-agent", "--since-hours", "6",
                   "--no-storage", "--repair-buy-links", "--repair-anchor-brackets", "--commit",
                   "--report-json", str(report), "--fail-on-reject"])
    capsys.readouterr()
    assert rc == 0 and db.committed and db.closed and db.connects == [("postgresql://fake/ap", False)]
    assert len(db.updates) == 1
    sql, (new_body, slug, read_body) = db.updates[0]
    assert sql == ("UPDATE editorial_articles SET body_md = %s, updated_at = NOW() "
                   "WHERE slug = %s AND body_md = %s")
    assert slug == "gifts-for-bakers" and read_body == GIFTS
    assert ("[OXO scale](/kitchen/oxo-good-grips-11-pound-scale) ([Check price]"
            "(/k/oxo-good-grips-11-pound-scale?source=amazon))") in new_body
    assert ("(https://aisleprompt.com/kitchen/nordic-ware-naturals-half-sheet) ([Check price]"
            "(/k/nordic-ware-naturals-half-sheet?source=amazon))") in new_body
    assert "retired-mixer?source" not in new_body                    # inactive: no buy link
    assert "[FLUX.2 (klein) card](https://hf.co/x)" in new_body
    rep = json.loads(report.read_text())
    assert rep["committed"] is True and rep["repaired"][0]["passes_after"] is True


def test_sweep_without_a_catalog_never_adds_buy_links(capsys, monkeypatch):
    db = _sweep_db()

    def broken(conn, bodies, cfg, own_hosts=None):
        raise RuntimeError("catalog down")
    monkeypatch.setattr(cli, "_connect", db.connect)
    monkeypatch.setattr(cli, "product_slug_checker_from_db", broken)
    monkeypatch.setenv("DATABASE_URL_AISLEPROMPT", "postgresql://fake/ap")
    rc = cli.main(["sweep", "--site", "aisleprompt", "--no-storage", "--repair-buy-links", "--commit",
                   "--fail-on-reject"])
    cap = capsys.readouterr()
    assert rc == 1 and db.updates == []
    assert "catalog check failed" in cap.err and cap.out.startswith("gifts-for-bakers: only 0 direct buy links")


def test_sweep_commit_never_overwrites_a_body_changed_since_it_was_read(tmp_path, capsys, monkeypatch):
    db = _sweep_db()
    real_cursor = db.cursor

    def racing_cursor():
        cur = real_cursor()
        orig = cur.execute

        def execute(sql, params=None):
            if sql.lstrip().startswith("UPDATE"):          # another writer got there first
                db.rows = [("gifts-for-bakers", GIFTS + " edited", *db.rows[0][2:])] + db.rows[1:]
            return orig(sql, params)
        cur.execute = execute
        return cur
    db.cursor = racing_cursor
    monkeypatch.setattr(cli, "_connect", db.connect)
    monkeypatch.setenv("DATABASE_URL_AISLEPROMPT", "postgresql://fake/ap")
    report = tmp_path / "r.json"
    cli.main(["sweep", "--site", "aisleprompt", "--no-storage", "--repair-buy-links", "--commit",
              "--report-json", str(report)])
    out = capsys.readouterr().out
    assert "gifts-for-bakers: not repaired — body changed since it was read" in out
    assert json.loads(report.read_text())["repaired"][0]["skipped"] == "body changed since it was read"
