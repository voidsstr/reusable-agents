"""category-integrity-agent: score clicked low-confidence products first (plan A23).

397 of ~950 clicked products failed the Top-Pick floor (category_confidence
>= 0.5), so the products people buy could not reach guides, category picks or
alternatives rails. The demand phase scores them with the site's own
categoriser, and only where that scoring is evidence. It never lowers the 0.5
floor, never moves a product, never touches validated rows.

Review (2026-11-11) tightened what counts as evidence, after a read-only
replay showed ~20% of the first run's 61 writes put a wrong-category
accessory over the Top-Pick floor (a CPU air cooler as a CPU, a GPU support
bracket as a GPU, a Quest 3 strap as a VR headset): only NEVER-scored rows
(an explicit sub-0.5 value is a deliberate demotion), only rows priced like
a Top Pick, never an off-domain title, a confirm needs the categoriser's
unhinted best rule to agree, and accessory-led titles only land in
accessory categories. An enabled phase reports by default (mode: report)."""
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
    # by default the categoriser's unhinted best placement agrees with the
    # hinted one; the agreement tests pass their own
    kw.setdefault("unhinted", result)
    return demand.decide(row, result, slug_ids=SLUGS, **kw)


# -- policy -------------------------------------------------------------------

def test_defaults_are_off_and_the_floor_cannot_be_loosened():
    cfg = demand.resolve(None)
    assert cfg["enabled"] is False and cfg["min_score"] == 0.6
    assert demand.resolve({"min_score": 0.2})["min_score"] == 0.5
    assert demand.resolve({"min_score": "x"})["min_score"] == 0.6
    assert demand.resolve({"clicks": {"table": "t"}})["clicks"]["group_col"] == "asin"
    # an enabled phase only reports until write mode is chosen explicitly
    assert cfg["mode"] == "report" and demand.resolve({"mode": "WRITE"})["mode"] == "write"
    assert demand.resolve({"mode": "yolo"})["mode"] == "report"
    assert cfg["require_agreement"] is True and cfg["require_valid_price"] is True
    assert cfg["accessory_guard"]["enabled"] is True and cfg["accessory_guard"]["categories"] == []


def test_confirm_sets_the_score_and_keeps_the_category():
    v, upd = _decide(_row(), {"slug": "gpus", "confidence": 0.75, "junk": False})
    assert v == "confirm"
    assert upd == {"id": 1, "category_id": 3, "category_confidence": 0.75,
                   "expect_category_id": 3}


def test_never_moves_a_product_to_another_category():
    v, upd = _decide(_row(), {"slug": "ram", "confidence": 0.95, "junk": False})
    assert (v, upd) == ("conflict", None)


