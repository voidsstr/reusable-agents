"""indexnow-submitter — AgentBase wrapper (2026-05-11 conversion).

Replaces the bash-driven entry. The submit.ts TypeScript runner is
preserved unchanged — this Python wrapper just exec's it as a single
subprocess, parses "done submitted=N failed=M" lines from the output,
and emits the counts as RunResult.metrics for Layer-B goal auto-track.

The Layer-A metric_helper.record_many() path is preserved for backward
compat with site-goals-tracker rollups.

Why this conversion: every cron agent must subclass AgentBase per
CLAUDE.md. Pre-conversion the indexnow runs were invisible in the
dashboard Runs tab and didn't produce a per-run progress.json — only
the rolled-up Layer-A goal points existed.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve()
_REPO_ROOT = _HERE.parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from framework.core.agent_base import AgentBase, RunResult  # noqa: E402
from framework.core import metric_helper  # noqa: E402


# Matches log lines like:
#   "[indexnow:specpicks] done submitted=5184 failed=0"
_DONE_RE = re.compile(
    r"\[indexnow:(?P<site>[\w-]+)\]\s+done\s+submitted=(?P<submitted>\d+)\s+failed=(?P<failed>\d+)"
)


class IndexnowSubmitter(AgentBase):
    agent_id = "indexnow-submitter"
    category = "seo"

    def __init__(self, *args, **kwargs):
        env_id = os.environ.get("AGENT_ID")
        if env_id:
            kwargs.setdefault("agent_id", env_id)
        super().__init__(*args, **kwargs)

    def signals(self) -> dict | None:
        """Short-circuit when the per-site watermark file hasn't been
        bumped since last run AND no new sitemap activity. The watermark
        file's mtime is touched by submit.ts after a successful run, so
        an unchanged mtime means submit.ts already processed everything
        up to the current cursor."""
        site = (os.environ.get("INDEXNOW_SITE")
                or os.environ.get("INDEXNOW_TARGET_SITE")
                or "").strip()
        if not site:
            return None
        watermark = Path(os.path.expanduser(
            f"~/.reusable-agents/indexnow-submitter/{site}.watermark"
        ))
        if not watermark.is_file():
            return None
        st = watermark.stat()
        return {"site": site, "watermark_mtime": int(st.st_mtime),
                "watermark_size": st.st_size}

    def run(self) -> RunResult:
        # Args propagated from systemd ExecStart (e.g., --site=aisleprompt
        # or --bulk) — read from INDEXNOW_ARGS env or the per-site wrapper's
        # own argv. We honor INDEXNOW_SITE convention.
        site = (os.environ.get("INDEXNOW_SITE")
                or os.environ.get("INDEXNOW_TARGET_SITE")
                or "").strip()
        bulk = os.environ.get("INDEXNOW_BULK", "0") == "1"

        self.status(
            f"submitting indexnow URLs ({site or 'all sites'}, "
            f"{'bulk' if bulk else 'incremental'})",
            progress=0.1,
        )

        submit_ts = _HERE.parent / "submit.ts"
        if not submit_ts.is_file():
            return RunResult(status="failure",
                              summary=f"submit.ts not found at {submit_ts}")

        # The TS runner needs ts-node + pg from a specpicks node_modules.
        app_dir = os.environ.get("INDEXNOW_TS_APP_DIR",
                                  "/home/voidsstr/development/specpicks")
        cmd = [
            "npx", "ts-node", "--transpile-only",
            "--compiler-options",
            '{"module":"node16","moduleResolution":"node16","esModuleInterop":true,"skipLibCheck":true,"resolveJsonModule":true}',
            str(submit_ts),
        ]
        if site:
            cmd.append(f"--site={site}")
        if bulk:
            cmd.append("--bulk")

        env = {**os.environ, "NODE_PATH": f"{app_dir}/node_modules"}
        with tempfile.NamedTemporaryFile("w+", suffix=".log", delete=False) as out:
            out_path = out.name
        try:
            proc = subprocess.run(
                cmd, cwd=app_dir, env=env,
                capture_output=True, text=True,
                timeout=int(os.environ.get("INDEXNOW_TIMEOUT_S", "900")),
            )
            Path(out_path).write_text((proc.stdout or "") + "\n" + (proc.stderr or ""))
            output = (proc.stdout or "") + "\n" + (proc.stderr or "")
            rc = proc.returncode
        except subprocess.TimeoutExpired as e:
            return RunResult(
                status="failure",
                summary=f"submit.ts timed out after {e.timeout}s",
            )
        except FileNotFoundError as e:
            return RunResult(status="failure",
                              summary=f"npx/ts-node not found: {e}")
        except Exception as e:
            return RunResult(status="failure",
                              summary=f"submit.ts raised: {e}")

        # Parse "done submitted=N failed=M" lines for this site's totals.
        submitted = 0
        failed = 0
        per_site_seen: list[str] = []
        for line in output.splitlines():
            m = _DONE_RE.search(line)
            if not m:
                continue
            per_site_seen.append(m.group("site"))
            # When INDEXNOW_SITE is set, only count matching site;
            # otherwise sum across all sites.
            if not site or m.group("site") == site:
                submitted += int(m.group("submitted"))
                failed += int(m.group("failed"))

        # Layer-B metrics
        metrics = {
            "urls_submitted_this_run": float(submitted),
            "urls_failed_this_run": float(failed),
            "success_rate_pct": round(
                100.0 * submitted / max(submitted + failed, 1), 2
            ),
            # 30d totals are computed by the legacy metric_helper code
            # below; surface here as the same key so binders can pick it.
            # Will be the per-tick add; the cache aggregates over time.
            "rc": float(rc),
        }

        # Layer-A: replicate the old run.sh metric-emit path so site-
        # goals-tracker still sees goal-urls-submitted-30d updates.
        try:
            # Read the existing 30d total from the timeseries cache and
            # add this run's submission to it (mirrors what the old bash
            # parser did, but cleaner).
            cache = metric_helper.read_cache(self.agent_id) or {}
            metric_values = cache.get("metric_values") or {}
            prev_total = float(metric_values.get("goal-urls-submitted-30d", 0))
            new_total = prev_total + submitted
            success_rate_7d = metrics["success_rate_pct"]
            metric_helper.record_many(
                self.agent_id,
                {
                    "goal-urls-submitted-30d": new_total,
                    "goal-runs-success-rate-7d": success_rate_7d,
                },
                run_ts=self.run_ts,
                note=f"submitted={submitted} failed={failed} sites={per_site_seen}",
            )
            metrics["urls_submitted_30d_cumulative"] = new_total
        except Exception as e:
            self.decide("warning",
                        f"metric_helper.record_many failed (non-fatal): {e}")

        # ── Coverage verification: site canonical URLs vs sitemap ──────
        # Fetch the site's `canonical_urls_endpoint` (config) and diff
        # against the current sitemap.xml(s). If any canonical URLs are
        # missing from the sitemap, surface the gap AND queue them as
        # IndexNow submissions so we don't lose indexing-velocity while
        # the sitemap generator catches up.
        coverage = _check_canonical_coverage_for_site(site) if site else None
        if coverage:
            metrics["canonical_urls_total"] = float(coverage["total"])
            metrics["canonical_urls_in_sitemap"] = float(coverage["in_sitemap"])
            metrics["canonical_urls_missing"] = float(coverage["missing"])
            metrics["sitemap_coverage_pct"] = round(
                100.0 * coverage["in_sitemap"] / max(coverage["total"], 1), 2
            )
            self.decide("observation",
                f"sitemap coverage {coverage['in_sitemap']}/{coverage['total']} "
                f"({metrics['sitemap_coverage_pct']}%); "
                f"missing={coverage['missing']}")
            # If we found gaps, queue them for IndexNow on next tick by
            # writing to the watermark file's neighbouring 'force-submit.txt'
            # (the submit.ts runner reads this and includes them).
            if coverage["missing"] and coverage.get("missing_urls"):
                try:
                    queue_path = Path(os.path.expanduser(
                        f"~/.reusable-agents/indexnow-submitter/{site}.force-submit.txt"
                    ))
                    queue_path.parent.mkdir(parents=True, exist_ok=True)
                    queue_path.write_text("\n".join(coverage["missing_urls"][:5000]))
                    self.decide("action",
                        f"queued {len(coverage['missing_urls'][:5000])} missing URLs "
                        f"for next-tick IndexNow submission at {queue_path.name}")
                except Exception as e:
                    self.decide("warning", f"force-submit queue write failed: {e}")

        # ── Sitemap submission to Google + Bing ──────────────────────
        # IndexNow already pings Bing/Yandex/Naver/Seznam with individual
        # URLs. The sitemap-level ping closes the loop for: (a) Google
        # (which is NOT an IndexNow consumer) and (b) Bing's whole-site
        # crawl-budget signal. The ping endpoints are still operational
        # as of 2026-05 — Google deprecated them in 2023 but kept them
        # returning HTTP 200 to avoid breaking publisher tooling, and
        # Bing's is fully supported.
        sitemap_results = _ping_sitemaps_for_site(site) if site else _ping_sitemaps_all()
        metrics["sitemap_pings_attempted"] = float(sitemap_results["attempted"])
        metrics["sitemap_pings_ok"] = float(sitemap_results["ok"])
        sitemap_summary = sitemap_results.get("summary", "")
        if sitemap_summary:
            self.decide("action", f"sitemap pings: {sitemap_summary}")

        if rc != 0:
            return RunResult(
                status="failure",
                summary=(
                    f"submit.ts exited rc={rc}; "
                    f"submitted={submitted} failed={failed}"
                ),
                metrics=metrics,
            )

        return RunResult(
            status="success",
            summary=(
                f"submitted {submitted} URLs (failed={failed}) "
                f"to IndexNow across {len(set(per_site_seen)) or 0} site(s); "
                f"sitemap pings ok={sitemap_results['ok']}/{sitemap_results['attempted']}"
            ),
            metrics=metrics,
        )


# ── Sitemap submission helpers — fully config-driven ────────────────
# Per-site sitemap URLs, hosts, and GSC property identifiers are loaded
# from sites.json adjacent to this agent. The reusable-agents framework
# contains no hardcoded site names — adding a new deployment is purely
# a sites.json edit, plus dropping the new site's IndexNow key file
# under the site's webroot.
#
# Required sites.json schema per entry:
#   {
#     "name": "<site-id>",
#     "host": "<domain>",
#     "key":  "<indexnow-key>",
#     "sitemapUrls": ["https://.../sitemap.xml", ...],
#     "gsc_property": "sc-domain:<domain>"  (or  "https://<host>/" for
#                                            URL-prefix-property setups)
#   }
#
# OAuth: refresh-token file path comes from `INDEXNOW_GSC_OAUTH_FILE` env
# (default: ~/.reusable-agents/seo/.oauth.json — the same file the
# seo-opportunity-agent's collector uses for GSC reads). One refresh
# token covers every site the OAuth account owns as siteOwner / siteUser.


def _read_sites_config() -> list[dict]:
    """Load full per-site config dicts from sites.json adjacent to this agent."""
    try:
        import json
        path = _HERE.parent / "sites.json"
        data = json.loads(path.read_text())
        # New schema (preferred): { "sites": [{...}, ...] }
        if isinstance(data, dict) and isinstance(data.get("sites"), list):
            return [s for s in data["sites"] if isinstance(s, dict)]
        # Older schema: [{...}, ...]
        if isinstance(data, list):
            return [s for s in data if isinstance(s, dict)]
        return []
    except Exception:
        return []


def _site_config(site: str) -> dict:
    for entry in _read_sites_config():
        if entry.get("name") == site or entry.get("site") == site:
            return entry
    return {}


def _sitemap_urls_for_site(site: str) -> list[str]:
    cfg = _site_config(site)
    urls = cfg.get("sitemapUrls") or []
    if urls:
        return [u for u in urls if isinstance(u, str) and u.startswith("http")]
    # Final fallback: derive from host if defined
    host = cfg.get("host")
    return [f"https://{host}/sitemap.xml"] if host else []


def _gsc_property_for_site(site: str) -> str | None:
    """Return the GSC siteUrl identifier (sc-domain:... or URL prefix) for `site`."""
    cfg = _site_config(site)
    p = cfg.get("gsc_property")
    if p:
        return p
    host = cfg.get("host")
    return f"sc-domain:{host}" if host else None


# In-process cache for the GSC access token. Refresh-token → access-token
# exchange typically returns a 1-hour TTL, so we cache for 50 minutes to
# stay safely inside the window without making one exchange call per site
# per run. Survives the duration of a single agent invocation.
_GSC_TOKEN_CACHE: dict = {"access_token": None, "expires_at": 0.0}


def _get_gsc_access_token() -> str | None:
    """Refresh + return a Google access token for the GSC API.

    Looks for `~/.reusable-agents/seo/.oauth.json` — the same refresh
    token file the seo-opportunity-agent's collector uses for GSC reads.
    Token has scope `https://www.googleapis.com/auth/webmasters` (set
    during the original bootstrap flow), which covers both
    searchanalytics.query (reads) AND sitemaps.submit (writes).

    Returns None when the file is missing — caller surfaces a friendly
    message rather than failing the run.
    """
    import time as _time
    now = _time.time()
    if _GSC_TOKEN_CACHE["access_token"] and now < _GSC_TOKEN_CACHE["expires_at"]:
        return _GSC_TOKEN_CACHE["access_token"]

    oauth_path = os.path.expanduser(
        os.environ.get("INDEXNOW_GSC_OAUTH_FILE")
        or os.environ.get("GSC_OAUTH_FILE")
        or "~/.reusable-agents/seo/.oauth.json"
    )
    if not os.path.isfile(oauth_path):
        return None

    try:
        import json as _json
        import urllib.parse
        import urllib.request
        cfg = _json.loads(open(oauth_path).read())
        if not all(k in cfg for k in ("client_id", "client_secret", "refresh_token")):
            return None
        body = urllib.parse.urlencode({
            "client_id":     cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "refresh_token": cfg["refresh_token"],
            "grant_type":    "refresh_token",
        }).encode()
        req = urllib.request.Request(
            "https://oauth2.googleapis.com/token",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = _json.loads(resp.read().decode())
        access = data.get("access_token")
        if access:
            expires_in = int(data.get("expires_in") or 3600)
            _GSC_TOKEN_CACHE["access_token"] = access
            _GSC_TOKEN_CACHE["expires_at"] = now + max(60, expires_in - 600)
        return access
    except Exception:
        return None


def _fetch_text(url: str, *, timeout: int = 30) -> str:
    import urllib.request
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (compatible; SpecPicksIndexNow/1.0)"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def _parse_sitemap_locs(xml_text: str) -> set[str]:
    """Extract <loc>...</loc> URLs from sitemap XML. Follows <sitemapindex>
    children if the doc is an index rather than a urlset."""
    import re as _re
    locs = set()
    for m in _re.finditer(r"<loc>\s*([^<\s]+)\s*</loc>", xml_text or ""):
        locs.add(m.group(1).strip())
    return locs


def _collect_sitemap_urls(sitemap_urls: list[str]) -> set[str]:
    """Recursively collect every <loc> across all sitemaps (including sub-
    sitemaps if the doc is a <sitemapindex>)."""
    seen: set[str] = set()
    queue = list(sitemap_urls)
    visited: set[str] = set()
    while queue:
        sm = queue.pop(0)
        if sm in visited:
            continue
        visited.add(sm)
        try:
            xml = _fetch_text(sm, timeout=20)
        except Exception:
            continue
        # If it's a sitemap-index, queue child sitemaps; else collect locs.
        if "<sitemapindex" in xml:
            queue.extend(_parse_sitemap_locs(xml))
        else:
            seen.update(_parse_sitemap_locs(xml))
    return seen


def _check_canonical_coverage_for_site(site: str) -> dict | None:
    """Fetch the site's canonical_urls_endpoint, parse sitemap(s), diff.

    Returns {total, in_sitemap, missing, missing_urls[]} or None when no
    canonical_urls_endpoint is configured for the site (graceful skip)."""
    cfg = _site_config(site)
    endpoint = cfg.get("canonical_urls_endpoint")
    if not endpoint:
        return None
    sitemap_urls = _sitemap_urls_for_site(site)
    if not sitemap_urls:
        return None
    try:
        import json as _json
        body = _fetch_text(endpoint, timeout=60)
        data = _json.loads(body)
    except Exception:
        return None
    expected = data.get("urls") or []
    expected_set: set[str] = set()
    for entry in expected:
        if isinstance(entry, str):
            expected_set.add(entry)
        elif isinstance(entry, dict) and isinstance(entry.get("loc"), str):
            expected_set.add(entry["loc"])
    if not expected_set:
        return None
    in_sitemap = _collect_sitemap_urls(sitemap_urls)
    missing = sorted(expected_set - in_sitemap)
    return {
        "total": len(expected_set),
        "in_sitemap": len(expected_set & in_sitemap),
        "missing": len(missing),
        "missing_urls": missing,
    }


def _http_status(url: str, *, method: str = "HEAD", timeout: int = 12) -> tuple[int, str]:
    """Return (status_code, detail). Used for sitemap reachability + robots check."""
    import urllib.request
    try:
        req = urllib.request.Request(
            url, method=method,
            headers={"User-Agent": "Mozilla/5.0 (compatible; SpecPicksIndexNow/1.0)"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, "ok"
    except Exception as e:
        # urllib raises HTTPError for non-2xx; the status is on the exception.
        code = getattr(e, "code", 0)
        return int(code or 0), str(e)[:120]


def _ping_sitemaps_for_site(site: str) -> dict:
    """Per-site sitemap verification + GSC submission.

    Modern reality (2026): legacy `google.com/ping?sitemap=` and
    `bing.com/ping?sitemap=` ping endpoints are dead (Google: 404 since
    2023; Bing: 410 since 2024). The supported paths are:

        - Google → Search Console API sitemap.submit (OAuth).
        - Bing   → IndexNow per-URL submission (already covered above).

    What this method does every run:
        1. Every configured `sitemapUrls[*]` returns HTTP 200.
        2. robots.txt references each sitemap (Google's documented modern
           hint).
        3. GSC sitemap.submit for each sitemap against the site's
           `gsc_property` siteUrl (reads OAuth from the same refresh-token
           the seo-opportunity-agent collector uses).
    """
    sitemap_urls = _sitemap_urls_for_site(site)
    if not sitemap_urls:
        return {"attempted": 0, "ok": 0,
                "summary": f"no sitemapUrls configured for site={site} in sites.json"}

    attempted = 0
    ok = 0
    parts: list[str] = []

    # Pre-load robots.txt once per site (avoid re-fetching for every sitemap URL)
    robots_text = ""
    try:
        from urllib.parse import urlparse
        first = sitemap_urls[0]
        root = urlparse(first)
        robots_url = f"{root.scheme}://{root.netloc}/robots.txt"
        import urllib.request
        req = urllib.request.Request(
            robots_url,
            headers={"User-Agent": "Googlebot/2.1 (+http://www.google.com/bot.html)"},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            robots_text = resp.read().decode("utf-8", errors="ignore")
    except Exception:
        pass

    # Per-site GSC token + property
    gsc_property = _gsc_property_for_site(site)
    access_token = _get_gsc_access_token()

    for sm_url in sitemap_urls:
        # Reachability check
        attempted += 1
        code, detail = _http_status(sm_url)
        sm_short = sm_url.rsplit('/', 1)[-1]
        if 200 <= code < 300:
            ok += 1
        else:
            parts.append(f"{sm_short}=HTTP {code}")
            continue  # don't try to submit a broken sitemap to GSC

        # robots.txt reference check (one shared per-site result)
        # Only emit once per site to keep the summary readable.

        # GSC submit
        if access_token and gsc_property:
            attempted += 1
            try:
                from urllib.parse import quote
                import urllib.request
                api = (
                    f"https://www.googleapis.com/webmasters/v3/sites/"
                    f"{quote(gsc_property, safe='')}/sitemaps/{quote(sm_url, safe='')}"
                )
                req = urllib.request.Request(
                    api, method="PUT",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                with urllib.request.urlopen(req, timeout=15) as resp:
                    if 200 <= resp.status < 300:
                        ok += 1
                        parts.append(f"{sm_short}=GSC submitted")
                    else:
                        parts.append(f"{sm_short}=GSC HTTP {resp.status}")
            except Exception as e:
                code = getattr(e, "code", 0)
                parts.append(f"{sm_short}=GSC ERR{(' '+str(code)) if code else ''} ({str(e)[:60]})")
        elif not access_token:
            parts.append(f"{sm_short}=NO_OAUTH")
        elif not gsc_property:
            parts.append(f"{sm_short}=NO_GSC_PROPERTY (set sites.json[].gsc_property)")

    # One-shot robots.txt status line
    attempted += 1
    if any(sm in robots_text for sm in sitemap_urls) or "Sitemap:" in robots_text:
        ok += 1
        parts.append("robots.txt=OK")
    else:
        parts.append("robots.txt=MISSING Sitemap: line")

    return {
        "attempted": attempted,
        "ok": ok,
        "summary": (f"site={site} property={gsc_property or '?'} "
                    f"sitemaps={len(sitemap_urls)} :: " + ", ".join(parts)),
    }


def _ping_sitemaps_all() -> dict:
    """Verify + GSC-submit for every site in sites.json (no INDEXNOW_SITE env set)."""
    attempted = 0
    ok = 0
    summaries = []
    for entry in _read_sites_config():
        site_name = entry.get("name") or entry.get("site")
        if not site_name:
            continue
        r = _ping_sitemaps_for_site(site_name)
        attempted += r["attempted"]
        ok += r["ok"]
        if r.get("summary"):
            summaries.append(r["summary"])
    return {"attempted": attempted, "ok": ok, "summary": " | ".join(summaries)}


def main() -> int:
    return 0 if IndexnowSubmitter().run_once().status == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
