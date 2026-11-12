"""catalog-audit-agent image/name rule: brand != manufacturer alone never
deactivates a product, normal marketplace patterns are cleared, only a title
or image that names a different product may deactivate, and rows in the
site's protect set come back report-only."""
from __future__ import annotations

import importlib.util as iu
import os
import time
from pathlib import Path

import pytest

_AGENT = Path(__file__).resolve().parents[2] / "agents" / "catalog-audit-agent" / "agent.py"


@pytest.fixture(scope="module")
def mod():
    spec = iu.spec_from_file_location("catalog_audit_agent_image_name", _AGENT)
    m = iu.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


RULE_SQL = ("UPDATE products\nSET is_active = false, updated_at = NOW()\n"
            "WHERE is_active = true\n  AND asin IN ({ids});")


@pytest.fixture
def rule(mod):
    return mod._deep_merge(mod.GUARD_DEFAULTS["image_name_rule"], {
        "deactivate_template": {"table": "products", "action": "deactivate",
                                "sql": RULE_SQL, "filename": "catalog-audit-product-image-matches-name",
                                "quote_ids": True},
        "affected_url_template": "https://shop.test/product/{id}",
    })


def _ident(asin, title, brand, manufacturer):
    return {"id": asin, "title": title, "brand": brand, "manufacturer": manufacturer,
            "description": "", "image_url": ""}


# Reasons are verbatim from the image/name audit (2026-10..11).
WD_PASSPORT = ("Product name 'WD My Passport SSD' indicates Western Digital, but manufacturer field "
               "lists 'Sandisk Technologies, Inc.'; model number prefix 'WD' and 'My Passport' "
               "branding confirm this should be Western Digital, not Sandisk.")
SN850X = ("Product name claims WD_BLACK SN850X (Western Digital gaming brand), but manufacturer is "
          "SanDisk and description redirects to a different product (SanDisk Optimus GX PRO 850X).")
YETI = ("Product name specifies 'Logitech Creators Blue Yeti' but description identifies 'Logitech "
        "G Blue Yeti'—these are distinct product variants in Logitech's Blue Yeti lineup.")
PENTIUM = ("Product name claims an Intel Pentium 4 processor, but manufacturer lists 'SweetWay' (a "
           "reseller/retailer name, not the CPU manufacturer Intel), indicating either a "
           "miscategorized resale or a corrupted product entry.")
CYLEWET = ("Product name specifies brand 'Cylewet' with model 'CYT1078', but manufacturer field "
           "lists 'Qianxin'—conflicting brand claims for the same product.")
VELZTORM = ("Product name claims '1000W PSU' but description explicitly states 'No 1000W PSU'—"
            "direct contradiction on a major component.")
WIRED = ("Product name claims a wireless 2.4GHz controller for SNES/NES consoles, but description "
         "describes a wired USB controller for PC emulators and Raspberry Pi—two different products.")
KVM = ("Product name and manufacturer indicate a KVM switch (StarTech Starview), but the "
       "description is a biography of composer Philip Glass, which is completely unrelated to "
       "computer hardware.")
LEGION = ("Product name claims RTX 5090 (a desktop GPU), which is incompatible with a laptop form "
          "factor and inconsistent with Lenovo Legion Pro 7i's mobile GPU configuration.")
MOLEX = ("Product describes a legacy Molex-to-Floppy power cable, but manufacturer 'Micro SATA "
         "Cables' specializes in SATA connectors, a completely different product category.")
UNKNOWN_MAN = ("Product name explicitly identifies Corsair as the brand, but manufacturer field is "
               "marked unknown, creating an internal inconsistency in product attribution.")


@pytest.mark.parametrize("reason, hint, want", [
    (WIRED, "", "identity"),
    (KVM, "", "description"),
    (CYLEWET, "", "attribution"),
    (PENTIUM, "", "attribution"),
    (MOLEX, "", "attribution"),          # the manufacturer, not the listing, points elsewhere
    (VELZTORM, "", "attribute"),
    (LEGION, "", "attribute"),
    ("The product photo shows a mouse, not the keyboard the name lists.", "", "image"),
    ("the description and image URL suggest a generic cable", "", "attribute"),
    (VELZTORM, "brand", "attribution"),  # a classifier's explicit basis wins
    (CYLEWET, "image", "image"),
])
def test_classify_reason(mod, reason, hint, want):
    assert mod.classify_reason(reason, hint) == want


