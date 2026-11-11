"""product-hydration-agent: the demand-ranked order needs outcome_flags (A21 review).

In demand order the hydration agent re-prices clicked products at 12h gates,
before the site's price verifier (17h) ever looks at them again. If the site's
pages read Amazon-offer flags, a hydration verdict that does not keep those
flags leaves a product priced back in stock flagged unbuyable for good. So the
dispatcher refuses the demand order unless price_refresh.outcome_flags is set
or the config says the site has no such flags; the featured group is also
scoped to the site like the legacy selector."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from framework.core import price_refresh_priority as prp
from framework.core.storage import LocalFilesystemStorage

_AGENT = Path(__file__).resolve().parents[2] / "agents" / "product-hydration-agent" / "agent.py"

BLOCK = {"enabled": True, "sources": {"human_clicks": {"table": "clicks", "spec": {}}}}
FLAGS = {"owned": ["unavailable-listing", "no-buybox"],
         "by_status": {"no_offer": ["no-buybox"]}}


@pytest.fixture(scope="module")
def hyd():
    spec = importlib.util.spec_from_file_location("hydration_agent_flags_gate", _AGENT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def storage(tmp_path):
    return LocalFilesystemStorage(str(tmp_path))


def _cfg(hyd, raw, storage):
    return hyd._demand_ranked_config(raw, agent_id="x-hyd", site="x",
                                     site_id_filter=None, storage=storage)


def test_demand_without_outcome_flags_is_refused(hyd, storage):
    raw = {"price_freshness_hours": 24, "demand_priority": BLOCK}
    cfg = _cfg(hyd, raw, storage)
    assert cfg  # resolved and enabled ...
    why = hyd._demand_needs_outcome_flags(raw, cfg)
    assert why and "outcome_flags" in why  # ... but not used


def test_demand_with_outcome_flags_is_used(hyd, storage):
    raw = {"price_freshness_hours": 24, "demand_priority": BLOCK, "outcome_flags": FLAGS}
    assert hyd._demand_needs_outcome_flags(raw, _cfg(hyd, raw, storage)) is None


def test_site_without_offer_flags_can_opt_out(hyd, storage):
    raw = {"price_freshness_hours": 24,
           "demand_priority": dict(BLOCK, require_outcome_flags=False)}
    cfg = _cfg(hyd, raw, storage)
    assert cfg.get("require_outcome_flags") is False
    assert hyd._demand_needs_outcome_flags(raw, cfg) is None


def test_storage_opt_out_reaches_the_gate(hyd, storage):
    storage.write_json(prp.CONFIG_KEY, {
        "by_site": {"x": {"sources": BLOCK["sources"]}},
        "by_agent_id": {"x-hyd": {"enabled": True, "require_outcome_flags": False}}})
    raw = {"price_freshness_hours": 24}
    assert hyd._demand_needs_outcome_flags(raw, _cfg(hyd, raw, storage)) is None


def test_off_demand_is_never_blocked(hyd):
    assert hyd._demand_needs_outcome_flags({}, None) is None


def test_dispatcher_keeps_the_legacy_order_without_flags(hyd, storage, monkeypatch):
    """Storage enables demand order, the site has no outcome_flags: the
    Creators pass gets demand_cfg=None and the decision log says why."""
    storage.write_json(prp.CONFIG_KEY, {
        "by_site": {"x": {"sources": BLOCK["sources"]}},
        "by_agent_id": {"x-hyd": {"enabled": True}}})
    seen = {}

    def fake_creators(**kw):
        seen["demand_cfg"] = kw.get("demand_cfg")
        return {"provider": "creators", "refreshed": 0}

    import framework.core.amazon_creators as ac
    monkeypatch.setattr(ac, "client_from_env", lambda **kw: object())
    monkeypatch.setattr(hyd, "_refresh_via_creators", fake_creators)
    notes = []
    hyd._refresh_amazon_prices(conn=None, raw_cfg={"price_freshness_hours": 24},
                               paapi_cfg=None, bd_cfg=None, provider="creators",
                               site_id_filter=None, status_cb=lambda *a, **k: None,
                               decide_cb=lambda kind, msg: notes.append(msg),
                               agent_id="x-hyd", site="x", storage=storage)
    assert seen["demand_cfg"] is None
    assert any("outcome_flags" in n for n in notes)


def test_featured_group_is_scoped_to_the_site(hyd, storage):
    raw = {"price_freshness_hours": 24, "prioritize_featured_and_linked": True,
           "demand_priority": BLOCK}
    cfg = hyd._demand_ranked_config(raw, agent_id="x-hyd", site="x",
                                    site_id_filter="pc", storage=storage)
    featured = next(g for g in cfg["groups"] if g["name"] == "featured")
    assert "site_id = %(hyd_site_id)s" in featured["sql"]
    assert featured["params"] == {"hyd_site_id": "pc"}
    unscoped = hyd._demand_ranked_config(raw, agent_id="x-hyd", site="x",
                                         site_id_filter=None, storage=storage)
    f2 = next(g for g in unscoped["groups"] if g["name"] == "featured")
    assert "site_id" not in f2["sql"] and not f2.get("params")
