"""Trending-topics signal for content-authoring agents.

Pulls Google Trends Daily RSS (no auth, no API key, no pytrends dep) and
exposes a stable interface every article-author agent can call once per
run to bias proposals toward what real people are searching for *today*.

Why a framework primitive: both aisleprompt (food trends) and specpicks
(tech / gaming trends) want the same signal class — "what's spiking in
US search today, filtered to my audience." Without this every site
would copy-paste the RSS fetcher; the trending-recipe-discovery and
specpicks/scripts/scrape-trending-topics.ts proved that.

Sources (in order of preference):

  1. Google Trends daily RSS (`trends.google.com/trending/rss`) — the
     authoritative source. Two endpoint shapes are tried; older URL
     redirects to newer.
  2. Reddit RSS — `/r/food`, `/r/cooking`, `/r/gaming`, `/r/buildapc`
     etc. depending on audience.
  3. Per-site custom RSS — sites can configure additional RSS URLs in
     `config/trends-config.json`.

Output is normalized to a list of `TrendTerm` dataclasses (term, source,
audience, score). `score` is 1.0 for Google Trends; lower for ancillary
sources. The article-author agent picks the top N for its audience and
injects them into the LLM prompt as a "biased toward this query" hint.

This module is intentionally network-tolerant:
  - All fetches time out at 8s (config-overridable).
  - Failures swallow silently and return [] for that source — the
    agent must still produce a run even when Google Trends is down.
  - Results are cached to storage at
    `agents/<agent_id>/state/trends-cache.json` with a TTL (default 6h)
    so an agent that fires every 30 minutes doesn't pound the RSS feed.
"""
from __future__ import annotations

import dataclasses
import datetime
import json
import re
import time
import urllib.request
from typing import Iterable


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class TrendTerm:
    """One trending search term."""
    term: str
    source: str           # "google-trends" | "reddit:r/food" | ...
    audience: str         # "food" | "tech" | "gaming" | "general"
    score: float          # 1.0 = top-ranked from primary source; <1 = ancillary

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


# ---------------------------------------------------------------------------
# Audience keyword filters
# ---------------------------------------------------------------------------

_FOOD_KEYWORDS = {
    "recipe", "recipes", "cook", "cooking", "bake", "baked", "baking",
    "dish", "dinner", "lunch", "breakfast", "brunch", "snack", "dessert",
    "meal", "ingredient", "ingredients", "kitchen", "grill", "grilling",
    "bbq", "smoker", "smoke", "smoked", "roast", "roasted", "fried",
    "saute", "boil", "boiled", "steam", "steamed", "marinade", "spice",
    "sauce", "soup", "stew", "salad", "pasta", "pizza", "burger",
    "taco", "burrito", "sandwich", "wing", "wings", "chicken", "beef",
    "pork", "lamb", "fish", "salmon", "shrimp", "tuna", "tofu",
    "vegan", "vegetarian", "keto", "paleo", "mediterranean", "diet",
    "drink", "cocktail", "coffee", "tea", "wine", "beer", "smoothie",
    "watermelon", "berries", "berry", "apple", "tomato", "pepper",
    "garlic", "onion", "potato", "rice", "noodle", "bread", "cake",
    "pie", "cookie", "ice cream", "cheesecake", "brownie",
    "memorial day", "fourth of july", "thanksgiving", "christmas",
    "easter", "valentine", "halloween", "labor day", "cinco de mayo",
    "super bowl", "cookout", "tailgate", "party",
}

_TECH_KEYWORDS = {
    "ai", "gpt", "claude", "openai", "anthropic", "llm", "model",
    "chip", "cpu", "gpu", "intel", "amd", "ryzen", "nvidia", "radeon",
    "geforce", "snapdragon", "apple", "iphone", "macbook", "ipad",
    "android", "pixel", "samsung", "galaxy", "windows", "linux",
    "ubuntu", "debian", "macos", "ios",
    "console", "ps5", "ps6", "xbox", "switch", "steam deck",
    "rog", "asus", "razer", "logitech", "keyboard", "mouse",
    "monitor", "display", "oled", "qled", "4k", "8k", "hdr",
    "router", "wifi", "5g", "6g", "vpn",
    "framework", "thinkpad", "laptop", "tablet", "ssd", "hdd",
    "ram", "ddr5", "pcie", "thunderbolt", "usb-c",
    "github", "gitlab", "vscode", "neovim", "cursor", "copilot",
    "kubernetes", "docker", "rust", "python", "javascript", "typescript",
}

