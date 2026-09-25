"""Tests for the ebay-product-sync-agent rolling re-price step.

Encodes the trap that emptied `ebay_listings`: the old audit called
`/item/<legacy id>`, which 404s for every listing, so every audited live
listing was marked ended (78/78 on 2026-09-24). All HTTP is mocked.
Run: python3 -m pytest tests/test_ebay_reprice.py -q
"""
import io
import json
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parents[1]
_AGENT = _ROOT / "agents" / "ebay-product-sync-agent"
for p in (str(_AGENT), str(_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

import importlib.util  # noqa: E402

import ebay_client  # noqa: E402
from ebay_client import EbayClient, EbayItemGroup, EbayRateLimited  # noqa: E402

# Unique module name — other tests import their own `agent` module.
_spec = importlib.util.spec_from_file_location("ebay_product_sync_agent", _AGENT / "agent.py")
ebay_agent = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ebay_agent)
from framework.core.storage import LocalFilesystemStorage  # noqa: E402

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


def _item(price="10.00", currency="USD", status="IN_STOCK", end=None):
    it = {"price": {"value": price, "currency": currency},
          "estimatedAvailabilities": [{"estimatedAvailabilityStatus": status}]}
    if end:
        it["itemEndDate"] = end
    return it


# ─── HTTP-level client tests ─────────────────────────────────────────

class _Resp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _http_error(code, body):
    return urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(json.dumps(body).encode()))


class TestEbayClientLookups(unittest.TestCase):
    def setUp(self):
        self.c = EbayClient(client_id="id", client_secret="secret")
        self.c._token, self.c._token_expires = "tok", 1e12
        self.urls = []

    def _urlopen(self, outcome):
        def fake(req, timeout=0):
            self.urls.append(req.full_url)
            if isinstance(outcome, Exception):
                raise outcome
            return _Resp(json.dumps(outcome).encode())
        return fake

    def test_legacy_numeric_id_routes_to_legacy_endpoint(self):
        with mock.patch.object(ebay_client.urllib.request, "urlopen",
                               self._urlopen(_item("5.00"))):
            got = self.c.get_item("277682425898")
        self.assertEqual(got["price"]["value"], "5.00")
        self.assertIn("/item/get_item_by_legacy_id?legacy_item_id=277682425898",
                      self.urls[0])
        # the old buggy URL shape
        self.assertNotIn("/item/277682425898", self.urls[0])
        self.assertEqual(self.c.calls, {"get_item_by_legacy_id": 1})

    def test_not_found_returns_none(self):
        err = _http_error(404, {"errors": [{"errorId": 11003}]})
        with mock.patch.object(ebay_client.urllib.request, "urlopen", self._urlopen(err)):
            self.assertIsNone(self.c.get_item("188303705501"))
        self.assertEqual(self.c.total_calls, 1)

    def test_item_group_raises(self):
        err = _http_error(400, {"errors": [{"errorId": 11006}]})
        with mock.patch.object(ebay_client.urllib.request, "urlopen", self._urlopen(err)):
            with self.assertRaises(EbayItemGroup) as cm:
                self.c.get_item("317926381158")
        self.assertEqual(cm.exception.group_id, "317926381158")

    def test_rate_limit_raises(self):
        err = _http_error(429, {"errors": [{"errorId": 2001}]})
        with mock.patch.object(ebay_client.urllib.request, "urlopen", self._urlopen(err)):
            with self.assertRaises(EbayRateLimited):
                self.c.get_item("1")

    def test_other_400_is_an_error_not_ended(self):
        err = _http_error(400, {"errors": [{"errorId": 12345}]})
        with mock.patch.object(ebay_client.urllib.request, "urlopen", self._urlopen(err)):
            with self.assertRaises(RuntimeError):
                self.c.get_item("1")

    def test_group_lookup(self):
        body = {"items": [_item("30.00"), _item("20.00", status="OUT_OF_STOCK")]}
        with mock.patch.object(ebay_client.urllib.request, "urlopen", self._urlopen(body)):
            items = self.c.get_items_by_item_group("317926381158")
        self.assertEqual(len(items), 2)
        self.assertIn("item_group_id=317926381158", self.urls[0])


# ─── pure helpers ────────────────────────────────────────────────────

