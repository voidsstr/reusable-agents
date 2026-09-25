"""AI-assistant landings from a site's ``ai_traffic_log`` — any site.

WHY THIS EXISTS
---------------
Both sites log AI traffic server-side into an ``ai_traffic_log`` table. Two
kinds of row say "an AI assistant used this page for a real person":

  referral     a human clicked through from ChatGPT / Perplexity / Claude /
               Copilot / Gemini (``kind='referral'``).
  live fetch   an assistant fetched the page at answer time on a user's
               behalf (``kind='crawler'`` from the user-triggered agents:
               ChatGPT-User, OAI-SearchBot (ChatGPT search's index),
               Perplexity-User, Claude-User).

Training crawlers (GPTBot, ClaudeBot, PerplexityBot, Bytespider, ...) are NOT
landings and are left out.

Agents kept steering from other signals instead: the SEO audit re-tuned PDPs
that no assistant had used, the price verifier priced the catalog tail before
the pages ChatGPT reads, and the article proposer had no AI-demand input at
all. This module is the one reader they share, so every agent means the same
thing by "AI-landed".

SPOOF FILTER
------------
Crawler rows are classified by User-Agent alone, and UAs are trivially
spoofed: scanners (and our own audit runs) send a rotation of bot UAs from a
single IP. A real vendor never shares an IP with a different vendor, so an IP
that claimed >= ``spoof_min_vendors`` distinct crawler VENDORS in the window
(OpenAI + Anthropic + ByteDance + ...) is a spoofer and all of its crawler
rows are dropped. Counting vendors, not UA names, keeps one real OpenAI IP
that serves both ChatGPT-User and OAI-SearchBot. Referral rows are never
spoof-filtered (they are human browsers with an assistant Referer). When the
table has an ``ip_verified`` column (IP checked against the vendor's
published ranges at insert time), rows proven false are dropped too.

Schema drift between sites is absorbed by config: the IP column is
``ip_address`` on one site and ``ip`` on another, one site has no
``status_code`` column, referral source names differ ('chatgpt' vs
'chatgpt.com'). Optional columns are auto-detected from
``information_schema``; values come from the caller's config (a site.yaml
knob) merged over :data:`DEFAULTS`.

Every query runs under ``SET LOCAL statement_timeout`` so a slow shared DB
degrades the caller's optional input instead of hanging it.
"""
from __future__ import annotations

import copy
import json
import os
import re
from typing import Any, Iterable, Mapping, Optional, Sequence

DEFAULTS: dict[str, Any] = {
    "table": "ai_traffic_log",
    # Column names. ip/status/verified are auto-detected when set to "auto".
    "ts_column": "ts",
    "kind_column": "kind",
    "source_column": "source",
    "path_column": "path",
    "ip_column": "auto",          # ip_address | ip | "" (none)
    "status_column": "auto",      # status_code | "" (none)
    "verified_column": "auto",    # ip_verified | "" (none)
    "referral_kinds": ["referral"],
    # [] = every referral source counts (the logger only writes AI referrers).
    "referral_sources": [],
    "crawler_kinds": ["crawler"],
    # User-triggered answer-time fetchers. Training crawlers are excluded.
    "live_fetch_sources": ["chatgpt-user", "oai-searchbot",
                           "perplexity-user", "claude-user"],
    # Windows. Referrals are rare, so they get a longer window.
    "referral_days": 90,
    "fetch_days": 30,
    # Scoring: one human landing is worth several machine fetches.
    "referral_weight": 5,
    "fetch_weight": 1,
    # Spoof filter: drop crawler rows from IPs that claimed this many distinct
    # vendors. 0 disables.
    "spoof_min_vendors": 2,
    # source → vendor. Unknown sources count as their own vendor.
    "source_vendors": {
        "gptbot": "openai", "chatgpt-user": "openai", "oai-searchbot": "openai",
        "perplexitybot": "perplexity", "perplexity-user": "perplexity",
        "claudebot": "anthropic", "claude-user": "anthropic",
        "claude-searchbot": "anthropic", "claude-code": "anthropic",
        "claude-web": "anthropic", "anthropic-ai": "anthropic",
        "google-extended": "google", "googlebot": "google",
        "applebot": "apple", "applebot-extended": "apple",
        "bingbot": "microsoft", "bytespider": "bytedance",
        "amazonbot": "amazon", "meta-externalagent": "meta",
        "ccbot": "commoncrawl", "youbot": "you", "duckassistbot": "duckduckgo",
        "cohere-ai": "cohere", "mistralai-user": "mistral",
    },
    # Exact IPs never counted (our own fleet host running audits). Merged
    # with the comma-separated AI_TRAFFIC_EXCLUDE_IPS env var.
    "exclude_ips": [],
    # Only count landings that served a 200 (when a status column exists):
    # a 301 hit on an ASIN URL is counted on the slug URL it redirects to.
    "only_status_200": True,
    "statement_timeout_ms": 30000,
}

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")
_AUTO_COLUMNS = {
    "ip_column": ("ip_address", "ip", "client_ip"),
    "status_column": ("status_code", "status"),
    "verified_column": ("ip_verified", "verified"),
}


