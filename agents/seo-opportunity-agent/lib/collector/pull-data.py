#!/usr/bin/env python3
"""Phase 1 of the SEO pipeline — pull GSC + GA4 + DB into <run_dir>/data/.

RECONSTRUCTED 2026-08-14 after the fleet-host loss. Like its sibling
refresh-token.py, this file lived only on the retired box: a bare `lib/`
pattern in .gitignore matches at ANY depth, so
agents/seo-opportunity-agent/lib/{collector,analyzer}/ was never committed.
Its absence is what made both *-seo-opportunity-agent instances fail with
`collector exited rc=2` — and those are the tier-1 SEO rec producers, so
the whole organic-search rec stream was dead.

The tracked ancestor (nsc-assistant/agents/_legacy-seo-opportunity-agent/
pull-data.py) is an OLDER generation and its filenames no longer match what
the current analyzer reads: it wrote countries-90d.json / events-28d.json /
geo-28d.json where analyzer.build_snapshot() now loads gsc-countries-90d
/ ga4-events-28d / ga4-geo-28d. This is written against the CURRENT
contract, derived by reading analyzer.build_snapshot() directly rather than
porting the ancestor's names.

CONTRACT — analyzer.build_snapshot() loads these from <run_dir>/data/:
    gsc-queries-90d.json        gsc-pages-90d.json
    gsc-devices-90d.json        gsc-countries-90d.json
    ga4-summary-28d.json        ga4-events-28d.json
    ga4-geo-28d.json            ga4-traffic-sources-28d.json
    db-stats.json               site-signals.json
    sitemap-urls.json           (coverage-gap inventory: per-pattern counts)
    ads-*.json                  (optional — not produced here)
Each GSC/GA4 file is the raw API response, because the analyzer reads
`.get("rows", [])` straight off it. `_load()` returns {} for a missing
file, so a source that fails degrades instead of crashing the phase.

FAILURE POLICY: GSC is the primary signal and drives nearly every rec type,
so a GSC failure exits non-zero — better a visible failed run than a run
that silently emits recommendations from an empty snapshot. GA4, DB and
site-signals degrade to {} with a warning.

Usage (the orchestrator passes both):
  python3 pull-data.py --agent-id <id> --run-ts <ts>
Env: SEO_AGENT_CONFIG -> site.yaml
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from shared.site_config import load_config_from_env  # noqa: E402

REFRESH_TOKEN_PY = _HERE / "refresh-token.py"

# GSC data lags ~2-3 days; ending "yesterday" yields a partial tail that
# makes period-over-period comparisons look like a cliff.
GSC_LAG_DAYS = 3
GSC_WINDOW_DAYS = 90
GA4_WINDOW_DAYS = 28
ROW_LIMIT = 25000


def warn(msg: str) -> None:
    print(f"[collector] {msg}", file=sys.stderr)


def _post_json(url: str, token: str, body: dict, timeout: int = 90) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def mint_token(cfg) -> str:
    """Mint a Google access token via the sibling refresh-token.py.

    Subprocess rather than import: the filename has a hyphen, so it is not
    importable, and shelling out keeps ONE implementation of the refresh +
    consent flow instead of a second copy drifting here.
    """
    # auth_oauth_path is a METHOD on SiteConfig, not a @property (site_id and
    # domain are properties, which makes this easy to get wrong) -- without
    # the call you pass a bound-method repr as a filename and the mint fails
    # with a confusing "No OAuth file at <bound method ...>".
    oauth_file = str(cfg.auth_oauth_path())
    proc = subprocess.run(
        [sys.executable, str(REFRESH_TOKEN_PY), "--oauth-file", oauth_file],
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        raise SystemExit(
            f"[collector] token mint failed rc={proc.returncode}: "
            f"{(proc.stderr or '').strip()[:300]}\n"
            f"  Re-auth with: bash install/reauth-google-oauth.sh"
        )
    token = (proc.stdout or "").strip().splitlines()[-1] if proc.stdout else ""
    if not token.startswith("ya29"):
        raise SystemExit(f"[collector] token mint returned no ya29 token: {token[:80]!r}")
    return token


# ---------------------------------------------------------------------------
# GSC
# ---------------------------------------------------------------------------

def gsc_query(token: str, site_url: str, start: str, end: str,
              dimensions: list[str]) -> dict:
    url = ("https://www.googleapis.com/webmasters/v3/sites/"
           f"{urllib.parse.quote(site_url, safe='')}/searchAnalytics/query")
    return _post_json(url, token, {
        "startDate": start, "endDate": end,
        "dimensions": dimensions, "rowLimit": ROW_LIMIT,
    })


def collect_gsc(token: str, cfg, data: Path) -> int:
    src = (cfg.get("data_sources") or {}).get("gsc") or {}
    site_url = src.get("site_url")
    if not site_url:
        raise SystemExit("[collector] data_sources.gsc.site_url missing from site.yaml")
    end = date.today() - timedelta(days=GSC_LAG_DAYS)
    start = end - timedelta(days=GSC_WINDOW_DAYS)
    s, e = start.isoformat(), end.isoformat()

    total_rows = 0
    for fname, dims in (
        ("gsc-queries-90d.json", ["query"]),
        ("gsc-pages-90d.json", ["page"]),
        ("gsc-devices-90d.json", ["device"]),
        ("gsc-countries-90d.json", ["country"]),
    ):
        resp = gsc_query(token, site_url, s, e, dims)
        n = len(resp.get("rows") or [])
        total_rows += n
        (data / fname).write_text(json.dumps(resp, indent=1))
        warn(f"  ✓ {fname}: {n} rows")
    warn(f"  GSC window {s}..{e} ({GSC_WINDOW_DAYS}d), {total_rows} rows total")
    return total_rows


# ---------------------------------------------------------------------------
# GA4
# ---------------------------------------------------------------------------

def ga4_report(token: str, property_id: str, body: dict) -> dict:
    url = f"https://analyticsdata.googleapis.com/v1beta/properties/{property_id}:runReport"
    return _post_json(url, token, body)


def collect_ga4(token: str, cfg, data: Path) -> None:
    src = (cfg.get("data_sources") or {}).get("ga4") or {}
    prop = str(src.get("property_id") or "").strip()
    if not prop:
        warn("  GA4 skipped — data_sources.ga4.property_id not configured")
        return
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=GA4_WINDOW_DAYS)
    rng = [{"startDate": start.isoformat(), "endDate": end.isoformat()}]
    reports = {
        "ga4-summary-28d.json": {
            "dateRanges": rng,
            "metrics": [{"name": m} for m in
                        ("sessions", "totalUsers", "newUsers",
                         "screenPageViews", "engagementRate")],
        },
        "ga4-events-28d.json": {
            "dateRanges": rng,
            "dimensions": [{"name": "eventName"}],
            "metrics": [{"name": "eventCount"}],
        },
        "ga4-geo-28d.json": {
            "dateRanges": rng,
            "dimensions": [{"name": "country"}],
            "metrics": [{"name": "sessions"}],
        },
        "ga4-traffic-sources-28d.json": {
            "dateRanges": rng,
            "dimensions": [{"name": "sessionDefaultChannelGroup"}],
            "metrics": [{"name": "sessions"}, {"name": "totalUsers"}],
        },
    }
    for fname, body in reports.items():
        try:
            resp = ga4_report(token, prop, body)
        except Exception as e:
            # Degrade: the analyzer tolerates {} and GA4 drives only the
            # conversion-path rec types, not the core GSC-driven ones.
            warn(f"  ! {fname} failed ({str(e)[:120]}) — writing empty")
            resp = {}
        (data / fname).write_text(json.dumps(resp, indent=1))
        warn(f"  ✓ {fname}: {len(resp.get('rows') or [])} rows")


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

_QUERY_MARKER = re.compile(r"^--\s*@@QUERY:\s*(\S+)\s*$", re.M)


def collect_db(cfg, data: Path) -> None:
    """Run each `-- @@QUERY: <name>` block into stats[<name>].

    The marker convention is documented at the top of each site's
    db-queries.sql; this is the reader for it.
    """
    src = (cfg.get("data_sources") or {}).get("db") or {}
    qfile = src.get("queries_file")
    dsn = os.environ.get(src.get("dsn_env") or "DATABASE_URL", "")
    out: dict[str, Any] = {}
    if not qfile or not Path(qfile).is_file():
        warn(f"  DB skipped — queries_file missing ({qfile})")
        (data / "db-stats.json").write_text(json.dumps(out, indent=1))
        return
    if not dsn:
        warn(f"  DB skipped — ${src.get('dsn_env') or 'DATABASE_URL'} not set")
        (data / "db-stats.json").write_text(json.dumps(out, indent=1))
        return
    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor
    except Exception as e:
        warn(f"  DB skipped — psycopg2 unavailable: {e}")
        (data / "db-stats.json").write_text(json.dumps(out, indent=1))
        return

    text = Path(qfile).read_text()
    parts = _QUERY_MARKER.split(text)
    # split() -> [preamble, name1, body1, name2, body2, ...]
    pairs = list(zip(parts[1::2], parts[2::2]))
    conn = psycopg2.connect(dsn)
    try:
        for name, sql in pairs:
            sql = sql.strip()
            if not sql:
                continue
            try:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute("SET statement_timeout = 60000")
                    cur.execute(sql)
                    rows = [dict(r) for r in cur.fetchall()]
                # A single-row result is a scalar block (totals); keep it as
                # a dict so the analyzer's db_stats[key].get("last_7d") works.
                out[name] = rows[0] if len(rows) == 1 else rows
            except Exception as e:
                conn.rollback()
                warn(f"  ! db query {name} failed: {str(e)[:120]}")
                out[name] = []
    finally:
        try:
            conn.close()
        except Exception:
            pass
    (data / "db-stats.json").write_text(json.dumps(out, indent=1, default=str))
    warn(f"  ✓ db-stats.json: {len(out)} query blocks")


# ---------------------------------------------------------------------------
# Site signals (robots + homepage) — cheap, no auth
# ---------------------------------------------------------------------------

def _extract_jsonld_types(html: str) -> list[str]:
    """All schema.org @type names present in the page's ld+json blocks.

    Handles a top-level dict, a top-level list, @graph containers, and
    list-valued @type. Unparseable blocks are skipped (a broken block is
    effectively absent markup as far as Google is concerned).
    """
    types: list[str] = []
    for block in re.findall(
            r"(?is)<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
            html):
        try:
            doc = json.loads(block)
        except Exception:
            continue
        if isinstance(doc, dict):
            items = [doc] + [g for g in (doc.get("@graph") or []) if isinstance(g, dict)]
        elif isinstance(doc, list):
            items = [d for d in doc if isinstance(d, dict)]
        else:
            continue
        for it in items:
            t = it.get("@type")
            for name in (t if isinstance(t, list) else [t]):
                if isinstance(name, str) and name and name not in types:
                    types.append(name)
    return types


def collect_site_signals(cfg, data: Path) -> None:
    domain = cfg.domain
    base = domain if domain.startswith("http") else f"https://{domain}"
    signals: dict[str, Any] = {}
    try:
        with urllib.request.urlopen(f"{base}/robots.txt", timeout=25) as r:
            body = r.read().decode("utf-8", "replace")
        signals["robots"] = {
            "status": r.status,
            "has_sitemap": "sitemap:" in body.lower(),
            "disallow_all": bool(re.search(r"(?im)^disallow:\s*/\s*$", body)),
            "body": body[:4000],
        }
    except Exception as e:
        warn(f"  ! robots.txt failed: {str(e)[:100]}")
        signals["robots"] = {}
    try:
        req = urllib.request.Request(base, headers={"User-Agent": "reusable-agents-seo/1.0"})
        with urllib.request.urlopen(req, timeout=25) as r:
            html = r.read().decode("utf-8", "replace")
        title = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
        desc = re.search(r'(?is)<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']', html)
        signals["homepage"] = {
            "status": r.status,
            "title": (title.group(1).strip()[:300] if title else ""),
            "meta_description": (desc.group(1).strip()[:400] if desc else ""),
            "html_bytes": len(html),
            "h1_count": len(re.findall(r"(?i)<h1[\s>]", html)),
            "has_canonical": bool(re.search(r'(?i)<link[^>]+rel=["\']canonical', html)),
            # The analyzer's home-jsonld-missing check reads this. It MUST
            # be measured from the live HTML — before 2026-08-18 the key was
            # never written, the analyzer treated "unmeasured" as "missing",
            # and the rec fired every run on sites that DO emit the markup.
            "jsonld_types": _extract_jsonld_types(html),
        }
    except Exception as e:
        warn(f"  ! homepage fetch failed: {str(e)[:100]}")
        signals["homepage"] = {}
    (data / "site-signals.json").write_text(json.dumps(signals, indent=1))
    warn("  ✓ site-signals.json")


# ---------------------------------------------------------------------------
# Sitemap inventory — feeds the analyzer's coverage-gap pass
# ---------------------------------------------------------------------------

_SITEMAP_LOC = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.S)
_SITEMAP_CHILD_CAP = 100  # child sitemaps per index; both sites are < 25 today


def _fetch_text(url: str, timeout: int = 25) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "reusable-agents-seo/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def collect_sitemap_inventory(cfg, data: Path) -> None:
    """Fetch sitemap.xml (following one level of sitemap-index children) and
    write sitemap-urls.json for the analyzer's coverage-gap pass.

    Without this file `_add_content_gap_recs` counts 0 existing pages for
    every `coverage_targets` pattern and re-ships the same new-page-* recs
    every run regardless of what's already published (2026-08-18: specpicks
    had 101 live /buying-guide/ pages while the rec demanded 30 "missing").

    The sites run to ~180k sitemap URLs, so the artifact stores per-pattern
    counts (computed against coverage_targets at collect time) plus a small
    URL sample, not the full list. `complete: false` marks a partial fetch
    (a child sitemap failed) — the analyzer must not trust undercounts.
    """
    targets = (cfg.get("coverage_targets") or {})
    patterns: dict[str, Any] = {}
    for tname, tcfg in targets.items():
        pat = (tcfg or {}).get("sitemap_pattern", "")
        if not pat:
            continue
        try:
            patterns[pat] = re.compile(pat)
        except re.error as e:
            warn(f"  ! coverage_targets.{tname} bad sitemap_pattern: {e}")

    domain = cfg.domain
    base = domain if domain.startswith("http") else f"https://{domain}"
    try:
        root = _fetch_text(f"{base}/sitemap.xml")
    except Exception as e:
        warn(f"  ! sitemap.xml fetch failed: {str(e)[:100]} — no sitemap-urls.json")
        return

    complete = True
    urls: set[str] = set()
    if "<sitemapindex" in root:
        children = _SITEMAP_LOC.findall(root)
        if len(children) > _SITEMAP_CHILD_CAP:
            warn(f"  ! sitemap index has {len(children)} children — capping at {_SITEMAP_CHILD_CAP}")
            children = children[:_SITEMAP_CHILD_CAP]
            complete = False
        for child in children:
            try:
                urls.update(_SITEMAP_LOC.findall(_fetch_text(child)))
            except Exception as e:
                warn(f"  ! child sitemap failed ({child}): {str(e)[:80]}")
                complete = False
    else:
        urls.update(_SITEMAP_LOC.findall(root))

    pattern_counts = {
        pat: sum(1 for u in urls if rx.search(u))
        for pat, rx in patterns.items()
    }
    (data / "sitemap-urls.json").write_text(json.dumps({
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "complete": complete,
        "total_urls": len(urls),
        "pattern_counts": pattern_counts,
        "sample_urls": sorted(urls)[:100],
    }, indent=1))
    warn(f"  ✓ sitemap-urls.json: {len(urls)} urls, "
         f"{len(pattern_counts)} pattern counts, complete={complete}")


# ---------------------------------------------------------------------------

def run_into(data: Path, cfg) -> None:
    data.mkdir(parents=True, exist_ok=True)
    token = mint_token(cfg)
    rows = collect_gsc(token, cfg, data)
    if rows == 0:
        # Not fatal: a brand-new property legitimately has no rows yet, and
        # the analyzer has a pre_traffic_mode for exactly that.
        warn("  GSC returned 0 rows across all dimensions (new/quiet property?)")
    collect_ga4(token, cfg, data)
    collect_db(cfg, data)
    collect_site_signals(cfg, data)
    collect_sitemap_inventory(cfg, data)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--agent-id", default=None)
    p.add_argument("--run-ts", default=None)
    args = p.parse_args()

    cfg = load_config_from_env()

    if args.agent_id and args.run_ts:
        # Azure-backed mode, mirroring analyzer._main_azure so both phases
        # of one run land in the same blob prefix.
        from framework.core.run_dir import RunDir
        rd = RunDir(args.agent_id, args.run_ts, site=cfg.site_id)
        warn(f"site={cfg.site_id} run_ts={args.run_ts} agent_id={args.agent_id} (azure)")
        with rd.tempdir(structured_parent=True) as td:
            run_into(Path(td) / "data", cfg)
        print(rd.handle)
        return 0

    if args.agent_id and not args.run_ts:
        raise SystemExit("--agent-id requires --run-ts")

    run_ts, run_dir = cfg.make_run_dir()
    warn(f"site={cfg.site_id} run_ts={run_ts} (local)")
    run_into(run_dir / "data", cfg)
    print(str(run_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
