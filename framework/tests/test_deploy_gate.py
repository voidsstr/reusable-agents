"""Tests for the post-deploy indexability + latency gate.

Regression cover for audit F115: agent deploys passed a smoke check that only
looked for HTTP 200, so a release that noindexed 99.8% of a site's recipes
and a shop sitemap that 500s both shipped. The gate compares a pre-deploy
baseline with the post-deploy state and fails only on regressions.
"""
from __future__ import annotations

import http.server
import importlib.util
import threading
import time
from pathlib import Path

import pytest

from framework.core import deploy_gate, page_indexability as pi


# ── A tiny switchable site ──────────────────────────────────────────────────

class _Site:
    def __init__(self):
        self.noindex: set[str] = set()
        self.broken_sitemaps: set[str] = set()
        self.slow: dict[str, float] = {}
        self.port = 0

    def page(self, path: str) -> str:
        head = f'<link rel="canonical" href="http://127.0.0.1:{self.port}{path}">'
        if path in self.noindex:
            head += '<meta name="robots" content="noindex, follow">'
        return f"<html><head><title>t</title>{head}</head><body>{'real content ' * 40}</body></html>"


@pytest.fixture()
def site():
    s = _Site()

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            path = self.path
            if path in s.slow:
                time.sleep(s.slow[path])
            if path == "/sitemap.xml":
                body = ("<sitemapindex>" + "".join(
                    f"<sitemap><loc>http://127.0.0.1:{s.port}/sm-{i}.xml</loc></sitemap>" for i in (1, 2))
                    + "</sitemapindex>")
                return self._send(200, body)
            if path.startswith("/sm-"):
                if path in s.broken_sitemaps:
                    return self._send(500, "Error generating sitemap")
                n = path[4]
                urls = "".join(f"<url><loc>http://127.0.0.1:{s.port}/p{n}-{k}</loc></url>" for k in range(5))
                return self._send(200, f"<urlset>{urls}</urlset>")
            if path == "/gone":
                return self._send(404, "nope")
            return self._send(200, s.page(path))

        def _send(self, code, body):
            b = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    s.port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield s
    srv.shutdown()


def _cfg(**over):
    c = {"expect_indexable_paths": ["/", "/guide"], "min_text_chars": 100,
         "sitemap_index": "/sitemap.xml", "sitemap_sample": 6, "max_bad_ratio": 0.25,
         "warm_max_ttfb_s": 0.3, "cold_fail_s": 5, "timeout_s": 5}
    c.update(over)
    return c


def _base(site):
    return f"http://127.0.0.1:{site.port}"


def test_healthy_deploy_passes(site):
    cfg = _cfg()
    pre = deploy_gate.measure(_base(site), cfg)
    post = deploy_gate.measure(_base(site), cfg, sample_urls=pre["sample_urls"])
    failures, warnings = deploy_gate.compare(pre, post, cfg)
    assert failures == [] and warnings == []
    assert len(pre["sample_urls"]) == 6


def test_deploy_that_noindexes_a_canary_or_breaks_a_sitemap_fails(site):
    cfg = _cfg()
    pre = deploy_gate.measure(_base(site), cfg)
    site.noindex.add("/guide")
    site.broken_sitemaps.add("/sm-2.xml")
    post = deploy_gate.measure(_base(site), cfg, sample_urls=pre["sample_urls"])
    failures, _ = deploy_gate.compare(pre, post, cfg)
    assert any("/guide not indexable" in f and "noindex-meta" in f for f in failures)
    assert any("sm-2.xml" in f and "HTTP 500" in f for f in failures)


def test_already_broken_before_the_deploy_is_only_a_warning(site):
    # The shop sitemap was 500ing before this deploy: blocking the deploy
    # would also block the fix for it.
    site.broken_sitemaps.add("/sm-2.xml")
    cfg = _cfg()
    pre = deploy_gate.measure(_base(site), cfg)
    post = deploy_gate.measure(_base(site), cfg, sample_urls=pre["sample_urls"])
    failures, warnings = deploy_gate.compare(pre, post, cfg)
    assert failures == []
    assert any("already before deploy" in w and "sm-2.xml" in w for w in warnings)


def test_sample_mass_noindex_fails(site):
    cfg = _cfg()
    pre = deploy_gate.measure(_base(site), cfg)
    for u in pre["sample_urls"]:
        site.noindex.add(u.split(str(site.port), 1)[1])
    post = deploy_gate.measure(_base(site), cfg, sample_urls=pre["sample_urls"])
    failures, _ = deploy_gate.compare(pre, post, cfg)
    assert any(f.startswith("sitemap sample: 6/6") for f in failures)


