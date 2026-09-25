"""Amazon Creators: searchItems + parse_item on search results + the shared
daily call budget. No network: urlopen is mocked, the ledger lives in the
tmp LocalFilesystemStorage from conftest.

The fixture item is trimmed from a real searchItems response captured
2026-09-24 ("RTX 5070", searchIndex=Electronics).
"""
import copy
import io
import json
import urllib.error
from unittest import mock

import pytest

from framework.core import amazon_creators as ac


SEARCH_ITEM = {
    "asin": "B0DS6WPTLL",
    "browseNodeInfo": {"browseNodes": [{
        "contextFreeName": "Computer Graphics Cards", "displayName": "Graphics Cards",
        "id": "284822", "isRoot": False}]},
    "detailPageURL": "https://www.amazon.com/dp/B0DS6WPTLL?tag=t-20&linkCode=osi&th=1&psc=1",
    "images": {"primary": {"large": {
        "height": 500, "url": "https://m.media-amazon.com/images/I/41zeOBYIU9L._SL500_.jpg",
        "width": 500}}},
    "itemInfo": {
        "byLineInfo": {"brand": {"displayValue": "ASUS", "label": "Brand", "locale": "en_US"},
                       "manufacturer": {"displayValue": "ASUS", "label": "Manufacturer",
                                        "locale": "en_US"}},
        "classifications": {
            "binding": {"displayValue": "Personal Computers", "label": "Binding"},
            "productGroup": {"displayValue": "Personal Computer", "label": "ProductGroup"}},
        "features": {"displayValues": ["AI Performance: 1005 AI TOPS"]},
        "title": {"displayValue": "ASUS Prime GeForce RTX 5070 12GB GDDR7 OC Edition",
                  "label": "Title", "locale": "en_US"},
    },
    "offersV2": {"listings": [{
        "availability": {"maxOrderQuantity": 1, "message": "In Stock",
                         "minOrderQuantity": 1, "type": "IN_STOCK"},
        "condition": {"conditionNote": "", "subCondition": "Unknown", "value": "New"},
        "isBuyBoxWinner": True,
        "merchantInfo": {"id": "ATVPDKIKX0DER", "name": "Amazon.com"},
        "price": {
            "money": {"amount": 856.99, "currency": "USD", "displayAmount": "$856.99"},
            "savingBasis": {"money": {"amount": 909.99, "currency": "USD",
                                      "displayAmount": "$909.99"},
                            "savingBasisType": "LIST_PRICE",
                            "savingBasisTypeLabel": "List Price:"},
            "savings": {"money": {"amount": 53.0, "currency": "USD"}, "percentage": 6},
        },
        "violatesMAP": False,
    }]},
}

SEARCH_RESPONSE = {"searchResult": {
    "items": [SEARCH_ITEM, {"no_asin": True}],
    "searchURL": "https://www.amazon.com/s?k=RTX+5070&tag=t-20",
    "totalResultCount": 1000,
}}


class _Resp:
    def __init__(self, body): self._b = json.dumps(body).encode()
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return self._b


def _http_error(code, body=b"", headers=None):
    return urllib.error.HTTPError("u", code, "err", headers or {}, io.BytesIO(body))


def _client(storage=None, consumer="discovery", **kw):
    cfg = ac.CreatorsConfig(client_id="id", client_secret="sec", partner_tag="t-20")
    c = ac.AmazonCreatorsClient(cfg, min_interval_s=0, consumer=consumer,
                                storage=storage, **kw)
    c._token = ac._Token("tok", 9e12)  # skip the LWA round-trip
    return c


@pytest.fixture(autouse=True)
def _pin_day(monkeypatch):
    monkeypatch.setattr(ac, "_utc_day", lambda: "2026-09-24")


# -- search_items -------------------------------------------------------------

def test_search_items_request_shape_and_result(storage):
    c = _client(storage)
    sent = []

    def fake(req, timeout=None):
        sent.append(req)
        return _Resp(SEARCH_RESPONSE)

    with mock.patch.object(ac.urllib.request, "urlopen", side_effect=fake):
        items = c.search_items("RTX 5070", search_index="Electronics", item_count=25,
                               item_page=2, browse_node_id="284822", sort_by="Price:LowToHigh",
                               brand=None, minPrice=10000)
    assert [i["asin"] for i in items] == ["B0DS6WPTLL"]  # asin-less entries dropped
    req = sent[0]
    assert req.full_url == "https://creatorsapi.amazon/catalog/v1/searchItems"
    assert req.get_header("X-marketplace") == "www.amazon.com"
    body = json.loads(req.data)
    assert body["keywords"] == "RTX 5070"
    assert body["searchIndex"] == "Electronics"
    assert body["itemCount"] == 10          # clamped to the API ceiling
    assert body["itemPage"] == 2
    assert body["browseNodeId"] == "284822"  # snake_case → camelCase
    assert body["sortBy"] == "Price:LowToHigh"
    assert body["minPrice"] == 10000         # camelCase passes through
    assert "brand" not in body               # None filters dropped
    assert body["partnerTag"] == "t-20" and body["partnerType"] == "Associates"
    assert body["resources"] == list(ac.SEARCH_RESOURCES)
    assert c.last_search_meta["total_result_count"] == 1000
    assert c.last_search_meta["search_url"].startswith("https://www.amazon.com/s?")
    assert c.last_search_meta["throttled"] is False