def config(overrides: Optional[dict] = None) -> dict:
    """DEFAULTS merged with a caller's (site.yaml) overrides. Dict values
    merge one level deep so a site can add a vendor without restating all."""
    out = copy.deepcopy(DEFAULTS)
    for k, v in (overrides or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = {**out[k], **v}
        else:
            out[k] = copy.deepcopy(v)
    env_ips = [s.strip() for s in os.environ.get("AI_TRAFFIC_EXCLUDE_IPS", "").split(",")
               if s.strip()]
    out["exclude_ips"] = sorted({*(out.get("exclude_ips") or []), *env_ips})
    return out


def _ident(name: str) -> str:
    if not name or not _IDENT_RE.match(name):
        raise ValueError(f"unsafe SQL identifier: {name!r}")
    return name


def _set_timeout(cur, ms: int) -> None:
    if ms and int(ms) > 0:
        cur.execute(f"SET LOCAL statement_timeout = {int(ms)}")


def table_columns(conn, table: str) -> set[str]:
    """Column names of `table` (schema-qualified names allowed)."""
    schema, _, name = table.rpartition(".")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            " WHERE table_name = %s AND (%s = '' OR table_schema = %s)",
            (name, schema, schema))
        rows = cur.fetchall()
    return {(r["column_name"] if isinstance(r, dict) else r[0]) for r in rows}


def resolve_columns(conn, cfg: dict) -> dict:
    """Fill the "auto" column slots from information_schema. A missing
    optional column resolves to "" (feature off), never an error."""
    cfg = dict(cfg)
    if not any(cfg.get(k) == "auto" for k in _AUTO_COLUMNS):
        return cfg
    try:
        cols = table_columns(conn, cfg["table"])
    except Exception:
        cols = set()
    for key, candidates in _AUTO_COLUMNS.items():
        if cfg.get(key) == "auto":
            cfg[key] = next((c for c in candidates if c in cols), "")
    return cfg


def _like_prefix(prefix: str) -> str:
    return (prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            + "%")


