"""price_strip_guard: cases taken from the 2026-09-26 local-model bake-off."""
from framework.core.price_strip_guard import (
    check_rewrite, has_price_or_rating, is_price_or_review_question, required_numbers,
)

SRC = ("For anyone buying a Raspberry Pi 5 8GB in 2026, the CanaKit Starter Kit PRO is the better buy: "
       "it's the identical 2.4 GHz quad-core silicon (2153 Geekbench 6 multi-core) but bundles a 128GB "
       "card for $59.95 more — about 30% over the $200 bare-board listing.")


def test_clean_rewrite_passes():
    out = ("For anyone buying a Raspberry Pi 5 8GB in 2026, the CanaKit Starter Kit PRO is the better buy: "
           "it's the identical 2.4 GHz quad-core silicon (2153 Geekbench 6 multi-core) but bundles a 128GB "
           "card for a modest premium over the bare board.")
    r = check_rewrite(SRC, out)
    assert r.ok, r.reasons


def test_price_left_fails():
    r = check_rewrite(SRC, SRC)
    assert not r.ok and "price_left" in r.reasons


def test_dropped_benchmark_number_fails():
    out = "The CanaKit Starter Kit PRO is the better buy for a Raspberry Pi 5 8GB in 2026: same 2.4 GHz silicon, 128GB card."
    r = check_rewrite(SRC, out)
    assert not r.ok and "missing_numbers" in r.reasons and "2153" in r.missing_numbers


def test_invented_number_fails():
    out = SRC.replace("$59.95 more — about 30% over the $200 bare-board listing", "a premium, and 9 out of 10 buyers prefer it")
    r = check_rewrite(SRC, out)
    assert not r.ok and "new_numbers" in r.reasons


def test_price_derived_numbers_are_optional():
    req, _ = required_numbers(SRC)
    assert "30" not in req and "59.95" not in req and "200" not in req
    assert {"2153", "2.4", "128", "2026", "5", "8"} <= req


def test_rating_left_fails_and_rating_numbers_optional():
    src = "It holds a 4.0 out of 5 rating from 929 reviewers and draws 12W under load."
    assert required_numbers(src)[0] == {"12"}
    r = check_rewrite(src, "It is well reviewed, 4.0 out of 5, and draws 12W under load.")
    assert "rating_left" in r.reasons


def test_year_is_not_a_review_count():
    assert not has_price_or_rating("For most 2026 buyers the 12GB card is the safer pick.")


def test_scaffold_leak_fails():
    r = check_rewrite("It ships with 5 cables.", "Question: what ships?\nAnswer to rewrite: It ships with 5 cables.")
    assert "scaffold_leak" in r.reasons


def test_empty_fails():
    assert check_rewrite("x", "  ").reasons == ["empty"]


def test_price_or_review_questions():
    # pure lookups: dropped
    for q in ["How much does the HP Pavilion 600 keyboard cost?",
              "What do reviews say about the fan noise?",
              "How well reviewed is the Ultimate 2C?",
              "How well is this PSU rated by buyers?",
              "How well is this part rated by other buyers?"]:
        assert is_price_or_review_question(q), q
    # value / feature questions: rewritten, not dropped
    for q in ["Does this case work with MagSafe wireless charging?",
              "Is the TurboGrafx-16 mini worth it compared to the original console?",
              "How good is the HDR on a monitor this cheap?",
              "Is this controller any good for the price?",
              "Is the i5-12600KF worth it at this price?",
              "Is it worth it compared with a higher-end retro scaler?"]:
        assert not is_price_or_review_question(q), q
