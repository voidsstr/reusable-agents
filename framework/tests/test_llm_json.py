"""framework.core.llm_json: extraction + stray-quote repair."""
import pytest

from framework.core.llm_json import (
    extract_json_array, extract_json_object, repair_unescaped_quotes,
)


def test_trailing_prose_and_fences():
    assert extract_json_object('```json\n{"a": [1]}\n```\nsee [1]') == {"a": [1]}
    assert extract_json_array('Here: [1, 2] and [3]') == [1, 2]


def test_unescaped_quote_inside_string_is_repaired():
    # The 2026-10-23 aisleprompt proposal shape: a list item that opens with
    # an unescaped quoted phrase.
    raw = ('{"proposals":[{"title":"Moccamaster","angles":["price tier",'
           '""The brewer is the same": the shared element"],"bucket":"b"}]}')
    got = extract_json_object(raw)
    assert got["proposals"][0]["angles"][1] == '"The brewer is the same": the shared element'
    assert got["proposals"][0]["bucket"] == "b"


def test_quote_mid_string_is_repaired():
    raw = '{"q": "the so-called "best" pan", "n": 2}'
    assert extract_json_object(raw) == {"q": 'the so-called "best" pan', "n": 2}


def test_valid_json_is_untouched():
    s = '{"a": "x \\"y\\" z"}'
    assert repair_unescaped_quotes(s) == s


def test_unrepairable_still_raises():
    with pytest.raises(ValueError):
        extract_json_object('{"a": 1,, "b": }')
