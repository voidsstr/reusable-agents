"""agents/affiliate-article-agent: the generic Opus guide engine, and its new
framework primitives (authoring_capacity, editorial_calendar, facts_pack,
guide_claims_guard).

No LLM, no claude CLI, no network, no production database, no real framework
storage. The model is a fake; storage is a temp LocalFilesystemStorage; the
database tests use the throwaway postgres:16 the spec-extractor tests start
(one per test process; skipped without docker / the local image), loaded with
the affiliate-site kit's and the site's migrations plus the site's fixture
data (AFFILIATE_ARTICLE_TEST_SITE_DIR, default the reference site checkout;
skipped when absent).

Covered:
  * blackout / not-before -> no model call; closed output gate -> no model
    call; a lane-only gate admits only lane-tagged guides;
  * pool short, CLAUDE_POOL_RESERVE, a recent production-author defer, a
    provider that cannot serve the model, a required tier that is not Opus,
    the daily Opus-minutes cap -> defer with no model call;
  * the facts pack never carries a price, an offer column, Amazon text or a
    forbidden relation (unit + on the real views for every calendar entry);
  * the claims guard catches every violation class; the in-process checks
    catch content-policy, link, placeholder, shape and honesty failures;
    50 hand-written unsafe bodies are all rejected;
  * re-prompt once then hold; the dry run writes its files and no row;
  * the happy path inserts ONE pending row and the real publish gate CLI
    promotes it through the publisher role;
  * signals / short-circuit persistence, goals bound to metrics, no site
    names in the engine or the new framework modules.
"""
from __future__ import annotations

import datetime as dt
import importlib.util as iu
import json
import os
import re
import secrets
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from framework.core import article_output_gate as og
from framework.core import authoring_capacity as cap
from framework.core import editorial_calendar as ecal
from framework.core import facts_pack as fp
from framework.core import guide_claims_guard as cg
from framework.core.storage import LocalFilesystemStorage

ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = ROOT / "agents" / "affiliate-article-agent"
EXAMPLE = AGENT_DIR / "site.example.yaml"
SITE_DIR = Path(os.environ.get("AFFILIATE_ARTICLE_TEST_SITE_DIR", "/home/voidsstr/development/runtimepicks"))
AGENT_ID = "example-site-article-engine"
DSN_ENV = "DATABASE_URL_EXAMPLE_SITE"
LIVE_NOW = dt.datetime(2026, 12, 5, 10, 0, tzinfo=dt.timezone.utc)

if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))
_spec = iu.spec_from_file_location("affiliate_article_agent_under_test", AGENT_DIR / "agent.py")
A = iu.module_from_spec(_spec)
sys.modules[_spec.name] = A
_spec.loader.exec_module(A)
E = A.engine_mod
S = A.settings_mod
C = sys.modules["article_checks"]
P = sys.modules["article_prompt"]


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


def settings(**over):
    """The example config; the fakes stand in for a verified, isolated
    claude-cli (the example ships verified: false, as a real site starts)."""
    import yaml
    block = yaml.safe_load(EXAMPLE.read_text())["article_engine"]
    block["model"]["claude_cli"] = {"verified": True}
    for k, v in over.items():
        block[k] = v
    return S.build_settings(block, agent_id=AGENT_ID, source=str(EXAMPLE))


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

ST = [("B0EXAMPLE01", "alpha-500", "Alpha 500", 512, 431.1, 500, 1000),
      ("B0EXAMPLE02", "alpha-1000", "Alpha 1000", 1024, 862.2, 1800, 3600),
      ("B0EXAMPLE03", "beta-1500", "Beta 1500", 1536, 1293.3, 1800, 3600),
      ("B0EXAMPLE04", "beta-2000", "Beta 2000", 2048, 1724.4, 2400, 4800),
      ("B0EXAMPLE05", "gamma-3000", "Gamma 3000", 3072, 2586.6, 3000, 6000),
      ("B0EXAMPLE06", "gamma-3600", "Gamma 3600", 3600, 3031.2, 3600, 7200)]


def fake_rows() -> dict:
    stations = [{"model_id": i + 1, "slug": slug, "brand": name.split()[0], "model_name": name,
                 "product_path": f"/product/{asin}", "compat_path": f"/compat/{slug}", "capacity_wh": wh,
                 "ac_cont_w": cont, "ac_surge_w": surge, "chemistry": "LiFePO4", "recall_status": "no recall match"}
                for i, (asin, slug, name, wh, _u, cont, surge) in enumerate(ST)]
    runtimes = [{"model_id": i + 1, "model_name": name, "preset_id": "refrigerator", "basis": "rated-x-0.842",
                 "usable_wh": u, "hours_est": round(u / 62, 2), "hours_low": round(u / 62 * 0.9, 2),
                 "hours_high": round(u / 62 * 1.1, 2), "surge_ok": surge >= 2600}
                for i, (_a, _s, name, _w, u, _c, surge) in enumerate(ST)]
    presets = [{"id": "refrigerator", "label": "Refrigerator (full size)", "avg_w": 62, "surge_w": 2600,
                "wh_per_day": 1488, "source_kind": "energy-star", "hub_path": "/runtime/refrigerator",
                "source_url": "https://www.energystar.gov/productfinder/example"}]
    picks = [{"list_slug": "power-station-for-refrigerator", "list_path": "/best/power-station-for-refrigerator",
              "rank": r + 1, "asin": ST[r + 2][0], "product_path": f"/product/{ST[r + 2][0]}",
              "model_name": ST[r + 2][2], "usable_wh": ST[r + 2][4]} for r in range(3)]
    return {"presets": presets, "stations": stations, "runtimes": runtimes, "picks": picks}


class FakeStore:
    def __init__(self, s, rows=None, *, counts=None, live=None, done=(), placeholders=True):
        self.s = s
        self.rows = rows if rows is not None else fake_rows()
        self.counts = counts if counts is not None else {}
        self.live = live
        self.done = list(done)
        self.placeholders = placeholders
        self.inserted: list[dict] = []
        self.updated: list[dict] = []
        self.closed = False

    def count(self, check, params):
        return self.counts.get(check, 99)

    def done_slugs(self):
        return list(self.done)

    def data_signal(self):
        return ["2026-12-01T00:00:00+00:00", 7]

    def facts(self, params):
        return fp.from_rows(self.rows, self.s.fact_queries)

    def indexable_paths(self, paths):
        paths = set(paths)
        if self.live is not None:
            return paths & set(self.live)
        statics = set(self.s.static_paths)
        return {p for p in paths if p in statics or re.match(r"^/(product|compat|runtime|best|kits)/", p)}

    def placeholder_resolves(self, kind, key):
        return bool(self.placeholders)

    def eligible_asins(self, asins):
        return {a for a in asins if a.startswith("B0EXAMPLE")}

    def insert_article(self, row):
        if any(r["slug"] == row["slug"] for r in self.inserted):
            return False
        self.inserted.append(dict(row))
        return True

    def update_article(self, slug, row):
        self.updated.append(dict(row))
        return 1

    def close(self):
        self.closed = True


class LiveFake(P.CannedModel):
    """A fake client that looks like an Opus-capable provider."""

    class provider:  # noqa: N801
        kind = "claude-cli"
        name = "fake-claude-cli"

    def __init__(self, responses=None):
        super().__init__(None)
        self.responses = list(responses or [])
        self.messages: list[list[dict]] = []

    def chat(self, messages, **kw):
        self.messages.append([dict(m) for m in messages])
        if self.responses:
            r = self.responses.pop(0)
            self.calls += 1
            return r if isinstance(r, str) else json.dumps(r)
        return super().chat(messages, **kw)


def open_gate(**kw):
    return og.GateDecision(True, 1, "test: open", "cap+traffic", cap=1, **kw)


def make_deps(s, store=None, *, gate=None, ready=2, model=None, storage=None, now=LIVE_NOW, publish=None,
              environ=None):
    store = store or FakeStore(s)
    calls = {"model_factory": 0, "gate": 0}

    def model_factory(agent_id, model_id):
        calls["model_factory"] += 1
        if model is None:
            raise AssertionError("the model was requested")
        return model

    def gate_fn(**kw):
        calls["gate"] += 1
        return gate if gate is not None else open_gate()

    if storage is None:
        import tempfile
        storage = LocalFilesystemStorage(tempfile.mkdtemp(prefix="aae-test-"))
    deps = E.Deps(settings=s, storage=storage, store_factory=lambda: store, model_factory=model_factory,
                  gate_fn=gate_fn, ready_fn=lambda m: [f"profile-{i}" for i in range(ready)],
                  publish_fn=publish or (lambda slug: {"promoted": True, "held": False, "reasons": []}),
                  now=lambda: now, environ=environ if environ is not None else {})
    return deps, store, calls


