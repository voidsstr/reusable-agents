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

    def test_extra_picks_keys_add_a_deals_table(self):
        # aisleprompt growth-20261110T101200Z-02: the Black Friday page has no
        # answer_picks; its table rows come back as deal_table_picks.
        doc = {"answer_picks": [], "deal_table_picks": [
            {"label": "Air fryer", "name": "Ninja", "href": "/k/ninja"},
            {"label": "Slow cooker", "name": "Crock-Pot", "href": "/k/crock"},
        ]}
        self.assertEqual(ap.extract_pick_hrefs(doc), [])
        hrefs = [p["href"] for p in ap.extract_pick_hrefs(doc, extra_keys=["deal_table_picks"])]
        self.assertEqual(hrefs, ["/k/ninja", "/k/crock"])

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


class TestFastWindow(unittest.TestCase):
    """Cyber Week (aisleprompt growth-20261020T001200Z-01): a same-product
    relink goes out on the first strike inside the window; an unlink still
    waits for the second."""
    WINDOW = {"start": "2026-11-24", "end": "2026-12-02", "one_strike_actions": ["relink"]}

    def _run(self, now, cands, windows):
        cfg = {**TestRunCheck.CFG, "fast_windows": windows}
        docs = {"https://s/api/blog/best-x": {"answer_picks": [
            {"label": "Best Overall", "href": "/k/dead"}]}}
        return ap.run_check(cfg, origin="https://s", pages=["/blog/best-x"],
                            state={}, now=now, fetch_json=docs.get,
                            probe_fn={"https://s/k/dead": SEARCH}.get,
                            lookup=lambda slug: ({"title": "Dead Pan 10 inch", "image": "i"}, cands),
                            same_picture=lambda a, b: ("same", 0))

    def test_window_bounds_are_inclusive_dates(self):
        w = [self.WINDOW, {"start": "bad", "end": "2026-12-31"}]
        cfg = {"fast_windows": w}
        self.assertIsNone(ap.active_fast_window(cfg, datetime(2026, 11, 23, 23, tzinfo=timezone.utc)))
        self.assertEqual(ap.active_fast_window(cfg, datetime(2026, 11, 24, 0, tzinfo=timezone.utc)), self.WINDOW)
        self.assertEqual(ap.active_fast_window(cfg, datetime(2026, 12, 2, 23, tzinfo=timezone.utc)), self.WINDOW)
        self.assertIsNone(ap.active_fast_window(cfg, datetime(2026, 12, 3, 0, tzinfo=timezone.utc)))
        self.assertIsNone(ap.active_fast_window({}, NOW))

    def test_relink_confirmed_on_first_strike_inside_window(self):
        in_window = datetime(2026, 11, 27, 12, tzinfo=timezone.utc)
        r = self._run(in_window, [{"slug": "pan-b", "title": "Dead Pan 10 inch", "image": "j"}], [self.WINDOW])
        self.assertEqual([(c["action"], c["strikes"]) for c in r["confirmed"]], [("relink", 1)])
        recs = ap.to_recs(r, "t")
        self.assertIn("one strike", recs[0]["rationale"])

    def test_unlink_still_waits_inside_window(self):
        in_window = datetime(2026, 11, 27, 12, tzinfo=timezone.utc)
        self.assertEqual(self._run(in_window, [], [self.WINDOW])["confirmed"], [])

    def test_relink_waits_outside_window(self):
        before = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)
        r = self._run(before, [{"slug": "pan-b", "title": "Dead Pan 10 inch", "image": "j"}], [self.WINDOW])
        self.assertEqual(r["confirmed"], [])


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