def test_warm_latency_regression_fails(site):
    cfg = _cfg()
    pre = deploy_gate.measure(_base(site), cfg)
    site.slow["/guide"] = 0.6
    post = deploy_gate.measure(_base(site), cfg, sample_urls=pre["sample_urls"])
    failures, _ = deploy_gate.compare(pre, post, cfg)
    assert any("/guide warm TTFB" in f for f in failures)


def test_no_baseline_means_every_failure_counts(site):
    site.broken_sitemaps.add("/sm-1.xml")
    cfg = _cfg(sitemap_sample=0)
    post = deploy_gate.measure(_base(site), cfg)
    failures, _ = deploy_gate.compare(None, post, cfg)
    assert any("sm-1.xml" in f for f in failures)


def test_classify_page():
    u = "https://x.test/p"
    ok = '<html><head><link rel="canonical" href="https://x.test/p/"></head><body>hi</body></html>'
    assert pi.classify_page(u, 200, "index, follow", ok) == (True, "ok")
    assert pi.classify_page(u, 200, "noindex", ok)[1] == "noindex-header"
    assert pi.classify_page(u, 200, "googlebot: noindex", ok)[0] is True
    assert pi.classify_page(u, 301, None, "")[1] == "redirect-301"
    assert pi.classify_page(u, 200, None, ok.replace("/p/", "/q"))[1] == "canonical-elsewhere"
    assert pi.classify_page(u, 200, None, ok, min_text_chars=50)[1] == "thin-body"


# ── Deployer rollback helpers ───────────────────────────────────────────────

def _load_deployer():
    path = Path(__file__).resolve().parents[2] / "agents" / "deployer" / "deployer.py"
    spec = importlib.util.spec_from_file_location("deployer_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_rollback_captures_prior_image_and_redeploys_it(tmp_path):
    d = _load_deployer()
    out = tmp_path / "rolled"
    rb = {"capture_cmd": "echo registry.test/{app}:20260925-0400",
          "cmd": f"echo {{prior_image}} > {out}"}
    prior = d._capture_prior_image(rb, {"app": "site"}, "20260925-0500", "registry.test/site", None)
    assert prior == "registry.test/site:20260925-0400"
    meta: dict = {}
    assert d._rollback(rb, {"app": "site"}, "20260925-0500", "registry.test/site", prior, None, meta, "gate")
    assert out.read_text().strip() == prior
    assert meta["rollback"]["attempted"] is True and meta["rollback"]["rc"] == 0


def test_rollback_without_a_usable_capture_is_not_attempted():
    d = _load_deployer()
    rb = {"capture_cmd": "echo 'ERROR: not logged in' 1>&2; exit 1", "cmd": "true"}
    assert d._capture_prior_image(rb, {}, "t", "img", None) == ""
    meta: dict = {}
    assert d._rollback(rb, {}, "t", "img", "", None, meta, "gate") is False
    assert meta["rollback"]["attempted"] is False


def test_schema_rejects_unknown_gate_keys(tmp_path):
    import yaml
    from shared.site_config import load_config
    cfg = {
        "site": {"id": "t", "domain": "t.example", "mode": "implement"},
        "data_sources": {"gsc": {"site_url": "sc-domain:t.example"}, "ga4": {"property_id": "1"}},
        "deployer": {"smoke_check": {"base_url": "https://t.example", "paths": ["/"],
                                     "gate": {"expect_indexable_paths": ["/"], "bogus": 1}},
                     "rollback": {"capture_cmd": "x", "cmd": "y"}},
    }
    p = tmp_path / "s.yaml"
    p.write_text(yaml.safe_dump(cfg))
    with pytest.raises(SystemExit, match="bogus"):
        load_config(p)
    del cfg["deployer"]["smoke_check"]["gate"]["bogus"]
    p.write_text(yaml.safe_dump(cfg))
    assert load_config(p).site_id == "t"


def test_parse_sitemap_accepts_feeds():
    rss = "<rss><channel><item><title>a</title><link>https://x.test/a</link></item></channel></rss>"
    atom = '<feed><entry><link href="https://x.test/b"/></entry></feed>'
    assert pi.parse_sitemap(rss) == (["https://x.test/a"], [])
    assert pi.parse_sitemap(atom) == (["https://x.test/b"], [])
