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
     table is unreachable. Optional widening (audit_pages): pages assistants
     land on or fetch (`ai_landed`, framework ai_traffic) and a hand-kept
     `extra_pages` list, unioned after the click pages. A page whose picks
     are dead earns no clicks, so a click-ranked set alone never sees it.
  2. Picks: each page's JSON (`picks_endpoint`, `{slug}` substituted) →
     `picks_key[].href` plus `picks_key[].<parts_key>[].href`, then the same
     for each key in `extra_picks_keys` (optional: e.g. a deals page's table
     rows, which the site serves under its own key so they don't render as
     answer picks).
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
  6. Fast windows (optional `fast_windows`): inside a dated window (a deal
     event such as Black Friday → Cyber Monday), a pick broken on its FIRST
     strike is confirmed when its remedy is in `one_strike_actions` (default
     relink only: the backup is provably the same product, so acting early
     costs nothing if the stock was only gone for a moment). Everything else
     still waits for the second strike.
"""
from __future__ import annotations

import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, Optional

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


def ai_landed_pages(conn, cfg: dict) -> list[str]:
    """Top `ai_landed.limit` paths assistants used (referrals + answer-time
    fetches, framework ai_traffic), filtered to `path_prefix`, best first."""
    block = cfg.get("ai_landed") or {}
    if not block or block.get("enabled") is False:
        return []
    from framework.core import ai_traffic
    prefix = cfg.get("path_prefix") or "/"
    rows = ai_traffic.landed_paths(conn, cfg=ai_traffic.config(block.get("config") or {}),
                                   prefixes=[prefix], limit=int(block.get("limit", 30)))
    return [r["path"].rstrip("/") for r in rows if r["path"].startswith(prefix)]


def union_pages(*lists: list[str], cap: int = 0) -> list[str]:
    """Order-preserving union (first list wins ties), capped when cap > 0."""
    out: list[str] = []
    seen: set[str] = set()
    for lst in lists:
        for p in lst or []:
            p = p.rstrip("/")
            if p and p not in seen:
                seen.add(p)
                out.append(p)
    return out[:cap] if cap > 0 else out


def audit_pages(conn, cfg: dict, warn: Callable[[str], None] = lambda m: None) -> list[str]:
    """Click pages, then AI-landed pages, then `extra_pages`, deduped and
    capped at `max_pages_total` (0 / absent = no cap). Each source fails on
    its own; with no click pages the `fallback_pages` stand in for them."""
    clicks: list[str] = []
    ai: list[str] = []
    if conn is not None:
        try:
            clicks = top_click_pages(conn, cfg)
        except Exception as e:
            warn("answer-pick pages from clicks failed: %s" % str(e)[:120])
        try:
            conn.rollback()
            ai = ai_landed_pages(conn, cfg)
        except Exception as e:
            warn("answer-pick pages from AI traffic failed: %s" % str(e)[:120])
        try:
            conn.rollback()
        except Exception:
            pass
    if not clicks:
        clicks = list(cfg.get("fallback_pages") or [])
    return union_pages(clicks, ai, list(cfg.get("extra_pages") or []),
                       cap=int(cfg.get("max_pages_total") or 0))


# ---------- picks ----------

def extract_pick_hrefs(doc: dict, picks_key: str = "answer_picks",
                       parts_key: str = "parts",
                       extra_keys: Iterable[str] = ()) -> list[dict]:
    """[{label, href}] for every pick and pick part, in page order, deduped.
    Picks under each of extra_keys follow those under picks_key."""
    out, seen = [], set()
    items = []
    for key in [picks_key, *extra_keys]:
        items.extend((doc or {}).get(key) or [])
    for p in items:
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


def probe_headers(cfg: Optional[dict]) -> dict:
    """UA plus the site's `probe_headers` (top-level site config, e.g. a
    header the site reads to skip click logging for our own probes)."""
    h = dict(UA)
    for k, v in ((cfg or {}).get("probe_headers") or {}).items():
        if k and v is not None:
            h[str(k)] = str(v)
    return h


def probe(url: str, timeout: int = 20, max_hops: int = 3,
          headers: Optional[dict] = None) -> dict:
    """{status, location}. Same-host redirects are followed; the first
    off-host Location is returned unfollowed."""
    opener = urllib.request.build_opener(_NoRedirect)
    host = urllib.parse.urlparse(url).netloc
    hdrs = headers or UA
    for _ in range(max_hops + 1):
        try:
            r = opener.open(urllib.request.Request(url, headers=hdrs), timeout=timeout)
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
                         "last_seen": now.isoformat(),
                         "runs_seen": int(prev.get("runs_seen") or 1) + 1}
            if now - first >= timedelta(hours=gap_hours):
                confirmed.append(href)
        else:
            new[href] = {**info, "first_seen": now.isoformat(),
                         "last_seen": now.isoformat(), "runs_seen": 1}
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


# ---------- fast windows ----------

def calendar_fast_windows(cfg: dict, now: datetime, occasions=None) -> list[dict]:
    """Fast windows derived from framework seasonal_calendar retail-deal
    occasions (opt-in: `fast_windows_from_calendar: {enabled: true,
    audience, lead_days, trail_days, one_strike_actions}`), so a deal window
    is not hand-kept per site. Each occasion of kind retail-deal for the
    audience becomes [start - lead_days, end + trail_days] for this year and
    last (a window spilling into January)."""
    block = cfg.get("fast_windows_from_calendar") or {}
    if not block.get("enabled"):
        return []
    try:
        from framework.core import seasonal_calendar as sc
    except Exception:
        return []
    audience = block.get("audience")
    lead = timedelta(days=max(0, int(block.get("lead_days", 0))))
    trail = timedelta(days=max(0, int(block.get("trail_days", 0))))
    acts = list(block.get("one_strike_actions") or ["relink"])
    pool = occasions if occasions is not None else sc.load_occasions()
    today = now.astimezone(timezone.utc).date()
    out = []
    for occ in pool:
        if occ.kind != sc.RETAIL_DEAL_KIND:
            continue
        if audience and occ.audience not in ("general", audience):
            continue
        for year in (today.year - 1, today.year):
            w = sc.event_window(occ, year)
            if w is None:
                continue
            out.append({"start": (w.start - lead).isoformat(),
                        "end": (w.end + trail).isoformat(),
                        "one_strike_actions": acts, "occasion": occ.id})
    return out


def active_fast_window(cfg: dict, now: datetime, occasions=None) -> Optional[dict]:
    """The `fast_windows` entry (hand-kept, then calendar-derived) whose
    [start, end] dates (inclusive, UTC) contain `now`, else None. Bad dates
    are ignored."""
    day = now.astimezone(timezone.utc).date().isoformat()
    windows = list(cfg.get("fast_windows") or []) + calendar_fast_windows(cfg, now, occasions)
    for w in windows:
        start, end = str(w.get("start") or ""), str(w.get("end") or "")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", start) or \
                not re.fullmatch(r"\d{4}-\d{2}-\d{2}", end):
            continue
        if start <= day <= end:
            return w
    return None


# ---------- swap to a reviewed in-stock product (opt-in: `swap`) ----------
#
# A dead pick with no in-stock row of the SAME product used to end as
# "unlink" (an untracked raw Amazon search button) or "review" (waits on a
# person; 8 such picks, oldest 17 days, on 2026-11-11). With `swap.enabled`
# the engine first offers a DIFFERENT product the page already links and
# reviews: same type (the site's `swap.query` decides, e.g. same category),
# in stock, freshly priced, not already another pick on the page, and backed
# by a sentence on the page (the evidence the implementer's model checks
# against the slot label in place of a human approval). A person-approved
# entry in `approved_alternates[<page>][<label>]` wins when it is buyable.

_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
_LABEL_STOP = {"best", "for", "the", "and", "pick", "overall", "of", "a", "an"}


def _kind_words(title: str) -> set:
    return {t for t in _tokens(title) if len(t) >= 4 and t.isalpha()}


def evidence_sentence(body: str, slug: str, max_len: int = 300) -> str:
    """The first sentence/line of `body` (markdown) that links the product's
    slug (/k/<slug> or /kitchen/<slug>), with links reduced to their text."""
    if not body or not slug:
        return ""
    needles = ("/k/%s" % slug, "/kitchen/%s" % slug)
    for part in _SENT_SPLIT.split(body):
        low = part.lower()
        if any(n in low for n in needles):
            text = _LINK_RE.sub(lambda m: m.group(1), part).strip(" #*-|>\t")
            text = re.sub(r"\s+", " ", text)
            if text:
                return text[:max_len]
    return ""


def _price(c: dict):
    v = c.get("price_cents")
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def choose_swap(*, page: str, label: str, broken_slug: str, candidates: list[dict],
                body: str, used_slugs: Iterable[str], approved: Optional[dict] = None,
                require_evidence: bool = True, broken_title: str = "",
                min_title_overlap: int = 1) -> Optional[dict]:
    """A swap remedy {action: swap, replacement, replacement_title, evidence,
    reason} or None. `candidates` are the site query's rows (slug, title,
    price_cents, ...), already in stock and priced per the site's rules.

    Same TYPE: unless a person approved it, a candidate must share at least
    `min_title_overlap` product words (letters only, 4+ chars) with the dead
    pick's title, so a category-mate accessory (a descaler on an espresso
    guide) is never offered for a machine slot. 0 turns the check off."""
    used = {u for u in used_slugs if u} | {broken_slug}
    pool = [c for c in candidates if c.get("slug") and c["slug"] not in used
            and _price(c) is not None and _price(c) > 0]
    if not pool:
        return None
    by_slug = {c["slug"]: c for c in pool}
    page_alts = ((approved or {}).get(page.rstrip("/")) or {}) if isinstance(approved, dict) else {}
    for alt in (page_alts.get(label) or []) if isinstance(page_alts, dict) else []:
        c = by_slug.get((alt or {}).get("product_slug") or "")
        if c:
            return {"action": "swap", "replacement": c["slug"],
                    "replacement_title": c.get("title"),
                    "evidence": str(alt.get("evidence") or "")[:300]
                                or evidence_sentence(body, c["slug"]),
                    "approved_by": alt.get("approved_by"),
                    "reason": "person-approved alternate for this slot, in stock and priced"}
    kind_words = _kind_words(broken_title)
    if min_title_overlap > 0 and kind_words:
        pool = [c for c in pool
                if len(kind_words & _kind_words(c.get("title") or "")) >= min_title_overlap]
    scored = []
    want = {t for t in _tokens(label) if t not in _LABEL_STOP and len(t) > 2}
    budget = bool(want & {"budget", "value", "cheap", "affordable", "under"})
    premium = bool(want & {"premium", "splurge", "luxury", "upgrade"})
    for c in pool:
        ev = evidence_sentence(body, c["slug"])
        if require_evidence and not ev:
            continue
        overlap = len(want & set(_tokens(ev))) if ev else 0
        price = _price(c)
        order = price if budget else (-price if premium else 0)
        scored.append((-overlap, order, c["slug"], c, ev))
    if not scored:
        return None
    scored.sort(key=lambda t: t[:3])
    _o, _p, _s, c, ev = scored[0]
    return {"action": "swap", "replacement": c["slug"],
            "replacement_title": c.get("title"), "evidence": ev,
            "reason": "no in-stock row of the same product; the page already reviews "
                      "this in-stock, priced product of the same type (evidence quoted)"}


# ---------- review escalation ----------

def _age_hours(iso: str, now: datetime) -> float:
    try:
        return max(0.0, (now - datetime.fromisoformat(iso)).total_seconds() / 3600.0)
    except (TypeError, ValueError):
        return 0.0


def _waits_on_person(r: dict, held: set) -> bool:
    return r.get("action") == "review" or any(
        (p or "").rstrip("/") in held for p in (r.get("pages") or []))


def escalations(result: dict, cfg: dict, now: datetime,
                held_pages: Iterable[str] = ()) -> list[dict]:
    """Person-review picks (action review, or any fix on a held page) older
    than `escalate_after_hours` (default 24) or seen on `escalate_after_runs`
    runs (default 2), not escalated before. Marks them in result['state'] so
    each href escalates once; a href that recovers is dropped from state and
    may escalate again if it dies again."""
    held = {p.rstrip("/") for p in held_pages}
    hours = float(cfg.get("escalate_after_hours", 24))
    runs = int(cfg.get("escalate_after_runs", 2))
    out = []
    state = result.get("state") or {}
    for r in result.get("confirmed") or []:
        if not _waits_on_person(r, held) or r["href"] not in state:
            continue
        st = state[r["href"]]
        if st.get("escalated_at"):
            continue
        age = _age_hours(st.get("first_seen") or "", now)
        if age >= hours or int(st.get("runs_seen") or 0) >= runs:
            st["escalated_at"] = now.isoformat()
            out.append({"href": r["href"], "pages": r.get("pages"), "label": r.get("label"),
                        "age_h": round(age, 1), "runs_seen": st.get("runs_seen"),
                        "reason": r.get("reason")})
    return out


def review_max_age_h(result: dict, now: datetime, held_pages: Iterable[str] = ()) -> float:
    """Age (hours since first strike) of the oldest confirmed pick that waits
    on a person (review, or any fix on a held page)."""
    held = {p.rstrip("/") for p in held_pages}
    state = result.get("state") or {}
    ages = [_age_hours((state.get(r["href"]) or {}).get("first_seen") or "", now)
            for r in result.get("confirmed") or [] if _waits_on_person(r, held)]
    return round(max(ages), 1) if ages else 0.0


# ---------- orchestration ----------

def run_check(cfg: dict, *, origin: str, pages: list[str], state: dict,
              now: datetime, fetch_json: Callable[[str], dict],
              probe_fn: Callable[[str], dict] = probe,
              lookup: Optional[Callable[[str], tuple]] = None,
              same_picture: Optional[Callable[[str, str], tuple]] = None,
              swap_lookup: Optional[Callable[[str, str], list]] = None,
              occasions=None) -> dict:
    """Pure-ish core: everything with I/O is injected so it unit-tests.

    lookup(slug) -> (broken_row|None, [candidate rows]); rows carry
    slug/title/image. swap_lookup(pick_slug, page_slug) -> [rows with
    slug/title/price_cents] (only used with `swap.enabled`)."""
    expect = cfg.get("expect_location") or DEFAULT_EXPECT
    prefix = cfg.get("path_prefix") or "/"
    endpoint = cfg["picks_endpoint"]
    broken: dict[str, dict] = {}
    healthy: set[str] = set()
    checked = 0
    page_errors = []
    swap = cfg.get("swap") or {}
    body_key = swap.get("body_key") or "body_md"
    bodies: dict[str, str] = {}
    used: dict[str, set] = {}

    def page_slug(path):
        return path[len(prefix):] if path.startswith(prefix) else path.lstrip("/")

    for path in pages:
        slug = page_slug(path)
        doc = fetch_json(endpoint.replace("{slug}", slug))
        picks = extract_pick_hrefs(doc, cfg.get("picks_key", "answer_picks"),
                                   cfg.get("parts_key", "parts"),
                                   cfg.get("extra_picks_keys") or ())
        if not picks:
            page_errors.append(path)
            continue
        if swap.get("enabled"):
            bodies[path] = str((doc or {}).get(body_key) or "")
            used[path] = {pick_slug(p["href"]) for p in picks}
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
    def remedy(href):
        """Same-product relink first; then (swap.enabled) a reviewed in-stock
        product of the same type; the unlink/review verdict only after both."""
        row = None
        if not (lookup and same_picture):
            rem = {"action": "review", "reason": "no replacement lookup configured"}
        else:
            try:
                row, cands = lookup(pick_slug(href))
                rem = choose_remedy(row, cands, same_picture)
            except Exception as e:
                rem = {"action": "review", "reason": "lookup failed: %s" % str(e)[:80]}
        if rem["action"] == "relink" or not swap.get("enabled") or swap_lookup is None:
            return rem
        info = broken.get(href) or {}
        for path in info.get("pages") or []:
            try:
                cands = swap_lookup(pick_slug(href), page_slug(path)) or []
                sw = choose_swap(page=path, label=info.get("label") or "",
                                 broken_slug=pick_slug(href), candidates=cands,
                                 body=bodies.get(path, ""), used_slugs=used.get(path, ()),
                                 approved=cfg.get("approved_alternates"),
                                 require_evidence=swap.get("require_evidence", True) is not False,
                                 broken_title=(row or {}).get("title") or info.get("name") or "",
                                 min_title_overlap=int(swap.get("min_title_overlap", 1)))
            except Exception as e:
                rem = {**rem, "swap_error": str(e)[:80]}
                continue
            if sw:
                return {**sw, "swap_page": path, "prior_action": rem["action"],
                        "prior_reason": rem.get("reason")}
        return rem

    remedies = []
    for href in confirmed:
        remedies.append({"href": href, "slug": pick_slug(href), **new_state[href],
                         **remedy(href), "strikes": 2})
    window = active_fast_window(cfg, now, occasions)
    if window:
        one_strike = set(window.get("one_strike_actions") or ["relink"])
        for href in broken:
            if href in confirmed:
                continue
            rem = remedy(href)
            if rem["action"] in one_strike:
                remedies.append({"href": href, "slug": pick_slug(href),
                                 **new_state[href], **rem, "strikes": 1})
    return {"pages": pages, "picks_checked": checked, "broken_now": broken,
            "confirmed": remedies, "pages_without_picks": page_errors,
            "state": new_state, "fast_window": window}


AUTO_ACTIONS = ("relink", "swap", "unlink")


def to_recs(result: dict, run_ts: str, held_pages: Iterable[str] = (),
            dispatch_kind: str = "") -> list[dict]:
    """One rec for the dispatchable fixes, one for the ones a person decides.

    `held_pages` (framework page_hold): a fix on a held page is still
    reported, but in the person-review rec, never the dispatched one, even a
    same-product relink. `dispatch_kind` (config `rec_dispatch_kind`, e.g.
    article-refresh: a DB-only body edit the implementer ships without the
    deployer) is stamped on the dispatchable rec."""
    recs = []
    held = {p.rstrip("/") for p in held_pages}

    def on_hold(r):
        return any((p or "").rstrip("/") in held for p in (r.get("pages") or []))

    auto, manual = [], []
    for r in result["confirmed"]:
        if r["action"] in AUTO_ACTIONS and not on_hold(r):
            auto.append(r)
        elif r["action"] in AUTO_ACTIONS + ("review",):
            manual.append({**r, "held": True} if on_hold(r) else r)

    def line(r):
        tgt = (" -> /k/%s" % r["replacement"]) if r.get("replacement") else ""
        if r.get("evidence"):
            tgt += ' (page evidence: "%s")' % r["evidence"][:200]
        return "%s%s [%s] %s on %s (now %s %s)%s" % (
            "PAGE ON HOLD, proposed " if r.get("held") else "",
            r["action"].upper(), r.get("label") or "pick", r["href"],
            ", ".join(r.get("pages") or []), r.get("status"),
            (r.get("location") or "")[:80], tgt)

    if auto:
        recs.append({
            "id": "shelf-%s-answer-pick-dead" % run_ts,
            "title": "%d answer pick(s) on top-click pages land on an Amazon search page"
                     % len(auto),
            "severity": "high", "priority": "high", "tier": "lever",
            "rationale": ("Two strikes at least 8 h apart (one strike for a same-product "
                          "relink inside a fast window)" if any(r.get("strikes") == 1 for r in auto)
                          else "Two strikes at least 8 h apart") +
                         ": each pick's click-out no "
                         "longer redirects to a product page. " + "; ".join(line(r) for r in auto),
            "fix": "In each listed page's article body (editorial_articles.body_md), "
                   "RELINK: replace the /k/<slug> link with the /k/<replacement> slug "
                   "given (same product: same title numbers and same listing photo). "
                   + ("SWAP: the same product is not buyable, so the slot moves to a "
                      "DIFFERENT in-stock product the page already reviews. First check "
                      "that the quoted page evidence supports the slot label; if it "
                      "does, point the pick's /k/ link (and its product name in the pick "
                      "line) at /k/<replacement> and keep the label; if it does not, "
                      "leave that pick unchanged and say so in the summary. "
                      if any(r["action"] == "swap" for r in auto) else "") +
                   "UNLINK: no in-stock row exists; keep the product name as plain "
                   "text and remove the link, and if it was the page's #1 pick, say "
                   "so in the summary rather than promoting another product. Write "
                   "the change as a db/migrations file and commit it.",
            "expected_impact": "The picks behind most affiliate clicks keep "
                               "reaching a buyable product page.",
            "answer_picks": auto,
        })
        if dispatch_kind:
            recs[-1]["dispatch_kind"] = dispatch_kind
    if manual:
        recs.append({
            "id": "shelf-%s-answer-pick-review" % run_ts,
            "title": "%d dead answer pick(s) need a person to choose the replacement"
                     % len(manual),
            "severity": "medium", "priority": "medium", "tier": "smaller",
            "rationale": ("Either the page is on a page hold (a person approves "
                          "every change to it), or in-stock candidates exist but none "
                          "is provably the same product (title numbers or listing "
                          "photo differ, e.g. a different pack size). "
                          if any(r.get("held") for r in manual) else
                          "In-stock candidates exist but none is provably the same "
                          "product (title numbers or listing photo differ, e.g. a "
                          "different pack size). ") + "; ".join(line(r) for r in manual),
            "expected_impact": "Avoids auto-swapping in a different product.",
            "answer_picks": manual,
        })
    return recs
