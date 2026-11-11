"""category-integrity-agent: score clicked low-confidence products first (plan A23).

397 of ~950 clicked products failed the Top-Pick floor (category_confidence
>= 0.5), so the products people buy could not reach guides, category picks or
alternatives rails. The demand phase scores them with the site's own
categoriser, and only where that scoring is evidence. It never lowers the 0.5
floor, never moves a product, never touches validated rows."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_DIR = Path(__file__).resolve().parents[2] / "agents" / "category-integrity-agent"


def _load(name, file):
    if str(_DIR) not in sys.path:
        sys.path.insert(0, str(_DIR))
    spec = importlib.util.spec_from_file_location(name, _DIR / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


demand = _load("category_integrity_demand_under_test", "demand.py")
SLUGS = {"gpus": 3, "ram": 7}


def _row(**kw):
    base = {"id": 1, "asin": "B0AAAAAAA1", "category_id": 3, "slug": "gpus",
            "confidence": None, "validated": False}
    base.update(kw)
    return base


def _decide(row, result, **kw):
    kw.setdefault("min_score", 0.6)
    kw.setdefault("assign_uncategorised", True)
    return demand.decide(row, result, slug_ids=SLUGS, **kw)


# -- policy -------------------------------------------------------------------

def test_defaults_are_off_and_the_floor_cannot_be_loosened():
    cfg = demand.resolve(None)
    assert cfg["enabled"] is False and cfg["min_score"] == 0.6
    assert demand.resolve({"min_score": 0.2})["min_score"] == 0.5
    assert demand.resolve({"min_score": "x"})["min_score"] == 0.6
    assert demand.resolve({"clicks": {"table": "t"}})["clicks"]["group_col"] == "asin"


def test_confirm_sets_the_score_and_keeps_the_category():
    v, upd = _decide(_row(), {"slug": "gpus", "confidence": 0.75, "junk": False})
    assert v == "confirm"
    assert upd == {"id": 1, "category_id": 3, "category_confidence": 0.75,
                   "expect_category_id": 3}


def test_never_moves_a_product_to_another_category():
    v, upd = _decide(_row(), {"slug": "ram", "confidence": 0.95, "junk": False})
    assert (v, upd) == ("conflict", None)


def test_assigns_an_uncategorised_product_only_when_the_rules_place_it():
    v, upd = _decide(_row(category_id=None, slug=None, confidence=0.3),
                     {"slug": "ram", "confidence": 0.9})
    assert v == "assign" and upd["category_id"] == 7 and upd["expect_category_id"] is None
    assert _decide(_row(category_id=None, slug=None), {"slug": "ram", "confidence": 0.9},
                   assign_uncategorised=False) == ("unplaced", None)
    assert _decide(_row(category_id=None, slug=None),
                   {"slug": "cases", "confidence": 0.9}) == ("unknown-slug", None)


@pytest.mark.parametrize("result,verdict", [
    (None, "no-result"),
    ({"slug": None, "confidence": 0.3, "junk": False}, "unplaced"),
    ({"slug": None, "confidence": 0, "junk": True}, "junk"),
    # a rule with no patterns returns a neutral 0.5: not evidence
    ({"slug": "gpus", "confidence": 0.5}, "low-score"),
    ({"slug": "gpus", "confidence": "high"}, "no-result"),
])
def test_writes_nothing_without_evidence(result, verdict):
    assert _decide(_row(), result) == (verdict, None)


def test_validated_rows_are_never_touched():
    assert _decide(_row(validated=True), {"slug": "gpus", "confidence": 0.95}) == ("validated", None)


def test_problems_name_missing_config():
    probs = demand.problems(demand.resolve({"enabled": True}))
    assert any("clicks.table" in p for p in probs)
    assert any("classifier.cmd" in p for p in probs)
    ok = demand.resolve({"enabled": True, "clicks": {"table": "clicks"},
                         "classifier": {"cmd": ["x"]}})
    assert demand.problems(ok) == []


def test_run_classifier_speaks_the_json_contract(tmp_path):
    script = tmp_path / "clf.py"
    script.write_text(
        "import json,sys\n"
        "items=json.load(sys.stdin)\n"
        "print(json.dumps([{'asin':i['asin'],'slug':i['hint'],'confidence':0.8,'junk':False}"
        " for i in items]+[{'oops':1}]))\n")
    cfg = demand.resolve({"classifier": {"cmd": [sys.executable, str(script)]}})
    out = demand.run_classifier(cfg, [{"asin": "B0AAAAAAA1", "title": "t", "brand": None,
                                       "hint": "gpus"}])
    assert out == {"B0AAAAAAA1": {"asin": "B0AAAAAAA1", "slug": "gpus",
                                  "confidence": 0.8, "junk": False}}
    assert demand.run_classifier(cfg, []) == {}
    bad = demand.resolve({"classifier": {"cmd": [sys.executable, "-c", "import sys; sys.exit(3)"]}})
    with pytest.raises(Exception):
        demand.run_classifier(bad, [{"asin": "B0AAAAAAA1"}])


# -- the agent's phase ----------------------------------------------------------

class _Cur:
    def __init__(self, conn):
        self.conn = conn
        self.rows = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.conn.executed.append((sql, params))
        self.rowcount = 0
        for needle, rows in self.conn.answers:
            if needle in sql:
                self.rows = list(rows)
                break
        else:
            self.rows = []
        if sql.lstrip().startswith("UPDATE products"):
            self.rowcount = 1

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class _Conn:
    def __init__(self, answers):
        self.answers = answers
        self.executed = []
        self.commits = 0

    def cursor(self):
        return _Cur(self)

    def rollback(self):
        pass

    def commit(self):
        self.commits += 1

    def close(self):
        pass

    def updates(self):
        return [p for s, p in self.executed if s.lstrip().startswith("UPDATE products")]


@pytest.fixture
def agent_mod(monkeypatch, tmp_path):
    cfg = tmp_path / "site.yaml"
    cfg.write_text("site: s\ndb_env: X_DSN\ndomain_vocabulary: [gpu]\nforeign_vocabulary: [sofa]\n")
    monkeypatch.setenv("CATEGORY_INTEGRITY_CONFIG", str(cfg))
    return _load("category_integrity_agent_under_test", "agent.py")


def _agent(mod, cfg, storage):
    a = mod.CategoryIntegrityAgent.__new__(mod.CategoryIntegrityAgent)
    a.cfg = cfg
    a.site = cfg.get("site", "s")
    a.agent_id = "s-category-integrity-agent"
    a.run_ts = "20261111T180000Z"
    a.storage = storage
    a.decisions = []
    a.status = lambda *x, **k: None
    a.decide = lambda kind, msg: a.decisions.append(msg)
    return a


DEMAND = {"enabled": True, "clicks": {"table": "clicks", "spec": {}, "match": {"target": "amazon"}},
          "classifier": {"cmd": ["unused"]}}


def test_phase_scores_clicked_rows_with_guards(agent_mod, monkeypatch, storage):
    from framework.core import human_clicks
    monkeypatch.setattr(human_clicks, "human_counts_by",
                        lambda conn, spec, **kw: {"B0AAAAAAA1": 5, "B0AAAAAAA2": 3,
                                                  "B0AAAAAAA3": 2, "B0AAAAAAA4": 1, "": 9})
    seen = {}

    def classify(cfg, items):
        seen["items"] = items
        return {"B0AAAAAAA1": {"asin": "B0AAAAAAA1", "slug": "gpus", "confidence": 0.75},
                "B0AAAAAAA2": {"asin": "B0AAAAAAA2", "slug": None, "confidence": 0.3},
                "B0AAAAAAA4": {"asin": "B0AAAAAAA4", "slug": "ram", "confidence": 0.9}}
    monkeypatch.setattr(agent_mod.demand, "run_classifier", classify)
    rows = [  # id, asin, title, brand, category_id, slug, confidence, validated
        (11, "B0AAAAAAA1", "RTX 5070 graphics card", "ASUS", 3, "gpus", None, False),
        (12, "B0AAAAAAA2", "iPad case", None, 3, "gpus", 0.3, False),
        (13, "B0AAAAAAA3", "Pi kit", None, 9, "sbc", 0.3, True),
        (14, "B0AAAAAAA4", "DDR5 32GB kit", None, None, None, 0.3, False),
    ]
    conn = _Conn([("FROM products p LEFT JOIN categories c", rows),
                  ("FROM categories WHERE is_active GROUP BY slug", [("gpus", 3), ("ram", 7)]),
                  ("count(DISTINCT asin)", [(3, 4)])])
    a = _agent(agent_mod, {"site": "s", "demand_scoring": DEMAND}, storage)
    out = a._demand_scoring(conn)
    assert out["error"] == "" and out["clicked_asins"] == 4
    assert out["verdicts"] == {"confirm": 1, "unplaced": 1, "validated": 1, "assign": 1}
    assert out["written"] == 2 and out["pass_pct"] == 75.0
    # validated rows are never sent to the classifier; hints are the current slug
    assert [i["asin"] for i in seen["items"]] == ["B0AAAAAAA1", "B0AAAAAAA2", "B0AAAAAAA4"]
    assert seen["items"][0]["hint"] == "gpus" and seen["items"][2]["hint"] is None
    ups = conn.updates()
    assert ups == [(3, 0.75, 11, 0.5, 3), (7, 0.9, 14, 0.5, None)]
    upd_sql = [s for s, _ in conn.executed if s.lstrip().startswith("UPDATE products")][0]
    for guard in ("COALESCE(category_confidence, 0) < %s",
                  "NOT COALESCE(category_validated, false)",
                  "category_id IS NOT DISTINCT FROM %s", "AND is_active"):
        assert guard in upd_sql
    assert conn.commits == 1
    assert out["unresolved"][0]["asin"] == "B0AAAAAAA2"


def test_phase_off_by_default_runs_no_queries(agent_mod, storage):
    conn = _Conn([])
    out = _agent(agent_mod, {"site": "s"}, storage)._demand_scoring(conn)
    assert out["enabled"] is False and conn.executed == []


def test_classifier_failure_writes_nothing(agent_mod, monkeypatch, storage):
    from framework.core import human_clicks
    monkeypatch.setattr(human_clicks, "human_counts_by", lambda conn, spec, **kw: {"B0AAAAAAA1": 1})

    def boom(cfg, items):
        raise FileNotFoundError("npx")
    monkeypatch.setattr(agent_mod.demand, "run_classifier", boom)
    conn = _Conn([("FROM products p LEFT JOIN categories c",
                   [(11, "B0AAAAAAA1", "t", None, 3, "gpus", None, False)])])
    out = _agent(agent_mod, {"site": "s", "demand_scoring": DEMAND}, storage)._demand_scoring(conn)
    assert "npx" in out["error"] and out["written"] == 0 and conn.updates() == []


def test_run_reports_demand_metrics_and_keeps_old_ones(agent_mod, monkeypatch, storage):
    a = _agent(agent_mod, {"site": "s", "db_env": "X", "domain_vocabulary": ["gpu"],
                           "foreign_vocabulary": ["sofa"]}, storage)
    conn = _Conn([("count(*) FILTER (WHERE category_confidence IS NULL)", [(10, 100)]),
                  ("AND NOT COALESCE(p.category_validated, false)",
                   [(5, "B0SOFA0001", "Velvet sofa cover", "slow-cookers")])])
    monkeypatch.setattr(a, "_conn", lambda: conn)
    res = a.run()
    # demand phase off: the result is exactly the old metric set
    assert set(res.metrics) == {"decategorised", "unscored_pct", "recs"}
    assert res.metrics["decategorised"] == 1.0
    a.cfg["demand_scoring"] = {"enabled": True}          # enabled but unconfigured
    res = a.run()
    assert res.metrics["demand_error"] == 1.0 and "demand phase error" in res.summary
