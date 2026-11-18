"""agents/spec-extractor-agent: the spec-extractor engine (F17).

Offline by construction:
  * HTTP: a fake transport serves the F9 fixture documents (real, trimmed
    manufacturer pages and manuals: framework/tests/fixtures/spec_sources)
    and robots.txt files; urllib is patched to fail.
  * LLM: a fake chat answers the batched fallback; the framework chat route
    is patched to fail, so no test can reach a model.
  * DB: a throwaway postgres:16 container (one per test process, one
    database per test) loaded with agents/spec-extractor-agent/fixtures/
    schema.sql, the reference implementation of the TABLE CONTRACT. DB tests
    skip without docker / the local image; F17_TEST_PG_DSN may name an admin
    conninfo of an existing THROWAWAY server instead.
  * Storage: the conftest LocalFilesystemStorage in tmp_path.

Covered: settings validation (schema, identifiers, fragments, runtime, the
dsn_env name), the source scheduler (due reasons, backoff, refusal, host
interleaving), the runtime estimator, candidate round-trips, and end to end
on the fixture catalog: provenance rows, typed rows, model status, conflicts
into the adjudication queue, an operator decision unlocking compat edges,
compat edges exactly as framework.core.compat_graph computes them from the
stored values, runtime estimates (and their removal when a model leaves the
scope), the Amazon refusal, short-circuits (unchanged sources are not
parsed, unchanged models are not rewritten, conditional GETs) and
AGENT_FORCE_RUN / ENGINE_VERSION bypassing them, never blanking on failed
fetches, a config change re-extracting from the fetch cache, the batched
cheap-model fallback, its pool-reserve deferral and LLM answers surviving a
config change, disabled rulesets purged, a kind change removing the old
typed row, a failing run keeping its state, a dropped connection replaced
between phases, operator rows never deleted, read-only CLI sessions, the
advisory lock and the goals wiring. Compat withdrawals: a deleted fit record
and an edited published list (the shipped template's fit_lists) lose their
edges on the next incremental run; the manual rows the engine writes from
operator fit records are released with their record, a removed model or a
disabled ruleset (replaced by the edge that decides the pair now), an
operator's own or hand-edited manual row is never touched, a lost state
re-adopts, and a failing batch keeps the ownership it committed. The DSN:
mis-encoded passwords ('%', '@', a space) never reach a RunResult field, a
file under the storage root, stdout/stderr or the CLI, also on a reconnect.
"""
from __future__ import annotations

import atexit
import copy
import importlib.util as iu
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import pytest

from framework.core import compat_graph as cg
from framework.core import spec_sources as ss

ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = ROOT / "agents" / "spec-extractor-agent"
FX = AGENT_DIR / "fixtures"
SSFX = ROOT / "framework" / "tests" / "fixtures" / "spec_sources"

if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))
_spec = iu.spec_from_file_location("spec_extractor_agent_under_test", AGENT_DIR / "agent.py")
A = iu.module_from_spec(_spec)
sys.modules[_spec.name] = A
_spec.loader.exec_module(A)
S = A.settings_mod
P = A.pipeline
ST = A.store_mod
REAL_DEFAULT_CONNECT = A.default_connect     # the autouse guard replaces the module attribute

AGENT_ID = "example-site-spec-extractor"
DSN_ENV = "F17_SPEC_EXTRACTOR_TEST_DSN"
SOURCES = {d["id"]: d for d in json.loads((SSFX / "SOURCES.json").read_text())["documents"]}
JACKERY_PAGE = SOURCES["jackery-product-page"]["url"]
JACKERY_FAQ = SOURCES["jackery-faq"]["url"]
PECRON_PDF = SOURCES["pecron-manual"]["url"]
FOSSI_PAGE = SOURCES["fossibot-product-page"]["url"]
FOSSI_MANUAL = SOURCES["fossibot-manual"]["url"]
VOLTARO_PAGE = "https://www.voltaro.example/products/vx2000"
AMAZON_URL = "https://www.amazon.com/dp/B0EXAMPLE1"
PDF_BYTES = b"%PDF-1.4\n% stand-in bytes for the Pecron E2000LFP manual\n"
START = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Guards: no network, no LLM, no production database
# ---------------------------------------------------------------------------

LLM_ROUTE_CALLS: list[str] = []


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    import urllib.request

    def no_network(*_a, **_k):
        raise AssertionError("network access attempted in an offline test")

    def no_llm(*_a, **_k):
        LLM_ROUTE_CALLS.append("routed")
        raise AssertionError("a test reached the framework LLM route")

    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    monkeypatch.setattr(ss, "urllib_transport", no_network)
    monkeypatch.setattr(ss, "_routed_chat", no_llm)
    monkeypatch.setattr(ss, "pool_admits", lambda *_a, **_k: no_llm())
    monkeypatch.setattr(A, "default_connect", no_network)
    LLM_ROUTE_CALLS.clear()
    from framework.core import site_registry
    site_registry.clear_cache()
    yield
    site_registry.clear_cache()


# ---------------------------------------------------------------------------
# Throwaway Postgres (one container per process; shared with the pick-builder
# tests through F17_TEST_PG_DSN; password via a 0600 env-file)
# ---------------------------------------------------------------------------

_PG: dict[str, Any] = {}
_IMAGE = "postgres:16"
PG_ENV = "F17_TEST_PG_DSN"


def _free_port(start: int = 55100, end: int = 55990) -> int:
    for port in range(start + secrets.randbelow(200), end):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("no free port above 55000")


def _conninfo(params: dict, dbname: str) -> str:
    return " ".join(f"{k}={v}" for k, v in {**params, "dbname": dbname}.items())


def _stop_container() -> None:
    name = _PG.pop("container", None)
    if name:
        subprocess.run(["docker", "stop", "-t", "2", name], capture_output=True, timeout=60)


def pg_params() -> dict:
    if "params" in _PG:
        return _PG["params"]
    if "skip" in _PG:
        pytest.skip(_PG["skip"])
    psycopg2 = pytest.importorskip("psycopg2")
    env = os.environ.get(PG_ENV, "").strip()
    if env:
        parsed = psycopg2.extensions.parse_dsn(env)
        _PG["params"] = {k: parsed[k] for k in ("host", "port", "user", "password") if parsed.get(k)}
        return _PG["params"]
    if not shutil.which("docker"):
        _PG["skip"] = "docker not available for the throwaway Postgres"
        pytest.skip(_PG["skip"])
    if subprocess.run(["docker", "image", "inspect", _IMAGE], capture_output=True, timeout=30).returncode:
        _PG["skip"] = f"docker image {_IMAGE} not present locally (offline test run)"
        pytest.skip(_PG["skip"])
    password = secrets.token_hex(16)
    port = _free_port()
    name = f"rp-f17-pg-{os.getpid()}-{secrets.token_hex(3)}"
    fd, env_path = tempfile.mkstemp(prefix="f17-pg-", suffix=".env")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(f"POSTGRES_PASSWORD={password}\n")
        os.chmod(env_path, 0o600)
        r = subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "--env-file", env_path,
                            "-p", f"127.0.0.1:{port}:5432", _IMAGE], capture_output=True, text=True, timeout=120)
    finally:
        os.unlink(env_path)
    if r.returncode != 0:
        _PG["skip"] = f"could not start {_IMAGE}: {r.stderr.strip()[:200]}"
        pytest.skip(_PG["skip"])
    _PG["container"] = name
    atexit.register(_stop_container)
    params = {"host": "127.0.0.1", "port": port, "user": "postgres", "password": password}
    deadline = time.time() + 90
    while True:
        try:
            psycopg2.connect(_conninfo(params, "postgres"), connect_timeout=2).close()
            break
        except psycopg2.OperationalError:
            if time.time() > deadline:
                _stop_container()
                _PG["skip"] = "throwaway Postgres did not become ready"
                pytest.skip(_PG["skip"])
            time.sleep(0.5)
    _PG["params"] = params
    os.environ[PG_ENV] = _conninfo(params, "postgres")   # the pick-builder tests reuse it
    return params


def fresh_database(prefix: str) -> str:
    import psycopg2
    params = pg_params()
    name = f"{prefix}_{secrets.token_hex(5)}"
    admin = psycopg2.connect(_conninfo(params, "postgres"))
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f"CREATE DATABASE {name}")
    admin.close()
    return _conninfo(params, name)


