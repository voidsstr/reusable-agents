"""Outline-leak heading guard: detection + repair.

The repair cases below are verbatim headings pulled from published
specpicks `editorial_articles` rows during the 2026-08-19 sweep, so a
regression here is a regression against real production content.
"""
import pytest

from framework.core.article_metadata_guard import (
    body_lede_is_leaky,
    has_leaky_heading,
    repair_leaky_headings,
)


@pytest.mark.parametrize("heading", [
    "## Editorial intro: single-tower vs dual-tower air cooling",
    "## Editorial intro",
    "## Editorial intro (~280w): the cultural moment of BYOC LANs",
    "## Editorial intro — homelab tinkerer audience",
    "## 280w editorial intro — Ryzen 5000 thermal behavior + audience",
    "### 5-column spec-delta table",
    "## 5-Column Comparison Table",
    "## Pick | Best For | Price",
])
def test_detects_outline_label_headings(heading):
    assert has_leaky_heading(f"# Title\n\n{heading}\n\nProse here.\n")


@pytest.mark.parametrize("body", [
    "# Title\n\n## Key takeaways\n\nProse.\n",
    "# Title\n\n## What separates a single-tower from a dual-tower cooler?\n",
    # "Editorial" mid-heading is fine — only the label PREFIX leaks.
    "# Title\n\n## Our editorial intro policy explained\n",
    "",
])
def test_clean_bodies_are_not_flagged(body):
    assert has_leaky_heading(body) is None
    assert repair_leaky_headings(body) == (body, [])


@pytest.mark.parametrize("before,after", [
    ("Editorial intro: single-tower vs dual-tower air cooling",
     "Single-tower vs dual-tower air cooling"),
    ("Editorial intro — the sub-$300 market in 2026",
     "The sub-$300 market in 2026"),
    ("Editorial intro (~280w): what the startup-credit push gives you",
     "What the startup-credit push gives you"),
    ("280w editorial intro — Pi 4 8GB as the home-lab workhorse",
     "Pi 4 8GB as the home-lab workhorse"),
    ("5-column spec-delta table", "Spec-delta table"),
    ("5-column spec-delta table: cores, boost clock, TDP",
     "Spec-delta table: cores, boost clock, TDP"),
    ("5-Column Comparison Table", "Comparison Table"),
    ("5-column spec-delta at a glance", "Spec-delta at a glance"),
])
def test_repair_keeps_the_prose_remainder(before, after):
    body = f"# Title\n\n## {before}\n\nProse paragraph.\n"
    fixed, changes = repair_leaky_headings(body)
    assert changes == [(before, after)]
    assert f"## {after}" in fixed
    assert has_leaky_heading(fixed) is None


@pytest.mark.parametrize("scaffolding", [
    "Editorial intro",
    "Editorial intro (~280w)",
    "Pick | Best For | Price",
])
def test_repair_drops_headings_with_nothing_to_salvage(scaffolding):
    body = f"# Title\n\n## {scaffolding}\n\nProse paragraph.\n"
    fixed, changes = repair_leaky_headings(body)
    assert changes == [(scaffolding, "")]
    assert scaffolding not in fixed
    assert "Prose paragraph." in fixed
    assert has_leaky_heading(fixed) is None


def test_repair_preserves_heading_level_and_body():
    body = (
        "# Noctua NH-U12S vs DeepCool AK620\n\n"
        "## Editorial intro: single-tower vs dual-tower air cooling\n\n"
        "Air coolers split cleanly into two shapes.\n\n"
        "### 5-column spec-delta table\n\n"
        "| Spec | NH-U12S | AK620 |\n| --- | --- | --- |\n"
        "| Height | 158mm | 160mm |\n\n"
        "## Verdict\n\nBuy the [AK620](/product/B09NQ6BP1R).\n"
    )
    fixed, changes = repair_leaky_headings(body)
    assert len(changes) == 2
    assert "## Single-tower vs dual-tower air cooling" in fixed
    assert "### Spec-delta table" in fixed
    # Untouched surfaces: H1, table rows, links, clean headings.
    assert "# Noctua NH-U12S vs DeepCool AK620" in fixed
    assert "| Height | 158mm | 160mm |" in fixed
    assert "[AK620](/product/B09NQ6BP1R)" in fixed
    assert "## Verdict" in fixed


