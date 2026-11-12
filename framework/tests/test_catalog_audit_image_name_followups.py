"""catalog-audit-agent image/name rule, review follow-ups (2026-11-12).

- Slugs are not unique (specpicks: 14,998 duplicated slugs; 14 of the 351
  mismatch slugs map to more than one ASIN). The lookup kept whichever row
  came back last, so a deactivate rec could name the wrong ASIN.
- An alias contrast cleared image and identity findings silently, including
  a real wrong product inside one maker's family.
- Hedged reasons and spec nits worded as "different products" deactivated.
- The pending-migration dedup missed tuple IN lists, slug-keyed files and
  values followed by an inline `-- rec-NNN` comment, and it dropped a
  protected row silently when a stranded file already targeted it.
- A by_site `extra_brand_groups` replaced the defaults-level one."""
from __future__ import annotations

import importlib.util as iu
import types
from pathlib import Path

import pytest

from framework.core import brand_identity as bi
from framework.core import rec_contract as rc

_AGENT = Path(__file__).resolve().parents[2] / "agents" / "catalog-audit-agent" / "agent.py"


@pytest.fixture(scope="module")
def mod():
    spec = iu.spec_from_file_location("catalog_audit_agent_followups", _AGENT)
    m = iu.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


SQL = ("UPDATE products\nSET is_active = false, updated_at = NOW()\n"
       "WHERE is_active = true\n  AND asin IN ({ids});")


@pytest.fixture
def rule(mod):
    return mod._deep_merge(mod.GUARD_DEFAULTS["image_name_rule"], {
        "deactivate_template": {"table": "products", "action": "deactivate", "sql": SQL,
                                "filename": "catalog-audit-product-image-matches-name",
                                "quote_ids": True},
        "affected_url_template": "https://shop.test/product/{id}"})


def _row(i, title, brand="Acme", man="Acme", image=""):
    return {"id": i, "title": title, "brand": brand, "manufacturer": man,
            "description": "", "image_url": image}


# verbatim audit reasons
N64 = ("Product name claims a fixed '342 in 1' multi-game cartridge, but description details "
       "'The 64 Plus'—an SD/SDHC card-based ROM loader supporting variable-sized ROM files and "
       "32GB cards, which is a different product category entirely.")
SD_CARD = ("Product name claims 'SSD Drive' but description explicitly states '2TB Micro SD "
           "Card'—these are completely different storage media, indicating a product name error.")
TEST_CARD = ("Product name describes a diagnostic test card, but description identifies it as a "
             "voltage regulator IC in TQFP package—two entirely different product categories.")
CABLE_LEN = ("Product name states 0.5m cable length while description explicitly describes a 20m "
             "cable — these are fundamentally different products and an internal contradiction.")
LED_RGB = ("Name explicitly states 'White LED Backlit' while description clearly claims 'RGB "
           "Gaming Keyboard' with '21 colors'—these are mutually exclusive lighting systems "
           "indicating two different products.")
ZIPPER = ("The product name describes a standard open name badge holder, but the description "
          "prominently features a 'secure zipper closure' — these are materially different "
          "product types, and the zipper feature should appear in the name if accurate.")
SWITCH = ("The product name suggests a Nintendo Switch exclusive collection, but the manufacturer "
          "'Maximum Entertainment' and description of arcade games indicate a different type of "
          "product than a typical Nintendo Switch game.")
BOUUNCIY = ("The product name describes a BOUUNCIY brand cable, but the manufacturer listed is "
            "BOUUNCIY while the description and naming convention suggest a different product "
            "than what would typically be expected from that brand.")
MOTHERBOARD = ("The product name suggests an industrial control motherboard while the manufacturer "
               "KLAWKKNI and description indicate a different product type with no clear "
               "alignment.")
VGA = ("Product name claims VGA Female to DVI 24+5 Male adapter, but Description Type field states "
       "DVI-I Male to DVI-I Female adapter—these are different product types (VGA-to-DVI vs. "
       "DVI-to-DVI), though the Package line agrees with the name.")
WD_PASSPORT = ("Product name 'WD My Passport SSD' indicates Western Digital, but manufacturer field "
               "lists 'Sandisk Technologies, Inc.'; model number prefix 'WD' and 'My Passport' "
               "branding confirm this should be Western Digital, not Sandisk.")