def live_run(s, deps, state=None):
    return E.GuideRun(deps, E.RunOptions(run_ts="20261205T100000Z"), agent_id=AGENT_ID, prior_state=state).run()


# ---------------------------------------------------------------------------
# Settings, calendar, manifest, goals, no site names
# ---------------------------------------------------------------------------

def test_example_settings_validate_and_reject_unsafe_values():
    s = settings()
    assert s.site == "example-site" and s.articles["insert_status"] == "pending"
    assert {q.name for q in s.fact_queries} >= {"presets", "stations", "runtimes", "picks", "compat"}
    with pytest.raises(S.SettingsError, match="insert_status"):
        settings(articles={"table": "editorial_articles", "insert_status": "published", "byline": "X"})
    with pytest.raises(S.SettingsError, match="dsn_env"):
        settings(database={"dsn_env": "postgresql://u:p@h/db"})
    with pytest.raises(S.SettingsError, match="output_gate"):
        settings(proposals={})


def test_calendar_parses_and_chooses_first_ready_entry():
    cal = ecal.load_calendar(settings().calendar_path)
    assert [e.order for e in cal.entries] == sorted(e.order for e in cal.entries)
    counts = {"runtime_stations": 3}
    ch = ecal.choose(cal, done_slugs=[], today=dt.date(2026, 12, 5), active_occasions=["winter-storm-season"],
                     count_fn=lambda c, p: counts.get(c, 99), flags={})
    assert ch.entry is not None and ch.entry.id != "fridge-24h-outage"      # fridge needs 12 stations
    assert any(x["id"] == "fridge-24h-outage" and x["reason"] == "data not ready" for x in ch.skipped)
    # a written slug is skipped; a flag requirement holds the commerce entry
    ch2 = ecal.choose(cal, done_slugs=[e.slug for e in cal.entries if e.id != "home-backup-under-1000"],
                      today=dt.date(2026, 12, 5), active_occasions=[], count_fn=lambda c, p: 99, flags={})
    assert ch2.entry is None
    ch3 = ecal.choose(cal, done_slugs=[e.slug for e in cal.entries if e.id != "home-backup-under-1000"],
                      today=dt.date(2026, 12, 5), active_occasions=[], count_fn=lambda c, p: 99,
                      flags={"commerce": True})
    assert ch3.entry.id == "home-backup-under-1000" and ch3.occasion == ""


def test_site_calendar_validates_when_present():
    p = SITE_DIR / "content" / "editorial-calendar.yaml"
    if not p.exists():
        pytest.skip("site checkout not present")
    cal = ecal.load_calendar(p)
    assert len(cal.entries) >= 12
    for e in cal.entries:
        assert e.questions, e.id
        assert e.requires, e.id


def test_manifest_goals_and_no_site_names():
    man = json.loads((AGENT_DIR / "manifest.json").read_text())
    assert man["enabled"] is False and not man.get("cron_expr")
    goals = json.loads((AGENT_DIR / "goals.json").read_text())["goals"]
    assert 3 <= len(goals) <= 7
    for g in goals:
        assert g["target_metric"] in E.METRIC_KEYS, g["id"]
    for f in [*AGENT_DIR.glob("*.py"), *AGENT_DIR.glob("*.json"), *AGENT_DIR.glob("*.yaml"),
              *AGENT_DIR.glob("*.md"), *(AGENT_DIR / "fixtures").glob("*"),
              *(ROOT / "framework" / "core" / n for n in ("authoring_capacity.py", "editorial_calendar.py",
                                                          "facts_pack.py", "guide_claims_guard.py"))]:
        text = f.read_text().lower()
        for site in ("runtimepicks", "specpicks", "aisleprompt"):
            assert site not in text, f"{f.name} names a site ({site})"


# ---------------------------------------------------------------------------
# Window, gate, pool / model guards: no model call
# ---------------------------------------------------------------------------

def test_blackout_means_no_model_call():
    s = settings()
    model = LiveFake()
    deps, store, calls = make_deps(s, model=model, now=dt.datetime(2026, 11, 28, 12, tzinfo=dt.timezone.utc))
    out = live_run(s, deps)
    assert out.outcome == "blackout" and out.metrics["blackout"] == 1.0
    assert out.metrics["model_calls"] == 0 and model.calls == 0 and calls["gate"] == 0
    assert out.persist_signals is False


def test_not_before_live_and_dry_run_with_live_model():
    s = settings()
    model = LiveFake()
    deps, _store, _calls = make_deps(s, model=model, now=dt.datetime(2026, 12, 3, 12, tzinfo=dt.timezone.utc))
    assert live_run(s, deps).outcome == "not-before"
    # a dry run with the live model may run from dry_run_not_before (12-02) ...
    out = E.GuideRun(deps, E.RunOptions(dry_run=True, live_model=True), agent_id=AGENT_ID).run()
    assert out.outcome == "dry-run-pass" and model.calls == 1
    # ... but not before it
    deps2, _s2, _c2 = make_deps(s, model=LiveFake(), now=dt.datetime(2026, 12, 1, 23, tzinfo=dt.timezone.utc))
    assert E.GuideRun(deps2, E.RunOptions(dry_run=True, live_model=True), agent_id=AGENT_ID).run().outcome \
        in ("blackout", "not-before")


def test_closed_gate_means_no_model_call():
    s = settings()
    model = LiveFake()
    closed = og.GateDecision(False, 0, "traffic gate closed: 0/4", "cap+traffic")
    deps, store, calls = make_deps(s, gate=closed, model=model)
    out = live_run(s, deps)
    assert out.outcome == "gate-closed" and out.metrics["gate_closed"] == 1.0
    assert model.calls == 0 and calls["model_factory"] == 0 and not store.inserted
    assert out.persist_signals is False


def test_lane_only_gate_admits_only_lane_tagged_guides():
    lane_gate = og.GateDecision(True, 1, "format lanes only", "format-lane", lane_ids=["launch"],
                                lane_room={"launch": 3})
    s_no_lane = settings(gate_lanes=[])
    deps, _store, _c = make_deps(s_no_lane, gate=lane_gate, model=LiveFake())
    assert live_run(s_no_lane, deps).outcome == "gate-closed"
    s = settings()                               # gate_lanes: [launch]
    model = LiveFake()
    deps, store, _c = make_deps(s, gate=lane_gate, model=model)
    out = live_run(s, deps)
    assert out.outcome == "published", out.summary
    assert "lane:launch" in store.inserted[0]["tags"]


@pytest.mark.parametrize("ready,reserve,ok", [(1, "", False), (2, "", True), (2, "2", False), (3, "2", True)])
def test_pool_readiness_and_reserve(ready, reserve, ok):
    s = settings()
    model = LiveFake()
    deps, _store, calls = make_deps(s, model=model, ready=ready,
                                    environ={"CLAUDE_POOL_RESERVE": reserve} if reserve else {})
    out = live_run(s, deps)
    if ok:
        assert out.outcome == "published", out.summary
    else:
        assert out.outcome == "deferred" and out.metrics["deferred_pool"] == 1.0
        assert model.calls == 0 and calls["model_factory"] == 0


def test_recent_production_author_defer_blocks(tmp_path):
    s = settings()
    st = LocalFilesystemStorage(str(tmp_path / "st"))
    st.write_json("framework/defer-backoff/other-site-article-proposal-agent.json", {"schema_version": "1", "recs": {
        "rec-001": {"count": 1, "last_deferred_at": (LIVE_NOW - dt.timedelta(hours=2)).isoformat(),
                    "last_reason": "required-model-unavailable: opus"}}})
    model = LiveFake()
    deps, _store, _c = make_deps(s, model=model, storage=st)
    out = live_run(s, deps)
    assert out.outcome == "deferred" and "deferred in the last 6h" in out.summary and model.calls == 0
    # an old defer, or one for another reason, does not
    st.write_json("framework/defer-backoff/other-site-article-proposal-agent.json", {"recs": {
        "a": {"last_deferred_at": (LIVE_NOW - dt.timedelta(hours=7)).isoformat(),
              "last_reason": "required-model-unavailable: opus"},
        "b": {"last_deferred_at": LIVE_NOW.isoformat(), "last_reason": "deploy gate red"}}})
    deps, _store, _c = make_deps(s, model=LiveFake(), storage=st)
    assert live_run(s, deps).outcome == "published"


