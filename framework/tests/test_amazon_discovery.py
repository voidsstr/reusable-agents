"""framework.core.amazon_discovery — keyword rotation, the pricing-integrity
gate, and the discover() loop against a fake Creators client (no network)."""
from framework.core import amazon_discovery as ad
from framework.core.amazon_creators import CreatorsRequestError


def _raw(asin, *, price=99.0, currency="USD", condition="New", title="Thing"):
    listing = {"price": {"money": {"amount": price, "currency": currency}},
               "condition": {"value": condition}, "isBuyBoxWinner": True,
               "availability": {"type": "IN_STOCK", "message": "In Stock"}}
    return {"asin": asin,
            "itemInfo": {"title": {"displayValue": title}},
            "offersV2": {"listings": [listing] if price is not None else []}}


class FakeClient:
    def __init__(self, pages, *, budget=10_000, throttle_on=None, error_on=None):
        self.pages = pages              # {(q, page): [raw items]}
        self.calls = []
        self.budget = budget
        self.throttle_on = throttle_on or set()
        self.error_on = error_on or set()
        self.last_search_meta = {}

    def budget_ok(self, consumer="", calls=1):
        return self.budget - len(self.calls) >= calls

    def search_items(self, q, *, search_index="All", item_count=10, item_page=1, **kw):
        self.calls.append((q, item_page, search_index))
        self.last_search_meta = {"throttled": False}
        if q in self.error_on:
            raise CreatorsRequestError("bad", status=400, detail="x")
        if q in self.throttle_on:
            self.last_search_meta["throttled"] = True
            return []
        return self.pages.get((q, item_page), [])


def test_normalize_keywords_dedupes_and_accepts_dicts():
    kws = ad.normalize_keywords(["RTX 5090", {"q": " rtx  5090 "}, {"q": "Ryzen", "category": "cpus",
                                 "min_price": "50"}, "", None, {"keywords": "SSD"}])
    assert [k["q"] for k in kws] == ["RTX 5090", "Ryzen", "SSD"]
    assert kws[1]["category"] == "cpus" and kws[1]["min_price"] == 50.0
    assert kws[0]["category"] is None and kws[0]["min_price"] is None


def test_select_rotation_wraps():
    kws = list("abcde")
    assert ad.select_rotation(kws, 0, 2) == (["a", "b"], 2)
    assert ad.select_rotation(kws, 4, 3) == (["e", "a", "b"], 2)
    assert ad.select_rotation(kws, 7, 10) == (["c", "d", "e", "a", "b"], 2)
    assert ad.select_rotation([], 3, 5) == ([], 0)


def test_listing_rejection_reasons():
    ok = {"title": "x", "price": 10.0, "currency": "USD", "condition": "New"}
    assert ad.listing_rejection(ok) is None
    assert ad.listing_rejection({**ok, "title": ""}) == "no-title"
    assert ad.listing_rejection({**ok, "price": None}) == "no-price"
    assert ad.listing_rejection({**ok, "price": 0.01}) == "price-below-min"
    assert ad.listing_rejection({**ok, "currency": "INR"}) == "currency"
    assert ad.listing_rejection({**ok, "condition": "Used"}) == "used-only"
    assert ad.listing_rejection({**ok, "condition": None}) is None
    assert ad.listing_rejection({**ok, "condition": "Used"}, require_new=False) is None


def test_discover_splits_new_known_and_rejected():
    client = FakeClient({
        ("gpu", 1): [_raw("A1"), _raw("A2"), _raw("A3", condition="Used"),
                     _raw("A4", price=0.5), _raw("A5", currency="CAD")],
        ("cpu", 1): [_raw("A1"), _raw("B1")],   # A1 repeats across keywords
    })
    kws = ad.normalize_keywords([{"q": "gpu", "category": "gpus"}, "cpu"])
    res = ad.discover(client, kws, known_asins=lambda asins: {"A2"} & set(asins))
    assert [c["asin"] for c in res.candidates] == ["A1", "B1"]
    assert res.candidates[0]["_category_hint"] == "gpus"
    assert res.candidates[1]["_keyword"] == "cpu"
    assert [k["asin"] for k in res.seen_known] == ["A2"]
    assert res.rejected == {"used-only": 1, "price-below-min": 1, "currency": 1}
    assert res.api_calls == 2 and res.searched_keywords == 2 and res.stopped_reason == ""


