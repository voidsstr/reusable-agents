"""ebay-product-sync: a non-dict slot in the model's hydration array must not
crash the run.

2026-09-26: the model put a bare string in one array slot, the string was
spliced into the hydration list unchanged, and _ingest_v2's hyd.get() raised
AttributeError and failed the whole run.
"""
from __future__ import annotations

import json
from pathlib import Path

_AGENT_DIR = Path(__file__).resolve().parents[2] / "agents" / "ebay-product-sync-agent"


def _load_agent_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("ebay_sync_agent", _AGENT_DIR / "agent.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_non_dict_llm_slot_becomes_none():
    mod = _load_agent_module()
    agent = mod.EbayProductSyncAgent.__new__(mod.EbayProductSyncAgent)
    agent._hydrate_from_ebay_fields = lambda item, cat_hint: None  # force the LLM pass
    agent.ai_chat = lambda *a, **kw: json.dumps([
        {"canonical_title": "RTX 3060 12GB", "confidence": 0.9},
        "skip: accessory listing",
        None,
    ])

    items = [{"title": "RTX 3060"}, {"title": "GPU bracket"}, {"title": "mystery"}]
    out = agent._hydrate_canonical_products(items, "gpus")

    assert len(out) == 3
    assert isinstance(out[0], dict) and out[0]["confidence"] == 0.9
    assert out[1] is None
    assert out[2] is None
