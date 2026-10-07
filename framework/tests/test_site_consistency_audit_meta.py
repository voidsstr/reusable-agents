"""_meta() must read the whole attribute value. A `[^"']*` content class
stopped at the first apostrophe, so aisleprompt's "Peshawar's iconic flat
kebab..." recipe description read as "Peshawar" and the 2026-10-07 crawl
reported false short-meta-description findings."""
from __future__ import annotations

from framework.cli import site_consistency_audit as sca

LONG = ("Peshawar's iconic flat kebab reimagined with ground chicken, packed with "
        "pomegranate seeds, coriander, and caramelized onion.")


def test_apostrophe_inside_double_quoted_content():
    html = f'<head><meta name="description" content="{LONG}" /></head>'
    assert sca._meta(html, "description") == LONG


def test_double_quote_inside_single_quoted_content():
    html = """<meta name='description' content='The 12" skillet that "just works"'>"""
    assert sca._meta(html, "description") == 'The 12" skillet that "just works"'


def test_property_fallback_and_missing():
    html = '<meta property="og:description" content="Ghana\'s beloved nkatenkwan">'
    assert sca._meta(html, "og:description") == "Ghana's beloved nkatenkwan"
    assert sca._meta("<head></head>", "description") == ""