def test_repair_is_idempotent():
    body = "# T\n\n## Editorial intro: why it matters\n\nProse.\n"
    once, changes = repair_leaky_headings(body)
    assert changes
    twice, changes2 = repair_leaky_headings(once)
    assert changes2 == []
    assert twice == once


def test_outline_index_label_heading_is_detected_and_repaired():
    """"## Section 1: The iconic mains" is the proposal's `outline` index
    label copied verbatim. Leaked to production 2026-08-22 on the Greek
    recipe roundup (5 of 14 H2s). Strip the label, keep the prose."""
    body = (
        "# 22 Greek Recipes\n\n"
        "## Section 1: The iconic mains\n\n"
        "Moussaka is the anchor.\n\n"
        "## Section 2 - Meze, the small plates\n\n"
        "Tzatziki first.\n\n"
        "## Sources\n\nSee below.\n"
    )
    assert has_leaky_heading(body) == "Section 1: The iconic mains"
    fixed, changes = repair_leaky_headings(body)
    assert len(changes) == 2
    assert "## The iconic mains" in fixed
    assert "## Meze, the small plates" in fixed
    assert "## Sources" in fixed
    assert "Moussaka is the anchor." in fixed
    assert has_leaky_heading(fixed) is None


def test_prose_heading_starting_with_section_is_not_touched():
    """The separator after the number is required — a real heading that
    merely opens with the word "Section" must survive untouched."""
    body = (
        "# T\n\n"
        "## Section 230 of the CDA and recipe UGC\n\n"
        "Prose.\n\n"
        "## Sectioning a chicken in 4 cuts\n\nProse.\n"
    )
    assert has_leaky_heading(body) is None
    fixed, changes = repair_leaky_headings(body)
    assert changes == []
    assert fixed == body


def test_bare_outline_index_heading_is_dropped():
    """Nothing but scaffolding left after the strip -> drop the line."""
    body = "# T\n\n## Section 3:\n\nProse.\n"
    fixed, changes = repair_leaky_headings(body)
    assert changes == [("Section 3:", "")]
    assert "Section 3" not in fixed
    assert "Prose." in fixed


def test_body_lede_window_cut_midword_is_not_a_leak():
    """The `window` slice ends mid-word by construction — that is OUR cut,
    not an LLM length cap, so it must not read as prompt-metadata.

    Regression: the raw-slice check flagged 36 of the 60 most recently
    published aisleprompt articles (zero real leaks) and refuses the
    INSERT in the implementer, i.e. it silently zeroed publish volume.
    """
    lede = (
        "The school-night dinner problem is a scheduling problem, not a "
        "cooking problem: practice ends at 6:15 and you have about thirty "
        "minutes.\n\nSummer dinners are flexible. School dinners are not. "
        "Within two weeks of the first bell most families are absorbing "
        "three to five fixed evening commitments, practices and lessons"
    )
    assert body_lede_is_leaky(lede, window=200) is False


def test_body_lede_leak_pattern_still_caught():
    """A genuine prompt-metadata lede still refuses the INSERT."""
    body = (
        "Bucket-12 seasonal-occasion; trending rows (score 70, back to "
        "school) make this the canonical buying-guide slot the site "
        "lacks.\n\nReal prose would go here."
    )
    assert body_lede_is_leaky(body) is True


def test_body_lede_first_paragraph_truncated_midword_is_caught():
    """A first paragraph that itself ends mid-word is a real cap artifact."""
    body = (
        "Holiday-evergreen nostalgia demand is high-intent and the vertical "
        "rotates weekly, which anchors to the featured slot the site lacks "
        "right now so we shoul"
    )
    assert body_lede_is_leaky(body) is True
