"""Shared helpers for the recall-gate tests (no tests in this module).

* Fixture loaders for the real CPSC data in fixtures/recall_gate.
* ``FakeHttp``: serves those fixtures for the SaferProducts API (honouring
  its LastPublishDateStart/End window, like the real API) and the two CPSC CSV
  URLs, plus configurable product pages; records every request.
* A throwaway Postgres: one ``postgres:16`` container per test process
  (started on first use, stopped at exit) on a free 127.0.0.1 port above
  55000, its password passed through a 0600 env-file, never a command line.
  Each test gets its own database. Set RECALL_GATE_TEST_PG_DSN to an admin
  conninfo of an existing throwaway server to skip docker. Without docker or
  the local image, DB tests skip.
"""
from __future__ import annotations

import atexit
import json
import os
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Optional

import pytest

from framework.core import recall_gate as rg

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "recall_gate"
API_FILES = ("saferproducts_api_delta_max.json", "saferproducts_api_seagull_pro.json",
             "saferproducts_api_recalldate_2025-10-08_2025-10-10.json")
RECALL_CSV = "recalls_recall_listing.sample.csv"
WARNING_CSV = "product_safety_warning_listing.sample.csv"
# Real rows cut for the review findings (list naming, "X and X Pro", an
# explicit model-number exclusion, home energy storage recalls).
REVIEW_RECALL_CSV = "recalls_review_cases.sample.csv"
REVIEW_WARNING_CSV = "warnings_review_cases.sample.csv"
# Real rows for elided list naming ("Prowler Pro / Pro Crew", "City X3 and
# X3 Sport", "RANGER 1000, 1000 Crew, XP 1000, ...") and its negatives.
LIST_NAMING_CSV = "recalls_list_naming_cases.sample.csv"

POWER_CATALOG_SQL = """
SELECT p.id AS product_id, m.id AS model_id, b.name AS brand, m.model_name, m.model_number,
       m.family, p.category, p.asin,
       ARRAY(SELECT cm.model_number FROM kit_components kc
               JOIN models cm ON cm.id = kc.component_model_id
              WHERE kc.kit_model_id = m.id AND cm.model_number IS NOT NULL) AS components,
       'https://power.example/product/' || m.slug AS url
  FROM products p
  JOIN models m ON m.id = p.model_id
  JOIN brands b ON b.id = m.brand_id
 WHERE p.is_active
"""


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def api_payload() -> list[dict[str, Any]]:
    """The three API fixtures as one response (deduped by RecallID)."""
    seen: dict[Any, dict[str, Any]] = {}
    for name in API_FILES:
        for rec in json.loads(fixture_bytes(name)):
            seen.setdefault(rec["RecallID"], rec)
    return list(seen.values())


def fixture_records() -> dict[str, rg.Recall]:
    """All fixture records merged the way the agent merges them."""
    return rg.merge_sources({
        rg.SOURCE_WARNING_CSV: rg.SourceResult(
            rg.SOURCE_WARNING_CSV, True, rg.parse_warning_csv(fixture_bytes(WARNING_CSV))),
        rg.SOURCE_RECALL_CSV: rg.SourceResult(
            rg.SOURCE_RECALL_CSV, True, rg.parse_recall_csv(fixture_bytes(RECALL_CSV))),
        rg.SOURCE_API: rg.SourceResult(
            rg.SOURCE_API, True, rg.parse_api_records(json.dumps(api_payload()))),
    })


def review_case_records() -> dict[str, rg.Recall]:
    """The real review-case rows (recalls_review_cases / warnings_review_cases)."""
    return rg.merge_sources({
        rg.SOURCE_WARNING_CSV: rg.SourceResult(
            rg.SOURCE_WARNING_CSV, True, rg.parse_warning_csv(fixture_bytes(REVIEW_WARNING_CSV))),
        rg.SOURCE_RECALL_CSV: rg.SourceResult(
            rg.SOURCE_RECALL_CSV, True, rg.parse_recall_csv(fixture_bytes(REVIEW_RECALL_CSV))),
    })


def list_naming_records() -> dict[str, rg.Recall]:
    """The real list-naming rows (recalls_list_naming_cases)."""
    return {r.id: r for r in rg.parse_recall_csv(fixture_bytes(LIST_NAMING_CSV))}


def catalog_rows(name: str) -> list[dict[str, Any]]:
    return json.loads((FIXTURES / f"catalog_{name}.json").read_text())["rows"]


def repo_config_doc() -> dict[str, Any]:
    return json.loads(rg.REPO_CONFIG_PATH.read_text())


def config_with_site(site: str, profile: str, **site_over: Any) -> dict[str, Any]:
    """The repo config plus one enabled by_site entry, resolved."""
    doc = repo_config_doc()
    entry = {"enabled": True, "profile": profile, "catalog_sql": "SELECT 1",
             "dsn_env": f"RG_TEST_{site.upper().replace('-', '_')}_DSN"}
    entry.update(site_over)
    doc["by_site"] = {site: entry}
    return rg.resolve_config(doc)


