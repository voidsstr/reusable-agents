"""Tests for framework.core.demand_signal prompt rendering (no storage)."""
from framework.core import demand_signal as ds

AI = {
    "referral_days": 90, "fetch_days": 30,
    "clusters": [
        {"cluster": "LLM", "articles": 800, "referrals": 150, "fetches": 20, "referrals_per_100": 18.8},
        {"cluster": "Memory", "articles": 9, "referrals": 1, "fetches": 0, "referrals_per_100": 11.1},
        {"cluster": "Monitors", "articles": 110, "referrals": 6, "fetches": 9, "referrals_per_100": 5.5},
        {"cluster": "empty", "articles": 0, "referrals": 0, "fetches": 0, "referrals_per_100": 0},
    ],
    "top_products": [{"key": "B0AAAAAAAA", "title": "Some Desk", "category": "gaming-desks",
                      "referrals": 9, "fetches": 0}],
}


def test_block_without_ai_demand_is_unchanged():
    sig = {"generated_at": "2026-09-25T08:00:00+00:00",
           "steer_topics": [{"topic": "GPUs", "template": "article", "impressions": 10}]}
    block = ds.build_prompt_block(sig)
    assert "AI ASSISTANT DEMAND" not in block
    assert "STEER" in block


def test_ai_demand_ranks_by_yield_and_demotes_small_samples():
    block = ds.build_prompt_block({"generated_at": "2026-09-25T08:00:00+00:00",
                                   "ai_assistant_demand": AI})
    assert "AI ASSISTANT DEMAND" in block
    lines = block.splitlines()
    llm = next(i for i, l in enumerate(lines) if "- LLM:" in l)
    mon = next(i for i, l in enumerate(lines) if "- Monitors:" in l)
    mem = next(i for i, l in enumerate(lines) if "- Memory:" in l)
    assert llm < mon < mem, "small-sample cluster must rank after judged ones"
    assert "too few to judge" in lines[mem]
    assert "empty" not in block
    assert "not a reason to widen the topic scope" in block
    assert "Some Desk [gaming-desks]: 9 referrals" in block


def test_ai_demand_alone_renders():
    assert ds._ai_demand_lines({}) == []
    assert ds._ai_demand_lines({"top_products": AI["top_products"]})[0].startswith(
        "AI ASSISTANT DEMAND")
