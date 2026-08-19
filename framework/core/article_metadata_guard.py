"""Editorial-metadata leak guard for the article-author pipeline.

The article-author LLM is given the proposal as context (bucket id,
why_now rationale, trending-signal scores, internal anchor notes). When
the model is sloppy it copies that internal prompt metadata verbatim
into the published `subtitle` (and occasionally the lede of `body_md`),
exposing terms like "Bucket-12", "trending rows (score 70, …)",
"canonical buying-guide slot the site lacks", or mid-word truncations
that prove the brief was pasted in.

A 2026-06-15 progressive-improvement-agent audit flagged 4 such leaks
(rec-001..rec-004 of that run). All four had clean body_md prose; the
leak lived entirely in `subtitle`, which the SSR renders into the page
lede and uses as a meta-description fallback.

This module provides:

  - `LEAK_PATTERNS` — the regex catalog of known internal-prompt phrases
    that must never appear in editor-facing fields.
  - `scrub(text) -> (clean, was_leak)` — returns "" + True when `text`
    matches any leak pattern OR looks like a mid-word truncation; else
    returns the input unchanged.
  - `body_lede_is_leaky(body_md) -> bool` — defense-in-depth check on
    the first 600 chars of body_md.

The implementer's article-insert wrapper calls `scrub()` on
`user_meta["subtitle"]` + `user_meta["excerpt"]` and refuses to ship
the row's metadata if a leak is detected. Bodies that fail
`body_lede_is_leaky` are deferred entirely (no INSERT, re-queued).
"""
from __future__ import annotations

import re

FABRICATED_CITATION_PATTERNS: tuple[re.Pattern, ...] = tuple(
    re.compile(p, re.IGNORECASE) for p in (
        # Attributing measurements to "SpecPicks" — the site has no
        # benchmarking lab, so any "(SpecPicks YYYY data)" /
        # "according to SpecPicks 2026 Q1 data" / "according to
        # SpecPicks benchmarks" phrasing is fabricated. Flagged by
        # progressive-improvement-agent 2026-06-08 rec-002 on
        # /reviews/ah-yes-gaming-laptops-overhyped-or-underpowered
        # after a follow-up sweep found the pattern surviving beyond
        # the first 600 chars of the body (past body_lede_is_leaky).
        r"\(SpecPicks\s+\d{4}(?:\s+Q\d)?\s+data\)",
        r"according to SpecPicks\s+\d{4}(?:\s+Q\d)?\s+data\b",
        r"according to SpecPicks (?:benchmarks?|methodology|measurements?|testing|lab(?:oratory)? data)\b",
        r"per SpecPicks (?:benchmarks?|methodology|measurements?|testing|lab(?:oratory)? data)\b",
        # Unattributed "YYYY repair data" / "YYYY failure data" style
        # citations that imply an internal or proprietary dataset the
        # site does not own.
        r"according to \d{4} (?:repair|failure|reliability|warranty|RMA) data\b",
    )
)


HEADING_LEAK_PATTERNS: tuple[re.Pattern, ...] = tuple(
    re.compile(p, re.IGNORECASE) for p in (
        # Outline / brief-label prefixes that must never survive as an
        # H2/H3 heading in a shipped article. Flagged by
        # progressive-improvement-agent 2026-06-30 rec-001 after
        # "## Editorial intro: split the front-end from the GPU box…"
        # leaked to production on
        # /reviews/open-webui-raspberry-pi-4-rtx-3060-self-hosted-2026.
        r"^Editorial intro\b",
        r"^\d+w editorial\b",
        r"^\d+\s*[-\u2013\u2014]?\s*column\b",
        r"^Pick \| Best For\b",
    )
)