def _csv_doc(rows):
    return {"rows": [{"slug": s, "name": n, "verdict": "mismatch", "reason": r, "image_url": img,
                      **({"id": i} if i else {})}
                     for s, n, r, img, i in rows]}


# ── shared slugs ────────────────────────────────────────────────────────────

SLUG = "342-in-1-multi-game-cartridge-save-compatible-for-n64-game-consoles"
TITLE = "342 in 1 Multi Game Cartridge Save Compatible for N64 Game Consoles"
TWINS = [_row("B0F2YNMMMY", TITLE, "Generic", "Generic", "https://img/a.jpg"),
         _row("B0GZKTW5BQ", TITLE, "Generic", "Generic", "https://img/b.jpg")]


def test_shared_slug_picks_the_row_whose_image_the_audit_saw(mod, rule):
    recs = mod._findings_to_recs_specpicks_images(_csv_doc([(SLUG, TITLE, N64, "https://img/a.jpg", "")]))
    out, triage = mod.apply_image_name_rule(recs, {SLUG: TWINS}, rule)
    assert triage[0]["remedy"] == "deactivate" and triage[0]["candidates"] == 2
    assert out[0]["ref_ids"] == ["B0F2YNMMMY"]
    assert out[0]["migration_template"]["sql_with_ids"].endswith("asin IN ('B0F2YNMMMY');")
    # the other image picks the other row, whichever order the rows came back in
    recs = mod._findings_to_recs_specpicks_images(_csv_doc([(SLUG, TITLE, N64, "https://img/b.jpg", "")]))
    out, _ = mod.apply_image_name_rule(recs, {SLUG: list(reversed(TWINS))}, rule)
    assert out[0]["ref_ids"] == ["B0GZKTW5BQ"]


def test_shared_slug_falls_back_to_the_name_then_reports(mod, rule):
    rows = [_row("B0A", "Alpha cartridge", image="https://img/a.jpg"),
            _row("B0B", "Beta cartridge", image="https://img/b.jpg")]
    by_name = mod._findings_to_recs_specpicks_images(
        _csv_doc([("dup", "Beta  cartridge", N64, "https://img/gone.jpg", "")]))
    out, _ = mod.apply_image_name_rule(by_name, {"dup": rows}, rule)
    assert out[0]["ref_ids"] == ["B0B"] and not out[0].get("review_required")
    # neither the image nor the name singles out a row: report, never deactivate
    same = [_row("B0F2YNMMMY", TITLE, image="https://img/s.jpg"),
            _row("B0GZKTW5BQ", TITLE, image="https://img/s.jpg")]
    for doc_rows, ident in (
            ([("dup", "Gamma", N64, "https://img/c.jpg", "")], rows),     # nothing matches
            ([(SLUG, TITLE, N64, "https://img/z.jpg", "")], same),        # names tie
            ([(SLUG, TITLE, N64, "https://img/s.jpg", "")], same)):       # image and name tie
        key = doc_rows[0][0]
        recs = mod._findings_to_recs_specpicks_images(_csv_doc(doc_rows))
        out, triage = mod.apply_image_name_rule(recs, {key: ident}, rule)
        assert triage[0]["remedy"] == "report", triage[0]
        assert "do not single one out" in triage[0]["why"]
        assert out[0]["review_required"] and "migration_template" not in out[0]
        assert sorted(out[0]["ref_ids"]) == sorted(r["id"] for r in ident)


def test_an_ambiguous_key_is_never_deactivated_blind(mod, rule):
    """require_lookup_to_deactivate: false lets a site with no lookup
    deactivate by the finding's key; it never lets an ambiguous key
    deactivate every product that has it."""
    loose = {**rule, "require_lookup_to_deactivate": False}
    same = [_row("B0F2YNMMMY", TITLE, image="https://img/s.jpg"),
            _row("B0GZKTW5BQ", TITLE, image="https://img/s.jpg")]
    recs = mod._findings_to_recs_specpicks_images(_csv_doc([(SLUG, TITLE, N64, "https://img/s.jpg", "")]))
    out, triage = mod.apply_image_name_rule(recs, {SLUG: same}, loose)
    assert triage[0]["remedy"] == "report" and "no single row" in triage[0]["why"]
    assert out[0]["review_required"] and "migration_template" not in out[0]
    # with no lookup at all, the old behaviour stands: the finding's id
    recs = mod._findings_to_recs_specpicks_images(_csv_doc([(SLUG, TITLE, N64, "", "B0GZKTW5BQ")]))
    out, triage = mod.apply_image_name_rule(recs, {}, loose)
    assert triage[0]["remedy"] == "deactivate" and out[0]["ref_ids"] == ["B0GZKTW5BQ"]


