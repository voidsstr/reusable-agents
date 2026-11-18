"""agents/pick-builder-agent: the deterministic pick-builder engine (F17).

Offline: no network, no LLM (the engine has none), no production database.
DB tests run against the throwaway postgres:16 container the spec-extractor
tests start (one per test process, one database per test; skipped without
docker / the local image), loaded with agents/pick-builder-agent/fixtures/
schema.sql, the reference implementation of the TABLE CONTRACT, and they run
the engine with the shipped site.example.yaml.

Covered: the selector language (expressions, templates, filters with NULLs,
diversity, budget), ladder plans (compat and via_of dependencies, one-unit
compat edges, conservative merging of duplicate compat rows, budget
exhaustion, adapter money reserved for the via slot, own-budget
alternatives, deliberately empty and not-needed slots with reasons, missing
parameters never loosening a filter), best lists (and why a list is short),
the substitutes map (+-15% window, subject comparisons on mapped attributes,
same-model preference, no near-miss), the integrity floor on top of the
eligibility view, stored text that never carries an Amazon price or a number
derived from one and does not churn with the pool, determinism under input
order, group-wise diff writes, the short-circuit, the eligibility sweep of
plan-less ladders and selector-less lists, the published pick-ASIN list
(read back through framework.core.price_refresh_priority), a failed publish
retried by the next run, dropped picks kept one run, the post-run ineligible
check, a failing run keeping its state, the read-only --plan, the advisory
lock, settings validation and the goals wiring, and the DSN: mis-encoded
passwords ('%', '@', a space) never reach a RunResult field, a file under
the storage root, stdout/stderr or the --plan CLI (the redaction helpers
are kept identical to the spec-extractor's copy).
"""
from __future__ import annotations

import copy
import importlib.util as iu
import json
import random
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from framework.core import price_refresh_priority as prp
from framework.tests.test_spec_extractor_agent import (LIBPQ_ECHOES, PW_TAIL, _free_port,
                                                       assert_secret_kept_out, fresh_database, query)

ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = ROOT / "agents" / "pick-builder-agent"
FX = AGENT_DIR / "fixtures"

if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))
_spec = iu.spec_from_file_location("pick_builder_agent_under_test", AGENT_DIR / "agent.py")
A = iu.module_from_spec(_spec)
sys.modules[_spec.name] = A
_spec.loader.exec_module(A)
S = A.settings_mod
R = A.run_mod
ST = A.store_mod
E = sys.modules["pick_builder_engine"]
REAL_DEFAULT_CONNECT = A.default_connect     # the autouse guard replaces the module attribute