def settings_for(site: str, profile: str, **site_over: Any) -> rg.SiteSettings:
    settings, problems = rg.site_settings(config_with_site(site, profile, **site_over), site)
    assert problems == [], problems
    return settings


def match_table(settings: rg.SiteSettings, catalog: list[dict[str, Any]],
                records: dict[str, rg.Recall]) -> set[tuple]:
    """{(recall_id, product_id, kind, rule, tier, status, hold_until)}."""
    items, problems = rg.catalog_from_rows(catalog)
    assert problems == []
    matcher = rg.Matcher(settings, items)
    out = set()
    for rid in sorted(records):
        _, found = matcher.match(rg.RecallView(records[rid]))
        for m in found:
            out.add((rid, m.item.product_id, m.match_kind, m.rule, m.tier, m.status,
                     m.hold_until))
    return out


# ---------------------------------------------------------------------------
# Fake HTTP
# ---------------------------------------------------------------------------

class FakeHttp:
    """Serves the CPSC fixtures. ``fail`` maps a source name to an HTTP status
    to return instead; ``pages`` maps a URL to (status, html)."""

    def __init__(self, cfg: Optional[dict[str, Any]] = None):
        cfg = cfg or rg.DEFAULTS
        src = cfg["sources"]
        self.urls = {rg.SOURCE_API: src[rg.SOURCE_API]["url"],
                     rg.SOURCE_RECALL_CSV: src[rg.SOURCE_RECALL_CSV]["url"],
                     rg.SOURCE_WARNING_CSV: src[rg.SOURCE_WARNING_CSV]["url"]}
        self.fail: dict[str, int] = {}
        # url -> (status, html) or (status, html, {header: value}) for redirects
        self.pages: dict[str, tuple] = {}
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.api_records = api_payload()
        self.last_modified = "Mon, 09 Nov 2026 14:23:39 GMT"

    def api_body(self, url: str) -> bytes:
        """The API fixture records whose LastPublishDate falls in the query's
        LastPublishDateStart..End window (the real API filters the same way)."""
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        start = (q.get("LastPublishDateStart") or [""])[0]
        end = (q.get("LastPublishDateEnd") or ["9999-12-31"])[0]
        return json.dumps([r for r in self.api_records
                           if start <= str(r.get("LastPublishDate") or "")[:10] <= end]).encode()

    def source_of(self, url: str) -> Optional[str]:
        for name, base in self.urls.items():
            if url == base or url.startswith(base + "?"):
                return name
        return None

    def __call__(self, url: str, *, headers: dict[str, str], timeout: float) -> rg.HttpResponse:
        self.calls.append((url, dict(headers)))
        name = self.source_of(url)
        if name is None:
            page = self.pages.get(url, (404, "not found"))
            status, body = page[0], page[1]
            hdrs = {"content-type": "text/html", **(page[2] if len(page) > 2 else {})}
            return rg.HttpResponse(status, hdrs, body.encode())
        if name in self.fail:
            return rg.HttpResponse(self.fail[name], {}, b"error")
        if name == rg.SOURCE_API:
            return rg.HttpResponse(200, {"content-type": "application/json"}, self.api_body(url))
        if headers.get("If-Modified-Since") == self.last_modified:
            return rg.HttpResponse(304, {"last-modified": self.last_modified}, b"")
        body = fixture_bytes(RECALL_CSV if name == rg.SOURCE_RECALL_CSV else WARNING_CSV)
        return rg.HttpResponse(200, {"content-type": "text/csv",
                                     "last-modified": self.last_modified,
                                     "etag": f'"{name}-v1"'}, body)

    def api_queries(self) -> list[dict[str, list[str]]]:
        return [urllib.parse.parse_qs(urllib.parse.urlsplit(u).query)
                for u, _ in self.calls if self.source_of(u) == rg.SOURCE_API]


