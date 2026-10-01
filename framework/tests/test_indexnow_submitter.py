"""Runs agents/indexnow-submitter/submit.test.ts (unit + end-to-end).

The IndexNow worker is TypeScript; its tests are too. This wrapper finds a
ts-node the same way agent.py does (INDEXNOW_TS_APP_DIR's node_modules, else
the global modules) and skips when none is installed.

Regression cover for the 2026-09-25 audit: bulkOnly sets re-sent every tick,
404/noindex URLs submitted, the `-vs-` compose separator dropped, and the
force-submit queue never read.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_AGENT_DIR = _HERE.parents[2] / "agents" / "indexnow-submitter"


def _node_env() -> dict | None:
    if not shutil.which("npx"):
        return None
    app = os.environ.get("INDEXNOW_TS_APP_DIR", "/home/voidsstr/development/specpicks")
    candidates = [Path(app) / "node_modules"]
    try:
        root = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True, timeout=30).stdout.strip()
        if root:
            candidates.append(Path(root))
    except Exception:
        pass
    for nm in candidates:
        if (nm / "ts-node").is_dir() and (nm / "pg").is_dir():
            env = {**os.environ, "NODE_PATH": str(nm)}
            env["PATH"] = f"{nm / '.bin'}:{env.get('PATH', '')}"
            return env
    return None


def test_indexnow_submit_ts():
    env = _node_env()
    if env is None:
        pytest.skip("ts-node + pg not installed (npm i -g ts-node typescript pg)")
    proc = subprocess.run(
        ["npx", "--no-install", "ts-node", "--transpile-only", "--compiler-options",
         '{"module":"node16","moduleResolution":"node16","esModuleInterop":true,"skipLibCheck":true}',
         "submit.test.ts"],
        cwd=_AGENT_DIR, env=env, capture_output=True, text=True, timeout=600,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "all passed" in out, out


def _load_agent_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("indexnow_agent", _AGENT_DIR / "agent.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_coverage_gap_appends_and_skips_ledgered_urls(tmp_path, monkeypatch):
    """The coverage writer used to OVERWRITE the queue every run (erasing
    queue-publish.py entries) and re-queue the same permanent gap forever."""
    import time
    mod = _load_agent_module()
    monkeypatch.setattr(mod, "_STATE_DIR", tmp_path)
    q = tmp_path / "s.force-submit.txt"
    q.write_text("https://s.test/published-by-hook")            # no trailing newline
    now_ms = int(time.time() * 1000)
    (tmp_path / "s.indexnow-ledger.tsv").write_text(
        f"https://s.test/sent-yesterday\tsent\t{now_ms - 86_400_000}\t\n"
        f"https://s.test/404-yesterday\trej:http-404\t{now_ms - 86_400_000}\t\n"
        f"https://s.test/sent-long-ago\tsent\t{now_ms - 30 * 86_400_000}\t\n"
    )
    n = mod._append_force_queue("s", [
        "https://s.test/sent-yesterday", "https://s.test/404-yesterday",
        "https://s.test/sent-long-ago", "https://s.test/new", "https://s.test/new",
        "https://s.test/published-by-hook",
    ])
    assert n == 2
    assert q.read_text().splitlines() == [
        "https://s.test/published-by-hook", "https://s.test/sent-long-ago", "https://s.test/new",
    ]
    # A second run with the same permanent gap queues nothing new.
    assert mod._append_force_queue("s", ["https://s.test/new"]) == 0


def test_coverage_check_is_throttled(tmp_path, monkeypatch):
    mod = _load_agent_module()
    monkeypatch.setattr(mod, "_STATE_DIR", tmp_path)
    assert mod._coverage_due("s", 6) is True
    assert mod._coverage_due("s", 6) is False


# ── GSC sitemap submission hygiene (2026-10-01 audit) ────────────────────

_INDEX_XML = """<?xml version="1.0"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://s.test/sitemap-core.xml</loc><lastmod>2026-09-30</lastmod></sitemap>
  <sitemap><loc>https://s.test/sitemap-blog.xml</loc><lastmod>2026-09-30</lastmod></sitemap>