def test_rows_that_share_an_id_are_one_product(mod, rule):
    """The same ASIN under two catalogs (only (site_id, asin) is unique) is
    one product: no ambiguity about which id a deactivation names."""
    rows = [_row("B0SAME", TITLE, image="https://img/one.jpg"),
            _row("B0SAME", TITLE + " (US)", image="https://img/two.jpg")]
    recs = mod._findings_to_recs_specpicks_images(_csv_doc([(SLUG, TITLE, N64, "https://img/zz.jpg", "")]))
    out, triage = mod.apply_image_name_rule(recs, {SLUG: rows}, rule)
    assert triage[0]["remedy"] == "deactivate" and out[0]["ref_ids"] == ["B0SAME"]
    row, why = mod.pick_identity_row({"name": TITLE + " (US)"}, rows)
    assert row is rows[1] and why == ""


def test_an_id_column_pins_the_finding_to_its_row(mod, rule):
    recs = mod._findings_to_recs_specpicks_images(
        _csv_doc([(SLUG, TITLE, N64, "https://img/a.jpg", "B0GZKTW5BQ")]))
    assert recs[0]["finding"]["id"] == "B0GZKTW5BQ"
    out, _ = mod.apply_image_name_rule(recs, {SLUG: TWINS}, rule)
    assert out[0]["ref_ids"] == ["B0GZKTW5BQ"]   # the id wins over the image match
    recs = mod._findings_to_recs_specpicks_images(
        _csv_doc([(SLUG, TITLE, N64, "https://img/a.jpg", "B0ELSEWHERE")]))
    out, triage = mod.apply_image_name_rule(recs, {SLUG: TWINS}, rule)
    assert triage[0]["remedy"] == "report" and out[0]["review_required"]


def test_load_identity_keeps_every_row_for_a_key(mod):
    class Cur:
        description = [("key",), ("id",), ("title",), ("brand",), ("manufacturer",),
                       ("description",), ("image_url",)]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            self.params = params

        def fetchall(self):
            return [(SLUG, "B0F2YNMMMY", TITLE, "Generic", "Generic", None, "https://img/a.jpg"),
                    (SLUG, "B0GZKTW5BQ", TITLE, "Generic", "Generic", None, "https://img/b.jpg"),
                    ("solo", "B0SOLO", "Solo", "A", "A", "d", "https://img/s.jpg")]

    class Conn:
        def cursor(self):
            return Cur()

        def rollback(self):
            pass

    got = mod.load_identity(Conn(), {"lookup_sql": "select ..."}, [SLUG, "solo"])
    assert [r["id"] for r in got[SLUG]] == ["B0F2YNMMMY", "B0GZKTW5BQ"]
    assert got["solo"][0]["description"] == "d" and got[SLUG][0]["description"] == ""


def test_untriaged_recs_say_do_nothing_and_are_held_by_the_rec_contract(mod, rule):
    recs = mod._findings_to_recs_specpicks_images(_csv_doc([(SLUG, TITLE, N64, "https://img/a.jpg", "")]))
    assert "deactivate" not in recs[0]["implementation_outline"]["approach"].lower().replace(
        "do not change any row", "")
    cfg = rc.load_config(file_cfg={})
    assert rc.hold_reason(recs[0], producer="s-catalog-audit-agent", cfg=cfg)
    out, _ = mod.apply_image_name_rule(recs, {SLUG: TWINS}, rule)
    assert all(rc.hold_reason(r, producer="s-catalog-audit-agent", cfg=cfg) is None for r in out)


# ── alias contrasts: cleared only for attribution / attribute / description ──

@pytest.mark.parametrize("reason, row", [
    ("The image depicts a Logitech MX Master mouse, a completely different product from the "
     "Logitech G Pro keyboard the title names.",
     _row("B0K1", "Logitech G Pro X Mechanical Gaming Keyboard", "Logitech G", "Logitech")),
    ("The product image shows a SanDisk Extreme portable SSD, a different product from the WD My "
     "Passport SSD this listing names.",
     _row("B0K2", "WD 1TB My Passport SSD", "Western Digital", "Sandisk Technologies, Inc.")),
    ("The image shows a LaCie Rugged drive, not the Seagate BarraCuda internal drive named in "
     "the listing.", _row("B0K3", "Seagate BarraCuda 4TB Internal Hard Drive", "Seagate", "Seagate")),
])
def test_alias_contrast_on_an_image_or_identity_finding_is_reported(mod, rule, reason, row):
    t = mod.triage_image_name_finding({"key": "k", "name": row["title"], "reason": reason}, row, rule)
    assert t["basis"] in ("image", "identity")
    assert t["remedy"] == "report" and "aliases of this listing's own maker" in t["why"]