def build_landed_paths_sql(cfg: dict, *, prefixes: Sequence[str] = (),
                           ) -> tuple[str, dict]:
    """(sql, params) for :func:`landed_paths`. `cfg` must already have its
    columns resolved (no "auto" left). Pure — unit-testable without a DB."""
    t = _ident(cfg["table"])
    ts, kind = _ident(cfg["ts_column"]), _ident(cfg["kind_column"])
    src, path = _ident(cfg["source_column"]), _ident(cfg["path_column"])
    ip = _ident(cfg["ip_column"]) if cfg.get("ip_column") else ""
    status = _ident(cfg["status_column"]) if cfg.get("status_column") else ""
    verified = _ident(cfg["verified_column"]) if cfg.get("verified_column") else ""

    params: dict[str, Any] = {
        "ref_kinds": list(cfg.get("referral_kinds") or ["referral"]),
        "crawl_kinds": list(cfg.get("crawler_kinds") or ["crawler"]),
        "fetch_sources": list(cfg.get("live_fetch_sources") or []),
        "ref_days": int(cfg.get("referral_days") or 90),
        "fetch_days": int(cfg.get("fetch_days") or 30),
        "rw": float(cfg.get("referral_weight") or 0),
        "fw": float(cfg.get("fetch_weight") or 0),
    }
    ref_src = ""
    if cfg.get("referral_sources"):
        params["ref_sources"] = list(cfg["referral_sources"])
        ref_src = f" AND l.{src} = ANY(%(ref_sources)s)"
    fetch_arm = ""
    if params["fetch_sources"]:
        fetch_arm = (f"\n        OR (l.{kind} = ANY(%(crawl_kinds)s)"
                     f" AND l.{src} = ANY(%(fetch_sources)s)"
                     f" AND l.{ts} > NOW() - make_interval(days => %(fetch_days)s))")
    where = [f"""(
           (l.{kind} = ANY(%(ref_kinds)s){ref_src}
            AND l.{ts} > NOW() - make_interval(days => %(ref_days)s)){fetch_arm}
         )"""]
    if status and cfg.get("only_status_200", True):
        where.append(f"l.{status} = 200")
    if prefixes:
        params["prefix_likes"] = [_like_prefix(p) for p in prefixes]
        where.append(f"l.{path} LIKE ANY(%(prefix_likes)s)")
    if verified:
        where.append(f"(l.{kind} = ANY(%(ref_kinds)s) OR l.{verified} IS NOT FALSE)")
    if ip and cfg.get("exclude_ips"):
        params["exclude_ips"] = list(cfg["exclude_ips"])
        where.append(f"(l.{ip} IS NULL OR NOT (l.{ip} = ANY(%(exclude_ips)s)))")

    ip_sel = f"l.{ip}" if ip else "NULL::text"
    spoof_cte, spoof_filter = "", ""
    spoof_min = int(cfg.get("spoof_min_vendors") or 0)
    if ip and spoof_min > 0 and params["fetch_sources"]:
        params["vendors"] = json.dumps(cfg.get("source_vendors") or {})
        params["spoof_min"] = spoof_min
        spoof_cte = f""",
     spoof AS MATERIALIZED (
       SELECT s.{ip} AS ip
         FROM {t} s
        WHERE s.{kind} = ANY(%(crawl_kinds)s)
          AND s.{ts} > NOW() - make_interval(days => %(fetch_days)s)
          AND s.{ip} IN (SELECT DISTINCT ip FROM hits
                          WHERE NOT is_referral AND ip IS NOT NULL)
        GROUP BY 1
       HAVING COUNT(DISTINCT COALESCE(%(vendors)s::jsonb ->> s.{src}, s.{src}))
              >= %(spoof_min)s)"""
        spoof_filter = ("\n      WHERE is_referral OR ip IS NULL"
                        " OR ip NOT IN (SELECT ip FROM spoof)")

    sql = f"""
    WITH hits AS MATERIALIZED (
       SELECT split_part(split_part(l.{path}, '?', 1), '#', 1) AS path,
              (l.{kind} = ANY(%(ref_kinds)s)) AS is_referral,
              {ip_sel} AS ip
         FROM {t} l
        WHERE {" AND ".join(where)}
     ){spoof_cte}
    SELECT path,
           COUNT(*) FILTER (WHERE is_referral)     AS referrals,
           COUNT(*) FILTER (WHERE NOT is_referral) AS fetches
      FROM hits{spoof_filter}
     GROUP BY path
     ORDER BY COUNT(*) FILTER (WHERE is_referral) * %(rw)s
            + COUNT(*) FILTER (WHERE NOT is_referral) * %(fw)s DESC, path
     LIMIT %(limit)s
    """
    return sql, params