def test_search_items_page_one_and_no_keywords_omit_fields(storage):
    c = _client(storage)
    sent = []
    with mock.patch.object(ac.urllib.request, "urlopen",
                           side_effect=lambda r, timeout=None: sent.append(r) or _Resp({})):
        assert c.search_items("", browse_node_id="284822") == []
    body = json.loads(sent[0].data)
    assert "itemPage" not in body and "keywords" not in body
    assert body["searchIndex"] == "All" and body["itemCount"] == 10


def test_search_items_throttled_returns_empty(storage):
    c = _client(storage)
    with mock.patch.object(ac.urllib.request, "urlopen",
                           side_effect=lambda r, timeout=None: (_ for _ in ()).throw(
                               _http_error(429, b"TooManyRequests"))), \
         mock.patch.object(ac.time, "sleep"):
        assert c.search_items("gpu") == []
    assert c.last_search_meta["throttled"] is True


def test_search_items_no_results_is_empty_not_error(storage):
    c = _client(storage)
    err = _http_error(404, b'{"errors":[{"code":"NoResults","message":"No results."}]}')
    with mock.patch.object(ac.urllib.request, "urlopen", side_effect=err):
        assert c.search_items("zzzz-no-such-thing") == []


def test_search_items_other_http_error_raises_typed_runtime_error(storage):
    c = _client(storage)
    with mock.patch.object(ac.urllib.request, "urlopen",
                           side_effect=_http_error(400, b"InvalidParameterValue")):
        with pytest.raises(ac.CreatorsRequestError) as ei:
            c.search_items("gpu", search_index="Bogus")
    assert isinstance(ei.value, RuntimeError)  # old `except RuntimeError` callers still work
    assert ei.value.status == 400 and "InvalidParameterValue" in ei.value.detail


# -- parse_item ---------------------------------------------------------------

def test_parse_item_search_result():
    p = ac.parse_item(SEARCH_ITEM)
    assert p["asin"] == "B0DS6WPTLL"
    assert p["title"].startswith("ASUS Prime GeForce RTX 5070")
    assert p["brand"] == "ASUS" and p["manufacturer"] == "ASUS"
    assert p["url"] == SEARCH_ITEM["detailPageURL"] and "tag=t-20" in p["url"]
    assert p["price"] == 856.99 and p["currency"] == "USD"
    assert p["original_price"] == 909.99 and p["original_price_type"] == "LIST_PRICE"
    assert p["availability"] == "In Stock" and p["availability_type"] == "IN_STOCK"
    assert p["in_stock"] is True
    assert p["condition"] == "New" and p["merchant"] == "Amazon.com"
    assert p["image_url"].endswith("_SL500_.jpg")
    assert p["browse_nodes"] == [{"id": "284822", "name": "Graphics Cards",
                                  "context_free_name": "Computer Graphics Cards"}]
    assert p["product_group"] == "Personal Computer"
    assert p["binding"] == "Personal Computers"
    assert p["features"] == ["AI Performance: 1005 AI TOPS"]
    # searchItems returned no reviews / sales rank — present as None, not missing.
    assert p["rating"] is None and p["review_count"] is None and p["sales_rank"] is None


def _listing(amount, *, cond=None, buybox=False, avail="IN_STOCK"):
    l = {"price": {"money": {"amount": amount, "currency": "USD"}},
         "availability": {"type": avail, "message": avail}, "isBuyBoxWinner": buybox}
    if cond:
        l["condition"] = {"value": cond}
    return l


def test_parse_item_prefers_new_over_used_buybox():
    raw = {"asin": "B1", "offersV2": {"listings": [
        _listing(771.20, cond="Used", buybox=True, avail="IN_STOCK_SCARCE"),
        _listing(937.39, cond="New"),
    ]}}
    p = ac.parse_item(raw)
    assert p["price"] == 937.39 and p["condition"] == "New"


