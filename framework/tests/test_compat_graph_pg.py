"""framework.core.compat_graph.EdgeWriter against a real PostgreSQL.

Skipped unless COMPAT_GRAPH_TEST_PG_DSN points at a THROWAWAY database (a
local container, never a site database). Everything happens inside a
temporary schema that is dropped afterwards:

* the reference DDL creates the table;
* a first build is written, read back (jsonb config, bigint[] evidence) and a
  rebuild plans no writes;
* a rule-version bump rewrites every edge of that ruleset;
* a pair that turns unknown is deleted;
* a manual row survives both the plan and a forced upsert (SQL guard);
* a manual fit that fails the cold check replaces its older manual row
  (manual may overwrite manual), and purging a disabled ruleset's rows
  leaves manual rows in place;
* a column map writes a differently named table."""
from __future__ import annotations

import json
import os
import uuid
from types import SimpleNamespace

import pytest

from framework.core import compat_graph as cg

DSN = os.environ.get("COMPAT_GRAPH_TEST_PG_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="set COMPAT_GRAPH_TEST_PG_DSN to a throwaway PostgreSQL")

CLOCK = "2026-11-17T00:00:00Z"


@pytest.fixture
def db():
    psycopg2 = pytest.importorskip("psycopg2")
    c = psycopg2.connect(DSN)
    schema = "cg_test_" + uuid.uuid4().hex[:10]
    with c.cursor() as cur:
        cur.execute(f"CREATE SCHEMA {schema}")
        cur.execute(cg.reference_ddl(f"{schema}.compat_edges"))
    c.commit()
    try:
        yield SimpleNamespace(conn=c, schema=schema)
    finally:
        c.rollback()
        with c.cursor() as cur:
            cur.execute(f"DROP SCHEMA {schema} CASCADE")
        c.commit()
        c.close()


def graph(cold_version=1):
    cfg = cg.resolve_config(file_cfg={}, site_cfg={
        "rules": {"mfr": {"kind": "manufacturer_list", "version": 1},
                  "cold": {"kind": "max_voltage_cold", "version": cold_version,
                           "a_fields": {"max_v": "max_v"}, "b_fields": {"voc": "voc", "tc": "tc"}}},
        "rulesets": {
            "solar": {"version": 1, "relation": "charges_from", "a_kinds": ["station"],
                      "b_kinds": ["panel"], "rules": ["mfr", "cold"],
                      "arrays": {"max_series": 3, "max_parallel": 1, "max_units": 3}},
            "expansion": {"version": 1, "relation": "expands", "a_kinds": ["station"],
                          "b_kinds": ["battery"], "rules": ["mfr"]}}})
    return cg.CompatGraph(cfg, clock=lambda: CLOCK)


def models(voc_5=30.0):
    src = lambda i: {"source_url": f"https://maker.example/{i}", "source_kind": "manufacturer-page",  # noqa: E731
                     "source_id": 1000 + i}
    p5 = {"tc": -0.3} if voc_5 is None else {"voc": voc_5, "tc": -0.3}
    return [cg.ModelSpec.from_values(1, "station", {"max_v": 60}, source=src(1)),
            cg.ModelSpec.from_values(4, "station", {"max_v": 150}, source=src(4)),
            cg.ModelSpec.from_values(2, "panel", {"voc": 21.5}, source=src(2)),
            cg.ModelSpec.from_values(5, "panel", p5, source=src(5)),
            cg.ModelSpec.from_values(3, "battery", {})]


RECS = [cg.FitRecord(a=1, b=3, relation="expands", source_url="https://maker.example/list",
                     source_kind="manufacturer-page", source_id=1099)]


def write(g, db, ms, **plan_kw):
    w = cg.EdgeWriter(db.conn, table=f"{db.schema}.compat_edges")
    plan = g.plan(g.build(ms, RECS), w.load_existing(rule_ids=list(g.rulesets)), **plan_kw)
    w.apply(plan)
    return plan, w


def test_round_trip_version_bump_delete_and_manual_protection(db):
    conn = db.conn
    g = graph()
    plan, w = write(g, db, models())
    assert plan.summary()["upserts"] == 5
    rows = {(r["a_model_id"], r["b_model_id"], r["rule_id"]): r for r in w.load_existing()}
    assert len(rows) == 5
    r12 = rows[(1, 2, "solar")]
    assert r12["verdict"] == "fits" and r12["confidence"] == "computed"
    assert isinstance(r12["config"], dict) and r12["config"]["series"] == 1
    assert r12["config"]["design_temp_c"] == -40.0 and r12["config"]["max_series"] == 2
    with conn.cursor() as cur:
        cur.execute(f"SELECT evidence_source_ids, computed_at FROM {db.schema}.compat_edges "
                    "WHERE a_model_id = 1 AND b_model_id = 2")
        ids, at = cur.fetchone()
    assert ids == [1001, 1002] and at.year == 2026
    assert rows[(1, 3, "expansion")]["confidence"] == "manufacturer-listed"

    # A rebuild changes nothing.
    again, _ = write(g, db, models())
    assert again.upserts == [] and again.deletes == [] and len(again.unchanged) == 5

    # Bumping the cold rule rewrites the solar edges only.
    g2 = graph(cold_version=2)
    bumped, w2 = write(g2, db, models())
    assert len(bumped.recomputed) == 4 and len(bumped.upserts) == 4
    after = {(r["a_model_id"], r["b_model_id"], r["rule_id"]): r for r in w2.load_existing()}
    assert {r["rule_version"] for k, r in after.items() if k[2] == "solar"} == {g2.ruleset_version("solar")}
    assert after[(1, 3, "expansion")]["rule_version"] == g.ruleset_version("expansion")

    # An operator marks one row manual; a pair losing its Voc is deleted, the
    # manual row is untouched by both the plan and a forced upsert.
    with conn.cursor() as cur:
        cur.execute(f"UPDATE {db.schema}.compat_edges SET confidence = 'manual', verdict = 'no_fit' "
                    "WHERE a_model_id = 4 AND b_model_id = 5")
    conn.commit()
    plan3, w3 = write(g2, db, models(voc_5=None))
    assert {(d["a_model_id"], d["b_model_id"]) for d in plan3.deletes} == {(1, 5)}
    assert {(p["a_model_id"], p["b_model_id"]) for p in plan3.protected} == {(4, 5)}
    forced = cg.EdgePlan(upserts=[e for e in g2.build(models(), RECS).edges
                                  if (e.a_model_id, e.b_model_id) == (4, 5)])
    w3.apply(forced)
    final = {(r["a_model_id"], r["b_model_id"], r["rule_id"]): r for r in w3.load_existing()}
    assert (1, 5, "solar") not in final
    assert (final[(4, 5, "solar")]["confidence"], final[(4, 5, "solar")]["verdict"]) == ("manual", "no_fit")


def test_column_map_writes_a_differently_named_table(db):
    conn = db.conn
    t = f"{db.schema}.edges_alt"
    with conn.cursor() as cur:
        cur.execute(f"""CREATE TABLE {t} (
            a_model_id bigint NOT NULL, b_model_id bigint NOT NULL, relation text NOT NULL,
            verdict text NOT NULL, details jsonb NOT NULL, via_model_id bigint, ruleset text NOT NULL,
            ruleset_version text NOT NULL, evidence_source_ids bigint[] NOT NULL DEFAULT '{{}}',
            confidence text NOT NULL, computed_at timestamptz NOT NULL,
            UNIQUE (a_model_id, b_model_id, relation, ruleset))""")
    conn.commit()
    g = graph()
    w = cg.EdgeWriter(conn, table=t, columns={"config": "details", "rule_id": "ruleset",
                                              "rule_version": "ruleset_version"})
    w.apply(g.plan(g.build(models(), RECS), w.load_existing()))
    rows = w.load_existing(rule_ids=["solar"])
    assert len(rows) == 4 and all(isinstance(r["config"], dict) for r in rows)
    assert all(json.dumps(r["config"]) for r in rows)


def test_a_vetoed_manual_fit_replaces_its_old_row_and_orphans_are_purged(db):
    conn = db.conn
    g = graph()
    plan, w = write(g, db, models())
    # An operator's manual fit for an over-volting pair, as an older engine
    # would have stored it: a manual 'fits' row.
    hot = cg.ModelSpec.from_values(6, "panel", {"voc": 49.5, "tc": -0.27},
                                   source={"source_url": "https://maker.example/6",
                                           "source_kind": "manufacturer-page", "source_id": 1006})
    manual = cg.FitRecord(a=1, b=6, relation="charges_from", verdict="fits", origin="manual",
                          statement="operator says fine")
    edge = next(e for e in g.build(models() + [hot], [manual]).edges if (e.a_model_id, e.b_model_id) == (1, 6))
    assert (edge.verdict, edge.confidence) == (cg.OVER_LIMIT, cg.CONF_MANUAL)
    old = cg.Edge(**{**edge.__dict__, "verdict": cg.FITS, "inputs_hash": "old"})
    w.apply(cg.EdgePlan(upserts=[old]))
    rows = {(r["a_model_id"], r["b_model_id"]): r for r in w.load_existing(rule_ids=["solar"])}
    assert rows[(1, 6)]["verdict"] == cg.FITS
    plan2 = g.plan(g.build(models() + [hot], RECS + [manual]), w.load_existing(rule_ids=list(g.rulesets)))
    assert (1, 6, cg.OVER_LIMIT) in {(u.a_model_id, u.b_model_id, u.verdict) for u in plan2.upserts}
    assert plan2.protected == []                     # manual replaces manual
    w.apply(plan2)
    rows = {(r["a_model_id"], r["b_model_id"]): r for r in w.load_existing(rule_ids=["solar"])}
    assert (rows[(1, 6)]["verdict"], rows[(1, 6)]["confidence"]) == (cg.OVER_LIMIT, cg.CONF_MANUAL)

    # Disable the solar ruleset: its rows are orphans, deleted only on request,
    # and an operator's manual row survives the purge (plan and SQL).
    with conn.cursor() as cur:
        cur.execute(f"UPDATE {db.schema}.compat_edges SET confidence = 'manual' "
                    "WHERE a_model_id = 4 AND b_model_id = 2")
    conn.commit()
    off = cg.CompatGraph(cg.merge_config(g.cfg, {"rulesets": {"solar": {"enabled": False}}}),
                         clock=lambda: CLOCK)
    solar_rows = [r for r in w.load_existing() if r["rule_id"] == "solar"]
    listed = off.plan(off.build(models() + [hot], RECS), w.load_existing())
    assert listed.deletes == [] and len(listed.orphaned) == len(solar_rows) == 6
    purge = off.plan(off.build(models() + [hot], RECS), w.load_existing(), purge_orphaned=True)
    w.apply(purge)
    left = {(r["a_model_id"], r["b_model_id"], r["rule_id"]) for r in w.load_existing()}
    assert left == {(1, 3, "expansion"), (4, 2, "solar"), (1, 6, "solar")}   # the manual rows stay
