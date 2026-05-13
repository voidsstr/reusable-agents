"""Built-in tools for OpenAI-shape providers (Copilot, Azure OpenAI,
OpenAI direct) so research-style agents that previously only worked on
the claude-cli provider can run on any backend.

Why this exists:
  Several agents (benchmark-research, competitor-research,
  product-hydration) issued web_search / WebFetch calls implicitly
  because they ran on `claude --print` which has those tools built in.
  When operators flipped them to a non-claude provider via the
  per-agent override, the agents went silent / stuck — the underlying
  model got the prompt asking it to "search the web" but had no tool
  to do so.

  This module provides:
    1. Two function-tool implementations (`web_search`, `web_fetch`)
       that work over plain HTTP using `httpx` (already a framework
       dependency).
    2. OpenAI-tool-schema specs (`OPENAI_TOOL_SPECS`) the AIClient
       hands to the model so it knows what's available.
    3. A `default_runner(name, args)` callable the AIClient invokes
       when the model returns a tool_call.

Caller responsibilities:
  - Provider clients pass `tools=OPENAI_TOOL_SPECS` and a runner.
  - The model decides when/whether to call tools.
  - Errors inside a tool body are caught and returned as a string so
    the model can recover instead of the loop crashing.

Search backend: DuckDuckGo HTML (no API key required). It's not a
research-grade index, but it's sufficient to find canonical review
sites — which is what the typical agent prompt asks for. Operators
who want a higher-quality index can set BRAVE_API_KEY to switch to
Brave Search.
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
from typing import Any, Callable, Optional
from urllib.parse import quote, urlparse

import httpx


logger = logging.getLogger("framework.tools")


_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 "
    "ReusableAgents/1.0"
)
_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)


# ── web_search ────────────────────────────────────────────────────────────

def _search_brave(query: str, num_results: int) -> list[dict]:
    key = os.environ.get("BRAVE_API_KEY", "")
    if not key:
        return []
    try:
        r = httpx.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": min(num_results, 20)},
            headers={"X-Subscription-Token": key, "Accept": "application/json"},
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        web = r.json().get("web", {}).get("results", [])
        return [
            {"title": x.get("title", ""), "url": x.get("url", ""),
             "snippet": x.get("description", "")}
            for x in web[:num_results]
        ]
    except Exception as e:
        logger.warning("brave search failed: %s", e)
        return []


_DDG_RESULT_RE = re.compile(
    r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>'
    r'.*?<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
    re.DOTALL | re.IGNORECASE,
)


def _strip_tags(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def _search_ddg(query: str, num_results: int) -> list[dict]:
    # html.duckduckgo.com is the no-JS variant — stable HTML, no rate-limit
    # in practice for occasional research traffic. Returns ~30 results per
    # page; we slice the top N.
    try:
        r = httpx.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query, "kl": "us-en"},
            headers={
                "User-Agent": _USER_AGENT,
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": "https://duckduckgo.com/",
            },
            timeout=_TIMEOUT,
            follow_redirects=True,
        )
        r.raise_for_status()
    except Exception as e:
        logger.warning("ddg search failed: %s", e)
        return []
    out: list[dict] = []
    for m in _DDG_RESULT_RE.finditer(r.text):
        url = m.group(1)
        # DDG rewrites real URLs through /l/?uddg= — extract the original.
        if url.startswith("/") or "duckduckgo.com" in url:
            mu = re.search(r"uddg=([^&]+)", url)
            if mu:
                from urllib.parse import unquote
                url = unquote(mu.group(1))
            else:
                continue
        out.append({
            "title": _strip_tags(m.group(2))[:200],
            "url": url,
            "snippet": _strip_tags(m.group(3))[:400],
        })
        if len(out) >= num_results:
            break
    return out


def web_search(query: str, num_results: int = 5) -> list[dict]:
    """Return up to N {title,url,snippet} results. Brave if BRAVE_API_KEY
    is set, else DuckDuckGo HTML."""
    if not query or not query.strip():
        return []
    n = max(1, min(int(num_results or 5), 15))
    results = _search_brave(query, n) if os.environ.get("BRAVE_API_KEY") else []
    if not results:
        results = _search_ddg(query, n)
    return results


# ── web_fetch ─────────────────────────────────────────────────────────────

_BLOCK_TAGS = re.compile(
    r"<(script|style|noscript|svg|nav|footer|header|aside|form)[^>]*>.*?</\1>",
    re.DOTALL | re.IGNORECASE,
)


def _html_to_text(body: str, max_chars: int) -> str:
    body = _BLOCK_TAGS.sub(" ", body)
    body = re.sub(r"<br\s*/?>", "\n", body, flags=re.IGNORECASE)
    body = re.sub(r"</p>", "\n\n", body, flags=re.IGNORECASE)
    body = re.sub(r"<[^>]+>", " ", body)
    body = html.unescape(body)
    body = re.sub(r"[ \t]+", " ", body)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    if len(body) > max_chars:
        body = body[:max_chars] + f"\n\n…[truncated, {len(body) - max_chars} more chars]"
    return body


def web_fetch(url: str, max_chars: int = 20000) -> str:
    """Fetch URL, return readable text. Truncates to `max_chars`."""
    if not url:
        return "ERROR: empty url"
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return f"ERROR: unsupported scheme: {parsed.scheme!r}"
    try:
        r = httpx.get(
            url,
            headers={
                "User-Agent": _USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=_TIMEOUT,
            follow_redirects=True,
        )
    except httpx.TimeoutException:
        return f"ERROR: timeout fetching {url}"
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"
    if r.status_code >= 400:
        return f"ERROR: HTTP {r.status_code} fetching {url}"
    ct = r.headers.get("content-type", "").lower()
    if "html" in ct or "xml" in ct or ct == "":
        return _html_to_text(r.text, max_chars)
    if "json" in ct:
        try:
            return json.dumps(r.json(), indent=2)[:max_chars]
        except Exception:
            return r.text[:max_chars]
    if ct.startswith("text/"):
        return r.text[:max_chars]
    return f"ERROR: unsupported content-type: {ct}"


# ── OpenAI tool specs ─────────────────────────────────────────────────────

OPENAI_TOOL_SPECS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the web for pages matching a query. Returns up to "
                "`num_results` results, each with title, url, and a short "
                "snippet. Use this to find authoritative sources before "
                "fetching their full content with web_fetch."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string",
                              "description": "Search query string."},
                    "num_results": {"type": "integer",
                                    "description": "How many results to return (1-15). Default 5.",
                                    "default": 5},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": (
                "Fetch a URL and return its readable text content. HTML is "
                "stripped to plain text. Truncates very long pages. Use "
                "after web_search to read the contents of a page."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string",
                            "description": "Absolute http(s) URL to fetch."},
                    "max_chars": {"type": "integer",
                                  "description": "Max characters to return. Default 20000.",
                                  "default": 20000},
                },
                "required": ["url"],
            },
        },
    },
]


# ── Default runner ────────────────────────────────────────────────────────

def default_runner(name: str, args: dict) -> str:
    """Execute one tool call and return its output as a string. Errors are
    stringified rather than raised so the conversation loop can continue
    and let the model self-correct (typo'd URL, bad query, etc)."""
    try:
        if name == "web_search":
            results = web_search(args.get("query", ""),
                                 args.get("num_results", 5))
            if not results:
                return "No results."
            lines = []
            for i, r in enumerate(results, 1):
                lines.append(f"[{i}] {r['title']}\n{r['url']}\n{r['snippet']}\n")
            return "\n".join(lines)
        if name == "web_fetch":
            return web_fetch(args.get("url", ""),
                             args.get("max_chars", 20000))
        return f"ERROR: unknown tool {name!r}"
    except Exception as e:  # never let a tool crash the loop
        return f"ERROR: {type(e).__name__}: {e}"


ToolRunner = Callable[[str, dict], str]
