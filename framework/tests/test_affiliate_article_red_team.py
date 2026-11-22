"""Red team for the affiliate article engine (launch plan 2026-11-21 §4.2
Safety): 50 hand-written unsafe guide drafts through the engine's in-process
checks; 50 of 50 must be rejected, and 10 clean controls must pass.

Fixture: agents/affiliate-article-agent/fixtures/red-team-guides.json. Each
item is a complete draft in the engine's output shape for one calendar entry
of the reference site, with one subtle violation of that site's rules
(medical loads by euphemism, furnace/boiler runtime, 240 V, generators and
backfeed, transfer switches, fuel heat and light, e-bike batteries, price and
deal wording, Amazon URLs and image hosts, runtimes that contradict the facts
pack, dead or non-indexable links, guarantees, unresolvable placeholders,
holiday-tag misuse). The clean bases use only figures from the site's fixture
database.

Each draft goes through the engine's real dry-run path (`agent.dry_run` with
the canned model replaying the draft): parse_draft, merge_tags with the
code's required tags, the buystrip filter, every in-process check (shape,
content_policy, claims guard, honesty guard, live-link classes,
placeholders), one re-prompt, then held. Nothing is written: the DB session
is read-only and storage is a refusing wrapper.

Runs against the reference site's REAL article-engine site.yaml (its claims
block, link classes, placeholder kinds and SQL) and a throwaway postgres:16
loaded with the site's kit and site migrations plus db/fixtures (shared
helper from test_affiliate_article_agent; skipped without docker, the local
image or the site checkout). No LLM, no network, no production database.

A red-team body that passes is a finding for the engine's checks, not for
this test: never edit the fixture or loosen this assertion to make it pass.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from framework.core import article_output_gate as og

# The engine under test, loaded once by the main engine test module (same
# sys.modules entries, same site-database helper).
from framework.tests import test_affiliate_article_agent as base

A, E, P = base.A, base.E, base.P

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "agents" / "affiliate-article-agent" / "fixtures" / "red-team-guides.json"
SITE_DIR = base.SITE_DIR
SITE_YAML = Path(os.environ.get("AFFILIATE_ARTICLE_TEST_SITE_YAML", str(SITE_DIR / "agents" / "article-engine" / "site.yaml")))
AGENT_ID = "red-team-article-engine"
NOW = dt.datetime(2026, 12, 5, 10, 0, tzinfo=dt.timezone.utc)   # inside winter-storm-season, after the blackout
OWN_HOST_TOKEN = "own-host.invalid"

DOC = json.loads(FIXTURE.read_text())
RED = DOC["red_team"]
CONTROLS = DOC["controls"]
REGRESSIONS = json.loads((FIXTURE.parent / "red-team-regressions.json").read_text())
CATEGORY_RULES = REGRESSIONS["category_rules"]
VARIANTS = REGRESSIONS["variants"]
CATEGORIES = {"medical", "furnace", "240v", "wiring", "backfeed", "generator", "transfer-switch", "fuel", "ebike",
              "price", "amazon", "facts", "links", "guarantee", "placeholder", "holiday"}


@pytest.fixture(autouse=True)
def _offline(monkeypatch, tmp_path):
    import urllib.request
    monkeypatch.setenv("AUTHORING_LEDGER_DIR", str(tmp_path / "authoring-ledger"))   # never the real ledger

    def no_network(*_a, **_k):
        raise AssertionError("network access attempted in an offline test")

    monkeypatch.setattr(urllib.request, "urlopen", no_network)

    def no_pool(*_a, **_k):
        raise AssertionError("the real claude-pool state was read")
    from framework.cli import claude_pool
    monkeypatch.setattr(claude_pool, "ready_profiles", no_pool)
    monkeypatch.delenv("CLAUDE_POOL_RESERVE", raising=False)
    yield


@pytest.fixture(scope="module")
def site_yaml(tmp_path_factory) -> Path:
    """The site's real config with every operator flag on. Flags (e.g. the
    commerce switch that holds the under-$N guides until L0b) only decide
    which calendar entry is READY; no flag changes a check, so turning them on
    lets every entry's draft reach the checks."""
    import yaml
    if not SITE_YAML.exists():
        pytest.skip(f"site config not found: {SITE_YAML}")
    doc = yaml.safe_load(SITE_YAML.read_text())
    flags = doc["article_engine"].get("flags") or {}
    doc["article_engine"]["flags"] = {k: True for k in flags}
    out = tmp_path_factory.mktemp("red-team-site") / "site.yaml"
    out.write_text(yaml.safe_dump(doc, sort_keys=False))
    return out


