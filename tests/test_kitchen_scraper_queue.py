"""Regression test for the Amazon reprice queue head-of-line block.

Found 2026-08-28 via the KTLO §5c stale gauge: 56,033 of 66,104 aisleprompt
Amazon listings (85%) were >30 days stale and the backlog could never converge.

Cause: Amazon returns these items successfully but with no offer, so
`parse_item()` yields price=None. The agent skipped them with a bare
`continue` -- no price write (correct) but also NO `fetched_at` stamp. Because
the work queue is `ORDER BY fetched_at NULLS FIRST`, those rows sat permanently
at the head: every run re-fetched the same dead ASINs and exhausted its budget
before reaching rows that could actually be repriced. Measured: all 100 of the
stalest ASINs came back priceless, and the run repriced ~120 of a 1000 cap.

After stamping them (as the ItemNotAccessible path already did), one run drained
1000 rows and repriced 867.

Run: python3 -m pytest tests/test_kitchen_scraper_queue.py -q
"""
import re, unittest
from pathlib import Path

_AGENT = Path("/home/voidsstr/development/aisleprompt/agents/kitchen-scraper/agent.py")


class TestNoOfferRowsAgeDown(unittest.TestCase):
    def setUp(self):
        self.src = _AGENT.read_text()
        m = re.search(r"if price is None or price < 1\.0:(.*?)\n\s+continue",
                      self.src, re.S)
        self.assertIsNotNone(m, "the no-offer skip branch moved; re-anchor this test")
        self.branch = m.group(1)

    def test_no_offer_rows_are_stamped(self):
        """Old-buggy value: a bare `continue` with no UPDATE at all."""
        self.assertIn("fetched_at = NOW()", self.branch,
                      "a priceless item must still age down the queue")
        self.assertIn("UPDATE kitchen_product_listings", self.branch)

    def test_no_offer_never_writes_a_price(self):
        """The guard that made the bug tempting must survive the fix."""
        self.assertNotIn("price_cents", self.branch,
                         "must never overwrite a known price with a missing one")

    def test_queue_is_still_stalest_first(self):
        """The stamp only helps because the queue is ordered by fetched_at."""
        self.assertIn("ORDER BY fetched_at NULLS FIRST", self.src)

    def test_no_offer_is_reported(self):
        """A silently-dropped row is how this hid for months."""
        self.assertIn("creators_no_offer", self.src, "metric must be recorded")
        self.assertIn("no_offer=", self.src, "must appear in the run summary")


if __name__ == "__main__":
    unittest.main()
