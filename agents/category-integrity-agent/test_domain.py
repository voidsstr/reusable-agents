"""Tests for the off-domain detector.

Each case is one the naive versions got wrong during development, so each pins
both the fix and the failure it replaced.
"""
import sys, yaml
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from domain import is_off_domain, load_vocab, tokens

CFG = yaml.safe_load(Path("/home/voidsstr/development/specpicks/agents/"
                          "category-integrity-agent/site.yaml").read_text())
DOMAIN, FOREIGN = load_vocab(CFG)


def off(t):
    return is_off_domain(t, DOMAIN, FOREIGN)[0]


def test_catches_the_products_that_prompted_this():
    for t in ["Crock-Pot 7-Quart Manual Slow Cooker",
              "Wondercide Flea, Tick & Mosquito Spray for Pets",
              "YUWELL Womens Striped Crew Socks",
              "Grass Fed Beef Protein Powder Vanilla",
              # Real catalogue title. Truncating it to "...Hardwood Floors"
              # removes the only foreign word ("furniture") and the detector
              # correctly stops flagging it - the signal is the vocabulary, not
              # the vibe, so test fixtures have to carry the real words.
              "Rectangle Chair Leg Protectors for Hardwood Floors, 16 PCS "
              "Silicone Covers to Protect Furniture"]:
        assert off(t), t


def test_leaves_real_hardware_alone():
    for t in ["ASUS TUF Gaming GeForce RTX 4070 Ti 12GB GDDR6X",
              "Nintendo 64 Controller OEM Grey",
              "SanDisk Cruzer Blade 8GB USB 2.0 Flash Drive",
              "Sony PlayStation 2 PS2 Slim Console"]:
        assert not off(t), t


def test_both_halves_are_required():
    """Foreign words alone must not condemn a product.

    A gaming chair carries furniture vocabulary AND domain vocabulary; flagging
    on foreign words alone would remove it, and flagging on absent-domain alone
    would remove anything titled only with a model number.
    """
    assert not off("GTPLAYER Gaming Chair with Foot Rest")     # has 'gaming'
    assert not off("XYZ-9000")                                 # no foreign words


def test_vocabularies_are_populated():
    # The agent refuses to run on an empty vocabulary rather than judge blind;
    # an empty list here would silently pass everything.
    assert len(DOMAIN) > 50 and len(FOREIGN) > 50


def test_tokeniser_splits_punctuation():
    assert "crock" in tokens("Crock-Pot 7-Quart")
    assert "pot" in tokens("Crock-Pot 7-Quart")