def test_provider_that_cannot_serve_opus_defers():
    s = settings()

    class Ollama(LiveFake):
        class provider:  # noqa: N801
            kind = "ollama"
            name = "ollama"
    model = Ollama()
    deps, _store, _c = make_deps(s, model=model)
    out = live_run(s, deps)
    assert out.outcome == "deferred" and "cannot serve" in out.summary and model.calls == 0


def test_required_model_never_downgrades_and_minutes_cap(tmp_path):
    s = settings()
    st = LocalFilesystemStorage(str(tmp_path / "st"))
    st.write_json("config/required-models.json", {"by_dispatch_kind": {"article-author": "sonnet"}})
    model = LiveFake()
    deps, _store, _c = make_deps(s, model=model, storage=st)
    out = live_run(s, deps)
    assert out.outcome == "deferred" and model.calls == 0
    # the daily Opus minutes cap
    deps, _store, _c = make_deps(s, model=model)
    out = live_run(s, deps, state={"opus_day": "2026-12-05", "opus_minutes_day": 20.5})
    assert out.outcome == "deferred" and "minutes cap" in out.summary and model.calls == 0


def test_model_capacity_error_defers_without_fallback():
    s = settings()

    class Limited(LiveFake):
        def chat(self, messages, **kw):
            self.calls += 1
            raise RuntimeError("claude-pool refused: CLAUDE_POOL_RESERVE=1 (exit 75)")
    model = Limited()
    deps, store, _c = make_deps(s, model=model)
    out = live_run(s, deps)
    assert out.outcome == "deferred" and model.calls == 1 and not store.inserted


# ---------------------------------------------------------------------------
# Facts pack: never a price or Amazon text
# ---------------------------------------------------------------------------

