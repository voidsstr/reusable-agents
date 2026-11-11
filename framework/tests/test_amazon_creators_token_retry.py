"""Amazon Creators: the LWA token request survives a network blip.

2026-11-11 11:12Z: a site's price-verifier run died on one TLS handshake
timeout to the token endpoint ("LWA token request failed: handshake operation
timed out"); hydration reached the same API at 11:15Z. ~3.8k priority product
pages then went ~6h without a price. No network here: urlopen is mocked and
time.sleep is recorded instead of slept.
"""
import io
import json
import socket
import urllib.error
from unittest import mock

import pytest

from framework.core import amazon_creators as ac


class _Resp:
    def __init__(self, body):
        self._b = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._b


TOKEN = {"access_token": "tok-1", "expires_in": 3600}


def _http_error(code, body=b"{}"):
    return urllib.error.HTTPError(ac.TOKEN_ENDPOINT, code, "err", {}, io.BytesIO(body))


def _client():
    cfg = ac.CreatorsConfig(client_id="id", client_secret="sec", partner_tag="t-20")
    return ac.AmazonCreatorsClient(cfg, min_interval_s=0, track_usage=False)


def _run(side_effect):
    sleeps = []
    with mock.patch.object(ac.urllib.request, "urlopen", side_effect=side_effect) as uo, \
         mock.patch.object(ac.time, "sleep", side_effect=sleeps.append):
        try:
            return _client()._access_token(), sleeps, uo.call_count
        except ac.CreatorsUnavailable as e:
            return e, sleeps, uo.call_count


def test_timeout_twice_then_success():
    tok, sleeps, calls = _run([TimeoutError("handshake operation timed out"),
                               TimeoutError("handshake operation timed out"),
                               _Resp(TOKEN)])
    assert tok == "tok-1"
    assert sleeps == [3.0, 10.0] and calls == 3


@pytest.mark.parametrize("exc", [
    urllib.error.URLError("handshake operation timed out"),
    socket.timeout("timed out"),
    ConnectionResetError("reset by peer"),
    OSError("network unreachable"),
])
def test_every_network_error_class_is_retried(exc):
    tok, sleeps, calls = _run([exc, _Resp(TOKEN)])
    assert tok == "tok-1" and sleeps == [3.0] and calls == 2


def test_gives_up_after_three_retries():
    err, sleeps, calls = _run([TimeoutError("t")] * 10)
    assert isinstance(err, ac.CreatorsUnavailable)
    assert sleeps == list(ac.TOKEN_RETRY_BACKOFF_S) == [3.0, 10.0, 30.0]
    assert calls == 4
    assert "after 4 attempt(s)" in str(err)


def test_http_4xx_fails_fast():
    err, sleeps, calls = _run([_http_error(400, b'{"error":"invalid_scope"}'), _Resp(TOKEN)])
    assert isinstance(err, ac.CreatorsUnavailable)
    assert "HTTP 400" in str(err) and "invalid_scope" in str(err)
    assert sleeps == [] and calls == 1
    err, sleeps, calls = _run([_http_error(401), _Resp(TOKEN)])
    assert isinstance(err, ac.CreatorsUnavailable) and sleeps == [] and calls == 1


def test_http_5xx_is_retried():
    tok, sleeps, calls = _run([_http_error(503), _Resp(TOKEN)])
    assert tok == "tok-1" and sleeps == [3.0] and calls == 2
    err, sleeps, calls = _run([_http_error(502)] * 5)
    assert isinstance(err, ac.CreatorsUnavailable) and "HTTP 502" in str(err)
    assert calls == 4


def test_missing_access_token_is_not_retried():
    err, sleeps, calls = _run([_Resp({"error": "nope"}), _Resp(TOKEN)])
    assert isinstance(err, ac.CreatorsUnavailable) and "no access_token" in str(err)
    assert sleeps == [] and calls == 1


def test_token_is_cached_after_a_retried_fetch():
    c = _client()
    with mock.patch.object(ac.urllib.request, "urlopen",
                           side_effect=[TimeoutError("t"), _Resp(TOKEN)]) as uo, \
         mock.patch.object(ac.time, "sleep"):
        assert c._access_token() == "tok-1"
        assert c._access_token() == "tok-1"
    assert uo.call_count == 2          # second call served from the cache


def test_get_items_survives_a_token_blip():
    items = {"itemsResult": {"items": [{"asin": "B000000001"}]}}
    with mock.patch.object(ac.urllib.request, "urlopen",
                           side_effect=[TimeoutError("t"), _Resp(TOKEN), _Resp(items)]), \
         mock.patch.object(ac.time, "sleep"):
        got, errors = _client().get_items(["B000000001"])
    assert list(got) == ["B000000001"] and errors == {}
