"""Regression tests for shelf-audit-agent — encodes the traps that cost time.

Each asserts BOTH the fixed behaviour and the old buggy value.
Run: python3 -m pytest tests/test_shelf_audit.py -q
"""
import sys, unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "agents" / "shelf-audit-agent"))
import shelf  # noqa: E402

_AGENT_SRC = (_ROOT / "agents" / "shelf-audit-agent" / "agent.py").read_text()


class TestImageNormalization(unittest.TestCase):
    """Amazon serves one product under several image IDs, and two renditions
    of the SAME id hash 117/256 bits apart. Comparing raw URLs reported ~99%
    of images wrong where the measured rate is 10-14%."""

    BASE = "https://m.media-amazon.com/images/I/71UBG06NKFL"

    def test_renditions_of_same_image_normalize_equal(self):
        a = shelf.normalize_image(self.BASE + "._SL500_.jpg")
        b = shelf.normalize_image(self.BASE + "._AC_SX679_.jpg")
        c = shelf.normalize_image(self.BASE + "._AC_SL1500_.jpg")
        self.assertEqual(a, b)
        self.assertEqual(b, c)
        # old buggy behaviour: comparing the raw strings
        self.assertNotEqual(self.BASE + "._SL500_.jpg",
                            self.BASE + "._AC_SX679_.jpg")

    def test_different_images_stay_different(self):
        self.assertNotEqual(
            shelf.normalize_image(self.BASE + "._SL500_.jpg"),
            shelf.normalize_image(
                "https://m.media-amazon.com/images/I/31QMvD-j0EL._SL500_.jpg"))

    def test_non_amazon_url_passes_through(self):
        u = "https://example.com/x.png"
        self.assertEqual(shelf.normalize_image(u), u)


class TestCrawlBudget(unittest.TestCase):
    """specpicks' homepage alone links 408 pages, so a GLOBAL 400-page cap was
    spent entirely at depth 1 and depths 2-3 were never crawled."""

    def test_budget_is_per_depth_not_global(self):
        import inspect
        src = inspect.getsource(shelf.crawl)
        self.assertIn("per_depth", src)
        self.assertIn("taken[d]", src)
        self.assertNotIn("len(seen) < max_pages", src)   # the old global cap


class TestPriceComparison(unittest.TestCase):
    ROW = {"db_price_cents": 1000, "title": "t", "brand": "b", "db_image": "x"}

    def test_drift_beyond_tolerance_flagged(self):
        # the real worst case found on aisleprompt: $48.02 shown, $14.88 actual
        row = dict(self.ROW, db_price_cents=4802)
        issues = shelf.compare_row(row, {"price": 14.88, "in_stock": True,
                                         "image_url": "x"}, check_images=False)
        self.assertTrue(any(i.startswith("price_drift") for i in issues), issues)

    def test_within_tolerance_not_flagged(self):
        issues = shelf.compare_row(self.ROW, {"price": 10.10, "in_stock": True,
                                              "image_url": "x"}, check_images=False)
        self.assertFalse(any(i.startswith("price_drift") for i in issues), issues)

    def test_out_of_stock_flagged(self):
        self.assertIn("out_of_stock_on_amazon",
                      shelf.compare_row(self.ROW, {"price": 10.00, "in_stock": False,
                                                   "image_url": "x"},
                                        check_images=False))

    def test_missing_brand_flagged(self):
        row = dict(self.ROW, brand="")
        self.assertIn("missing_brand",
                      shelf.compare_row(row, {"price": 10.00, "in_stock": True,
                                              "image_url": "x"},
                                        check_images=False))


class TestMonitorSemantics(unittest.TestCase):
    """This is a monitor: recurring findings MUST re-surface. The proposal
    dedup collapsed 257 findings into 1 rec on the second run."""

    def test_no_producer_history_dedup(self):
        self.assertNotIn("filter_proposals_against_history(recs)", _AGENT_SRC)
        self.assertNotIn("record_emitted_proposals(recs)", _AGENT_SRC)


class TestAmazonTupleContract(unittest.TestCase):
    """get_items returns (items, errors) — treating it as a dict silently
    failed all 974 lookups with "'tuple' object has no attribute 'items'"."""

    def test_lookup_unpacks_the_tuple(self):
        self.assertIn("got, aerrs = client.get_items(chunk)", _AGENT_SRC)


class TestDispatchGate(unittest.TestCase):
    """Findings route to the implementer only when the site opts in. The
    remedy writes to LIVE catalog data and observed drifts span -95% to
    +279%, so the default must be off."""

    def _agent(self):
        import sys, types
        sys.path.insert(0, str(_ROOT / "agents" / "shelf-audit-agent"))
        sys.argv = ["x"]
        import agent as A

        class Fake(A.ShelfAuditAgent):
            def __init__(self):
                self.agent_id = "test-shelf"; self.run_ts = "T"
            def decide(self, *a, **k):
                pass
        return Fake()

    RECS = [{"id": "r%d" % i, "tier": "lever"} for i in range(1, 5)] + \
           [{"id": "r5", "tier": "smaller"}]

    def test_default_is_off(self):
        n, note = self._agent()._maybe_dispatch(self.RECS, {})
        self.assertEqual(n, 0)
        self.assertIn("dispatch off", note)

    def test_tier_gate_excludes_smaller(self):
        n, note = self._agent()._maybe_dispatch(
            [{"id": "s", "tier": "smaller"}], {"dispatch_findings": "true"})
        self.assertEqual(n, 0)

    def test_cap_and_tier_respected_when_enabled(self):
        import types
        import framework.core.dispatch as D
        calls = {}
        orig = D.gated_dispatch_now
        D.gated_dispatch_now = lambda **kw: (
            calls.update(kw) or types.SimpleNamespace(fell_back_to_queue=False))
        try:
            n, _ = self._agent()._maybe_dispatch(
                self.RECS, {"dispatch_findings": "true", "max_dispatch": 3})
        finally:
            D.gated_dispatch_now = orig
        self.assertEqual(n, 3)
        self.assertEqual(calls["rec_ids"], ["r1", "r2", "r3"])
        self.assertNotIn("r5", calls["rec_ids"])   # smaller tier excluded


if __name__ == "__main__":
    unittest.main(verbosity=2)
