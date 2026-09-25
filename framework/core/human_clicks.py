"""Verified-human click counting for first-party affiliate click tables.

WHY THIS EXISTS
---------------
Outbound affiliate clicks are logged server-side by the redirect handler, so
every crawler that walks a buy-link lands in the click table. A site's goal
tracker that counts raw rows is counting bots. Measured 2026-09-24 on one
reference deployment: 21,258 "Amazon clicks" in 30 days, of which the
site's own per-row classifier (`is_bot`) flagged 99.3%, and the 142 rows it
let through were still almost entirely automation:

  * 77 rows from ONE user agent (desktop Chrome/126, a 2024 build that no
    auto-updating browser still reports) spread over 27 IPs, all "clicked"
    from the homepage
  * 30 rows from Tencent Cloud (43.x, incl. the Singapore region) that were
    inserted before the classifier shipped and never re-labelled
  * 29 rows from Alibaba Cloud (47.79.x)
  * one `Sogou web spider/4.0`

A goal keyed off that number is steering the fleet toward whatever makes a
scraper fire a redirect. This module is the single definition of "a person
clicked a buy link", so every consumer (goal trackers, conversion reports,
the price verifier's demand list) counts the same thing.

WHAT COUNTS AS HUMAN
--------------------
A row is human only if NONE of these fire (evaluated in this order, and the
first match names the row's verdict so the breakdown is auditable):

  site-flagged-bot   the table's own per-row verdict column is true
  no-ua              empty user agent
  ua-bot             UA matches the bot / headless / automation regex
                     (crawlers, HeadlessChrome, puppeteer, playwright,
                     selenium, phantomjs, python-requests, curl, …)
  stale-browser      Chromium or Firefox major version more than
                     `slack_majors` behind the estimated current release.
                     Real browsers auto-update; a hard-coded UA string in a
                     scraper does not. Browsers that legitimately lag
                     (Samsung Internet, UC, …) are exempt.
  excluded-country   country column is in `exclude_countries` (default SG —
                     the Singapore headless datacenter fleet that made up
                     92.8% of raw sessions in the 2026-09-23 growth memo)
  datacenter-ip      IP starts with a cloud / hosting prefix
  no-referer         `require_referer` and the click has no on-site referer
                     (a person reaches a redirect by clicking a link on one
                     of our pages; a crawler fetches it directly)
  scanner-referer    referer is attack-scanner exhaust (SQLi probes, .php)
  velocity           survived every per-row rule but its IP produced more
                     than `max_clicks_per_ip_day` such clicks that day

Everything is DATA, not code: table, column names, regexes, prefixes,
countries and thresholds come from (lowest to highest precedence)

  1. DEFAULT_SPEC below
  2. storage config `config/human-click-filter-config.json` → "defaults"
  3. the caller's spec (per-site values: table + column names live with the
     consumer, e.g. a goal tracker's site profile)
  4. storage config → "by_profile"[<profile>]  (operator override per
     consumer, keyed by e.g. the tracker's agent id)

List-valued keys REPLACE rather than append, except `extra_*` keys which
append to their base list (`extra_datacenter_ip_prefixes`,
`extra_bot_ua_terms`) so an override can add a prefix without restating the
whole default list.

Example storage config:

    {
      "schema_version": "1",
      "defaults": {"max_clicks_per_ip_day": 10},
      "by_profile": {
        "<site>-site-goals-tracker": {
          "extra_datacenter_ip_prefixes": ["203.0.113."],
          "exclude_countries": ["SG", "VN"]
        }
      }
    }

Typical use:

    from framework.core import human_clicks
    spec = human_clicks.resolve_spec(
        {"time_col": "clicked_at", "referer_col": "source_page",
         "country_col": "country", "bot_flag_col": "is_bot"},
        profile=agent_id)
    n = human_clicks.count_human(conn, spec, table="outbound_clicks",
                                 window_days=30, match={"target": "amazon"})

CLI: `python3 -m framework.cli.human_clicks --help` prints the same
breakdown for an operator.
"""
from __future__ import annotations

import copy
import re
from datetime import date, datetime, timezone
from typing import Any, Mapping, Optional

CONFIG_KEY = "config/human-click-filter-config.json"

