#!/usr/bin/env python3
"""Submit a site's sitemap.xml to Google Search Console via the
Sitemaps API. One-shot script — run after a deploy or when adding
a new sitemap. Idempotent: GSC accepts the same submission repeatedly
and just refreshes the lastSubmitted timestamp.

Why we need this on top of the Sitemap: directive in robots.txt:
the directive is auto-discovered by Googlebot eventually, but for
young/low-authority domains the discovery lag is days-to-weeks.
Calling Sitemaps.submit explicitly tells GSC "here, please prioritize
crawling this" and substantially shortens the time to first crawl.

Requires: webmasters WRITE scope in the OAuth token (the bootstrap
in refresh-token.py now requests it; existing tokens need
re-bootstrap via /tmp/seo-reauth-with-write-scope.sh).

Usage:
  GSC_SITE=specpicks  python3 submit-sitemap.py
  GSC_SITE=aisleprompt python3 submit-sitemap.py
  GSC_SITE=specpicks GSC_SITEMAP_URL=https://specpicks.com/sitemap-special.xml \
    python3 submit-sitemap.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
SITES_JSON = HERE / "sites.json"
REFRESH_SCRIPT = (HERE.parent / "seo-data-collector" / "refresh-token.py").resolve()
OAUTH_FILE = Path(os.path.expanduser(
    os.environ.get("GSC_OAUTH_FILE", "~/.reusable-agents/seo/.oauth.json")
))


def err(*a) -> None:
    print(*a, file=sys.stderr)


def get_access_token() -> str:
    out = subprocess.check_output(
        [sys.executable, str(REFRESH_SCRIPT), "--oauth-file", str(OAUTH_FILE)],
        stderr=subprocess.PIPE, timeout=60,
    ).decode().strip()
    if not out:
        raise SystemExit("refresh-token.py returned empty output")
    return out


def load_site(site_name: str) -> dict:
    sites = json.loads(SITES_JSON.read_text())
    for s in sites["sites"]:
        if s["name"] == site_name:
            return s
    raise SystemExit(f"site {site_name!r} not in sites.json")


def submit_sitemap(token: str, site_url: str, sitemap_url: str) -> bool:
    """PUT https://searchconsole.googleapis.com/webmasters/v3/sites/{siteUrl}/sitemaps/{feedpath}"""
    site_enc = urllib.parse.quote(site_url, safe="")
    feed_enc = urllib.parse.quote(sitemap_url, safe="")
    api = (
        f"https://searchconsole.googleapis.com/webmasters/v3/sites/"
        f"{site_enc}/sitemaps/{feed_enc}"
    )
    req = urllib.request.Request(
        api, method="PUT",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            err(f"  ✓ {sitemap_url}  HTTP {r.status}")
            return True
    except urllib.request.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        err(f"  ✗ {sitemap_url}  HTTP {e.code}: {body}")
        return False


def list_submitted(token: str, site_url: str) -> list[dict]:
    site_enc = urllib.parse.quote(site_url, safe="")
    api = f"https://searchconsole.googleapis.com/webmasters/v3/sites/{site_enc}/sitemaps"
    req = urllib.request.Request(api, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return (json.loads(r.read().decode("utf-8")).get("sitemap") or [])
    except Exception as e:
        err(f"  list-sitemaps failed: {e}")
        return []


def main() -> None:
    site_name = os.environ.get("GSC_SITE")
    if not site_name:
        raise SystemExit("set GSC_SITE=<aisleprompt|specpicks>")
    site = load_site(site_name)
    site_url = site.get("gscSiteUrl") or f"sc-domain:{site['host']}"

    sitemaps_to_submit: list[str] = []
    explicit = os.environ.get("GSC_SITEMAP_URL")
    if explicit:
        sitemaps_to_submit = [explicit]
    else:
        sitemaps_to_submit = list(site.get("sitemapUrls") or [])
        if not sitemaps_to_submit:
            sitemaps_to_submit = [f"https://{site['host']}/sitemap.xml"]

    err(f"[gsc-sitemap] site={site_name} ({site_url}) submitting {len(sitemaps_to_submit)} sitemap(s)")
    token = get_access_token()

    n_ok = n_fail = 0
    for sm in sitemaps_to_submit:
        if submit_sitemap(token, site_url, sm):
            n_ok += 1
        else:
            n_fail += 1

    err("")
    err(f"[gsc-sitemap] done: ok={n_ok} fail={n_fail}")
    err("")
    err(f"[gsc-sitemap] currently submitted sitemaps for {site_url}:")
    for entry in list_submitted(token, site_url):
        path = entry.get("path", "?")
        last = (entry.get("lastSubmitted") or "?")[:19]
        err(f"  {path}  lastSubmitted={last}  errors={entry.get('errors',0)}  warnings={entry.get('warnings',0)}")


if __name__ == "__main__":
    main()