def test_parse_item_used_only_is_labelled_used():
    raw = {"asin": "B1", "offersV2": {"listings": [_listing(771.20, cond="Used", buybox=True)]}}
    p = ac.parse_item(raw)
    assert p["price"] == 771.20 and p["condition"] == "Used"


def test_parse_item_without_condition_keeps_buybox_rule():
    raw = {"asin": "B1", "offersV2": {"listings": [_listing(10.0), _listing(12.0, buybox=True)]}}
    assert ac.parse_item(raw)["price"] == 12.0


@pytest.mark.parametrize("atype,expected", [
    ("IN_STOCK", True), ("IN_STOCK_SCARCE", True),
    ("OUT_OF_STOCK", False), ("PREORDER", False), (None, False),
])
def test_parse_item_in_stock_types(atype, expected):
    raw = {"asin": "B1", "offersV2": {"listings": [_listing(5.0, avail=atype)]}}
    assert ac.parse_item(raw)["in_stock"] is expected


def test_parse_item_drops_negative_discount_and_its_type():
    item = copy.deepcopy(SEARCH_ITEM)
    item["offersV2"]["listings"][0]["price"]["savingBasis"]["money"]["amount"] = 800.0
    p = ac.parse_item(item)
    assert p["original_price"] is None and p["original_price_type"] is None


def test_parse_item_empty_item_is_safe():
    p = ac.parse_item({"asin": "B1"})
    assert p["price"] is None and p["in_stock"] is False and p["browse_nodes"] == []


# -- shared daily budget ------------------------------------------------------

def _ok(req, timeout=None):
    return _Resp({"itemsResult": {"items": []}})


def test_every_answered_call_is_charged_to_the_consumer_ledger(storage):
    c = _client(storage, consumer="Price Refresh")  # normalized to kebab-case
    assert c.consumer == "price-refresh"
    with mock.patch.object(ac.urllib.request, "urlopen", side_effect=_ok):
        c.get_items([f"B{i:09d}" for i in range(25)])      # 3 getItems calls
    with mock.patch.object(ac.urllib.request, "urlopen",
                           side_effect=lambda r, timeout=None: _Resp(SEARCH_RESPONSE)):
        c.search_items("gpu")                              # 1 searchItems call
    rec = storage.read_json("config/amazon-creators-usage/2026-09-24/price-refresh.json")
    assert rec["calls"] == 4 and rec["throttled"] == 0
    assert rec["by_operation"] == {"getItems": 3, "searchItems": 1}
    assert rec["consumer"] == "price-refresh" and rec["date"] == "2026-09-24"


def test_429s_count_as_throttled_not_calls_and_network_errors_not_at_all(storage):
    c = _client(storage, consumer="hydration")
    seq = [_http_error(429), _http_error(429), _Resp({"itemsResult": {"items": []}})]
    with mock.patch.object(ac.urllib.request, "urlopen", side_effect=seq), \
         mock.patch.object(ac.time, "sleep"):
        c.get_items(["B000000001"])
    with mock.patch.object(ac.urllib.request, "urlopen",
                           side_effect=urllib.error.URLError("handshake timed out")), \
         mock.patch.object(ac.time, "sleep"):
        with pytest.raises(ac.CreatorsUnavailable):
            c.get_items(["B000000002"])
    rec = storage.read_json(ac.usage_key("hydration"))
    assert rec["calls"] == 1 and rec["throttled"] == 2


def test_remaining_budget_defaults_share_and_total_cap(storage):
    total = ac.DEFAULT_BUDGET_CONFIG["total_calls_per_day"]
    share = ac.DEFAULT_BUDGET_CONFIG["shares"]["price-refresh"]
    assert ac.remaining_budget("price-refresh", storage=storage) == int(total * share)
    assert ac.remaining_budget("somebody-new", storage=storage) == int(
        total * ac.DEFAULT_BUDGET_CONFIG["default_share"])
    # Another consumer burning almost the whole day caps everyone by the total.
    storage.write_json(ac.usage_key("discovery"), {"calls": total - 7})
    assert ac.remaining_budget("price-refresh", storage=storage) == 7
    storage.write_json(ac.usage_key("discovery"), {"calls": total + 50})
    assert ac.remaining_budget("price-refresh", storage=storage) == 0  # never negative
    usage = ac.usage_today(storage)
    assert usage["total"] == total + 50 and set(usage["consumers"]) == {"discovery"}