AGENT_ID = "example-site-pick-builder"
DSN_ENV = "F17_PICK_BUILDER_TEST_DSN"
PUBLISH_KEY = f"agents/{AGENT_ID}/state/pick-asins.json"
NOW = datetime(2026, 11, 17, 12, 35, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    import urllib.request

    def no_network(*_a, **_k):
        raise AssertionError("network / production database access attempted in an offline test")

    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    monkeypatch.setattr(A, "default_connect", no_network)
    from framework.core import site_registry
    site_registry.clear_cache()
    yield
    site_registry.clear_cache()


def example_block() -> dict:
    import yaml
    doc = yaml.safe_load((AGENT_DIR / "site.example.yaml").read_text())
    block = doc["pick_builder"]
    block["database"]["dsn_env"] = DSN_ENV
    return block


# ---------------------------------------------------------------------------
# The fixture catalog (prices relative to the database clock: the view's
# 24h window uses now())
# ---------------------------------------------------------------------------

MODELS = [  # id, kind, name, grid_interactive, (capacity, usable, cont, rated_w)
    (1, "station", "Alpha 1000", False, (1070, 900, 1500, None)),
    (2, "station", "Beta 2000", False, (2048, 1700, 2200, None)),
    (3, "station", "Gamma 2000", False, (2048, 1750, 2400, None)),
    (4, "station", "Delta 3600", False, (3600, 3000, 3600, None)),
    (5, "station", "Epsilon 500", False, (512, 450, 600, None)),
    (6, "station", "Grid-tie 5000", True, (None, None, None, None)),
    (11, "panel", "Panel 100W DC8020", False, (None, None, None, 100)),
    (12, "panel", "Panel 200W MC4", False, (None, None, None, 200)),
    (21, "adapter", "MC4 to DC8020 adapter", False, (None, None, None, None)),
]
PRODUCTS = [  # id, asin, model, kind, brand, price, list_price, currency, age_h, extra
    (1, "B0ALPHA001", 1, "station", "alpha", 499, 599, "USD", 2, {}),
    (6, "B0ALPHA002", 1, "station", "alpha", 529, None, "USD", 3, {}),
    (2, "B0BETA0001", 2, "station", "beta", 999, None, "USD", 2, {}),
    (3, "B0GAMMA001", 3, "station", "gamma", 1099, 1299, "USD", 2, {}),
    (4, "B0DELTA001", 4, "station", "delta", 2199, None, "USD", 2, {}),
    (5, "B0EPSILN01", 5, "station", "epsilon", 199, None, "USD", 2, {}),
    (11, "B0PANEL101", 11, "panel", "sunco", 199, None, "USD", 2, {}),
    (12, "B0PANEL201", 12, "panel", "sunco", 249, None, "USD", 2, {}),
    (21, "B0ADAPTR01", 21, "adapter", "sunco", 29, None, "USD", 2, {}),
    (7, "B0STALE001", 2, "station", "beta", 949, None, "USD", 30, {}),          # price 30h old
    (8, "B0RECALL01", 3, "station", "gamma", 1049, None, "USD", 2, {"recall": True}),
    (9, "B0EUR00001", 4, "station", "delta", 1999, None, "EUR", 2, {}),
    (10, "B0GRIDTIE1", 6, "station", "gridco", 2999, None, "USD", 2, {}),
]
EDGES = [  # a, b, relation, verdict, via
    (1, 11, "charges_from", "fits", None),
    (1, 12, "charges_from", "fits_with_adapter", 21),
    (2, 12, "charges_from", "fits", None),
    (4, 12, "charges_from", "fits", None),
    (4, 11, "charges_from", "over_limit", None),
]
PRICES = {str(p[5]) for p in PRODUCTS} | {str(p[6]) for p in PRODUCTS if p[6]}


def seed(dsn: str, *, reverse: bool = False) -> None:
    import psycopg2
    conn = psycopg2.connect(dsn)
    order = (lambda xs: list(reversed(xs))) if reverse else (lambda xs: list(xs))
    with conn.cursor() as cur:
        cur.execute((FX / "schema.sql").read_text())
        for mid, kind, name, grid, (cap, usable, cont, rated) in order(MODELS):
            cur.execute("INSERT INTO models (id, kind, model_name, grid_interactive) VALUES (%s,%s,%s,%s)",
                        (mid, kind, name, grid))
            cur.execute("INSERT INTO model_specs (model_id, capacity_wh, usable_wh, ac_cont_w, rated_w) "
                        "VALUES (%s,%s,%s,%s,%s)", (mid, cap, usable, cont, rated))
        for pid, asin, mid, kind, brand, price, lp, cur_, age, extra in order(PRODUCTS):
            cur.execute("INSERT INTO products (id, asin, model_id, kind, brand) VALUES (%s,%s,%s,%s,%s)",
                        (pid, asin, mid, kind, brand))
            cur.execute("INSERT INTO amazon_item_cache (asin, price, list_price, currency, outcome, fetched_at) "
                        "VALUES (%s,%s,%s,%s,'priced', now() - make_interval(hours => %s))",
                        (asin, price, lp, cur_, age))
            if extra.get("recall"):
                cur.execute("INSERT INTO recall_holds (product_id, tier) VALUES (%s, 'stop-use')", (pid,))
        for a, b, rel, verdict, via in order(EDGES):
            cur.execute("INSERT INTO compat_edges (a_model_id, b_model_id, relation, verdict, via_model_id, "
                        "rule_id, rule_version, confidence) VALUES (%s,%s,%s,%s,%s,'solar-charging','v1','computed')",
                        (a, b, rel, verdict, via))
        cur.execute("INSERT INTO ladders (id, slug, kind) VALUES (1, 'winter-outage', 'outage-kit'), "
                    "(2, 'rv-weekend', 'rv-kit'), (3, 'draft-kit', 'outage-kit')")
        cur.execute("UPDATE ladders SET status = 'draft' WHERE id = 3")
        cur.execute("INSERT INTO ladder_tiers (id, ladder_id, budget_usd, required_wh) VALUES "
                    "(10, 1, 800, 800), (11, 1, 5000, 2500), (12, 1, 300, 5000), (13, 1, NULL, 500), "
                    "(20, 2, 1500, 1000), (30, 3, 1000, 500)")
        # an RV ladder slot written long ago (its kind has no plan) that is now ineligible
        cur.execute("INSERT INTO ladder_slots (tier_id, role, rank, product_id, reason) VALUES "
                    "(20, 'station', 1, 7, 'hand-picked'), (20, 'panel', 1, 11, 'hand-picked')")
        cur.execute("INSERT INTO pick_lists (id, slug, kind, params) VALUES "
                    "(1, 'best-power-station-under-1000', 'best-under', %s), "
                    "(2, 'best-for-well-pump', 'best-for', %s), "
                    "(3, 'best-under-no-cap', 'best-under', %s), "
                    "(4, 'black-friday-deals', 'deal', '{}')",
                    (json.dumps({"product_kind": "station", "max_price": 1000, "size": 3}),
                     json.dumps({"min_cont_w": 2000}),
                     json.dumps({"product_kind": "station"})))
        cur.execute("INSERT INTO pick_list_items (list_id, rank, product_id, reason) VALUES "
                    "(4, 1, 9, 'old deal'), (4, 2, 5, 'old deal')")
    conn.commit()
    conn.close()


@pytest.fixture
def db():
    dsn = fresh_database("pkb")
    seed(dsn)
    return dsn


class Harness:
    def __init__(self, storage, dsn: str, block: dict | None = None):
        self.storage = storage
        self.dsn = dsn
        self.block = block or example_block()

    def settings(self):
        return S.build_settings(self.block, agent_id=AGENT_ID)

    def run(self):
        import psycopg2
        ag = A.PickBuilderAgent(agent_id=AGENT_ID, storage=self.storage, settings=self.settings(),
                                connect=lambda _s, environ=None: psycopg2.connect(self.dsn), clock=lambda: NOW)
        ag.auto_recovery_enabled = False
        return ag.run_once()

    def state(self) -> dict:
        return (self.storage.read_json(f"agents/{AGENT_ID}/state/latest.json") or {}).get("state") or {}

    def q(self, sql: str, params: Any = None) -> list[dict]:
        return query(self.dsn, sql, params)

    def slots(self) -> dict:
        return {(r["tier_id"], r["role"], r["rank"]): r for r in
                self.q("SELECT * FROM ladder_slots ORDER BY tier_id, role, rank")}

    def items(self, list_id: int) -> list[int]:
        return [r["product_id"] for r in self.q("SELECT product_id FROM pick_list_items WHERE list_id = %s "
                                                "ORDER BY rank", (list_id,))]

    def subs(self) -> dict:
        out: dict = {}
        for r in self.q("SELECT product_id, alt_product_id FROM product_substitutes ORDER BY product_id, rank"):
            out.setdefault(r["product_id"], []).append(r["alt_product_id"])
        return out


@pytest.fixture
def h(storage, db):
    return Harness(storage, db)


# ---------------------------------------------------------------------------
# Selector language (no DB)
# ---------------------------------------------------------------------------

def P(pid, kind="station", price=100.0, model=None, **attrs) -> "E.Product":
    return E.Product(product_id=pid, asin=f"B0TEST{pid:04d}", kind=kind, price=float(price),
                     model_id=model if model is not None else pid, attrs=attrs)


def test_expressions_parse_safely_and_propagate_nulls():
    p = P(1, price=200, usable_wh=1000, cont_w=None)
    assert E.Expr("usable_wh / price").value(p.get) == 5.0
    assert E.Expr("(usable_wh - price) * 2 + -1").value(p.get) == 1599.0
    assert E.Expr("1 + 2 * 3").value(p.get) == 7.0
    assert E.Expr("usable_wh / cont_w").value(p.get) is None          # NULL operand
    assert E.Expr("usable_wh / 0").value(p.get) is None               # no division by zero
    assert E.Expr("kind").value(p.get) == "station"                   # a bare name keeps its type
    assert E.Expr("usable_wh/price").names == {"usable_wh", "price"}
    for bad in ("", "price +", "__import__('os')", "price.real", "a; b", "(price"):
        with pytest.raises(E.ExprError):
            E.Expr(bad)


def test_templates_resolve_or_refuse():
    ctx = {"tier": {"required_wh": 800}, "list": {"size": 3}}
    assert E.resolve("{tier.required_wh}", ctx) == 800
    assert E.resolve("{list.max_price|500}", ctx) == 500
    assert E.resolve("{list.size|10}", ctx) == 3
    assert E.resolve("station", ctx) == "station" and E.resolve(5, ctx) == 5
    with pytest.raises(E.MissingParam) as exc:
        E.resolve("{tier.required_w}", ctx)
    assert exc.value.name == "tier.required_w"
    assert E.format_reason("{usable_wh:,.0f} Wh, {rated_w:.0f} W", {"usable_wh": 1700.0, "rated_w": 200}) \
        == "1,700 Wh, 200 W"
    assert E.format_reason("{missing} x", {}) is None
    assert E.format_reason("{price.__class__}", {"price": 1}) is None
    assert E.format_reason("{x[0]}", {"x": [1]}) is None
    # a stored reason never sees an Amazon value, even when a template names one
    assert P(1, price=499, usable_wh=900).stored_ctx() == {"usable_wh": 900, "product_id": 1,
                                                           "asin": "B0TEST0001", "model_id": 1,
                                                           "kind": "station", "category_confidence": None}
    assert E.reason_names("{rated_w:,.0f} W for ${price:,.0f} {{lit}}") == ["rated_w", "price"]
    assert E.hidden_name("slot.station.price", E.AMAZON_FIELDS) and not E.hidden_name("tier.max_price",
                                                                                         E.AMAZON_FIELDS)


def test_filters_never_pass_a_null_and_missing_params_empty_the_slot():
    pool = [P(1, usable_wh=1000), P(2, usable_wh=None), P(3, usable_wh=500)]
    sel = E.select(pool, {"filters": [{"attr": "usable_wh", "op": "gte", "value": 600}]}, {}, count=5)
    assert [p.product_id for p in sel.picks] == [1]
    sel = E.select(pool, {"filters": [{"attr": "usable_wh", "op": "is_null"}]}, {}, count=5)
    assert [p.product_id for p in sel.picks] == [2]
    sel = E.select(pool, {"filters": [{"attr": "usable_wh", "op": "gte", "value": "{tier.required_wh}"}]},
                   {"tier": {}}, count=5)
    assert sel.picks == [] and "missing parameter tier.required_wh" in sel.empty_reason
    sel = E.select(pool, {"filters": [{"attr": "usable_wh", "op": "gte", "value": 5000}]}, {}, count=1)
    # the stored reason names the filter, without counts; the counted trace is for --plan only
    assert sel.picks == [] and sel.empty_reason == "no eligible candidate: usable_wh >= 5000"
    assert sel.trace_text == "3 eligible; usable_wh >= 5000 -> 0"
    assert E.select([], {}, {}, count=1).empty_reason == "no eligible candidate: nothing in the eligibility view"


def test_expression_filters_sorting_and_diversity_are_deterministic():
    pool = [P(1, price=100, brand="a", usable_wh=500, list_price=None),
            P(2, price=200, brand="a", usable_wh=1200),
            P(3, price=300, brand="a", usable_wh=1500),
            P(4, price=250, brand="b", usable_wh=900, model=2),     # same model as product 2
            P(5, price=90, brand="c", usable_wh=None)]
    spec = {"sort": [{"key": "usable_wh / price", "dir": "desc"}], "max_per": [{"attr": "brand", "count": 2}]}
    first = [p.product_id for p in E.select(pool, spec, {}, count=5).picks]
    # 6.0 first; 1 and 3 tie at 5.0 and the cheaper wins; brand a is then capped at 2 (3 out);
    # 4 shares 2's model; NULL sorts last
    assert first == [2, 1, 5]
    for _ in range(5):
        shuffled = pool[:]
        random.shuffle(shuffled)
        assert [p.product_id for p in E.select(shuffled, spec, {}, count=5).picks] == first
    deal = {"filters": [{"attr": "list_price", "op": "gte", "expr": "price * 1.5"}]}
    pool2 = [E.Product(1, "B0TEST0001", "station", 100.0, 1, list_price=160.0),
             E.Product(2, "B0TEST0002", "station", 100.0, 2, list_price=120.0)]
    assert [p.product_id for p in E.select(pool2, deal, {}, count=5).picks] == [1]


def test_ties_break_on_price_then_asin():
    pool = [P(3, price=100, usable_wh=1), P(1, price=100, usable_wh=1), P(2, price=90, usable_wh=1)]
    assert [p.product_id for p in E.sort_products(pool, [{"key": "usable_wh", "dir": "desc"}])] == [2, 1, 3]


def test_budget_and_budget_share():
    pool = [P(1, price=700, usable_wh=2000), P(2, price=400, usable_wh=1000), P(3, price=90, usable_wh=10)]
    spec = {"sort": [{"key": "usable_wh", "dir": "desc"}], "max_budget_share": 0.5}
    sel = E.select(pool, spec, {}, budget_left=1000, tier_budget=1000, count=2)
    assert [p.product_id for p in sel.picks] == [2, 3]          # 700 > 50% of the budget
    assert sel.checks[-1] == "price within 50% of the tier budget and the remaining budget"
    sel = E.select(pool, {"sort": [{"key": "usable_wh", "dir": "desc"}]}, {}, budget_left=1000,
                   tier_budget=1000, count=3)
    assert [p.product_id for p in sel.picks] == [1, 3]          # 700 + 400 would exceed 1000
    # alternatives (cumulative off): each is judged against the budget alone
    sel = E.select(pool, {"sort": [{"key": "usable_wh", "dir": "desc"}]}, {}, budget_left=1000,
                   tier_budget=1000, count=3, cumulative=False)
    assert [p.product_id for p in sel.picks] == [1, 2, 3]


def test_integrity_floor():
    cfg = {"min_price": 1.0, "currencies": ["USD"], "list_price_not_below_price": True,
           "min_category_confidence": 0.5, "asin_regex": "^B[0-9A-Z]{9}$",
           "_mapped": {"category_confidence"}}
    ok = E.Product(1, "B0ABCDEFGH", "station", 10.0, currency="USD", category_confidence=0.9)
    assert E.integrity_problem(ok, cfg) == ""
    cases = {"price": dict(price=0.5), "currency": dict(currency="EUR"),
             "list-price-below-price": dict(list_price=5.0), "category-confidence": dict(category_confidence=0.2),
             "asin": dict(asin="X123")}
    from dataclasses import replace
    for why, change in cases.items():
        assert E.integrity_problem(replace(ok, **change), cfg) == why
    assert E.integrity_problem(replace(ok, currency=None), cfg) == ""   # NULL currency = US marketplace


def _tier_pool():
    return [P(1, price=499, model=1, usable_wh=900), P(11, kind="panel", price=199, model=11, rated_w=100),
            P(12, kind="panel", price=249, model=12, rated_w=200), P(21, kind="adapter", price=29, model=21),
            P(4, price=2199, model=4, usable_wh=3000)]


PLAN = [{"role": "station", "max_budget_share": 0.8,
         "filters": [{"attr": "kind", "op": "eq", "value": "station"},
                     {"attr": "usable_wh", "op": "gte", "value": "{tier.required_wh}"}],
         "sort": [{"key": "usable_wh / price", "dir": "desc"}]},
        {"role": "panel", "filters": [{"attr": "kind", "op": "eq", "value": "panel"}],
         "compat": {"relation": "charges_from", "with": "station", "side": "b"},
         "sort": [{"key": "rated_w / price", "dir": "desc"}]},
        {"role": "cable", "via_of": {"relation": "charges_from", "a_role": "station", "b_role": "panel"}}]

COMPAT_ROWS = [{"relation": "charges_from", "a_model_id": 1, "b_model_id": 11, "verdict": "fits"},
               {"relation": "charges_from", "a_model_id": 1, "b_model_id": 12, "verdict": "fits_with_adapter",
                "via_model_id": 21},
               {"relation": "charges_from", "a_model_id": 4, "b_model_id": 12, "verdict": "fits"}]


def _picks(out) -> list:
    return [o.product.product_id if o.product else None for o in out]


def test_ladder_dependencies_via_and_empty_reasons():
    compat = E.CompatIndex(COMPAT_ROWS)
    out = E.fill_tier({"id": 1, "budget": 800, "params": {"required_wh": 800}}, {"slug": "x"}, PLAN, _tier_pool(),
                      compat=compat, enforce_budget=True, view="v")
    assert [(o.role, o.product.product_id if o.product else None) for o in out] == [
        ("station", 1), ("panel", 12), ("cable", 21)]
    out = E.fill_tier({"id": 2, "budget": 5000, "params": {"required_wh": 2500}}, {}, PLAN, _tier_pool(),
                      compat=compat, enforce_budget=True, view="v")
    assert [(o.role, o.product.product_id if o.product else None) for o in out] == [
        ("station", 4), ("panel", 12), ("cable", None)]
    # a direct connection: the kit is complete, the slot is not a gap
    assert out[2].reason == "direct connection: no adapter needed"
    assert out[2].evidence == {"status": "not-needed", "view": "v"}
    out = E.fill_tier({"id": 3, "budget": 300, "params": {"required_wh": 5000}}, {}, PLAN, _tier_pool(),
                      compat=compat, enforce_budget=True, view="v")
    assert all(o.product is None for o in out)
    assert out[0].reason == "no eligible candidate: usable_wh >= 5000"
    assert "usable_wh >= 5000 -> 0" in out[0].trace          # counts: --plan only
    assert out[1].reason == "depends on the station slot, which is empty"
    assert out[0].evidence == {"status": "empty", "view": "v"}
    out = E.fill_tier({"id": 4, "budget": None, "params": {"required_wh": 500}}, {}, PLAN, _tier_pool(),
                      compat=compat, enforce_budget=True, view="v")
    assert all(o.product is None and "no budget" in o.reason for o in out)


def test_a_panel_that_needs_an_adapter_must_fit_the_budget_with_it():
    compat = E.CompatIndex(COMPAT_ROWS[:2])
    pool = _tier_pool()
    plan = copy.deepcopy(PLAN)
    plan[1]["compat"]["include_via_in_budget"] = True
    tier = {"id": 1, "budget": 760, "params": {"required_wh": 800}}     # 499 + 249 + 29 = 777 > 760
    greedy = E.fill_tier(tier, {}, PLAN, pool, compat=compat, enforce_budget=True, view="v")
    assert _picks(greedy) == [1, 12, None]                                 # adapter unaffordable
    whole = E.fill_tier(tier, {}, plan, pool, compat=compat, enforce_budget=True, view="v")
    assert _picks(whole) == [1, 11, None]
    assert whole[2].reason == "direct connection: no adapter needed"
    no_adapter = [p for p in pool if p.product_id != 21]                  # the adapter is not eligible
    out = E.fill_tier({"id": 2, "budget": 5000, "params": {"required_wh": 800}}, {}, plan, no_adapter,
                      compat=compat, enforce_budget=True, view="v")
    assert _picks(out)[:2] == [1, 11]


def test_adapter_money_is_reserved_until_the_via_slot_buys_it():
    """A slot between the panel and its adapter cannot spend the adapter's money."""
    compat = E.CompatIndex(COMPAT_ROWS[:2])
    plan = copy.deepcopy(PLAN[:2]) + [
        {"role": "expansion", "filters": [{"attr": "kind", "op": "eq", "value": "battery"}]},
        copy.deepcopy(PLAN[2])]
    plan[1]["compat"]["include_via_in_budget"] = True
    tier = {"id": 1, "budget": 800, "params": {"required_wh": 800}}
    pool = _tier_pool() + [P(31, kind="battery", price=50, model=31)]
    out = E.fill_tier(tier, {}, plan, pool, compat=compat, enforce_budget=True, view="v")
    # station 499 + panel 249 + its adapter 29 = 777: the $50 battery does not fit in the $23 left
    assert _picks(out) == [1, 12, None, 21]
    assert out[2].reason == "no eligible candidate: price within the tier's remaining budget"
    cheap = _tier_pool() + [P(32, kind="battery", price=20, model=32)]
    assert _picks(E.fill_tier(tier, {}, plan, cheap, compat=compat, enforce_budget=True, view="v")) == [1, 12, 32, 21]
    # an adapter no slot buys stays committed: the battery still cannot take its money
    no_cable = plan[:3]
    assert _picks(E.fill_tier(tier, {}, no_cable, pool, compat=compat, enforce_budget=True, view="v")) == [1, 12, None]


def test_own_budget_alternatives_spend_nothing_of_the_kit():
    plan = [PLAN[0],
            {"role": "alt", "budget": "own", "max_budget_share": 0.8,
             "filters": [{"attr": "kind", "op": "eq", "value": "station"},
                         {"attr": "model_id", "op": "ne", "value": "{slot.station.model_id|}"}],
             "sort": [{"key": "price", "dir": "asc"}]},
            {"role": "panel", "filters": [{"attr": "kind", "op": "eq", "value": "panel"}],
             "sort": [{"key": "price", "dir": "asc"}]}]
    pool = [P(1, price=499, usable_wh=900), P(2, price=599, usable_wh=1000),
            P(11, kind="panel", price=199, model=11)]
    out = E.fill_tier({"id": 1, "budget": 800, "params": {"required_wh": 800}}, {}, plan, pool,
                      compat=None, enforce_budget=True, view="v")
    # the $599 alternative fits 80% of $800 on its own; the panel still has 800 - 499 left
    assert _picks(out) == [1, 2, 11]


def test_compat_edges_must_work_with_one_unit():
    """A slot is one unit: series / parallel-only fits never qualify, a
    fits_with_adapter edge needs 1 x 1 to work, and an edge whose safety
    check could not run is skipped by default."""
    station = P(1, price=499, model=1, usable_wh=900)
    opts_series_only = {"series": 2, "parallel": 1, "options": [
        {"series": 1, "parallel": 1, "outcome": "no_fit"}, {"series": 2, "parallel": 1, "outcome": "fits"}]}
    opts_single_ok = {"series": 2, "parallel": 1, "options": [
        {"series": 1, "parallel": 1, "outcome": "fits"}, {"series": 2, "parallel": 1, "outcome": "fits"}]}
    rows = [
        {"relation": "r", "a_model_id": 1, "b_model_id": 11, "verdict": "fits_in_series",
         "config": {"series": 2, "parallel": 1}},
        {"relation": "r", "a_model_id": 1, "b_model_id": 12, "verdict": "fits_with_adapter", "via_model_id": 21,
         "config": opts_series_only},
        {"relation": "r", "a_model_id": 1, "b_model_id": 13, "verdict": "fits_with_adapter", "via_model_id": 21,
         "config": opts_single_ok},
        {"relation": "r", "a_model_id": 1, "b_model_id": 14, "verdict": "fits", "config": {"series": 3}},
        {"relation": "r", "a_model_id": 1, "b_model_id": 15, "verdict": "fits",
         "config": {"flags": ["computed-check-unknown"]}},
        {"relation": "r", "a_model_id": 1, "b_model_id": 16, "verdict": "fits_in_parallel", "config": {}},
    ]
    idx = E.CompatIndex(rows)
    pool = [P(m, kind="panel", price=100, model=m) for m in (11, 12, 13, 14, 15, 16)]
    spec = {"compat": {"relation": "r", "with": "station", "side": "b"}}
    got = E.select(pool, spec, {}, picks={"station": station}, compat=idx, count=10)
    assert sorted(p.product_id for p in got.picks) == [13, 14]       # 14: one unit fits, 2+ is only advice
    everything = {"compat": {"relation": "r", "with": "station", "side": "b",
                             "verdicts": ["fits", "fits_with_adapter", "fits_in_series", "fits_in_parallel"]}}
    got = E.select(pool, everything, {}, picks={"station": station}, compat=idx, count=10, exclude_flags=())
    assert sorted(p.product_id for p in got.picks) == [13, 14, 15]   # labels never buy a second unit
    assert E.single_unit_works("fits_with_adapter", {}) is True       # a statement without a configuration
    assert E.single_unit_works("fits_with_adapter", {"series": 1, "parallel": 2}) is False
    assert E.single_unit_works("over_limit", {}) is False


def test_duplicate_compat_rows_merge_conservatively_whatever_their_order():
    rows = [
        {"relation": "r", "a_model_id": 1, "b_model_id": 2, "verdict": "fits", "rule_id": "a",
         "confidence": "computed"},
        {"relation": "r", "a_model_id": 1, "b_model_id": 2, "verdict": "fits_with_adapter", "via_model_id": 9,
         "rule_id": "op", "confidence": "manual"},
        {"relation": "r", "a_model_id": 1, "b_model_id": 3, "verdict": "fits", "rule_id": "op",
         "confidence": "manual"},
        {"relation": "r", "a_model_id": 1, "b_model_id": 3, "verdict": "over_limit", "rule_id": "z",
         "confidence": "computed"},
    ]
    for _ in range(6):
        random.shuffle(rows)
        idx = E.CompatIndex(rows)
        assert idx.edge("r", 1, 2).verdict == "fits_with_adapter" and idx.via("r", 1, 2) == (True, 9)
        assert idx.verdict("r", 1, 3) == "over_limit"       # a failure is a floor no statement lifts
        assert idx.duplicate_pairs == 2
        assert idx.signature() == E.CompatIndex(sorted(rows, key=repr)).signature()


def test_substitutes_window_subject_rules_and_same_model():
    pool = [P(2, model=2, price=999, usable_wh=1700, cont_w=2200), P(3, model=3, price=1099, usable_wh=1750, cont_w=2400),
            P(31, model=31, price=899, usable_wh=1600, cont_w=1800), P(41, model=41, price=500, usable_wh=1200,
                                                                         cont_w=3000)]
    rules = {"station": {"constraints": [{"attr": "usable_wh", "op": "within_pct", "value": 15},
                                         {"attr": "cont_w", "op": "gte_subject"}],
                         "sort": [{"key": "price", "dir": "asc"}]}}
    subj = [E.Subject(7, "station", model_id=2, unbuyable=True, attrs={"usable_wh": 1700, "cont_w": 2200}),
            E.Subject(8, "station", model_id=9, attrs={"usable_wh": None, "cont_w": 2000}),
            E.Subject(9, "panel", attrs={})]
    built, why = E.build_substitutes(subj, pool, rules, max_per_subject=3, same_model="prefer")
    assert [o.product.product_id for o in built["7"]] == [2, 3]     # same model first, then cheapest match
    assert built["7"][0].reason == "same model, another listing"
    assert built["7"][1].reason == "usable_wh within ±15% of the subject's; cont_w >= subject"
    assert built["8"] == [] and why["8"] == "subject has no usable_wh"
    assert "9" not in built                                          # no rule for panels
    built, _ = E.build_substitutes(subj[:1], pool, rules, same_model="exclude")
    assert [o.product.product_id for o in built["7"]] == [3]        # 1600 Wh is in the window but only 1800 W


def test_a_subject_price_band_uses_the_mapped_subject_attribute():
    """A subject-anchored price band: the subject's own price attribute
    (when the site maps one) is what a subject comparison reads."""
    pool = [P(2, model=2, price=999, usable_wh=1700), P(3, model=3, price=1399, usable_wh=1750)]
    rules = {"station": {"constraints": [{"attr": "usable_wh", "op": "within_pct", "value": 15},
                                         {"attr": "price", "op": "lte_subject"}]}}
    subj = [E.Subject(7, "station", model_id=9, attrs={"usable_wh": 1700, "price": 1200}),
            E.Subject(8, "station", model_id=9, attrs={"usable_wh": 1700, "price": None})]
    built, why = E.build_substitutes(subj, pool, rules)
    assert [o.product.product_id for o in built["7"]] == [2]
    assert built["7"][0].reason == "usable_wh within ±15% of the subject's; price <= subject"
    assert built["8"] == [] and why["8"] == "subject has no price"   # unknown price: no near-miss
    assert E.Subject(1, "x", model_id=5, attrs={"model_id": 6}).get("model_id") == 6


def test_fill_list_says_why_a_list_is_short():
    pool = [P(1, price=100), P(2, price=300)]
    spec = {"count": 3, "filters": [{"attr": "price", "op": "lte", "value": "{list.max_price}"}]}
    lf = E.fill_list({"id": 1, "params": {"max_price": 200}}, spec, pool)
    assert [o.product.product_id for o in lf.outputs] == [1] and lf.size == 3 and lf.short
    assert lf.reason == "only 1 of 3 position(s) could be filled"
    lf = E.fill_list({"id": 2, "params": {}}, spec, pool)
    assert lf.outputs == [] and lf.reason == "missing parameter list.max_price: slot left empty"


# ---------------------------------------------------------------------------
# Settings (no DB)
# ---------------------------------------------------------------------------

def test_example_site_yaml_is_a_valid_template():
    st = S.load_settings(str(AGENT_DIR / "site.example.yaml"), agent_id=AGENT_ID)
    assert st.publish_key == PUBLISH_KEY and st.dsn_env == "DATABASE_URL_EXAMPLE_SITE"
    assert st.lock_key == f"pick-builder:{AGENT_ID}"
    assert st.warnings == ()                       # every integrity column mapped
    assert st.block["compat"]["fit_verdicts"] == ["fits", "fits_with_adapter"]


def test_defaults_are_niche_neutral_and_single_unit():
    import framework.core.compat_graph as cg
    assert S.DEFAULTS["compat"]["fit_verdicts"] == [cg.FITS, cg.FITS_WITH_ADAPTER]
    assert S.DEFAULTS["compat"]["exclude_flags"] == ["computed-check-unknown"]
    assert S.DEFAULTS["ladders"]["tiers"]["columns"]["budget"] == "budget"


def test_manifest_and_goals():
    from framework.core.goals import _validate_goals_doc
    m = json.loads((AGENT_DIR / "manifest.json").read_text())
    assert m["id"] == "pick-builder-agent" and m["enabled"] is False and "cron_expr" not in m
    doc = json.loads((AGENT_DIR / "goals.json").read_text())
    _validate_goals_doc(doc)
    assert 3 <= len(doc["goals"]) <= 7
    tm = {g["target_metric"]: g["metric"] for g in doc["goals"]}
    assert tm["ineligible_in_picks"]["target"] == 0 and tm["ineligible_in_picks"]["direction"] == "decrease"
    assert set(tm) == {"ineligible_in_picks", "slots_filled_pct", "unbuyable_with_substitute_pct",
                       "pick_publish_failed", "pool_integrity_rejects"}


@pytest.mark.parametrize("mutate,needle", [
    (lambda b: b["ladders"]["plans"]["outage-kit"][0]["filters"].append({"attr": "usable_wh", "op": "near",
                                                                          "value": 1}), "is not one of"),
    (lambda b: b["ladders"]["plans"]["outage-kit"][0]["filters"].append({"attr": "nope", "op": "eq", "value": 1}),
     "not an eligibility column or attribute"),
    (lambda b: b["ladders"]["plans"]["outage-kit"].insert(0, {"role": "cable2", "via_of": {
        "relation": "charges_from", "a_role": "station", "b_role": "panel"}}), "not an earlier slot"),
    (lambda b: b["lists"]["kinds"]["best-under"].update({"sort": [{"key": "usable_wh /", "dir": "desc"}]}),
     "sort[0].key"),
    (lambda b: b["lists"]["kinds"]["best-under"].update({"compat": {"relation": "x", "with": "station"}}),
     "only ladder slots"),
    (lambda b: b["lists"]["kinds"]["best-under"].update({"budget": "own"}), "only ladder slots"),
    (lambda b: b["eligibility"].update({"relation": ""}), "eligibility.relation"),
    (lambda b: b["eligibility"].update({"where": "e.price > 1; DELETE FROM x"}), "may not contain ';'"),
    (lambda b: [b[k].update({"enabled": False}) for k in ("ladders", "lists", "substitutes")], "nothing to build"),
    (lambda b: b["substitutes"]["rules"]["station"]["constraints"].append({"attr": "rated_w",
                                                                           "op": "gte_subject"}),
     "the subject has no attribute"),
    # a subject comparison on price reads the subject's MAPPED price attribute: unmapped -> rejected
    (lambda b: b["substitutes"]["rules"]["station"]["constraints"].append({"attr": "price",
                                                                           "op": "lte_subject"}),
     "the subject has no attribute 'price'"),
    (lambda b: b["eligibility"]["columns"].update({"price": None}), "price: required"),
    (lambda b: b["compat"].setdefault("columns", {}).update({"config": None}), "compat.columns.config: required"),
    (lambda b: b["ladders"]["plans"]["outage-kit"][1]["compat"].update({"verdicts": ["fits", "fits_in_series"]}),
     "a slot is one unit"),
    (lambda b: b["compat"].update({"fit_verdicts": ["fits", "fitz"]}), "not fit verdicts"),
    # stored reasons may never carry a value from the Amazon API
    (lambda b: b["ladders"]["plans"]["outage-kit"][1].update({"reason": "{rated_w:,.0f} W for ${price:,.0f}"}),
     "come from the Amazon API"),
    (lambda b: b["ladders"]["plans"]["outage-kit"][2].update({"reason": "fits the ${slot.station.list_price}"}),
     "come from the Amazon API"),
    (lambda b: b["substitutes"]["rules"]["station"].update({"reason": "{currency} {usable_wh}"}),
     "come from the Amazon API"),
    (lambda b: (b["eligibility"].update({"never_store": ["brand"]}),
                b["lists"]["kinds"]["best-for"].update({"reason": "by {brand}"})), "never_store"),
    (lambda b: b["eligibility"].update({"never_store": ["title"]}), "not an eligibility attribute"),
])
def test_semantic_validation(mutate, needle):
    block = example_block()
    mutate(block)
    with pytest.raises(S.SettingsError) as exc:
        S.build_settings(block, agent_id=AGENT_ID)
    assert needle in str(exc.value)


def test_a_mapped_subject_price_is_accepted():
    block = example_block()
    block["substitutes"]["subjects"]["attributes"]["price"] = "last_price"
    block["substitutes"]["rules"]["station"]["constraints"].append({"attr": "price", "op": "lte_subject"})
    S.build_settings(block, agent_id=AGENT_ID)


def test_dsn_env_must_be_a_variable_name_and_is_never_echoed():
    secret = "postgresql://admin:hunter2@db.example:5432/site"
    block = example_block()
    block["database"]["dsn_env"] = secret
    with pytest.raises(S.SettingsError) as exc:
        S.build_settings(block, agent_id=AGENT_ID)
    assert "NAME of an environment variable" in str(exc.value) and "hunter2" not in str(exc.value)
    block["database"]["dsn_env"] = "lower_case_name"
    with pytest.raises(S.SettingsError, match="NAME of an environment variable"):
        S.build_settings(block, agent_id=AGENT_ID)


def test_unmapped_integrity_columns_are_warned_about():
    block = example_block()
    for c in ("list_price", "currency", "category_confidence"):
        block["eligibility"]["columns"][c] = None
    st = S.build_settings(block, agent_id=AGENT_ID)
    text = " ".join(st.warnings)
    assert "currency is not mapped" in text and "list_price is not mapped" in text
    assert "category_confidence is not mapped" in text


def test_schema_rejects_unknown_keys():
    block = example_block()
    block["publish"]["destination"] = "x"
    with pytest.raises(S.SettingsError, match="Additional properties"):
        S.build_settings(block, agent_id=AGENT_ID)


def test_cli_config_and_sql_are_offline(capsys):
    example = str(AGENT_DIR / "site.example.yaml")
    assert A.main(["--print-sql", "--site-yaml", example, "--agent-id", AGENT_ID]) == 0
    assert 'FROM "v_pick_eligible" e' in capsys.readouterr().out
    assert A.main(["--config", "--site-yaml", example, "--agent-id", AGENT_ID]) == 0
    assert json.loads(capsys.readouterr().out)["publish_key"] == PUBLISH_KEY


def test_default_connect_opens_a_read_only_session_for_plan(monkeypatch):
    from framework.core import pg_conn
    made = []

    class FakeConn:
        readonly = None

        def set_session(self, **kw):
            self.readonly = kw.get("readonly")

    def fake_connect(dsn, **kw):
        made.append((dsn, kw))
        return FakeConn()

    monkeypatch.setattr(pg_conn, "connect", fake_connect)
    st = S.build_settings(example_block(), agent_id=AGENT_ID)
    conn = REAL_DEFAULT_CONNECT(st, environ={DSN_ENV: "dbname=x"}, readonly=True)
    assert conn.readonly is True and made[0][0] == "dbname=x"
    assert REAL_DEFAULT_CONNECT(st, environ={DSN_ENV: "dbname=x"}).readonly is None
    with pytest.raises(RuntimeError, match=DSN_ENV):
        REAL_DEFAULT_CONNECT(st, environ={})


# ---------------------------------------------------------------------------
# The DSN never reaches a run record, framework storage, a log or the CLI
# (the cases and the checker are the spec-extractor tests')
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("dsn,cls,message", LIBPQ_ECHOES)
def test_connect_error_text_withholds_every_echo_of_the_password(dsn, cls, message):
    psycopg2 = pytest.importorskip("psycopg2")
    text = A.connect_error_text(getattr(psycopg2, cls)(message), dsn_env=DSN_ENV, dsn=dsn)
    assert PW_TAIL not in text and text.startswith(f"cannot connect to the site DB (${DSN_ENV}): {cls}: ")


def test_the_redaction_helpers_are_the_spec_extractors_copy():
    """Both engines carry the same helpers (one framework copy is the
    post-freeze cleanup): they must not drift apart."""
    def block(path: Path) -> str:
        src = path.read_text()
        return src[src.index("class SiteDBConnectError"):src.index("def default_connect")]

    assert block(AGENT_DIR / "agent.py") == block(ROOT / "agents" / "spec-extractor-agent" / "agent.py")


@pytest.mark.parametrize("case", ["raw-percent", "keyword-space", "at-sign", "refused"])
def test_a_bad_dsn_never_reaches_the_run_record_storage_or_logs(case, tmp_path, storage, monkeypatch, capsys):
    pytest.importorskip("psycopg2")
    from framework.core import pg_conn
    if case == "raw-percent":            # a generated password with '%', pasted into a URI unencoded
        dsn = f"postgresql://site_rw:Zq9%x!{PW_TAIL}@127.0.0.1:1/site"
    elif case == "keyword-space":
        dsn = f"host=127.0.0.1 port=1 user=site_rw password=Ab3 {PW_TAIL} dbname=site"
    elif case == "at-sign":              # libpq would resolve the broken host name: its message, offline
        dsn = f"postgresql://site_rw:Ab3@{PW_TAIL}@127.0.0.1:1/site"
        message = LIBPQ_ECHOES[3][2].replace("db.example", "127.0.0.1")

        def resolve_fails(_dsn, **_kw):
            import psycopg2
            raise psycopg2.OperationalError(message)

        monkeypatch.setattr(pg_conn, "connect", resolve_fails)
    else:                                # a closed local port: refused, and the diagnosis survives
        dsn = f"postgresql://site_rw:{PW_TAIL}@127.0.0.1:{_free_port()}/site"
    ag = A.PickBuilderAgent(agent_id=AGENT_ID, storage=storage, settings=S.build_settings(example_block(),
                                                                                         agent_id=AGENT_ID),
                            connect=REAL_DEFAULT_CONNECT, environ={DSN_ENV: dsn}, clock=lambda: NOW)
    ag.auto_recovery_enabled = False
    res = ag.run_once()
    assert res.status == "failure" and res.summary.startswith(f"cannot connect to the site DB (${DSN_ENV})")
    if case == "refused":
        assert "Connection refused" in res.summary
    out = capsys.readouterr()
    assert_secret_kept_out(res, tmp_path, out.out, out.err)
    assert storage.read_json(PUBLISH_KEY) is None


def test_cli_plan_prints_a_redacted_connect_error(monkeypatch, capsys):
    monkeypatch.setattr(A, "default_connect", REAL_DEFAULT_CONNECT)
    monkeypatch.setenv("DATABASE_URL_EXAMPLE_SITE", f"postgresql://site_rw:Zq9%x!{PW_TAIL}@127.0.0.1:1/site")
    assert A.main(["--plan", "--site-yaml", str(AGENT_DIR / "site.example.yaml"), "--agent-id", AGENT_ID]) == 1
    out = capsys.readouterr()
    assert PW_TAIL not in out.out + out.err and "Traceback" not in out.err
    assert out.err.startswith("cannot connect to the site DB ($DATABASE_URL_EXAMPLE_SITE): ProgrammingError")


# ---------------------------------------------------------------------------
# End to end against the throwaway Postgres
# ---------------------------------------------------------------------------

def test_first_run_fills_from_the_eligibility_view_only(h):
    res = h.run()
    assert res.status == "success", res.summary
    m = res.metrics
    slots = h.slots()
    pid = lambda k: slots[k]["product_id"]  # noqa: E731
    # $800 tier: best usable Wh per dollar, its panel, the documented adapter; the alternative
    # (judged on its own budget) finds no other station within 80% of $800
    assert (pid((10, "station", 1)), pid((10, "panel", 1)), pid((10, "cable", 1))) == (1, 12, 21)
    assert pid((10, "alt", 1)) is None
    assert slots[(10, "alt", 1)]["reason"] == "no eligible candidate: price within 80% of the tier budget"
    assert slots[(10, "station", 1)]["reason"] == "Most usable energy per dollar at 900 Wh"
    assert slots[(10, "panel", 1)]["reason"] == "200 W panel that charges the station"
    assert slots[(10, "station", 1)]["eligibility"]["status"] == "filled"
    # $5,000 tier: a direct connection makes the cable slot unnecessary (not a gap)
    assert (pid((11, "station", 1)), pid((11, "panel", 1)), pid((11, "cable", 1))) == (4, 12, None)
    assert slots[(11, "cable", 1)]["reason"] == "direct connection: no adapter needed"
    assert slots[(11, "cable", 1)]["eligibility"] == {"status": "not-needed", "view": "v_pick_eligible"}
    assert pid((11, "alt", 1)) is None        # Delta's EUR listing is not eligible: never a fallback
    # $300 tier asking for 5 kWh: every slot empty, each says why
    assert all(pid((12, r, 1)) is None for r in ("station", "panel", "cable", "alt"))
    assert slots[(12, "station", 1)]["reason"] == "no eligible candidate: usable_wh >= 5000"
    assert slots[(12, "panel", 1)]["reason"] == "depends on the station slot, which is empty"
    assert all("no budget" in slots[(13, r, 1)]["reason"] for r in ("station", "panel", "cable", "alt"))
    # the RV ladder has no plan: only swept (the stale listing cleared, the eligible panel kept)
    assert pid((20, "station", 1)) is None and "no longer eligible" in slots[(20, "station", 1)]["reason"]
    assert pid((20, "panel", 1)) == 11
    assert not any(k[0] == 30 for k in slots)  # draft ladder untouched
    # lists
    assert h.items(1) == [5, 1, 2]            # one listing per model (Alpha's second listing skipped)
    assert h.items(2) == [2, 3, 4]            # 3 of the default 5 positions
    assert h.items(3) == []                   # no max_price: never loosened into "any price"
    assert h.items(4) == [5]                  # no selector for 'deal': only swept (the EUR item removed)
    assert m["lists_short"] == 2 and m["lists_empty"] == 1
    assert any("best-under-no-cap: 0/5 filled: missing parameter list.max_price" in w
               for w in res.summary_md.splitlines())
    # substitutes: the stale Beta listing -> Beta's live listing first, then Gamma
    subs = h.subs()
    assert subs[7] == [2, 3] and subs[8] == [3] and subs[9] == [4] and subs[1] == [6]
    assert 5 not in subs and 10 not in subs   # nothing in the +-15% window / no specs: no near-miss
    reasons = {r["product_id"]: r["reason"] for r in h.q("SELECT * FROM product_substitutes WHERE rank = 2")}
    assert reasons[7] == "1,750 Wh and 2,400 W continuous"
    assert m["unbuyable_subjects"] == 4 and m["unbuyable_with_substitute"] == 3
    assert m["unbuyable_with_substitute_pct"] == 75.0
    # every live pick is eligible, and the metrics add up (the not-needed slot is no gap)
    assert m["ineligible_in_picks"] == 0 and m["ineligible_found"] == 2
    assert m["pool_size"] == 9 and m["pool_integrity_rejects"] == 0
    assert m["ladder_slots_total"] == 18 and m["ladder_slots_filled"] == 6
    assert m["ladder_slots_not_needed"] == 1 and m["ladder_slots_empty"] == 11
    assert m["ladder_slots_filled_pct"] == round(100 * 6 / 17, 2)
    assert m["list_positions_total"] == 3 + 5 + 5 and m["list_items_filled"] == 6
    assert m["slots_filled_pct"] == round(100 * 12 / 30, 2)
    # the pick-ASIN list, in ladder (tier budget order) then list order
    doc = h.storage.read_json(PUBLISH_KEY)
    assert doc["asins"] == ["B0ALPHA001", "B0PANEL201", "B0ADAPTR01", "B0DELTA001", "B0PANEL101",
                            "B0BETA0001", "B0GAMMA001", "B0EPSILN01"]
    assert doc["source"] == AGENT_ID and m["pick_asins_published"] == 8 and m["pick_publish_failed"] == 0
    assert h.state()["publish_pending"] is False


def _stored_texts(h) -> list[str]:
    out = []
    for r in h.q("SELECT reason, eligibility::text AS ev FROM ladder_slots"):
        out += [r["reason"] or "", r["ev"] or ""]
    out += [r["reason"] or "" for r in h.q("SELECT reason FROM pick_list_items")]
    out += [r["reason"] or "" for r in h.q("SELECT reason FROM product_substitutes")]
    return out


def test_no_amazon_value_or_derived_number_is_ever_stored(h):
    """Amendment 2: only the ASIN and our own outcome persist past 24h. A
    reason or evidence row must not carry a price, a currency symbol, or a
    remaining budget (which is derived from the earlier picks' prices)."""
    h.run()
    h.q("UPDATE amazon_item_cache SET fetched_at = now() - interval '25 hours' WHERE asin = 'B0ALPHA001'")
    h.run()                                   # a kit priced from other listings
    texts = _stored_texts(h)
    assert texts
    for t in texts:
        if t in ("hand-picked", "old deal"):
            continue
        assert "$" not in t, t
        numbers = set(re.findall(r"\d+(?:\.\d+)?", t.replace(",", "")))
        assert not numbers & PRICES, (t, numbers & PRICES)
        # a share of the tier budget ("80% of the tier budget") is config; an amount is not
        assert not re.search(r"(price|budget)\D{0,12}\d", re.sub(r"\d+(?:\.\d+)?%", "PCT", t), re.I), t


def test_empty_slot_text_does_not_churn_with_the_pool(h):
    """Stored reasons carry no counts: a product that changes no pick
    changes no row (the old 'N eligible' trace rewrote every tier)."""
    h.run()
    h.q("INSERT INTO models (id, kind, model_name) VALUES (7, 'station', 'Tiny 100')")
    h.q("INSERT INTO model_specs (model_id, capacity_wh, usable_wh, ac_cont_w) VALUES (7, 120, 100, 100)")
    h.q("INSERT INTO products (id, asin, model_id, kind, brand) VALUES (30, 'B0TINY0001', 7, 'station', 'tiny')")
    h.q("INSERT INTO amazon_item_cache (asin, price, currency, outcome, fetched_at) "
        "VALUES ('B0TINY0001', 4999, 'USD', 'priced', now())")
    res = h.run()
    assert res.status == "success" and not res.short_circuited and res.metrics["pool_size"] == 10
    assert res.metrics["groups_written"] == 0, res.summary_md


def test_price_refresh_priority_reads_the_published_list(h):
    import psycopg2
    h.run()
    cfg = prp.resolve_config(file_cfg={}, site_cfg={
        "enabled": True, "catalog": {"table": "products", "asin_col": "asin", "fresh_cols": []},
        "groups": [{"name": "picks", "kind": "storage_list", "key": PUBLISH_KEY, "gate_hours": 12}],
        "long_tail": {"enabled": False}})
    conn = psycopg2.connect(h.dsn)
    try:
        tiers = prp.collect_tiers(conn, cfg, storage=h.storage)
    finally:
        conn.close()
    assert tiers.errors == {}
    assert tiers.asins() == h.storage.read_json(PUBLISH_KEY)["asins"]


def test_second_run_short_circuits(h):
    h.run()
    before = h.q("SELECT * FROM ladder_slots ORDER BY tier_id, role, rank")
    res = h.run()
    assert res.status == "success" and res.short_circuited is True
    assert "short-circuited" in res.summary and res.metrics["ineligible_in_picks"] == 0
    assert h.q("SELECT * FROM ladder_slots ORDER BY tier_id, role, rank") == before
    assert res.metrics["ladder_slots_filled"] == 6 and res.metrics["pick_asins_published"] == 8
    assert res.metrics["ladder_slots_not_needed"] == 1


class FlakyStorage:
    """Framework storage whose writes to one key fail while ``broken``."""

    def __init__(self, inner, key: str):
        self.inner = inner
        self.key = key
        self.broken = False

    def write_json(self, key, doc, *a, **k):
        if self.broken and key == self.key:
            raise OSError("storage unavailable")
        return self.inner.write_json(key, doc, *a, **k)

    def __getattr__(self, name):
        return getattr(self.inner, name)


def test_a_failed_publish_is_retried_by_the_next_unchanged_run(h):
    flaky = FlakyStorage(h.storage, PUBLISH_KEY)
    h.storage = flaky
    h.run()
    first = flaky.read_json(PUBLISH_KEY)["asins"]
    # the picks change while storage is down: the run reports it, the old list stays readable
    h.q("UPDATE amazon_item_cache SET fetched_at = now() - interval '25 hours' WHERE asin = 'B0ALPHA001'")
    flaky.broken = True
    res = h.run()
    assert res.status == "success" and res.metrics["pick_publish_failed"] == 1
    assert res.metrics["pick_asins_published"] == 0 and "NOT published" in res.summary
    assert flaky.read_json(PUBLISH_KEY)["asins"] == first and h.state()["publish_pending"] is True
    # storage is back and nothing changed: the run must not short-circuit on the stale list
    flaky.broken = False
    res = h.run()
    assert res.status == "success" and not res.short_circuited and res.metrics["pick_publish_failed"] == 0
    doc = flaky.read_json(PUBLISH_KEY)
    assert doc["asins"][0] == "B0ALPHA002" and "B0ALPHA001" in doc["asins"]   # dropped pick kept one run
    assert h.state()["publish_pending"] is False
    assert h.run().short_circuited is True                                     # and now it may skip


def test_a_run_that_raises_keeps_its_state(h):
    h.run()
    published = h.state()["published"]
    h.q("ALTER TABLE ladder_slots ADD CONSTRAINT no_writes CHECK (rank < 0) NOT VALID")
    h.q("UPDATE amazon_item_cache SET fetched_at = now() - interval '25 hours' WHERE asin = 'B0ALPHA001'")
    res = h.run()
    assert res.status == "failure" and "CheckViolation" in res.summary
    st = h.state()
    assert st["published"] == published and st.get("inputs_sig")      # not wiped to {}
    assert h.storage.read_json(PUBLISH_KEY)["asins"] == published
    h.q("ALTER TABLE ladder_slots DROP CONSTRAINT no_writes")
    res = h.run()
    assert res.status == "success" and h.slots()[(10, "station", 1)]["product_id"] == 6


def test_a_stale_price_replaces_the_pick_and_keeps_it_published_one_run(h):
    h.run()
    h.q("UPDATE amazon_item_cache SET fetched_at = now() - interval '25 hours' WHERE asin = 'B0ALPHA001'")
    res = h.run()
    assert res.status == "success" and not res.short_circuited
    assert res.metrics["ineligible_found"] >= 1 and res.metrics["ineligible_in_picks"] == 0
    slots = h.slots()
    assert slots[(10, "station", 1)]["product_id"] == 6        # Alpha's other listing, still eligible
    # $529 leaves $271: the 200 W panel needs the $29 adapter (278 > 271), so the kit takes the
    # direct-fit 100 W panel instead of a panel whose adapter would not fit the budget
    assert slots[(10, "panel", 1)]["product_id"] == 11
    assert slots[(10, "cable", 1)]["product_id"] is None
    assert slots[(10, "cable", 1)]["reason"] == "direct connection: no adapter needed"
    assert slots[(10, "alt", 1)]["product_id"] is None
    doc = h.storage.read_json(PUBLISH_KEY)
    assert doc["asins"][0] == "B0ALPHA002" and "B0ALPHA001" in doc["asins"]   # kept for the refresh
    assert doc["counts"]["recently_dropped"] == 2                              # + the adapter
    # one run later it is gone from the list (and the refresh would have repriced it by then)
    h.q("UPDATE amazon_item_cache SET price = 540 WHERE asin = 'B0ALPHA002'")
    h.run()
    assert "B0ALPHA001" not in h.storage.read_json(PUBLISH_KEY)["asins"]


def test_only_changed_groups_are_rewritten(h):
    h.run()
    h.q("UPDATE ladder_slots SET updated_at = '2000-01-01' WHERE tier_id = 11")
    # a price change that reorders nothing writes nothing
    h.q("UPDATE amazon_item_cache SET price = 950 WHERE asin = 'B0BETA0001'")
    res = h.run()
    assert res.metrics["groups_written"] == 0 and res.metrics["rows_inserted"] == 0
    # Gamma at $899 enters the under-$1000 list and leads the price-sorted list: exactly 2 groups
    h.q("UPDATE amazon_item_cache SET price = 899 WHERE asin = 'B0GAMMA001'")
    res = h.run()
    assert res.metrics["groups_written"] == 2
    assert h.items(1) == [5, 3, 1] and h.items(2) == [3, 2, 4]
    stamps = {r["tier_id"]: r["updated_at"] for r in h.q("SELECT tier_id, max(updated_at) AS updated_at "
                                                          "FROM ladder_slots GROUP BY tier_id")}
    assert stamps[11].year == 2000             # unchanged tier: not rewritten


def test_a_series_only_edge_never_fills_a_one_panel_kit(h):
    """compat_graph's fits_in_series / series-only adapter edges need two
    panels; a ladder slot holds one, so the kit takes another panel or none."""
    h.q("UPDATE compat_edges SET verdict = 'fits_in_series', config = '{\"series\": 2, \"parallel\": 1}' "
        "WHERE a_model_id = 1 AND b_model_id = 11")
    h.q("UPDATE compat_edges SET config = '{\"series\": 2, \"parallel\": 1, \"options\": "
        "[{\"series\": 1, \"parallel\": 1, \"outcome\": \"no_fit\"}, {\"series\": 2, \"parallel\": 1, "
        "\"outcome\": \"fits\"}]}' WHERE a_model_id = 1 AND b_model_id = 12")
    res = h.run()
    assert res.status == "success", res.summary
    slots = h.slots()
    assert slots[(10, "station", 1)]["product_id"] == 1
    assert slots[(10, "panel", 1)]["product_id"] is None and slots[(10, "cable", 1)]["product_id"] is None
    assert "one unit" in slots[(10, "panel", 1)]["reason"]
    assert slots[(11, "panel", 1)]["product_id"] == 12          # Delta's direct one-panel fit is untouched


def test_picks_are_deterministic_whatever_the_row_order(storage):
    a, b = fresh_database("pkd"), fresh_database("pkd")
    seed(a)
    seed(b, reverse=True)
    for dsn in (a, b):        # an operator row next to the computed one: merged by precedence, not scan order
        query(dsn, "INSERT INTO compat_edges (a_model_id, b_model_id, relation, verdict, rule_id, rule_version, "
                   "confidence) VALUES (2, 12, 'charges_from', 'over_limit', 'operator', 'v1', 'manual')")
    Harness(storage, a).run()
    from framework.core.storage import LocalFilesystemStorage
    import tempfile
    other = LocalFilesystemStorage(tempfile.mkdtemp(dir=str(Path(storage.root).parent)))
    Harness(other, b).run()
    for sql in ("SELECT tier_id, role, rank, product_id, reason FROM ladder_slots ORDER BY 1, 2, 3",
                "SELECT list_id, rank, product_id, reason FROM pick_list_items ORDER BY 1, 2",
                "SELECT product_id, rank, alt_product_id, reason FROM product_substitutes ORDER BY 1, 2"):
        assert query(a, sql) == query(b, sql)
    assert storage.read_json(PUBLISH_KEY)["asins"] == other.read_json(PUBLISH_KEY)["asins"]


def test_integrity_floor_applies_on_top_of_a_lax_view(storage, db):
    query(db, "CREATE VIEW v_lax AS SELECT p.id AS product_id, p.asin, p.model_id, p.kind, p.brand, c.price, "
              "c.list_price, c.currency, p.category_confidence, s.capacity_wh, s.usable_wh, s.ac_cont_w, s.rated_w "
              "FROM products p JOIN amazon_item_cache c ON c.asin = p.asin "
              "LEFT JOIN model_specs s ON s.model_id = p.model_id")
    query(db, "UPDATE amazon_item_cache SET price = 0.5 WHERE asin = 'B0EPSILN01'")
    block = example_block()
    block["eligibility"]["relation"] = "v_lax"
    res = Harness(storage, db, block).run()
    assert res.status == "success"
    m = res.metrics
    assert m["pool_integrity_rejects"] == 2    # the $0.50 price and the EUR listing
    picked = {r["product_id"] for r in query(db, "SELECT product_id FROM pick_list_items")}
    assert 5 not in picked and 9 not in picked


def test_a_held_lock_skips_the_run(h):
    import psycopg2
    other = psycopg2.connect(h.dsn)
    with other.cursor() as cur:
        cur.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (f"pick-builder:{AGENT_ID}",))
    try:
        res = h.run()
        assert res.metrics == {"skipped_locked": 1} and h.storage.read_json(PUBLISH_KEY) is None
    finally:
        other.close()