@pytest.mark.parametrize("reason, ident, want", [
    # brand != manufacturer patterns that misfired: never a deactivation
    (WD_PASSPORT, ("B08F27J5SQ", "WD 1TB My Passport SSD", "Western Digital",
                   "Sandisk Technologies, Inc."), "cleared"),
    # an identity finding ("a different product") that only contrasts one
    # maker's aliases goes to a person, never a deactivation (review
    # 2026-11-12: inside one maker's family an identity or image finding can
    # still be a real wrong product, so it is not cleared silently)
    (SN850X, ("B0B7CMZ3QH", "WD_BLACK SN850X 2TB NVMe SSD", "Sandisk",
              "Sandisk Technologies, Inc."), "report"),
    (YETI, ("B01LY6Z2M6", "Logitech Creators Blue Yeti USB Microphone", "Logitech G",
            "Logitech"), "cleared"),
    (PENTIUM, ("B009ZJ5DHM", "Intel Pentium 4 3.4GHZ HT Socket 478 CPU", "Intel", "SweetWay"),
     "cleared"),
    (UNKNOWN_MAN, ("PCB_105F33C9291B", "Corsair Vengeance DDR5 16GB", "", ""), "cleared"),
    (CYLEWET, ("B0752RMB7Q", "Cylewet 12Pcs Momentary Push Button", "Cylewet", "Qianxin"),
     "report"),
    # a spec the title and description disagree on: reported, not deactivated
    (VELZTORM, ("B0F3BD5N8Z", "Velztorm Y60 Gaming Desktop PC", "Velztorm", "Velztorm"),
     "report"),
    # title phrase "Lenovo Legion" is not a naming contrast; still a report
    (LEGION, ("B0X", "Lenovo Legion Pro 7i Gaming Laptop", "Lenovo", "Lenovo"), "report"),
    # the description is the odd one out: report
    (KVM, ("B0K", "2port Mini Starview KVM Switch", "StarTech", "StarTech"), "report"),
    # the title names a different product than the listing's own description
    (WIRED, ("B0W", "2 Pack 2.4GHz Wireless Gamepad Controller", "USonline911", "USonline911"),
     "deactivate"),
])
def test_triage(mod, rule, reason, ident, want):
    row = _ident(*ident)
    t = mod.triage_image_name_finding({"key": "slug-x", "name": row["title"], "reason": reason},
                                      row, rule)
    assert t["remedy"] == want, t


def test_no_row_never_deactivates(mod, rule):
    t = mod.triage_image_name_finding({"key": "gone", "reason": WIRED}, None, rule)
    assert (t["remedy"], t["basis"], t["relation"]) == ("report", "identity", None)
    t = mod.triage_image_name_finding({"key": "gone", "reason": CYLEWET}, None, rule)
    assert t["remedy"] == "report"
    # an operator may allow it explicitly
    t = mod.triage_image_name_finding({"key": "gone", "reason": WIRED}, None,
                                      {**rule, "require_lookup_to_deactivate": False})
    assert t["remedy"] == "deactivate"


def test_deactivate_bases_are_configurable(mod, rule):
    row = _ident("B0F3BD5N8Z", "Velztorm Y60", "Velztorm", "Velztorm")
    f = {"key": "k", "reason": VELZTORM}
    assert mod.triage_image_name_finding(f, row, rule)["remedy"] == "report"
    loose = {**rule, "deactivate_bases": ["identity", "image", "attribute"]}
    assert mod.triage_image_name_finding(f, row, loose)["remedy"] == "deactivate"
    # attribution never deactivates, whatever the config says
    loosest = {**rule, "deactivate_bases": ["attribution", "attribute", "identity", "image"]}
    cy = _ident("B0752RMB7Q", "Cylewet push button", "Cylewet", "Qianxin")
    assert mod.triage_image_name_finding({"key": "k", "reason": CYLEWET}, cy, loosest)[
        "remedy"] == "report"


def _csv_doc(rows):
    return {"rows": [{"slug": s, "name": n, "verdict": v, "reason": r, "image_url": "https://i/x.jpg"}
                     for s, n, v, r in rows]}


def test_converter_names_rows_and_keeps_the_finding(mod):
    recs = mod._findings_to_recs_specpicks_images(_csv_doc([
        ("wd-passport", "WD 1TB My Passport", "mismatch", WD_PASSPORT),
        ("fine", "Fine", "match", "ok"),
    ]))
    assert len(recs) == 1
    r = recs[0]
    assert r["check_id"] == mod.IMAGE_NAME_CHECK and r["ref_ids"] == ["wd-passport"]
    assert r["finding"]["key"] == "wd-passport" and r["finding"]["reason"] == WD_PASSPORT