def test_alias_contrast_still_clears_attribution_findings(mod, rule):
    row = _row("B08F27J5SQ", "WD 1TB My Passport SSD", "Western Digital", "Sandisk Technologies, Inc.")
    t = mod.triage_image_name_finding({"key": "k", "name": row["title"], "reason": WD_PASSPORT},
                                      row, rule)
    assert (t["basis"], t["remedy"]) == ("attribution", "cleared")


# ── hedges and spec contrasts never deactivate ──────────────────────────────

@pytest.mark.parametrize("reason, want_basis", [
    (CABLE_LEN, "attribute"),     # 0.5m vs 20m: a length, not another product
    (LED_RGB, "attribute"),       # white LED vs RGB
    (ZIPPER, "attribute"),        # a closure
    (VGA, "description"),         # part of the listing agrees with the name
    (SWITCH, "identity"),         # hedged
    (BOUUNCIY, "identity"),       # hedged
    (MOTHERBOARD, "identity"),    # hedged
])
def test_spec_nits_and_hedged_findings_are_report_only(mod, rule, reason, want_basis):
    row = _row("B0N", "some title")
    t = mod.triage_image_name_finding({"key": "k", "name": "some title", "reason": reason}, row, rule)
    assert t["basis"] == want_basis and t["remedy"] == "report", t


@pytest.mark.parametrize("reason", [SD_CARD, TEST_CARD, N64])
def test_a_title_naming_another_kind_of_product_still_deactivates(mod, rule, reason):
    row = _row("B0D", "some title")
    t = mod.triage_image_name_finding({"key": "k", "name": "some title", "reason": reason}, row, rule)
    assert (t["basis"], t["remedy"]) == ("identity", "deactivate"), t


def test_a_site_can_require_the_classifiers_basis_to_deactivate(mod, rule):
    strict = {**rule, "deactivate_requires_explicit_basis": True}
    row = _row("B0D", "some title")
    t = mod.triage_image_name_finding({"key": "k", "reason": SD_CARD}, row, strict)
    assert t["remedy"] == "report" and "read from the reason text" in t["why"]
    t = mod.triage_image_name_finding({"key": "k", "reason": SD_CARD, "basis": "identity"}, row, strict)
    assert t["remedy"] == "deactivate"


# ── pending-migration dedup ─────────────────────────────────────────────────

FILES = {
    "20261109T170001Z_catalog-audit-rec001-009-yeti-image-mismatch.sql":
        "-- rec-001 id 72007 B01LY6Z2M6 (description)\n"
        "UPDATE products SET is_active = false\n"
        "WHERE (id, asin) IN (\n  (72007,  'B01LY6Z2M6'),\n  (111526, 'B08F27J5SQ'))\n"
        "  AND (is_active = true OR NOT (COALESCE(quality_flags, '[]'::jsonb) ? 'x'));\n"
        "-- VERIFY: SELECT 1 FROM products WHERE asin = 'B0VERIFYONLY';\n",
    "20261110T160001Z_catalog-audit-rec001-011-barracuda-image-mismatch.sql":
        "UPDATE products SET is_active = false WHERE is_active = true\n  AND asin IN (\n"
        "    'B07D9C7SQH',  -- rec-001 SANSUI 27\" 4K monitor (Express Luck white-label\n"
        "    'B0B7CMZ3QH'   -- rec-005\n  );\n",
    "20261111T150001Z_catalog-audit-rec001-003-steam-deck-mismatch.sql":
        "UPDATE products SET is_active = false WHERE is_active = true\n"
        "  AND (asin, slug) IN (('B0BBQRYN9M', 'valve-steam-deck-512gb'),\n"
        "                       ('B009ZJ5DHM', 'intel-pentium-4-sl793'));\n",
    "20261111T210001Z_catalog-audit-rec001-002-n64-342in1-mismatch.sql":
        "UPDATE products SET is_active = false WHERE slug IN (\n"
        "    'dow-340-compound',\n    '342-in-1-cartridge'\n  );\n",
    "20261022T160001Z_catalog-audit-rec001-logitech-m510-mismatch.sql":
        "UPDATE products SET is_active = false\nWHERE slug = 'logitech-m510' AND is_active = true;\n",
    "20261112T150000Z_catalog-audit-rec009-not-in-mismatch.sql":
        "UPDATE products SET is_active = true WHERE NOT asin IN ('B0KEEP1')\n"
        "  AND asin NOT IN ('B0KEEP2') AND title <> 'a -- b' AND asin IN "
        "(SELECT asin FROM x);\n",
}