def test_discover_per_keyword_min_price():
    client = FakeClient({("rtx", 1): [_raw("C1", price=12.0), _raw("C2", price=600.0),
                                      _raw("K1", price=120.0), _raw("K2", price=0.5)]})
    kws = ad.normalize_keywords([{"q": "rtx", "min_price": 150}])
    res = ad.discover(client, kws, known_asins=lambda a: {"K1", "K2"} & set(a))
    assert [c["asin"] for c in res.candidates] == ["C2"]
    # the keyword floor gates NEW products only; a carried product below it
    # still gets its (valid) search price — but never a sub-$1 one
    assert [k["asin"] for k in res.seen_known] == ["K1"]


def test_discover_stops_on_caps_budget_and_throttle():
    pages = {(q, 1): [_raw(q + "x")] for q in "abcdef"}
    kws = ad.normalize_keywords(list("abcdef"))

    res = ad.discover(FakeClient(pages), kws, known_asins=lambda a: set(), max_calls=2)
    assert res.api_calls == 2 and res.stopped_reason == "max-calls"

    res = ad.discover(FakeClient(pages), kws, known_asins=lambda a: set(), max_new=3)
    assert len(res.candidates) == 3 and res.stopped_reason == "max-new"

    res = ad.discover(FakeClient(pages, budget=1), kws, known_asins=lambda a: set())
    assert res.api_calls == 1 and res.stopped_reason == "budget-exhausted"

    res = ad.discover(FakeClient(pages, throttle_on={"b"}), kws, known_asins=lambda a: set())
    assert res.throttled == 1 and res.stopped_reason == "throttled"
    assert [c["asin"] for c in res.candidates] == ["ax"]


def test_discover_skips_request_errors_and_pages_only_when_full():
    full = [_raw(f"P{i}") for i in range(10)]
    client = FakeClient({("a", 1): full, ("a", 2): [_raw("P10")], ("b", 1): [_raw("Q1")]},
                        error_on={"err"})
    kws = ad.normalize_keywords(["a", "err", "b"])
    res = ad.discover(client, kws, known_asins=lambda a: set(), pages_per_keyword=3)
    assert ("a", 2, "All") in client.calls and ("a", 3, "All") not in client.calls
    assert res.errors == 1 and res.searched_keywords == 2
    assert len(res.candidates) == 12


def test_listing_rejection_unavailable_but_orderable_ok():
    ok = {"title": "x", "price": 10.0, "currency": "USD", "condition": "New"}
    assert ad.listing_rejection({**ok, "availability_type": "OUT_OF_STOCK"}) == "unavailable"
    for t in ("IN_STOCK", "IN_STOCK_SCARCE", "PREORDER", "LEADTIME", None):
        assert ad.listing_rejection({**ok, "availability_type": t}) is None


def test_cursor_after_resumes_at_first_unreached_keyword():
    kws = ad.normalize_keywords(list("abcdefgh"))
    picked, full_cursor = ad.select_rotation(kws, 6, 4)          # g h a b
    pages = {(q, 1): [_raw(q + "x")] for q in "abcdefgh"}

    res = ad.discover(FakeClient(pages), picked, known_asins=lambda a: set())
    assert ad.cursor_after(6, len(kws), picked, res) == full_cursor == 2

    # budget for 2 calls: g, h searched → resume at a (index 0), not skip to c
    res = ad.discover(FakeClient(pages, budget=2), picked, known_asins=lambda a: set())
    assert res.stopped_reason == "budget-exhausted"
    assert ad.cursor_after(6, len(kws), picked, res) == 0

    # 429 on h: g done, h not → resume at h
    res = ad.discover(FakeClient(pages, throttle_on={"h"}), picked, known_asins=lambda a: set())
    assert ad.cursor_after(6, len(kws), picked, res) == 7

    # a request error is a bad keyword: move past it
    res = ad.discover(FakeClient(pages, error_on={"g"}), picked, known_asins=lambda a: set())
    assert ad.cursor_after(6, len(kws), picked, res) == 2

    # nothing searched (budget 0) → cursor unchanged
    res = ad.discover(FakeClient(pages, budget=0), picked, known_asins=lambda a: set())
    assert ad.cursor_after(6, len(kws), picked, res) == 6
    assert ad.cursor_after(3, 0, [], res) == 0