def normalize_path(path: str) -> str:
    """Strip query/fragment and a trailing slash (root stays "/")."""
    p = (path or "").split("?", 1)[0].split("#", 1)[0]
    if len(p) > 1:
        p = p.rstrip("/") or "/"
    return p or "/"


def landed_paths(conn, *, cfg: Optional[dict] = None,
                 prefixes: Sequence[str] = (), limit: int = 200) -> list[dict]:
    """Paths AI assistants used, best first.

    Returns ``[{"path", "referrals", "fetches", "score"}]`` — referrals over
    ``referral_days``, live fetches over ``fetch_days`` (spoof-filtered),
    score = referrals * referral_weight + fetches * fetch_weight. Paths are
    normalized (no query string / trailing slash) and merged after
    normalization. Raises on DB errors; callers treat the signal as optional.
    """
    cfg = resolve_columns(conn, cfg if cfg is not None else config())
    sql, params = build_landed_paths_sql(cfg, prefixes=prefixes)
    # Over-fetch a little: normalization can merge rows.
    params["limit"] = max(1, int(limit)) * 2
    with conn.cursor() as cur:
        _set_timeout(cur, int(cfg.get("statement_timeout_ms") or 0))
        cur.execute(sql, params)
        rows = cur.fetchall()
    merged: dict[str, dict] = {}
    for r in rows:
        if isinstance(r, dict):
            p, ref, fet = r["path"], r["referrals"], r["fetches"]
        else:
            p, ref, fet = r[0], r[1], r[2]
        key = normalize_path(str(p or ""))
        m = merged.setdefault(key, {"path": key, "referrals": 0, "fetches": 0})
        m["referrals"] += int(ref or 0)
        m["fetches"] += int(fet or 0)
    rw = float(cfg.get("referral_weight") or 0)
    fw = float(cfg.get("fetch_weight") or 0)
    out = []
    for m in merged.values():
        m["score"] = m["referrals"] * rw + m["fetches"] * fw
        out.append(m)
    out.sort(key=lambda m: (-m["score"], m["path"]))
    return out[:limit]


def build_referral_counts_sql(cfg: dict, windows: Sequence[int] = (7, 30),
                              ) -> tuple[str, dict]:
    """(sql, params) for :func:`referral_counts`. Pure — no DB needed."""
    t = _ident(cfg["table"])
    ts, kind = _ident(cfg["ts_column"]), _ident(cfg["kind_column"])
    src = _ident(cfg["source_column"])
    status = _ident(cfg["status_column"]) if cfg.get("status_column") else ""
    wins = sorted({max(1, int(w)) for w in windows}) or [30]
    params: dict[str, Any] = {
        "ref_kinds": list(cfg.get("referral_kinds") or ["referral"]),
        "max_days": wins[-1],
    }
    where = [f"l.{kind} = ANY(%(ref_kinds)s)",
             f"l.{ts} > NOW() - make_interval(days => %(max_days)s)"]
    if cfg.get("referral_sources"):
        params["ref_sources"] = list(cfg["referral_sources"])
        where.append(f"l.{src} = ANY(%(ref_sources)s)")
    if status and cfg.get("only_status_200", True):
        where.append(f"l.{status} = 200")
    cols = []
    for w in wins:
        params[f"d{w}"] = w
        cols.append(f"COUNT(*) FILTER (WHERE l.{ts} > NOW() - make_interval(days => %(d{w})s))"
                    f" AS last_{w}d")
    sql = (f"SELECT {', '.join(cols)}\n  FROM {t} l\n WHERE "
           + "\n   AND ".join(where))
    return sql, params