</sitemapindex>"""
_BLOG_XML = """<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://s.test/blog/a</loc><lastmod>2026-09-29</lastmod><priority>0.9</priority></url>
</urlset>"""


def _wire_site(mod, monkeypatch, tmp_path, pages, *, extra_cfg=None):
    """Point the agent at a fake site `s` and record every GET/PUT."""
    cfg = {"name": "s", "host": "s.test", "gsc_property": "sc-domain:s.test",
           "sitemapUrls": ["https://s.test/sitemap.xml"],
           "gscChildSitemaps": ["https://s.test/sitemap-blog.xml"],
           "probeHeaders": {"x-site-probe": "indexnow"}}
    cfg.update(extra_cfg or {})
    monkeypatch.setattr(mod, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(mod, "_read_sites_config", lambda: [cfg])
    monkeypatch.setattr(mod, "_get_gsc_access_token", lambda: "tok")
    calls = {"get": [], "put": []}

    def fake_get(url, *, headers=None, timeout=20):
        calls["get"].append((url, dict(headers or {})))
        if url.endswith("/robots.txt"):
            return 200, "Sitemap: https://s.test/sitemap.xml\n"
        return (200, pages[url]) if url in pages else (404, "")

    def fake_put(prop, sm_url, token):
        calls["put"].append(sm_url)
        return True, "GSC submitted"

    monkeypatch.setattr(mod, "_fetch_status_and_text", fake_get)
    monkeypatch.setattr(mod, "_gsc_put_sitemap", fake_put)
    return calls


def test_robots_and_sitemap_fetches_use_truthful_ua(tmp_path, monkeypatch):
    mod = _load_agent_module()
    pages = {"https://s.test/sitemap.xml": _INDEX_XML,
             "https://s.test/sitemap-blog.xml": _BLOG_XML}
    calls = _wire_site(mod, monkeypatch, tmp_path, pages)
    mod._ping_sitemaps_for_site("s")
    assert calls["get"], "expected robots.txt + sitemap fetches"
    for url, headers in calls["get"]:
        ua = headers.get("User-Agent", "")
        assert "googlebot" not in ua.lower() and "bingbot" not in ua.lower(), (url, ua)
        assert ua == mod._FETCH_UA
        assert headers.get("x-site-probe") == "indexnow"
    src = (_AGENT_DIR / "agent.py").read_text()
    assert "Googlebot/2.1 (+http" not in src


def test_gsc_submits_index_and_child_only_on_change(tmp_path, monkeypatch):
    import time
    mod = _load_agent_module()
    pages = {"https://s.test/sitemap.xml": _INDEX_XML,
             "https://s.test/sitemap-blog.xml": _BLOG_XML}
    calls = _wire_site(mod, monkeypatch, tmp_path, pages)

    # First run: both the index and the configured child are submitted.
    r = mod._ping_sitemaps_for_site("s")
    assert calls["put"] == ["https://s.test/sitemap.xml", "https://s.test/sitemap-blog.xml"]
    assert r["gsc_submitted"] == 2

    # Next tick, nothing changed: no PUT at all.
    calls["put"].clear()
    r = mod._ping_sitemaps_for_site("s")
    assert calls["put"] == [] and r["gsc_skipped"] == 2

    # The blog sitemap gains an article, but the last submit was minutes
    # ago: still held back (at most one submission per 24h).
    pages["https://s.test/sitemap-blog.xml"] = _BLOG_XML.replace(
        "</urlset>", "<url><loc>https://s.test/blog/b</loc><lastmod>2026-10-01</lastmod></url></urlset>")
    mod._ping_sitemaps_for_site("s")
    assert calls["put"] == []

    # 25h later only the changed child is re-submitted; the unchanged index is not.
    state = mod._read_gsc_state("s")
    for v in state.values():
        v["submitted_at"] = time.time() - 25 * 3600
    mod._write_gsc_state("s", state)
    mod._ping_sitemaps_for_site("s")
    assert calls["put"] == ["https://s.test/sitemap-blog.xml"]


def test_gsc_failed_put_is_retried_next_tick(tmp_path, monkeypatch):
    mod = _load_agent_module()
    pages = {"https://s.test/sitemap.xml": _INDEX_XML}
    calls = _wire_site(mod, monkeypatch, tmp_path, pages,
                       extra_cfg={"gscChildSitemaps": []})
    monkeypatch.setattr(mod, "_gsc_put_sitemap",
                        lambda p, u, t: (calls["put"].append(u) or (False, "GSC HTTP 500")))
    mod._ping_sitemaps_for_site("s")
    mod._ping_sitemaps_for_site("s")
    assert calls["put"] == ["https://s.test/sitemap.xml"] * 2
    assert mod._read_gsc_state("s") == {}


def test_gsc_submit_due_rules():
    mod = _load_agent_module()
    now = 1_000_000.0
    due = lambda prev, fp: mod._gsc_submit_due(prev, fp, now, min_hours=24, max_hours=168)[0]
    assert due(None, "a")
    assert not due({"fingerprint": "a", "submitted_at": now - 3600}, "b")        # too soon
    assert due({"fingerprint": "a", "submitted_at": now - 25 * 3600}, "b")       # changed
    assert not due({"fingerprint": "a", "submitted_at": now - 25 * 3600}, "a")   # unchanged
    assert due({"fingerprint": "a", "submitted_at": now - 170 * 3600}, "a")      # weekly refresh
    assert not mod._gsc_submit_due({"fingerprint": "a", "submitted_at": now - 170 * 3600},
                                   "a", now, min_hours=24, max_hours=0)[0]


def test_sitemap_fingerprint_ignores_noise():
    mod = _load_agent_module()
    a = mod._sitemap_fingerprint(_INDEX_XML)
    reordered = _INDEX_XML.replace("  <sitemap><loc>https://s.test/sitemap-core.xml</loc>"
                                   "<lastmod>2026-09-30</lastmod></sitemap>\n", "")
    reordered = reordered.replace("</sitemapindex>",
                                  "<sitemap>\n <loc>https://s.test/sitemap-core.xml</loc>\n"
                                  " <lastmod>2026-09-30</lastmod>\n</sitemap></sitemapindex>")
    assert mod._sitemap_fingerprint(reordered) == a
    assert mod._sitemap_fingerprint(_INDEX_XML.replace("2026-09-30</lastmod></sitemap>\n</sitemapindex>",
                                                       "2026-10-01</lastmod></sitemap>\n</sitemapindex>")) != a
    assert mod._sitemap_fingerprint(_BLOG_XML.replace("<priority>0.9</priority>", "")) == \
        mod._sitemap_fingerprint(_BLOG_XML)
    assert mod._sitemap_fingerprint("<html>challenge</html>") is None
