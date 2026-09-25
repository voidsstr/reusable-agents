"""Post-deploy indexability + latency gate — fail a deploy that makes the
site less crawlable than it was a minute earlier.

Why: agent deploys (~40 commits/day) passed a smoke check that only looked
for HTTP 200. In September 2026 a PI change noindexed 99.8% of a site's
recipes and stayed live for ~28 hours / 39 releases; a UGS change pushed the
shop sitemap query past the DB timeout (HTTP 500) and nothing noticed.

How: `measure()` takes a snapshot of the configured canaries —
  pages     each `expect_indexable_paths` entry: status, indexable (no
            noindex, self-canonical, ≥ min_text_chars visible text), cold
            TTFB and a warm re-fetch TTFB
  sitemaps  every child of the sitemap index: status + URL count
  sample    `sitemap_sample` random page URLs from those children, checked
            for indexability (the same URLs before and after the deploy)
The deployer measures once BEFORE the deploy (baseline) and once after;
`compare()` fails only on REGRESSIONS — a check that passed before and fails
now — so an already-broken page or sitemap never blocks the deploy that
might fix it. Without a baseline every failing check is a failure.

Config (site.yaml → deployer.smoke_check.gate; schema:
shared/schemas/site-config.schema.json):
    gate:
      expect_indexable_paths: ["/", "/blog/<a-guide>", ...]
      min_text_chars: 200            # empty-shell guard (0 = off)
      sitemap_index: /sitemap.xml    # "" = skip sitemap checks
      allow_empty_sitemaps: []       # children allowed to have 0 URLs
      sitemap_sample: 20
      max_bad_ratio: 0.25            # sample share that may be non-indexable
      warm_max_ttfb_s: 3.0           # warm re-fetch budget per canary
      cold_fail_s: 14                # cold TTFB this slow is recorded as a
                                     # failure only if the warm re-fetch is
                                     # also over budget (a real slowdown, not
                                     # a cold revision)
      user_agent: "<honest UA>"      # default: IndexabilityCheck; do NOT
                                     # spoof AI crawlers — sites log those
                                     # into their AI-traffic tables
      timeout_s: 30
"""
from __future__ import annotations

import random
from typing import Optional

from . import page_indexability as pi

DEFAULTS = {
    "expect_indexable_paths": [],
    "min_text_chars": 0,
    "sitemap_index": "/sitemap.xml",
    "allow_empty_sitemaps": [],
    "sitemap_sample": 0,
    "max_bad_ratio": 0.25,
    "warm_max_ttfb_s": 3.0,
    "cold_fail_s": 14.0,
    "user_agent": pi.DEFAULT_UA,
    "timeout_s": 30.0,
}


def resolve(cfg: Optional[dict]) -> dict:
    out = dict(DEFAULTS)
    out.update({k: v for k, v in (cfg or {}).items() if v is not None})
    return out


def _abs(base_url: str, path: str) -> str:
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return base_url.rstrip("/") + (path if path.startswith("/") else "/" + path)


def measure(base_url: str, cfg: dict, *, sample_urls: Optional[list[str]] = None,
            rng: Optional[random.Random] = None) -> dict:
    """One snapshot of the site's crawl-facing health (see module doc)."""
    c = resolve(cfg)
    ua, timeout = c["user_agent"], float(c["timeout_s"])
    snap: dict = {"pages": {}, "sitemaps": {}, "sample": {}, "sample_urls": []}

    for path in c["expect_indexable_paths"]:
        url = _abs(base_url, path)
        cold = pi.check_page(url, ua=ua, timeout=max(timeout, float(c["cold_fail_s"]) + 5),
                             min_text_chars=int(c["min_text_chars"]))
        warm = pi.check_page(url, ua=ua, timeout=timeout, min_text_chars=int(c["min_text_chars"]))
        snap["pages"][url] = {
            "status": warm.status or cold.status,
            "indexable": warm.indexable,
            "reason": warm.reason,
            "cold_ttfb_s": cold.ttfb_s,
            "warm_ttfb_s": warm.ttfb_s,
        }

    if c["sitemap_index"]:
        index_url = _abs(base_url, c["sitemap_index"])
        rep = pi.check_sitemaps(index_url, ua=ua, timeout=max(timeout, 60.0),
                                allow_empty=c["allow_empty_sitemaps"], sample_n=0)
        snap["sitemaps"][index_url] = {"status": rep.index_status, "urls": None}
        for ch in rep.children:
            snap["sitemaps"][ch["url"]] = {"status": ch["status"], "urls": ch["urls"]}
        n = int(c["sitemap_sample"] or 0)
        if n:
            if sample_urls is None:
                # Draw from the sitemaps just read; the same URLs are
                # re-checked after the deploy so the comparison is like for like.
                r = rng or random.Random()
                sample_urls = r.sample(rep.pool, min(n, len(rep.pool))) if rep.pool else []
            snap["sample_urls"] = list(sample_urls)
            for u in sample_urls:
                pc = pi.check_page(u, ua=ua, timeout=timeout, min_text_chars=int(c["min_text_chars"]))
                snap["sample"][u] = {"status": pc.status, "indexable": pc.indexable, "reason": pc.reason}
    return snap