def referral_counts(conn, *, cfg: Optional[dict] = None,
                    windows: Sequence[int] = (7, 30)) -> dict:
    """Human AI-assistant referral landings per window:
    ``{"last_7d": n, "last_30d": n}``. Same referral definition as
    :func:`landed_paths` (kinds, sources, status 200). Raises on DB errors."""
    cfg = resolve_columns(conn, cfg if cfg is not None else config())
    sql, params = build_referral_counts_sql(cfg, windows)
    with conn.cursor() as cur:
        _set_timeout(cur, int(cfg.get("statement_timeout_ms") or 0))
        cur.execute(sql, params)
        row = cur.fetchone()
    wins = sorted({max(1, int(w)) for w in windows}) or [30]
    if isinstance(row, dict):
        return {f"last_{w}d": int(row.get(f"last_{w}d") or 0) for w in wins}
    row = row or [0] * len(wins)
    return {f"last_{w}d": int(row[i] or 0) for i, w in enumerate(wins)}


def cluster_yield(landed: Iterable[dict], articles: Iterable[Mapping],
                  clusters: Sequence[Mapping], *, path_template: str,
                  other_label: str = "other") -> list[dict]:
    """AI-assistant demand per topic cluster, normalised by supply.

    `landed` is :func:`landed_paths` output; `articles` are published
    articles ``{slug, title}``; `clusters` are ``{name, pattern}`` (regex,
    case-insensitive, matched against "<slug> <title>"; first match wins,
    unmatched articles go to `other_label`). `path_template` maps a slug to
    its URL path, e.g. "/reviews/{slug}".

    Returns ``[{cluster, articles, referrals, fetches, referrals_per_100,
    fetches_per_100}]`` sorted by referrals_per_100 then fetches_per_100.
    Raw hit counts favour whichever cluster has the most articles; the
    per-100 yield says where one more article is most likely to be used.
    """
    compiled = [(str(c.get("name")), re.compile(str(c.get("pattern") or "(?!)"), re.I))
                for c in clusters or [] if c.get("name")]
    by_path: dict[str, str] = {}
    counts: dict[str, dict] = {}

    def bucket(name: str) -> dict:
        return counts.setdefault(name, {"cluster": name, "articles": 0,
                                        "referrals": 0, "fetches": 0})

    for a in articles or []:
        slug = str(a.get("slug") or "").strip()
        if not slug:
            continue
        text = f"{slug} {a.get('title') or ''}"
        name = next((n for n, rx in compiled if rx.search(text)), other_label)
        by_path[normalize_path(path_template.replace("{slug}", slug))] = name
        bucket(name)["articles"] += 1
    for r in landed or []:
        name = by_path.get(normalize_path(str(r.get("path") or "")))
        if name is None:
            continue
        b = bucket(name)
        b["referrals"] += int(r.get("referrals") or 0)
        b["fetches"] += int(r.get("fetches") or 0)
    out = []
    for b in counts.values():
        n = max(1, b["articles"])
        b["referrals_per_100"] = round(b["referrals"] * 100.0 / n, 1)
        b["fetches_per_100"] = round(b["fetches"] * 100.0 / n, 1)
        out.append(b)
    out.sort(key=lambda b: (-b["referrals_per_100"], -b["fetches_per_100"], b["cluster"]))
    return out


def path_keys(rows: Iterable[dict], prefix: str, *,
              key_regex: Optional[str] = None) -> list[str]:
    """First path segment after `prefix` for each row, in row order, de-duped.
    ``/product/B0ABC12345`` with prefix ``/product/`` → ``B0ABC12345``.
    `key_regex` (full match) drops keys that are not e.g. real ASINs."""
    rx = re.compile(key_regex) if key_regex else None
    seen: set[str] = set()
    out: list[str] = []
    for r in rows:
        p = normalize_path(str(r.get("path") or ""))
        if not p.startswith(prefix):
            continue
        key = p[len(prefix):].split("/", 1)[0]
        if not key or key in seen:
            continue
        if rx is not None and not rx.fullmatch(key):
            continue
        seen.add(key)
        out.append(key)
    return out