def test_plan_mode_writes_and_publishes_nothing(h):
    import psycopg2
    conn = psycopg2.connect(h.dsn)
    try:
        store = ST.PickStore(conn, h.settings())

        class NoStorage:
            def read_json(self, _k):
                return None

            def write_json(self, *_a, **_k):
                raise AssertionError("plan mode published")

        rep = R.PickBuild(h.settings(), store, storage=NoStorage(), prior_state={}, say=lambda *a, **k: None,
                          decide=lambda *a, **k: None, dry_run=True).run()
        # the plan's snapshot is READ ONLY: the server refuses a write in it
        import psycopg2.errors
        store.begin_snapshot(read_only=True)
        with pytest.raises(psycopg2.errors.ReadOnlySqlTransaction):
            with conn.cursor() as cur:
                cur.execute("DELETE FROM ladder_slots")
        conn.rollback()
    finally:
        conn.close()
    assert rep.plan["changes"]["tiers"] >= 4
    assert [r["product_id"] for r in rep.plan["slots"]["10"]][:3] == [1, 12, 21]
    assert h.q("SELECT count(*) AS n FROM product_substitutes")[0]["n"] == 0
    assert h.q("SELECT count(*) AS n FROM ladder_slots")[0]["n"] == 2
    lines = A.plan_lines(rep.plan)
    assert any(l.startswith("tier 11 cable#1: NOT NEEDED") for l in lines)
    assert any(l.startswith("tier 12 station#1: EMPTY") and "usable_wh >= 5000 -> 0" in l for l in lines)
    assert any(l.startswith("list 3: []") and "missing parameter list.max_price" in l for l in lines)


