#!/usr/bin/env python3
"""GSC URL Inspection auditor — checks Google's index coverage status for
sample URLs across SpecPicks + AislePrompt and emits a JSONL coverage
report consumed by seo-analyzer.

Why: Google's Indexing API is restricted to JobPosting + BroadcastEvent
schemas, so general article URLs can't be force-indexed. The next-best
signal is the URL Inspection API — it returns whether each URL is
indexed, was crawled but rejected, or is unknown to Google. Surfacing
"crawled but not indexed" URLs as SEO recommendations lets the
implementer fix content issues that block indexing.

Strategy:
  1. Build the URL universe from sites.json (DB queries + sitemap).
  2. Pick the LEAST-RECENTLY-INSPECTED N URLs (round-robin coverage).
     Quota: 2,000 inspections/day per GSC property — we cap at ~500
     per run to leave headroom for ad-hoc usage.
  3. Call POST searchconsole.googleapis.com/v1/urlInspection/index:inspect
     for each URL. Throttle at ~1 RPS to be polite.
  4. Append results to <state-dir>/<site>-coverage.jsonl. Each row is the
     full inspection response plus our metadata (inspected_at, url).
  5. Update <state-dir>/<site>-last-inspected.json so next run skips
     URLs we just covered.

Output JSONL row example:
  {
    "url": "https://specpicks.com/reviews/...",
    "inspected_at": "2026-05-04T15:00:00Z",
    "verdict": "PASS",  // or "PARTIAL", "FAIL", "NEUTRAL"
    "coverageState": "Submitted and indexed",
    "lastCrawlTime": "2026-04-15T03:22:14Z",
    "indexingState": "INDEXING_ALLOWED",
    "robotsTxtState": "ALLOWED",
    "pageFetchState": "SUCCESSFUL",
    "raw": { ... full API response ... }
  }

Usage:
  GSC_INSPECT_SITE=specpicks python3 inspect.py
  GSC_INSPECT_SITE=specpicks GSC_INSPECT_LIMIT=100 python3 inspect.py
  GSC_INSPECT_DRY_RUN=1 ...   # show what would be inspected, no API calls
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import psycopg2

HERE = Path(__file__).resolve().parent
# Site-specific configs live in each site's repo at
# ~/development/<site>/agents/seo-config/site-indexnow.json. Override with
# SITE_CONFIG_PATHS=<path1>,<path2> to use explicit files. Last-resort
# fallback: the legacy sites.json in this dir (symlinked to indexnow's).
SITES_JSON = HERE / "sites.json"
REFRESH_SCRIPT = (HERE.parent / "seo-data-collector" / "refresh-token.py").resolve()
OAUTH_FILE = Path(os.path.expanduser(
    os.environ.get("GSC_OAUTH_FILE", "~/.reusable-agents/seo/.oauth.json")
))
STATE_DIR = Path(os.path.expanduser(
    os.environ.get("GSC_INSPECT_STATE_DIR", "~/.reusable-agents/gsc-coverage-auditor")
))
STATE_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_LIMIT = int(os.environ.get("GSC_INSPECT_LIMIT", "500"))
# GSC URL Inspection quota is 2,000/day per property. 2 RPS ≈ 7,200/hour
# burst capacity, but a single run is capped at DEFAULT_LIMIT (500 by
# default) so daily volume stays at ~25% of quota with one run, leaving
# room for ad-hoc usage. The 1.0 RPS we ran with previously was leaving
# huge unused capacity on the table.
DEFAULT_QPS = float(os.environ.get("GSC_INSPECT_QPS", "2.0"))
DRY_RUN = os.environ.get("GSC_INSPECT_DRY_RUN", "0") == "1"


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
    """Load site config — search per-site repos first, fall back to legacy."""
    candidates = []
    explicit = os.environ.get("SITE_CONFIG_PATHS")
    if explicit:
        candidates.extend(p.strip() for p in explicit.split(",") if p.strip())
    home = os.path.expanduser("~/development")
    for site in ("aisleprompt", "specpicks"):
        candidates.append(f"{home}/{site}/agents/seo-config/site-indexnow.json")
    candidates.append(str(SITES_JSON))
    seen = set()
    for path_ in candidates:
        if path_ in seen or not Path(path_).is_file():
            continue
        seen.add(path_)
        try:
            doc = json.loads(Path(path_).read_text())
        except Exception:
            continue
        for s in doc.get("sites", []):
            if s.get("name") == site_name:
                return s
    raise SystemExit(f"site {site_name!r} not found in any site config")


def slugify(s: str) -> str:
    return ("".join(c if c.isalnum() else "-" for c in (s or "").lower())
            .strip("-").replace("--", "-").replace("--", "-"))


def render_url(host: str, prefix: str, template: str, row: tuple) -> Optional[str]:
    """Mirror submit.ts urlTemplate semantics."""
    if not row:
        return None
    if template == "slug":
        return f"https://{host}{prefix}{row[0]}"
    if template.startswith("slugify:"):
        # "slugify:title|-|id" → slugify(title) + '-' + id
        rest = template.split(":", 1)[1]
        parts = rest.split("|")
        out = []
        for i, p in enumerate(parts):
            if i % 2 == 0:
                # field name
                idx = i // 2
                if idx < len(row):
                    val = row[idx]
                    out.append(slugify(str(val)) if i == 0 or template.startswith("slugify:") else str(val))
            else:
                out.append(p)
        # The pattern is field|sep|field — stitch as: slugify(row[0]) + sep + str(row[1])
        if len(parts) >= 3 and len(row) >= 2:
            return f"https://{host}{prefix}{slugify(str(row[0]))}{parts[1]}{row[1]}"
        return f"https://{host}{prefix}{slugify(str(row[0]))}"
    if template.startswith("compose:"):
        rest = template.split(":", 1)[1]
        parts = rest.split("|")
        if len(parts) == 3 and len(row) >= 2:
            return f"https://{host}{prefix}{row[0]}{parts[1]}{row[1]}"
    return None


def collect_urls_from_db(site: dict) -> set[str]:
    """Build the URL universe by running each querySet's bulkSql."""
    db_url_env = site.get("databaseUrlEnv") or "DATABASE_URL"
    db_url = os.environ.get(db_url_env) or site.get("databaseUrlFallback")
    if not db_url:
        err(f"  no DB URL — set {db_url_env} or databaseUrlFallback")
        return set()
    out: set[str] = set()
    site_ids = site.get("siteIds") or []
    site_ids_clause = ",".join(f"'{s}'" for s in site_ids) if site_ids else "NULL"
    conn = psycopg2.connect(db_url)
    try:
        for qs in site.get("querySets", []):
            sql = qs["bulkSql"].replace("$SITE_IDS", site_ids_clause)
            cur = conn.cursor()
            try:
                cur.execute(sql)
                for row in cur.fetchall():
                    url = render_url(site["host"], qs.get("urlPrefix", "/"),
                                     qs.get("urlTemplate", "slug"), row)
                    if url:
                        out.add(url)
            except Exception as e:
                err(f"  [{qs['name']}] query failed: {e}")
                conn.rollback()
            finally:
                cur.close()
    finally:
        conn.close()
    # Also include sitemap-listed URLs
    for sitemap_url in site.get("sitemapUrls", []):
        try:
            req = urllib.request.Request(sitemap_url, headers={"User-Agent": "GSCCoverageAuditor/1.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                xml = r.read().decode("utf-8", "replace")
            # Both sitemap-index and url-set
            if "<sitemapindex" in xml:
                import re
                children = re.findall(r"<loc>([^<]+)</loc>", xml)
                for child in children[:20]:
                    try:
                        req2 = urllib.request.Request(child, headers={"User-Agent": "GSCCoverageAuditor/1.0"})
                        with urllib.request.urlopen(req2, timeout=30) as r2:
                            child_xml = r2.read().decode("utf-8", "replace")
                        for u in re.findall(r"<loc>([^<]+)</loc>", child_xml):
                            out.add(u)
                    except Exception:
                        pass
            else:
                import re
                for u in re.findall(r"<loc>([^<]+)</loc>", xml):
                    out.add(u)
        except Exception as e:
            err(f"  sitemap fetch failed for {sitemap_url}: {e}")
    return out


def load_last_inspected(site_name: str) -> dict[str, str]:
    f = STATE_DIR / f"{site_name}-last-inspected.json"
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text())
    except Exception:
        return {}


def save_last_inspected(site_name: str, data: dict[str, str]) -> None:
    f = STATE_DIR / f"{site_name}-last-inspected.json"
    f.write_text(json.dumps(data, indent=2))


def inspect_url(token: str, site_url: str, target_url: str) -> Optional[dict]:
    """POST /v1/urlInspection/index:inspect."""
    body = json.dumps({
        "inspectionUrl": target_url,
        "siteUrl": site_url,
        "languageCode": "en-US",
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://searchconsole.googleapis.com/v1/urlInspection/index:inspect",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    # Retry once on 429 (rate-limit) with a 60s backoff. GSC's quota is
    # per-day not per-second so a 429 mid-run usually means we briefly
    # outpaced the burst window, not that we exhausted the daily cap.
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.request.HTTPError as e:
            body_txt = ""
            try:
                body_txt = e.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            if e.code == 429 and attempt == 0:
                err(f"  HTTP 429 on {target_url} — sleeping 60s then retrying")
                time.sleep(60)
                continue
            err(f"  HTTP {e.code}: {target_url}  {body_txt}")
            return None
        except Exception as e:
            err(f"  request failed: {target_url}  {e}")
            return None
    return None


def flatten_inspection(target_url: str, response: dict) -> dict:
    """Pull the headline fields out of the inspection response."""
    inspection = (response or {}).get("inspectionResult") or {}
    index = inspection.get("indexStatusResult") or {}
    return {
        "url": target_url,
        "inspected_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "verdict": index.get("verdict"),                # PASS / PARTIAL / FAIL / NEUTRAL
        "coverageState": index.get("coverageState"),    # human-readable like "Crawled — currently not indexed"
        "lastCrawlTime": index.get("lastCrawlTime"),
        "indexingState": index.get("indexingState"),    # INDEXING_ALLOWED / BLOCKED_BY_*
        "robotsTxtState": index.get("robotsTxtState"),
        "pageFetchState": index.get("pageFetchState"),
        "googleCanonical": index.get("googleCanonical"),
        "userCanonical": index.get("userCanonical"),
        "raw": response,
    }


def main() -> None:
    site_name = os.environ.get("GSC_INSPECT_SITE")
    if not site_name:
        raise SystemExit("set GSC_INSPECT_SITE=<aisleprompt|specpicks>")
    site = load_site(site_name)
    err(f"[gsc-coverage] site={site_name} host={site['host']} limit={DEFAULT_LIMIT} dry={DRY_RUN}")

    # Step 0: re-register the sitemap with GSC (idempotent — refreshes
    # the lastSubmitted timestamp). Cheap insurance: most cost-effective
    # nudge to bump up Google's crawl priority on young domains. Skipped
    # silently if the OAuth token doesn't have the webmasters write scope.
    if not DRY_RUN and os.environ.get("GSC_INSPECT_SKIP_SITEMAP_SUBMIT") != "1":
        try:
            token_for_sm = get_access_token()
            site_url = site.get("gscSiteUrl") or f"sc-domain:{site['host']}"
            for sm in (site.get("sitemapUrls") or [f"https://{site['host']}/sitemap.xml"]):
                site_enc = urllib.parse.quote(site_url, safe="")
                feed_enc = urllib.parse.quote(sm, safe="")
                api = (
                    f"https://searchconsole.googleapis.com/webmasters/v3/sites/"
                    f"{site_enc}/sitemaps/{feed_enc}"
                )
                req = urllib.request.Request(api, method="PUT", headers={"Authorization": f"Bearer {token_for_sm}"})
                try:
                    with urllib.request.urlopen(req, timeout=30) as r:
                        err(f"[gsc-sitemap-submit] ✓ {sm}  HTTP {r.status}")
                except urllib.request.HTTPError as e:
                    if e.code in (401, 403):
                        err(f"[gsc-sitemap-submit] skipped: {e.code} (token lacks webmasters write scope — re-bootstrap to enable)")
                        break
                    err(f"[gsc-sitemap-submit] ✗ {sm}  HTTP {e.code}")
        except Exception as e:
            err(f"[gsc-sitemap-submit] error: {e}")

    err(f"[gsc-coverage] building URL universe from DB + sitemap…")
    universe = collect_urls_from_db(site)
    err(f"[gsc-coverage] universe size: {len(universe)} URLs")
    if not universe:
        err("[gsc-coverage] empty universe, exiting")
        return

    last = load_last_inspected(site_name)
    # Sort URLs by last-inspected (oldest first), so we cycle round-robin
    sortable = [(last.get(u, "1970-01-01T00:00:00Z"), u) for u in universe]
    sortable.sort()
    targets = [u for (_, u) in sortable[:DEFAULT_LIMIT]]
    err(f"[gsc-coverage] picking {len(targets)} oldest URLs (or never-inspected)")

    if DRY_RUN:
        for u in targets[:10]:
            err(f"  would inspect: {u}  (last={last.get(u, 'never')})")
        err(f"[gsc-coverage] dry run: would inspect {len(targets)} URLs")
        return

    token = get_access_token()
    site_url = site.get("gscSiteUrl") or f"sc-domain:{site['host']}"
    out_path = STATE_DIR / f"{site_name}-coverage.jsonl"
    interval = 1.0 / DEFAULT_QPS if DEFAULT_QPS > 0 else 0
    n_ok = 0
    n_fail = 0
    counts: dict[str, int] = {}
    with out_path.open("a") as f_out:
        for i, target in enumerate(targets):
            response = inspect_url(token, site_url, target)
            if response is None:
                n_fail += 1
                # Don't update last-inspected on failure — retry next run
                if interval:
                    time.sleep(interval)
                continue
            row = flatten_inspection(target, response)
            f_out.write(json.dumps(row) + "\n")
            n_ok += 1
            cs = row.get("coverageState") or "(unknown)"
            counts[cs] = counts.get(cs, 0) + 1
            last[target] = row["inspected_at"]
            if interval:
                time.sleep(interval)
            if (i + 1) % 50 == 0:
                err(f"  progress: {i+1}/{len(targets)} (ok={n_ok} fail={n_fail})")
                save_last_inspected(site_name, last)
    save_last_inspected(site_name, last)
    err(f"[gsc-coverage] done. ok={n_ok} fail={n_fail}")
    err(f"[gsc-coverage] coverageState distribution:")
    for cs, n in sorted(counts.items(), key=lambda x: -x[1]):
        err(f"  {n:>4}  {cs}")
    err(f"[gsc-coverage] appended to {out_path}")

    # Record metrics to the framework's goals system. Each per-site
    # auditor agent has goals defined: goal-urls-inspected-7d (rolling
    # 7d count from coverage.jsonl), goal-indexed-pct, goal-unknown-pct.
    try:
        sys.path.insert(0, str(HERE.parent.parent))
        from framework.core import metric_helper
        agent_id = f"{site_name}-gsc-coverage-auditor"

        # 7d rolling URL count from the jsonl
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
        n_recent = 0
        latest_state: dict[str, str] = {}
        try:
            with out_path.open() as fh:
                for raw in fh:
                    try:
                        row = json.loads(raw)
                    except Exception:
                        continue
                    ts = row.get("inspected_at", "")
                    if ts >= cutoff:
                        n_recent += 1
                    cs = row.get("coverageState") or ""
                    if cs:
                        url = row.get("url", "")
                        if url and (url not in latest_state or ts > latest_state.get(url, ("", ""))[1] if False else True):
                            latest_state[url] = (cs, ts)
        except Exception:
            pass
        # Simpler latest-state pass (the above was over-engineered)
        latest_state = {}
        try:
            with out_path.open() as fh:
                for raw in fh:
                    try:
                        row = json.loads(raw)
                    except Exception:
                        continue
                    url = row.get("url")
                    if not url:
                        continue
                    cs = row.get("coverageState") or ""
                    ts = row.get("inspected_at", "")
                    prev = latest_state.get(url, ("", ""))
                    if ts > prev[1]:
                        latest_state[url] = (cs, ts)
        except Exception:
            pass
        total_urls = len(latest_state) or 1
        n_indexed = sum(1 for cs, _ in latest_state.values() if cs == "Submitted and indexed")
        n_unknown = sum(1 for cs, _ in latest_state.values() if cs == "URL is unknown to Google")
        metrics = {
            "goal-urls-inspected-7d": float(n_recent),
            "goal-indexed-pct": round(100.0 * n_indexed / total_urls, 2),
            "goal-unknown-pct": round(100.0 * n_unknown / total_urls, 2),
        }
        metric_helper.record_many(
            agent_id, metrics,
            run_ts=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
            note=f"auto: ok={n_ok} fail={n_fail} latest_urls={total_urls}",
        )
        err(f"[gsc-coverage] recorded metrics: {metrics}")
    except Exception as e:
        err(f"[gsc-coverage] metric recording failed: {e}")


if __name__ == "__main__":
    main()