def test_apply_rule_shapes_recs(mod, rule):
    recs = mod._findings_to_recs_specpicks_images(_csv_doc([
        ("wd-passport", "WD 1TB My Passport", "mismatch", WD_PASSPORT),
        ("cylewet", "Cylewet push button", "mismatch", CYLEWET),
        ("pad", "2 Pack Wireless Gamepad", "mismatch", WIRED),
        ("missing", "Gone", "mismatch", WIRED),
    ]))
    other = {"check_id": "recipe-image-present", "ref_ids": ["1"], "title": "x"}
    ident = {"wd-passport": _ident("B08F27J5SQ", "WD 1TB My Passport SSD", "Western Digital",
                                   "Sandisk Technologies, Inc."),
             "cylewet": _ident("B0752RMB7Q", "Cylewet push button", "Cylewet", "Qianxin"),
             "pad": _ident("B0GAMEPAD1", "2 Pack 2.4GHz Wireless Gamepad Controller",
                           "USonline911", "USonline911")}
    out, triage = mod.apply_image_name_rule(recs + [other], ident, rule)
    assert [t["remedy"] for t in triage] == ["cleared", "report", "deactivate", "report"]
    by = {r.get("finding", {}).get("key", r["check_id"]): r for r in out}
    assert "wd-passport" not in by and by["recipe-image-present"] is other
    rep = by["cylewet"]
    assert rep["review_required"] and rep["tier"] == "review" and "migration_template" not in rep
    assert rep["ref_ids"] == ["B0752RMB7Q"] and "Do not deactivate" in rep[
        "implementation_outline"]["approach"]
    d = by["pad"]
    assert d["ref_ids"] == ["B0GAMEPAD1"] and not d.get("review_required")
    assert d["migration_template"]["sql_with_ids"].endswith("asin IN ('B0GAMEPAD1');")
    assert d["affected_urls"] == ["https://shop.test/product/B0GAMEPAD1"]
    assert d["identity_rule"]["basis"] == "identity"
    assert by["missing"]["review_required"]  # its row could not be read


def test_render_ids_quotes_text_ids_and_drops_unsafe_ones(mod):
    assert mod.render_ids([1, 2]) == "1, 2"
    assert mod.render_ids(["B0A", "PCB_1F"], {"quote_ids": True}) == "'B0A', 'PCB_1F'"
    assert mod.render_ids(["B0A", "x'); DROP TABLE p;--"], {"quote_ids": True}) == "'B0A'"


def test_report_only_recs_are_never_guarded_and_idless_recs_survive_guards(mod):
    protect = {"tables": ["products"], "criteria_prefixes": ["product-image"]}
    report = {"check_id": mod.IMAGE_NAME_CHECK, "ref_ids": ["B0P"], "review_required": True}
    assert not mod.rec_is_guarded(report, protect)
    idless = {"check_id": "recipe-image-present", "ref_ids": [], "title": "t"}
    destructive_idless = {"check_id": mod.IMAGE_NAME_CHECK, "ref_ids": []}
    out, blocked, dropped = mod.apply_guards(
        [report, idless, destructive_idless], protected={"B0P": "article-linked"}, pending={},
        protect_cfg=protect)
    assert out == [report, idless]
    assert [(b["id"], b["reason"]) for b in blocked] == [("", "rec names no row ids")]


def test_guard_turns_a_protected_deactivation_into_a_report(mod, rule, monkeypatch):
    a = mod.CatalogAuditAgent.__new__(mod.CatalogAuditAgent)
    a.decisions, a.saved = [], {}
    a.decide = lambda kind, msg, **k: a.decisions.append((kind, msg))
    a._save_artifact = lambda name, content: a.saved.__setitem__(name, content)
    a.storage, a.run_ts = None, "T"

    class Cfg:
        site_id = "s"

        def get(self, k, d=None):
            return d

    a.cfg = Cfg()
    gcfg = mod._deep_merge(mod.GUARD_DEFAULTS, {
        "protect_set": {"enabled": True, "dsn_env": "X_DSN", "tables": ["products"],
                        "image_check": {"enabled": False}},
        "image_name_rule": rule})
    monkeypatch.setattr(mod, "resolve_guard_config", lambda *a_, **k: gcfg)
    monkeypatch.setenv("X_DSN", "postgresql://unused")

    class Conn:
        def set_session(self, **k):
            pass

        def close(self):
            pass

    import types
    monkeypatch.setitem(__import__("sys").modules, "psycopg2",
                        types.SimpleNamespace(connect=lambda *a_, **k: Conn()))
    monkeypatch.setattr(mod, "load_protect_set",
                        lambda conn, protect, storage=None: {"B0GAMEPAD1": {"image": None,
                                                                            "reason": "clicked"}})
    recs = mod._findings_to_recs_specpicks_images(_csv_doc([
        ("pad", "2 Pack Wireless Gamepad", "mismatch", WIRED),
        ("pad2", "Other gamepad", "mismatch", WIRED)]))
    ident = {"pad": _ident("B0GAMEPAD1", "2 Pack Wireless Gamepad Controller", "U", "U"),
             "pad2": _ident("B0GAMEPAD2", "Other Wireless Gamepad Controller", "U", "U")}
    recs, _ = mod.apply_image_name_rule(recs, ident, rule)
    out, metrics = a._apply_remedy_guards(recs)
    by = {r["ref_ids"][0]: r for r in out}
    assert set(by) == {"B0GAMEPAD1", "B0GAMEPAD2"}
    assert by["B0GAMEPAD1"]["review_required"] and "protected row (clicked)" in by[
        "B0GAMEPAD1"]["implementation_outline"]["approach"]
    assert "migration_template" not in by["B0GAMEPAD1"]
    assert not by["B0GAMEPAD2"].get("review_required")
    assert metrics["protected_remedies_blocked"] == 1.0


