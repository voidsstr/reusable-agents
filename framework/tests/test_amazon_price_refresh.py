"""Tests for framework.core.amazon_price_refresh (no network, no storage)."""
from __future__ import annotations

import pytest

from framework.core.amazon_creators import CreatorsRequestError, CreatorsUnavailable
from framework.core.amazon_price_refresh import (
    PRICE_RESOURCES, classify_item, refresh_prices,
)


def _item(asin, amount=99.99, currency="USD", avail="IN_STOCK", condition="New",
          basis=None, listings=True):
    if not listings:
        return {"asin": asin, "offersV2": {"listings": []}}
    price = {"money": {"amount": amount, "currency": currency}}
    if basis is not None:
        price["savingBasis"] = {"money": {"amount": basis, "currency": currency},
                                "savingBasisType": "LIST_PRICE"}
    return {"asin": asin, "offersV2": {"listings": [{
        "price": price,
        "availability": {"type": avail, "message": "In Stock" if avail == "IN_STOCK" else avail},
        "condition": {"value": condition},
        "isBuyBoxWinner": True,
    }]}}


class FakeClient:
    def __init__(self, responses, *, throttle_after=None, error_on=None, budget=None,
                 unavailable_after=None):
        self.responses = responses  # asin -> raw item | "ERR:<msg>" | None (omitted)
        self.calls = []
        self.throttled_remainder = []
        self.throttle_after = throttle_after
        self.unavailable_after = unavailable_after
        self.error_on = error_on or set()
        self.track_usage = budget is not None
        self._budget = budget

    def remaining_budget(self):
        return self._budget

    def get_items(self, asins, *, resources=None):
        assert resources == PRICE_RESOURCES
        self.throttled_remainder = []
        if self.throttle_after is not None and len(self.calls) >= self.throttle_after:
            self.throttled_remainder = list(asins)
            return {}, {}
        if self.unavailable_after is not None and len(self.calls) >= self.unavailable_after:
            raise CreatorsUnavailable("Creators getItems unreachable: timed out")
        self.calls.append(list(asins))
        if set(asins) & self.error_on:
            raise CreatorsRequestError("bad", status=400, detail="bad")
        items, errors = {}, {}
        for a in asins:
            r = self.responses.get(a)
            if isinstance(r, str) and r.startswith("ERR:"):
                errors[a] = r[4:]
            elif r is not None:
                items[a] = r
        return items, errors


def test_classify_priced_with_list_price():
    o = classify_item("A1", _item("A1", 856.99, basis=909.99))
    assert o.status == "priced"
    assert o.price == 856.99 and o.original_price == 909.99 and o.currency == "USD"
    assert not o.marks_unavailable


def test_classify_scarce_and_preorder_are_priced():
    assert classify_item("A", _item("A", avail="IN_STOCK_SCARCE")).status == "priced"
    assert classify_item("A", _item("A", avail="PREORDER")).status == "priced"


def test_classify_out_of_stock_clears_price():
    o = classify_item("A", _item("A", avail="OUT_OF_STOCK"))
    assert o.status == "unavailable" and o.price is None and o.original_price is None
    assert o.marks_unavailable


def test_classify_out_of_stock_without_price_is_unavailable():
    # Live shape 2026-09-24: availability block, no price.
    raw = {"asin": "A", "offersV2": {"listings": [{
        "availability": {"type": "OUT_OF_STOCK", "message": "Currently unavailable."}}]}}
    o = classify_item("A", raw)
    assert o.status == "unavailable" and o.price is None
    assert o.availability == "Currently unavailable."


def test_classify_non_usd_never_priced():
    o = classify_item("A", _item("A", 659.0, currency="INR"))
    assert o.status == "non_usd" and o.price is None


def test_classify_used_only_is_no_offer():
    o = classify_item("A", _item("A", 771.20, condition="Used", avail="IN_STOCK_SCARCE"))
    assert o.status == "no_offer" and o.price is None
    # The used listing's "Only 2 left" must not sit next to a NULL price.
    assert o.availability is None


def test_classify_available_date_is_priced():
    assert classify_item("A", _item("A", avail="AVAILABLE_DATE")).status == "priced"


def test_classify_no_listings_and_not_found_and_missing():
    assert classify_item("A", _item("A", listings=False)).status == "no_offer"
    nf = classify_item("A", None, "ItemNotAccessible A")
    assert nf.status == "not_found" and nf.marks_unavailable and nf.error
    assert classify_item("A", None).status == "missing"


def test_refresh_batches_by_ten_and_classifies():
    asins = [f"B{i:09d}" for i in range(23)]
    resp = {a: _item(a) for a in asins}
    resp[asins[0]] = "ERR:ItemNotAccessible " + asins[0]
    resp[asins[1]] = None
    client = FakeClient(resp)
    seen = []
    outcomes, stats = refresh_prices(client, asins + [asins[2]], on_batch=seen.append)
    assert [len(c) for c in client.calls] == [10, 10, 3]
    assert stats["api_calls"] == 3 and stats["untried"] == []
    assert stats["priced"] == 21 and stats["not_found"] == 1 and stats["missing"] == 1
    assert sum(len(b) for b in seen) == 23


def test_refresh_respects_max_calls_and_budget():
    asins = [f"B{i:09d}" for i in range(50)]
    client = FakeClient({a: _item(a) for a in asins}, budget=2)
    _, stats = refresh_prices(client, asins, max_calls=4)
    assert stats["api_calls"] == 2 and stats["budget_calls"] == 2
    assert len(stats["untried"]) == 30


def test_refresh_stops_on_throttle_keeps_work():
    asins = [f"B{i:09d}" for i in range(30)]
    client = FakeClient({a: _item(a) for a in asins}, throttle_after=1)
    outcomes, stats = refresh_prices(client, asins)
    assert stats["throttled"] == 1 and stats["api_calls"] == 1
    assert len(outcomes) == 10 and len(stats["untried"]) == 20


def test_refresh_bad_batch_does_not_end_sweep():
    asins = [f"B{i:09d}" for i in range(20)]
    client = FakeClient({a: _item(a) for a in asins}, error_on={asins[0]})
    seen = []
    outcomes, stats = refresh_prices(client, asins, on_batch=seen.append)
    assert stats["errors"] == 1 and stats["api_calls"] == 1 and stats["priced"] == 10
    assert stats["error"] == 10
    assert sum(len(b) for b in seen) == 10  # error batch is never handed to the writer


def test_refresh_aborts_after_consecutive_batch_errors():
    asins = [f"B{i:09d}" for i in range(60)]
    client = FakeClient({a: _item(a) for a in asins}, error_on=set(asins[10:]))
    seen = []
    _, stats = refresh_prices(client, asins, on_batch=seen.append)
    # 1 good batch, then 3 failed in a row -> stop; 2 batches never tried.
    assert stats["api_calls"] == 1 and stats["errors"] == 3
    assert stats["aborted"] and len(stats["untried"]) == 20
    assert sum(len(b) for b in seen) == 10


def test_refresh_unreachable_midway_keeps_work():
    asins = [f"B{i:09d}" for i in range(30)]
    client = FakeClient({a: _item(a) for a in asins}, unavailable_after=2)
    seen = []
    _, stats = refresh_prices(client, asins, on_batch=seen.append)
    assert stats["api_calls"] == 2 and "unreachable" in stats["aborted"]
    assert sum(len(b) for b in seen) == 20 and len(stats["untried"]) == 10


def test_refresh_unreachable_at_start_raises():
    client = FakeClient({}, unavailable_after=0)
    with pytest.raises(CreatorsUnavailable):
        refresh_prices(client, ["B000000001"])


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
