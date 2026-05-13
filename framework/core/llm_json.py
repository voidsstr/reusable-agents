"""Robust JSON extraction from LLM text output.

LLMs frequently return JSON wrapped in prose, fenced code blocks, or
followed by trailing commentary / footnote-style references like `[1]`,
`[2]` that contain literal `]` characters. The naive
`s[s.find("[") : s.rfind("]") + 1]` slice breaks on these — `rfind`
lands on the bracket inside `[1]` and the resulting substring is
"valid JSON + trailing prose + ]", which `json.loads` rejects with
"Extra data: line N column 1".

This module uses `json.JSONDecoder.raw_decode()`, which parses one
complete JSON value starting at a given offset and returns the index
where it stopped — so we can ignore everything after the closing
bracket and the trailing prose problem disappears.

Use:
    from framework.core.llm_json import extract_json_object, extract_json_array

    payload = extract_json_object(claude_response)        # → dict
    rows    = extract_json_array(claude_response)         # → list

Both raise ValueError on a hard parse failure (no JSON in the text,
malformed) so callers can `try/except ValueError` and treat the
batch as failed instead of crashing the whole run.
"""
from __future__ import annotations

import json
from typing import Any


_FENCE_OPEN = "```"


def _strip_fences(s: str) -> str:
    """Remove a single ```/```json fenced block wrapping the content.
    Leaves prose-with-incidental-fences alone."""
    s = s.strip()
    if not s.startswith(_FENCE_OPEN):
        return s
    # Drop the opening fence (and optional language tag on same line)
    nl = s.find("\n")
    if nl == -1:
        return s[len(_FENCE_OPEN):]
    s = s[nl + 1:]
    # Drop the trailing fence if present (last occurrence).
    end = s.rfind(_FENCE_OPEN)
    if end != -1:
        s = s[:end]
    return s.strip()


def _extract(s: str, opener: str) -> Any:
    """Find the first `opener` ('{' or '[') and parse one JSON value
    starting there using raw_decode. Tolerates trailing prose (footnote
    refs, model commentary) AFTER the JSON value because raw_decode
    returns at the closing bracket of the first complete value."""
    if not s:
        raise ValueError("empty input")
    s = _strip_fences(s)

    decoder = json.JSONDecoder()
    # Try every `opener` position from earliest to latest. The first
    # one that parses cleanly wins. Earlier-than-the-real-start
    # candidates (e.g. a `[` inside a prose intro line) usually fail
    # raw_decode because the next char isn't valid JSON; we just move
    # on. This is O(n*k) where k = number of opener candidates, which
    # is tiny in practice.
    start = 0
    while True:
        i = s.find(opener, start)
        if i == -1:
            raise ValueError(f"no {opener!r} in input: {s[:200]!r}…")
        try:
            value, _end = decoder.raw_decode(s, i)
            return value
        except json.JSONDecodeError:
            start = i + 1
            continue


def extract_json_object(text: str) -> dict:
    """Return the first JSON object in `text`. Raises ValueError if
    none can be parsed."""
    value = _extract(text, "{")
    if not isinstance(value, dict):
        raise ValueError(f"expected object, got {type(value).__name__}")
    return value


def extract_json_array(text: str) -> list:
    """Return the first JSON array in `text`. Raises ValueError if
    none can be parsed."""
    value = _extract(text, "[")
    if not isinstance(value, list):
        raise ValueError(f"expected array, got {type(value).__name__}")
    return value