def query(dsn: str, sql: str, params: Any = None) -> list[dict]:
    import psycopg2
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            if cur.description is None:
                conn.commit()
                return []
            cols = [c[0] for c in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        conn.commit()
        return rows
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Fixture catalog
# ---------------------------------------------------------------------------

SEED_SQL = """
INSERT INTO brands (id, slug, name) VALUES
  (1, 'jackery', 'Jackery'), (2, 'pecron', 'Pecron'), (3, 'fossibot', 'FOSSiBOT'),
  (4, 'voltaro', 'Voltaro'), (5, 'example', 'Example');
INSERT INTO models (id, brand_id, kind, model_name, model_number, lifecycle) VALUES
  (1, 1, 'station', 'Explorer 1000 v2', 'JE-1000D', 'live'),
  (2, 2, 'station', 'E2000LFP', NULL, 'live'),
  (3, 3, 'station', 'F2400', NULL, 'live'),
  (4, 4, 'station', 'VX2000', 'VX-2000', 'live'),
  (5, 5, 'station', 'Listing-only station', NULL, 'live'),
  (6, 5, 'station', 'Retired station', NULL, 'retired'),
  (12, 5, 'panel', 'Example 100W DC8020 panel', NULL, 'live'),
  (13, 5, 'panel', 'Example 400W rigid panel', NULL, 'live'),
  (21, 5, 'adapter', 'Example MC4 to DC8020 adapter', NULL, 'live');
INSERT INTO spec_sources (id, model_id, kind, url, publisher) VALUES
  (101, 1, 'manufacturer-page', %(jp)s, 'jackery.com'),
  (102, 1, 'manufacturer-faq', %(jf)s, 'jackery.com'),
  (103, 2, 'manual-pdf', %(pp)s, 'pecron.com'),
  (104, 3, 'manufacturer-page', %(fp)s, 'fossibot.com'),
  (105, 3, 'manual-pdf', %(fm)s, 'manualslib.tech'),
  (106, 4, 'manufacturer-page', %(vp)s, 'voltaro.example'),
  (107, 5, 'manufacturer-page', %(az)s, NULL),
  (108, 6, 'manufacturer-page', 'https://www.retired.example/station', NULL),
  (112, 12, 'manual-entry', 'https://panels.example/example-100w-dc8020', 'panels.example'),
  (113, 13, 'manual-entry', 'https://panels.example/example-400w-rigid', 'panels.example'),
  (121, 21, 'manual-entry', 'https://panels.example/mc4-to-dc8020', 'panels.example');
-- operator-entered values (extractor 'manual', extractor_id NULL): panels, the
-- adapter, and two station facts hand-checked against the FAQ
INSERT INTO spec_values (model_id, field, value_num, value_text, unit, source_id, extractor, confidence, status) VALUES
  (12, 'voc', 24.0, NULL, 'V', 112, 'manual', 1, 'accepted'),
  (12, 'vmp', 19.8, NULL, 'V', 112, 'manual', 1, 'accepted'),
  (12, 'isc', 5.6, NULL, 'A', 112, 'manual', 1, 'accepted'),
  (12, 'rated_w', 100, NULL, 'W', 112, 'manual', 1, 'accepted'),
  (12, 'temp_coeff_voc_pct_c', -0.3, NULL, '%%/degC', 112, 'manual', 1, 'accepted'),
  (12, 'cell_type', NULL, 'Monocrystalline', NULL, 112, 'manual', 1, 'accepted'),
  (12, 'connector', NULL, 'DC8020', NULL, 112, 'manual', 1, 'accepted'),
  (13, 'voc', 49.5, NULL, 'V', 113, 'manual', 1, 'accepted'),
  (13, 'vmp', 41.5, NULL, 'V', 113, 'manual', 1, 'accepted'),
  (13, 'isc', 10.3, NULL, 'A', 113, 'manual', 1, 'accepted'),
  (13, 'rated_w', 0.4, NULL, 'kW', 113, 'manual', 1, 'accepted'),
  (13, 'temp_coeff_voc_pct_c', -0.27, NULL, '%%/degC', 113, 'manual', 1, 'accepted'),
  (13, 'cell_type', NULL, 'Monocrystalline PERC', NULL, 113, 'manual', 1, 'accepted'),
  (13, 'connector', NULL, 'MC4', NULL, 113, 'manual', 1, 'accepted'),
  (21, 'connector_a', NULL, 'MC4', NULL, 121, 'manual', 1, 'accepted'),
  (21, 'connector_b', NULL, 'DC8020', NULL, 121, 'manual', 1, 'accepted'),
  (21, 'max_a', 15, NULL, 'A', 121, 'manual', 1, 'accepted'),
  (21, 'max_v', 60, NULL, 'V', 121, 'manual', 1, 'accepted'),
  (1, 'pv_input_connectors', NULL, 'DC8020', NULL, 102, 'manual', 1, 'accepted'),
  (1, 'pv_current_limited', 1, 'true', NULL, 102, 'manual', 1, 'accepted');
INSERT INTO compat_fit_records (a_model_id, b_model_id, relation, verdict, origin, source_url, source_kind,
                                publisher, statement) VALUES
  (1, 21, 'adapter_for', 'fits', 'manufacturer', 'https://panels.example/mc4-to-dc8020', 'manufacturer-page',
   'panels.example', 'Compatible with Jackery Explorer 1000 v2 (DC8020 solar input)');
INSERT INTO load_presets (id, label, avg_w, surge_w, duty_cycle, wh_per_day, source_url) VALUES
  ('fridge', 'Full-size refrigerator', 150, 1200, 0.33, NULL, 'https://www.energystar.gov/productfinder'),
  ('router', 'Wi-Fi router', 20, NULL, NULL, NULL, 'https://www.energy.gov/energysaver'),
  ('well-pump', 'Well pump', 2200, 4500, NULL, NULL, 'https://www.energy.gov/energysaver');
INSERT INTO products (asin, model_id, kind) VALUES
  ('B0JACKERY1', 1, 'station'), ('B0PECRON01', 2, 'station'), ('B0FOSSI001', 3, 'station'),
  ('B0VOLTARO1', 4, 'station'), ('B0LISTING1', 5, 'station'), ('B0PANEL100', 12, 'panel');
"""


def direct_graph_build(h) -> "cg.GraphResult":
    """What framework.core.compat_graph computes from the stored ACCEPTED
    values and fit records of the in-scope models: the engine must store
    exactly its stored edges and report its tallies."""
    graph = cg.CompatGraph(cg.resolve_config(site_cfg={"profile": "home-power"}, file_cfg=h.compat_cfg))
    kinds = sorted({k for rs in graph.rulesets.values() if rs.enabled for k in (*rs.a_kinds, *rs.b_kinds)})
    nodes = []
    for m in h.q("SELECT id, kind, name, brand, model_number FROM v_spec_models "
                 "WHERE lifecycle <> 'retired' AND kind = ANY(%s) ORDER BY id", (kinds,)):
        rows = h.q("SELECT v.model_id, v.field, v.value_num, v.value_text, v.unit, v.source_id, v.status, "
                   "s.url AS source_url, s.kind AS source_kind, s.publisher, s.content_sha256 "
                   "FROM spec_values v LEFT JOIN spec_sources s ON s.id = v.source_id "
                   "WHERE v.model_id = %s AND v.status = 'accepted' ORDER BY v.field", (m["id"],))
        for r in rows:
            r["value_num"] = None if r["value_num"] is None else float(r["value_num"])
        extra = ({"model_number": m["model_number"]}
                 if m["model_number"] and not any(r["field"] == "model_number" for r in rows) else None)
        nodes.append(cg.ModelSpec.from_db_rows(m["id"], m["kind"], rows, name=m["name"] or "",
                                               brand=m["brand"] or "", extra=extra))
    records = [cg.FitRecord.from_dict({k: v for k, v in r.items() if v is not None}) for r in h.q(
        "SELECT a_model_id AS a, b_model_id AS b, relation, verdict, origin, config, via_model_id AS via, "
        "source_url, source_kind, publisher, source_id, statement FROM compat_fit_records")]
    return graph.build(nodes, records)


def spec_file_cfg() -> dict:
    hp = json.loads((SSFX / "profiles" / "home-power.json").read_text())
    extra = json.loads((FX / "spec-sources-test-profiles.json").read_text())
    hp = copy.deepcopy(hp)
    hp["fields"] = {**hp["fields"], **extra["station_extra_fields"]}
    return {"defaults": {}, "profiles": {"home-power": hp, **extra["profiles"]}, "by_site": {}, "by_agent_id": {}}


def compat_file_cfg() -> dict:
    return json.loads((FX / "compat-graph-test-config.json").read_text())


def site_block(**over) -> dict:
    block = {
        "site": "example-site",
        "database": {"dsn_env": DSN_ENV},
        "tables": {
            "models": {"relation": "v_spec_models", "write_table": "models", "where": "m.lifecycle <> 'retired'",
                       "columns": {"id": "id", "kind": "kind", "brand": "brand", "name": "name",
                                   "model_number": "model_number"},
                       "status_columns": {"status": "spec_status", "completeness": "spec_completeness",
                                          "checked_at": "spec_checked_at"}},
            "values": {"columns": {"snippet": "snippet", "qualifier": "qualifier", "conflict_id": "conflict_id"}},
            "products": {"table": "products", "where": "p.is_active",
                         "columns": {"id": "id", "model_id": "model_id", "kind": "kind"}},
        },
        "spec_sources": {"profile": "home-power",
                         "fetch": {"min_interval_s": 0,
                                   "user_agent": "Mozilla/5.0 (compatible; SpecSourcesBot/1.0; "
                                                 "+https://example-site.example/bot)"}},
        "kinds": {
            "station": {
                "required": ["capacity_wh", "chemistry", "ac_cont_w", "pv_voc_max"],
                "typed": {"table": "station_specs",
                          "columns": {"model_number": "model_number", "capacity_wh": "capacity_wh",
                                      "chemistry": "chemistry", "ac_cont_w": "ac_cont_w",
                                      "ac_surge_w": "ac_surge_w", "ups_ms": "ups_ms",
                                      "ups_supported": "ups_supported", "pv_v_min": "pv_v_min",
                                      "pv_voc_max": "pv_voc_max", "pv_w_max": "pv_w_max",
                                      "weight_kg": "weight_kg", "dimensions_mm": "dimensions_mm"},
                          "completeness": "spec_completeness", "updated_at": "validated_at"}},
            "panel": {"spec_sources": {"profile": "solar-panel"},
                      "typed": {"table": "panel_specs",
                                "columns": {"rated_w": "rated_w", "voc": "voc", "vmp": "vmp", "isc": "isc",
                                            "temp_coeff_voc_pct_c": "temp_coeff_voc_pct_c",
                                            "connector": "connector"},
                                "updated_at": "validated_at"}},
            "adapter": {"spec_sources": {"profile": "solar-adapter"}},
        },
        "compat": {"enabled": True, "graph": {"profile": "home-power"},
                   "fit_records": {"table": "compat_fit_records"}},
        "runtime": {"enabled": True, "kinds": ["station"],
                    "energy": {"rated_field": "capacity_wh", "rated_factor": 0.842, "rated_interval": [0.75, 0.925]},
                    "continuous_field": "ac_cont_w", "peak_field": "ac_surge_w",
                    "presets": {"table": "load_presets",
                                "columns": {"id": "id", "power_w": "avg_w", "peak_w": "surge_w",
                                            "duty_cycle": "duty_cycle", "energy_per_day_wh": "wh_per_day"}},
                    "output": {"table": "runtime_estimates",
                               "columns": {"usable_energy": "usable_wh", "peak_ok": "surge_ok"}}},
        "metrics": {"primary_kinds": ["station"]},
    }
    return S.merge(block, over)


class Clock:
    def __init__(self, t: datetime = START):
        self.t = t
        self.mono = 1000.0

    def now(self) -> datetime:
        return self.t

    def monotonic(self) -> float:
        return self.mono

    def sleep(self, s: float) -> None:
        self.mono += s

    def advance(self, hours: float) -> None:
        self.t += timedelta(hours=hours)
        self.mono += hours * 3600


class FakeHttp:
    """url -> (status, body, content type, etag). Answers 304 to a matching
    If-None-Match. Unknown URLs 404. Every request is recorded; ``on_call``
    (optional) runs first (a test can drop the DB connection mid-fetch)."""

    ALLOW_ALL = b"User-agent: *\nAllow: /\n"

    def __init__(self):
        self.pages: dict[str, tuple[int, bytes, str, str]] = {}
        self.calls: list[tuple[str, dict]] = []
        self.on_call = None
        robots = SSFX / "robots"
        self.set("https://www.jackery.com/robots.txt", (robots / "jackery.com.robots.txt").read_bytes(), "text/plain")
        self.set("https://cdn.shopify.com/robots.txt", (robots / "cdn.shopify.com.robots.txt").read_bytes(),
                 "text/plain")
        for host in ("www.fossibot.com", "www.manualslib.tech", "www.voltaro.example"):
            self.set(f"https://{host}/robots.txt", self.ALLOW_ALL, "text/plain")
        for doc_id in ("jackery-product-page", "jackery-faq", "fossibot-product-page", "fossibot-manual"):
            s = SOURCES[doc_id]
            self.set(s["url"], (SSFX / s["file"]).read_bytes(), "text/html; charset=utf-8", etag=f'"{doc_id}-v1"')
        self.set(PECRON_PDF, PDF_BYTES, "application/pdf", etag='"pecron-v1"')
        self.set(VOLTARO_PAGE, (FX / "docs" / "voltaro-vx2000-synthetic.html").read_bytes(),
                 "text/html; charset=utf-8")

    def set(self, url: str, body: bytes, ctype: str = "text/html; charset=utf-8", *, status: int = 200,
            etag: str = "") -> None:
        self.pages[url] = (status, body, ctype, etag)

    def __call__(self, url, headers, timeout_s, max_bytes):
        if self.on_call is not None:
            self.on_call(url)
        self.calls.append((url, dict(headers)))
        page = self.pages.get(url)
        if page is None:
            return ss.HttpResponse(404, {}, b"", url)
        status, body, ctype, etag = page
        h = {"content-type": ctype}
        if etag:
            h["etag"] = etag
        if status != 200:
            return ss.HttpResponse(status, h, b"", url)
        if etag and headers.get("If-None-Match") == etag:
            return ss.HttpResponse(304, h, b"", url)
        return ss.HttpResponse(200, h, body, url)

    def urls(self) -> list[str]:
        return [u for u, _ in self.calls if not u.endswith("/robots.txt")]


def pdf_converter(data: bytes) -> str:
    assert data == PDF_BYTES, "only the stand-in Pecron PDF is converted"
    return (SSFX / SOURCES["pecron-manual"]["file"]).read_text()


class Harness:
    def __init__(self, tmp_path: Path, storage, dsn: str = ""):
        self.clock = Clock()
        self.http = FakeHttp()
        self.storage = storage
        self.dsn = dsn
        self.tmp = tmp_path
        self.block = site_block()
        self.spec_cfg = spec_file_cfg()
        self.compat_cfg = compat_file_cfg()
        self.chat = None
        self.admit = lambda _m: True
        self.chat_calls: list[list[dict]] = []
        self.conns: list = []

    def collab(self) -> "P.Collaborators":
        return P.Collaborators(transport=self.http, cache_dir=str(self.tmp / "fetch-cache"),
                               clock=self.clock.now, monotonic=self.clock.monotonic, sleep=self.clock.sleep,
                               pdf_converter=pdf_converter, chat=self.chat, admit=self.admit,
                               spec_file_cfg=self.spec_cfg, compat_file_cfg=self.compat_cfg)

    def settings(self):
        return S.build_settings(self.block, agent_id=AGENT_ID)

    def connect(self, _settings=None, environ=None):
        import psycopg2
        conn = psycopg2.connect(self.dsn)
        self.conns.append(conn)
        return conn

    def agent(self):
        ag = A.SpecExtractorAgent(agent_id=AGENT_ID, storage=self.storage, settings=self.settings(),
                                  connect=self.connect, collaborators=self.collab())
        ag.auto_recovery_enabled = False
        return ag

    def state(self) -> dict:
        return (self.storage.read_json(f"agents/{AGENT_ID}/state/latest.json") or {}).get("state") or {}

    def run(self):
        return self.agent().run_once()

    def q(self, sql: str, params: Any = None) -> list[dict]:
        return query(self.dsn, sql, params)

    def values(self, model_id: int, status: Optional[str] = "accepted") -> dict:
        rows = self.q("SELECT field, value_num, value_text FROM spec_values WHERE model_id = %s"
                      + (" AND status = %s" if status else ""), (model_id, status) if status else (model_id,))
        return {r["field"]: (float(r["value_num"]) if r["value_num"] is not None and r["value_text"] is None
                             else r["value_text"]) for r in rows}

    def queue(self) -> dict:
        return self.storage.read_json(f"agents/{AGENT_ID}/spec-adjudication/queue.json") or {}


@pytest.fixture
def db():
    import psycopg2
    dsn = fresh_database("spx")
    conn = psycopg2.connect(dsn)
    with conn.cursor() as cur:
        cur.execute((FX / "schema.sql").read_text())
        cur.execute(SEED_SQL, {"jp": JACKERY_PAGE, "jf": JACKERY_FAQ, "pp": PECRON_PDF, "fp": FOSSI_PAGE,
                               "fm": FOSSI_MANUAL, "vp": VOLTARO_PAGE, "az": AMAZON_URL})
    conn.commit()
    conn.close()
    return dsn


@pytest.fixture
def h(tmp_path, storage, db):
    return Harness(tmp_path, storage, db)


# ---------------------------------------------------------------------------
# Settings (no DB)
# ---------------------------------------------------------------------------

def test_example_site_yaml_is_a_valid_template():
    st = S.load_settings(str(AGENT_DIR / "site.example.yaml"), agent_id="example-site-spec-extractor")
    assert st.site == "example-site" and st.dsn_env == "DATABASE_URL_EXAMPLE_SITE"
    assert sorted(st.kinds) == ["battery", "panel", "station"]     # battery: the fit list's subject kind
    assert st.kinds["station"].typed.name == "station_specs"
    assert st.models.name == "v_spec_models" and st.models_write_table == "models"
    assert st.lock_key == "spec-extractor:example-site-spec-extractor"


def test_manifest_is_an_inert_engine_template():
    m = json.loads((AGENT_DIR / "manifest.json").read_text())
    assert m["id"] == "spec-extractor-agent" and m["enabled"] is False
    assert "cron_expr" not in m and "entry_command" not in m
    assert m["metadata"]["config_env"] == S.ENV_CONFIG


def test_goals_are_valid_and_bound_to_emitted_metrics():
    from framework.core.goals import _validate_goals_doc
    doc = json.loads((AGENT_DIR / "goals.json").read_text())
    _validate_goals_doc(doc)
    assert 3 <= len(doc["goals"]) <= 7
    metrics = {g["target_metric"] for g in doc["goals"]}
    assert metrics == {"primary_spec_complete_pct", "open_spec_conflicts", "runtime_models_live",
                       "amazon_sourced_values", "sources_unhealthy_pct"}
    targets = {g["target_metric"]: g["metric"]["target"] for g in doc["goals"]}
    assert targets["primary_spec_complete_pct"] == 45 and targets["open_spec_conflicts"] == 20


@pytest.mark.parametrize("patch,needle", [
    ({"tables": {"sources": {"table": "Spec Sources"}}}, "tables.sources.table"),
    ({"tables": {"values": {"columns": {"extractor_id": None}}}}, "values.columns.extractor_id: required"),
    ({"tables": {"models": {"where": "m.kind = 'a'; DROP TABLE x"}}}, "may not contain ';'"),
    ({"tables": {"models": {"where": "m.name LIKE 'a%'"}}}, "double every literal '%'"),
    ({"sources": {"fetch_kinds": ["manufacturer-page", "amazon-page"]}}, "unknown source kinds"),
    ({"sources": {"fetch_kinds": ["manual-entry"]}}, "never fetched"),
    ({"runtime": {"continuous_field": ""}}, "runtime.continuous_field"),
    ({"runtime": {"energy": {"rated_interval": [0.9, 0.8]}}}, "rated_interval"),
    ({"runtime": {"kinds": ["robot"]}}, "not configured under kinds"),
    ({"kinds": {"station": {"fields": ["capacity_wh"], "required": ["chemistry"]}}}, "not in kinds.station.fields"),
])
def test_semantic_validation_rejects_bad_config(patch, needle):
    with pytest.raises(S.SettingsError) as exc:
        S.build_settings(site_block(**patch), agent_id=AGENT_ID)
    assert needle in str(exc.value)


def test_schema_rejects_unknown_keys_and_missing_dsn(storage):
    with pytest.raises(S.SettingsError, match="Additional properties"):
        S.build_settings(site_block(sources={"fetch_kind": []}), agent_id=AGENT_ID)
    block = site_block()
    block["database"] = {}
    block["site"] = "no-such-site"
    with pytest.raises(S.SettingsError, match="dsn_env"):
        S.build_settings(block, agent_id=AGENT_ID, storage=storage)


def test_identifiers_are_quoted_and_validated():
    assert S.quote_ident("public.spec_values") == '"public"."spec_values"'
    with pytest.raises(S.SettingsError):
        S.quote_ident('x"; DROP TABLE y; --')


def test_contract_sql_uses_the_configured_names():
    st = S.build_settings(site_block(), agent_id=AGENT_ID)
    store = ST.SpecStore(None, st)
    assert 'FROM "v_spec_models" m' in store.models_sql() and "(m.lifecycle <> 'retired')" in store.models_sql()
    sql = store.sources_sql()
    assert "- 'candidates'" in sql and '"fetch_state"' in sql
    assert "LIKE 'manual-entry%%'" in store._operator_pred()


def test_config_problems_fail_fast_before_any_fetch(tmp_path, storage):
    h = Harness(tmp_path, storage)
    h.block["kinds"]["station"]["fields"] = ["capacity_wh", "chemistry", "ac_cont_w", "pv_voc_max",
                                             "no_such_field"]
    engines, _common, _f, problems = P.build_engines(h.settings(), storage=storage, collab=h.collab())
    assert any("no_such_field" in p for p in problems)
    h2 = Harness(tmp_path, storage)
    h2.block["spec_sources"]["fetch"]["user_agent"] = "Mozilla/5.0 (compatible; SomeBot/1.0)"
    _e, _c, _f, problems = P.build_engines(h2.settings(), storage=storage, collab=h2.collab())
    assert any("robots_token" in p for p in problems)


def test_kind_engines_have_their_own_fields_and_required_sets(tmp_path, storage):
    h = Harness(tmp_path, storage)
    engines, common, fetcher, problems = P.build_engines(h.settings(), storage=storage, collab=h.collab())
    assert problems == []
    assert engines["station"].required == ("capacity_wh", "chemistry", "ac_cont_w", "pv_voc_max")
    assert "voc" not in engines["station"].cfg["fields"] and "capacity_wh" not in engines["panel"].cfg["fields"]
    assert engines["panel"].required == ("voc", "vmp", "isc", "rated_w")
    # one fetcher for every kind: the per-host interval holds across kinds
    assert engines["station"].ss.fetcher is engines["panel"].ss.fetcher is fetcher


def test_cli_config_and_sql_are_offline(tmp_path, capsys):
    cfg_file = tmp_path / "spec-sources.json"
    cfg_file.write_text(json.dumps(spec_file_cfg()))
    example = str(AGENT_DIR / "site.example.yaml")
    assert A.main(["--print-sql", "--site-yaml", example, "--agent-id", AGENT_ID]) == 0
    assert '"v_spec_models"' in capsys.readouterr().out
    assert A.main(["--config", "--site-yaml", example, "--agent-id", AGENT_ID,
                   "--spec-config-file", str(cfg_file)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["problems"] == [] and out["kinds"]["station"]["required"] == [
        "capacity_wh", "chemistry", "ac_cont_w", "pv_voc_max"]
    assert out["kinds"]["panel"]["llm_enabled"] is False
    assert A.main(["--config", "--site-yaml", str(tmp_path / "missing.yaml")]) == 2


# ---------------------------------------------------------------------------
# Scheduling, runtime math, serialisation (no DB)
# ---------------------------------------------------------------------------

def _src(state=None, url=JACKERY_PAGE, sid=1, model=1) -> "ST.SourceRow":
    return ST.SourceRow(id=sid, model_id=model, kind="manufacturer-page", url=url, state=dict(state or {}))


def test_due_reasons_cover_every_trigger():
    now = START
    fp = "fp1"
    assert P.due_reasons(_src(), now=now, refetch_hours=20, fingerprint=fp) == ["never-fetched", "not-extracted"]
    fresh = {"fetch": {"url": JACKERY_PAGE, "checked_at": P.to_iso(now - timedelta(hours=2)),
                       "content_sha256": "c1"}, "extracted_sha": "c1", "fingerprint": fp}
    assert P.due_reasons(_src(fresh), now=now, refetch_hours=20, fingerprint=fp) == []
    old = dict(fresh, fetch=dict(fresh["fetch"], checked_at=P.to_iso(now - timedelta(hours=21))))
    assert P.due_reasons(_src(old), now=now, refetch_hours=20, fingerprint=fp) == ["refetch"]
    assert P.due_reasons(_src(fresh), now=now, refetch_hours=20, fingerprint="fp2") == ["config-changed"]
    moved = dict(fresh, fetch=dict(fresh["fetch"], content_sha256="c2"))
    assert P.due_reasons(_src(moved), now=now, refetch_hours=20, fingerprint=fp) == ["not-extracted"]
    backoff = dict(old, next_attempt_at=P.to_iso(now + timedelta(hours=3)))
    assert P.due_reasons(_src(backoff), now=now, refetch_hours=20, fingerprint=fp) == []
    refused = dict(old, status="refused", refused_url=JACKERY_PAGE)
    assert P.due_reasons(_src(refused), now=now, refetch_hours=20, fingerprint=fp) == []
    # an operator who fixes the URL gets the row fetched again
    assert P.due_reasons(_src(refused, url=JACKERY_FAQ), now=now, refetch_hours=20, fingerprint=fp)


def test_backoff_doubles_and_caps():
    assert [P.backoff_hours(n, 6, 168) for n in (1, 2, 3, 6, 9)] == [6, 12, 24, 168, 168]


def test_due_order_puts_urgent_first_and_interleaves_hosts():
    old = lambda h: {"fetch": {"checked_at": P.to_iso(START - timedelta(hours=h))},  # noqa: E731
                     "extracted_sha": "x", "fingerprint": "f"}
    items = [(_src(old(30), url="https://a.example/1", sid=1), ["refetch"]),
             (_src(old(40), url="https://a.example/2", sid=2), ["refetch"]),
             (_src(old(25), url="https://b.example/1", sid=3), ["refetch"]),
             (_src({}, url="https://a.example/3", sid=4), ["never-fetched"])]
    order = [s.id for s, _ in P.order_due(items, priority={})]
    assert order[0] == 4                       # never fetched first
    assert order[1:] == [3, 2, 1] or order[1:] == [2, 3, 1]
    hosts = [P.host_of(s.url) for s, _ in P.order_due(items, priority={})]
    assert hosts[:2] != ["a.example", "a.example"]


def test_runtime_estimator_math_and_conservatism():
    est = P.RuntimeEstimator({"energy": {"rated_field": "cap", "measured_field": "meas", "rated_factor": 0.842,
                                         "rated_interval": [0.75, 0.925]},
                              "continuous_field": "cont", "peak_field": "peak", "round_hours": 2})
    presets = [{"id": "fridge", "power_w": 150, "peak_w": 1200, "duty_cycle": 0.33, "energy_per_day_wh": None},
               {"id": "router", "power_w": 20, "peak_w": None, "duty_cycle": None, "energy_per_day_wh": None},
               {"id": "freezer", "power_w": 100, "peak_w": None, "duty_cycle": None, "energy_per_day_wh": 600},
               {"id": "pump", "power_w": 2200, "peak_w": 4500, "duty_cycle": None, "energy_per_day_wh": None}]
    vals = {"cap": cg.FieldValue(1070.0, "Wh"), "cont": cg.FieldValue(1500.0, "W"), "peak": cg.FieldValue(3000.0, "W")}
    rows = {r["preset_id"]: r for r in est.rows(1, vals, presets)}
    assert set(rows) == {"fridge", "router", "freezer"}          # the 2200 W pump exceeds 1500 W continuous
    f = rows["fridge"]
    assert f["basis"] == "rated-x-0.842" and f["usable_energy"] == pytest.approx(900.9, abs=0.05)
    assert f["hours_est"] == pytest.approx(1070 * 0.842 / (150 * 0.33), abs=0.01)
    assert f["hours_low"] == pytest.approx(1070 * 0.75 / 49.5, abs=0.01)
    assert f["hours_high"] == pytest.approx(1070 * 0.925 / 49.5, abs=0.01)
    assert f["peak_ok"] is True and rows["router"]["peak_ok"] is None
    assert rows["freezer"]["hours_est"] == pytest.approx(900.94 / 25.0, abs=0.01)
    # a measured figure wins; kWh converts
    vals["meas"] = cg.FieldValue(0.95, "kWh")
    r = {x["preset_id"]: x for x in est.rows(1, vals, presets)}["router"]
    assert r["basis"] == "measured" and r["usable_energy"] == 950.0 and r["hours_est"] == 47.5
    # unknown continuous output: no claim at all
    assert est.rows(1, {"cap": cg.FieldValue(1070.0, "Wh")}, presets) == []
    # unknown peak: peak_ok unknown, not True
    del vals["peak"]
    assert {x["preset_id"]: x for x in est.rows(1, vals, presets)}["fridge"]["peak_ok"] is None


def test_candidates_round_trip_with_rank():
    c = ss.Candidate(field="capacity_wh", value_num=1070.0, value_text=None, unit="Wh",
                     source=ss.SourceRef(JACKERY_PAGE, "manufacturer-page", "jackery.com", "t", "sha"),
                     extractor_id="set:x/y", confidence=0.9, snippet="Capacity: 1070Wh", rank=(3, 1),
                     components=())
    back = P.cand_from_dict(json.loads(json.dumps(P.cand_to_dict(c))))
    assert back == c


def test_operator_candidates_convert_units_and_booleans():
    fields = ss.FieldRegistry({"rated_w": {"kind": "quantity", "unit": "W"},
                               "flag": {"kind": "bool", "values": {"true": ["yes"], "false": ["no"]}}})
    src = ST.SourceRow(id=9, model_id=1, kind="manual-entry", url="https://maker.example/x", publisher="maker")
    c = P.operator_candidate({"id": 5, "field": "rated_w", "value_num": 0.4, "unit": "kW"}, src, fields)
    assert c.value_num == pytest.approx(400.0) and c.unit == "W" and c.extractor == ss.EXTRACTOR_MANUAL
    assert c.extractor_id == "manual-entry:5" and c.source.kind == "manual-entry"
    b = P.operator_candidate({"id": 6, "field": "flag", "value_text": "true"}, None, fields)
    assert (b.value_num, b.value_text) == (1.0, "true") and b.source.kind == "manual-entry"
    assert P.operator_candidate({"id": 7, "field": "unknown"}, None, fields) is None


def test_engine_rows_drop_amazon_sources_and_route_operator_status():
    fields = ss.FieldRegistry({"capacity_wh": {"kind": "quantity", "unit": "Wh", "required": True}})
    ok = ss.Candidate("capacity_wh", 1000.0, None, "Wh", ss.SourceRef("https://maker.example/p", "manufacturer-page"),
                      extractor_id="set:a/b")
    amazon = ss.Candidate("capacity_wh", 1000.0, None, "Wh", ss.SourceRef(AMAZON_URL, "manufacturer-page"),
                          extractor_id="set:a/b")
    spec = ss.consolidate("1", [ok, amazon], fields)
    rows, op, dropped = P.engine_value_rows(spec, ST.ModelRow(1, "station"), {"https://maker.example/p": 11})
    assert [r["source_id"] for r in rows] == [11] and dropped == 1 and op == {}
    # an operator row citing the SAME document the extractor read still heads that source
    src = ST.SourceRow(id=11, model_id=1, kind="manufacturer-page", url="https://maker.example/p")
    manual = P.operator_candidate({"id": 44, "field": "capacity_wh", "value_num": 990.0, "unit": "Wh",
                                   "confidence": 0.9}, src, fields)
    assert manual.rank == (-1, 0)
    spec = ss.consolidate("1", [ok, manual], fields)
    rows, op, _ = P.engine_value_rows(spec, ST.ModelRow(1, "station"), {"https://maker.example/p": 11})
    assert op == {"44": "accepted"} and [r["status"] for r in rows] == ["superseded"]


# ---------------------------------------------------------------------------
# End to end against the throwaway Postgres
# ---------------------------------------------------------------------------

def test_first_run_writes_provenance_typed_rows_compat_and_runtime(h):
    res = h.run()
    assert res.status == "success", res.summary
    m = res.metrics
    # the Amazon listing URL is refused before any request; the retired model is out of scope
    assert AMAZON_URL not in h.http.urls() and "https://www.retired.example/station" not in h.http.urls()
    assert m["sources_refused"] == 1 and m["amazon_sourced_values"] == 0
    assert m["sources_attempted"] == 7 and m["sources_extracted"] == 6
    # Jackery: the PV-input conflict (12-60 V page vs 16-60 V FAQ) is stored, the rest accepted
    v = h.values(1)
    assert v["capacity_wh"] == 1070.0 and v["pv_voc_max"] == 60.0 and v["model_number"] == "JE-1000D"
    assert "pv_v_min" not in v
    conflict = h.q("SELECT value_num, source_id, conflict_id FROM spec_values WHERE model_id = 1 "
                   "AND field = 'pv_v_min' AND status = 'conflict' ORDER BY value_num")
    assert [(float(r["value_num"]), r["source_id"]) for r in conflict] == [(12.0, 101), (16.0, 102)]
    assert conflict[0]["conflict_id"] and conflict[0]["conflict_id"] == conflict[1]["conflict_id"]
    # provenance: every engine row names its source row, extractor and a snippet
    rows = h.q("SELECT * FROM spec_values WHERE model_id = 1 AND extractor <> 'manual'")
    assert rows and all(r["source_id"] in (101, 102) and r["extractor_id"] and r["snippet"] for r in rows)
    cap = h.q("SELECT snippet, extractor FROM spec_values WHERE model_id = 1 AND field = 'capacity_wh' "
              "AND status = 'accepted'")[0]
    assert cap["extractor"] == "deterministic" and "1070" in cap["snippet"].replace(",", "")
    # Pecron came through the PDF path (stand-in bytes, injected converter)
    assert h.values(2)["pv_voc_max"] == 95.0 and h.values(2)["capacity_wh"] == 1920.0
    # FOSSiBOT: UPS 10 ms (page) vs about 8 ms (manual) is a conflict
    assert {r["field"] for r in h.q("SELECT field FROM spec_values WHERE model_id = 3 AND status = 'conflict'")} \
        >= {"ups_ms"}
    # typed projection and model status
    st = h.q("SELECT * FROM station_specs ORDER BY model_id")
    jack = next(r for r in st if r["model_id"] == 1)
    assert float(jack["capacity_wh"]) == 1070.0 and jack["pv_v_min"] is None and jack["ups_supported"] is True
    assert float(jack["spec_completeness"]) == 1.0 and jack["validated_at"] is not None
    assert len(jack["dimensions_mm"]) == 3
    status = {r["id"]: r["spec_status"] for r in h.q("SELECT id, spec_status FROM models")}
    assert status[1] == status[2] == status[3] == "complete"
    assert status[4] == "partial"            # capacity only in prose: no deterministic hit
    assert status[5] == "missing" and status[6] is None
    assert status[12] == status[13] == status[21] == "complete"
    panel13 = h.q("SELECT rated_w FROM panel_specs WHERE model_id = 13")[0]
    assert float(panel13["rated_w"]) == 400.0  # 0.4 kW operator entry, normalised to W
    # operator rows are untouched (never deleted, still accepted)
    assert len(h.q("SELECT 1 FROM spec_values WHERE extractor = 'manual' AND extractor_id IS NULL")) == 20
    # conflicts are queued for adjudication (model route)
    items = h.queue()["items"]
    assert {(i["conflict"]["model_key"], i["conflict"]["field"]) for i in items.values()} >= {("1", "pv_v_min"),
                                                                                          ("3", "ups_ms")}
    assert all(i["status"] == "pending" for i in items.values())
    assert m["open_spec_conflicts"] == len(h.q("SELECT DISTINCT model_id, field FROM spec_values "
                                               "WHERE status = 'conflict'"))
    # completeness goal: 3 of the 5 live station products
    assert m["primary_spec_complete_pct"] == 60.0 and m["primary_products_total"] == 5
    assert (m["catalog_models_complete"], m["catalog_models_partial"], m["catalog_models_missing"]) == (6, 1, 1)
    assert "catalog_models_conflict" not in m      # the open conflicts are on optional fields
    # compat: the adapter fit record is stored (F17's fit_records path). The stored edges are
    # exactly what compat_graph computes from the stored accepted values, and the run reports the
    # primitive's own tallies (not pinned here: compat_graph owns its verdicts). No solar pair FITS
    # yet: Jackery's pv_v_min is in conflict, so it is not accepted, so it is missing to the graph.
    edges = {(r["a_model_id"], r["b_model_id"], r["relation"]): r
             for r in h.q("SELECT * FROM compat_edges")}
    assert (1, 21, "adapter_for") in edges and edges[(1, 21, "adapter_for")]["verdict"] == "fits"
    assert edges[(1, 21, "adapter_for")]["confidence"] == "manufacturer-listed"
    direct = direct_graph_build(h)
    assert {k: r["verdict"] for k, r in edges.items()} == {
        (int(e.a_model_id), int(e.b_model_id), e.relation): e.verdict for e in direct.stored()}
    solar = {k: r["verdict"] for k, r in edges.items() if k[2] == "charges_from"}
    assert solar and not any(v in cg.FIT_VERDICTS for v in solar.values())
    dm = direct.metrics()
    assert m["compat_mode_full"] == 1.0
    for key in ("compat_pairs_evaluated", "compat_edges_stored", "compat_edges_unknown", "compat_edges_fit"):
        assert m[key] == dm[key], key
    # runtime: complete stations only, loads their continuous output covers
    rt = {(r["model_id"], r["preset_id"]): r for r in h.q("SELECT * FROM runtime_estimates")}
    assert {k[0] for k in rt} == {1, 2, 3}
    assert (1, "well-pump") not in rt and (3, "well-pump") in rt and rt[(3, "well-pump")]["surge_ok"] is True
    fr = rt[(1, "fridge")]
    assert fr["basis"] == "rated-x-0.842" and float(fr["hours_est"]) == pytest.approx(1070 * 0.842 / 49.5, abs=0.01)
    assert m["runtime_models_live"] == 3
    assert LLM_ROUTE_CALLS == []


def test_second_run_short_circuits_and_writes_nothing(h):
    h.run()
    before = h.q("SELECT id, status FROM spec_values ORDER BY id")
    edges = h.q("SELECT id, computed_at FROM compat_edges ORDER BY id")
    calls = len(h.http.calls)
    h.clock.advance(1)
    res = h.run()
    assert res.status == "success" and res.short_circuited is True
    assert len(h.http.calls) == calls                     # nothing due: no request at all
    assert res.metrics.get("models_consolidated", 0) == 0 and res.metrics["models_unchanged"] == 8
    assert res.metrics.get("models_written", 0) == 0 and res.metrics["compat_skipped"] == 1
    assert h.q("SELECT id, status FROM spec_values ORDER BY id") == before
    assert h.q("SELECT id, computed_at FROM compat_edges ORDER BY id") == edges
    assert "short-circuited" in res.summary


def test_refetch_uses_conditional_requests_and_skips_unchanged_content(h):
    h.run()
    before = h.q("SELECT id FROM spec_values ORDER BY id")
    h.clock.advance(21)
    res = h.run()
    assert res.metrics["sources_attempted"] == 6          # the refused URL is never retried
    sent = [hd for u, hd in h.http.calls if u in (JACKERY_PAGE, JACKERY_FAQ, PECRON_PDF)]
    assert sum(1 for hd in sent if hd.get("If-None-Match")) >= 3
    assert res.metrics.get("sources_extracted", 0) == 0 and res.metrics["sources_extraction_skipped"] == 6
    assert res.metrics.get("models_written", 0) == 0
    assert h.q("SELECT id FROM spec_values ORDER BY id") == before


def test_operator_decision_settles_the_conflict_and_unlocks_compat_edges(h):
    h.run()
    st = h.settings()
    queue = ss.AdjudicationQueue(ss.StorageQueueStore(h.storage, f"agents/{AGENT_ID}/spec-adjudication/queue.json"))
    item = next(i for i in h.queue()["items"].values() if i["conflict"]["field"] == "pv_v_min")
    choice = next(k for k, c in enumerate(item["conflict"]["candidates"]) if c["value_num"] == 16.0)
    queue.add_operator_decision(item["conflict"]["conflict_id"], choice, "the FAQ states the solar window")
    h.clock.advance(1)
    res = h.run()
    assert res.status == "success", res.summary
    assert h.values(1)["pv_v_min"] == 16.0
    row = h.q("SELECT extractor, extractor_id FROM spec_values WHERE model_id = 1 AND field = 'pv_v_min' "
              "AND status = 'accepted'")[0]
    assert row["extractor"] == "manual" and row["extractor_id"].startswith("manual:")   # an engine row
    assert float(h.q("SELECT pv_v_min FROM station_specs WHERE model_id = 1")[0]["pv_v_min"]) == 16.0
    assert res.metrics["compat_mode_incremental"] == 1.0
    edges = {(r["a_model_id"], r["b_model_id"], r["relation"]): r for r in h.q("SELECT * FROM compat_edges")}
    fit = edges[(1, 12, "charges_from")]
    assert fit["verdict"] in cg.FIT_VERDICTS and fit["confidence"] == "computed"
    assert fit["config"]["design_temp_c"] == -40.0 and fit["config"]["assumptions"]
    assert edges[(1, 13, "charges_from")]["verdict"] == "over_limit"   # 49.5 V x 1.1755 > 95% of 60 V
    # the stored edges are exactly what compat_graph computes from the stored accepted values
    graph = cg.CompatGraph(cg.resolve_config(site_cfg={"profile": "home-power"}, file_cfg=compat_file_cfg()))
    nodes = []
    for mid, kind in ((1, "station"), (12, "panel"), (13, "panel"), (21, "adapter")):
        rows = h.q("SELECT v.*, s.url AS source_url, s.kind AS source_kind FROM spec_values v "
                   "LEFT JOIN spec_sources s ON s.id = v.source_id WHERE v.model_id = %s", (mid,))
        for r in rows:
            r["value_num"] = None if r["value_num"] is None else float(r["value_num"])
        nodes.append(cg.ModelSpec.from_db_rows(mid, kind, rows))
    direct = {e.key[:3]: e.verdict for e in graph.build(nodes, [cg.FitRecord(a=1, b=21, relation="adapter_for",
              source_url="https://panels.example/mc4-to-dc8020", source_kind="manufacturer-page")]).edges if e.store}
    assert direct[("1", "12", "charges_from")] == fit["verdict"]
    # the next run re-applies the decision without a write
    h.clock.advance(1)
    again = h.run()
    assert again.short_circuited is True and h.values(1)["pv_v_min"] == 16.0


def test_failed_and_gone_sources_never_blank_values_and_back_off(h):
    h.run()
    before = h.values(1)
    h.http.set(JACKERY_PAGE, b"", status=503)
    h.http.set(JACKERY_FAQ, b"", status=404)
    h.clock.advance(21)
    res = h.run()
    assert res.metrics["sources_gone"] == 1 and res.metrics["sources_failed"] == 1
    assert h.values(1) == before                      # last good values kept
    state = {r["id"]: r["fetch_state"] for r in h.q("SELECT id, fetch_state FROM spec_sources WHERE id IN (101, 102)")}
    assert state[101]["status"] == "failed" and state[102]["status"] == "gone"
    assert state[101]["next_attempt_at"] and state[101]["candidates"]
    assert res.metrics["sources_unhealthy"] == 3      # + the refused Amazon row
    # in backoff: the next tick does not hit the failing host again
    h.http.calls.clear()
    h.clock.advance(1)
    h.run()
    assert JACKERY_PAGE not in h.http.urls() and JACKERY_FAQ not in h.http.urls()


def test_changed_content_is_re_extracted_and_conflicts_retired(h):
    h.run()
    faq = (SSFX / SOURCES["jackery-faq"]["file"]).read_text().replace("16–60V", "12–60V").replace("16-60V", "12-60V")
    assert "12" in faq
    h.http.set(JACKERY_FAQ, faq.encode(), etag='"jackery-faq-v2"')
    h.clock.advance(21)
    res = h.run()
    assert res.metrics["sources_changed"] == 1 and res.metrics["sources_extracted"] == 1
    assert h.values(1).get("pv_v_min") == 12.0         # both sources now agree
    assert not h.q("SELECT 1 FROM spec_values WHERE model_id = 1 AND status = 'conflict'")
    items = [i for i in h.queue()["items"].values() if i["conflict"]["field"] == "pv_v_min"]
    assert items and all(i["status"] == "stale" for i in items)
    assert res.metrics["conflicts_retired"] >= 1


def test_config_change_re_extracts_from_the_fetch_cache(h):
    h.run()
    calls = len(h.http.calls)
    cfg = h.spec_cfg["profiles"]["home-power"]
    cfg["extractor_sets"]["station-spec-table"][0]["fields"]["fan_noise_db"]["labels"].append("^sound level$")
    h.clock.advance(1)
    res = h.run()
    assert res.metrics["sources_due"] == 6 and res.metrics["sources_extracted"] == 6
    assert len(h.http.calls) == calls                 # re-parsed from the cache, no request
    assert res.metrics.get("models_written", 0) == 0  # same values: nothing rewritten


def test_llm_fallback_fills_a_prose_only_field_with_a_verbatim_quote(h):
    def chat(messages, *, model="", max_tokens=0):
        h.chat_calls.append(messages)
        payload = json.loads(messages[1]["content"].split("\n\n", 1)[1])
        out = []
        for it in payload["items"]:
            vals = {}
            names = [f["name"] for f in it["fields"]]
            if "capacity_wh" in names and "2,048 Wh" in it["excerpt"]:
                vals["capacity_wh"] = {"value": "2048", "unit": "Wh", "quote": "rated at 2,048 Wh"}
            if "cycle_life" in names:
                vals["cycle_life"] = {"value": "6000", "unit": "", "quote": "lasts 6000 cycles"}  # invented
            out.append({"id": it["id"], "values": vals})
        return json.dumps({"items": out})

    h.chat = chat
    h.block["spec_sources"]["llm"] = {"enabled": True}
    res = h.run()
    assert res.status == "success", res.summary
    assert len(h.chat_calls) == 1 and res.metrics["llm_calls"] == 1      # one batch for every item
    assert res.metrics["llm_items"] >= 2 and res.metrics["llm_rejected"] >= 1
    row = h.q("SELECT value_num, extractor, extractor_id, snippet FROM spec_values WHERE model_id = 4 "
              "AND field = 'capacity_wh' AND status = 'accepted'")[0]
    assert float(row["value_num"]) == 2048.0 and row["extractor"] == "haiku"
    assert row["extractor_id"].startswith("llm:") and "2,048 Wh" in row["snippet"]
    assert h.q("SELECT spec_status FROM models WHERE id = 4")[0]["spec_status"] == "complete"
    assert not h.q("SELECT 1 FROM spec_values WHERE field = 'cycle_life' AND extractor = 'haiku'")
    assert LLM_ROUTE_CALLS == []
    # the answer is kept with the source: the next run asks nothing
    h.clock.advance(1)
    again = h.run()
    assert len(h.chat_calls) == 1 and again.short_circuited is True


def test_llm_pool_reserve_defers_and_retries_next_run(h):
    calls = []

    def chat(messages, **_k):
        calls.append(1)
        payload = json.loads(messages[1]["content"].split("\n\n", 1)[1])
        return json.dumps({"items": [{"id": it["id"], "values": {}} for it in payload["items"]]})

    h.chat = chat
    h.admit = lambda _m: False
    h.block["spec_sources"]["llm"] = {"enabled": True}
    res = h.run()
    assert calls == [] and res.metrics["llm_deferred"] >= 1
    h.admit = lambda _m: True
    h.clock.advance(1)
    res = h.run()
    assert calls and res.metrics["llm_calls"] >= 1


def test_operator_rows_are_never_deleted_and_get_status_updates(h):
    h.q("INSERT INTO spec_values (model_id, field, value_num, unit, source_id, extractor, extractor_id, "
        "confidence, status) VALUES (1, 'capacity_wh', 99999, 'Wh', 102, 'manual', 'manual-entry:typo', 1, 'accepted')")
    res = h.run()
    assert res.status == "success"
    row = h.q("SELECT status FROM spec_values WHERE extractor_id = 'manual-entry:typo'")[0]
    assert row["status"] == "rejected"                 # out of the field's range: flagged, not deleted
    assert h.values(1)["capacity_wh"] == 1070.0
    h.q("UPDATE spec_values SET value_num = 1075 WHERE extractor_id = 'manual-entry:typo'")
    h.clock.advance(1)
    h.run()
    assert h.values(1)["capacity_wh"] == 1075.0        # a valid operator value wins consolidation
    assert h.q("SELECT status FROM spec_values WHERE extractor_id = 'manual-entry:typo'")[0]["status"] == "accepted"


def test_removed_model_edges_are_deleted_and_manual_edges_protected(h):
    h.run()
    h.q("INSERT INTO compat_edges (a_model_id, b_model_id, relation, verdict, rule_id, rule_version, confidence) "
        "VALUES (2, 12, 'charges_from', 'fits', 'solar-charging', 'operator', 'manual')")
    h.q("UPDATE models SET lifecycle = 'retired' WHERE id = 21")
    h.clock.advance(1)
    res = h.run()
    assert res.status == "success", res.summary
    rel = {(r["a_model_id"], r["b_model_id"], r["relation"]) for r in h.q("SELECT * FROM compat_edges")}
    assert (1, 21, "adapter_for") not in rel          # the adapter left the catalog
    assert (2, 12, "charges_from") in rel             # an operator's row is never touched


def test_conflicts_of_a_model_that_left_the_catalog_are_retired(h):
    h.run()
    assert any(i["conflict"]["model_key"] == "3" and i["status"] == "pending" for i in h.queue()["items"].values())
    h.q("UPDATE models SET lifecycle = 'retired' WHERE id = 3")
    h.clock.advance(1)
    res = h.run()
    items = [i for i in h.queue()["items"].values() if i["conflict"]["model_key"] == "3"]
    assert items and all(i["status"] == "stale" for i in items) and res.metrics["conflicts_retired"] == len(items)
    assert any(i["status"] == "pending" for i in h.queue()["items"].values())   # others untouched


def test_rule_version_bump_rebuilds_every_edge(h):
    h.run()
    h.compat_cfg["profiles"]["home-power"]["rulesets"]["adapter-fit"]["version"] = 2
    h.clock.advance(1)
    res = h.run()
    assert res.metrics["compat_mode_full"] == 1.0
    versions = {r["rule_version"] for r in h.q("SELECT rule_version FROM compat_edges WHERE rule_id = 'adapter-fit'")}
    assert versions and all(v.startswith("adapter-fit@2[") for v in versions)


def test_one_failing_model_write_does_not_block_the_rest(h):
    # a typed column whose type cannot take the field's value: every station write fails
    h.q("ALTER TABLE station_specs ALTER COLUMN chemistry TYPE numeric USING NULL")
    res = h.run()
    assert res.status == "failure" and "write failed" in res.error_text
    assert res.metrics["models_write_failed"] == 4            # every station with a chemistry value
    assert h.q("SELECT spec_status FROM models WHERE id = 12")[0]["spec_status"] == "complete"   # panels written
    assert not h.q("SELECT 1 FROM spec_values WHERE model_id = 1 AND extractor = 'deterministic'")  # rolled back
    h.q("ALTER TABLE station_specs ALTER COLUMN chemistry TYPE text")
    h.clock.advance(1)
    res = h.run()
    assert res.status == "success", res.summary
    assert res.metrics["models_consolidated"] == 4 and h.values(1)["capacity_wh"] == 1070.0


def test_a_held_lock_skips_the_run(h):
    import psycopg2
    other = psycopg2.connect(h.dsn)
    with other.cursor() as cur:
        cur.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (f"spec-extractor:{AGENT_ID}",))
    try:
        res = h.run()
        assert res.status == "success" and res.metrics == {"skipped_locked": 1}
        assert h.http.calls == []
    finally:
        other.close()


def test_goal_progress_is_recorded_from_run_metrics(h):
    from framework.core import goals
    doc = json.loads((AGENT_DIR / "goals.json").read_text())
    goals.init_goals(AGENT_ID, doc["goals"], storage=h.storage)
    res = h.run()
    active = {g["id"]: g for g in goals.read_active_goals(AGENT_ID, storage=h.storage)}
    for g in doc["goals"]:
        hist = active[g["id"]].get("progress_history") or []
        assert hist and hist[-1]["value"] == pytest.approx(float(res.metrics[g["target_metric"]]))
    keys = [k for k in h.storage.list_prefix(f"agents/{AGENT_ID}/runs/") if k.endswith("/progress.json")]
    progress = h.storage.read_json(keys[0])
    assert progress["status"] == "success" and progress["metrics"]["primary_spec_complete_pct"] == 60.0


def test_dry_run_writes_nothing(h, monkeypatch):
    import psycopg2
    settings = h.settings()
    conn = psycopg2.connect(h.dsn)
    try:
        store = ST.SpecStore(conn, settings)
        rep = P.SpecExtractorRun(settings, store, storage=h.storage, collab=h.collab(), prior_state={},
                                 say=lambda *a, **k: None, decide=lambda *a, **k: None, dry_run=True,
                                 only_models=["1"]).run()
    finally:
        conn.close()
    assert rep.status == "success" and "[dry-run] model 1" in rep.summary_md
    assert h.q("SELECT count(*) AS n FROM spec_values WHERE extractor <> 'manual'")[0]["n"] == 0
    assert h.q("SELECT count(*) AS n FROM compat_edges")[0]["n"] == 0
    assert h.q("SELECT fetch_state FROM spec_sources WHERE id = 101")[0]["fetch_state"] == {}
    assert h.queue() == {}


# ---------------------------------------------------------------------------
# Review round 1 regressions
# ---------------------------------------------------------------------------

def _capacity_chat(h, asked: list):
    def chat(messages, *, model="", max_tokens=0):
        h.chat_calls.append(messages)
        payload = json.loads(messages[1]["content"].split("\n\n", 1)[1])
        out = []
        for it in payload["items"]:
            names = [f["name"] for f in it["fields"]]
            asked.append(names)
            vals = {}
            if "capacity_wh" in names and "2,048 Wh" in it["excerpt"]:
                vals["capacity_wh"] = {"value": "2048", "unit": "Wh", "quote": "rated at 2,048 Wh"}
            out.append({"id": it["id"], "values": vals})
        return json.dumps({"items": out})
    return chat


def _voltaro_capacity(h) -> list:
    return h.q("SELECT value_num, extractor FROM spec_values WHERE model_id = 4 AND field = 'capacity_wh' "
               "AND status = 'accepted'")


def test_an_llm_config_change_asks_again_and_never_drops_answers(h):
    asked: list = []
    h.chat = _capacity_chat(h, asked)
    h.block["spec_sources"]["llm"] = {"enabled": True}
    assert h.run().status == "success"
    assert [(float(r["value_num"]), r["extractor"]) for r in _voltaro_capacity(h)] == [(2048.0, "haiku")]
    asked.clear()
    h.block["spec_sources"]["llm"]["model"] = "another-cheap-model"      # an LLM_SIG_KEYS change
    h.clock.advance(1)
    res = h.run()
    assert res.status == "success", res.summary
    # every field the earlier answers covered is asked again, and the answer replaces the old one
    assert any("capacity_wh" in names for names in asked)
    assert [(float(r["value_num"]), r["extractor"]) for r in _voltaro_capacity(h)] == [(2048.0, "haiku")]
    assert h.q("SELECT spec_status FROM models WHERE id = 4")[0]["spec_status"] == "complete"
    assert h.q("SELECT count(*) AS n FROM runtime_estimates WHERE model_id = 4")[0]["n"] > 0
    calls = len(h.chat_calls)
    h.clock.advance(1)
    assert h.run().short_circuited is True and len(h.chat_calls) == calls


def test_a_re_extraction_of_unchanged_content_keeps_llm_answers_through_a_deferral(h):
    asked: list = []
    h.chat = _capacity_chat(h, asked)
    h.block["spec_sources"]["llm"] = {"enabled": True}
    h.run()
    # a new extraction config (fingerprint) while the pool reserve defers every LLM call
    cfg = h.spec_cfg["profiles"]["home-power"]
    cfg["extractor_sets"]["station-spec-table"][0]["fields"]["fan_noise_db"]["labels"].append("^sound level$")
    h.admit = lambda _m: False
    h.clock.advance(1)
    res = h.run()
    assert res.status == "success", res.summary
    assert res.metrics["sources_extracted"] == 6 and res.metrics["llm_deferred"] >= 1
    assert res.metrics["llm_candidates_kept"] >= 1
    # nothing blanked: the deferred pass leaves the earlier answer in place
    assert [(float(r["value_num"]), r["extractor"]) for r in _voltaro_capacity(h)] == [(2048.0, "haiku")]
    assert h.q("SELECT spec_status FROM models WHERE id = 4")[0]["spec_status"] == "complete"
    assert res.metrics.get("runtime_rows_deleted", 0) == 0
    # the pool frees up: the pass runs and confirms the answer
    h.admit = lambda _m: True
    asked.clear()
    h.clock.advance(1)
    res = h.run()
    assert res.metrics["llm_calls"] >= 1 and any("capacity_wh" in names for names in asked)
    assert [(float(r["value_num"]), r["extractor"]) for r in _voltaro_capacity(h)] == [(2048.0, "haiku")]


def test_runtime_rows_of_models_that_left_the_scope_are_removed(h):
    h.run()
    assert {r["model_id"] for r in h.q("SELECT DISTINCT model_id FROM runtime_estimates")} == {1, 2, 3}
    h.q("UPDATE models SET lifecycle = 'retired' WHERE id = 3")
    h.clock.advance(1)
    res = h.run()
    assert res.status == "success", res.summary
    assert {r["model_id"] for r in h.q("SELECT DISTINCT model_id FROM runtime_estimates")} == {1, 2}
    assert res.metrics["runtime_models_live"] == 2 and res.metrics["runtime_models_removed"] == 1


def test_a_kind_change_removes_the_old_typed_row(h):
    h.run()
    assert h.q("SELECT 1 FROM station_specs WHERE model_id = 5")
    h.q("UPDATE models SET kind = 'panel' WHERE id = 5")
    h.clock.advance(1)
    res = h.run()
    assert res.status == "success", res.summary
    assert not h.q("SELECT 1 FROM station_specs WHERE model_id = 5")
    assert h.q("SELECT 1 FROM panel_specs WHERE model_id = 5") and res.metrics["typed_deletes"] == 1


def test_disabling_a_ruleset_purges_its_computed_edges(h):
    h.run()
    h.q("INSERT INTO compat_edges (a_model_id, b_model_id, relation, verdict, rule_id, rule_version, confidence) "
        "VALUES (2, 12, 'charges_from', 'fits', 'solar-charging', 'operator', 'manual'), "
        "(3, 12, 'charges_from', 'fits', 'legacy-rule', 'v1', 'computed')")
    assert h.q("SELECT 1 FROM compat_edges WHERE rule_id = 'solar-charging' AND confidence = 'computed'")
    h.compat_cfg["profiles"]["home-power"]["rulesets"]["solar-charging"]["enabled"] = False
    h.clock.advance(1)
    res = h.run()
    assert res.status == "success", res.summary
    assert res.metrics["compat_mode_full"] == 1.0 and res.metrics["compat_purged"] >= 3
    rows = {(r["rule_id"], r["confidence"]) for r in h.q("SELECT rule_id, confidence FROM compat_edges")}
    assert ("solar-charging", "computed") not in rows           # the withdrawn verdicts are gone
    assert ("solar-charging", "manual") in rows                 # an operator's row is never touched
    assert ("legacy-rule", "computed") in rows                  # unknown ruleset: kept by default...
    assert ("adapter-fit", "manufacturer-listed") in rows
    h.block["compat"]["purge_orphaned"] = "all"                 # ...and purged on request
    h.clock.advance(1)
    h.run()
    assert not h.q("SELECT 1 FROM compat_edges WHERE rule_id = 'legacy-rule'")


# ---------------------------------------------------------------------------
# Compat: withdrawn statements, and the manual rows the engine writes itself
# ---------------------------------------------------------------------------

def edges(h, relation: Optional[str] = None) -> dict:
    rows = h.q("SELECT a_model_id, b_model_id, relation, verdict, confidence FROM compat_edges"
               + (" WHERE relation = %s" if relation else ""), (relation,) if relation else None)
    return {(r["a_model_id"], r["b_model_id"], r["relation"]): (r["verdict"], r["confidence"]) for r in rows}


def owned_manual(h) -> set:
    """The engine-written manual rows the agent state records, as key tuples."""
    return {tuple(json.loads(k)) for k in ((h.state().get("compat") or {}).get("owned_manual") or {})}


def test_a_withdrawn_fit_record_loses_its_edge_on_the_next_run(h):
    h.run()
    before = edges(h)
    assert before[(1, 21, "adapter_for")] == ("fits", "manufacturer-listed")
    h.q("DELETE FROM compat_fit_records WHERE a_model_id = 1 AND b_model_id = 21")
    h.clock.advance(1)
    res = h.run()
    assert res.status == "success", res.summary
    # an incremental pass (not the periodic full rebuild days later) deletes exactly that row,
    # although a manufacturer_list-only ruleset no longer evaluates the pair at all
    assert res.metrics["compat_mode_incremental"] == 1.0 and res.metrics["compat_deletes"] == 1.0
    assert edges(h) == {k: v for k, v in before.items() if k != (1, 21, "adapter_for")}
    h.clock.advance(1)
    assert h.run().metrics["compat_skipped"] == 1.0


def test_the_template_fit_list_adds_and_withdraws_expands_edges(h):
    import yaml
    template = yaml.safe_load((AGENT_DIR / "site.example.yaml").read_text())["spec_extractor"]
    h.block["kinds"]["battery"] = copy.deepcopy(template["kinds"]["battery"])     # the shipped fit_lists path
    h.block["compat"]["fit_lists"] = copy.deepcopy(template["compat"]["fit_lists"])
    h.q("INSERT INTO models (id, brand_id, kind, model_name, lifecycle) VALUES "
        "(31, 1, 'battery', 'Explorer 1000 Plus pack', 'live')")
    h.q("INSERT INTO spec_sources (id, model_id, kind, url, publisher) VALUES "
        "(131, 31, 'manufacturer-page', 'https://www.jackery.com/products/battery-pack-x', 'jackery.com')")
    h.q("INSERT INTO spec_values (model_id, field, value_num, value_text, unit, source_id, extractor, confidence, "
        "status) VALUES (31, 'capacity_wh', 1264, NULL, 'Wh', 131, 'manual', 1, 'accepted'), "
        "(31, 'compatible_stations', NULL, 'Explorer 1000 v2', NULL, 131, 'manual', 1, 'accepted')")
    res = h.run()
    assert res.status == "success", res.summary
    assert res.metrics["compat_fit_names_unresolved"] == 0.0
    assert edges(h, "expands") == {(1, 31, "expands"): ("fits", "manufacturer-listed")}
    # the battery's page no longer lists that station: the edge goes on the next run
    h.q("UPDATE spec_values SET value_text = 'Explorer 2000 Pro' WHERE model_id = 31 AND field = 'compatible_stations'")
    h.clock.advance(1)
    res = h.run()
    assert res.status == "success", res.summary
    assert res.metrics["compat_mode_incremental"] == 1.0 and res.metrics["compat_fit_names_unresolved"] == 1.0
    assert edges(h, "expands") == {}
    h.q("UPDATE spec_values SET value_text = 'Explorer 1000 v2' WHERE model_id = 31 AND field = 'compatible_stations'")
    h.clock.advance(1)
    h.run()
    assert edges(h, "expands") == {(1, 31, "expands"): ("fits", "manufacturer-listed")}


def test_an_operator_fit_record_edge_is_withdrawn_with_its_record(h):
    h.q("INSERT INTO compat_fit_records (a_model_id, b_model_id, relation, verdict, origin, statement) VALUES "
        "(2, 21, 'adapter_for', 'fits', 'manual', 'operator: fits the E2000LFP'), "
        "(1, 21, 'adapter_for', 'no_fit', 'manual', 'operator: the v2 needs the other cable')")
    # a manual row an operator wrote straight into compat_edges is never the engine's
    h.q("INSERT INTO compat_edges (a_model_id, b_model_id, relation, verdict, rule_id, rule_version, confidence) "
        "VALUES (3, 21, 'adapter_for', 'fits', 'adapter-fit', 'operator', 'manual')")
    res = h.run()
    assert res.status == "success", res.summary
    e = edges(h, "adapter_for")
    assert e[(2, 21, "adapter_for")] == ("fits", "manual")
    assert e[(1, 21, "adapter_for")] == ("no_fit", "manual")             # the operator outranks the manufacturer
    assert owned_manual(h) == {("1", "21", "adapter_for", "adapter-fit"), ("2", "21", "adapter_for", "adapter-fit")}
    h.q("DELETE FROM compat_fit_records WHERE origin = 'manual'")
    h.clock.advance(1)
    res = h.run()
    assert res.status == "success", res.summary
    assert res.metrics["compat_mode_incremental"] == 1.0 and res.metrics["compat_manual_released"] == 2.0
    assert edges(h, "adapter_for") == {
        (1, 21, "adapter_for"): ("fits", "manufacturer-listed"),     # the manufacturer's record decides again
        (3, 21, "adapter_for"): ("fits", "manual"),                  # the operator's own row: untouched
    }
    assert owned_manual(h) == set()


def test_an_engine_manual_edge_an_operator_edited_is_never_released(h):
    h.q("INSERT INTO compat_fit_records (a_model_id, b_model_id, relation, verdict, origin, statement) VALUES "
        "(2, 21, 'adapter_for', 'fits', 'manual', 'operator: fits')")
    h.run()
    h.q("UPDATE compat_edges SET verdict = 'no_fit' WHERE a_model_id = 2 AND b_model_id = 21")   # by hand
    h.q("DELETE FROM compat_fit_records WHERE a_model_id = 2")
    h.clock.advance(1)
    res = h.run()
    assert res.status == "success", res.summary
    assert res.metrics.get("compat_manual_released", 0) == 0 and res.metrics["compat_protected"] >= 1
    assert edges(h, "adapter_for")[(2, 21, "adapter_for")] == ("no_fit", "manual")    # the operator's now
    assert owned_manual(h) == set()


def test_a_lost_state_re_adopts_the_manual_edges_the_engine_derives(h):
    h.q("INSERT INTO compat_fit_records (a_model_id, b_model_id, relation, verdict, origin, statement) VALUES "
        "(2, 21, 'adapter_for', 'fits', 'manual', 'operator: fits')")
    h.run()
    h.storage.write_json(f"agents/{AGENT_ID}/state/latest.json", {"state": {}})       # the state blob is lost
    h.clock.advance(1)
    res = h.run()
    assert res.metrics["compat_mode_full"] == 1.0 and res.metrics.get("compat_upserts", 0) == 0
    assert owned_manual(h) == {("2", "21", "adapter_for", "adapter-fit")}   # identical row: the engine's again
    h.q("DELETE FROM compat_fit_records WHERE a_model_id = 2")
    h.clock.advance(1)
    h.run()
    assert (2, 21, "adapter_for") not in edges(h)


def test_a_removed_model_and_a_disabled_ruleset_release_the_engines_manual_edges(h):
    h.q("INSERT INTO compat_fit_records (a_model_id, b_model_id, relation, verdict, origin, statement) VALUES "
        "(2, 21, 'adapter_for', 'fits', 'manual', 'operator: fits'), "
        "(3, 21, 'adapter_for', 'fits', 'manual', 'operator: fits')")
    h.q("INSERT INTO compat_edges (a_model_id, b_model_id, relation, verdict, rule_id, rule_version, confidence) "
        "VALUES (2, 12, 'charges_from', 'fits', 'solar-charging', 'operator', 'manual'), "
        "(4, 21, 'adapter_for', 'fits', 'adapter-fit', 'operator', 'manual')")
    h.run()
    h.q("UPDATE models SET lifecycle = 'retired' WHERE id = 2")         # station 2 leaves the catalog
    h.clock.advance(1)
    res = h.run()
    assert res.status == "success", res.summary
    e = edges(h)
    assert (2, 21, "adapter_for") not in e and res.metrics["compat_manual_released"] == 1.0
    assert e[(2, 12, "charges_from")] == ("fits", "manual")             # the operator's row stays
    h.compat_cfg["profiles"]["home-power"]["rulesets"]["adapter-fit"]["enabled"] = False   # the kill switch
    h.clock.advance(1)
    res = h.run()
    assert res.status == "success", res.summary
    assert res.metrics["compat_mode_full"] == 1.0 and res.metrics["compat_manual_released"] == 1.0
    assert edges(h, "adapter_for") == {(4, 21, "adapter_for"): ("fits", "manual")}   # only the operator's row
    assert owned_manual(h) == set()


def test_a_failing_compat_batch_keeps_the_ownership_it_committed(h, monkeypatch):
    h.q("INSERT INTO compat_fit_records (a_model_id, b_model_id, relation, verdict, origin, statement) VALUES "
        "(1, 21, 'adapter_for', 'no_fit', 'manual', 'operator: the v2 needs the other cable')")
    h.block["compat"]["batch_size"] = 1                                  # station 1 is the first batch
    real_apply = P.CompatPhase._apply

    def apply_then_fail(self, plan, releases):
        if getattr(self, "_test_calls", 0) == 1:
            raise RuntimeError("injected write failure")
        self._test_calls = getattr(self, "_test_calls", 0) + 1
        return real_apply(self, plan, releases)

    monkeypatch.setattr(P.CompatPhase, "_apply", apply_then_fail)
    res = h.run()
    assert res.status == "failure" and "injected write failure" in res.error_text
    assert edges(h, "adapter_for")[(1, 21, "adapter_for")] == ("no_fit", "manual")       # committed
    assert owned_manual(h) == {("1", "21", "adapter_for", "adapter-fit")}                 # and remembered
    monkeypatch.setattr(P.CompatPhase, "_apply", real_apply)
    h.q("DELETE FROM compat_fit_records WHERE origin = 'manual'")
    h.clock.advance(1)
    res = h.run()
    assert res.status == "success", res.summary
    assert edges(h, "adapter_for")[(1, 21, "adapter_for")] == ("fits", "manufacturer-listed")


def test_an_exception_in_a_late_phase_keeps_the_agent_state(h):
    h.block["tables"]["products"]["where"] = "p.no_such_column"     # valid config, fails in SQL
    res = h.run()
    assert res.status == "failure" and "UndefinedColumn" in res.summary
    st = h.state()
    assert st.get("model_hashes") and st.get("compat", {}).get("graph_sig")      # never wiped to {}
    h.block["tables"]["products"]["where"] = "p.is_active"
    h.clock.advance(1)
    res = h.run()
    assert res.status == "success", res.summary
    assert res.metrics["models_unchanged"] == 8 and res.metrics["compat_skipped"] == 1


def test_agent_force_run_and_engine_version_bypass_the_in_run_short_circuits(h, monkeypatch):
    h.run()
    monkeypatch.setenv("AGENT_FORCE_RUN", "1")
    h.clock.advance(1)
    res = h.run()
    assert res.status == "success" and res.short_circuited is False
    assert res.metrics["models_consolidated"] == 8 and res.metrics["compat_mode_full"] == 1.0
    assert res.metrics.get("models_written", 0) == 0                 # same inputs, same rows
    monkeypatch.delenv("AGENT_FORCE_RUN")
    h.clock.advance(1)
    assert h.run().short_circuited is True
    monkeypatch.setattr(P, "ENGINE_VERSION", "test-next")          # an engine fix reaches every model
    h.clock.advance(1)
    assert h.run().metrics["models_consolidated"] == 8


def drop_first_connection_once(h):
    """A FakeHttp ``on_call`` hook: at the first document fetch, terminate the
    engine's first DB session and wait until it (and its lock) is gone."""
    import psycopg2
    done = []

    def hook(url):
        if url.endswith("/robots.txt") or not h.conns or done:
            return
        done.append(url)
        pid = h.conns[0].get_backend_pid()
        admin = psycopg2.connect(h.dsn)
        admin.autocommit = True
        try:
            with admin.cursor() as cur:
                cur.execute("SELECT pg_terminate_backend(%s)", (pid,))
                for _ in range(100):           # until the old session (and its lock) is gone
                    cur.execute("SELECT count(*) FROM pg_stat_activity WHERE pid = %s", (pid,))
                    if cur.fetchone()[0] == 0:
                        break
                    time.sleep(0.05)
        finally:
            admin.close()

    return hook


def test_a_dropped_connection_is_replaced_between_phases(h):
    h.http.on_call = drop_first_connection_once(h)
    res = h.run()
    assert res.status == "success", res.summary
    assert res.metrics["db_reconnects"] == 1 and len(h.conns) == 2
    assert h.values(1)["capacity_wh"] == 1070.0


def test_a_primary_kind_needs_required_fields(h):
    h.block["kinds"]["station"]["required"] = []
    res = h.run()
    assert res.status == "failure" and "no required fields" in res.summary
    assert h.http.calls == []


def test_a_kind_without_required_fields_counts_only_models_with_inputs(h):
    h.block["kinds"]["adapter"]["required"] = []
    h.q("INSERT INTO models (id, brand_id, kind, model_name) VALUES (22, 5, 'adapter', 'Unsourced adapter')")
    res = h.run()
    assert res.status == "success", res.summary
    assert "no required fields" in res.summary_md
    # the sourced adapter (21) is complete; the unsourced one (22) is not, although nothing is required
    assert (res.metrics["catalog_models_complete"], res.metrics["catalog_models_missing"]) == (6, 2)


@pytest.mark.parametrize("drop,needle", [
    ("rated_factor", "rated_factor: required with rated_field"),
    ("rated_interval", "rated_interval: required with rated_field"),
])
def test_runtime_niche_values_have_no_engine_default(drop, needle):
    block = site_block()
    del block["runtime"]["energy"][drop]
    with pytest.raises(S.SettingsError) as exc:
        S.build_settings(block, agent_id=AGENT_ID)
    assert needle in str(exc.value)
    with pytest.raises(S.SettingsError, match="purge_orphaned"):
        S.build_settings(site_block(compat={"purge_orphaned": "sometimes"}), agent_id=AGENT_ID)
    assert S.DEFAULTS["runtime"]["energy"]["rated_factor"] is None
    assert S.DEFAULTS["runtime"]["output"]["columns"]["usable_energy"] == "usable_energy"


def test_dsn_env_must_be_a_variable_name_and_is_never_echoed():
    secret = "postgresql://admin:hunter2@db.example:5432/site"
    with pytest.raises(S.SettingsError) as exc:
        S.build_settings(site_block(database={"dsn_env": secret}), agent_id=AGENT_ID)
    assert "NAME of an environment variable" in str(exc.value) and "hunter2" not in str(exc.value)


# ---------------------------------------------------------------------------
# The DSN never reaches a run record, framework storage, a log or the CLI
# ---------------------------------------------------------------------------

PW_TAIL = "Pw0rdLeak7"    # the part of each password below that must never be stored or printed

#: Real libpq texts (see the calibration in the F17 fix notes) for DSNs whose
#: password is mis-encoded: (dsn, driver exception class, its message).
LIBPQ_ECHOES = [
    (f"postgresql://site_rw:Zq9%x!{PW_TAIL}@db.example:5432/site", "ProgrammingError",
     f'invalid dsn: invalid percent-encoded token: "Zq9%x!{PW_TAIL}"'),
    (f"host=db.example user=site_rw password=Ab3 {PW_TAIL} dbname=site", "ProgrammingError",
     f'invalid dsn: missing "=" after "{PW_TAIL}" in connection info string'),
    (f"postgresql://site_rw:Ab3{PW_TAIL}@[db.example:5432/site", "ProgrammingError",
     f'invalid dsn: end of string reached when looking for matching "]" in IPv6 host address in URI: '
     f'"postgresql://site_rw:Ab3{PW_TAIL}@[db.example:5432/site"'),
    (f"postgresql://site_rw:Ab3@{PW_TAIL}@db.example:5432/site", "OperationalError",
     f'could not translate host name "{PW_TAIL}@db.example" to address: Name or service not known\n'),
    (f"postgresql://site_rw:Ab3/{PW_TAIL}@db.example:5432/site", "OperationalError",
     'invalid integer value "Ab3" for connection option "port"\n'),
    (f"postgresql://site_rw:{PW_TAIL}@db.example:5432/site", "OperationalError",
     'connection to server at "db.example" (10.0.0.5), port 5432 failed: FATAL:  password authentication '
     'failed for user "site_rw"\n'),
    (f"postgresql://site_rw:Ab3@{PW_TAIL}@db.example:5432/site", "OperationalError",   # an unquoted echo
     f"driver text that echoes Ab3@{PW_TAIL} without quotes"),
]


def assert_secret_kept_out(res, root: Path, *printed: str) -> None:
    texts = [res.summary or "", res.summary_md or "", res.error_text or "",
             json.dumps(res.metrics, default=str), json.dumps(res.next_state, default=str), *printed]
    assert [t for t in texts if PW_TAIL in t] == []
    leaked = [str(p.relative_to(root)) for p in sorted(root.rglob("*"))
              if p.is_file() and PW_TAIL.encode() in p.read_bytes()]
    assert leaked == [], f"the password reached {leaked}"


@pytest.mark.parametrize("dsn,cls,message", LIBPQ_ECHOES)
def test_connect_error_text_withholds_every_echo_of_the_password(dsn, cls, message):
    psycopg2 = pytest.importorskip("psycopg2")
    text = A.connect_error_text(getattr(psycopg2, cls)(message), dsn_env=DSN_ENV, dsn=dsn)
    assert PW_TAIL not in text
    assert text.startswith(f"cannot connect to the site DB (${DSN_ENV}): {cls}: ")
    if cls == "ProgrammingError":           # a parse error of the DSN keeps no driver text at all
        assert "not a valid connection string or URI" in text and "%25" in text
    else:                                   # the diagnosis survives the redaction
        assert any(s in text for s in ("Name or service not known", "connection option", "password authentication",
                                       "without quotes"))


def test_password_fragments_read_malformed_dsns_leniently():
    assert A.password_fragments(f"postgresql://u:Ab3@{PW_TAIL}@h:1/db") == [f"Ab3@{PW_TAIL}", PW_TAIL]
    assert A.password_fragments(f"host=h password='a {PW_TAIL}' user=u") == [f"a {PW_TAIL}", PW_TAIL]
    assert A.password_fragments(f"postgresql://u:{PW_TAIL}%40x@h/db") == [f"{PW_TAIL}%40x", f"{PW_TAIL}@x",
                                                                         PW_TAIL]
    assert A.password_fragments("postgresql://u@h/db") == [] and A.password_fragments("") == []
    assert A.redact_dsn(f"x {PW_TAIL[2:]} y", f"postgresql://u:{PW_TAIL}@h/db") == "x <redacted> y"


def _loopback_refused_dsn() -> str:
    return f"postgresql://site_rw:{PW_TAIL}@127.0.0.1:{_free_port()}/site"   # a closed local port: refused


@pytest.mark.parametrize("case", ["raw-percent", "keyword-space", "at-sign", "refused"])
def test_a_bad_dsn_never_reaches_the_run_record_storage_or_logs(case, tmp_path, storage, monkeypatch, capsys):
    pytest.importorskip("psycopg2")
    from framework.core import pg_conn
    if case == "raw-percent":            # a generated password with '%', pasted into a URI unencoded
        dsn = f"postgresql://site_rw:Zq9%x!{PW_TAIL}@127.0.0.1:1/site"
    elif case == "keyword-space":
        dsn = f"host=127.0.0.1 port=1 user=site_rw password=Ab3 {PW_TAIL} dbname=site"
    elif case == "at-sign":              # libpq would resolve the broken host name: its message, offline
        dsn = f"postgresql://site_rw:Ab3@{PW_TAIL}@127.0.0.1:1/site"
        message = LIBPQ_ECHOES[3][2].replace("db.example", "127.0.0.1")

        def resolve_fails(_dsn, **_kw):
            import psycopg2
            raise psycopg2.OperationalError(message)

        monkeypatch.setattr(pg_conn, "connect", resolve_fails)
    else:
        dsn = _loopback_refused_dsn()
    ag = A.SpecExtractorAgent(agent_id=AGENT_ID, storage=storage, settings=S.build_settings(site_block(),
                                                                                           agent_id=AGENT_ID),
                              connect=REAL_DEFAULT_CONNECT, environ={DSN_ENV: dsn},
                              collaborators=P.Collaborators(spec_file_cfg=spec_file_cfg(),
                                                            compat_file_cfg=compat_file_cfg()))
    ag.auto_recovery_enabled = False
    res = ag.run_once()
    assert res.status == "failure" and res.summary.startswith(f"cannot connect to the site DB (${DSN_ENV})")
    if case == "refused":
        assert "Connection refused" in res.summary
    out = capsys.readouterr()
    assert_secret_kept_out(res, tmp_path, out.out, out.err)


def test_a_failed_reconnect_between_phases_keeps_the_dsn_out(h, capsys):
    bad_dsn = f"postgresql://site_rw:Zq9%x!{PW_TAIL}@127.0.0.1:1/site"
    calls = []

    def connect(settings, environ=None):
        calls.append(environ)
        if len(calls) == 1:
            return h.connect()
        return REAL_DEFAULT_CONNECT(settings, environ=environ)      # the reconnect reads the bad DSN

    h.http.on_call = drop_first_connection_once(h)
    ag = A.SpecExtractorAgent(agent_id=AGENT_ID, storage=h.storage, settings=h.settings(), connect=connect,
                              collaborators=h.collab(), environ={DSN_ENV: bad_dsn})
    ag.auto_recovery_enabled = False
    res = ag.run_once()
    assert res.status == "failure" and len(calls) == 2
    assert res.summary.startswith("spec-extractor failed: cannot connect to the site DB")
    assert "SiteDBConnectError" in res.error_text and "ProgrammingError(" not in res.error_text
    out = capsys.readouterr()
    assert_secret_kept_out(res, h.tmp, out.out, out.err)


def test_cli_prints_a_redacted_connect_error(tmp_path, monkeypatch, capsys):
    import yaml
    monkeypatch.setattr(A, "default_connect", REAL_DEFAULT_CONNECT)
    monkeypatch.setenv(DSN_ENV, f"postgresql://site_rw:Zq9%x!{PW_TAIL}@127.0.0.1:1/site")
    cfg_file = tmp_path / "spec-sources.json"
    cfg_file.write_text(json.dumps(spec_file_cfg()))
    site_yaml = tmp_path / "site.yaml"
    site_yaml.write_text(yaml.safe_dump({"spec_extractor": site_block()}))
    assert A.main(["--plan", "--site-yaml", str(site_yaml), "--agent-id", AGENT_ID,
                   "--spec-config-file", str(cfg_file)]) == 1
    out = capsys.readouterr()
    assert PW_TAIL not in out.out + out.err and "Traceback" not in out.err
    assert out.err.startswith(f"cannot connect to the site DB (${DSN_ENV}): ProgrammingError")


def test_a_fit_list_of_an_unconfigured_kind_is_warned_about():
    st = S.build_settings(site_block(compat={"fit_lists": [{"field": "compatible_stations", "kinds": ["battery"],
                                                            "relation": "expands"}]}), agent_id=AGENT_ID)
    assert any("fit_lists[0].kinds ['battery'] are not configured" in w for w in st.warnings)
    example = S.load_settings(str(AGENT_DIR / "site.example.yaml"), agent_id=AGENT_ID)
    assert "battery" in example.kinds and not any("fit_lists" in w for w in example.warnings)


def test_default_connect_opens_a_read_only_session(monkeypatch):
    from framework.core import pg_conn

    class FakeConn:
        readonly = None

        def set_session(self, **kw):
            self.readonly = kw.get("readonly")

    monkeypatch.setattr(pg_conn, "connect", lambda dsn, **kw: FakeConn())
    st = S.build_settings(site_block(), agent_id=AGENT_ID)
    assert REAL_DEFAULT_CONNECT(st, environ={DSN_ENV: "dbname=x"}, readonly=True).readonly is True
    assert REAL_DEFAULT_CONNECT(st, environ={DSN_ENV: "dbname=x"}).readonly is None


def test_cli_plan_runs_on_a_read_only_session(h, monkeypatch, capsys, tmp_path):
    import psycopg2
    import yaml
    seen = []

    def connect(settings, *, environ=None, readonly=False):
        seen.append(readonly)
        conn = psycopg2.connect(h.dsn)
        conn.set_session(readonly=readonly)
        return conn

    monkeypatch.setattr(A, "default_connect", connect)
    cfg_file = tmp_path / "spec-sources.json"
    cfg_file.write_text(json.dumps(spec_file_cfg()))
    site_yaml = tmp_path / "site.yaml"
    site_yaml.write_text(yaml.safe_dump({"spec_extractor": site_block()}))
    assert A.main(["--plan", "--site-yaml", str(site_yaml), "--agent-id", AGENT_ID,
                   "--spec-config-file", str(cfg_file)]) == 0
    assert seen == [True]
    out = json.loads(capsys.readouterr().out)
    assert out["due"] == 7 and out["models"] == 8