_GAMING_KEYWORDS = {
    "game", "games", "gaming", "esports", "stream", "twitch", "discord",
    "fortnite", "minecraft", "roblox", "valorant", "league of legends",
    "lol", "dota", "csgo", "cs2", "apex", "warzone", "elden ring",
    "baldur", "starfield", "cyberpunk", "fallout", "skyrim",
    "nintendo", "playstation", "xbox", "steam", "epic games",
}

_DENY_KEYWORDS = {
    "celebrity gossip", "tragedy", "obituary", "murder", "shooting",
    "scandal", "lawsuit", "divorce",
    # Politics
    "trump", "biden", "election", "congress", "senate",
    # Non-evergreen short-news / sports we don't cover
    "nba", "nfl", "mlb", "fifa", "uefa", "premier league",
}


def _audience_match(term: str, audience: str) -> bool:
    """True if `term` looks relevant to `audience`."""
    lc = term.lower()
    if any(w in lc for w in _DENY_KEYWORDS):
        return False
    if audience == "general":
        return True
    if audience == "food":
        return any(w in lc for w in _FOOD_KEYWORDS)
    if audience == "tech":
        return (any(w in lc for w in _TECH_KEYWORDS)
                or any(w in lc for w in _GAMING_KEYWORDS))
    if audience == "gaming":
        return any(w in lc for w in _GAMING_KEYWORDS)
    return True


# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------

_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def _fetch(url: str, *, timeout: int = 8) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA,
        "Accept": "application/rss+xml, text/xml, */*",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _parse_rss_titles(body: bytes) -> list[str]:
    """Parse both RSS 2.0 (`<item>`) and Atom (`<entry>`) feed shapes —
    Reddit serves Atom under the `.rss` URL, Google Trends serves RSS.
    """
    text = body.decode("utf-8", errors="ignore")
    items: list[str] = (
        re.findall(r"<item\b[\s\S]*?</item>", text)
        + re.findall(r"<entry\b[\s\S]*?</entry>", text)
    )
    out: list[str] = []
    for it in items:
        m = re.search(r"<title[^>]*>(?:<!\[CDATA\[)?([^<\]]+?)(?:\]\]>)?</title>", it)
        if not m:
            continue
        t = (m.group(1) or "").strip()
        t = re.sub(r"\s+", " ", t)
        if 3 <= len(t) <= 200:
            out.append(t)
    return out


def google_trends(audience: str = "general",
                  *, timeout: int = 8,
                  include_general_fallback: bool = True) -> list[TrendTerm]:
    """Top daily trends from Google Trends RSS, filtered to audience.

    When `include_general_fallback=True` and no audience-specific trends
    matched, returns the top 5 unfiltered trends at a reduced score so
    the LLM still has cultural-moment context. The dedicated
    seasonal_calendar carries the always-relevant holiday signal, so
    this is the noisy/short-term spike layer.
    """
    candidates = (
        "https://trends.google.com/trending/rss?geo=US",
        "https://trends.google.com/trends/trendingsearches/daily/rss?geo=US",
    )
    titles: list[str] = []
    for url in candidates:
        try:
            body = _fetch(url, timeout=timeout)
            titles = _parse_rss_titles(body)
            if titles:
                break
        except Exception:
            continue
    out: list[TrendTerm] = []
    for i, t in enumerate(titles):
        if not _audience_match(t, audience):
            continue
        score = 1.0 - 0.5 * (i / max(1, len(titles) - 1))
        out.append(TrendTerm(term=t, source="google-trends",
                             audience=audience, score=round(score, 3)))
    if not out and include_general_fallback and titles:
        # General-zeitgeist fallback at reduced score
        for i, t in enumerate(titles[:5]):
            if any(w in t.lower() for w in _DENY_KEYWORDS):
                continue
            out.append(TrendTerm(term=t, source="google-trends",
                                 audience="general",
                                 score=round(0.4 - 0.05 * i, 3)))
    return out


