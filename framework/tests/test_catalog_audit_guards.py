"""catalog-audit-agent remedy guards (SHOP-20): never deactivate or blank a
row people use, check those rows' images first, and do not re-flag a row
whose fix is already pending."""
from __future__ import annotations

import http.server
import importlib.util as iu
import os
import threading
import time
from pathlib import Path

import pytest

_AGENT = Path(__file__).resolve().parents[2] / "agents" / "catalog-audit-agent" / "agent.py"


@pytest.fixture(scope="module")
def mod():
    spec = iu.spec_from_file_location("catalog_audit_agent_guards", _AGENT)
    m = iu.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _rec(m, crit, ids):
    t = m._AISLEPROMPT_TEMPLATES.get(crit)
    rec = {"check_id": crit, "category": crit, "ref_ids": [str(i) for i in ids],
           "evidence": [{"ref_id": str(i), "snippet": "x"} for i in ids],
           "rationale": "r", "affected_urls": ["https://s.test/products/%s" % i for i in ids]}
    if t:
        rec["migration_template"] = {**t, "sql_with_ids": t["sql"].replace(
            "{ids}", ", ".join(str(i) for i in ids))}
    return rec


def test_guard_config_off_by_default_and_layers(mod):
    cfg = mod.resolve_guard_config("s", file_cfg={})
    assert cfg["protect_set"]["enabled"] is False
    assert cfg["pending_migrations"]["enabled"] is False
    cfg = mod.resolve_guard_config(
        "s", site_block={"protect_set": {"tables": ["t1"], "dsn_env": "A"}},
        file_cfg={"by_site": {"s": {"protect_set": {"enabled": True, "dsn_env": "B"}}},
                  "defaults": {"protect_set": {"criteria_prefixes": ["product-"]}}})
    ps = cfg["protect_set"]
    assert (ps["enabled"], ps["dsn_env"], ps["tables"], ps["criteria_prefixes"]) == \
        (True, "B", ["t1"], ["product-"])
    assert ps["image_check"]["enabled"] is True  # nested defaults survive


def test_destructive_sql_detection(mod):
    T = mod._AISLEPROMPT_TEMPLATES
    assert mod.is_destructive_sql(T["product-image-present"]["sql"])
    assert mod.is_destructive_sql(T["recipe-image-valid-url"]["sql"])
    assert not mod.is_destructive_sql(T["product-schema-rich-results"]["sql"])


def test_protected_rows_leave_destructive_recs(mod):
    protect = {"tables": ["kitchen_products"], "criteria_prefixes": ["product-"]}
    recs = [_rec(mod, "product-image-present", [61595, 7, 8]),
            _rec(mod, "recipe-image-present", [61595]),          # other table
            _rec(mod, "product-image-valid-url", [8]),           # no template, prefix
            _rec(mod, "product-schema-rich-results", [61595])]   # not destructive
    out, blocked, dropped = mod.apply_guards(
        recs, protected={"61595": "linked", "8": "clicked"}, pending={}, protect_cfg=protect)
    by = {r["check_id"]: r for r in out}
    assert by["product-image-present"]["ref_ids"] == ["7"]
    assert "61595" not in by["product-image-present"]["migration_template"]["sql_with_ids"]
    assert by["product-image-present"]["affected_urls"] == ["https://s.test/products/7"]
    assert by["recipe-image-present"]["ref_ids"] == ["61595"]
    assert "product-image-valid-url" not in by
    assert by["product-schema-rich-results"]["ref_ids"] == ["61595"]
    assert sorted((b["id"], b["criterion"]) for b in blocked) == [
        ("61595", "product-image-present"), ("8", "product-image-present"),
        ("8", "product-image-valid-url")]
    assert dropped == 0


