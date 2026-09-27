"""price_strip_guard: cases taken from the 2026-09-26 local-model bake-off."""
from framework.core.price_strip_guard import (
    check_rewrite, has_price, has_price_or_rating, is_price_or_review_question, required_numbers,
    strip_price_from_question,
)


def test_strip_price_from_question():
    cases = {
        "Is it worth $31.99 compared to a basic flat power strip?": "Is it worth the price compared to a basic flat power strip?",
        "How much tape do you actually get for $19.99?": "How much tape do you actually get for the price?",
        "Is the G240 worth it at around $10?": "Is the G240 worth it at its price?",
        "Is the H6 Flow RGB worth $109.97?": "Is the H6 Flow RGB worth the price?",
        "Does it support 4K at 120Hz?": "Does it support 4K at 120Hz?",
    }
    for q, want in cases.items():
        got = strip_price_from_question(q)
        assert got == want, (q, got)
        assert not has_price(got)

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
              "How well is this part rated by other buyers?",
              "How do buyers rate the GIGABYTE Radeon RX 9060 XT Gaming OC 16G?",
              "Is the 1UPcard cleaning kit well reviewed?"]:
        assert is_price_or_review_question(q), q
    # value / feature questions: rewritten, not dropped
    for q in ["Does this case work with MagSafe wireless charging?",
              "Is the TurboGrafx-16 mini worth it compared to the original console?",
              "How good is the HDR on a monitor this cheap?",
              "Is this controller any good for the price?",
              "Is the i5-12600KF worth it at this price?",
              "Is it worth it compared with a higher-end retro scaler?"]:
        assert not is_price_or_review_question(q), q


def test_per_dollar_and_price_multiples_are_optional():
    src = ("Per dollar, the 5600X wins: 12,357 PassMark points per $100 against 8,109, and 15.8 vs 8.8 tok/s per $100. "
           "It scores 2.6x in Time Spy (22,562 vs 8,682) but costs only 1.77x as much. The Quadro is only worth 2.4x the price with 24 GB.")
    req, _ = required_numbers(src)
    for n in ("12357", "8109", "15.8", "8.8", "1.77", "2.4"):
        assert n not in req, n
    for n in ("2.6", "22562", "8682", "24", "5600"):
        assert n in req, n


def test_price_clause_numbers_optional_but_benchmarks_kept():
    # comparison_commentary 9996 (2026-09-26 verdict scrub)
    src = ("For most 2026 buyers, the GeForce RTX 3060 12 GB is the better purchase: the Radeon RX 6950 XT is "
           "1.6–2.3x faster in matched public benchmarks (115 fps vs 64 fps in Cyberpunk 2077 at 1080p Ultra), "
           "but its $1,190 listing is about 2.5x the RTX 3060's $479.99.")
    good = ("For most 2026 buyers, the GeForce RTX 3060 12 GB is the better purchase: the Radeon RX 6950 XT is "
            "1.6–2.3x faster in matched public benchmarks (115 fps vs 64 fps in Cyberpunk 2077 at 1080p Ultra), "
            "but its current listing costs far more than the RTX 3060's.")
    assert check_rewrite(src, good).ok
    bad = good.replace("115 fps vs 64 fps", "a big lead")
    r = check_rewrite(src, bad)
    assert not r.ok and {"115", "64"} <= set(r.missing_numbers)
    # every figure in a per-dollar clause is optional
    req, _ = required_numbers("Per $100: Cyberpunk 2077 is 13.3 fps vs 9.7 fps, and PassMark is 3,535 vs 2,360 points.")
    assert req == {"2077"}


class _FakeJudge:
    def __init__(self, reply):
        self.reply, self.calls = reply, 0

    def chat(self, messages, **kw):
        self.calls += 1
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def test_judgeable_reasons():
    from framework.core.price_strip_guard import GuardResult, judgeable
    assert judgeable(GuardResult(False, ["missing_numbers", "rating_left"]))
    assert not judgeable(GuardResult(False, ["missing_numbers", "price_left"]))  # a "$" left is never overridden
    assert not judgeable(GuardResult(True, []))


def test_judge_verdicts_and_context_filter():
    from framework.core.price_strip_guard import judge_rewrites
    reply = ('[{"id": "a", "lost_facts": [], "invented": [], "price_or_rating_left": false},'
             ' {"id": "b", "lost_facts": ["PassMark 52,045"], "invented": [], "price_or_rating_left": false},'
             ' {"id": "c", "lost_facts": [], "invented": ["42,423"], "price_or_rating_left": false},'
             ' {"id": "d", "lost_facts": [], "invented": ["42,423"], "price_or_rating_left": false}]')
    items = [{"id": "a", "source": "s", "rewrite": "r"},
             {"id": "b", "source": "s", "rewrite": "r"},
             {"id": "c", "source": "s", "rewrite": "r 42,423"},
             {"id": "d", "source": "s", "rewrite": "r 42,423", "context": "the 9950X3D scores 42,423"},
             {"id": "e", "source": "s", "rewrite": "r"}]
    v = judge_rewrites(_FakeJudge(reply), items)
    assert v["a"].ok and not v["b"].ok and not v["c"].ok
    assert v["d"].ok  # the "invented" number is stated elsewhere in the record
    assert not v["e"].ok and v["e"].error  # no verdict -> not verified


def test_judge_failure_means_not_verified():
    from framework.core.price_strip_guard import judge_rewrites
    v = judge_rewrites(_FakeJudge(RuntimeError("pool exhausted")), [{"id": "a", "source": "s", "rewrite": "r"}])
    assert not v["a"].ok and "pool exhausted" in v["a"].error
