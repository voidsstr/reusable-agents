"""Outline-leak heading guard: detection + repair.

The repair cases below are verbatim headings pulled from published
specpicks `editorial_articles` rows during the 2026-08-19 sweep, so a
regression here is a regression against real production content.
"""
import pytest

from framework.core.article_metadata_guard import (
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