def test_pending_migrations_are_not_reflagged(mod, tmp_path):
    (tmp_path / "20261020T000000Z_catalog-audit-recipe-image-present.sql").write_text(
        "UPDATE recipe_catalog SET is_active = false WHERE id IN (166211, 5);")
    (tmp_path / "20261001T000000Z_catalog-audit-recipe-image-present.sql").write_text(
        "UPDATE recipe_catalog SET is_active = false WHERE id IN (9);")
    old = tmp_path / "20250101T000000Z_catalog-audit-recipe-image-present.sql"
    old.write_text("... id IN (77) ...")
    os.utime(old, (time.time() - 400 * 86400,) * 2)
    (tmp_path / "20261021T000000Z_catalog-audit-rec003-product-image-valid-url.sql").write_text(
        "UPDATE kitchen_products SET primary_image_url = NULL WHERE id IN (16) AND is_active;")
    (tmp_path / "20261021T000000Z_growth-01.sql").write_text("... id IN (1) ...")
    pend = mod.pending_migration_ids(
        str(tmp_path), {"20261001T000000Z_catalog-audit-recipe-image-present.sql"},
        max_age_days=60)
    assert pend == {"recipe-image-present": {"166211", "5"},
                    "product-image-valid-url": {"16"}}
    out, blocked, dropped = mod.apply_guards(
        [_rec(mod, "recipe-image-present", [166211]), _rec(mod, "recipe-image-present", [5, 6])],
        protected={}, pending=pend)
    assert [r["ref_ids"] for r in out] == [["6"]] and dropped == 2 and blocked == []


class _Img(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _go(self, body):
        if self.path == "/ok.jpg":
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
        elif self.path == "/nohead.jpg":
            if self.command == "HEAD":
                self.send_response(405)
            else:
                self.send_response(206)
                self.send_header("Content-Type", "image/png")
        elif self.path == "/html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
        elif self.path == "/flaky.jpg":
            self.send_response(503)
        else:
            self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self):
        self._go(False)

    def do_GET(self):
        self._go(True)


def test_check_images_verdicts():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Img)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % srv.server_address[1]
    try:
        res = mod_check = None
        spec = iu.spec_from_file_location("catalog_audit_agent_img", _AGENT)
        m = iu.module_from_spec(spec)
        spec.loader.exec_module(m)
        res = m.check_images([("1", base + "/ok.jpg"), ("2", base + "/gone.jpg"),
                              ("3", base + "/nohead.jpg"), ("4", base + "/html"),
                              ("5", base + "/flaky.jpg"), ("6", None), ("7", "")],
                             timeout_s=5, workers=4)
    finally:
        srv.shutdown()
    assert sorted(d["id"] for d in res["dead"]) == ["2", "4"]
    assert res["missing"] == ["6", "7"] and res["checked"] == 5 and res["unknown"] == 1


def test_unreadable_protect_set_holds_guarded_recs(mod, monkeypatch):
    a = mod.CatalogAuditAgent.__new__(mod.CatalogAuditAgent)
    a.decisions = []
    a.decide = lambda kind, msg, **k: a.decisions.append((kind, msg))
    a.storage = None

    class Cfg:
        site_id = "s"

        def get(self, k, d=None):
            return {"protect_set": {"enabled": True, "dsn_env": "NO_SUCH_DSN_ENV_X",
                                    "tables": ["kitchen_products"]}} if k == "catalog_audit" else d

    a.cfg = Cfg()
    monkeypatch.setattr(mod, "resolve_guard_config",
                        lambda site, site_block=None, storage=None, file_cfg=None:
                        mod._deep_merge(mod.GUARD_DEFAULTS, site_block))
    recs = [_rec(mod, "product-image-present", [1]), _rec(mod, "recipe-image-present", [2])]
    out, metrics = a._apply_remedy_guards(recs)
    assert [r["check_id"] for r in out] == ["recipe-image-present"]
    assert metrics["protected_remedies_blocked"] == 1.0
    assert any(k == "error" for k, _ in a.decisions)
