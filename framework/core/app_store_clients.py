"""Thin clients for iTunes Search API + Google Play store metadata.

Goals:
  • No paid SDKs. iTunes API is public + free; Play uses a polite scrape
    of the public store HTML.
  • Each `search_*` returns a list of normalized dicts with the same
    keys regardless of store, so the agent treats both alike.

Normalized app shape:
{
  "store":            "ios" | "android",
  "store_id":         <str>,
  "store_url":        <str>,
  "name":             <str>,
  "publisher":        <str>,
  "category":         <str>,
  "country":          <str>,
  "price":            "free" | "<usd>",
  "average_rating":   <float>,
  "review_count":     <int>,
  "current_version":  <str>,
  "released_at":      <iso-date>,
  "last_updated_at":  <iso-date>,
  "days_since_update": <int>,
  "version_count_12mo": <int>,
  "description":      <str>,         # used downstream for LLM analysis
  "screenshots":      [<url>, ...],  # caps at 5
}

The Play scraper depends on bs4 + requests + a couple of regex hits on
the bundled __NEXT_DATA__ JSON. Google occasionally re-shuffles the page
layout — when that happens, fall back to skipping Play (don't crash the
whole run) and surface the iOS results alone.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote_plus

import requests

_UA = (
    "Mozilla/5.0 (compatible; reusable-agents/app-store-opportunity-agent; "
    "+https://github.com/anthropics/claude-code)"
)
_TIMEOUT = 20


def _days_since(iso_date: str) -> int:
    if not iso_date:
        return 0
    try:
        d = datetime.fromisoformat(iso_date.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - d).days
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# iTunes / Apple App Store
# ---------------------------------------------------------------------------

_ITUNES_SEARCH = "https://itunes.apple.com/search"
_ITUNES_LOOKUP = "https://itunes.apple.com/lookup"


def _itunes_normalize(r: dict, country: str) -> dict:
    track_id = str(r.get("trackId") or "")
    return {
        "store": "ios",
        "store_id": track_id,
        "store_url": r.get("trackViewUrl", ""),
        "name": r.get("trackName") or r.get("trackCensoredName") or "",
        "publisher": r.get("artistName", ""),
        "category": r.get("primaryGenreName", ""),
        "country": country,
        "price": "free" if (r.get("price") in (0, 0.0)) else f"{r.get('price')}",
        "average_rating": float(r.get("averageUserRating") or 0),
        "review_count": int(r.get("userRatingCount") or 0),
        "current_version": r.get("version", ""),
        "released_at": (r.get("releaseDate") or "")[:10],
        "last_updated_at": (r.get("currentVersionReleaseDate") or "")[:10],
        "days_since_update": _days_since(r.get("currentVersionReleaseDate") or ""),
        "version_count_12mo": 0,  # iTunes Search doesn't expose history; left for
                                  # downstream enrichment if you wire SensorTower.
        "description": r.get("description", ""),
        "screenshots": list(r.get("screenshotUrls") or [])[:5],
    }


def search_itunes(query: str, *, country: str = "us", limit: int = 25) -> list[dict]:
    """Top app results for `query` from the App Store via iTunes Search API."""
    params = {
        "term": query,
        "country": country,
        "media": "software",
        "entity": "software",
        "limit": str(min(limit, 200)),
    }
    try:
        resp = requests.get(_ITUNES_SEARCH, params=params, timeout=_TIMEOUT,
                            headers={"User-Agent": _UA})
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return []
    return [_itunes_normalize(r, country) for r in (data.get("results") or [])
            if r.get("trackId")]


def lookup_itunes(track_ids: list[str], *, country: str = "us") -> list[dict]:
    """Fetch fresh metadata for a known list of iTunes track IDs."""
    out: list[dict] = []
    if not track_ids:
        return out
    # iTunes lookup caps at 200 per call.
    for i in range(0, len(track_ids), 100):
        chunk = track_ids[i:i + 100]
        try:
            resp = requests.get(
                _ITUNES_LOOKUP,
                params={"id": ",".join(chunk), "country": country},
                timeout=_TIMEOUT,
                headers={"User-Agent": _UA},
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            continue
        out.extend(_itunes_normalize(r, country) for r in (data.get("results") or [])
                   if r.get("trackId"))
        time.sleep(0.2)  # be polite
    return out


# ---------------------------------------------------------------------------
# Google Play
# ---------------------------------------------------------------------------

_PLAY_SEARCH_URL = "https://play.google.com/store/search?q={q}&c=apps&hl=en&gl={country}"
_PLAY_DETAIL_URL = "https://play.google.com/store/apps/details?id={pkg}&hl=en&gl={country}"


def _play_get(url: str) -> str | None:
    try:
        r = requests.get(url, timeout=_TIMEOUT, headers={"User-Agent": _UA})
        if r.status_code != 200:
            return None
        return r.text
    except Exception:
        return None


_PLAY_PKG_RE = re.compile(r"/store/apps/details\?id=([\w\.]+)")


def _play_search_packages(query: str, country: str, limit: int) -> list[str]:
    html = _play_get(_PLAY_SEARCH_URL.format(q=quote_plus(query), country=country))
    if not html:
        return []
    seen: list[str] = []
    seen_set: set[str] = set()
    for m in _PLAY_PKG_RE.finditer(html):
        pkg = m.group(1)
        if pkg in seen_set:
            continue
        seen_set.add(pkg)
        seen.append(pkg)
        if len(seen) >= limit:
            break
    return seen


def _play_detail(pkg: str, country: str) -> dict | None:
    html = _play_get(_PLAY_DETAIL_URL.format(pkg=pkg, country=country))
    if not html:
        return None
    # Play stuffs the structured app metadata into one big AF_initDataCallback
    # blob. We extract a few specific signals via tolerant regex — the schema
    # shifts but these landmarks have been stable.
    def _grab(pattern: str, default: Any = "") -> Any:
        m = re.search(pattern, html, re.DOTALL)
        return m.group(1).strip() if m else default

    name = _grab(r'<meta itemprop="name" content="([^"]+)"')
    publisher = _grab(r'"applicationCategory".*?"name"\s*:\s*"([^"]+)"', "") or _grab(
        r'<a [^>]*href="/store/apps/dev[^"]*"[^>]*>\s*<span[^>]*>([^<]+)</span>', "")
    rating_text = _grab(r'"ratingValue":\s*"([\d\.]+)"', "0")
    review_text = _grab(r'"reviewCount"\s*:\s*"?(\d+)', "0")
    version = _grab(r'\[\[\["([\d\.]+)"\],?\d+,"version"', "")
    last_updated = _grab(r'Updated on</div><div[^>]*>([^<]+)<', "")
    category = _grab(r'itemprop="genre">([^<]+)<', "")
    price = _grab(r'"price":\s*"([^"]+)"', "free")
    description = _grab(r'<meta itemprop="description" content="([^"]+)"', "")

    # Date parsing: "Apr 23, 2024" -> 2024-04-23
    last_updated_iso = ""
    if last_updated:
        try:
            d = datetime.strptime(last_updated, "%b %d, %Y")
            last_updated_iso = d.strftime("%Y-%m-%d")
        except Exception:
            pass

    if not name:
        return None
    return {
        "store": "android",
        "store_id": pkg,
        "store_url": f"https://play.google.com/store/apps/details?id={pkg}",
        "name": name,
        "publisher": publisher or "",
        "category": category,
        "country": country,
        "price": "free" if price.lower() in ("free", "0", "$0.00") else price,
        "average_rating": float(rating_text or 0),
        "review_count": int(review_text or 0),
        "current_version": version,
        "released_at": "",
        "last_updated_at": last_updated_iso,
        "days_since_update": _days_since(last_updated_iso),
        "version_count_12mo": 0,
        "description": description,
        "screenshots": [],
    }


def search_play(query: str, *, country: str = "us", limit: int = 25) -> list[dict]:
    pkgs = _play_search_packages(query, country, limit)
    out: list[dict] = []
    for pkg in pkgs:
        d = _play_detail(pkg, country)
        if d:
            out.append(d)
        time.sleep(0.3)  # polite — we're scraping
    return out


# ---------------------------------------------------------------------------
# Top-charts discovery — pulls "popular and highly used" apps fleet-wide.
# Uses Apple's legacy RSS feeds (stable for 15+ years, no API key needed).
# Each feed returns top N apps for one (country, kind, optional genre)
# combination. Combined with `lookup_itunes()` to pull full metadata.
# ---------------------------------------------------------------------------

# iTunes app genres (https://rss.applemarketingtools.com/genre-ids).
# Top-level numeric IDs the legacy RSS feed accepts via `genre=<id>`.
ITUNES_GENRES: dict[int, str] = {
    6000: "Business",
    6001: "Weather",
    6002: "Utilities",
    6003: "Travel",
    6004: "Sports",
    6005: "Social Networking",
    6006: "Reference",
    6007: "Productivity",
    6008: "Photo & Video",
    6009: "News",
    6010: "Navigation",
    6011: "Music",
    6012: "Lifestyle",
    6013: "Health & Fitness",
    6014: "Games",
    6015: "Finance",
    6016: "Entertainment",
    6017: "Education",
    6018: "Books",
    6020: "Medical",
    6022: "Catalogs",
    6023: "Food & Drink",
    6024: "Shopping",
    6026: "Developer Tools",
    6027: "Graphics & Design",
}

_RSS_KIND_MAP = {
    "free":     "topfreeapplications",
    "paid":     "toppaidapplications",
    "grossing": "topgrossingapplications",
    "new":      "newapplications",
}


def top_apps_itunes(
    *,
    country: str = "us",
    kind: str = "free",
    limit: int = 100,
    genre_id: int | None = None,
) -> list[dict]:
    """Return the top N apps for a country / kind / optional genre.

    `kind` is one of "free", "paid", "grossing", "new".
    `genre_id` is one of ITUNES_GENRES.keys() (numeric) — None = all genres.

    Returns normalized dicts (same shape as `search_itunes`). Apple's RSS
    only includes basic fields, so we follow up with `lookup_itunes()` to
    pull rating, review count, last_updated, description, etc.
    """
    feed = _RSS_KIND_MAP.get(kind)
    if not feed:
        raise ValueError(f"unknown kind {kind!r}; pick one of {list(_RSS_KIND_MAP)}")
    parts = [f"https://itunes.apple.com/{country}/rss/{feed}/limit={min(limit,200)}"]
    if genre_id:
        parts.append(f"genre={genre_id}")
    parts.append("json")
    url = "/".join(parts)
    try:
        resp = requests.get(url, timeout=_TIMEOUT, headers={"User-Agent": _UA})
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return []
    entries = (data.get("feed") or {}).get("entry") or []
    if not isinstance(entries, list):
        return []
    track_ids: list[str] = []
    for e in entries:
        tid = (e.get("id") or {}).get("attributes", {}).get("im:id")
        if tid:
            track_ids.append(str(tid))
    return lookup_itunes(track_ids, country=country)


def sweep_itunes_top_charts(
    *,
    country: str = "us",
    kinds: tuple[str, ...] = ("free", "paid", "grossing"),
    overall_limit: int = 100,
    genre_ids: list[int] | None = None,
    per_genre_limit: int = 50,
    exclude_genres: tuple[int, ...] = (6014, 6025),  # Games, Stickers — too noisy by default
) -> list[dict]:
    """Bulk top-charts sweep — pulls each `kind` overall + per-genre and
    deduplicates by track_id. Use this for "find me popular apps to
    replace" workloads. Returns a list of normalized app dicts.

    Defaults exclude Games (massive volume, distinct competitive
    dynamics) and Stickers (not really apps).

    To include EVERY genre, pass `genre_ids=list(ITUNES_GENRES.keys())`.
    To skip per-genre and only do overall: `genre_ids=[]`.
    """
    seen_ids: set[str] = set()
    out: list[dict] = []

    for kind in kinds:
        for app in top_apps_itunes(country=country, kind=kind, limit=overall_limit):
            sid = str(app.get("store_id") or "")
            if sid and sid not in seen_ids:
                seen_ids.add(sid)
                out.append(app)
        time.sleep(0.3)

    if genre_ids is None:
        genre_ids = [g for g in ITUNES_GENRES if g not in exclude_genres]
    for gid in genre_ids:
        for kind in kinds:
            for app in top_apps_itunes(
                country=country, kind=kind,
                limit=per_genre_limit, genre_id=gid,
            ):
                sid = str(app.get("store_id") or "")
                if sid and sid not in seen_ids:
                    seen_ids.add(sid)
                    out.append(app)
            time.sleep(0.3)

    return out


def discover_country_gaps(
    *,
    popular_in_countries: list[str],
    missing_in_countries: list[str],
    kinds: tuple[str, ...] = ("free", "grossing"),
    overall_limit: int = 50,
    per_genre_limit: int = 25,
    genre_ids: list[int] | None = None,
) -> list[dict]:
    """Find apps popular in `popular_in_countries` but absent from
    `missing_in_countries`.

    For each app discovered in the popular markets, we issue an
    `iTunes lookup` per missing-country and check if the response is
    empty (= app not available there). An app that's popular in 2+
    "have it" markets and absent from 2+ "don't have it" markets is
    a strong "regional gap" candidate — there's demand the existing
    app doesn't reach.

    Each returned app dict has these extra fields:
      • `gap_present_in`:  countries where the app IS available
                            (subset of popular_in_countries)
      • `gap_absent_in`:   countries where the app is NOT available
                            (subset of missing_in_countries)
      • `gap_score`:       int — len(present_in) × len(absent_in)
                            (higher = stronger regional-gap signal)
    """
    seen: dict[str, dict] = {}
    for c in popular_in_countries:
        for app in sweep_itunes_top_charts(
            country=c, kinds=kinds,
            overall_limit=overall_limit,
            genre_ids=genre_ids,
            per_genre_limit=per_genre_limit,
        ):
            key = str(app.get("store_id") or "")
            if not key:
                continue
            entry = seen.setdefault(key, {**app, "gap_present_in": [],
                                            "gap_absent_in": []})
            if c not in entry["gap_present_in"]:
                entry["gap_present_in"].append(c)
        time.sleep(0.3)

    # For each candidate app, check absence in the missing markets.
    out: list[dict] = []
    for key, app in seen.items():
        # Only worth checking if it's popular in 2+ source countries
        # (single-country popularity is too noisy a signal).
        if len(app.get("gap_present_in", [])) < 2:
            continue
        absent_in: list[str] = []
        for c in missing_in_countries:
            try:
                hits = lookup_itunes([key], country=c)
            except Exception:
                continue
            if not hits:
                absent_in.append(c)
            time.sleep(0.15)
        if len(absent_in) >= 2:
            app["gap_absent_in"] = absent_in
            app["gap_score"] = len(app["gap_present_in"]) * len(absent_in)
            out.append(app)
    out.sort(key=lambda a: -int(a.get("gap_score", 0)))
    return out