# Substrings that self-identify automation. Lowercase; matched
# case-insensitively as one alternation. Kept in step with the reference
# deployment's insert-time classifier so the two agree on history.
BOT_UA_TERMS: list[str] = [
    # crawlers / generic
    "bot", "spider", "crawler", "slurp", "bingpreview", "preview",
    "facebookexternalhit", "semrush", "ahrefs", "mj12", "dotbot", "petal",
    "yandex", "bytespider", "gptbot", "claudebot", "ccbot", "perplexity",
    "applebot", "amazonbot", "dataforseo", "serpstat", "screaming frog",
    "monitor", "uptime", "pingdom", "lighthouse", "pagespeed",
    # headless / automation frameworks
    "headless", "phantom", "puppeteer", "playwright", "selenium",
    "webdriver", "cypress", "electron/", "scrapingbee", "zenrows", "apify",
    # HTTP libraries
    "python", "curl", "wget", "scrapy", "http-client", "httpclient",
    "okhttp", "java/", "go-http", "libwww", "lwp::", "apache-httpclient",
    "axios", "node-fetch", "undici", "postman", "insomnia", "httpie",
    "aiohttp", "guzzle",
]

# Referer paths that are scanner exhaust rather than a page on the site.
SCANNER_REFERER_REGEX = (
    r"[()<>']|sysdate|sleep\(|union\s+select|\.jsp|\.php|\.asp|etc/passwd|\.\./"
    r"|^/(web|wp-admin|wp-login|cgi-bin|phpmyadmin|admin)(/|$)"
)

# Coarse IPv4 prefixes for cloud / hosting ranges seen driving fake clicks.
# A prefix list rather than an ASN lookup: there is no ASN database on the
# host, and a partial-but-honest signal beats a dependency that fails open.
DATACENTER_IP_PREFIXES: list[str] = [
    # Tencent Cloud (43.x incl. Singapore, 49.51, 101.32-35, 106.52-55,
    # 118.195, 119.28-29, 124.156, 124.220-223, 129.226, 150.109, 162.62,
    # 170.106, 175.24, 175.27, 175.178)
    "43.", "49.51.", "101.32.", "101.33.", "101.34.", "101.35.",
    "106.52.", "106.53.", "106.54.", "106.55.", "118.195.", "119.28.",
    "119.29.", "124.156.", "124.220.", "124.221.", "124.222.", "124.223.",
    "129.226.", "150.109.", "162.62.", "170.106.", "175.24.", "175.27.",
    "175.178.",
    # Alibaba Cloud (47.74-91, 47.236-254 intl incl. Singapore, 8.208-222)
    *[f"47.{n}." for n in range(74, 92)],
    *[f"47.{n}." for n in (236, 237, 241, 242, 243, 244, 245, 246, 250,
                           251, 252, 253, 254)],
    *[f"8.{n}." for n in range(208, 223)],
    # Huawei Cloud (incl. Singapore 159.138 / 119.8 / 119.13)
    "1.92.", "110.238.", "119.8.", "119.12.", "119.13.", "121.36.",
    "121.37.", "124.70.", "124.71.", "139.9.", "139.159.", "159.138.",
    "166.108.", "190.92.",
    # Cloudflare WARP egress
    "104.28.",
    # Linode / Akamai
    "172.104.", "139.162.", "45.79.", "45.33.", "50.116.",
    # DigitalOcean (incl. Singapore 128.199 / 139.59 / 188.166 / 206.189)
    "159.89.", "167.71.", "134.209.", "165.227.", "207.154.", "128.199.",
    "139.59.", "188.166.", "206.189.", "159.223.", "143.198.", "146.190.",
    "64.225.", "68.183.",
    # Vultr
    "45.32.", "45.63.", "45.76.", "45.77.", "66.42.", "108.61.", "149.28.",
    "155.138.", "207.148.",
    # Scaleway
    "51.15.", "163.172.", "212.47.",
    # GCP / AWS / Azure (coarse /8s)
    "35.", "34.", "52.", "54.", "18.", "13.", "20.", "40.",
    # Hetzner
    "5.9.", "88.198.", "78.46.", "95.216.", "116.202.", "135.181.",
    "65.108.", "65.21.",
    # OVH
    "51.68.", "51.75.", "51.77.", "51.79.", "51.81.", "51.83.", "51.89.",
    "51.91.", "51.161.", "51.178.", "51.195.", "51.210.", "51.222.",
    "54.36.", "54.37.", "54.38.", "147.135.", "149.56.", "158.69.",
    "192.99.", "198.27.",
    # Oracle Cloud
    "129.146.", "132.145.", "150.136.", "152.67.", "158.101.", "193.122.",
    # Search-engine crawl infrastructure
    "66.249.", "157.55.", "207.46.",
]