@pytest.fixture
def mig(tmp_path):
    for name, text in FILES.items():
        (tmp_path / name).write_text(text)
    return tmp_path


def test_pending_refs_read_tuples_slugs_and_ignore_comments(mod, mig):
    refs = mod.pending_migration_refs(str(mig), set(), id_cols=["asin"], key_cols=["slug"],
                                      criterion=mod.IMAGE_NAME_CHECK)
    ids = refs["ids"][mod.IMAGE_NAME_CHECK]
    assert ids == {"B01LY6Z2M6", "B08F27J5SQ", "B07D9C7SQH", "B0B7CMZ3QH",
                   "B0BBQRYN9M", "B009ZJ5DHM"}
    # a tuple naming the asin names one row, so its slug is not a key
    assert refs["keys"][mod.IMAGE_NAME_CHECK] == {"dow-340-compound", "342-in-1-cartridge",
                                                  "logitech-m510"}
    assert refs["files"][mod.IMAGE_NAME_CHECK]["B0B7CMZ3QH"] == [
        "20261110T160001Z_catalog-audit-rec001-011-barracuda-image-mismatch.sql"]
    # the old reader's id columns still work, and the wrapper keeps its shape
    assert mod.pending_migration_ids(str(mig), set(), id_cols=["id"],
                                     criterion="c") == {"c": {"72007", "111526"}}


def test_pending_keys_resolve_to_every_row_that_has_them(mod, mig):
    refs = mod.pending_migration_refs(str(mig), set(), id_cols=["asin"], key_cols=["slug"],
                                      criterion="c")

    class Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            self.keys = (params or {}).get("keys")

        def fetchall(self):
            return [("342-in-1-cartridge", "B0F2YNMMMY"), ("342-in-1-cartridge", "B0GZKTW5BQ"),
                    ("logitech-m510", "B003NREDC8")]

    class Conn:
        def cursor(self):
            return Cur()

        def rollback(self):
            pass

    refs = mod.resolve_pending_keys(Conn(), "select slug, asin ...", refs)
    assert {"B0F2YNMMMY", "B0GZKTW5BQ", "B003NREDC8"} <= refs["ids"]["c"]
    assert refs["files"]["c"]["B0GZKTW5BQ"] == [
        "20261111T210001Z_catalog-audit-rec001-002-n64-342in1-mismatch.sql"]


def test_a_protected_row_already_targeted_by_a_pending_file_is_reported(mod):
    protect = {"tables": ["products"], "criteria_prefixes": []}
    rec = {"check_id": mod.IMAGE_NAME_CHECK, "ref_ids": ["B0P"], "title": "t",
           "migration_template": {"table": "products", "sql": SQL, "quote_ids": True}}
    out, blocked, dropped = mod.apply_guards(
        [rec], protected={"B0P": "article-linked"}, pending={mod.IMAGE_NAME_CHECK: {"B0P"}},
        protect_cfg=protect, pending_files={mod.IMAGE_NAME_CHECK: {"B0P": ["f.sql"]}})
    assert out == [] and dropped == 1
    assert blocked[0]["id"] == "B0P" and blocked[0]["pending_files"] == ["f.sql"]
    assert "remove that file" in blocked[0]["remedy_instead"]


