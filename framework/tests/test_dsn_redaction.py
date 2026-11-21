"""framework.core.dsn_redaction: connect failures as text that never carries the DSN.

The helpers are a port of agents/spec-extractor-agent/agent.py's, which its
tests calibrate against real libpq messages (LIBPQ_ECHOES): the same input
gives the same text, the label aside. Offline: no connection is opened.
"""
from __future__ import annotations

import pytest

from framework.core import dsn_redaction as dr
from framework.tests.test_spec_extractor_agent import A as SPEC_AGENT, LIBPQ_ECHOES, PW_TAIL


@pytest.mark.parametrize("dsn,cls,message", LIBPQ_ECHOES)
def test_the_port_gives_the_calibrated_texts(dsn, cls, message):
    psycopg2 = pytest.importorskip("psycopg2")
    exc = getattr(psycopg2, cls)(message)
    text = dr.connect_error_text(exc, dsn_env="SITE_DSN", dsn=dsn)
    assert text == SPEC_AGENT.connect_error_text(exc, dsn_env="SITE_DSN", dsn=dsn)
    assert PW_TAIL not in text and text.startswith(f"cannot connect to the site DB ($SITE_DSN): {cls}: ")
    assert dr.password_fragments(dsn) == SPEC_AGENT.password_fragments(dsn)
    assert dr.redact_dsn(message, dsn) == SPEC_AGENT.redact_dsn(message, dsn)


def test_the_label_and_the_variable_name_head_the_text():
    exc = RuntimeError('connection to server at "db.example" failed: timeout')
    assert dr.connect_error_text(exc, dsn_env="CATALOG_DSN", label="catalog DB") == (
        'cannot connect to the catalog DB ($CATALOG_DSN): RuntimeError: connection to server at '
        '"<redacted>" failed: timeout')
    assert dr.connect_error_text(exc, dsn_env="", label="seed DB").startswith(
        "cannot connect to the seed DB: RuntimeError: ")


def test_unquoted_echoes_lose_every_piece_of_the_password():
    dsn = f"postgresql://u:Ab3@{PW_TAIL}@h:1/db"           # an unencoded '@' in the password
    assert dr.redact_dsn(f"host {PW_TAIL}@h and Ab3@{PW_TAIL[:5]}", dsn) == "host <redacted>@h and <redacted>"
    assert dr.password_fragments("postgresql://u@h/db") == [] and dr.redact_dsn("text", "") == "text"