def test_pending_ids_for_text_keyed_catalogs(mod, tmp_path):
    (tmp_path / "20261112T150004Z_catalog-audit-rec004-wd-green-mismatch.sql").write_text(
        "-- The WDS240G2G0B sibling (asin = 'B078WYS5K6') is NOT touched.\n"
        "UPDATE products SET is_active = false\nWHERE asin = 'B09TMVV6CQ';\n")
    (tmp_path / "20261112T220001Z_catalog-audit-rec001-011-mixed.sql").write_text(
        "UPDATE products SET is_active = false WHERE asin IN ('B0A', 'B0B') AND is_active;")
    (tmp_path / "20261101T000000Z_catalog-audit-rec001-applied.sql").write_text(
        "UPDATE products SET is_active = false WHERE asin = 'B0APPLIED';")
    pend = mod.pending_migration_ids(
        str(tmp_path), {"20261101T000000Z_catalog-audit-rec001-applied.sql"},
        id_cols=["asin"], criterion=mod.IMAGE_NAME_CHECK)
    assert pend == {mod.IMAGE_NAME_CHECK: {"B09TMVV6CQ", "B0A", "B0B"}}
    # default columns keep the old behaviour: text-keyed files name nothing
    assert mod.pending_migration_ids(str(tmp_path), set()) == {}


def test_dispatch_skips_report_only_recs(mod, monkeypatch):
    a = mod.CatalogAuditAgent.__new__(mod.CatalogAuditAgent)
    a.decisions = []
    a.decide = lambda kind, msg, **k: a.decisions.append((kind, msg))
    a.cfg, a.agent_id, a.run_dir = {"site_id": "s"}, "s-catalog-audit-agent", "/tmp/x"
    seen = {}
    from framework.core import dispatch

    class H:
        fell_back_to_queue = False

    def fake(**kw):
        seen.update(kw)
        return H()

    monkeypatch.setattr(dispatch, "gated_dispatch_now", fake)
    a.cfg = type("C", (), {"site_id": "s", "get": lambda self, k, d=None: d})()
    a._write_auto_queue(request_id="r", recs=[{"id": "rec-001"},
                                              {"id": "rec-002", "review_required": True}])
    assert seen["rec_ids"] == ["rec-001"]
    seen.clear()
    a._write_auto_queue(request_id="r", recs=[{"id": "rec-002", "review_required": True}])
    assert seen == {} and any("report-only" in m for _, m in a.decisions)


def test_ledger_metrics_count_untracked_agent_migrations(mod, tmp_path):
    import subprocess

    def git(*args):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    mig = tmp_path / "db" / "migrations"
    mig.mkdir(parents=True)
    (mig / "20261001T000000Z_catalog-audit-rec001-x.sql").write_text("select 1;")
    git("add", ".")
    git("commit", "-q", "-m", "init")
    (mig / "20261020T150001Z_catalog-audit-rec002-y.sql").write_text("select 2;")
    (mig / "20261112T220001Z_catalog-audit-rec001-z.sql").write_text("select 3;")
    (mig / "20261112T220002Z_growth-01.sql").write_text("select 4;")
    a = mod.CatalogAuditAgent.__new__(mod.CatalogAuditAgent)
    a.decisions, a.saved, a.run_ts = [], {}, "T"
    a.decide = lambda kind, msg, **k: a.decisions.append((kind, msg))
    a._save_artifact = lambda name, content: a.saved.__setitem__(name, content)
    m = a._migration_ledger_metrics({"dir": str(mig)},
                                    {"20261001T000000Z_catalog-audit-rec001-x.sql",
                                     "20261020T150001Z_catalog-audit-rec002-y.sql"})
    assert m == {"migrations_applied_uncommitted": 1.0, "migrations_stranded": 1.0}
    assert a.saved["migration-ledger.json"]["stranded"] == [
        "20261112T220001Z_catalog-audit-rec001-z.sql"]