@pytest.fixture(scope="module")
def site_db():
    return base.site_database()


def run_draft(item: dict, dsn: str, out: Path, site_yaml: Path) -> dict:
    """One draft through the engine's dry-run path; returns the result plus
    the checks report and the final draft the engine held or passed."""
    s = base.S.load_settings(str(site_yaml), agent_id=AGENT_ID, environ={})
    out.mkdir(parents=True, exist_ok=True)
    reply = out / "reply.json"
    # the fixture names no site: own-host.invalid stands for the site's own host
    own = next((h for h in s.own_hosts if "." in h), "")
    assert own, "the site config has no own host"
    reply.write_text(json.dumps(item["draft"]).replace(OWN_HOST_TOKEN, own))
    args = SimpleNamespace(agent_id=AGENT_ID, site_yaml=str(site_yaml), no_storage=True, fake_response=str(reply),
                           adversarial="", out=str(out / "artifacts"), live_model=False, entry=item["entry"])
    gate = og.GateDecision(True, 1, "red team: open", "cap+traffic", cap=1)
    res = A.dry_run(args, environ={s.dsn_env: dsn}, storage=A.ReadOnlyStorage(None),
                    deps_overrides={"now": lambda: NOW, "gate_fn": lambda **kw: gate})
    art = Path(res["artifacts"]) if res.get("artifacts") else None
    checks = json.loads((art / "checks.json").read_text()) if art and (art / "checks.json").exists() else {}
    draft = json.loads((art / "draft.json").read_text()) if art and (art / "draft.json").exists() else {}
    return {**res, "checks": checks, "final_draft": draft}


def _rules(r: dict) -> set[str]:
    """Every rule that rejected the draft: claims-guard rule ids plus the
    names of the other failed checks."""
    out = set()
    for c in r["checks"].get("checks") or []:
        if c.get("passes"):
            continue
        if c["name"] == "claims":
            out |= {v["rule"] for v in (c.get("details") or {}).get("violations") or []}
        else:
            out.add(c["name"])
    return out


def _variant(v: dict) -> dict:
    """A regression variant: its control's draft with one unsafe paragraph."""
    import copy
    base_item = next(c for c in CONTROLS if c["id"] == v["control"])
    assert base_item["entry"] == v["entry"], v["id"]
    draft = copy.deepcopy(base_item["draft"])
    draft["body_md"] = draft["body_md"].rstrip() + "\n\n" + v["append_body"]
    return {"id": v["id"], "entry": v["entry"], "draft": draft}


def _why(r: dict) -> str:
    return "; ".join(f"{c['name']}: {c.get('reason', '')[:160]}" for c in (r["checks"].get("checks") or [])
                     if not c.get("passes"))


# ---------------------------------------------------------------------------
# The fixture itself (no database)
# ---------------------------------------------------------------------------

def test_fixture_has_fifty_unsafe_drafts_and_ten_controls():
    assert len(RED) == 50 and len({r["id"] for r in RED}) == 50
    assert len(CONTROLS) == 10 and len({c["id"] for c in CONTROLS}) == 10
    assert {r["category"] for r in RED} == CATEGORIES
    bodies = [r["draft"]["body_md"] + r["draft"]["excerpt"] + json.dumps(r["draft"]["faqs"]) + r["draft"]["subtitle"]
              + json.dumps(r["draft"]["tags"]) for r in RED]
    assert len(set(bodies)) == 50, "two red-team drafts are identical"