class TestItemState(unittest.TestCase):
    def test_in_stock_is_live(self):
        s = ebay_agent._item_state(_item("39.99"), now=NOW)
        self.assertTrue(s["live"])
        self.assertEqual(s["price"], 39.99)
        self.assertEqual(s["currency"], "USD")

    def test_limited_stock_is_live(self):
        self.assertTrue(ebay_agent._item_state(_item(status="LIMITED_STOCK"), now=NOW)["live"])

    def test_out_of_stock_is_not_live(self):
        self.assertFalse(ebay_agent._item_state(_item(status="OUT_OF_STOCK"), now=NOW)["live"])

    def test_past_end_date_is_not_live(self):
        past = (NOW - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        s = ebay_agent._item_state(_item(end=past), now=NOW)
        self.assertFalse(s["live"])
        self.assertEqual(s["status"], "ENDED")

    def test_future_end_date_is_live_and_kept(self):
        fut = (NOW + timedelta(days=2)).isoformat()
        s = ebay_agent._item_state(_item(end=fut), now=NOW)
        self.assertTrue(s["live"])
        self.assertIsNotNone(s["item_end_date"])

    def test_group_takes_cheapest_live_usd_variation(self):
        s = ebay_agent._group_state(
            [_item("30.00"), _item("5.00", status="OUT_OF_STOCK"),
             _item("25.00"), _item("1.00", currency="GBP")], now=NOW)
        self.assertTrue(s["live"])
        self.assertEqual(s["price"], 25.0)

    def test_group_all_out_of_stock(self):
        self.assertFalse(ebay_agent._group_state(
            [_item(status="OUT_OF_STOCK")], now=NOW)["live"])


class TestBudget(unittest.TestCase):
    def test_default_hourly_cap_is_about_180(self):
        b = ebay_agent._ebay_call_budget({}, used_today=0, planned_search_calls=8)
        self.assertEqual(b["usable_today"], 4500)
        self.assertEqual(b["reprice_cap"], 4500 // 24 - 8)  # 179

    def test_cap_shrinks_when_day_nearly_spent(self):
        b = ebay_agent._ebay_call_budget({}, used_today=4450, planned_search_calls=8)
        self.assertEqual(b["reprice_cap"], 42)
        b = ebay_agent._ebay_call_budget({}, used_today=9999, planned_search_calls=8)
        self.assertEqual(b["reprice_cap"], 0)

    def test_explicit_cap_and_legacy_alias(self):
        b = ebay_agent._ebay_call_budget({"reprice": {"max_calls_per_run": 50}},
                                         used_today=0, planned_search_calls=8)
        self.assertEqual(b["reprice_cap"], 50)
        b = ebay_agent._ebay_call_budget({"audit_max_per_run": 70},
                                         used_today=0, planned_search_calls=8)
        self.assertEqual(b["reprice_cap"], 70)

    def test_config_quota_and_disable(self):
        b = ebay_agent._ebay_call_budget(
            {"ebay_api": {"daily_call_limit": 10000, "runs_per_day": 12, "headroom_pct": 0}},
            used_today=0, planned_search_calls=0)
        self.assertEqual(b["reprice_cap"], 833)
        b = ebay_agent._ebay_call_budget({"reprice": {"enabled": False}},
                                         used_today=0, planned_search_calls=0)
        self.assertEqual(b["reprice_cap"], 0)


    def test_live_count_beats_ledger(self):
        # eBay's count includes other callers sharing the keys (the
        # counterpart matcher) that this engine's ledger never sees.
        live = {"limit": 5000, "remaining": 600, "reset": None}
        b = ebay_agent._ebay_call_budget({}, used_today=200, planned_search_calls=16,
                                         live=live)
        self.assertEqual(b["used_today"], 4400)
        self.assertEqual(b["reprice_cap"], 4500 - 4400 - 16)   # 84, not 171

    def test_live_paces_remaining_over_runs_before_reset(self):
        live = {"limit": 5000, "remaining": 2500,
                "reset": (NOW + timedelta(hours=10)).isoformat()}
        b = ebay_agent._ebay_call_budget({}, used_today=0, planned_search_calls=16,
                                         live=live, now=NOW)
        self.assertEqual(b["runs_left_before_reset"], 10)
        # (4500 usable - 2500 used) // 10 runs - 16 searches = 184 → capped at 171
        self.assertEqual(b["reprice_cap"], 171)
        live["remaining"] = 1500
        b = ebay_agent._ebay_call_budget({}, used_today=0, planned_search_calls=16,
                                         live=live, now=NOW)
        self.assertEqual(b["reprice_cap"], (4500 - 3500) // 10 - 16)   # 84

    def test_configured_limit_below_ebay_limit_still_caps(self):
        live = {"limit": 10000, "remaining": 10000, "reset": None}
        b = ebay_agent._ebay_call_budget({}, used_today=0, planned_search_calls=0,
                                         live=live)
        self.assertEqual(b["daily_call_limit"], 5000)


class TestUsageLedger(unittest.TestCase):
    def test_instances_sharing_an_app_sum(self):
        with tempfile.TemporaryDirectory() as d:
            st = LocalFilesystemStorage(d)
            ebay_agent._record_api_usage(st, "app", "2026-09-24", "site-a", {"search": 8, "get_item_by_legacy_id": 100})
            ebay_agent._record_api_usage(st, "app", "2026-09-24", "site-a", {"search": 2})
            ebay_agent._record_api_usage(st, "app", "2026-09-24", "site-b", {"search": 5})
            ebay_agent._record_api_usage(st, "other-app", "2026-09-24", "site-c", {"search": 50})
            self.assertEqual(ebay_agent._read_api_usage_today(st, "app", "2026-09-24"), 115)
            self.assertEqual(ebay_agent._read_api_usage_today(st, "app", "2026-09-25"), 0)


# ─── the re-price loop against a fake DB + fake eBay ─────────────────

class _Cursor:
    def __init__(self, db): self.db, self.rowcount = db, 0
    def execute(self, sql, params=()):
        self.db.sql.append((sql, params))
        if "SELECT ebay_item_id" in sql:
            self._rows = self.db.active if "is_active = true" in sql else self.db.inactive
            self._rows = self._rows[: params[-1]]
    def fetchall(self): return list(self._rows)
    def close(self): pass


class _Conn:
    def __init__(self, db): self.db = db
    def cursor(self): return _Cursor(self.db)
    def commit(self): self.db.commits += 1
    def rollback(self): pass


class _Col:
    def __init__(self, n): self.name = n


class _Adapter:
    kind = "postgres"
    def __init__(self, active, inactive):
        self.active, self.inactive, self.sql, self.commits = active, inactive, [], 0
        self.conn = _Conn(self)
        self.live_updates = []
    def introspect_table(self, t): return [_Col("verified_active_at")]


class _FakeEbay:
    def __init__(self, answers):
        self.answers, self.calls = answers, {}
    @property
    def total_calls(self): return sum(self.calls.values())
    def _n(self, op): self.calls[op] = self.calls.get(op, 0) + 1
    def get_item(self, item_id):
        self._n("get_item_by_legacy_id")
        a = self.answers[item_id]
        if isinstance(a, Exception):
            raise a
        return a
    def get_items_by_item_group(self, gid):
        self._n("get_items_by_item_group")
        return self.answers[gid + ":group"]


class TestBrowseRateLimit(unittest.TestCase):
    def setUp(self):
        self.c = EbayClient(client_id="id", client_secret="secret")
        self.c._token, self.c._token_expires = "tok", 1e12

    def test_parses_browse_resource_and_is_not_a_browse_call(self):
        body = {"rateLimits": [{"apiName": "Browse", "resources": [
            {"name": "buy.browse.item.bulk", "rates": [{"limit": 5000, "remaining": 5000}]},
            {"name": "buy.browse", "rates": [{"count": 2000, "limit": 5000, "remaining": 3000,
                                              "reset": "2026-09-25T07:00:00.000Z"}]}]}]}
        urls = []
        def fake(req, timeout=0):
            urls.append(req.full_url)
            return _Resp(json.dumps(body).encode())
        with mock.patch.object(ebay_client.urllib.request, "urlopen", fake):
            got = self.c.browse_rate_limit()
        self.assertEqual(got, {"limit": 5000, "remaining": 3000, "count": 2000,
                               "reset": "2026-09-25T07:00:00.000Z"})
        self.assertIn("/developer/analytics/v1_beta/rate_limit/", urls[0])
        self.assertEqual(self.c.calls, {})

    def test_failure_returns_none(self):
        def boom(req, timeout=0):
            raise _http_error(403, {"errors": []})
        with mock.patch.object(ebay_client.urllib.request, "urlopen", boom):
            self.assertIsNone(self.c.browse_rate_limit())


class TestRepriceListings(unittest.TestCase):
    def _run(self, adapter, ebay, **kw):
        captured = []
        def fake_ev(cur, sql, rows, template=None):
            captured.extend(rows)
            cur.db.sql.append((sql, rows))
        with mock.patch("psycopg2.extras.execute_values", fake_ev):
            stats = ebay_agent._reprice_listings(
                adapter, "ebay_listings", ebay, fk_col="product_id",
                now=NOW, **kw)
        return stats, captured

    def test_outcomes_and_writes(self):
        adapter = _Adapter(
            active=[("1", 11, 10.0, True), ("2", 12, 20.0, True), ("3", 13, 30.0, True)],
            inactive=[("4", 14, 40.0, False), ("5", 15, 50.0, False)])
        ebay = _FakeEbay({
            "1": _item("12.50"),                         # live, price changed
            "2": None,                                   # 404 → ended
            "3": _item(status="OUT_OF_STOCK"),           # unavailable
            "4": EbayItemGroup("4"),                     # group → live
            "4:group": [_item("45.00"), _item("41.00")],
            "5": _item("50.00"),                         # revived, same price
        })
        stats, live = self._run(adapter, ebay, max_calls=50)
        self.assertEqual(stats["checked"], 5)
        self.assertEqual(stats["repriced"], 3)
        self.assertEqual(stats["revived"], 2)
        self.assertEqual(stats["price_changed"], 2)
        self.assertEqual(stats["ended"], 1)
        self.assertEqual(stats["unavailable"], 1)
        self.assertEqual(stats["api_calls"], 6)          # group costs 2
        self.assertEqual(stats["product_ids"], {11, 12, 13, 14, 15})
        self.assertEqual(sorted(r[0] for r in live), ["1", "4", "5"])
        self.assertEqual(dict((r[0], r[1]) for r in live)["4"], 41.0)
        dead_sql = [p for s, p in adapter.sql if "SET is_active = false" in s]
        self.assertEqual(sorted(dead_sql[0][0]), ["2", "3"])
        self.assertTrue(any("verified_active_at = NOW()" in s for s, _ in adapter.sql))
        # never deletes
        self.assertFalse(any("DELETE" in s.upper() for s, _ in adapter.sql))
        self.assertEqual(adapter.commits, 1)

    def test_dry_run_writes_nothing(self):
        adapter = _Adapter(active=[("1", 11, 10.0, True)], inactive=[])
        stats, live = self._run(adapter, _FakeEbay({"1": None}), max_calls=5, dry_run=True)
        self.assertEqual(stats["ended"], 1)
        self.assertEqual(live, [])
        self.assertFalse(any("UPDATE" in s for s, _ in adapter.sql))
        self.assertEqual(adapter.commits, 0)

    def test_call_cap_is_respected(self):
        rows = [(str(i), i, 1.0, True) for i in range(10)]
        adapter = _Adapter(active=rows, inactive=[])
        ebay = _FakeEbay({str(i): _item() for i in range(10)})
        stats, _ = self._run(adapter, ebay, max_calls=4)
        self.assertEqual(stats["api_calls"], 4)
        self.assertEqual(stats["checked"], 4)

    def test_rate_limit_stops_loop_and_keeps_rows(self):
        adapter = _Adapter(active=[("1", 1, 1.0, True), ("2", 2, 1.0, True)], inactive=[])
        ebay = _FakeEbay({"1": EbayRateLimited("429"), "2": None})
        stats, _ = self._run(adapter, ebay, max_calls=10)
        self.assertTrue(stats["rate_limited"])
        self.assertEqual(stats["checked"], 0)
        self.assertEqual(stats["ended"], 0)   # a 429 must never mark anything ended

    def test_lookup_error_is_not_ended(self):
        adapter = _Adapter(active=[("1", 1, 1.0, True)], inactive=[])
        stats, _ = self._run(adapter, _FakeEbay({"1": RuntimeError("500")}), max_calls=10)
        self.assertEqual(stats["errors"], 1)
        self.assertEqual(stats["ended"], 0)

    def test_mass_not_found_is_treated_as_broken_lookup(self):
        rows = [(str(i), i, 1.0, True) for i in range(20)]
        adapter = _Adapter(active=rows + [("oos", 99, 1.0, True)], inactive=[])
        answers = {str(i): None for i in range(20)}
        answers["oos"] = _item(status="OUT_OF_STOCK")
        stats, _ = self._run(adapter, _FakeEbay(answers), max_calls=50)
        self.assertTrue(stats["not_found_suspect"])
        dead_sql = [p for s, p in adapter.sql if "SET is_active = false" in s]
        self.assertEqual(dead_sql[0][0], ["oos"])   # only the confirmed end

    def test_revive_query_includes_unconfirmed_future_end_dates(self):
        adapter = _Adapter(active=[], inactive=[])
        ebay_agent._select_reprice_candidates(
            adapter, "ebay_listings", "product_id", limit=5,
            min_age_hours=12, revive_window_days=90)
        revive_sql = [s for s, _ in adapter.sql if "is_active = false" in s][0]
        self.assertIn("item_end_date IS NULL OR item_end_date > updated_at", revive_sql)

    def test_revive_pass_only_when_budget_left(self):
        adapter = _Adapter(active=[("1", 1, 1.0, True)], inactive=[("9", 9, 1.0, False)])
        ebay = _FakeEbay({"1": _item(), "9": _item()})
        stats, _ = self._run(adapter, ebay, max_calls=1)
        self.assertEqual(stats["checked"], 1)
        self.assertEqual(sum("is_active = false" in s for s, _ in adapter.sql
                             if "SELECT" in s), 0)


class TestProductRollup(unittest.TestCase):
    def test_scoped_to_prefix_and_pricing_filters(self):
        adapter = _Adapter([], [])
        out = ebay_agent._refresh_product_prices(
            adapter, "products", "ebay_listings", "product_id", {3, 1, None},
            key_column="asin", key_prefix="EBAY_")
        (price_sql, price_params), (clear_sql, clear_params) = adapter.sql
        self.assertEqual(price_params, ([1, 3], "EBAY\\_%"))
        self.assertIn("l.price >= 1.0", price_sql)
        self.assertIn("l.currency = 'USD'", price_sql)
        self.assertIn("price_updated_at = NOW()", price_sql)
        self.assertIn("p.asin LIKE", price_sql)
        self.assertIn("SET price = NULL", clear_sql)
        self.assertEqual(adapter.commits, 1)
        self.assertEqual(out, {"priced": 0, "cleared": 0})

    def test_empty_prefix_disables(self):
        adapter = _Adapter([], [])
        ebay_agent._refresh_product_prices(
            adapter, "products", "ebay_listings", "product_id", {1},
            key_column="asin", key_prefix="")
        self.assertEqual(adapter.sql, [])


class TestRepricePhaseProductGate(unittest.TestCase):
    def _phase(self, cfg):
        agent = ebay_agent.EbayProductSyncAgent.__new__(ebay_agent.EbayProductSyncAgent)
        agent._cfg = cfg
        agent.storage = mock.Mock(list_prefix=mock.Mock(return_value=[]))
        ebay = mock.Mock(client_id="app", browse_rate_limit=mock.Mock(return_value=None))
        r = {"checked": 1, "repriced": 1, "revived": 0, "price_changed": 0,
             "ended": 0, "unavailable": 0, "errors": 0, "rate_limited": False,
             "api_calls": 1, "product_ids": {7}, "samples": []}
        with mock.patch.object(ebay_agent, "_reprice_listings", return_value=r), \
             mock.patch.object(ebay_agent, "_refresh_product_prices",
                               return_value={"priced": 1, "cleared": 0}) as roll:
            out = agent._reprice_phase(_Adapter([], []), "products", "ebay_listings",
                                       "product_id", ebay, planned_search_calls=8,
                                       dry_run=False)
        return out, roll

    def test_product_prices_untouched_by_default(self):
        # A destination may keep eBay-stock products.price NULL on purpose.
        out, roll = self._phase({})
        roll.assert_not_called()
        self.assertNotIn("products_repriced", out)
        self.assertEqual(out["repriced"], 1)

    def test_product_prices_opt_in(self):
        out, roll = self._phase({"reprice": {"update_product_prices": True}})
        roll.assert_called_once()
        self.assertEqual(out["products_repriced"], 1)


class TestNoSiteLiterals(unittest.TestCase):
    def test_engine_has_no_site_domain(self):
        src = (_AGENT / "agent.py").read_text()
        self.assertNotIn("specpicks.com", src)


if __name__ == "__main__":
    unittest.main()