class _Cur:
    def __init__(self, cols, rows):
        self.description = [(c,) for c in cols]
        self._rows = rows

    def execute(self, sql, params=None):
        pass

    def fetchmany(self, n):
        return self._rows[:n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    autocommit = True

    def __init__(self, cols, rows):
        self.cols, self.rows = cols, rows

    def cursor(self):
        return _Cur(self.cols, self.rows)

    def rollback(self):
        pass


def test_facts_pack_refuses_prices_offers_and_amazon_text():
    q = fp.FactQuery("stations", "SELECT 1")
    for cols, rows in ((["model_name", "price"], [("Alpha", 499.0)]),
                       (["model_name", "list_price"], [("Alpha", 1)]),
                       (["model_name", "offer_state"], [("Alpha", "quotable")]),
                       (["model_name", "title"], [("Alpha", "Fixture listing")]),
                       (["model_name", "currency"], [("Alpha", "USD")])):
        with pytest.raises(fp.FactsPackViolation, match="forbidden column"):
            fp.build(_Conn(cols, rows), [q], {})
    for value in ("Now $499 at the store", "https://www.amazon.com/dp/B0EXAMPLE01", "see amzn.to/abc",
                  "https://m.media-amazon.com/images/I/x.jpg"):
        with pytest.raises(fp.FactsPackViolation):
            fp.build(_Conn(["model_name", "note"], [("Alpha", value)]), [q], {})
    with pytest.raises(fp.FactsPackViolation, match="forbidden relation"):
        fp.build(_Conn(["a"], [(1,)]), [fp.FactQuery("x", "SELECT a FROM amazon_item_cache")], {},
                 forbidden_relations=["amazon_item_cache"])
    nested = {"compat": [{"assumptions": [{"text": "fits"}], "evidence": {"list_price": 3}}]}
    assert any("list_price" in v for v in fp.commerce_violations(nested))
    pack = fp.from_rows(fake_rows(), settings().fact_queries)
    assert fp.commerce_violations(pack.rows) == []
    assert 1488.0 in pack.numeric.values["Wh"] and "/compat/alpha-500" in pack.paths


# ---------------------------------------------------------------------------
# Claims guard: every violation class
# ---------------------------------------------------------------------------

def _claims(text, field="body", **kw):
    s = settings()
    pol = s.claims_policy()
    facts = fp.from_rows(fake_rows(), s.fact_queries).numeric
    scen = fp.NumericFacts()
    scen.add("h", 24)
    args = {"title": "T", "excerpt": "", "body_md": "", **kw}
    args["body_md" if field == "body" else field] = text
    return cg.check(facts=facts, policy=pol, scenario=scen, **args)


@pytest.mark.parametrize("text,rule", [
    ("The Alpha 1000 runs a refrigerator for 31.5 hours.", "number-not-in-facts"),
    ("It holds 9,999 Wh.", "number-not-in-facts"),
    ("It draws 75 W.", "number-not-in-facts"),
    ("Watch for a price drop before you buy.", "price-history"),
    ("This is the lowest it has been.", "lowest-ever"),
    ("Grab this deal while it lasts.", "deal-talk"),
    ("The price has fallen since Black Friday.", "since-date"),
    ("It costs $499 today.", "point-price"),
    ("It was $699, now $499.", "was-now"),
    ("Save 20% off the list.", "deal-talk"),
    ("It sells out every winter.", "stock-claim"),
    ("Buy it at https://www.amazon.com/dp/B0EXAMPLE01 now.", "amazon-url"),
    ("![photo](https://m.media-amazon.com/images/I/x.jpg)", "inline-image"),
    ("Read [a review](https://competitor.example/review).", "external-link"),
    ('<a href="/product/B0EXAMPLE01">Alpha</a>', "raw-html"),
    ("| Station | Hours |\n|---|---|\n| Alpha 500 | 6.95 |", "hand-written-table"),
    ("It keeps a CPAP machine going all night.", "medical"),
    ("It can run a gas furnace blower.", "furnace"),
    ("Use the L14-30 outlet for 240 V.", "240v"),
    ("Hardwire it to your breaker panel.", "wiring"),
    ("Install a transfer switch first.", "transfer-switch"),
    ("Never backfeed your house.", "backfeed"),
    ("A generator in the garage adds hours.", "generator"),
    ("Pair it with a propane heater.", "fuel"),
    ("It charges an e-bike twice.", "ebike"),
    ("Add grid-tie inverters later.", "grid-tie"),
    ("Every pick is UL certified.", "ul-mark"),
])
def test_claims_guard_catches_each_violation_class(text, rule):
    audit = _claims(text)
    assert not audit.passes and rule in audit.rules(), audit.to_dict()


@pytest.mark.parametrize("text", [
    "The Alpha 1000 runs a full-size refrigerator for 12.52 to 15.3 hours (13.91 hours expected).",
    "A refrigerator uses 1,488 Wh a day, about 1.5 kWh, and needs 2,600 W to start.",
    "Through a 24-hour outage, the Gamma 3600 (3,600 Wh rated) is the largest we cover.",
    "The winter kits at $500, $1,000 and $2,500 differ in what they keep running.",
    "The best stations under $1,000 are below; a solar generator kit adds panels.",
    "The Beta 2000 continuous output is 2,400 W; see [the Beta 2000](/product/B0EXAMPLE04).",
])
def test_claims_guard_passes_facts_and_labels(text):
    audit = _claims(text)
    assert audit.passes, audit.to_dict()


def test_claims_guard_checks_title_excerpt_and_faqs_too():
    assert "point-price" in _claims("Best station for $499", field="title").rules()
    assert "number-not-in-facts" in _claims("Runs 40 hours", field="excerpt").rules()
    audit = cg.check(title="T", body_md="", faqs=[{"question": "CPAP?", "answer": "Yes, 12 hours."}],
                     facts=fp.NumericFacts(), policy=settings().claims_policy())
    assert {"medical", "number-not-in-facts"} <= set(audit.rules())


def test_placeholder_grammar_and_position():
    kinds = ["table:runtime", "picks", "compat"]
    found, problems = cg.find_placeholders("Intro.\n\n{{picks:best-x}}\n\n## {{compat:a-b}}\n\n{{price:x}}\n- {{picks:y}}\n"
                                           "stray }} here", kinds)
    assert [p.kind for p in found] == ["picks", "compat", "", "picks"]
    assert any("heading" in p for p in problems) and any("unknown" in p for p in problems)
    assert any("stray" in p for p in problems)


# ---------------------------------------------------------------------------
# In-process checks and the red team (50 unsafe bodies, 50 rejected)
# ---------------------------------------------------------------------------

def base_draft(extra: str = "", **over) -> dict:
    body = ("The Alpha 1000 runs a full-size refrigerator for 12.52 to 15.3 hours (13.91 hours expected), "
            "assuming rated capacity times 0.842 is usable.\n\n## Stations\n\n"
            + "\n".join(f"See [the {n}](/product/{a})." for a, _s, n, *_ in ST[:5])
            + "\n\nCharging: [what charges the Alpha 500](/compat/alpha-500) and "
              "[what charges the Beta 2000](/compat/beta-2000).\n\n{{picks:power-station-for-refrigerator}}\n\n"
            + (extra + "\n\n" if extra else "") + "See [how we calculate](/how-we-calculate).")
    d = {"title": "Best power station for a refrigerator in a 24-hour outage",
         "slug": "best-power-station-refrigerator-24-hour-outage", "subtitle": "From our runtime estimates",
         "excerpt": "A full-size refrigerator uses 1,488 Wh a day; the Alpha 1000 runs it for 13.91 hours.",
         "body_md": body, "faqs": [{"question": "Where do the hours come from?",
                                    "answer": "From each maker's specifications and our usable-capacity rule."}],
         "tags": ["lane:station"], "buystrip_asins": ["B0EXAMPLE03"], "expected_links": ["/compat/alpha-500"]}
    d.update(over)
    return d


def run_checks(draft, store=None, s=None):
    from framework.core import content_policy
    s = s or settings()
    store = store or FakeStore(s)
    cal = ecal.load_calendar(s.calendar_path)
    entry = cal.by_id("fridge-24h-outage")
    pol = content_policy.resolve_policy(s.site, rule_sets=list(s.rule_sets))
    facts = fp.from_rows(fake_rows(), s.fact_queries)
    return C.run_checks(draft, entry=entry, settings=s, store=store, facts=facts, scenario=E.scenario_facts(entry),
                        content_pol=pol, claims_policy=s.claims_policy())


def test_base_draft_passes_every_check():
    rep = run_checks(base_draft())
    assert rep.passes, rep.to_dict()


def test_checks_catch_shape_links_placeholders_policy_and_honesty():
    s = settings()
    assert "shape" in run_checks(base_draft(slug="another-slug")).failed
    assert "content_policy" in run_checks(base_draft(
        "If you are handy, wire the inlet to the panel yourself.")).failed
    assert "honesty" in run_checks(base_draft("We tested five stations in our lab for three weeks.")).failed
    # a link to a page that is not live and indexable fails the draft; too few live links fail it
    dead = FakeStore(s, live={"/product/B0EXAMPLE01", "/product/B0EXAMPLE02", "/compat/alpha-500",
                              "/how-we-calculate"})
    rep = run_checks(base_draft(), store=dead)
    assert "links" in rep.failed and "not live" in rep.to_dict()["checks"][4]["reason"]
    # an unresolvable placeholder, and a required placeholder missing
    assert "placeholders" in run_checks(base_draft(), store=FakeStore(s, placeholders=False)).failed
    no_ph = base_draft()
    no_ph["body_md"] = no_ph["body_md"].replace("{{picks:power-station-for-refrigerator}}", "")
    assert "placeholders" in run_checks(no_ph).failed


RED_TEAM = [
    "A CPAP draws little, so the Alpha 1000 keeps it going for days.",
    "For an oxygen concentrator, pick the Gamma 3600.",
    "The ResMed AirSense works fine on any of these stations.",
    "BiPAP users should look at the Beta 2000.",
    "It also powers a nebulizer for a child.",
    "Medical equipment is safe on a LiFePO4 station.",
    "Keep your sleep apnea machine running through the storm.",
    "The Gamma 3600 keeps a gas furnace blower running overnight.",
    "Your boiler circulator pump runs for 20 hours.",
    "Run the HVAC fan on the Beta 2000.",
    "Plug the well pump into the L14-30 outlet.",
    "Use the 240V output for your dryer.",
    "A dryer outlet adapter lets the station feed the house.",
    "Split-phase output covers a whole panel.",
    "Use a 14-50 RV cord for more power.",
    "A NEMA 6-30 cord connects it to the welder outlet.",
    "The twist-lock outlet takes a generator cord.",
    "Hardwire the station into the sump circuit.",
    "Connect it to your breaker panel with a short cord.",
    "Add a subpanel for the essentials.",
    "An inlet box on the wall makes this easy.",
    "An interlock kit is a cheap way to power the house.",
    "A transfer switch lets the station run your circuits.",
    "Backfeed the house through a dryer outlet if you must.",
    "Never backfeed; use a double-male cord only with the main off.",
    "A suicide cord is the quick fix during an outage.",
    "A gas generator in the garage tops the station up.",
    "An inverter generator recharges it in two hours.",
    "Dual-fuel generators pair well with these stations.",
    "Keep a propane heater as a backup.",
    "Kerosene lanterns save battery for the fridge.",
    "Candles stretch the runtime further.",
    "It charges an e-bike battery twice.",
    "Add grid-tie micro-inverters next summer.",
    "Every station here is UL certified.",
    "The Alpha 1000 runs a refrigerator for 31 hours.",
    "Expect 5,000 Wh from the Beta 2000 in practice.",
    "Prices dropped since Black Friday, so buy now.",
    "This is the lowest price we have seen.",
    "Today's deal knocks 30% off the list price.",
    "It costs $899 at most stores.",
    "Was $1,299, now $899.",
    "It sells out every winter, so order early.",
    "Order it on [Amazon](https://www.amazon.com/dp/B0EXAMPLE02).",
    "Short link: amzn.to/3xYz.",
    "![box](https://m.media-amazon.com/images/I/81abc.jpg)",
    "Compare at [another site](https://other-reviews.example/power).",
    '<a href="https://www.amazon.com/dp/B0EXAMPLE02">Buy</a>',
    "If you're handy, wire the generator inlet to the panel yourself; no electrician needed.",
    "Bypass the BMS to unlock more capacity in the cold.",
]


def test_red_team_has_fifty_bodies():
    assert len(RED_TEAM) == 50 and len(set(RED_TEAM)) == 50


@pytest.mark.parametrize("unsafe", RED_TEAM)
def test_red_team_unsafe_body_is_rejected(unsafe):
    rep = run_checks(base_draft(unsafe))
    assert not rep.passes, f"unsafe body passed: {unsafe!r}"


# ---------------------------------------------------------------------------
# Draft loop: re-prompt once, then hold; happy path (fake DB); dry run files
# ---------------------------------------------------------------------------

def test_reprompt_once_then_hold_and_no_insert(tmp_path):
    s = settings()
    bad = base_draft("Grab this deal while it lasts.")
    model = LiveFake([bad, bad])
    st = LocalFilesystemStorage(str(tmp_path / "st"))
    deps, store, _c = make_deps(s, model=model, storage=st)
    out = live_run(s, deps)
    assert out.outcome == "held" and model.calls == 2 and out.metrics["model_calls"] == 2
    assert out.metrics["checks_failed"] >= 2 and not store.inserted
    second_user = model.messages[1][-1]["content"]
    assert "FAILED THESE CHECKS" in second_user and "deal-talk" in second_user
    assert st.read_json(f"agents/{AGENT_ID}/runs/20261205T100000Z/held-draft.json")["slug"] == bad["slug"]


def test_reprompt_fixes_then_publishes():
    s = settings()
    model = LiveFake([base_draft("Grab this deal while it lasts."), base_draft()])
    deps, store, _c = make_deps(s, model=model)
    out = live_run(s, deps)
    assert out.outcome == "published" and model.calls == 2 and len(store.inserted) == 1
    row = store.inserted[0]
    assert row["slug"] == "best-power-station-refrigerator-24-hour-outage"
    assert {"lane:station", "lane:launch", "holiday:winter-storm-season"} <= set(row["tags"])
    assert out.metrics["guides_published"] == 1.0 and out.metrics["opus_minutes"] >= 0


def test_publish_gate_hold_gets_one_reprompt_then_stays_held():
    s = settings()
    seen = []

    def publish(slug):
        seen.append(slug)
        return {"promoted": False, "held": True, "reasons": ["link_guard: too few links"], "errors": []}
    model = LiveFake([base_draft(), base_draft()])
    deps, store, _c = make_deps(s, model=model, publish=publish)
    out = live_run(s, deps)
    assert out.outcome == "held" and out.metrics["gate_held"] == 1.0
    assert model.calls == 2 and len(store.inserted) == 1 and len(store.updated) == 1 and len(seen) == 2
    assert "PUBLISH GATE HELD" in model.messages[1][-1]["content"]


def test_system_prompt_carries_the_rules():
    s = settings()
    sp = P.system_prompt(s, slug="x-y")
    assert "holiday:<id> tag rule" in sp and "never write a markdown table" in sp.lower()
    assert "{{table:runtime:<key>}}" in sp and "Never write a price" in sp
    assert "LINK CLASSES" in sp and "at least 5 distinct links under /product/" in sp
    for r in s.prompt["rules"]:
        assert r in sp


def test_occasion_block_never_uses_occasion_guidance():
    s = settings()
    acts = E.active_occasions(s, dt.date(2026, 12, 5))
    occ = next(a for a in acts if a.occasion.id == "winter-storm-season")
    assert occ.occasion.guidance == ""
    from framework.core import seasonal_calendar as sc
    deal = sc.Occasion(id="x", label="X", month_day="12-05", guidance="a price-history or MSRP anchor")
    block = P.occasion_block(sc.ActiveOccasion(deal, dt.date(2026, 12, 5), 0, "now"))
    assert "MSRP" not in block and "price" not in block.lower()


# ---------------------------------------------------------------------------
# AgentBase: signals and what a run persists
# ---------------------------------------------------------------------------

def _agent(tmp_path, s, store, **ov):
    st = LocalFilesystemStorage(str(tmp_path / "agentstore"))
    deps_over = {"store_factory": lambda: store, "now": lambda: LIVE_NOW,
                 "ready_fn": lambda m: ["p1", "p2"], **ov}
    ag = A.AffiliateArticleAgent(settings=s, agent_id=AGENT_ID, storage=st, deps_overrides=deps_over, environ={})
    ag.auto_recovery_enabled = False
    return ag, st


def _state(st):
    return (st.read_json(f"agents/{AGENT_ID}/state/latest.json") or {}).get("state") or {}


def test_signals_short_circuit_only_on_input_bound_outcomes(tmp_path):
    s = settings()
    # no topic ready -> depends only on inputs: the hash persists, the next run short-circuits
    store = FakeStore(s, counts={k: 0 for k in s.readiness_checks})
    ag, st = _agent(tmp_path, s, store)
    sig = ag.signals()
    assert set(sig) >= {"calendar", "data", "slugs", "occasions", "ledger_date"}
    r1 = ag.run_once()
    assert "no calendar entry is ready" in r1.summary and A.AffiliateArticleAgent.SIGNALS_STATE_KEY in _state(st)
    ag2, _ = _agent(tmp_path, s, store)
    assert ag2.run_once().short_circuited
    # a closed gate does NOT persist the hash: the next tick re-evaluates
    closed = og.GateDecision(False, 0, "closed", "cap+traffic")
    ag3, st3 = _agent(tmp_path / "b", s, FakeStore(s), gate_fn=lambda **kw: closed)
    r3 = ag3.run_once()
    assert r3.metrics["gate_closed"] == 1.0
    assert A.AffiliateArticleAgent.SIGNALS_STATE_KEY not in _state(st3)


def test_agent_live_run_records_ledger_and_metrics(tmp_path):
    s = settings()
    store = FakeStore(s)
    ag, st = _agent(tmp_path, s, store, model_factory=lambda a, m: LiveFake(),
                    gate_fn=lambda **kw: open_gate(),
                    publish_fn=lambda slug: {"promoted": True, "held": False, "reasons": []})
    r = ag.run_once()
    assert r.status == "success" and r.metrics["guides_published"] == 1.0, r.summary
    ledger = og.read_ledger(st, AGENT_ID)
    assert ledger["batches"][-1]["slugs"] == ["best-power-station-refrigerator-24-hour-outage"]
    assert ledger["batches"][-1].get("lane_ids") == ["launch"]
    assert st.read_json(f"agents/{AGENT_ID}/runs/{ag.run_ts}/engine-report.json")["outcome"] == "published"


# ---------------------------------------------------------------------------
# Real database: the site's kit + migrations + fixture data
# ---------------------------------------------------------------------------

def _site_sql_files() -> list[Path]:
    kit = sorted((SITE_DIR / "vendor" / "affiliate-kit" / "migrations").glob("kit_*.sql"))
    site = sorted((SITE_DIR / "db" / "migrations").glob("*.sql"))
    if not kit or not site or not (SITE_DIR / "db" / "fixtures" / "seed.sql").exists():
        pytest.skip(f"site migrations / fixture not found under {SITE_DIR}")
    return kit + site


def site_database() -> str:
    from framework.tests.test_spec_extractor_agent import fresh_database
    files = _site_sql_files()
    dsn = fresh_database("aae")
    import psycopg2
    for f in files:
        conn = psycopg2.connect(dsn)
        try:
            with conn.cursor() as cur:
                cur.execute(f.read_text())
            conn.commit()
        finally:
            conn.close()
    for f in (SITE_DIR / "db" / "fixtures" / "seed.sql", SITE_DIR / "db" / "fixtures" / "freshen.sql"):
        conn = psycopg2.connect(dsn)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute(f.read_text())
        finally:
            conn.close()
    return dsn


@pytest.fixture(scope="module")
def ro_db():
    return site_database()


def db_store(s, dsn, *, write=False):
    import psycopg2
    env = {DSN_ENV: dsn}
    return A.store_factory(s, readonly_only=not write, environ=env)()


def test_facts_pack_on_the_real_views_has_no_prices_or_amazon_text(ro_db):
    s = settings()
    store = db_store(s, ro_db)
    cal = ecal.load_calendar(s.calendar_path)
    try:
        for e in cal.entries:
            pack = store.facts({**s.fact_params, **e.scope})
            assert fp.commerce_violations(pack.rows) == [], e.id
            blob = json.dumps(pack.rows)
            assert not fp.MONEY_RE.search(blob) and not fp.AMAZON_RE.search(blob), e.id
            for rows in pack.rows.values():
                for r in rows:
                    assert not any(re.search(fp.DEFAULT_FORBIDDEN_COLUMN_RE, k) for k in r), (e.id, list(r))
            assert pack.ready, (e.id, pack.empty_required)
        with pytest.raises(Exception):                     # the session is read-only
            with store.ro.cursor() as cur:
                cur.execute("UPDATE models SET family = family")
    finally:
        store.close()


def test_dry_run_writes_files_and_no_rows(ro_db, tmp_path):
    from framework.tests.test_spec_extractor_agent import query
    before = query(ro_db, "SELECT count(*) AS n, max(updated_at) AS u FROM editorial_articles")[0]
    args = SimpleNamespace(agent_id=AGENT_ID, site_yaml=str(EXAMPLE), no_storage=True, fake_response="",
                           adversarial="", out=str(tmp_path / "out"), live_model=False, entry="")
    ro = A.ReadOnlyStorage(None)
    res = A.dry_run(args, environ={DSN_ENV: ro_db}, storage=ro)
    assert res["outcome"] == "dry-run-pass", res["summary"]
    d = Path(res["artifacts"])
    for name in ("draft.json", "article.md", "checks.json", "facts.json", "prompt.txt", "report.json"):
        assert (d / name).exists(), name
    assert json.loads((d / "checks.json").read_text())["passes"] is True
    assert "{{picks:" in (d / "article.md").read_text()
    after = query(ro_db, "SELECT count(*) AS n, max(updated_at) AS u FROM editorial_articles")[0]
    assert after == before and res["storage_writes_refused"] == []
    # a red-team reply through the dry run is held, still nothing written
    (tmp_path / "bad.json").write_text(json.dumps(base_draft("Never backfeed through the dryer outlet.")))
    args.fake_response = str(tmp_path / "bad.json")
    args.entry = "fridge-24h-outage"
    res2 = A.dry_run(args, environ={DSN_ENV: ro_db}, storage=A.ReadOnlyStorage(None))
    assert res2["outcome"] == "held"
    assert query(ro_db, "SELECT count(*) AS n, max(updated_at) AS u FROM editorial_articles")[0] == before


def test_happy_path_inserts_one_pending_row_and_the_publish_gate_promotes_it(tmp_path, monkeypatch):
    import psycopg2
    from framework.tests.test_spec_extractor_agent import query
    dsn = site_database()
    params = psycopg2.extensions.parse_dsn(dsn)
    pw = secrets.token_hex(12)
    admin = psycopg2.connect(dsn)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f"ALTER ROLE {_publisher_role(dsn)} LOGIN PASSWORD '{pw}'")
    admin.close()
    pub_dsn = " ".join(f"{k}={v}" for k, v in {**params, "user": _publisher_role(dsn), "password": pw}.items())
    store_root = tmp_path / "storage"
    st = LocalFilesystemStorage(str(store_root))
    # what WIN1 writes to storage for a site (here: the example site's keys)
    st.write_json("config/content-policy-config.json", {"sites": {"example-site": {
        "rule_sets": ["electrical", "electrical-year1", "medical"],
        "publish_gate": {"draft_status": "pending", "publisher_role": _publisher_role(dsn)}}}})
    st.write_json("config/article-link-guard-config.json", {"sites": {"example-site": {
        "min_recipes": 0, "min_kits": 0, "min_products": 0, "min_buy_links": 0,
        "site_root": "https://www.example-site.test",
        "link_classes": {"product": {"roots": ["/product/"], "min": 5, "own_host_only": True},
                         "compat": {"roots": ["/compat/"], "min": 2, "own_host_only": True}}}}})
    env = {DSN_ENV: dsn, f"{DSN_ENV}_PUBLISHER": pub_dsn, "STORAGE_BACKEND": "local",
           "AGENT_STORAGE_LOCAL_PATH": str(store_root), "PATH": os.environ.get("PATH", ""),
           "HOME": os.environ.get("HOME", "")}
    s = settings()
    before = {r["slug"] for r in query(dsn, "SELECT slug FROM editorial_articles")}
    model = LiveFake()                         # canned JSON from the real facts pack, Opus-capable kind
    ag = A.AffiliateArticleAgent(settings=s, agent_id=AGENT_ID, storage=st, environ=env, deps_overrides={
        "model_factory": lambda a, m: model, "ready_fn": lambda m: ["p1", "p2"], "now": lambda: LIVE_NOW,
        "publish_fn": E.default_publish_fn(s, env)})
    ag.auto_recovery_enabled = False
    r = ag.run_once()
    assert r.status == "success" and r.metrics["guides_published"] == 1.0, r.summary
    assert model.calls == 1
    rows = query(dsn, "SELECT slug, status, author, hero_image_url, published_at, tags, written_by, "
                      "related_product_asins FROM editorial_articles")
    new = [x for x in rows if x["slug"] not in before]
    assert len(new) == 1
    row = new[0]
    assert row["status"] == "published" and row["published_at"] is not None
    assert row["author"] == s.articles["byline"] and row["hero_image_url"] is None
    assert row["written_by"] == AGENT_ID
    assert {"lane:station", "lane:launch", "holiday:winter-storm-season"} <= set(row["tags"])
    elig = {x["asin"] for x in query(dsn, "SELECT asin FROM v_pick_eligible")}
    assert set(row["related_product_asins"]) <= elig
    # the guide is now a published, indexable page; a second run the same day is capped by the gate
    assert query(dsn, "SELECT indexable FROM v_indexable_pages WHERE family = 'guide' AND key = %s",
                 (row["slug"],))[0]["indexable"] is True
    ag2 = A.AffiliateArticleAgent(settings=s, agent_id=AGENT_ID, storage=st, environ=env, deps_overrides={
        "model_factory": lambda a, m: model, "ready_fn": lambda m: ["p1", "p2"], "now": lambda: LIVE_NOW})
    ag2.auto_recovery_enabled = False
    r2 = ag2.run_once()
    assert r2.metrics["gate_closed"] == 1.0 and model.calls == 1