def test_every_fixture_draft_is_in_the_engines_output_shape():
    import yaml
    cal_path = (yaml.safe_load(SITE_YAML.read_text()) or {})["article_engine"]["calendar"]["path"] \
        if SITE_YAML.exists() else ""
    slugs = {}
    if cal_path and Path(cal_path).exists():
        from framework.core import editorial_calendar as ecal
        slugs = {e.id: e.slug for e in ecal.load_calendar(cal_path).entries}
    for item in RED + CONTROLS:
        d = P.parse_draft(json.dumps(item["draft"]))
        assert set(d) >= {"title", "slug", "subtitle", "excerpt", "body_md", "faqs", "tags", "buystrip_asins",
                          "expected_links"}, item["id"]
        if slugs:
            assert d["slug"] == slugs[item["entry"]], item["id"]


# ---------------------------------------------------------------------------
# Through the engine (real site config + fixture database)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("item", CONTROLS, ids=[c["id"] for c in CONTROLS])
def test_clean_control_passes_every_check(item, site_db, site_yaml, tmp_path):
    r = run_draft(item, site_db, tmp_path, site_yaml)
    assert r["outcome"] == "dry-run-pass", f"{item['id']}: {r['summary']} | {_why(r)}"
    assert r["checks"].get("passes") is True


@pytest.mark.parametrize("item", RED, ids=[r["id"] for r in RED])
def test_red_team_draft_is_rejected(item, site_db, site_yaml, tmp_path):
    r = run_draft(item, site_db, tmp_path, site_yaml)
    assert r["outcome"] == "held", (
        f"UNSAFE DRAFT PASSED [{item['category']}] {item['id']}: {item['violation']} "
        f"(outcome {r['outcome']}: {r['summary']})")
    assert r["checks"].get("passes") is False and r["metrics"]["model_calls"] == 2


@pytest.mark.parametrize("item", [r for r in RED if r["category"] == "holiday"],
                         ids=[r["id"] for r in RED if r["category"] == "holiday"])
def test_forged_holiday_and_lane_tags_never_survive(item, site_db, site_yaml, tmp_path):
    """Whatever the checks decide about the prose, the engine replaces the
    model's holiday:/lane: tags with the ones the code planned."""
    r = run_draft(item, site_db, tmp_path, site_yaml)
    tags = r["final_draft"].get("tags") or []
    forged = [t for t in item["draft"]["tags"] if t.startswith(("holiday:", "lane:"))]
    assert forged and tags, item["id"]
    assert not set(forged) & set(tags), tags
    assert all(t in ("holiday:winter-storm-season",) for t in tags if t.startswith("holiday:")), tags


# ---------------------------------------------------------------------------
# Regressions: every red-team draft is rejected for ITS OWN violation (not an
# incidental one), and the variants of the drafts the first pass let through
# ---------------------------------------------------------------------------

def test_regression_fixture_shape():
    assert set(CATEGORY_RULES) >= CATEGORIES
    assert len(VARIANTS) >= 30 and len({v["id"] for v in VARIANTS}) == len(VARIANTS)
    assert not {v["id"] for v in VARIANTS} & {r["id"] for r in RED}
    for v in VARIANTS:
        assert v["expect"] and v["append_body"].strip(), v["id"]


@pytest.mark.parametrize("item", RED, ids=[r["id"] for r in RED])
def test_red_team_draft_is_rejected_for_its_own_violation(item, site_db, site_yaml, tmp_path):
    r = run_draft(item, site_db, tmp_path, site_yaml)
    own = set(CATEGORY_RULES[item["category"]])
    assert r["outcome"] == "held" and _rules(r) & own, (
        f"{item['id']} [{item['category']}]: rejected only by {sorted(_rules(r))}, never by {sorted(own)}")


@pytest.mark.parametrize("v", VARIANTS, ids=[v["id"] for v in VARIANTS])
def test_regression_variant_is_rejected_by_its_rule(v, site_db, site_yaml, tmp_path):
    r = run_draft(_variant(v), site_db, tmp_path, site_yaml)
    assert r["outcome"] == "held", f"UNSAFE VARIANT PASSED [{v['category']}] {v['id']}: {v['append_body']}"
    assert _rules(r) & set(v["expect"]), f"{v['id']}: rejected only by {sorted(_rules(r))}, expected {v['expect']}"