def reddit_subreddit(subreddit: str, audience: str,
                     *, timeout: int = 8,
                     score: float = 0.6) -> list[TrendTerm]:
    """Top posts from /r/<subreddit>/.rss — useful for niche signal."""
    url = f"https://www.reddit.com/r/{subreddit}/.rss"
    try:
        body = _fetch(url, timeout=timeout)
    except Exception:
        return []
    titles = _parse_rss_titles(body)
    out: list[TrendTerm] = []
    for t in titles[:20]:
        # Strip Reddit's bracketed flair
        t = re.sub(r"\[[^\]]*\]", "", t).strip()
        if len(t) < 4:
            continue
        if not _audience_match(t, audience):
            continue
        out.append(TrendTerm(term=t, source=f"reddit:r/{subreddit}",
                             audience=audience, score=score))
    return out


# ---------------------------------------------------------------------------
# Public entry — cached aggregate
# ---------------------------------------------------------------------------

def fetch_trends(*, audience: str = "general",
                 extra_subreddits: Iterable[str] = (),
                 timeout: int = 8) -> list[TrendTerm]:
    """Fetch + filter trending terms for the given audience.

    Combines Google Trends + audience-appropriate subreddit RSS. Sorts
    by score desc.
    """
    seen: set[str] = set()
    out: list[TrendTerm] = []
    for t in google_trends(audience=audience, timeout=timeout):
        if t.term.lower() in seen:
            continue
        seen.add(t.term.lower())
        out.append(t)
    # Audience-default subreddits
    defaults: dict[str, tuple[str, ...]] = {
        "food":   ("food", "cooking", "recipes", "MealPrepSunday"),
        "tech":   ("technology", "hardware", "buildapc", "gadgets"),
        "gaming": ("gaming", "buildapc", "patientgamers"),
    }
    subs: tuple[str, ...] = tuple(defaults.get(audience, ()) + tuple(extra_subreddits))
    for sub in subs:
        for t in reddit_subreddit(sub, audience=audience, timeout=timeout):
            if t.term.lower() in seen:
                continue
            seen.add(t.term.lower())
            out.append(t)
    out.sort(key=lambda x: -x.score)
    return out


def fetch_trends_cached(storage,
                        agent_id: str,
                        *,
                        audience: str = "general",
                        ttl_seconds: int = 6 * 3600,
                        extra_subreddits: Iterable[str] = (),
                        timeout: int = 8) -> list[TrendTerm]:
    """fetch_trends() with a per-agent storage-cache so repeated runs
    don't pound the RSS feed. TTL defaults to 6h. Empty results aren't
    cached (lets the next run retry).
    """
    cache_key = f"agents/{agent_id}/state/trends-cache.json"
    now = int(time.time())
    try:
        cached = storage.read_json(cache_key) or {}
        if (now - int(cached.get("fetched_at", 0))) < ttl_seconds:
            audience_cache = cached.get("by_audience", {}).get(audience) or []
            if audience_cache:
                return [TrendTerm(**t) for t in audience_cache]
    except Exception:
        cached = {}

    fresh = fetch_trends(audience=audience,
                         extra_subreddits=extra_subreddits,
                         timeout=timeout)
    if fresh:
        try:
            cur = cached if isinstance(cached, dict) else {}
            by_aud = cur.setdefault("by_audience", {})
            by_aud[audience] = [t.to_dict() for t in fresh]
            cur["fetched_at"] = now
            storage.write_json(cache_key, cur)
        except Exception:
            pass
    return fresh


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------

def build_prompt_block(trends: list[TrendTerm],
                       *,
                       limit: int = 18) -> str:
    """Render the top trending terms as a prompt block."""
    if not trends:
        return ""
    lines = [
        "TRENDING TODAY (US Google Trends + audience subreddits, "
        f"top {min(limit, len(trends))} by score):"
    ]
    for t in trends[:limit]:
        lines.append(f"  - {t.term}  ({t.source}, score={t.score:.2f})")
    lines.append("")
    lines.append(
        "When proposing articles, prefer titles that hook directly into "
        "these trending phrases when there's a fit. If a trend has no "
        "clear angle to your site's audience, skip it — don't force a "
        "weak proposal."
    )
    lines.append("")
    return "\n".join(lines)