DEFAULT_SPEC: dict[str, Any] = {
    # ── columns (identifiers; validated) ──
    "time_col": "created_at",
    "ua_col": "user_agent",
    "ip_col": "ip_address",
    "referer_col": None,        # e.g. "source_page" / "referer"; None = no referer rules
    "country_col": None,        # ISO-3166 alpha-2; None = no country rule
    "bot_flag_col": None,       # the table's own boolean verdict; None = ignore
    # ── rules ──
    "require_referer": True,
    "bot_ua_terms": BOT_UA_TERMS,
    "extra_bot_ua_terms": [],
    "referer_exclude_regex": SCANNER_REFERER_REGEX,
    "datacenter_ip_prefixes": DATACENTER_IP_PREFIXES,
    "extra_datacenter_ip_prefixes": [],
    "exclude_countries": ["SG"],
    "max_clicks_per_ip_day": 20,
    "stale_browser": {
        "enabled": True,
        # How many majors behind the estimated current release a UA may be
        # before it is treated as a hard-coded scraper string. 16 majors is
        # ~15 months, which clears Firefox ESR and slow corporate rollouts.
        "slack_majors": 16,
        "cadence_days": 28,
        # [major, stable-release date] anchors; the current major is
        # extrapolated at `cadence_days` per release from here.
        "anchors": {"chromium": [140, "2025-09-02"],
                    "firefox": [142, "2025-08-19"]},
        # Browsers that legitimately ship an older engine than upstream.
        "exempt_ua_regex": "SamsungBrowser|UCBrowser|YaBrowser|HuaweiBrowser"
                           "|MiuiBrowser|QQBrowser|Silk/",
    },
}

VERDICT_HUMAN = "human"
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_LIST_EXTENDS = {
    "extra_bot_ua_terms": "bot_ua_terms",
    "extra_datacenter_ip_prefixes": "datacenter_ip_prefixes",
}


# ── config resolution ──────────────────────────────────────────────────────

def _merge(base: dict, over: Optional[Mapping]) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, Mapping) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        elif k in _LIST_EXTENDS:
            out[k] = list(out.get(k) or []) + list(v or [])
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_config(storage=None) -> dict:
    """Read the storage override doc; {} when absent or unreadable."""
    try:
        if storage is None:
            from framework.core.storage import get_storage
            storage = get_storage()
        doc = storage.read_json(CONFIG_KEY) or {}
        return doc if isinstance(doc, dict) else {}
    except Exception:
        return {}


def resolve_spec(spec: Optional[Mapping] = None, *, profile: str = "",
                 storage=None, config: Optional[Mapping] = None) -> dict:
    """Merge DEFAULT_SPEC ← config.defaults ← spec ← config.by_profile[profile].

    Pass `config` to skip the storage read (tests / offline use)."""
    cfg = dict(config) if config is not None else load_config(storage)
    out = _merge(DEFAULT_SPEC, cfg.get("defaults"))
    out = _merge(out, spec)
    if profile:
        out = _merge(out, (cfg.get("by_profile") or {}).get(profile))
    # Fold extra_* into their base lists so callers see one list.
    for extra, base in _LIST_EXTENDS.items():
        out[base] = list(dict.fromkeys(list(out.get(base) or [])
                                       + list(out.get(extra) or [])))
        out[extra] = []
    return out


# ── stale-browser threshold ────────────────────────────────────────────────

def estimated_current_major(anchor_major: int, anchor_date: str,
                            *, cadence_days: int = 28,
                            today: Optional[date] = None) -> int:
    """Extrapolate the current stable major from an anchor release."""
    today = today or datetime.now(timezone.utc).date()
    a = date.fromisoformat(str(anchor_date))
    elapsed = max(0, (today - a).days)
    return int(anchor_major) + elapsed // max(1, int(cadence_days))