LEAK_PATTERNS: tuple[re.Pattern, ...] = tuple(
    re.compile(p, re.IGNORECASE) for p in (
        # Internal taxonomy ids
        r"\bBucket-\d+\b",
        # Editorial scheduling / signal vocabulary
        r"vertical rotates",
        r"news beat this week\b",
        r"makers? news brief",
        r"buyer angle\s*[—–-]",
        r"trending rows?\s*\(score\b",
        r"\(score \d+,\s*['\"]",
        r"Google-?autocomplete trending",
        r"autocomplete (?:seed |trending )",
        # Strategy/gap language that belongs in the proposal, not the article
        r"fresh angle no recent article covers",
        r"canonical buying-guide slot",
        r"slot the site lacks",
        r"anchors? to the featured",
        r"holiday-evergreen .* demand",
        # Cross-domain leak: internal infra hostname that must not be cited
        r"\bretropcfleet\.com\b",
    )
)


def _looks_truncated_midword(text: str) -> bool:
    """True when `text` ends mid-word without sentence punctuation.

    The article-author finalizer chops subtitle/excerpt to a length
    cap; if the LLM put a long rationale block in, the cap lands inside
    a word, leaving a fragment like "Holiday-evergreen nostalgia
    demand is high-inten". That truncation is a definitive sign the
    field carries prompt metadata rather than editorial prose.

    Conservative — only flags strings ≥120 chars that end in a letter
    sequence with no terminal `.`, `!`, `?`, or `)` in the last 30
    chars. Editorial subtitles always end punctuated.
    """
    s = (text or "").strip()
    if len(s) < 120:
        return False
    tail = s[-30:]
    if any(c in tail for c in ".!?)"):
        return False
    last_token = s.split()[-1] if s.split() else ""
    return bool(re.fullmatch(r"[A-Za-z][A-Za-z-]+", last_token))


def is_leaky(text: str) -> bool:
    """True if `text` contains any known leak pattern or looks truncated."""
    if not text:
        return False
    if _looks_truncated_midword(text):
        return True
    return any(p.search(text) for p in LEAK_PATTERNS)


def scrub(text: str) -> tuple[str, bool]:
    """Return ("", True) when leak detected; else (text, False)."""
    if is_leaky(text or ""):
        return "", True
    return (text or ""), False


def body_lede_is_leaky(body_md: str, *, window: int = 600) -> bool:
    """Run the same leak check against the first `window` chars of body_md.

    The article body is the primary editorial surface — a leaked phrase
    there is worse than in subtitle (SSR renders it in the article's
    main column, indexed verbatim by Googlebot). Defense in depth:
    the wrapper SHOULD reject the INSERT entirely when this returns
    True, even if subtitle/excerpt look fine.
    """
    head = (body_md or "")[:window]
    return is_leaky(head)


def body_has_fabricated_citation(body_md: str) -> str | None:
    """Return the offending snippet when body_md attributes numeric
    claims to a source the site does not own — "(SpecPicks 2026 data)",
    "according to SpecPicks benchmarks", "according to 2026 repair
    data". Scans the FULL body (not just the lede) because these
    citations tend to appear deep in the article next to the numbers
    they anchor. Returns None when the body is clean.
    """
    if not body_md:
        return None
    for pat in FABRICATED_CITATION_PATTERNS:
        m = pat.search(body_md)
        if m:
            return m.group(0)
    return None


_HEADING_RE = re.compile(r"^#{2,4}\s+(.+?)\s*$", re.MULTILINE)


def has_leaky_heading(body_md: str) -> str | None:
    """Return the offending heading text when body_md ships an H2/H3/H4
    that begins with a known outline-label prefix ("Editorial intro:",
    "500w editorial …", "2-column …", "Pick | Best For …"). These read
    as prompt scaffolding to users and Googlebot and MUST be rewritten
    as plain-prose headings before publish.

    Returns None when no leaky heading is found.
    """
    if not body_md:
        return None
    for m in _HEADING_RE.finditer(body_md):
        head_text = m.group(1).strip()
        for pat in HEADING_LEAK_PATTERNS:
            if pat.search(head_text):
                return head_text
    return None