def _publisher_role(dsn: str) -> str:
    """The publisher role the site's rendered publish guard names."""
    from framework.tests.test_spec_extractor_agent import query
    src = query(dsn, "SELECT prosrc FROM pg_proc WHERE proname = 'editorial_articles_publish_guard'")[0]["prosrc"]
    m = re.search(r"publisher\s+CONSTANT\s+name\s*:=\s*'([a-z_][a-z0-9_]*)'", src)
    assert m, "publisher role not found in the publish guard"
    return m.group(1)


# ---------------------------------------------------------------------------
# Skeptic / red-team fixes (2026-11-22)
# ---------------------------------------------------------------------------

def _claims_rows(text, rows=None, block_extra=None, **kw):
    """The claims guard on `text` with the example policy plus extra claims
    config, against fake rows (default fake_rows())."""
    s = settings()
    block = {**dict(s.claims_block), **(block_extra or {})}
    pol = cg.parse_policy(block, own_hosts=s.own_hosts)
    pack = fp.from_rows(rows or fake_rows(), s.fact_queries)
    scen = fp.NumericFacts()
    scen.add("h", 24)
    return cg.check(title="T", body_md=text, facts=pack.numeric, policy=pol, scenario=scen, rows=pack.rows, **kw)


@pytest.mark.parametrize("text", [
    # a pronoun inherits the previous sentence's model
    "The Alpha 500 is the smallest station here. It keeps a refrigerator going for 13.91 hours.",
    # an hour figure about no model is never a runtime
    "Most stations here keep a fridge going for 13.91 hours on a charge.",
    # always-allowed / scenario hours only as the scenario, never as a runtime
    "The Alpha 500 keeps a refrigerator cold for 24 hours.",
    # a model-name prefix never borrows the shorter model's figures
    "The Alpha 1000 Max runs a refrigerator for 13.91 hours.",
    # days are hours; 'full' claims at least that much
    "The Gamma 3600 keeps a refrigerator cold for three full days.",
    "The Alpha 1000 runs a refrigerator for 2 days.",
    # spelled-out figures are figures
    "The Beta 2000 gives it forty hours.",
    # the nearest model in the sentence owns the figure
    "The Alpha 500 gives 6.25 hours and the Alpha 1000 6.95 hours.",
])
def test_claims_guard_scopes_every_figure_to_its_model(text):
    audit = _claims(text)
    assert "number-not-in-facts" in audit.rules(), (text, audit.to_dict())


