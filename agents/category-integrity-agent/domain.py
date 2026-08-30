"""Decide whether a product belongs in a catalogue at all. Pure, testable.

This deliberately answers a NARROWER question than "is this the right
category". Measured on the live specpicks catalogue:

  * "which category should this be" — built from the category's own token
    profile — flagged 17,934 products, and hand-checking 12 found roughly half
    were correct. A CD-ripper filed under retro-software is arguable, and a
    checker that argues is a checker nobody trusts.

  * "is this product in our domain at all" — no domain vocabulary anywhere in
    the title, and foreign vocabulary present — flagged 2,617, and hand-checking
    found them unambiguous: chair-leg protectors, potato-sack race bags, beard
    trimmers, eyebrow pencils, beef protein powder.

The second question is the one a keyword test can answer honestly, so it is the
only one this module acts on. The first is reported for a human, never applied.
"""
from __future__ import annotations

import re

def tokens(s: str) -> set[str]:
    return set(re.sub(r"[^a-z0-9 ]", " ", (s or "").lower()).split())


def load_vocab(cfg: dict) -> tuple[set[str], set[str]]:
    """(domain, foreign) vocabularies from a site's config."""
    return (set(w.lower() for w in (cfg.get("domain_vocabulary") or [])),
            set(w.lower() for w in (cfg.get("foreign_vocabulary") or [])))


def is_off_domain(title: str, domain: set[str], foreign: set[str]) -> tuple[bool, list[str]]:
    """True when a title carries foreign vocabulary and NO domain vocabulary.

    Both halves matter. Foreign-only would flag a "gaming chair" (furniture words
    plus gaming); domain-absent-only would flag any product whose title is just a
    model number. Requiring both is what keeps this precise.
    """
    t = tokens(title)
    d = t & domain
    f = t & foreign
    if f and not d:
        return True, sorted(f)[:5]
    return False, []