# ── Repair (the "strips" half of the outline-leak guard) ─────────────
#
# `has_leaky_heading` only DETECTS. Detection alone means an otherwise
# good article is refused wholesale at INSERT (throughput loss), and it
# does nothing for the bodies already sitting in `editorial_articles`.
# The repair functions below turn an outline-label heading back into a
# plain-prose heading by stripping the scaffolding prefix and keeping
# the descriptive remainder:
#
#     "## Editorial intro: single-tower vs dual-tower air cooling"
#         -> "## Single-tower vs dual-tower air cooling"
#     "## 5-column spec-delta table"
#         -> "## Spec-delta table"
#     "## Editorial intro (~280w)"     (nothing but scaffolding left)
#         -> heading line dropped
#
# Callers: the implementer's article-insert wrapper (repair before the
# reject check) and `framework.cli.article_heading_repair` (sweeps rows
# the article-author LLM INSERTed itself, which never pass through the
# wrapper — see that module's docstring).

# Prefixes that are pure outline scaffolding. Stripped left-to-right,
# repeatedly, until the heading text stops shrinking.
_LABEL_STRIP_RES: tuple[re.Pattern, ...] = (
    # "Editorial intro", "280w editorial intro", "Editorial intro (~280w)",
    # each optionally followed by a ':' / '-' / en- or em-dash separator.
    re.compile(
        r"^(?:~?\d+\s*w\b\s*)?editorial\s+intro\b"
        r"(?:\s*\(\s*~?\s*\d+\s*w\s*\))?"
        r"\s*(?:[:\u2013\u2014-]\s*)?",
        re.IGNORECASE,
    ),
    # "5-column ", "5 column ", "12-column " — a table-shape note that
    # belongs in the brief, not in a reader-facing heading.
    re.compile(r"^\d+\s*[-\u2013\u2014]?\s*column\s+", re.IGNORECASE),
)

# Headings that are nothing but scaffolding — no prose to salvage, so
# the whole line is dropped.
_DROP_HEADING_RES: tuple[re.Pattern, ...] = (
    re.compile(r"^Pick\s*\|\s*Best\s+For\b", re.IGNORECASE),
)

_MD_HEADING_LINE_RE = re.compile(r"^(#{2,4})[ \t]+(.+?)[ \t]*$")


def _repair_heading_text(text: str) -> str:
    """Return the prose remainder of a leaky heading; "" means drop it."""
    s = (text or "").strip()
    for pat in _DROP_HEADING_RES:
        if pat.search(s):
            return ""
    shrinking = True
    while shrinking:
        shrinking = False
        for pat in _LABEL_STRIP_RES:
            stripped = pat.sub("", s, count=1)
            if stripped != s:
                s, shrinking = stripped.strip(), True
    s = s.strip(" \t:\u2013\u2014-")
    if not s:
        return ""
    return s[0].upper() + s[1:]


def repair_leaky_headings(body_md: str) -> tuple[str, list[tuple[str, str]]]:
    """Rewrite outline-label H2/H3/H4 headings into plain-prose headings.

    Returns `(repaired_body, changes)` where `changes` is a list of
    `(before, after)` heading-text pairs; `after == ""` means the whole
    heading line was dropped because nothing but scaffolding remained.
    A body with no leaky headings is returned unchanged with `[]`.

    The repair is deterministic and text-only — it never touches body
    prose, tables, links or heading levels, so it is safe to run over
    already-published rows.
    """
    if not body_md:
        return (body_md or ""), []
    changes: list[tuple[str, str]] = []
    out_lines: list[str] = []
    for line in body_md.split("\n"):
        m = _MD_HEADING_LINE_RE.match(line)
        if not m:
            out_lines.append(line)
            continue
        hashes, text = m.group(1), m.group(2).strip()
        if not any(p.search(text) for p in HEADING_LEAK_PATTERNS):
            out_lines.append(line)
            continue
        new_text = _repair_heading_text(text)
        # Belt-and-braces: if the salvaged remainder still trips the
        # detector, there was no prose worth keeping — drop the line.
        if new_text and any(p.search(new_text) for p in HEADING_LEAK_PATTERNS):
            new_text = ""
        changes.append((text, new_text))
        if new_text:
            out_lines.append(f"{hashes} {new_text}")
    if not changes:
        return body_md, []
    out = "\n".join(out_lines)
    # Dropping a heading leaves a blank-line pileup behind it.
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out, changes