@pytest.mark.parametrize("text", [
    "The Alpha 1000 is a solid pick. It runs a full-size refrigerator for 13.91 hours.",
    "Through a 24-hour outage, the Alpha 1000 gives a refrigerator 13.91 hours.",
    "Plan for an outage of 24 hours: the Gamma 3600 covers a refrigerator for 48.89 hours.",
    "The Alpha 500 gives 6.95 hours and the Alpha 1000 13.91 hours.",
    "The Gamma 3600 keeps a refrigerator cold for 2 days.",
])
def test_claims_guard_scoping_passes_true_figures(text):
    audit = _claims(text)
    assert audit.passes, (text, audit.to_dict())


@pytest.mark.parametrize("text,rule", [
    ("The Alpha 1000 is about 900 bucks right now.", "money-words"),
    ("It costs 45 cents per usable watt-hour.", "money-words"),
    ("It is 30 percent below its list price this week.", "list-price"),
    ("The Beta 2000 is cheaper now than it was in October.", "price-compare"),
    ("The Beta 2000 is the best value on the list.", "price-compare"),
    ("Clip the on-page coupon before checkout.", "promo-talk"),
    ("Cyber Monday week is when to order it.", "retail-event"),
    ("Wait for the Presidents' Day sales event.", "deal-talk"),
    ("It is the time to order the Gamma 3600.", "buy-urgency"),
    ("It will keep your family safe in a storm.", "guarantee"),
    ("Your basement is guaranteed to stay dry.", "guarantee"),
    ("It carries you through the whole storm.", "guarantee"),
])
def test_claims_guard_catches_price_guarantee_and_retail_wording(text, rule):
    audit = _claims(text)
    assert rule in audit.rules(), (text, audit.to_dict())


def test_claims_guard_wording_ignores_link_targets_and_placeholders():
    audit = _claims("See [the value list](/best/cheapest-usable-kwh).\n\n{{picks:cheapest-usable-kwh}}")
    assert audit.passes, audit.to_dict()


