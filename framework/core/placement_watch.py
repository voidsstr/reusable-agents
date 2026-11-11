"""Placement-level conversion watch: did a deploy cost verified-human clicks
on a money placement?

WHY THIS EXISTS
---------------
2026-11: one reference site's verified-human Amazon clicks went from ~40 to
~690 a week on a build that shipped on 10-25, 62% of them from one
placement (the product-page hero CTA). From 11-08 the agents stacked 69
revisions and 180 commits on that build, 61 of them on price, CTA or hero
surfaces, and nothing checked whether any of them cost clicks. The deploy
gate proves pages render; it cannot see conversion.

WHAT IT DOES
------------
For the newest deploy that has at least `min_window_h` full hours of clicks
after it (the deployer's gate-state deploy log, GateState.deploy_log()), it
counts verified-human clicks per money placement in the window after the
deploy (up to `max_window_h`) and on the same clock hours of each of the
`baseline_days` days before it. A placement is flagged when the window is at
least `drop_threshold` below the baseline mean AND the drop is unlikely to be
noise: z = (mean - window) / sqrt(max(mean, sample variance of the baseline
windows)) >= `min_z`. Since the spread is at least sqrt(mean), z can never
pass sqrt(mean), so a placement whose baseline mean is under min_z**2 (6.25
at min_z 2.5) could not be flagged even at zero clicks: it is reported as
`sparse` instead of a misleading `ok`. `min_baseline_clicks` can raise that
floor. Just above it only a near-total collapse flags.

A flagged placement opens ONE incident (framework/placement-watch/<site>.json)
and the operator gets ONE email listing the deploys in the window; the
incident closes when a later evaluation finds the placement back in range.
Nothing is paused automatically: the email says how to pause money-surface
work if the operator wants to.

"Verified human" is framework/core/human_clicks.py's definition (the same
per-row verdicts and per-IP velocity rule, through its storage config), so the
counts match the goals and the growth briefs.

Everything per site is DATA (storage config `config/placement-watch-config.json`):

    {
      "schema_version": "1",
      "defaults": {"min_window_h": 24, "drop_threshold": 0.3, ...},
      "sites": {
        "<site>": {
          "table": "<click table>",
          "placement_col": "<column naming the CTA>",       # or placement_from_referer
          "placement_from_referer": [{"value": "guide", "pattern": "^(https?://[^/]+)?/blog/best-"}],
          "human_clicks_profile": "<profile for human_clicks overrides>",
          "spec": {"time_col": "...", "referer_col": "...", "bot_flag_col": "...", ...},
          "match": {"target": "amazon", "via": "beacon"},
          "money_placements": {"<name>": ["<raw value>", ...], ...}
        }
      }
    }

A site with no entry is not watched. Consumers:
  * agents/deployer/deployer.py starts `python3 -m framework.core.placement_watch
    --site <site> --live --min-interval-m 55` in the background after every
    deployer run, red or green. A busy site gets an evaluation every hour or
    few, so a drop is flagged within the day (hourly evaluation flags a 50%
    pdp-hero drop 10-19 h after it starts).
  * agents/site-goals-tracker runs watch_site() daily after its metrics: the
    backstop when nothing has gone through the deployer.
Without --live the CLI prints an evaluation and never writes or emails.
"""
from __future__ import annotations

import argparse
import copy
import html as _html
import math
import os
import re
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping, Optional

from . import human_clicks

CONFIG_KEY = "config/placement-watch-config.json"
STATE_PREFIX = "framework/placement-watch/"
TOTAL = "_total"

DEFAULTS: dict[str, Any] = {
    # A deploy is judged once this many full hours of clicks follow it...
    "min_window_h": 24,
    # ...over at most this many hours after it.
    "max_window_h": 48,
    # Deploys older than this are not judged any more.
    "anchor_max_age_h": 72,
    # Baseline: the same clock hours on this many earlier days.
    "baseline_days": 7,
    # Flag when the window is this far below the baseline mean...
    "drop_threshold": 0.30,
    # ...and this many standard deviations below it (Poisson or the
    # baseline's own spread, whichever is wider).
    "min_z": 2.5,
    # Placements with a smaller baseline mean per window are not judged. The
    # floor is never below min_z**2: under it even zero clicks cannot reach
    # min_z, so 0 here means exactly that floor.
    "min_baseline_clicks": 0,
    # Closed incidents kept in the state blob.
    "max_incidents_kept": 50,
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime]) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds") if dt else ""


