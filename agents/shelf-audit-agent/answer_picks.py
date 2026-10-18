"""Answer-pick click-out check — do the money links on the top pages still
land on a product page?

Opt-in per site via `answer_pick_check` in the site yaml (off when absent).

Why it exists (aisleprompt, 2026-10-18 growth memo, Lever 3): every manual
sweep of the top-click buying guides found answer picks whose `/k/<slug>`
click-out no longer 302'd to `amazon.com/dp/` but to an Amazon *search* page
(the listing went out of stock and the redirect fell back). Seven rounds in a
row found more. Those picks earn most of the site's affiliate clicks, and
stock turns fastest going into Black Friday — a weekly spec someone remembers
to run does not protect a #1 pick that goes out of stock on a Tuesday.

What it does, per run:
  1. Pages: the site's top pages by verified-human clicks (framework
     human_clicks, same definition as the goals tracker), filtered to
     `path_prefix`, capped at `max_pages`; `fallback_pages` when the click
     table is unreachable.
  2. Picks: each page's JSON (`picks_endpoint`, `{slug}` substituted) →
     `picks_key[].href` plus `picks_key[].<parts_key>[].href`.
  3. Probe each href WITHOUT following the external redirect (same-host
     301s are followed, up to 3 hops). A pick is broken unless the final
     Location matches `expect_location`. The probe UA says "bot", so the
     site's click logger flags these rows is_bot and they never count as
     human clicks.
  4. Strikes: a broken href is CONFIRMED only when it was also broken on a
     run at least `strike_gap_hours` earlier (default 8 h) — two strikes, so
     a single API blip or a restock in flight is never acted on. State lives
     in framework storage; a href that comes back healthy is forgotten.
  5. Remedy, per confirmed pick:
       relink  — `replacement_query` returned an in-stock row that is the SAME
                 product: the listing photo hashes the same (shelf.same_picture)
                 AND the titles agree, including every number in them (pack
                 size, quart, inch, piece count). Dispatchable.
       review  — a NEAR-same row exists (same words, but a number or the photo
                 differs: the Nordic Ware half-sheet vs 2-pack case). Left to a
                 person, never dispatched.
       unlink  — no in-stock row of this product at all (other products of the
                 same brand don't count). Dispatchable.
"""
from __future__ import annotations

import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

UA = {"User-Agent": "Mozilla/5.0 (compatible; shelf-audit-bot/1.0; answer-pick monitor)"}
DEFAULT_EXPECT = r"^https://www\.amazon\.com/dp/"


# ---------- pages ----------

def top_click_pages(conn, cfg: dict) -> list[str]:
    """Top `max_pages` paths by verified-human clicks, best first."""
    from framework.core import human_clicks
    spec = human_clicks.resolve_spec(cfg.get("clicks_spec") or {},
                                     profile=cfg.get("clicks_profile", ""))
    rows = human_clicks.human_click_paths(
        conn, spec, table=cfg["clicks_table"],
        group_col=cfg.get("clicks_group_col", "referer"),
        window_days=int(cfg.get("window_days", 30)),
        match=cfg.get("clicks_match") or None, limit=500)
    prefix = cfg.get("path_prefix") or "/"
    paths = [r["path"].rstrip("/") for r in rows if r["path"].startswith(prefix)]
    return paths[:int(cfg.get("max_pages", 20))]


# ---------- picks ----------

def extract_pick_hrefs(doc: dict, picks_key: str = "answer_picks",
                       parts_key: str = "parts") -> list[dict]:
    """[{label, href}] for every pick and pick part, in page order, deduped."""
    out, seen = [], set()
    for p in (doc or {}).get(picks_key) or []:
        if not isinstance(p, dict):
            continue
        label = p.get("label") or p.get("award") or ""
        name = p.get("name") or p.get("title") or ""
        for item in [p] + [x for x in (p.get(parts_key) or []) if isinstance(x, dict)]:
            href = item.get("href")
            if href and href not in seen:
                seen.add(href)
                out.append({"label": label, "name": item.get("name") or name, "href": href})
    return out


def pick_slug(href: str) -> str:
    """/k/<slug>?source=amazon -> <slug>"""
    path = urllib.parse.urlparse(href).path
    return urllib.parse.unquote(path.rsplit("/", 1)[-1])