def stale_thresholds(spec: Mapping, *, today: Optional[date] = None) -> dict[str, int]:
    """{engine: minimum acceptable major}. Empty when the rule is disabled."""
    sb = spec.get("stale_browser") or {}
    if not sb.get("enabled"):
        return {}
    out: dict[str, int] = {}
    for engine, anchor in (sb.get("anchors") or {}).items():
        try:
            major, when = anchor
            cur = estimated_current_major(int(major), str(when),
                                          cadence_days=int(sb.get("cadence_days", 28)),
                                          today=today)
            out[engine] = cur - int(sb.get("slack_majors", 16))
        except Exception:
            continue
    return out


# ── SQL builder ────────────────────────────────────────────────────────────

def _ident(name: Any, what: str) -> str:
    if not isinstance(name, str) or not _IDENT.match(name):
        raise ValueError(f"human_clicks: invalid {what} identifier {name!r}")
    return name


def _ua_regex(terms: list[str]) -> str:
    return "(" + "|".join(re.escape(t) for t in terms if t) + ")"


def _ip_regex(prefixes: list[str]) -> str:
    return "^(" + "|".join(re.escape(p) for p in prefixes if p) + ")"


_ENGINE_UA_PATTERN = {
    "chromium": r"Chrome/([0-9]{1,6})",
    "firefox": r"Firefox/([0-9]{1,6})",
}


def _reason_case(spec: Mapping, *, today: Optional[date] = None) -> tuple[str, list]:
    """CASE expression naming the first per-row rule that fires (NULL = passes)."""
    ua = _ident(spec.get("ua_col"), "ua_col")
    ip = _ident(spec.get("ip_col"), "ip_col")
    ip_txt = f"split_part(CAST({ip} AS text), '/', 1)"
    whens: list[str] = []
    params: list = []

    flag = spec.get("bot_flag_col")
    if flag:
        whens.append(f"WHEN COALESCE({_ident(flag, 'bot_flag_col')}, FALSE) THEN 'site-flagged-bot'")
    whens.append(f"WHEN COALESCE(TRIM({ua}), '') = '' THEN 'no-ua'")
    terms = list(spec.get("bot_ua_terms") or [])
    if terms:
        whens.append(f"WHEN {ua} ~* %s THEN 'ua-bot'")
        params.append(_ua_regex(terms))

    thresholds = stale_thresholds(spec, today=today)
    if thresholds:
        exempt = (spec.get("stale_browser") or {}).get("exempt_ua_regex") or ""
        stale_parts = []
        for engine, min_major in thresholds.items():
            pat = _ENGINE_UA_PATTERN.get(engine)
            if not pat:
                continue
            stale_parts.append(
                f"COALESCE(substring({ua} from %s)::int, 2147483647) < %s")
            params.extend([pat, int(min_major)])
        if stale_parts:
            cond = "(" + " OR ".join(stale_parts) + ")"
            if exempt:
                cond = f"({cond} AND {ua} !~* %s)"
                params.append(exempt)
            whens.append(f"WHEN {cond} THEN 'stale-browser'")

    country = spec.get("country_col")
    countries = [str(c).upper() for c in (spec.get("exclude_countries") or []) if c]
    if country and countries:
        whens.append(
            f"WHEN upper(COALESCE({_ident(country, 'country_col')}, '')) = ANY(%s) "
            f"THEN 'excluded-country'")
        params.append(countries)

    prefixes = list(spec.get("datacenter_ip_prefixes") or [])
    if prefixes:
        whens.append(f"WHEN {ip_txt} ~ %s THEN 'datacenter-ip'")
        params.append(_ip_regex(prefixes))

    ref = spec.get("referer_col")
    if ref:
        ref = _ident(ref, "referer_col")
        if spec.get("require_referer"):
            whens.append(f"WHEN COALESCE(TRIM({ref}), '') = '' THEN 'no-referer'")
        scan = spec.get("referer_exclude_regex")
        if scan:
            whens.append(f"WHEN COALESCE({ref}, '') ~* %s THEN 'scanner-referer'")
            params.append(scan)

    return "CASE " + " ".join(whens) + " ELSE NULL END", params


