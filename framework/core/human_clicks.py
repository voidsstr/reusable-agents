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
                     `slack_majors` behind the estimated current release,
                     or a Safari `Version/<major>.<minor>` below its
                     `min_versions` floor (default 17.3). Real browsers
                     auto-update; a hard-coded UA string in a scraper does
                     not. Browsers that legitimately lag (Samsung Internet,
                     UC, …) are exempt. A device that cannot update past an
                     old Safari sits on that major's final point release
                     (17.6 macOS Monterey: above the floor; 16.6 iPhone 8/X:
                     below it, measured at 0-2 real clicks in 120 days on
                     the reference deployments).
  excluded-country   country column is in `exclude_countries` (default SG —
                     the Singapore headless datacenter fleet that made up
                     92.8% of raw sessions in the 2026-09-23 growth memo)
  datacenter-ip      IP starts with a cloud / hosting prefix
  no-referer         `require_referer` and the click has no on-site referer
                     (a person reaches a redirect by clicking a link on one
                     of our pages; a crawler fetches it directly)
  self-referer       referer matches `self_referer_regex`: the "page" the
                     click came from is the click redirect itself (off by
                     default; the redirect path is a per-site value)
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

ATTRIBUTION (same verdicts, no second definition of "human")
-----------------------------------------------------------
  trend_by()                 human clicks per page template (regex on the
                             page column's path) x an optional group column
                             (e.g. placement): d7 / prev7 / d30, each window
                             evaluated exactly like breakdown(window_days=W)
  conversion_attribution()   the "what converts" brief section built on
                             trend_by(): template x group rows, per-template
                             and per-group totals, fastest-rising pages;
                             add_engagement() adds clicks per 100 engaged
                             sessions from a caller-supplied GA4 page map
                             (add_engagement_from_report() from the GA4
                             response itself; a failed or empty report
                             leaves engagement_unavailable, not zeros)
  human_rows() + before_after() + lever_outcomes()
                             verified-human rows over a span, and the counts
                             in the N days before vs after a pivot (a shipped
                             change) on its pages / placements, with the
                             site-wide counts for the same two windows

CLI: `python3 -m framework.cli.human_clicks --help` prints the same
breakdown for an operator.
"""
from __future__ import annotations

import copy
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import urlparse

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
    # Referer regex (POSIX, case-insensitive) naming the click endpoint
    # itself, e.g. "^(https?://[^/]+)?/k/" for a site whose buy links go
    # through /k/<slug>. A person clicks a link ON a page; a referer that is
    # the redirect is a script replaying it. Per-site value, so off here.
    "self_referer_regex": None,
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
        # Static "<major>.<minor>" floors, checked alongside `anchors`, for
        # engines whose numbering has no steady cadence. Safari reports its
        # own version as `Version/<major>.<minor>` (the OS token is frozen)
        # and jumped 18 -> 26 in 2025, so it cannot be extrapolated. Every
        # device that ran Safari 17.0-17.2 was offered 17.3+. The floor
        # closes the Safari third of a hard-coded 2024 UA list whose Chrome
        # 120-125 / Firefox 121-128 strings the anchors already catch:
        # measured 2026-11-11, six Safari 17.0-17.2 strings at exactly one
        # click per IP were 348 of 681 "human" clicks on one deployment, and
        # 0 of 1,351 on the other. Raise it, or blank it ("") for one
        # consumer, in storage config: no code change.
        "min_versions": {"safari": "17.3"},
        # How each engine's version is read: exactly ONE capture group of
        # digits (anchors) or digits and dots (min_versions). Data, so a
        # deployment can add an engine. Merged over _ENGINE_UA_PATTERN.
        "engine_ua_regex": {
            "chromium": r"Chrome/([0-9]{1,6})",
            "firefox": r"Firefox/([0-9]{1,6})",
            "safari": r"Version/([0-9]{1,3}\.[0-9]{1,3})(?:\.[0-9]{1,3})?"
                      r"(?: Mobile/[0-9A-Za-z]{1,12})? Safari/",
        },
        # UAs that match an engine's regex but are another engine: Android
        # WebView carries `Version/4.0 ... Chrome/`; iOS Chrome / Firefox /
        # Edge / Opera carry their own token (their Version/ is not Safari's).
        "engine_exclude_ua_regex": {
            "safari": r"Chrome/|Chromium/|CriOS/|FxiOS/|EdgiOS/|OPiOS/|Android",
        },
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


def version_floors(spec: Mapping) -> dict[str, list[int]]:
    """{engine: [major, minor, ...]} from stale_browser.min_versions.

    Empty when the rule is disabled. An empty or unparsable floor turns that
    engine off (so storage config can blank it for one consumer)."""
    sb = spec.get("stale_browser") or {}
    if not sb.get("enabled"):
        return {}
    out: dict[str, list[int]] = {}
    for engine, floor in (sb.get("min_versions") or {}).items():
        parts = str(floor if floor is not None else "").strip().split(".")
        if parts and all(p.isdigit() for p in parts):
            out[str(engine)] = [int(p) for p in parts]
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
    floors = version_floors(spec)
    if thresholds or floors:
        sb = spec.get("stale_browser") or {}
        exempt = sb.get("exempt_ua_regex") or ""
        patterns = {**_ENGINE_UA_PATTERN, **(sb.get("engine_ua_regex") or {})}
        excludes = sb.get("engine_exclude_ua_regex") or {}
        stale_parts = []

        def _engine_part(engine: str, pred: str, pred_params: list) -> None:
            # `pred` reads the version through the engine's one capture group;
            # an engine exclude (a UA that only LOOKS like this engine) wraps it.
            if excludes.get(engine):
                pred = f"({ua} !~* %s AND {pred})"
                pred_params = [excludes[engine]] + pred_params
            stale_parts.append(pred)
            params.extend(pred_params)

        for engine, min_major in thresholds.items():
            pat = patterns.get(engine)
            if not pat:
                continue
            _engine_part(engine,
                         f"COALESCE(substring({ua} from %s)::int, 2147483647) < %s",
                         [pat, int(min_major)])
        for engine, floor in floors.items():
            pat = patterns.get(engine)
            if not pat:
                continue
            # int[] comparison is element-wise: {17,1} < {17,3} < {17,6}.
            _engine_part(engine,
                         f"COALESCE(string_to_array(substring({ua} from %s), '.')::int[]"
                         f" < %s::int[], FALSE)",
                         [pat, list(floor)])
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
        self_ref = spec.get("self_referer_regex")
        if self_ref:
            whens.append(f"WHEN COALESCE({ref}, '') ~* %s THEN 'self-referer'")
            params.append(str(self_ref))
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


def human_click_paths(conn, spec: Mapping, *, table: str, group_col: str,
                      path_template: str = "{value}", window_days: int = 30,
                      match: Optional[Mapping[str, Any]] = None,
                      limit: int = 50) -> list[dict]:
    """The site pages verified-human clicks came from, best first:
    ``[{"path", "clicks"}]``.

    Each `group_col` value is rendered through `path_template` ('{value}'
    substituted — e.g. "/product/{value}" for an id column). A value that is
    a full URL (a referer column) contributes its path; query strings and
    fragments are dropped and equal paths merged.
    """
    from urllib.parse import urlparse
    merged: dict[str, int] = {}
    for value, n in human_counts_by(conn, spec, table=table, group_col=group_col,
                                    window_days=window_days, match=match).items():
        if not value or n <= 0:
            continue
        raw = path_template.replace("{value}", value)
        parsed = urlparse(raw)
        path = parsed.path if parsed.scheme else raw.split("?", 1)[0].split("#", 1)[0]
        if not path.startswith("/"):
            continue
        merged[path] = merged.get(path, 0) + n
    rows = sorted(merged.items(), key=lambda kv: (-kv[1], kv[0]))[:max(0, int(limit))]
    return [{"path": p, "clicks": n} for p, n in rows]


# ── attribution: templates x groups, trends, before/after ──────────────────
#
# Everything below reuses _reason_case() — the one definition of "human" —
# and the same per-IP-per-UTC-day velocity rule as breakdown(). The page
# column is reduced to its PATH (scheme, host, query and fragment dropped),
# so a referer that holds full URLs and a column that holds paths classify
# the same way.

TEMPLATE_OTHER = "other"
TEMPLATE_ALL = "all"

_PATH_STRIP_RE = re.compile(r"^[a-z][a-z0-9+.-]*://[^/]*", re.I)


def _path_sql(col: str) -> str:
    """SQL: the path part of a referer/page column (no scheme/host/query)."""
    return (f"split_part(split_part(regexp_replace(COALESCE(CAST({col} AS text), ''), "
            f"'^[a-z][a-z0-9+.-]*://[^/]*', '', 'i'), '?', 1), '#', 1)")


def page_path(value: Any) -> str:
    """Python mirror of _path_sql: 'https://x.test/blog/a?u=1#t' -> '/blog/a'."""
    s = "" if value is None else str(value)
    s = _PATH_STRIP_RE.sub("", s, count=1)
    return s.split("?", 1)[0].split("#", 1)[0]


def _templates(templates: Optional[Iterable[Any]]) -> list[tuple[str, str]]:
    """[(name, pattern)] from [{"name", "pattern"}] (or (name, pattern)
    pairs); entries without both are dropped."""
    out: list[tuple[str, str]] = []
    for t in templates or []:
        if isinstance(t, Mapping):
            name, pat = t.get("name"), t.get("pattern")
        elif isinstance(t, (list, tuple)) and len(t) == 2:
            name, pat = t
        else:
            continue
        name, pat = str(name or "").strip(), str(pat or "")
        if name and pat:
            out.append((name, pat))
    return out


def classify_path(value: Any, templates: Optional[Iterable[Mapping]]) -> str:
    """The template a page belongs to — Python mirror of trend_by's SQL CASE
    (first match wins, case-insensitive, on the path). For classifying pages
    that come from elsewhere (e.g. GA4 landing pages) the same way."""
    tpls = _templates(templates)
    if not tpls:
        return TEMPLATE_ALL
    path = page_path(value)
    for name, pat in tpls:
        try:
            if re.search(pat, path, re.I):
                return name
        except re.error:
            continue
    return TEMPLATE_OTHER


def _windows(windows: Iterable[Any]) -> list[int]:
    out = sorted({int(w) for w in (windows or []) if int(w) > 0})
    if not out:
        raise ValueError("human_clicks: at least one positive window is required")
    return out


def _match_sql(match: Optional[Mapping[str, Any]]) -> tuple[list[str], list[str], list]:
    """(columns, WHERE terms, params) for a {"col": value | [values]} match."""
    cols, where, params = [], [], []
    for c, v in dict(match or {}).items():
        c = _ident(c, "match column")
        cols.append(c)
        if isinstance(v, (list, tuple, set)):
            where.append(f"{c} = ANY(%s)")
            params.append(list(v))
        else:
            where.append(f"{c} = %s")
            params.append(v)
    return cols, where, params


def build_trend_query(spec: Mapping, *, table: str, windows: Iterable[int] = (7, 14, 30),
                      match: Optional[Mapping[str, Any]] = None,
                      group_col: Optional[str] = None,
                      templates: Optional[Iterable[Mapping]] = None,
                      page_col: Optional[str] = None, group_as_path: bool = False,
                      today: Optional[date] = None) -> tuple[str, list]:
    """SQL returning (template_index, group, h_<W>...) for trend_by().

    Each window's human count is computed the way breakdown(window_days=W)
    computes it, including a per-window velocity count, so trend_by's d30
    summed over its rows equals breakdown(window_days=30)["human"].
    template_index is the 0-based index of the first matching template, or
    -1 (no template matched / no templates)."""
    tbl = _ident(table, "table")
    t = _ident(spec.get("time_col"), "time_col")
    ip = _ident(spec.get("ip_col"), "ip_col")
    wins = _windows(windows)
    tpls = _templates(templates)
    reason, rparams = _reason_case(spec, today=today)
    mcols, mwhere, mparams = _match_sql(match)

    tparams: list = []
    if tpls:
        page = page_col or spec.get("referer_col")
        if not page:
            raise ValueError("human_clicks: templates need a page_col (or a spec referer_col)")
        path = _path_sql(_ident(page, "page_col"))
        tmpl = "CASE " + " ".join(f"WHEN {path} ~* %s THEN {i}"
                                  for i in range(len(tpls))) + " ELSE -1 END"
        tparams = [pat for _, pat in tpls]
    else:
        tmpl = "-1"
    if group_col:
        g = _ident(group_col, "group_col")
        grp = _path_sql(g) if group_as_path else f"CAST({g} AS text)"
    else:
        grp = "NULL::text"

    max_ipd = int(spec.get("max_clicks_per_ip_day") or 0)
    vcols, hcols, vparams, hparams = [], [], [], []
    for i, w in enumerate(wins):
        recent = "_t > now() - make_interval(days => %s)"
        if max_ipd > 0:
            vcols.append(f"COUNT(*) FILTER (WHERE _reason IS NULL AND {recent})"
                         f" OVER (PARTITION BY _ip, (_t AT TIME ZONE 'UTC')::date) AS _v{i}")
            vparams.append(w)
            hcols.append(f"COUNT(*) FILTER (WHERE _reason IS NULL AND {recent}"
                         f" AND _v{i} <= {max_ipd}) AS h{i}")
        else:
            hcols.append(f"COUNT(*) FILTER (WHERE _reason IS NULL AND {recent}) AS h{i}")
        hparams.append(w)
    sel_match = "".join(f", {c}" for c in mcols)
    sql = (
        "WITH w AS ("
        f" SELECT split_part(CAST({ip} AS text), '/', 1) AS _ip, {t} AS _t,"
        f" {reason} AS _reason{sel_match}, {tmpl} AS _tmpl, {grp} AS _grp"
        f" FROM {tbl}"
        f" WHERE {t} > now() - make_interval(days => %s)"
        "), v AS ("
        " SELECT *" + "".join(f", {c}" for c in vcols) + " FROM w"
        ")"
        " SELECT _tmpl, _grp, " + ", ".join(hcols) +
        " FROM v"
        + (" WHERE " + " AND ".join(mwhere) if mwhere else "")
        + " GROUP BY 1, 2"
    )
    return sql, rparams + tparams + [max(wins)] + vparams + hparams + mparams


def _row_values(row: Any, n: int) -> list:
    if isinstance(row, Mapping):
        return list(row.values())[:n]
    return list(row)[:n]


def _pct(cur: int, prev: int) -> Optional[int]:
    return round((cur - prev) * 100.0 / prev) if prev else None


def trend_by(conn, spec: Mapping, *, table: str,
             match: Optional[Mapping[str, Any]] = None,
             group_col: Optional[str] = None,
             templates: Optional[Iterable[Mapping]] = None,
             windows: Iterable[int] = (7, 14, 30),
             page_col: Optional[str] = None, group_as_path: bool = False,
             today: Optional[date] = None) -> list[dict]:
    """Verified-human clicks per page template x group over trailing windows.

    `templates` = [{"name": ..., "pattern": <POSIX regex>}], matched
    case-insensitively against the PATH of `page_col` (default: the spec's
    referer_col); first match wins, unmatched rows are "other". No templates
    → every row is "all". `group_col` adds a second split (e.g. a placement
    column; `group_as_path` reduces it to a path, for per-page trends).

    Returns [{"template", "group", "d<W>"..., "prev<W0>", "wow_pct"}] for
    every (template, group) with at least one human click in the widest
    window, most recent clicks first. "prev<W0>" (= d<2·W0> − d<W0>, e.g.
    prev7 = the 7 days before the last 7) is present when 2·W0 is also a
    window; "wow_pct" compares d<W0> with it."""
    wins = _windows(windows)
    tpls = _templates(templates)
    sql, params = build_trend_query(spec, table=table, windows=wins, match=match,
                                    group_col=group_col, templates=tpls,
                                    page_col=page_col, group_as_path=group_as_path,
                                    today=today)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        fetched = cur.fetchall()
    w0 = wins[0]
    prev_key = f"prev{w0}" if (2 * w0) in wins else ""
    out: list[dict] = []
    for row in fetched:
        vals = _row_values(row, 2 + len(wins))
        idx, grp, counts = vals[0], vals[1], [int(x or 0) for x in vals[2:]]
        if not any(counts):
            continue
        idx = int(idx) if idx is not None else -1
        rec: dict = {
            "template": (tpls[idx][0] if 0 <= idx < len(tpls)
                         else (TEMPLATE_OTHER if tpls else TEMPLATE_ALL)),
            "group": "" if grp is None else str(grp),
        }
        for w, n in zip(wins, counts):
            rec[f"d{w}"] = n
        if prev_key:
            rec[prev_key] = rec[f"d{2 * w0}"] - rec[f"d{w0}"]
            rec["wow_pct"] = _pct(rec[f"d{w0}"], rec[prev_key])
        out.append(rec)
    big = f"d{wins[-1]}"
    out.sort(key=lambda r: (-r[f"d{w0}"], -r[big], r["template"], r["group"]))
    return out


def build_rows_query(spec: Mapping, *, table: str, window_days: int,
                     match: Optional[Mapping[str, Any]] = None,
                     page_col: Optional[str] = None,
                     group_col: Optional[str] = None,
                     today: Optional[date] = None, limit: int = 200_000) -> tuple[str, list]:
    """SQL returning (time, page path, group) for every verified-human row."""
    tbl = _ident(table, "table")
    t = _ident(spec.get("time_col"), "time_col")
    ip = _ident(spec.get("ip_col"), "ip_col")
    reason, rparams = _reason_case(spec, today=today)
    mcols, mwhere, mparams = _match_sql(match)
    page = page_col or spec.get("referer_col")
    path = _path_sql(_ident(page, "page_col")) if page else "''"
    grp = f"CAST({_ident(group_col, 'group_col')} AS text)" if group_col else "NULL::text"
    max_ipd = int(spec.get("max_clicks_per_ip_day") or 0)
    where = ["_reason IS NULL"] + ([f"_per_ip_day <= {max_ipd}"] if max_ipd > 0 else []) + mwhere
    sel_match = "".join(f", {c}" for c in mcols)
    sql = (
        "WITH w AS ("
        f" SELECT split_part(CAST({ip} AS text), '/', 1) AS _ip, {t} AS _t,"
        f" {reason} AS _reason{sel_match}, {path} AS _path, {grp} AS _grp"
        f" FROM {tbl}"
        f" WHERE {t} > now() - make_interval(days => %s)"
        "), v AS ("
        " SELECT *, COUNT(*) FILTER (WHERE _reason IS NULL)"
        " OVER (PARTITION BY _ip, (_t AT TIME ZONE 'UTC')::date) AS _per_ip_day"
        " FROM w"
        ")"
        " SELECT _t, _path, _grp FROM v WHERE " + " AND ".join(where) +
        f" ORDER BY _t LIMIT {max(1, int(limit))}"
    )
    return sql, rparams + [int(window_days)] + mparams


def _as_utc(v: Any) -> Optional[datetime]:
    """datetime / ISO string -> aware UTC datetime (None when unparsable)."""
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        d = v
    else:
        try:
            d = datetime.fromisoformat(str(v).strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)


def human_rows(conn, spec: Mapping, *, table: str, window_days: int,
               match: Optional[Mapping[str, Any]] = None,
               page_col: Optional[str] = None, group_col: Optional[str] = None,
               limit: int = 200_000) -> list[dict]:
    """Every verified-human click in the window: [{"t", "path", "group"}],
    oldest first (velocity evaluated per UTC day inside the window)."""
    sql, params = build_rows_query(spec, table=table, window_days=window_days,
                                   match=match, page_col=page_col,
                                   group_col=group_col, limit=limit)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        fetched = cur.fetchall()
    out = []
    for row in fetched:
        t, path, grp = _row_values(row, 3)
        out.append({"t": _as_utc(t), "path": "" if path is None else str(path),
                    "group": "" if grp is None else str(grp)})
    return out


def _values(v: Any) -> list:
    """A filter argument as a list: None/mapping -> [], a bare string or
    scalar -> [it], any other iterable -> list(it). LLM-written fields
    (target_urls: 5) must not raise."""
    if v is None or isinstance(v, Mapping):
        return []
    if isinstance(v, (str, bytes, int, float)):
        return [v]
    try:
        return list(v)
    except TypeError:
        return [v]


def _like_matcher(values: Optional[Iterable[Any]], *, as_path: bool):
    """Predicate for exact values or 'prefix*' patterns; None = no filter."""
    exact, prefixes = set(), []
    for v in _values(values):
        if isinstance(v, (Mapping, list, tuple, set)):
            continue
        s = page_path(v) if as_path else str(v or "").strip().lower()
        if not s:
            continue
        if s.endswith("*"):
            prefixes.append(s[:-1])
        else:
            exact.add(s.rstrip("/") if as_path else s)
    if not exact and not prefixes:
        return None

    def ok(value: str) -> bool:
        s = (value or "") if as_path else (value or "").lower()
        return (s.rstrip("/") if as_path else s) in exact or any(
            s.startswith(p) for p in prefixes)
    return ok


def before_after(rows: Iterable[Mapping], *, pivot: Any, days: int = 7,
                 pages: Optional[Iterable[Any]] = None,
                 groups: Optional[Iterable[Any]] = None,
                 now: Optional[datetime] = None) -> dict:
    """Clicks in the `days` before vs after `pivot`, from human_rows().

    `pages`: exact paths (or full URLs) and 'prefix*' patterns; `groups`:
    exact group values (e.g. placements) and 'prefix*' patterns. A row
    counts when it matches both filters given. site_before / site_after are
    the same windows with no filter, so a lever's change can be read against
    the site's own trend. The after window ends at `now`: after_days < days
    while the pivot is recent.

    With a `groups` filter the result also carries pages_before /
    pages_after (the page filter alone, every group; the site counts when no
    page filter is given) and pages_scoped: a lever that ADDS a placement
    has 0 clicks there before, so rec_memory.lever_verdict judges it on its
    pages' clicks across all placements instead."""
    pv = _as_utc(pivot)
    now = _as_utc(now) or datetime.now(timezone.utc)
    page_ok = _like_matcher(pages, as_path=True)
    grp_ok = _like_matcher(groups, as_path=False)
    out = {"before": 0, "after": 0, "site_before": 0, "site_after": 0,
           "days": int(days), "after_days": 0.0,
           "scoped": bool(page_ok or grp_ok)}
    if grp_ok is not None:
        out.update({"pages_before": 0, "pages_after": 0, "pages_scoped": page_ok is not None})
    if pv is None:
        return out
    start, end = pv - timedelta(days=days), min(pv + timedelta(days=days), now)
    out["after_days"] = round(max(0.0, (end - pv).total_seconds() / 86400.0), 2)
    for r in rows:
        t = _as_utc(r.get("t"))
        if t is None or t < start or t >= end:
            continue
        side = "before" if t < pv else "after"
        out[f"site_{side}"] += 1
        on_page = page_ok is None or page_ok(str(r.get("path") or ""))
        if on_page and grp_ok is not None:
            out[f"pages_{side}"] += 1
        if on_page and (grp_ok is None or grp_ok(str(r.get("group") or ""))):
            out[side] += 1
    return out


def lever_outcomes(conn, spec: Mapping, *, table: str, levers: list[dict],
                   match: Optional[Mapping[str, Any]] = None,
                   page_col: Optional[str] = None, group_col: Optional[str] = None,
                   days: int = 7, now: Optional[datetime] = None) -> list[dict]:
    """Attach `outcome` (before_after on the lever's target_urls and, when the
    table has a group column, its placements) to every lever with a
    `live_since`; one human_rows() query covers them all. Levers without a
    live_since get outcome None. Returns `levers` (mutated in place)."""
    now = _as_utc(now) or datetime.now(timezone.utc)
    pivots = [(lev, _as_utc(lev.get("live_since"))) for lev in levers]
    live = [p for _, p in pivots if p is not None]
    if not live:
        for lev in levers:
            lev["outcome"] = None
        return levers
    span = max(1, (now - min(live)).days + int(days) + 1)
    rows = human_rows(conn, spec, table=table, window_days=span, match=match,
                      page_col=page_col, group_col=group_col)
    for lev, pv in pivots:
        if pv is None:
            lev["outcome"] = None
            continue
        placements = [p for p in _values(lev.get("placements")) if str(p or "").strip()]
        res = before_after(rows, pivot=pv, days=days,
                           pages=_values(lev.get("target_urls")) or None,
                           groups=(placements or None) if group_col else None,
                           now=now)
        if placements and not group_col:
            res["placements_ignored"] = True
        lev["outcome"] = res
    return levers


def conversion_attribution(conn, spec: Mapping, *, table: str,
                           templates: Optional[Iterable[Mapping]] = None,
                           match: Optional[Mapping[str, Any]] = None,
                           group_col: Optional[str] = None,
                           page_col: Optional[str] = None,
                           rows_limit: int = 30, rising_limit: int = 12) -> dict:
    """The "what converts" brief section: verified-human clicks by page
    template x group (e.g. placement), d7 / prev7 / d30, plus per-template
    and per-group totals and the fastest-rising pages (d7 vs prev7).

    Two trend_by() queries; the totals equal breakdown()'s human counts for
    7, 14 and 30 days. Add engaged-session rates with add_engagement()."""
    tpls = _templates(templates)
    cells = trend_by(conn, spec, table=table, match=match, group_col=group_col,
                     templates=tpls, windows=(7, 14, 30), page_col=page_col)
    pages = trend_by(conn, spec, table=table, match=match,
                     group_col=page_col or spec.get("referer_col"),
                     group_as_path=True, windows=(7, 14, 30))

    def _roll(key: str) -> list[dict]:
        acc: dict[str, dict] = {}
        for c in cells:
            a = acc.setdefault(c[key], {key: c[key], "d7": 0, "prev7": 0, "d30": 0})
            for k in ("d7", "prev7", "d30"):
                a[k] += c[k]
        out = sorted(acc.values(), key=lambda r: (-r["d7"], -r["d30"], r[key]))
        for r in out:
            r["wow_pct"] = _pct(r["d7"], r["prev7"])
        return out

    totals = {k: sum(c[k] for c in cells) for k in ("d7", "prev7", "d30")}
    totals["wow_pct"] = _pct(totals["d7"], totals["prev7"])
    for c in cells:
        c["share_d7_pct"] = round(c["d7"] * 100.0 / totals["d7"], 1) if totals["d7"] else 0.0
    rising = sorted((p for p in pages if p["d7"] > p["prev7"]),
                    key=lambda p: (-(p["d7"] - p["prev7"]), -p["d7"], p["group"]))
    out = {
        "definition": ("verified-human clicks (framework/core/human_clicks.py verdicts, "
                       "the goal definition); d7 = last 7 days, prev7 = the 7 days "
                       "before that, d30 = last 30 days"),
        "group_col": group_col or None,
        "templates": [{"name": n, "pattern": p} for n, p in tpls],
        "totals": totals,
        "by_template": _roll("template"),
        "rows": [{k: c[k] for k in ("template", "group", "d7", "prev7", "d30",
                                    "wow_pct", "share_d7_pct")}
                 for c in cells[:max(0, int(rows_limit))]],
        "rising_pages": [{"page": p["group"], "d7": p["d7"], "prev7": p["prev7"],
                          "d30": p["d30"], "delta": p["d7"] - p["prev7"]}
                         for p in rising[:max(0, int(rising_limit))]],
    }
    if group_col:
        out["by_group"] = _roll("group")
    return out


def ga4_engaged_sessions_request(*, exclude_sources: Iterable[str] = (),
                                 exclude_countries: Iterable[str] = (),
                                 days: int = 30, limit: int = 10000) -> dict:
    """GA4 Data API runReport body for add_engagement()'s denominator:
    engaged sessions and average session duration per landing page x
    channel. Sources containing any of `exclude_sources` (referral bots) and
    `exclude_countries` (pass the click spec's exclude_countries, so both
    sides of the ratio drop the same traffic) are filtered out server-side.
    No HTTP here: the caller posts it with its own token."""
    drop = [{"filter": {"fieldName": "sessionSource",
                        "stringFilter": {"matchType": "CONTAINS", "value": str(s)}}}
            for s in exclude_sources if s]
    drop += [{"filter": {"fieldName": "countryId", "stringFilter": {"value": str(c).upper()}}}
             for c in exclude_countries if c]
    body: dict = {
        "dateRanges": [{"startDate": f"{int(days)}daysAgo", "endDate": "today"}],
        "dimensions": [{"name": "landingPage"}, {"name": "sessionDefaultChannelGroup"}],
        "metrics": [{"name": "engagedSessions"}, {"name": "averageSessionDuration"}],
        "metricFilter": {"filter": {"fieldName": "engagedSessions", "numericFilter": {
            "operation": "GREATER_THAN", "value": {"int64Value": "0"}}}},
        "orderBys": [{"metric": {"metricName": "engagedSessions"}, "desc": True}],
        "limit": int(limit),
    }
    if drop:
        body["dimensionFilter"] = {"notExpression": {"orGroup": {"expressions": drop}}}
    return body


def engaged_by_page(report: Optional[Mapping], *, min_avg_session_s: float = 10.0) -> dict[str, float]:
    """{landing page: engaged sessions} from a ga4_engaged_sessions_request()
    response, skipping (page, channel) cells whose sessions average under
    `min_avg_session_s`. A GA4-running bot fleet registers as "engaged" at
    1-5 s a session: measured 2026-11-11, one site's /chat drew 9,538
    "engaged" Direct sessions at 5.3 s on average, against 71 s for the
    Direct readers of its buying guides."""
    out: dict[str, float] = {}
    if not isinstance(report, Mapping) or report.get("error"):
        return out
    for r in report.get("rows") or []:
        dims = [d.get("value", "") for d in r.get("dimensionValues") or []]
        mets = [m.get("value", "") for m in r.get("metricValues") or []]
        if not dims or not mets:
            continue
        try:
            engaged = float(mets[0] or 0)
            avg = float(mets[1]) if len(mets) > 1 else None
        except (TypeError, ValueError):
            continue
        if avg is not None and avg < float(min_avg_session_s):
            continue
        out[dims[0]] = out.get(dims[0], 0.0) + engaged
    return out


def add_engagement(attribution: dict, engaged_by_page: Optional[Mapping[str, Any]], *,
                   min_engaged: int = 25, source: str = "GA4 engagedSessions, 30d") -> dict:
    """Add clicks per 100 engaged sessions per template to a
    conversion_attribution() result, from {page path: engaged sessions}
    (e.g. GA4 landingPage x engagedSessions). Pages are classified with the
    same templates. Templates with >= `min_engaged` sessions and no human
    click in 30 days are listed in `zero_click_high_attention`.

    No engaged sessions at all (the GA4 report failed or came back empty)
    adds no rates: the result gets `engagement_unavailable` instead, so a
    brief never reads "engaged=0" on every template as if it were data."""
    if not isinstance(attribution, dict) or attribution.get("error"):
        return attribution
    tpls = attribution.get("templates") or []
    engaged: dict[str, float] = {}
    items = engaged_by_page.items() if isinstance(engaged_by_page, Mapping) else ()
    for page, n in items:
        try:
            v = float(n or 0)
        except (TypeError, ValueError):
            continue
        if v <= 0:
            continue
        name = classify_path(page, tpls)
        engaged[name] = engaged.get(name, 0.0) + v
    if not engaged:
        attribution.setdefault("engagement_unavailable",
                               "no engaged-session rows (the GA4 report failed or came back empty)")
        return attribution
    attribution.pop("engagement_unavailable", None)
    rows = {r["template"]: r for r in attribution.get("by_template") or []}
    for name, v in engaged.items():
        rows.setdefault(name, {"template": name, "d7": 0, "prev7": 0, "d30": 0,
                               "wow_pct": None})
    for name, r in rows.items():
        e = int(round(engaged.get(name, 0.0)))
        r["engaged_sessions_30d"] = e
        r["clicks_per_100_engaged"] = round(r["d30"] * 100.0 / e, 1) if e else None
    attribution["by_template"] = sorted(
        rows.values(), key=lambda r: (-r["d7"], -r["d30"], -r["engaged_sessions_30d"],
                                      r["template"]))
    total_e = sum(int(round(v)) for v in engaged.values())
    d30 = (attribution.get("totals") or {}).get("d30") or 0
    attribution["engagement_source"] = source
    attribution["site_clicks_per_100_engaged"] = round(d30 * 100.0 / total_e, 1) if total_e else None
    attribution["zero_click_high_attention"] = [
        {"template": r["template"], "engaged_sessions_30d": r["engaged_sessions_30d"]}
        for r in attribution["by_template"]
        if r["d30"] == 0 and r["engaged_sessions_30d"] >= int(min_engaged)]
    return attribution


def add_engagement_from_report(attribution: dict, report: Optional[Mapping], *,
                               min_avg_session_s: float = 10.0, min_engaged: int = 25,
                               source: str = "GA4 engagedSessions, 30d") -> dict:
    """add_engagement() straight from a ga4_engaged_sessions_request()
    response (engaged_by_page() in between). A report that carries an error
    sets `engagement_unavailable` to that error and adds no rates."""
    if not isinstance(attribution, dict) or attribution.get("error"):
        return attribution
    if isinstance(report, Mapping) and report.get("error"):
        attribution["engagement_unavailable"] = (
            "GA4 engaged-sessions report failed: " + str(report.get("error"))[:200])
        return attribution
    return add_engagement(attribution,
                          engaged_by_page(report, min_avg_session_s=min_avg_session_s),
                          min_engaged=min_engaged, source=source)


def focus_rows(attribution: Optional[Mapping], *, top: int = 4, rising: int = 2,
               min_d7: int = 5) -> list[dict]:
    """The template x group rows a conversion lever should work on: the `top`
    rows with the most clicks in the last 7 days, plus up to `rising` of the
    fastest-growing others (largest d7 - prev7 gain, at least `min_d7` clicks
    in 7 days). Data-driven, so no placement name is hard-coded anywhere."""
    rows = [r for r in (attribution or {}).get("rows") or [] if int(r.get("d7") or 0) > 0]
    picked = rows[:max(0, int(top))]
    seen = {(r.get("template"), r.get("group")) for r in picked}
    growers = sorted(
        (r for r in rows if (r.get("template"), r.get("group")) not in seen
         and int(r.get("d7") or 0) >= int(min_d7)
         and int(r.get("d7") or 0) > int(r.get("prev7") or 0)),
        key=lambda r: (-(int(r.get("d7") or 0) - int(r.get("prev7") or 0)), -int(r.get("d7") or 0)))
    return picked + growers[:max(0, int(rising))]


def attribution_prompt_block(attribution: Optional[Mapping], *, max_rows: int = 24,
                             max_rising: int = 10) -> str:
    """Compact text rendering of conversion_attribution() for an LLM brief:
    one row per line, biggest last-7-day counts first."""
    a = attribution or {}
    if not a:
        return "(no attribution data)"
    if a.get("error"):
        return f"(attribution unavailable: {a['error']})"

    def _w(r: Mapping) -> str:
        wow = r.get("wow_pct")
        return (f"d7={r.get('d7', 0)} prev7={r.get('prev7', 0)} d30={r.get('d30', 0)}"
                + (f" ({'+' if wow >= 0 else ''}{wow}% wow)" if isinstance(wow, int) else ""))

    lines = [f"Definition: {a.get('definition', '')}"]
    tot = a.get("totals") or {}
    lines.append(f"TOTAL {_w(tot)}")
    grp = a.get("group_col")
    focus = focus_rows(a)
    if focus:
        lines.append("FOCUS ROWS (top-converting + fastest-growing, last 7 days): " + "; ".join(
            f"{r.get('template')}" + (f" x {r.get('group') or '(none)'}" if grp else "")
            + f" d7={r.get('d7')} prev7={r.get('prev7')}" for r in focus))
    if a.get("engagement_unavailable"):
        lines.append(f"Clicks per 100 engaged sessions: unavailable this run "
                     f"({a['engagement_unavailable']}).")
    lines.append("By template" + (" (clicks per 100 engaged sessions, "
                                  f"{a.get('engagement_source')}; site "
                                  f"{a.get('site_clicks_per_100_engaged')})"
                                  if a.get("engagement_source") else "") + ":")
    for r in a.get("by_template") or []:
        rate = ""
        if "engaged_sessions_30d" in r:
            rate = (f" engaged={r['engaged_sessions_30d']}"
                    f" per100={r.get('clicks_per_100_engaged')}")
        lines.append(f"  {r.get('template')}: {_w(r)}{rate}")
    if grp and a.get("by_group"):
        lines.append(f"By {grp}:")
        for r in a["by_group"][:max_rows]:
            lines.append(f"  {r.get('group') or '(none)'}: {_w(r)}")
    if grp:
        lines.append(f"Template x {grp} (top {max_rows}):")
        for r in (a.get("rows") or [])[:max_rows]:
            lines.append(f"  {r.get('template')} x {r.get('group') or '(none)'}: {_w(r)}"
                         f" share_d7={r.get('share_d7_pct')}%")
    zero = a.get("zero_click_high_attention") or []
    if zero:
        lines.append("High-attention templates with ZERO human clicks in 30d: "
                     + ", ".join(f"{z['template']} ({z['engaged_sessions_30d']} engaged)"
                                 for z in zero))
    rising = (a.get("rising_pages") or [])[:max_rising]
    if rising:
        lines.append("Fastest-rising pages (d7 vs prev7):")
        for p in rising:
            lines.append(f"  {p['page']}: d7={p['d7']} prev7={p['prev7']} d30={p['d30']}")
    return "\n".join(lines)
