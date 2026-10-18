"""answer_pick_check — the scheduled /k/ -> /dp/ check on top-click pages.

Run: python3 -m pytest tests/test_shelf_audit_answer_picks.py -q
"""
import sys, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "agents" / "shelf-audit-agent"))
import answer_picks as ap  # noqa: E402

NOW = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
DP = {"status": 302, "location": "https://www.amazon.com/dp/B089TQWJKK?tag=x"}
SEARCH = {"status": 302, "location": "https://www.amazon.com/s?k=carote&tag=x"}


class TestPicksAndProbe(unittest.TestCase):
    def test_extracts_picks_and_parts_once(self):
        doc = {"answer_picks": [
            {"label": "Best Overall", "name": "A", "href": "/k/a?source=amazon"},
            {"label": "Build", "name": "B", "href": "/k/b",
             "parts": [{"name": "B1", "href": "/k/b1"}, {"href": "/k/a?source=amazon"}]},
        ]}
        hrefs = [p["href"] for p in ap.extract_pick_hrefs(doc)]
        self.assertEqual(hrefs, ["/k/a?source=amazon", "/k/b", "/k/b1"])
        self.assertEqual(ap.pick_slug("/k/a%20b?source=amazon"), "a b")

    def test_only_a_dp_redirect_is_healthy(self):
        self.assertTrue(ap.is_healthy(DP))
        self.assertFalse(ap.is_healthy(SEARCH))
        self.assertFalse(ap.is_healthy({"status": 404, "location": ""}))
        self.assertFalse(ap.is_healthy({"status": 200, "location": ""}))

    def test_probe_ua_is_flagged_as_a_bot_by_the_click_logger(self):
        # aisleprompt BOT_UA_RE includes "bot" and "monitor"; a probe that
        # counted as a human click would inflate the goal it protects.
        self.assertRegex(ap.UA["User-Agent"].lower(), r"bot|monitor")


class TestStrikes(unittest.TestCase):
    def test_second_strike_8h_later_confirms(self):
        st, conf = ap.apply_strikes({}, {"/k/a": {"pages": ["/blog/x"]}}, set(), NOW, 8)
        self.assertEqual(conf, [])
        st, conf = ap.apply_strikes(st, {"/k/a": {"pages": ["/blog/x"]}}, set(),
                                    NOW + timedelta(hours=4), 8)
        self.assertEqual(conf, [])  # too soon: one API blip is not two strikes
        st, conf = ap.apply_strikes(st, {"/k/a": {"pages": ["/blog/x"]}}, set(),
                                    NOW + timedelta(hours=8), 8)
        self.assertEqual(conf, ["/k/a"])
        self.assertEqual(st["/k/a"]["first_seen"], NOW.isoformat())

    def test_recovery_clears_the_strike(self):
        st, _ = ap.apply_strikes({}, {"/k/a": {}}, set(), NOW, 8)
        st, conf = ap.apply_strikes(st, {}, {"/k/a"}, NOW + timedelta(hours=9), 8)
        self.assertEqual((st, conf), ({}, []))


class TestRemedy(unittest.TestCase):
    SAME = staticmethod(lambda a, b: ("same", 2))
    DIFF = staticmethod(lambda a, b: ("different", 60))

    def test_pack_size_difference_is_left_to_a_person(self):
        # The Nordic Ware case: same brand and photo family, different pack.
        self.assertFalse(ap.same_title(
            "Nordic Ware Naturals Aluminum Half Sheet Pan, 2-Pack",
            "Nordic Ware Naturals Aluminum Half Sheet Pan, 3-Pack"))
        rem = ap.choose_remedy(
            {"title": "Nordic Ware Naturals Half Sheet Pan 2-Pack", "image": "i"},
            [{"slug": "nw-3", "title": "Nordic Ware Naturals Half Sheet Pan 3-Pack", "image": "j"}],
            self.SAME)
        self.assertEqual(rem["action"], "review")

    def test_same_title_and_photo_relinks(self):
        rem = ap.choose_remedy(
            {"title": "CAROTE Nonstick Pots and Pans Set, 10 Pcs, White Granite", "image": "i"},
            [{"slug": "carote-10", "title": "CAROTE Pots and Pans Set Nonstick White Granite 10 Pcs", "image": "j"}],
            self.SAME)
        self.assertEqual((rem["action"], rem["replacement"]), ("relink", "carote-10"))

    def test_same_title_different_photo_is_review(self):
        rem = ap.choose_remedy({"title": "Pan 10 inch", "image": "i"},
                               [{"slug": "p", "title": "Pan 10 inch", "image": "j"}], self.DIFF)
        self.assertEqual(rem["action"], "review")

    def test_no_candidate_unlinks(self):
        self.assertEqual(ap.choose_remedy({"title": "x"}, [], self.SAME)["action"], "unlink")

    def test_other_products_of_the_brand_do_not_block_an_unlink(self):
        # 2026-10-18 dry run: the Joseph Joseph Index set went out of stock;
        # other Joseph Joseph boards are in stock but are different products.
        rem = ap.choose_remedy(
            {"title": "Joseph Joseph Index Plastic Cutting Board Set with Stainless Steel Storage Case", "image": "i"},
            [{"slug": "jj-folio", "title": "Joseph Joseph Folio Chopping Board Set, Icon Edition", "image": "j"}],
            self.SAME)
        self.assertEqual(rem["action"], "unlink")


class TestRunCheck(unittest.TestCase):
    CFG = {"picks_endpoint": "https://s/api/blog/{slug}", "path_prefix": "/blog/",
           "strike_gap_hours": 8}

    def _run(self, state, now):
        docs = {"https://s/api/blog/best-x": {"answer_picks": [
            {"label": "Best Overall", "href": "/k/good"},
            {"label": "Best Value", "href": "/k/dead?source=amazon"}]}}
        probes = {"https://s/k/good": DP, "https://s/k/dead?source=amazon": SEARCH}
        return ap.run_check(self.CFG, origin="https://s", pages=["/blog/best-x"],
                            state=state, now=now, fetch_json=docs.get,
                            probe_fn=probes.get,
                            lookup=lambda slug: ({"title": "Dead Pan 10 inch", "image": "i"}, []),
                            same_picture=lambda a, b: ("same", 0))

    def test_two_runs_8h_apart_produce_one_dispatchable_rec(self):
        r1 = self._run({}, NOW)
        self.assertEqual((r1["picks_checked"], len(r1["broken_now"]), r1["confirmed"]), (2, 1, []))
        self.assertEqual(ap.to_recs(r1, "t1"), [])
        r2 = self._run(r1["state"], NOW + timedelta(hours=8))
        self.assertEqual([c["action"] for c in r2["confirmed"]], ["unlink"])
        recs = ap.to_recs(r2, "t2")
        self.assertEqual([r["id"] for r in recs], ["shelf-t2-answer-pick-dead"])
        self.assertEqual(recs[0]["tier"], "lever")
        self.assertIn("/k/dead?source=amazon", recs[0]["rationale"])


class TestAgentWiring(unittest.TestCase):
    SRC = (_ROOT / "agents" / "shelf-audit-agent" / "agent.py").read_text()

    def test_off_unless_enabled_and_dispatch_is_its_own_flag(self):
        self.assertIn('if not apc.get("enabled")', self.SRC)
        self.assertIn('if not apc.get("dispatch")', self.SRC)
        # Runs before shelf discovery so a failed crawl cannot skip it.
        self.assertLess(self.SRC.index("self._answer_pick_check(cfg)"),
                        self.SRC.index("asins = self._discover(cfg)"))


if __name__ == "__main__":
    unittest.main()
