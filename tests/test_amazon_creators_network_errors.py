"""Regression: a network-level failure on the Creators catalog call must
surface as CreatorsUnavailable (callers skip the refresh and continue),
not escape as a raw URLError that fails the whole agent run.

2026-09-18: aisleprompt-kitchen-scraper died with
`URLError: <urlopen error _ssl.c:1063: The handshake operation timed out>`
after 27 minutes because `_post` only handled HTTPError.
"""
import socket
import urllib.error
from unittest import mock

import pytest

from framework.core import amazon_creators as ac


def _client():
    cfg = ac.CreatorsConfig(client_id="id", client_secret="sec", partner_tag="t-20")
    c = ac.AmazonCreatorsClient(cfg, min_interval_s=0, track_usage=False)
    c._token = ac._Token("tok", 9e12)  # skip the LWA round-trip
    return c


@pytest.mark.parametrize("exc", [
    urllib.error.URLError("_ssl.c:1063: The handshake operation timed out"),
    socket.timeout("timed out"),
    ConnectionResetError(104, "Connection reset by peer"),
])
def test_network_error_becomes_unavailable_after_one_retry(exc):
    c = _client()
    with mock.patch.object(ac.urllib.request, "urlopen", side_effect=exc) as uo, \
         mock.patch.object(ac.time, "sleep"):
        with pytest.raises(ac.CreatorsUnavailable) as ei:
            c._post("getItems", {"itemIds": ["B000000000"]})
    assert "unreachable" in str(ei.value)
    assert uo.call_count == 2  # one retry, then give up for this run


def test_transient_network_error_recovers_on_retry():
    c = _client()

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"itemsResult": {"items": []}}'

    with mock.patch.object(ac.urllib.request, "urlopen",
                           side_effect=[urllib.error.URLError("handshake timed out"), _Resp()]), \
         mock.patch.object(ac.time, "sleep"):
        out = c._post("getItems", {"itemIds": ["B000000000"]})
    assert out == {"itemsResult": {"items": []}}


def test_http_errors_still_take_their_own_paths():
    """HTTPError is a URLError subclass; the new handler must not swallow it."""
    c = _client()
    err = urllib.error.HTTPError("u", 403, "Forbidden", {}, mock.Mock(read=lambda: b"nope"))
    with mock.patch.object(ac.urllib.request, "urlopen", side_effect=err):
        with pytest.raises(ac.CreatorsUnavailable) as ei:
            c._post("getItems", {})
    assert "rejected credentials" in str(ei.value)