def _pair_rows():
    rows = fake_rows()
    rows["compat"] = [
        {"station_slug": "alpha-500", "compat_path": "/compat/alpha-500", "station": "Alpha 500",
         "other": "Sun Panel 200", "other_brand": "Sun", "other_kind": "panel", "relation": "charges_from",
         "verdict": "over_limit", "confidence": "computed", "cold_voc_v": 61.375, "max_input_v": 60, "flags": []},
        {"station_slug": "beta-2000", "compat_path": "/compat/beta-2000", "station": "Beta 2000",
         "other": "Sun Panel 200", "other_brand": "Sun", "other_kind": "panel", "relation": "charges_from",
         "verdict": "fits", "confidence": "computed", "cold_voc_v": 61.375, "max_input_v": 150, "flags": []}]
    rows["certifications"] = [
        {"model_id": 1, "model_name": "Alpha 500", "standard": "UL 2743", "nrtl": "Lab A", "status": "verified",
         "verified_at": "2026-11-21"},
        {"model_id": 2, "model_name": "Alpha 1000", "standard": "UL 2743", "nrtl": "maker", "status": "not-listed",
         "verified_at": "2026-11-21"}]
    stations = rows["stations"]
    stations[0]["charge_temp_min_c"] = 0
    stations[0]["discharge_temp_min_c"] = -20
    return rows


_ROW_RULES = {
    "units": ["W", "Wh", "h", "V", "A", "C"],
    "verdicts": [{"rows": "compat", "subjects": ["station", "other"], "verdict": "verdict",
                  "positive": ["fits", "fits_in_parallel"], "flags": "flags"}],
    "statuses": [{"rows": "certifications", "subject": "model_name", "key": "standard", "status": "status",
                  "positive": ["verified"], "date": "verified_at"}],
    "contexts": [{"pattern": r"\b(?:re)?charg", "unit": "C", "columns": ["charge_temp_min_c"]}],
}


def _fact_queries_with_rows():
    s = settings()
    extra = fp.parse_queries({"queries": [
        {"name": "compat", "sql": "SELECT 1", "units": {"cold_voc_v": "V", "max_input_v": "V"},
         "names": ["station", "other"], "entities": ["station", "other"]},
        {"name": "certifications", "sql": "SELECT 1", "names": ["model_name"], "entities": ["model_name"]}]})[0]
    stations = next(q for q in s.fact_queries if q.name == "stations")
    st2 = fp.FactQuery(**{**stations.__dict__, "units": {**stations.units, "charge_temp_min_c": "C",
                                                         "discharge_temp_min_c": "C"}})
    return tuple(q for q in s.fact_queries if q.name not in ("stations", "compat", "certifications")) + (st2,) + extra


def _row_audit(text):
    s = settings()
    pol = cg.parse_policy({**dict(s.claims_block), **_ROW_RULES}, own_hosts=s.own_hosts)
    pack = fp.from_rows(_pair_rows(), _fact_queries_with_rows())
    return cg.check(title="T", body_md=text, facts=pack.numeric, policy=pol, rows=pack.rows)


@pytest.mark.parametrize("text,rule", [
    ("The Sun Panel 200 reaches 61.375 V cold, above the 60 V input of the Alpha 500, but it still fits.",
     "verdict-contradicts-facts"),
    ("The Sun Panel 200 works with the Alpha 500.", "verdict-contradicts-facts"),
    ("The Alpha 1000 carries a UL 2743 listing verified 2026-11-21.", "status-contradicts-facts"),
    ("The Alpha 500's UL 2743 listing was verified 2025-01-02.", "status-contradicts-facts"),
    ("The Alpha 500 charges from solar down to -20 C.", "number-not-in-facts"),
    ("The Alpha 500 recharges at -4 F.", "number-not-in-facts"),
])
def test_row_rules_catch_verdicts_statuses_and_charge_limits(text, rule):
    audit = _row_audit(text)
    assert rule in audit.rules(), (text, audit.to_dict())


@pytest.mark.parametrize("text", [
    "The Sun Panel 200 reaches 61.375 V cold, above the 60 V input of the Alpha 500, so we do not call it a fit.",
    "The Sun Panel 200 fits the Beta 2000.",
    "The Alpha 500's UL 2743 listing was verified 2026-11-21 (Lab A).",
    "The Alpha 1000 has no UL 2743 listing in a public directory as of 2026-11-21.",
    "The Alpha 500 charges at 0 C or warmer and runs down to -20 C.",
])
def test_row_rules_pass_what_facts_say(text):
    audit = _row_audit(text)
    assert audit.passes, (text, audit.to_dict())


class _ViolatingStore(FakeStore):
    """The first calendar entry's facts pack carries a money amount."""

    def __init__(self, s, bad_presets=("refrigerator",), **kw):
        super().__init__(s, **kw)
        self.bad = set(bad_presets)
        self.facts_calls = []

    def facts(self, params):
        self.facts_calls.append(dict(params))
        if self.bad & set(params.get("presets") or []):
            raise fp.FactsPackViolation("ladder_slots[0].slot_reason: holds a money amount")
        return super().facts(params)


def test_a_refused_facts_pack_falls_through_to_the_next_entry():
    s = settings()
    model = LiveFake()
    store = _ViolatingStore(s, bad_presets=("refrigerator",))
    deps, _st, _c = make_deps(s, store=store, model=model)
    out = live_run(s, deps)
    assert out.outcome == "published", out.summary
    assert store.inserted[0]["slug"] != "best-power-station-refrigerator-24-hour-outage"
    tried = out.report["topic"]["tried"]
    assert tried.get("fridge-24h-outage") == "facts pack refused"
    assert any("money amount" in a.get("skipped", "") for a in out.report["topic_attempts"])


def test_only_refused_entries_is_a_failure_not_a_wedge():
    s = settings()
    store = _ViolatingStore(s, bad_presets=("refrigerator", "starlink-standard", "sump-pump-half-hp",
                                            "wifi-router-modem", "led-light-bulbs"))
    store.facts = lambda params: (_ for _ in ()).throw(fp.FactsPackViolation("money"))
    deps, _st, _c = make_deps(s, store=store, model=LiveFake())
    out = live_run(s, deps)
    assert out.outcome == "facts-violation" and out.status == "failure" and out.metrics["model_calls"] == 0
    assert out.persist_signals is False


def test_too_few_live_links_falls_through_to_the_next_entry():
    s = settings()

    class Store(FakeStore):
        def indexable_paths(self, paths):
            got = super().indexable_paths(paths)
            return set() if "/runtime/refrigerator" in set(paths) else got
    store = Store(s)
    deps, _st, _c = make_deps(s, store=store, model=LiveFake())
    out = live_run(s, deps)
    assert out.report["topic"]["tried"].get("fridge-24h-outage") == "too few live links"


def test_run_dir_deferred_json_blocks_and_unreadable_storage_denies(tmp_path):
    s = settings()
    st = LocalFilesystemStorage(str(tmp_path / "st"))
    # the implementer's pool-exhausted path: deferred.json in the producer's run dir, no backoff record
    st.write_json("agents/other-site-article-proposal-agent/runs/20261204T162000Z/deferred.json", {
        "deferred_at": (LIVE_NOW - dt.timedelta(hours=1)).isoformat(), "required_model": "claude-opus-5-5",
        "exit_code": 75, "reason": "required model claude-opus-5-5 unavailable (rc=75)"})
    model = LiveFake()
    deps, _store, _c = make_deps(s, model=model, storage=st)
    out = live_run(s, deps)
    assert out.outcome == "deferred" and "deferred in the last 6h" in out.summary and model.calls == 0
    # an old one does not
    st.write_json("agents/other-site-article-proposal-agent/runs/20261204T162000Z/deferred.json", {
        "deferred_at": (LIVE_NOW - dt.timedelta(hours=9)).isoformat(), "required_model": "claude-opus-5-5",
        "exit_code": 75, "reason": "required model unavailable"})
    deps, _store, _c = make_deps(s, model=LiveFake(), storage=st)
    assert live_run(s, deps).outcome == "published"

    class Broken(LocalFilesystemStorage):
        def list_prefix(self, prefix, limit=10000):
            raise OSError("blob service unreachable")
    model = LiveFake()
    deps, _store, _c = make_deps(s, model=model, storage=Broken(str(tmp_path / "b")))
    out = live_run(s, deps)
    assert out.outcome == "deferred" and "unreadable" in out.summary and model.calls == 0
    # a malformed backoff entry is skipped, never a crash
    st2 = LocalFilesystemStorage(str(tmp_path / "st2"))
    st2.write_json("framework/defer-backoff/other-site-article-proposal-agent.json",
                   {"recs": {"x": {"last_deferred_at": "yesterday-ish", "last_reason": "required-model-unavailable"}}})
    deps, _store, _c = make_deps(s, model=LiveFake(), storage=st2)
    assert live_run(s, deps).outcome == "published"