# ---------- probe ----------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def probe(url: str, timeout: int = 20, max_hops: int = 3) -> dict:
    """{status, location}. Same-host redirects are followed; the first
    off-host Location is returned unfollowed."""
    opener = urllib.request.build_opener(_NoRedirect)
    host = urllib.parse.urlparse(url).netloc
    for _ in range(max_hops + 1):
        try:
            r = opener.open(urllib.request.Request(url, headers=UA), timeout=timeout)
            return {"status": r.status, "location": ""}
        except urllib.error.HTTPError as e:
            loc = e.headers.get("Location", "") if e.headers else ""
            if e.code in (301, 302, 303, 307, 308) and loc:
                nxt = urllib.parse.urljoin(url, loc)
                if urllib.parse.urlparse(nxt).netloc == host:
                    url = nxt
                    continue
                return {"status": e.code, "location": nxt}
            return {"status": e.code, "location": loc}
        except Exception as e:
            return {"status": 0, "location": "", "error": str(e)[:80]}
    return {"status": 0, "location": url, "error": "too many same-host redirects"}


def is_healthy(result: dict, expect: str = DEFAULT_EXPECT) -> bool:
    return result.get("status") in (301, 302, 303, 307, 308) and \
        bool(re.search(expect, result.get("location") or ""))


# ---------- strikes ----------

def apply_strikes(state: dict, broken: dict[str, dict], healthy: set[str],
                  now: datetime, gap_hours: float) -> tuple[dict, list[str]]:
    """-> (new_state, confirmed_hrefs).

    state: {href: {"first_seen": iso, ...}}. A broken href is confirmed when
    its first strike is at least gap_hours old. Healthy hrefs are dropped."""
    new = {h: v for h, v in (state or {}).items() if h not in healthy}
    confirmed = []
    for href, info in broken.items():
        prev = new.get(href)
        if prev and prev.get("first_seen"):
            try:
                first = datetime.fromisoformat(prev["first_seen"])
            except ValueError:
                first = now
            new[href] = {**prev, **info, "first_seen": prev["first_seen"],
                         "last_seen": now.isoformat()}
            if now - first >= timedelta(hours=gap_hours):
                confirmed.append(href)
        else:
            new[href] = {**info, "first_seen": now.isoformat(),
                         "last_seen": now.isoformat()}
    return new, confirmed


# ---------- same-product match ----------

_WORD = re.compile(r"[a-z0-9]+(?:\.[0-9]+)?")


def _tokens(title: str) -> list[str]:
    return _WORD.findall((title or "").lower())


def _numbers(title: str) -> set[str]:
    return {t for t in _tokens(title) if any(c.isdigit() for c in t)}


def near_title(a: str, b: str, min_overlap: float = 0.8) -> bool:
    """High overlap of the words (numbers aside). Amazon titles reorder and
    pad words, so this is the same product line or a variant of it."""
    wa = {t for t in _tokens(a) if len(t) > 2 and not any(c.isdigit() for c in t)}
    wb = {t for t in _tokens(b) if len(t) > 2 and not any(c.isdigit() for c in t)}
    if not wa or not wb:
        return False
    return len(wa & wb) / min(len(wa), len(wb)) >= min_overlap


def same_title(a: str, b: str, min_overlap: float = 0.8) -> bool:
    """Near-same words AND every number equal (pack size, qt, inch, pieces):
    a title does not change a number unless the product changed."""
    return near_title(a, b, min_overlap) and _numbers(a) == _numbers(b)


def choose_remedy(broken_row: Optional[dict], candidates: list[dict],
                  same_picture: Callable[[str, str], tuple]) -> dict:
    """{action: relink|unlink|review, replacement?: slug, reason}."""
    if not broken_row:
        return {"action": "review", "reason": "broken pick's own row not found"}
    title = broken_row.get("title", "")
    near = [c for c in candidates if near_title(title, c.get("title", ""))]
    if not near:
        return {"action": "unlink",
                "reason": "no in-stock row of this product (%d other in-stock rows "
                          "of the brand/category, none the same product)" % len(candidates)}
    for c in near:
        if not same_title(title, c.get("title", "")):
            continue
        verdict, h = same_picture(broken_row.get("image") or "", c.get("image") or "")
        if verdict == "same":
            return {"action": "relink", "replacement": c["slug"],
                    "replacement_title": c.get("title"),
                    "reason": "same title and same listing photo (hamming %s)" % h}
    return {"action": "review",
            "reason": "%d near-same in-stock row(s) (%s), but a number in the title "
                      "or the listing photo differs; a person decides"
                      % (len(near), ", ".join(c["slug"] for c in near[:3]))}


# ---------- orchestration ----------

