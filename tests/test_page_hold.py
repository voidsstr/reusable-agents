"""page_hold — pages no agent edits automatically until a person releases them.

Run: python3 -m pytest tests/test_page_hold.py -q
"""
import sys, unittest
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "agents" / "shelf-audit-agent"))
from framework.core import page_hold as ph  # noqa: E402
import answer_picks as ap  # noqa: E402

IP = "/blog/best-instant-pots-electric-pressure-cookers-2026"
CFG = {"page_holds": [{"path": "https://aisleprompt.com" + IP + "/", "reason": "needs a person",
                       "until": "2026-12-02", "allow_dispatch_kinds": ["crash-fix"]}]}


class TestPageHold(unittest.TestCase):
    def setUp(self):
        self.holds = ph.holds_from_site_config(CFG)

    def test_parses_and_normalizes(self):
        self.assertEqual([h.path for h in self.holds], [IP])
        self.assertEqual(self.holds[0].until, date(2026, 12, 2))
        # implementer block is read too, and duplicates collapse
        both = ph.holds_from_site_config({**CFG, "implementer": {"page_holds": [IP]}})
        self.assertEqual(len(both), 1)

    def test_rec_touching_the_page_by_url_or_slug_is_held(self):
        today = date(2026, 10, 24)
        by_url = {"id": "r1", "target_urls": [IP]}
        by_slug = {"id": "r2", "fix": "UPDATE editorial_articles SET body_md = ... WHERE slug = "
                                      "'best-instant-pots-electric-pressure-cookers-2026'"}
        other = {"id": "r3", "target_urls": ["/blog/best-air-fryers-2026"]}
        self.assertTrue(ph.rec_holds(by_url, self.holds, today=today))
        self.assertTrue(ph.rec_holds(by_slug, self.holds, today=today))
        self.assertFalse(ph.rec_holds(other, self.holds, today=today))
        self.assertIn("page-hold", ph.deferral_reason(ph.rec_holds(by_url, self.holds, today=today)))

    def test_override_expiry_and_dispatch_kind_release(self):
        rec = {"id": "r1", "target_urls": [IP]}
        self.assertFalse(ph.rec_holds({**rec, "hold_override": True}, self.holds, today=date(2026, 10, 24)))
        self.assertFalse(ph.rec_holds(rec, self.holds, today=date(2026, 12, 3)))
        self.assertFalse(ph.rec_holds(rec, self.holds, dispatch_kind="crash-fix", today=date(2026, 10, 24)))

    def test_shelf_audit_routes_held_page_fixes_to_a_person(self):
        result = {"confirmed": [
            {"href": "/k/duo", "pages": [IP], "action": "unlink", "label": "Best Overall"},
            {"href": "/k/rio", "pages": [IP], "action": "relink", "replacement": "rio-2", "label": "Beginner"},
            {"href": "/k/af", "pages": ["/blog/best-air-fryers-2026"], "action": "unlink", "label": "Best Value"},
        ]}
        recs = ap.to_recs(result, "t", held_pages={IP})
        dead = next(r for r in recs if r["id"].endswith("answer-pick-dead"))
        review = next(r for r in recs if r["id"].endswith("answer-pick-review"))
        self.assertEqual([p["href"] for p in dead["answer_picks"]], ["/k/af"])
        self.assertEqual(sorted(p["href"] for p in review["answer_picks"]), ["/k/duo", "/k/rio"])
        self.assertIn("PAGE ON HOLD", review["rationale"])
        # without holds nothing changes
        self.assertEqual(len(ap.to_recs(result, "t")[0]["answer_picks"]), 3)


if __name__ == "__main__":
    unittest.main()