def test_guards_report_stranded_files_that_target_protected_rows(mod, mig, monkeypatch):
    a = mod.CatalogAuditAgent.__new__(mod.CatalogAuditAgent)
    a.decisions, a.saved, a.storage, a.run_ts = [], {}, None, "T"
    a.decide = lambda kind, msg, **k: a.decisions.append((kind, msg))
    a._save_artifact = lambda name, content: a.saved.__setitem__(name, content)
    a._migration_ledger_metrics = lambda pend, applied: {}
    a.cfg = types.SimpleNamespace(site_id="s", get=lambda k, d=None: d)
    gcfg = mod._deep_merge(mod.GUARD_DEFAULTS, {
        "protect_set": {"enabled": True, "dsn_env": "X_DSN", "tables": ["products"],
                        "image_check": {"enabled": False}},
        "pending_migrations": {"enabled": True, "dir": str(mig), "applied_sql": "select name",
                               "id_cols": ["asin"], "key_cols": ["slug"],
                               "key_resolve_sql": "select slug, asin",
                               "criterion": mod.IMAGE_NAME_CHECK}})
    monkeypatch.setattr(mod, "resolve_guard_config", lambda *a_, **k: gcfg)
    monkeypatch.setenv("X_DSN", "postgresql://unused")

    class Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            self.sql = sql

        def fetchall(self):
            if self.sql == "select name":
                return [("20261022T160001Z_catalog-audit-rec001-logitech-m510-mismatch.sql",)]
            return [("342-in-1-cartridge", "B0GZKTW5BQ")]

    class Conn:
        def set_session(self, **k):
            pass

        def cursor(self):
            return Cur()

        def rollback(self):
            pass

        def close(self):
            pass

    import sys
    monkeypatch.setitem(sys.modules, "psycopg2", types.SimpleNamespace(connect=lambda *a_, **k: Conn()))
    monkeypatch.setattr(mod, "load_protect_set", lambda conn, protect, storage=None: {
        "B0GZKTW5BQ": {"image": None, "reason": "clicked"},
        "B0B7CMZ3QH": {"image": None, "reason": "article-linked"}})
    out, metrics = a._apply_remedy_guards([])
    assert metrics["pending_migrations_targeting_protected"] == 2.0
    rows = a.saved["protected-rows.json"]["pending_migrations_targeting_protected"]
    assert [(r["id"], r["reason"]) for r in rows] == [("B0B7CMZ3QH", "article-linked"),
                                                     ("B0GZKTW5BQ", "clicked")]
    assert rows[1]["files"] == ["20261111T210001Z_catalog-audit-rec001-002-n64-342in1-mismatch.sql"]


# ── config layers ───────────────────────────────────────────────────────────

def test_extra_brand_groups_accumulate_across_config_layers(mod):
    file_cfg = {"defaults": {"image_name_rule": {"brand_identity": {
                    "extra_brand_groups": [["nexigo", "nexight"]]}}},
                "by_site": {"s": {"image_name_rule": {"brand_identity": {
                    "extra_brand_groups": [["hiltex", "ridgerock tools"]]}}}}}
    g = mod.resolve_guard_config("s", file_cfg=file_cfg)
    groups = g["image_name_rule"]["brand_identity"]["extra_brand_groups"]
    assert groups == [["nexigo", "nexight"], ["hiltex", "ridgerock tools"]]
    cfg = bi.resolve(g["image_name_rule"]["brand_identity"])
    assert bi.relation("NexiGo", "Nexight INC", cfg=cfg) == "alias"
    assert bi.relation("Hiltex", "Ridgerock Tools", cfg=cfg) == "alias"
    # a plain list key still replaces
    g = mod.resolve_guard_config("s", file_cfg={"by_site": {"s": {"image_name_rule": {
        "deactivate_bases": ["identity"]}}}})
    assert g["image_name_rule"]["deactivate_bases"] == ["identity"]


def test_brand_identity_shared_storage_layer(storage):
    assert bi.load_shared(storage) == {}
    storage.write_json(bi.CONFIG_KEY, {"extra_brand_groups": [["nexigo", "nexight"]]})
    shared = bi.load_shared(storage)
    cfg = bi.resolve({"extra_brand_groups": [["hiltex", "ridgerock tools"]]}, shared=shared)
    assert bi.relation("NexiGo", "Nexight INC", cfg=cfg) == "alias"
    assert bi.relation("Hiltex", "Ridgerock Tools", cfg=cfg) == "alias"
    # a caller's list key replaces the shared one; its extras still apply
    cfg = bi.resolve({"platform_brands": ["acme"]}, shared={"platform_brands": ["intel"],
                                                             "extra_platform_brands": ["amd"]})
    assert cfg["platform_brands"] == ["acme", "amd"]
    # an already-resolved config passes through
    assert bi.resolve(cfg) is cfg