def offline_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any real network call fail the test (urlopen and every opener,
    including the probe's no-redirect opener)."""
    import urllib.request

    def no_network(*a: Any, **k: Any) -> Any:
        raise AssertionError("network access attempted in an offline test")
    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", no_network)


# ---------------------------------------------------------------------------
# Throwaway Postgres
# ---------------------------------------------------------------------------

_PG: dict[str, Any] = {}
_IMAGE = "postgres:16"


def _free_port(start: int = 55480, end: int = 55990) -> int:
    for port in range(start, end):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("no free port above 55000")


def _stop_container() -> None:
    name = _PG.get("container")
    if name:
        subprocess.run(["docker", "stop", "-t", "2", name], capture_output=True, timeout=60)
        _PG.pop("container", None)


def _conninfo(params: dict[str, Any], dbname: str) -> str:
    return " ".join(f"{k}={v}" for k, v in {**params, "dbname": dbname}.items())


def pg_params() -> dict[str, Any]:
    """libpq params of the throwaway server (skips when none is available)."""
    if "params" in _PG:
        return _PG["params"]
    if "skip" in _PG:
        pytest.skip(_PG["skip"])
    import psycopg2
    env_dsn = os.environ.get("RECALL_GATE_TEST_PG_DSN", "").strip()
    if env_dsn:
        conn = psycopg2.connect(env_dsn)
        p = conn.get_dsn_parameters()
        conn.close()
        params = {k: p[k] for k in ("host", "port", "user") if p.get(k)}
        params["password"] = psycopg2.extensions.parse_dsn(env_dsn).get("password", "")
        _PG["params"] = params
        return params
    if not shutil.which("docker"):
        _PG["skip"] = "docker not available for the throwaway Postgres"
        pytest.skip(_PG["skip"])
    if subprocess.run(["docker", "image", "inspect", _IMAGE], capture_output=True,
                      timeout=30).returncode != 0:
        _PG["skip"] = f"docker image {_IMAGE} not present locally (offline test run)"
        pytest.skip(_PG["skip"])
    password = secrets.token_hex(16)
    port = _free_port()
    name = f"rp-f8-recallgate-pg-{os.getpid()}-{secrets.token_hex(3)}"
    fd, env_path = tempfile.mkstemp(prefix="rg-pg-", suffix=".env")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(f"POSTGRES_PASSWORD={password}\n")
        os.chmod(env_path, 0o600)
        r = subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "--env-file",
                            env_path, "-p", f"127.0.0.1:{port}:5432", _IMAGE],
                           capture_output=True, text=True, timeout=120)
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
            conn = psycopg2.connect(_conninfo(params, "postgres"), connect_timeout=2)
            conn.close()
            break
        except psycopg2.OperationalError:
            if time.time() > deadline:
                _stop_container()
                _PG["skip"] = "throwaway Postgres did not become ready"
                pytest.skip(_PG["skip"])
            time.sleep(0.5)
    _PG["params"] = params
    return params


def fresh_database(prefix: str = "rg") -> str:
    """Create an empty database on the throwaway server; return its conninfo."""
    import psycopg2
    params = pg_params()
    name = f"{prefix}_{secrets.token_hex(5)}"
    admin = psycopg2.connect(_conninfo(params, "postgres"))
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f"CREATE DATABASE {name}")
    admin.close()
    return _conninfo(params, name)


def seed_site_db(dsn: str, rows: list[dict[str, Any]], *, tables: Optional[dict] = None) -> None:
    """Create the synthetic site schema + the recall-gate DDL and load a
    catalog fixture into brands / models / products / kit_components."""
    import psycopg2
    conn = psycopg2.connect(dsn)
    with conn, conn.cursor() as cur:
        cur.execute((FIXTURES / "site_schema.sql").read_text())
        cur.execute(rg.schema_sql(tables))
        brands: dict[str, int] = {}
        by_number: dict[str, int] = {}
        for r in rows:
            if r["brand"] not in brands:
                brands[r["brand"]] = len(brands) + 1
                cur.execute("INSERT INTO brands (id, name) VALUES (%s, %s)",
                            (brands[r["brand"]], r["brand"]))
            model_id = r.get("model_id") or 1000 + int(r["product_id"])
            slug = f"{r['brand']}-{r.get('model_name') or model_id}".lower().replace(" ", "-")
            cur.execute("INSERT INTO models (id, brand_id, model_name, model_number, family, slug)"
                        " VALUES (%s, %s, %s, %s, %s, %s)",
                        (model_id, brands[r["brand"]], r.get("model_name"), r.get("model_number"),
                         r.get("family"), slug))
            cur.execute("INSERT INTO products (id, model_id, asin, category) VALUES (%s, %s, %s, %s)",
                        (r["product_id"], model_id, r.get("asin"), r.get("category")))
            if r.get("model_number"):
                by_number[r["model_number"]] = model_id
        for r in rows:
            for comp in r.get("components") or []:
                cur.execute("INSERT INTO kit_components (kit_model_id, component_model_id) "
                            "VALUES (%s, %s)", (r.get("model_id"), by_number[comp]))
    conn.close()


def connect(dsn: str) -> Any:
    import psycopg2
    return psycopg2.connect(dsn)


def query(dsn: str, sql: str, params: Any = None) -> list[dict[str, Any]]:
    conn = connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()


def make_send_recorder() -> tuple[list[dict[str, Any]], Callable[..., tuple[bool, str]]]:
    sent: list[dict[str, Any]] = []

    def send(**kw: Any) -> tuple[bool, str]:
        sent.append(kw)
        return True, "sent (test)"
    return sent, send