def run_check(cfg: dict, *, origin: str, pages: list[str], state: dict,
              now: datetime, fetch_json: Callable[[str], dict],
              probe_fn: Callable[[str], dict] = probe,
              lookup: Optional[Callable[[str], tuple]] = None,
              same_picture: Optional[Callable[[str, str], tuple]] = None) -> dict:
    """Pure-ish core: everything with I/O is injected so it unit-tests.

    lookup(slug) -> (broken_row|None, [candidate rows]); rows carry
    slug/title/image."""
    expect = cfg.get("expect_location") or DEFAULT_EXPECT
    prefix = cfg.get("path_prefix") or "/"
    endpoint = cfg["picks_endpoint"]
    broken: dict[str, dict] = {}
    healthy: set[str] = set()
    checked = 0
    page_errors = []
    for path in pages:
        slug = path[len(prefix):] if path.startswith(prefix) else path.lstrip("/")
        doc = fetch_json(endpoint.replace("{slug}", slug))
        picks = extract_pick_hrefs(doc, cfg.get("picks_key", "answer_picks"),
                                   cfg.get("parts_key", "parts"))
        if not picks:
            page_errors.append(path)
            continue
        for p in picks:
            checked += 1
            res = probe_fn(urllib.parse.urljoin(origin, p["href"]))
            if is_healthy(res, expect):
                healthy.add(p["href"])
            else:
                prev = broken.get(p["href"])
                pages_for = (prev["pages"] if prev else []) + [path]
                broken[p["href"]] = {"pages": pages_for, "label": p["label"],
                                     "name": p["name"], "status": res.get("status"),
                                     "location": (res.get("location") or "")[:200]}
    new_state, confirmed = apply_strikes(state, broken, healthy, now,
                                         float(cfg.get("strike_gap_hours", 8)))
    remedies = []
    for href in confirmed:
        info = new_state[href]
        rem = {"action": "review", "reason": "no replacement lookup configured"}
        if lookup and same_picture:
            try:
                row, cands = lookup(pick_slug(href))
                rem = choose_remedy(row, cands, same_picture)
            except Exception as e:
                rem = {"action": "review", "reason": "lookup failed: %s" % str(e)[:80]}
        remedies.append({"href": href, "slug": pick_slug(href), **info, **rem})
    return {"pages": pages, "picks_checked": checked, "broken_now": broken,
            "confirmed": remedies, "pages_without_picks": page_errors,
            "state": new_state}


def to_recs(result: dict, run_ts: str) -> list[dict]:
    """One rec for the dispatchable fixes, one for the ones a person decides."""
    recs = []
    auto = [r for r in result["confirmed"] if r["action"] in ("relink", "unlink")]
    manual = [r for r in result["confirmed"] if r["action"] == "review"]

    def line(r):
        tgt = (" -> /k/%s" % r["replacement"]) if r.get("replacement") else ""
        return "%s [%s] %s on %s (now %s %s)%s" % (
            r["action"].upper(), r.get("label") or "pick", r["href"],
            ", ".join(r.get("pages") or []), r.get("status"),
            (r.get("location") or "")[:80], tgt)

    if auto:
        recs.append({
            "id": "shelf-%s-answer-pick-dead" % run_ts,
            "title": "%d answer pick(s) on top-click pages land on an Amazon search page"
                     % len(auto),
            "severity": "high", "priority": "high", "tier": "lever",
            "rationale": "Two strikes at least 8 h apart: each pick's click-out no "
                         "longer redirects to a product page. " + "; ".join(line(r) for r in auto),
            "fix": "In each listed page's article body (editorial_articles.body_md), "
                   "RELINK: replace the /k/<slug> link with the /k/<replacement> slug "
                   "given (same product: same title numbers and same listing photo). "
                   "UNLINK: no in-stock row exists; keep the product name as plain "
                   "text and remove the link, and if it was the page's #1 pick, say "
                   "so in the summary rather than promoting another product. Write "
                   "the change as a db/migrations file and commit it.",
            "expected_impact": "The picks behind most affiliate clicks keep "
                               "reaching a buyable product page.",
            "answer_picks": auto,
        })
    if manual:
        recs.append({
            "id": "shelf-%s-answer-pick-review" % run_ts,
            "title": "%d dead answer pick(s) need a person to choose the replacement"
                     % len(manual),
            "severity": "medium", "priority": "medium", "tier": "smaller",
            "rationale": "In-stock candidates exist but none is provably the same "
                         "product (title numbers or listing photo differ, e.g. a "
                         "different pack size). " + "; ".join(line(r) for r in manual),
            "expected_impact": "Avoids auto-swapping in a different product.",
            "answer_picks": manual,
        })
    return recs