def test_budget_config_override_merges_shares(storage):
    storage.write_json(ac.BUDGET_CONFIG_KEY, {
        "total_calls_per_day": 1000, "shares": {"discovery": 0.5}, "default_share": 0.01})
    cfg = ac.load_budget_config(storage)
    assert cfg["shares"]["price-refresh"] == ac.DEFAULT_BUDGET_CONFIG["shares"]["price-refresh"]
    assert ac.remaining_budget("discovery", storage=storage) == 500
    assert ac.remaining_budget("price-refresh", storage=storage) == 450
    assert ac.remaining_budget("unknown-agent", storage=storage) == 10
    assert ac.DEFAULT_BUDGET_CONFIG["total_calls_per_day"] == 8000  # defaults not mutated


def test_client_budget_ok_tracks_own_spend(storage):
    storage.write_json(ac.BUDGET_CONFIG_KEY, {"total_calls_per_day": 100,
                                              "shares": {"discovery": 0.03}})
    c = _client(storage, consumer="discovery")
    assert c.remaining_budget() == 3 and c.budget_ok(calls=3)
    with mock.patch.object(ac.urllib.request, "urlopen", side_effect=_ok):
        c.get_items(["B000000001"])
        c.get_items(["B000000002"])
    assert c.remaining_budget() == 1
    assert c.budget_ok() and not c.budget_ok(calls=2)
    assert c.remaining_budget("price-refresh") == 45  # another consumer's line


def test_other_days_do_not_count(storage, monkeypatch):
    c = _client(storage, consumer="discovery")
    with mock.patch.object(ac.urllib.request, "urlopen", side_effect=_ok):
        c.get_items(["B000000001"])
    monkeypatch.setattr(ac, "_utc_day", lambda: "2026-09-25")
    assert ac.usage_today(storage)["total"] == 0
    assert storage.read_json("config/amazon-creators-usage/2026-09-24/discovery.json")["calls"] == 1


def test_ledger_failure_never_fails_the_call_and_charges_later(storage):
    c = _client(storage, consumer="discovery")
    real_write = storage.write_json
    with mock.patch.object(storage, "write_json", side_effect=OSError("disk full")), \
         mock.patch.object(ac.urllib.request, "urlopen", side_effect=_ok) as uo:
        items, errors = c.get_items(["B000000001"])
    assert (items, errors) == ({}, {})
    assert uo.call_count == 1          # no retry: a ledger error isn't a network error
    assert c._usage_pending["calls"] == 1
    assert c.remaining_budget() == 2000 - 1  # pending spend still counts locally
    with mock.patch.object(storage, "write_json", side_effect=real_write), \
         mock.patch.object(ac.urllib.request, "urlopen", side_effect=_ok):
        c.get_items(["B000000002"])
    assert storage.read_json(ac.usage_key("discovery"))["calls"] == 2
    assert c._usage_pending["calls"] == 0


def test_unbuildable_storage_disables_ledger_but_not_the_api(monkeypatch):
    from framework.core import storage as storage_mod

    def boom(*a, **k):
        raise SystemExit("unknown STORAGE_BACKEND='nope'")

    monkeypatch.setattr(storage_mod, "get_storage", boom)
    c = _client(None, consumer="discovery")
    with mock.patch.object(ac.urllib.request, "urlopen", side_effect=_ok):
        assert c.get_items(["B000000001"]) == ({}, {})
    # Fails open: no readable ledger → full default share.
    assert c.remaining_budget() == int(8000 * 0.25) - c._usage_pending["calls"]


def test_track_usage_off_writes_nothing(storage, monkeypatch):
    c = _client(storage, consumer="discovery", track_usage=False)
    with mock.patch.object(ac.urllib.request, "urlopen", side_effect=_ok):
        c.get_items(["B000000001"])
    assert storage.list_prefix(ac.USAGE_PREFIX) == []
    monkeypatch.setenv("AMAZON_CREATORS_TRACK_USAGE", "0")
    assert _client(storage).track_usage is False


def test_consumer_from_env_and_client_from_env(storage, monkeypatch):
    monkeypatch.setenv("AMAZON_CREATORS_CONSUMER", "shelf-audit")
    assert _client(storage, consumer="").consumer == "shelf-audit"
    monkeypatch.delenv("AMAZON_CREATORS_CONSUMER")
    assert _client(storage, consumer="").consumer == ac.UNATTRIBUTED
    monkeypatch.setenv("AMAZON_CREATORS_CLIENT_ID", "id")
    monkeypatch.setenv("AMAZON_CREATORS_CLIENT_SECRET", "sec")
    monkeypatch.setenv("AMAZON_CREATORS_PARTNER_TAG", "t-20")
    c = ac.client_from_env(consumer="hydration", storage=storage, partner_tag="other-20")
    assert c.consumer == "hydration" and c.cfg.partner_tag == "other-20"
    assert c._usage_storage is storage