def _sitemap_ok(entry: dict, url: str, allow_empty: list[str]) -> bool:
    if entry.get("status") != 200:
        return False
    if entry.get("urls") == 0:
        from urllib.parse import urlsplit
        return url in allow_empty or urlsplit(url).path in allow_empty
    return True


def compare(pre: Optional[dict], post: dict, cfg: dict) -> tuple[list[str], list[str]]:
    """(failures, warnings). With `pre`, only regressions fail; problems that
    already existed before the deploy are warnings."""
    c = resolve(cfg)
    failures: list[str] = []
    warnings: list[str] = []
    warm_budget = float(c["warm_max_ttfb_s"])
    cold_fail = float(c["cold_fail_s"])
    allow = list(c["allow_empty_sitemaps"])

    def _flag(before: Optional[dict], was_ok: bool, msg: str):
        # No baseline at all, no baseline entry for this check, or it passed
        # before → the deploy caused it. Failed before too → pre-existing.
        if pre is None or before is None or was_ok:
            failures.append(msg)
        else:
            warnings.append(f"(already before deploy) {msg}")

    for url, p in post.get("pages", {}).items():
        b = (pre or {}).get("pages", {}).get(url)
        if not p["indexable"]:
            _flag(b, bool(b and b.get("indexable")), f"{url} not indexable after deploy: {p['reason']} (HTTP {p['status']})")
            continue
        warm = p.get("warm_ttfb_s")
        cold = p.get("cold_ttfb_s")
        if warm is not None and warm > warm_budget:
            was_fast = b is not None and b.get("warm_ttfb_s") is not None and b["warm_ttfb_s"] <= warm_budget
            _flag(b, was_fast, f"{url} warm TTFB {warm:.2f}s > {warm_budget:.1f}s budget"
                            + (f" (was {b['warm_ttfb_s']:.2f}s)" if b and b.get('warm_ttfb_s') is not None else ""))
        if cold is not None and cold >= cold_fail:
            warnings.append(f"{url} cold TTFB {cold:.1f}s ≥ {cold_fail:.0f}s on the new revision "
                            f"(warm {warm if warm is not None else '?'}s)")

    for url, s in post.get("sitemaps", {}).items():
        if _sitemap_ok(s, url, allow):
            continue
        b = (pre or {}).get("sitemaps", {}).get(url)
        was_ok = b is not None and _sitemap_ok(b, url, allow)
        what = f"HTTP {s.get('status')}" if s.get("status") != 200 else "0 URLs"
        _flag(b, was_ok, f"sitemap {url} → {what}")

    sample = post.get("sample", {})
    if sample:
        bad = sum(1 for v in sample.values() if not v["indexable"])
        ratio = bad / len(sample)
        pre_sample = (pre or {}).get("sample", {})
        pre_bad = sum(1 for v in pre_sample.values() if not v["indexable"])
        pre_ratio = pre_bad / len(pre_sample) if pre_sample else None
        if ratio > float(c["max_bad_ratio"]):
            regressed = pre_ratio is None or ratio >= pre_ratio + 0.15
            msg = (f"sitemap sample: {bad}/{len(sample)} URLs not indexable"
                   + (f" (was {pre_bad}/{len(pre_sample)})" if pre_sample else ""))
            if pre is None or regressed:
                failures.append(msg)
            else:
                warnings.append(f"(already before deploy) {msg}")
    return failures, warnings