def build_breakdown_query(spec: Mapping, *, table: str, window_days: int,
                          match: Optional[Mapping[str, Any]] = None,
                          today: Optional[date] = None,
                          group_col: Optional[str] = None) -> tuple[str, list]:
    """SQL returning (verdict, count) rows for clicks in the window.

    `match` narrows the COUNTED rows ({"target": "amazon"}; a list value
    means IN). The velocity window deliberately spans ALL rows of the table
    in the window, so an IP spraying clicks across targets is still caught.

    `group_col` adds a second output column (verdict, <group value>, count):
    the same verdicts, split by e.g. the referer column so a caller can say
    WHICH pages produced the human clicks without a second definition.
    """
    tbl = _ident(table, "table")
    t = _ident(spec.get("time_col"), "time_col")
    ip = _ident(spec.get("ip_col"), "ip_col")
    reason, rparams = _reason_case(spec, today=today)
    match = dict(match or {})
    mcols = [_ident(c, "match column") for c in match]
    sel_match = "".join(f", {c}" for c in mcols)
    grp = _ident(group_col, "group_col") if group_col else ""
    if grp:
        sel_match += f", {grp} AS _grp"
    where_match: list[str] = []
    mparams: list = []
    for c, v in match.items():
        if isinstance(v, (list, tuple, set)):
            where_match.append(f"{c} = ANY(%s)")
            mparams.append(list(v))
        else:
            where_match.append(f"{c} = %s")
            mparams.append(v)
    max_ipd = int(spec.get("max_clicks_per_ip_day") or 0)
    velocity = (f"WHEN _per_ip_day > {max_ipd} THEN 'velocity' " if max_ipd > 0 else "")
    sql = (
        "WITH w AS ("
        f" SELECT split_part(CAST({ip} AS text), '/', 1) AS _ip, {t} AS _t,"
        f" {reason} AS _reason{sel_match}"
        f" FROM {tbl}"
        f" WHERE {t} > now() - make_interval(days => %s)"
        "), v AS ("
        " SELECT *, COUNT(*) FILTER (WHERE _reason IS NULL)"
        " OVER (PARTITION BY _ip, (_t AT TIME ZONE 'UTC')::date) AS _per_ip_day"
        " FROM w"
        ")"
        " SELECT CASE WHEN _reason IS NOT NULL THEN _reason "
        f"{velocity}ELSE '{VERDICT_HUMAN}' END AS verdict,"
        + (" _grp," if grp else "")
        + " COUNT(*)"
        " FROM v"
        + (" WHERE " + " AND ".join(where_match) if where_match else "")
        + (" GROUP BY 1, 2 ORDER BY 3 DESC" if grp else " GROUP BY 1 ORDER BY 2 DESC")
    )
    return sql, rparams + [int(window_days)] + mparams


def breakdown(conn, spec: Mapping, *, table: str, window_days: int = 30,
              match: Optional[Mapping[str, Any]] = None) -> dict[str, int]:
    """{verdict: count} for the window, plus "_total". "human" is the answer."""
    sql, params = build_breakdown_query(spec, table=table,
                                        window_days=window_days, match=match)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        # Tuple rows, or dict rows from a RealDictCursor connection.
        out = {str(r["verdict"] if isinstance(r, Mapping) else r[0]):
               int(r["count"] if isinstance(r, Mapping) else r[1])
               for r in cur.fetchall()}
    out.setdefault(VERDICT_HUMAN, 0)
    out["_total"] = sum(n for k, n in out.items() if k != "_total")
    return out


def count_human(conn, spec: Mapping, *, table: str, window_days: int = 30,
                match: Optional[Mapping[str, Any]] = None) -> int:
    """Verified-human click count for the window."""
    return breakdown(conn, spec, table=table, window_days=window_days,
                     match=match)[VERDICT_HUMAN]


def human_counts_by(conn, spec: Mapping, *, table: str, group_col: str,
                    window_days: int = 30,
                    match: Optional[Mapping[str, Any]] = None) -> dict[str, int]:
    """{<group_col value>: verified-human count} for the window, largest first.

    Same verdicts as :func:`breakdown`; NULL group values come back as "".
    Typical use: human clicks per referring page (`group_col="referer"`).
    """
    sql, params = build_breakdown_query(spec, table=table,
                                        window_days=window_days, match=match,
                                        group_col=group_col)
    out: dict[str, int] = {}
    with conn.cursor() as cur:
        cur.execute(sql, params)
        for row in cur.fetchall():
            verdict, grp, n = (row[k] for k in ("verdict", "_grp", "count")) \
                if isinstance(row, Mapping) else row
            if verdict == VERDICT_HUMAN:
                key = "" if grp is None else str(grp)
                out[key] = out.get(key, 0) + int(n)
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))