def test_assigns_an_uncategorised_product_only_when_the_rules_place_it():
    v, upd = _decide(_row(category_id=None, slug=None),
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


# -- review guards (2026-11-11) ---------------------------------------------------

def test_an_explicit_sub_floor_value_is_a_decision_not_a_gap():
    """0.3 is the reversible demotion the pricing-integrity and honesty fixes
    use; re-scoring it would undo them (13 of the first run's 61 writes)."""
    for conf in (0.3, 0.0, 0.15, 0.49):
        assert _decide(_row(confidence=conf), {"slug": "gpus", "confidence": 0.95}) == \
            ("explicit-score", None)
        assert _decide(_row(category_id=None, slug=None, confidence=conf),
                       {"slug": "ram", "confidence": 0.95}) == ("explicit-score", None)


def test_rows_failing_the_top_pick_price_filter_wait():
    assert _decide(_row(price_ok=False), {"slug": "gpus", "confidence": 0.95}) == \
        ("price-gated", None)
    # unknown (not computed) is not a failure
    assert _decide(_row(price_ok=None), {"slug": "gpus", "confidence": 0.95})[0] == "confirm"


def test_off_domain_titles_are_never_scored():
    assert _decide(_row(off_domain=True), {"slug": "gpus", "confidence": 0.95}) == \
        ("off-domain", None)
    assert _decide(_row(category_id=None, slug=None, off_domain=True),
                   {"slug": "ram", "confidence": 0.95}) == ("off-domain", None)


def test_confirm_needs_the_unhinted_best_rule_to_agree():
    hinted = {"slug": "gpus", "confidence": 0.8}
    # the classifier's own best placement is another category: keyword hit only
    assert _decide(_row(), hinted, unhinted={"slug": "cpus", "confidence": 0.9}) == \
        ("disagree", None)
    assert _decide(_row(), hinted, unhinted=None) == ("disagree", None)
    assert _decide(_row(), hinted, unhinted={"slug": "gpus", "confidence": 0.9,
                                             "junk": True}) == ("disagree", None)
    assert _decide(_row(), hinted, unhinted={"slug": "gpus", "confidence": "x"}) == \
        ("disagree", None)
    # agreement keeps the LOWER score, and that must still clear min_score
    v, upd = _decide(_row(), hinted, unhinted={"slug": "gpus", "confidence": 0.7})
    assert v == "confirm" and upd["category_confidence"] == 0.7
    assert _decide(_row(), hinted, unhinted={"slug": "gpus", "confidence": 0.55}) == \
        ("low-score", None)
    # opt-out restores hinted-only confirmation
    assert _decide(_row(), hinted, unhinted=None, require_agreement=False)[0] == "confirm"


@pytest.mark.parametrize("title,term", [
    ("Thermalright Peerless Assassin 120 White ARGB CPU Air Cooler", "cooler"),
    ("GPU Support Bracket for Fan Mounting, Adjustable GPU Support", "bracket"),
    ("Portable SSD Case for SanDisk Extreme Pro, for Samsung T7/T9", "case"),
    ("VIVO Behind Monitor VESA Mount Designed for Mac mini M4", "designed for"),
    ("Eightwood WiFi 6E Antenna Tri-Band 6GHz 5GHz 2.4GHz RP-SMA", "antenna"),
    ("Battery Head Strap for Meta Quest 3/3S - 10000mAh Fast Charging", "strap"),
    ("Hard Carrying Case Compatible with Retroid Pocket Classic", "compatible with"),
    ("CSTESVN SNES Controller Extension Cable, 2-Pack 1.8M/6FT", "cable"),
    ("for ANBERNIC R36S Retro Handheld Game Protective case,Soft Silicone", "case"),
])
def test_accessory_led_titles_are_recognised(title, term):
    assert demand.accessory_term(title, None) == term


@pytest.mark.parametrize("title", [
    "KTC 24 Inch 240Hz Gaming Monitor, FHD PC Computer Monitor, VESA Mount Compatible",
    "Nacon Revolution X Wireless Controller for Xbox, compatible with PC",
    "Sapphire Pulse AMD Radeon RX 9070 GRE Gaming Graphics Card",
    "Logitech Gamepad F310 with Touchpad",       # gamepad / touchpad are not "pad"
])
def test_the_product_noun_decides_not_a_feature_in_the_tail(title):
    assert demand.accessory_term(title, None) is None


def test_accessories_only_land_in_accessory_categories():
    guard = demand.resolve({"accessory_guard": {"categories": ["cooling"]}})["accessory_guard"]
    cooler = "Thermalright Peerless Assassin 120 SE CPU Cooler, 6 Heat Pipes"
    hint = {"slug": "gpus", "confidence": 0.8}
    assert _decide(_row(title=cooler), hint, guard=guard) == ("accessory", None)
    v, _ = _decide(_row(title=cooler, category_id=5, slug="cooling"),
                   {"slug": "cooling", "confidence": 0.8}, guard=guard)
    assert v == "confirm"
    # assign is guarded the same way
    assert _decide(_row(title=cooler, category_id=None, slug=None),
                   {"slug": "ram", "confidence": 0.9}, guard=guard) == ("accessory", None)
    # a site can add terms, or switch the guard off
    extra = demand.resolve({"accessory_guard": {"extra_terms": ["guitar"]}})["accessory_guard"]
    assert demand.accessory_term("Donner DP-1 Guitar Power Supply", extra) == "guitar"
    off = demand.resolve({"accessory_guard": {"enabled": False}})["accessory_guard"]
    assert _decide(_row(title=cooler), hint, guard=off)[0] == "confirm"


def test_title_head_stops_at_the_first_separator():
    assert demand.title_head("Gaming Monitor, 240Hz - VESA Mount") == "Gaming Monitor"
    assert demand.title_head("Battery Strap for Quest 3 - 10000mAh") == "Battery Strap for Quest 3"
    assert len(demand.title_head("x" * 200, 60)) == 60
    assert demand.title_head(None) == ""


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


ROWS = [  # id, asin, title, brand, category_id, slug, confidence, validated, price_ok
    (11, "B0AAAAAAA1", "RTX 5070 graphics card", "ASUS", 3, "gpus", None, False, True),
    (12, "B0AAAAAAA2", "iPad case", None, 3, "gpus", None, False, True),
    (13, "B0AAAAAAA3", "Pi kit", None, 9, "sbc", 0.3, True, True),
    (14, "B0AAAAAAA4", "DDR5 32GB kit", None, None, None, None, False, True),
    # an explicit 0.3: a pricing/honesty demotion, never re-promoted
    (15, "B0AAAAAAA5", "RTX 4060 graphics card", None, 3, "gpus", 0.3, False, True),
    # never scored but unpriced: waits for a valid price
    (16, "B0AAAAAAA6", "RTX 3050 graphics card", None, 3, "gpus", None, False, False),
    # off-domain: foreign vocabulary, no domain vocabulary
    (17, "B0AAAAAAA7", "Velvet sofa cover", None, 3, "gpus", None, False, True),
    # the hinted rule fires on a keyword, the unhinted best rule disagrees
    (18, "B0AAAAAAA8", "ThinkPad laptop RAM 32GB", None, 3, "gpus", None, False, True),
]
CLICKS = {"B0AAAAAAA1": 9, "B0AAAAAAA2": 8, "B0AAAAAAA3": 7, "B0AAAAAAA4": 6,
          "B0AAAAAAA5": 5, "B0AAAAAAA6": 4, "B0AAAAAAA7": 3, "B0AAAAAAA8": 2, "": 9}
HINTED = {"B0AAAAAAA1": {"asin": "B0AAAAAAA1", "slug": "gpus", "confidence": 0.75},
          "B0AAAAAAA2": {"asin": "B0AAAAAAA2", "slug": "gpus", "confidence": 0.75},
          "B0AAAAAAA4": {"asin": "B0AAAAAAA4", "slug": "ram", "confidence": 0.9},
          "B0AAAAAAA8": {"asin": "B0AAAAAAA8", "slug": "gpus", "confidence": 0.8}}
UNHINTED = {"B0AAAAAAA1": {"asin": "B0AAAAAAA1", "slug": "gpus", "confidence": 0.9},
            "B0AAAAAAA2": {"asin": "B0AAAAAAA2", "slug": "gpus", "confidence": 0.75},
            "B0AAAAAAA8": {"asin": "B0AAAAAAA8", "slug": "ram", "confidence": 0.95}}


def _phase(agent_mod, monkeypatch, storage, demand_cfg):
    from framework.core import human_clicks
    monkeypatch.setattr(human_clicks, "human_counts_by", lambda conn, spec, **kw: dict(CLICKS))
    calls = []

    def classify(cfg, items):
        calls.append(items)
        table = HINTED if any(i["hint"] for i in items) or len(calls) == 1 else UNHINTED
        return {i["asin"]: table[i["asin"]] for i in items if i["asin"] in table}
    monkeypatch.setattr(agent_mod.demand, "run_classifier", classify)
    conn = _Conn([("FROM products p LEFT JOIN categories c", ROWS),
                  ("FROM categories WHERE is_active GROUP BY slug", [("gpus", 3), ("ram", 7)]),
                  ("count(DISTINCT asin)", [(5, 8)])])
    a = _agent(agent_mod, {"site": "s", "demand_scoring": demand_cfg}, storage)
    out = a._demand_scoring(conn, domain={"gpu", "graphics", "ram", "laptop", "card"},
                            foreign={"sofa", "velvet"})
    return out, conn, calls


EXPECTED_VERDICTS = {"confirm": 1, "accessory": 1, "validated": 1, "assign": 1,
                     "explicit-score": 1, "price-gated": 1, "off-domain": 1, "disagree": 1}


def test_phase_reports_by_default_and_writes_nothing(agent_mod, monkeypatch, storage):
    out, conn, calls = _phase(agent_mod, monkeypatch, storage, DEMAND)
    assert out["error"] == "" and out["mode"] == "report" and out["clicked_asins"] == 8
    assert out["verdicts"] == EXPECTED_VERDICTS
    assert conn.updates() == [] and conn.commits == 0 and out["written"] == 0
    # the would-be writes are listed for review
    assert [(w["asin"], w["verdict"], w["category"], w["confidence"]) for w in out["writes"]] == [
        ("B0AAAAAAA1", "confirm", "gpus", 0.75), ("B0AAAAAAA4", "assign", "ram", 0.9)]
    # settled rows (validated / explicit / price-gated / off-domain) are never
    # classified; the unhinted call covers only categorised rows
    assert [i["asin"] for i in calls[0]] == ["B0AAAAAAA1", "B0AAAAAAA2", "B0AAAAAAA4",
                                             "B0AAAAAAA8"]
    assert calls[0][0]["hint"] == "gpus" and calls[0][2]["hint"] is None
    assert [i["asin"] for i in calls[1]] == ["B0AAAAAAA1", "B0AAAAAAA2", "B0AAAAAAA8"]
    assert all(i["hint"] is None for i in calls[1])
    unresolved = {u["asin"]: u for u in out["unresolved"]}
    assert set(unresolved) == {"B0AAAAAAA2", "B0AAAAAAA7", "B0AAAAAAA8"}
    assert unresolved["B0AAAAAAA2"]["accessory_term"] == "case"
    assert unresolved["B0AAAAAAA8"]["best_slug"] == "ram"
    assert out["pass_pct"] == 62.5


def test_phase_write_mode_applies_only_with_guards(agent_mod, monkeypatch, storage):
    out, conn, _ = _phase(agent_mod, monkeypatch, storage, {**DEMAND, "mode": "write"})
    assert out["verdicts"] == EXPECTED_VERDICTS
    ups = conn.updates()
    assert ups == [(3, 0.75, 11, 3, "USD"), (7, 0.9, 14, None, "USD")]
    upd_sql = [s for s, _ in conn.executed if s.lstrip().startswith("UPDATE products")][0]
    for guard in ("category_confidence IS NULL",
                  "NOT COALESCE(category_validated, false)",
                  "category_id IS NOT DISTINCT FROM %s", "AND is_active",
                  "price >= 1.0", "(currency IS NULL OR currency = %s)",
                  "(original_price IS NULL OR original_price >= price)"):
        assert guard in upd_sql
    assert "COALESCE(category_confidence, 0) <" not in upd_sql
    assert conn.commits == 1 and out["written"] == 2
    sel_sql, sel_params = [(s, p) for s, p in conn.executed
                           if "FROM products p LEFT JOIN categories c" in s][0]
    assert "AS price_ok" in sel_sql and sel_params[0] == "USD"


def test_phase_without_price_gate_or_agreement(agent_mod, monkeypatch, storage):
    cfg = {**DEMAND, "mode": "write", "require_valid_price": False, "require_agreement": False}
    out, conn, calls = _phase(agent_mod, monkeypatch, storage, cfg)
    assert len(calls) == 1                       # no unhinted call
    upd_sql = [s for s, _ in conn.executed if s.lstrip().startswith("UPDATE products")][0]
    assert "price >= 1.0" not in upd_sql
    sel = [(s, p) for s, p in conn.executed if "FROM products p LEFT JOIN categories c" in s][0]
    assert "TRUE AS price_ok" in sel[0] and sel[1][1] == 0.5


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