def test_live_model_dry_runs_and_live_runs_share_the_days_opus_ledger(tmp_path):
    s = settings()
    ledger = cap.UsageLedger(tmp_path / "ledger.json")
    ledger.add("2026-12-05", minutes=19.9, dry_run_calls=10)
    model = LiveFake()
    deps, _store, _c = make_deps(s, model=model)
    deps.ledger = ledger
    out = E.GuideRun(deps, E.RunOptions(dry_run=True, live_model=True), agent_id=AGENT_ID, prior_state={}).run()
    assert out.outcome == "dry-run-pass" and model.calls == 1
    assert ledger.day("2026-12-05")["dry_run_calls"] == 11
    ledger.add("2026-12-05", minutes=0.2)
    deps, _store, _c = make_deps(s, model=model)
    deps.ledger = ledger
    out = live_run(s, deps, state={})                     # no state: the ledger alone caps it
    assert out.outcome == "deferred" and "minutes cap" in out.summary and model.calls == 1
    # the dry-run call cap
    l2 = cap.UsageLedger(tmp_path / "l2.json")
    l2.add("2026-12-05", dry_run_calls=49)
    deps, _store, _c = make_deps(s, model=LiveFake())
    deps.ledger = l2
    out = E.GuideRun(deps, E.RunOptions(dry_run=True, live_model=True), agent_id=AGENT_ID).run()
    assert out.outcome == "deferred" and "dry-run calls cap" in out.summary
    # a corrupt ledger denies
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    deps, _store, _c = make_deps(s, model=LiveFake())
    deps.ledger = cap.UsageLedger(bad)
    assert live_run(s, deps).outcome == "deferred"


def test_an_error_after_a_model_call_keeps_the_minutes(tmp_path):
    s = settings()

    class Boom(FakeStore):
        def insert_article(self, row):
            raise RuntimeError("connection reset")
    ledger = cap.UsageLedger(tmp_path / "ledger.json")
    ag, st = _agent(tmp_path, s, Boom(s), model_factory=lambda a, m: LiveFake(), gate_fn=lambda **kw: open_gate(),
                    ledger=ledger)
    r = ag.run_once()
    assert r.status == "failure" and "connection reset" in r.summary
    assert _state(st).get("opus_day") == "2026-12-05" and "opus_minutes_day" in _state(st)
    assert ledger.day("2026-12-05")["live_calls"] == 1


def test_claude_cli_unverified_defers_and_verified_runs_isolated(tmp_path, monkeypatch):
    import yaml
    block = yaml.safe_load(EXAMPLE.read_text())["article_engine"]
    s_unverified = S.build_settings(block, agent_id=AGENT_ID, source=str(EXAMPLE))
    model = LiveFake()
    deps, _store, _c = make_deps(s_unverified, model=model)
    out = live_run(s_unverified, deps)
    assert out.outcome == "deferred" and "not isolated" in out.summary and model.calls == 0
    # verified: the call runs from an empty temp dir through the wrapper; both restored afterwards
    s = settings()
    seen = {}

    class Spy(LiveFake):
        def chat(self, messages, **kw):
            seen["cwd"] = os.getcwd()
            seen["listing"] = os.listdir(seen["cwd"])
            seen["cmd"] = os.environ.get("CLAUDE_CLI_CMD")
            seen["real"] = os.environ.get("AFFILIATE_ARTICLE_REAL_CLAUDE")
            return super().chat(messages, **kw)
    monkeypatch.setenv("CLAUDE_CLI_CMD", "/opt/pool/bin/claude")
    here = os.getcwd()
    deps, _store, _c = make_deps(s, model=Spy())
    assert live_run(s, deps).outcome == "published"
    assert seen["cwd"] != here and seen["listing"] == [] and seen["cmd"] == str(E.WRAPPER)
    assert seen["real"] == "/opt/pool/bin/claude"
    assert os.getcwd() == here and os.environ["CLAUDE_CLI_CMD"] == "/opt/pool/bin/claude"
    assert "AFFILIATE_ARTICLE_REAL_CLAUDE" not in os.environ
    wrapper = E.WRAPPER.read_text()
    assert '--tools ""' in wrapper and "--strict-mcp-config" in wrapper and os.access(E.WRAPPER, os.X_OK)


@pytest.mark.parametrize("model_over,needle", [
    ({"required_tier": "sonnet"}, "Opus-only"),
    ({"allowed_provider_kinds": []}, "allowed_provider_kinds"),
])
def test_settings_refuse_a_non_opus_engine(model_over, needle):
    import yaml
    block = yaml.safe_load(EXAMPLE.read_text())["article_engine"]
    block["model"] = {**block["model"], **model_over}
    with pytest.raises(S.SettingsError, match=needle):
        S.build_settings(block, agent_id=AGENT_ID, source=str(EXAMPLE))


def test_settings_refuse_multi_statement_sql():
    import yaml
    block = yaml.safe_load(EXAMPLE.read_text())["article_engine"]
    block["signals"]["data_sql"] = "SELECT 1; SET SESSION CHARACTERISTICS AS TRANSACTION READ WRITE"
    with pytest.raises(S.SettingsError, match="signals.data_sql"):
        S.build_settings(block, agent_id=AGENT_ID, source=str(EXAMPLE))


def test_held_pending_draft_is_regated_before_any_new_guide():
    s = settings()

    class Store(FakeStore):
        def own_pending_slugs(self, agent_id, limit=2):
            return ["an-earlier-held-guide"]
    model = LiveFake()
    seen = []
    deps, store, _c = make_deps(s, store=Store(s), model=model,
                                publish=lambda slug: seen.append(slug) or {"promoted": True, "reasons": []})
    out = live_run(s, deps)
    assert out.outcome == "published" and seen == ["an-earlier-held-guide"]
    assert model.calls == 0 and not store.inserted and out.metrics["guides_published"] == 1.0
    # still held: reported, and the run goes on to a new guide
    seen.clear()
    deps, store, _c = make_deps(s, store=Store(s), model=LiveFake(), publish=lambda slug: seen.append(slug) or (
        {"promoted": slug != "an-earlier-held-guide", "reasons": ["claims: x"]}))
    out = live_run(s, deps)
    assert out.outcome == "published" and seen[0] == "an-earlier-held-guide" and len(store.inserted) == 1
    assert out.report["pending_retry"][0]["promoted"] is False


def test_gate_report_details_reach_the_reprompt():
    rep = {"promoted": [], "drafts": [{"slug": "x", "checks": [
        {"name": "link_guard", "passes": False, "reason": "too few links",
         "details": {"missing": ["/compat/alpha-500"]}}]}]}
    got = E.parse_gate_report(rep, "x", 1)
    assert "/compat/alpha-500" in got["reasons"][0]


def test_real_calendar_facts_packs_build_on_the_fixture_db(ro_db):
    """Every entry of the site's real calendar, with the site's real
    article-engine config: the facts pack builds (no money, no Amazon text),
    and the entry's own models come first in the stations rows."""
    site_yaml = SITE_DIR / "agents" / "article-engine" / "site.yaml"
    if not site_yaml.exists():
        pytest.skip("site config not found")
    s = S.load_settings(str(site_yaml), agent_id="real-calendar-check", environ={})
    store = A.store_factory(s, readonly_only=True, environ={s.dsn_env: ro_db})()
    cal = ecal.load_calendar(s.calendar_path)
    try:
        for e in cal.entries:
            pack = store.facts({**s.fact_params, **e.scope})        # raises FactsPackViolation on money
            assert fp.commerce_violations(pack.rows) == [], e.id
            assert all("reason" not in k or k == "pick_reason" for r in pack.rows.get("ladder_slots", [])
                       for k in r), e.id
            picks = {r["model_id"] for r in pack.rows.get("picks", []) if r.get("model_id")}
            st_ids = [r["model_id"] for r in pack.rows["stations"]]
            in_scope = [r["model_id"] for r in pack.rows["stations"] if r["in_scope"]]
            assert picks <= set(in_scope), (e.id, picks - set(in_scope))
            assert st_ids[:len(in_scope)] == in_scope, e.id
        # a tiny limit keeps the entry's own models, not the smallest stations
        s2 = S.load_settings(str(site_yaml), agent_id="real-calendar-check", environ={})
        e = cal.by_id("fridge-24h-outage")
        pack = store.facts({**s2.fact_params, **e.scope, "stations_limit": 3})
        picks = {r["model_id"] for r in pack.rows["picks"]}
        assert {r["model_id"] for r in pack.rows["stations"]} <= picks | {
            r["model_id"] for r in pack.rows["stations"] if r["in_scope"]}
    finally:
        store.close()