def _parse_iso(s: Any) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _floor_hour(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


def _ceil_hour(dt: datetime) -> datetime:
    f = _floor_hour(dt)
    return f if f == dt else f + timedelta(hours=1)


# ── config ────────────────────────────────────────────────────────────────

def load_config(storage: Any = None) -> dict:
    """The storage config doc; {} when absent or unreadable (an archived
    blob reads as nothing, which means nothing is watched)."""
    try:
        if storage is None:
            from .storage import get_storage
            storage = get_storage()
        doc = storage.read_json(CONFIG_KEY) or {}
        return doc if isinstance(doc, dict) else {}
    except Exception:
        return {}


def site_config(site: str, *, storage: Any = None, config: Optional[Mapping] = None) -> Optional[dict]:
    """DEFAULTS < config.defaults < config.sites[site], or None when the site
    has no entry (or `enabled: false`). Keys starting with "_" are notes."""
    doc = dict(config) if config is not None else load_config(storage)
    sites = doc.get("sites") if isinstance(doc.get("sites"), dict) else {}
    entry = sites.get(site)
    if not isinstance(entry, dict) or entry.get("enabled", True) is False:
        return None
    out = copy.deepcopy(DEFAULTS)
    for layer in (doc.get("defaults") or {}, entry):
        if isinstance(layer, dict):
            out.update({k: copy.deepcopy(v) for k, v in layer.items() if not str(k).startswith("_")})
    return out


def placement_groups(cfg: Mapping) -> dict[str, str]:
    """raw placement value -> money placement name (first group wins)."""
    out: dict[str, str] = {}
    for name, values in (cfg.get("money_placements") or {}).items():
        for v in (values if isinstance(values, (list, tuple)) else [values]):
            out.setdefault(str(v), str(name))
    return out


# ── deploys ───────────────────────────────────────────────────────────────

def deploys_for(site: str) -> list[dict]:
    """The deployer's deploy log for `site`, oldest first: [{at, sha, tag}]."""
    from .deploy_repair import GateState
    return GateState.load(site).deploy_log()


def deploys_from_release_tags(repo: str, site: str) -> list[dict]:
    """Deploys from the release/<site>/NNNN tags the deployer stamps after a
    deploy (install/tag-release.sh). For backtests older than the gate-state
    log. Read-only git."""
    r = subprocess.run(
        ["git", "-C", repo, "for-each-ref", "--sort=creatordate",
         "--format=%(creatordate:iso8601-strict) %(refname:short) %(objectname)",
         f"refs/tags/release/{site}/"], capture_output=True, text=True, check=False)
    out = []
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and (at := _parse_iso(parts[0])):
            out.append({"at": _iso(at), "tag": parts[1], "sha": parts[2] if len(parts) > 2 else ""})
    return out


# ── the click series ──────────────────────────────────────────────────────

def scan_range(cfg: Mapping, now: datetime) -> tuple[datetime, datetime]:
    """Rows the evaluation can need: back to the oldest judged deploy's
    earliest baseline window, from a UTC midnight so each day's per-IP
    velocity count is complete."""
    # likely_cause() can span from the oldest judged deploy to now, and its
    # baseline then starts whole days before that span.
    span_h = max(float(cfg["anchor_max_age_h"]), float(cfg["max_window_h"]))
    back_h = (float(cfg["anchor_max_age_h"])
              + 24 * (math.ceil(span_h / 24) + int(cfg["baseline_days"]) + 1))
    start = (now - timedelta(hours=back_h)).replace(hour=0, minute=0, second=0, microsecond=0)
    return start, now


def build_series_query(cfg: Mapping, spec: Mapping, *, start: datetime, end: datetime,
                       today: Optional[date] = None) -> tuple[str, list]:
    """SQL returning (hour UTC, raw placement, verified-human clicks).

    The per-row verdict is human_clicks' own CASE and the velocity rule is the
    same (per IP per UTC day, over every row of the table in the scan, before
    `match` narrows the counted rows), so these counts agree with
    human_clicks.breakdown() over the same days."""
    tbl = human_clicks._ident(cfg.get("table"), "table")
    t = human_clicks._ident(spec.get("time_col"), "time_col")
    ip = human_clicks._ident(spec.get("ip_col"), "ip_col")
    reason, rparams = human_clicks._reason_case(spec, today=today)
    pparams: list = []
    if cfg.get("placement_col"):
        pl = f"COALESCE(CAST({human_clicks._ident(cfg['placement_col'], 'placement_col')} AS text), '')"
    elif cfg.get("placement_from_referer"):
        ref = human_clicks._ident(spec.get("referer_col"), "referer_col")
        whens = []
        for rule in cfg["placement_from_referer"]:
            if not isinstance(rule, Mapping) or not rule.get("pattern") or not rule.get("value"):
                raise ValueError(f"placement_watch: bad placement_from_referer rule {rule!r}")
            whens.append(f"WHEN COALESCE({ref}, '') ~* %s THEN %s")
            pparams += [str(rule["pattern"]), str(rule["value"])]
        pl = "CASE " + " ".join(whens) + " ELSE '' END"
    else:
        raise ValueError("placement_watch: set placement_col or placement_from_referer")
    match = dict(cfg.get("match") or {})
    mcols = [human_clicks._ident(c, "match column") for c in match]
    sel_match = "".join(f", {c}" for c in mcols)
    where, mparams = [], []
    for c, v in match.items():
        if isinstance(v, (list, tuple, set)):
            where.append(f"{c} = ANY(%s)")
            mparams.append(list(v))
        else:
            where.append(f"{c} = %s")
            mparams.append(v)
    max_ipd = int(spec.get("max_clicks_per_ip_day") or 0)
    if max_ipd > 0:
        where.append(f"_per_ip_day <= {max_ipd}")
    sql = (
        "WITH w AS ("
        f" SELECT split_part(CAST({ip} AS text), '/', 1) AS _ip, {t} AS _t,"
        f" {reason} AS _reason, {pl} AS _pl{sel_match}"
        f" FROM {tbl}"
        f" WHERE {t} >= %s AND {t} < %s"
        "), v AS ("
        " SELECT *, COUNT(*) FILTER (WHERE _reason IS NULL)"
        " OVER (PARTITION BY _ip, (_t AT TIME ZONE 'UTC')::date) AS _per_ip_day"
        " FROM w"
        ")"
        " SELECT date_trunc('hour', _t AT TIME ZONE 'UTC') AS _h, _pl, COUNT(*)"
        " FROM v WHERE _reason IS NULL"
        + "".join(f" AND {w}" for w in where)
        + " GROUP BY 1, 2"
    )
    return sql, rparams + pparams + [start, end] + mparams


def fetch_series(conn, cfg: Mapping, spec: Mapping, *, start: datetime, end: datetime,
                 today: Optional[date] = None) -> dict[str, dict[datetime, int]]:
    """{money placement: {hour: clicks}} plus TOTAL (every placement in
    `match`, money or not), from one query."""
    sql, params = build_series_query(cfg, spec, start=start, end=end, today=today)
    groups = placement_groups(cfg)
    series: dict[str, dict[datetime, int]] = {g: {} for g in set(groups.values())}
    series[TOTAL] = {}
    with conn.cursor() as cur:
        cur.execute(sql, params)
        for row in cur.fetchall():
            h, raw, n = (row["_h"], row["_pl"], row["count"]) if isinstance(row, Mapping) else row
            if h is None:
                continue
            h = h.replace(tzinfo=timezone.utc) if h.tzinfo is None else h.astimezone(timezone.utc)
            n = int(n)
            series[TOTAL][h] = series[TOTAL].get(h, 0) + n
            g = groups.get("" if raw is None else str(raw))
            if g:
                series[g][h] = series[g].get(h, 0) + n
    return series


# ── the decision ──────────────────────────────────────────────────────────

def _window_sum(hours: Mapping[datetime, int], a: datetime, n_hours: int) -> int:
    return sum(hours.get(a + timedelta(hours=i), 0) for i in range(n_hours))


def sparse_floor(cfg: Mapping) -> float:
    """The smallest baseline mean per window that is judged: min_z**2 (below
    it z cannot reach min_z even at zero clicks), or min_baseline_clicks if
    that is higher."""
    return max(float(cfg.get("min_baseline_clicks") or 0), float(cfg["min_z"]) ** 2)


def _stats(hours: Mapping[datetime, int], a: datetime, n_hours: int, cfg: Mapping) -> dict:
    after = _window_sum(hours, a, n_hours)
    k0 = max(1, math.ceil(n_hours / 24))
    base = [_window_sum(hours, a - timedelta(days=k), n_hours)
            for k in range(k0, k0 + int(cfg["baseline_days"]))]
    mean = sum(base) / len(base) if base else 0.0
    var = (sum((x - mean) ** 2 for x in base) / (len(base) - 1)) if len(base) > 1 else 0.0
    sd = math.sqrt(max(mean, var))
    drop = (1.0 - after / mean) if mean > 0 else 0.0
    z = ((mean - after) / sd) if sd > 0 else 0.0
    if mean < sparse_floor(cfg):
        status = "sparse"
    elif drop >= float(cfg["drop_threshold"]) and z >= float(cfg["min_z"]):
        status = "drop"
    else:
        status = "ok"
    return {"status": status, "after": after, "baseline": base, "baseline_mean": round(mean, 1),
            "baseline_sd": round(math.sqrt(var), 1), "drop_pct": round(100 * drop, 1), "z": round(z, 2)}


def _xlogx(n: float, e: float) -> float:
    return n * math.log(n / e) if n > 0 and e > 0 else 0.0


def likely_cause(hours: Mapping[datetime, int], cfg: Mapping, a: datetime, n_hours: int,
                 candidates: list[dict], *, min_segment_h: int = 3) -> Optional[dict]:
    """Which deploy in [a, a+n) the drop most likely started at.

    Poisson change point: each hour's clicks are Poisson around rate x the
    hour's expected count (its mean on the baseline days), with one rate
    before the split and one after. The split (a deploy time) that maximises
    the likelihood, N1 log(N1/E1) + N2 log(N2/E2), with a lower rate after
    it, is the likeliest start. Splits that leave fewer than min_segment_h
    hours on either side are not considered."""
    n_hours = int(n_hours)
    if n_hours < 2 * min_segment_h:
        return None
    k0 = max(1, math.ceil(n_hours / 24))
    days = max(1, int(cfg["baseline_days"]))
    obs, exp = [], []
    for i in range(n_hours):
        h = a + timedelta(hours=i)
        obs.append(hours.get(h, 0))
        exp.append(sum(hours.get(h - timedelta(days=k), 0) for k in range(k0, k0 + days)) / days)
    best, best_ll = None, None
    for c in candidates:
        cut = int((max(a, _ceil_hour(c["at"])) - a).total_seconds() // 3600)
        if cut < min_segment_h or n_hours - cut < min_segment_h:
            continue
        n1, e1 = sum(obs[:cut]), sum(exp[:cut])
        n2, e2 = sum(obs[cut:]), sum(exp[cut:])
        if e1 <= 0 or e2 <= 0 or n2 / e2 >= n1 / e1:
            continue
        ll = _xlogx(n1, e1) + _xlogx(n2, e2)
        if best_ll is None or ll > best_ll:
            best, best_ll = {"tag": c["tag"], "sha": c["sha"], "at": _iso(c["at"]),
                             "before_pct_of_baseline": round(100 * n1 / e1),
                             "after_pct_of_baseline": round(100 * n2 / e2)}, ll
    return best


def evaluate(series: Mapping[str, Mapping[datetime, int]], deploys: Iterable[Mapping],
             cfg: Mapping, *, now: Optional[datetime] = None) -> dict:
    """Judge the newest deploy that has min_window_h full hours after it.
    Pure: no DB, no storage. See the module docstring for the rule."""
    now = now or _now()
    end_h = _floor_hour(now)
    dlist = sorted(({"at": _parse_iso(d.get("at")), "tag": str(d.get("tag") or ""),
                     "sha": str(d.get("sha") or "")} for d in deploys if _parse_iso(d.get("at"))),
                   key=lambda d: d["at"])
    oldest = now - timedelta(hours=float(cfg["anchor_max_age_h"]))
    anchors = []
    for d in dlist:
        if d["at"] < oldest:
            continue
        a = _ceil_hour(d["at"])
        n = int(min(float(cfg["max_window_h"]), (end_h - a).total_seconds() / 3600))
        if n >= int(cfg["min_window_h"]):
            anchors.append({**d, "a": a, "n": n})
    pending = [d["tag"] for d in dlist if d["at"] > now - timedelta(hours=float(cfg["min_window_h"]))]
    groups = [g for g in series if g != TOTAL]
    result: dict = {"evaluated_at": _iso(now), "anchor": None, "anchors_evaluable": len(anchors),
                    "placements": {}, "total": None, "flagged": [], "pending": pending}
    if not anchors:
        return result
    per_anchor = [{g: _stats(series.get(g) or {}, an["a"], an["n"], cfg) for g in groups}
                  for an in anchors]
    latest, judged = anchors[-1], per_anchor[-1]
    result["anchor"] = {"tag": latest["tag"], "sha": latest["sha"], "at": _iso(latest["at"]),
                        "window": [_iso(latest["a"]), _iso(latest["a"] + timedelta(hours=latest["n"]))],
                        "window_h": latest["n"]}
    w_end = latest["a"] + timedelta(hours=latest["n"])
    in_window = [d for d in dlist if latest["at"] < d["at"] < w_end]
    for g in sorted(groups):
        st = dict(judged[g])
        if st["status"] == "drop":
            # The run of consecutive judged deploys that all show the drop.
            # Windows overlap, so its first deploy can predate the cause, and
            # the cause can predate the judged window: likely_cause() looks
            # for the split from the window of the last judged deploy that
            # did NOT show the drop up to the end of this one.
            idx = len(anchors) - 1
            while idx > 0 and per_anchor[idx - 1][g]["status"] == "drop":
                idx -= 1
            first = anchors[idx]
            span_a = anchors[idx - 1]["a"] if idx > 0 else first["a"]
            span_n = int((w_end - span_a).total_seconds() // 3600)
            st["first_flagged"] = {"tag": first["tag"], "sha": first["sha"], "at": _iso(first["at"])}
            st["likely_cause"] = likely_cause(
                series.get(g) or {}, cfg, span_a, span_n,
                [d for d in dlist if span_a <= _ceil_hour(d["at"]) < w_end])
            st["deploys_in_window"] = [d["tag"] for d in in_window][:20]
            result["flagged"].append(g)
        result["placements"][g] = st
    result["total"] = _stats(series.get(TOTAL) or {}, latest["a"], latest["n"], cfg)
    return result


# ── incidents + the one email ─────────────────────────────────────────────

def state_key(site: str) -> str:
    return f"{STATE_PREFIX}{re.sub(r'[^A-Za-z0-9_.-]', '_', site or 'site')}.json"


def send_alert(email: Mapping, subject: str, body_html: str) -> tuple[bool, str]:
    """One operator email through the same path as the deployer's gate
    alerts (shared.site_quality.send_via_msmtp, not folded into the digest).
    (delivered, detail)."""
    to = list((email or {}).get("to") or [])
    sender = str((email or {}).get("from") or "")
    m = re.search(r"<([^>]+)>", sender)
    sender = m.group(1) if m else sender
    if not to or not sender:
        return False, "no reporter.email configured"
    try:
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        if root not in sys.path:
            sys.path.insert(0, root)
        from shared.site_quality import send_via_msmtp
        ok, detail = send_via_msmtp(subject=subject, body_html=body_html, to=to, sender=sender,
                                    msmtp_account=(email or {}).get("msmtp_account") or "automation",
                                    bypass_digest=True)
        delivered = bool(ok) and not str(detail).startswith(("digest disabled", "suppressed:"))
        return delivered, str(detail)[:200]
    except Exception as e:
        return False, f"send failed: {e}"[:200]


def _email(site: str, result: Mapping, names: list[str]) -> tuple[str, str]:
    a = result.get("anchor") or {}
    esc = _html.escape
    rows = []
    for g in names:
        p = result["placements"][g]
        lc = p.get("likely_cause") or {}
        cause = (f"{esc(lc.get('tag', ''))} {esc(lc.get('at', ''))} ({lc.get('before_pct_of_baseline')}% "
                 f"of baseline before, {lc.get('after_pct_of_baseline')}% after)") if lc else "unclear"
        rows.append(
            f"<tr><td><b>{esc(g)}</b></td><td>{p['after']}</td><td>{p['baseline_mean']} "
            f"(days: {', '.join(str(x) for x in p['baseline'])})</td><td>-{p['drop_pct']}%</td>"
            f"<td>{p['z']}</td><td>{cause}</td></tr>")
    tot = result.get("total") or {}
    body = (
        f"<p>Verified-human clicks on <b>{esc(', '.join(names))}</b> ({esc(site)}) fell after a deploy, "
        f"beyond normal day-to-day variation.</p>"
        f"<p><b>Window:</b> {esc(' to '.join(a.get('window') or []))} ({a.get('window_h')} h after deploy "
        f"<b>{esc(a.get('tag', ''))}</b> {esc(str(a.get('sha', ''))[:12])}), compared with the same hours on "
        f"each of the {len((result['placements'][names[0]] or {}).get('baseline') or [])} days before.</p>"
        f"<table border='1' cellpadding='4' cellspacing='0'><tr><th>placement</th><th>clicks</th>"
        f"<th>baseline mean</th><th>change</th><th>z</th><th>likely cause (deploy)</th></tr>"
        f"{''.join(rows)}</table>"
        f"<p><b>All placements:</b> {tot.get('after')} vs {tot.get('baseline_mean')} "
        f"({-tot.get('drop_pct', 0):+}%). If everything fell together, demand or tracking moved, "
        f"not one surface.</p>"
        f"<p><b>Deploys inside the window:</b> "
        f"{esc(', '.join((result['placements'][names[0]] or {}).get('deploys_in_window') or []) or 'none')}</p>"
        f"<p>To stop agents changing money surfaces while you look, set storage "
        f"<code>config/implementer-allowed-dispatch-kinds.json</code> to "
        f"<code>{{\"allow\": [\"article-author\", \"article-refresh\", \"catalog-audit\", \"h2h\"]}}</code> "
        f"(restore <code>[\"*\"]</code> after). This incident stays open, with no further email, until a "
        f"later check finds the placement back in range: <code>{esc(state_key(site))}</code>.</p>")
    subject = (f"[placement-watch:{site}] clicks down on {', '.join(names)} after deploy "
               f"{a.get('tag', '')}")[:180]
    return subject, body


def update_incidents(result: dict, site: str, *, storage: Any = None, cfg: Optional[Mapping] = None,
                     email: Optional[Mapping] = None,
                     send: Optional[Callable[[Mapping, str, str], tuple[bool, str]]] = None,
                     now: Optional[datetime] = None, dry_run: bool = False) -> dict:
    """Open an incident per newly flagged placement, close incidents whose
    placement is back in range, and send ONE email for every open incident
    not yet announced (retried on the next run when it did not go out).
    dry_run changes nothing and sends nothing."""
    cfg = cfg or DEFAULTS
    now = now or _now()
    if storage is None:
        from .storage import get_storage
        storage = get_storage()
    try:
        state = storage.read_json(state_key(site)) or {}
    except Exception:
        state = {}
    if not isinstance(state, dict):
        state = {}
    state.setdefault("schema_version", "1")
    state["site"] = site
    opened: dict = state.get("open") if isinstance(state.get("open"), dict) else {}
    history: list = state.get("incidents") if isinstance(state.get("incidents"), list) else []
    new, closed = [], []
    for g in result.get("flagged") or []:
        p = result["placements"][g]
        latest = {k: p.get(k) for k in ("after", "baseline_mean", "drop_pct", "z")}
        if g in opened:
            opened[g]["last_seen"] = _iso(now)
            opened[g]["latest"] = latest
            continue
        opened[g] = {"placement": g, "opened_at": _iso(now), "last_seen": _iso(now),
                     "anchor": result.get("anchor"), "first_flagged": p.get("first_flagged"),
                     "likely_cause": p.get("likely_cause"),
                     "deploys_in_window": p.get("deploys_in_window"), **latest,
                     "alerted_at": "", "email": ""}
        new.append(g)
    for g in list(opened):
        if g not in (result.get("flagged") or []) and \
                (result.get("placements") or {}).get(g, {}).get("status") == "ok":
            inc = opened.pop(g)
            inc.update({"closed_at": _iso(now), "closed_reason": "back in range"})
            history.append(inc)
            closed.append(g)
    # An incident that is no longer flagged but never reads "ok" again (the
    # placement went sparse, or no deploy is judgeable during a long red
    # gate) would stay open forever and mute every later alert for that
    # placement. Expire it once it has not been re-flagged for a full
    # judging span (max_window_h + anchor_max_age_h).
    stale_h = float(cfg.get("max_window_h", DEFAULTS["max_window_h"])) + \
        float(cfg.get("anchor_max_age_h", DEFAULTS["anchor_max_age_h"]))
    for g in list(opened):
        if g in (result.get("flagged") or []):
            continue
        seen = _parse_iso(opened[g].get("last_seen") or opened[g].get("opened_at"))
        if seen is None or (now - seen).total_seconds() / 3600 >= stale_h:
            inc = opened.pop(g)
            status = (result.get("placements") or {}).get(g, {}).get("status") or "not evaluated"
            inc.update({"closed_at": _iso(now),
                        "closed_reason": f"expired: not re-flagged for {stale_h:g} h (now {status})"})
            history.append(inc)
            closed.append(g)
    unannounced = [g for g, inc in opened.items() if not inc.get("alerted_at")]
    delivered, detail = False, ""
    if unannounced and not dry_run:
        live = [g for g in unannounced if g in (result.get("placements") or {})
                and result["placements"][g].get("status") == "drop"]
        if live:
            subject, body = _email(site, result, live)
            delivered, detail = (send or send_alert)(email or {}, subject, body)
            for g in live:
                opened[g]["email"] = detail
                if delivered:
                    opened[g]["alerted_at"] = _iso(now)
    state["open"] = opened
    state["incidents"] = history[-int(cfg.get("max_incidents_kept", 50)):]
    state["last_run"] = {"at": _iso(now), "anchor": result.get("anchor"),
                         "flagged": result.get("flagged"),
                         "status": {g: p.get("status") for g, p in (result.get("placements") or {}).items()}}
    if not dry_run:
        try:
            storage.write_json(state_key(site), state)
        except Exception as e:
            detail = (detail + "; " if detail else "") + f"state write failed: {e}"
    return {"new": new, "closed": closed, "open": sorted(opened),
            "alerted": [g for g in unannounced if delivered and opened.get(g, {}).get("alerted_at")],
            "email": detail, "dry_run": dry_run}


# ── for consumers ─────────────────────────────────────────────────────────

def watch_site(site: str, *, conn, storage: Any = None, cfg: Optional[Mapping] = None,
               email: Optional[Mapping] = None, profile: str = "", now: Optional[datetime] = None,
               deploys: Optional[Iterable[Mapping]] = None, dry_run: bool = False,
               send: Optional[Callable[[Mapping, str, str], tuple[bool, str]]] = None) -> dict:
    """Evaluate one site and update its incidents. `conn` is a (read-only)
    DB connection to the site's database; `email` the site's reporter.email
    block; `profile` the human_clicks profile when the config names none."""
    if storage is None:
        from .storage import get_storage
        storage = get_storage()
    cfg = dict(cfg) if cfg is not None else site_config(site, storage=storage)
    if not cfg:
        return {"site": site, "skipped": f"no entry for this site in {CONFIG_KEY}"}
    now = now or _now()
    spec = human_clicks.resolve_spec(cfg.get("spec") or {},
                                     profile=cfg.get("human_clicks_profile") or profile,
                                     storage=storage)
    start, end = scan_range(cfg, now)
    series = fetch_series(conn, cfg, spec, start=start, end=end, today=now.date())
    result = evaluate(series, deploys if deploys is not None else deploys_for(site), cfg, now=now)
    result["site"] = site
    result["incidents"] = update_incidents(result, site, storage=storage, cfg=cfg, email=email,
                                           send=send, now=now, dry_run=dry_run)
    return result


def summary_lines(result: Mapping) -> list[str]:
    """Short log lines for a consumer's run log."""
    if result.get("skipped"):
        return [str(result["skipped"])]
    a = result.get("anchor")
    if not a:
        return [f"no deploy with {DEFAULTS['min_window_h']}+ h of clicks after it to judge "
                f"(pending: {', '.join(result.get('pending') or []) or 'none'})"]
    out = [f"judged deploy {a['tag']} ({a['window_h']} h after it)"]
    for g, p in sorted((result.get("placements") or {}).items()):
        out.append(f"{g}: {p['status']} {p['after']} vs mean {p['baseline_mean']} "
                   f"({-p['drop_pct']:+}%, z {p['z']})")
    inc = result.get("incidents") or {}
    if inc.get("new") or inc.get("closed"):
        out.append(f"incidents opened {inc.get('new')} closed {inc.get('closed')} email: {inc.get('email')}")
    return out


def last_run_age_m(site: str, *, storage: Any = None, now: Optional[datetime] = None) -> Optional[float]:
    """Minutes since this site's last recorded evaluation (the state blob's
    last_run.at, written by every live or tracker run), or None when there
    is none or it cannot be read."""
    try:
        if storage is None:
            from .storage import get_storage
            storage = get_storage()
        state = storage.read_json(state_key(site)) or {}
        last = state.get("last_run") if isinstance(state, dict) else None
        at = _parse_iso((last or {}).get("at") if isinstance(last, dict) else None)
    except Exception:
        return None
    if at is None:
        return None
    return ((now or _now()) - at).total_seconds() / 60.0


def main(argv: Optional[list[str]] = None) -> int:
    """Print one evaluation. Without --live it never writes storage and never
    emails. --live (the deployer's background run) records incidents and
    sends the alert email the way the tracker does, at most once per
    --min-interval-m, from the stored config, the gate-state deploy log and
    the current time only."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--site", required=True)
    ap.add_argument("--as-of", default="", help="evaluate as of this ISO time (default now)")
    ap.add_argument("--dsn-env", default="", help="env var holding the site DSN "
                    "(default DATABASE_URL_<SITE>)")
    ap.add_argument("--tags-repo", default="", help="take deploys from release/<site>/ tags in this "
                    "repo instead of the gate-state log (backtests)")
    ap.add_argument("--config", default="", help="a local JSON file in the storage config's shape, "
                    "instead of the storage copy")
    ap.add_argument("--live", action="store_true", help="record incidents and send the alert email")
    ap.add_argument("--min-interval-m", type=float, default=0.0,
                    help="with --live: skip when this site was evaluated less than this many minutes ago")
    ap.add_argument("--email-json", default="", help="with --live: the site's reporter.email block, as JSON")
    a = ap.parse_args(argv)
    import json
    if a.live and (a.config or a.tags_repo or a.as_of):
        print("--live judges the stored config, the gate-state deploy log and the current time only; "
              "drop --config / --tags-repo / --as-of", file=sys.stderr)
        return 2
    try:
        email = json.loads(a.email_json) if a.email_json else {}
    except ValueError:
        print("--email-json is not valid JSON", file=sys.stderr)
        return 2
    cfg = site_config(a.site, config=json.load(open(a.config)) if a.config else None)
    if not cfg:
        print(f"no entry for {a.site} in {CONFIG_KEY}", file=sys.stderr)
        return 0 if a.live else 1
    if a.live and a.min_interval_m > 0:
        age = last_run_age_m(a.site)
        if age is not None and age < a.min_interval_m:
            print(f"{a.site}: evaluated {age:.0f} min ago (interval {a.min_interval_m:g} min); skipped")
            return 0
    dsn = os.environ.get(a.dsn_env or f"DATABASE_URL_{a.site.upper().replace('-', '_')}", "")
    if not dsn:
        print("no DSN in the environment", file=sys.stderr)
        return 1
    now = _parse_iso(a.as_of) or _now()
    import psycopg2
    # A hung query must not leave the deployer's background run behind.
    conn = psycopg2.connect(dsn, connect_timeout=20, options="-c statement_timeout=120000")
    try:
        conn.set_session(readonly=True, autocommit=True)
        deploys = deploys_from_release_tags(a.tags_repo, a.site) if a.tags_repo else None
        res = watch_site(a.site, conn=conn, cfg=cfg, now=now, deploys=deploys,
                         email=email if isinstance(email, dict) else {}, dry_run=not a.live)
    finally:
        conn.close()
    for line in summary_lines(res):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