def test_cli_plan_uses_a_read_only_connection(h, monkeypatch, capsys):
    import psycopg2
    seen = []

    def connect(settings, *, environ=None, readonly=False):
        seen.append(readonly)
        conn = psycopg2.connect(h.dsn)
        conn.set_session(readonly=readonly)
        return conn

    monkeypatch.setattr(A, "default_connect", connect)
    assert A.main(["--plan", "--site-yaml", str(AGENT_DIR / "site.example.yaml"), "--agent-id", AGENT_ID]) == 0
    assert seen == [True]
    out = capsys.readouterr().out
    assert "tier 10 station#1: 1" in out and "NOT NEEDED" in out
    assert h.q("SELECT count(*) AS n FROM ladder_slots")[0]["n"] == 2


def test_goal_progress_is_recorded_from_run_metrics(h):
    from framework.core import goals
    doc = json.loads((AGENT_DIR / "goals.json").read_text())
    goals.init_goals(AGENT_ID, doc["goals"], storage=h.storage)
    res = h.run()
    active = {g["id"]: g for g in goals.read_active_goals(AGENT_ID, storage=h.storage)}
    for g in doc["goals"]:
        hist = active[g["id"]].get("progress_history") or []
        assert hist and hist[-1]["value"] == pytest.approx(float(res.metrics[g["target_metric"]]))
