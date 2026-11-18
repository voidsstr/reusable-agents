"""Content-policy check group for RENDERED pages (build item F6a).

The publish gate (framework/cli/article_publish_gate.py) checks article rows
before they go live. This module is the backstop on what visitors and
crawlers actually get: rendered HTML, whether it came from an article, a
product page, a hub an agent edited, or a template. Three callers:

  * the kit gate (K5) runs `python3 -m framework.cli.content_policy_scan`
    over the rendered fixture pages and fails the deploy on a violation;
  * a KTLO / site-consistency crawl of the live /guides/* pages
    (`crawl_check_group`; wiring into framework/cli/site_consistency_audit.py
    is build item W2);
  * an operator, with files, stdin or URLs.

What a page contributes (mapped onto the article fields the rules name):
  title    <title>, og:title and every <h1>
  excerpt  meta description, og:description, twitter:description
  body     everything else that renders as text (scripts and styles
           excluded), image alt text, and the string values of JSON-LD
           (FAQPage questions become level-7 headings over their answers)
Safety callouts on a rendered page are elements whose class matches
`callouts.html_class_re` (e.g. class="safety-callout") or whose attributes
match `callouts.html_attr_re` (data-callout="safety"), and blockquotes whose
text opens with a safety label (what markdown "> **Safety:** ..." renders
to). A callout longer than `callouts.max_chars` is plain text.

Third-party data the site does not author (an Amazon product title, a spec
table) is marked by the templates with data-content-policy="source" (any
attribute matching `callouts.html_skip_re`): that element and its subtree are
not scanned, on <title> and <meta> too. JSON-LD nodes whose @type is in
`callouts.jsonld_skip_types` (Product, Offer, ItemList, ...) are skipped the
same way. Authored copy can never carry the marker: the article check
(content_policy.check_article) rejects a body that does.

Network use is opt-in and polite: GET only, one request at a time, a byte
cap per page, never a path matching `never_fetch_re` (affiliate click-outs
such as /r/<ASIN> log a click) whether it comes from a sitemap, a crawl, a
command line or a redirect, and only the host being scanned (a redirect to
another host is refused, not followed).
"""
from __future__ import annotations

import dataclasses
import json as _json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any, Callable, Iterable, Mapping, Sequence

from framework.core.content_policy import (
    DEFAULT_CALLOUTS,
    Block,
    BlockBuilder,
    ContentPolicy,
    ContentPolicyAudit,
    ContentPolicyConfigError,
    check_blocks,
    resolve_policy,
    table_row_text,
)

CHECK_GROUP = "content-policy"
KIND_REJECT = "content-policy-violation"
KIND_NOTICE = "content-policy-notice-missing"
KIND_WARN = "content-policy-warning"
KIND_UNAVAILABLE = "content-policy-scan-unavailable"
DEFAULT_SEVERITY = {
    KIND_REJECT: "error",
    KIND_NOTICE: "error",
    KIND_WARN: "warn",
    KIND_UNAVAILABLE: "warn",
}
DEFAULT_UA = "Mozilla/5.0 (compatible; ContentPolicyScan/1.0; +ops)"
DEFAULT_NEVER_FETCH = r"^/(?:r|k|go)/|^/api/"
MAX_PAGE_BYTES = 2_000_000

_BLOCK_TAGS = {
    "p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "td", "th", "table", "thead", "tbody",
    "tfoot", "blockquote", "section", "article", "aside", "header", "footer", "nav", "main", "figure",
    "figcaption", "dt", "dd", "dl", "ul", "ol", "pre", "form", "details", "summary", "address", "caption",
    "hr", "fieldset", "legend", "label", "button", "option", "body", "html",
}
# Never "head": its end tag is optional, and a page without one would have
# its whole body skipped. Its children (title, meta, script, style) are
# handled one by one.
_SKIP_TAGS = {"script", "style", "template", "svg", "math", "iframe", "object", "canvas"}
_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source",
              "track", "wbr"}
_HEADING_TAGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
_META_TITLE = {"og:title", "twitter:title"}
_META_EXCERPT = {"description", "og:description", "twitter:description"}
_JSONLD_TEXT_KEYS = ("headline", "alternativeHeadline", "name", "description", "abstract", "articleBody",
                     "text", "reviewBody")


class _PageParser(HTMLParser):
    """HTML -> Blocks. Inline elements join text; block elements end it."""

    def __init__(self, callouts: Mapping[str, Any]):
        super().__init__(convert_charrefs=True)
        self.blocks: list[Block] = []
        self.b = BlockBuilder(self.blocks, "body", callouts)
        self.class_re = re.compile(str(callouts.get("html_class_re") or DEFAULT_CALLOUTS["html_class_re"]), re.I)
        self.attr_re = re.compile(str(callouts.get("html_attr_re") or DEFAULT_CALLOUTS["html_attr_re"]), re.I)
        self.label_re = re.compile(str(callouts.get("markdown_label_re") or DEFAULT_CALLOUTS["markdown_label_re"]),
                                   re.I)
        self.max_chars = int(callouts.get("max_chars") or DEFAULT_CALLOUTS["max_chars"])
        self.skip_re = re.compile(str(callouts.get("html_skip_re") or DEFAULT_CALLOUTS["html_skip_re"]), re.I)
        self.skipping: list[list] = []         # [tag, depth] of a data-content-policy="source" subtree
        self.lists: list[str] = []             # open ul / ol
        self.buf: list[str] = []
        self.stack: list[dict] = []            # open elements we track
        self.skip = 0
        self.in_title = False
        self.title_buf: list[str] = []
        self.jsonld: list[str] = []
        self.jsonld_buf: list[str] | None = None
        self.meta: list[tuple[str, str]] = []  # (field, text)
        self.tables: list[dict] = []           # {"header": [...], "row": [...] | None, "cell": [...] | None, "th_row": bool}
        self.notes: list[str] = []

    # -- helpers --------------------------------------------------------------
    def _callout_depth(self) -> bool:
        return any(e.get("callout") for e in self.stack)

    def _context_kind(self) -> tuple[str, int]:
        for e in reversed(self.stack):
            t = e["tag"]
            if t in _HEADING_TAGS:
                return "heading", _HEADING_TAGS[t]
            if t == "li":
                return "list_item", 0
            if t == "pre":
                return "code", 0
            if t == "blockquote":
                return "quote", 0
        return "paragraph", 0

    def _flush(self) -> None:
        if self.tables and self.tables[-1].get("cell") is not None:
            return                               # cell text is collected per cell
        text = "".join(self.buf)
        self.buf = []
        if not text.strip():
            return
        kind, level = self._context_kind()
        field = "title" if (kind == "heading" and level == 1) else "body"
        self.b.add(kind, text, level=level, callout=self._callout_depth(), field=field, raw=True,
                   ordered=bool(kind == "list_item" and self.lists and self.lists[-1] == "ol"))

    def _skip_attr(self, a: Mapping[str, str]) -> bool:
        return bool(self.skip_re.search(" ".join(f'{k}="{v}"' for k, v in a.items())))

    # -- HTMLParser hooks -----------------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        a = {k.lower(): (v or "") for k, v in attrs}
        if self.skipping:
            if tag == self.skipping[-1][0] and tag not in _VOID_TAGS:
                self.skipping[-1][1] += 1
            return
        if self._skip_attr(a):
            if tag in ("meta", "img", "br", "hr") or tag in _VOID_TAGS:
                return                                   # a marked void element: just skip it
            self._flush()
            self.skipping.append([tag, 1])
            return
        if tag == "title":
            self.in_title = True
            return
        if tag == "meta":
            key = (a.get("name") or a.get("property") or "").lower()
            if key in _META_TITLE and a.get("content"):
                self.meta.append(("title", a["content"]))
            elif key in _META_EXCERPT and a.get("content"):
                self.meta.append(("excerpt", a["content"]))
            return
        if tag == "script" and "ld+json" in a.get("type", "").lower():
            self.jsonld_buf = []
            self.skip += 1
            return
        if tag in _SKIP_TAGS:
            self.skip += 1
            return
        if self.skip:
            return
        if tag == "img":
            if a.get("alt"):
                self.buf.append(f" {a['alt']} ")
            return
        if tag == "br":
            self.buf.append(" ")
            return
        if tag in _VOID_TAGS:
            if tag == "hr":
                self._flush()
            return
        if tag in _BLOCK_TAGS:
            self._flush()
        attr_text = " ".join(f'{k}="{v}"' for k, v in a.items())
        callout = bool(self.class_re.search(a.get("class", "")) or self.attr_re.search(attr_text))
        entry = {"tag": tag, "callout": callout, "start": len(self.blocks), "chars": 0}
        if tag in ("ul", "ol"):
            self.b.end_list_run()
            self.lists.append(tag)
        if tag == "table":
            self.tables.append({"header": [], "row": None, "cell": None, "th_row": True, "in_head": False})
        elif self.tables:
            t = self.tables[-1]
            if tag == "thead":
                t["in_head"] = True
            elif tag == "tr":
                t["row"], t["th_row"] = [], True
            elif tag in ("td", "th") and t["row"] is not None:
                t["cell"] = []
                if tag == "td":
                    t["th_row"] = False
        self.stack.append(entry)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self.skipping:
            if tag == self.skipping[-1][0]:
                self.skipping[-1][1] -= 1
                if self.skipping[-1][1] <= 0:
                    self.skipping.pop()
            return
        if tag == "title":
            self.in_title = False
            return
        if tag == "script" and self.jsonld_buf is not None:
            self.jsonld.append("".join(self.jsonld_buf))
            self.jsonld_buf = None
            self.skip = max(0, self.skip - 1)
            return
        if tag in _SKIP_TAGS:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip or tag in _VOID_TAGS:
            return
        if not any(e["tag"] == tag for e in self.stack):
            return                                   # stray end tag
        if self.tables:
            t = self.tables[-1]
            if tag in ("td", "th") and t["cell"] is not None:
                t["row"].append("".join(t["cell"]).strip())
                t["cell"] = None
            elif tag == "tr" and t["row"] is not None:
                cells = t["row"]
                if t["in_head"] or (t["th_row"] and not t["header"]):
                    t["header"] = cells
                    self.b.add("table_row", " | ".join(c for c in cells if c), callout=self._callout_depth(),
                               raw=True)
                else:
                    self.b.add("table_row", table_row_text(cells, t["header"]), callout=self._callout_depth(),
                               raw=True)
                t["row"] = None
            elif tag == "thead":
                t["in_head"] = False
        if tag in _BLOCK_TAGS:
            self._flush()
        while self.stack:
            e = self.stack.pop()
            if e["tag"] == "table" and self.tables:
                self.tables.pop()
            if e["tag"] in ("ul", "ol"):
                self.b.end_list_run()
                if self.lists:
                    self.lists.pop()
            self._close_callout(e)
            if e["tag"] == tag:
                break

    def _close_callout(self, e: dict) -> None:
        """A blockquote opening with a safety label becomes a callout; any
        callout over max_chars is demoted to plain text."""
        start = e["start"]
        region = self.blocks[start:]
        if not region:
            return
        total = sum(len(b.text) for b in region)
        if e["tag"] == "blockquote" and not e["callout"] and not self._callout_depth():
            if self.label_re.search(region[0].text):
                if total <= self.max_chars:
                    self.blocks[start:] = [dataclasses.replace(b, callout=True) for b in region]
                else:
                    self.notes.append(f"a safety-labelled blockquote of {total} characters is over "
                                      f"{self.max_chars} and was checked as plain text")
        elif e["callout"] and total > self.max_chars and not self._callout_depth():
            self.blocks[start:] = [dataclasses.replace(b, callout=False) for b in region]
            self.notes.append(f"a safety callout of {total} characters is over {self.max_chars} and was "
                              f"checked as plain text")

    def handle_data(self, data: str) -> None:
        if self.skipping:
            return
        if self.jsonld_buf is not None:
            self.jsonld_buf.append(data)
            return
        if self.in_title:
            self.title_buf.append(data)
            return
        if self.skip:
            return
        if self.tables and self.tables[-1].get("cell") is not None:
            self.tables[-1]["cell"].append(data)
            return
        self.buf.append(data)

    def close(self) -> None:
        super().close()
        self._flush()
        if self.skipping:
            self.notes.append(f"an unclosed <{self.skipping[0][0]}> marked data-content-policy=source hid the "
                              f"rest of the page from the scan")


def _jsonld_types(node: Mapping[str, Any]) -> set[str]:
    t = node.get("@type")
    return {str(x).lower() for x in (t if isinstance(t, list) else [t]) if x}


def _jsonld_blocks(raw_docs: Sequence[str], b: BlockBuilder,
                   skip_types: Iterable[str] = DEFAULT_CALLOUTS["jsonld_skip_types"]) -> None:
    """String values of JSON-LD (what assistants and rich results quote).
    FAQPage questions become level-7 headings over their answers; each
    question's section ends after its answer, and each JSON-LD document
    starts with no open section, so a subject a question names never
    reaches unrelated data. Nodes of a type in `skip_types` (product and
    navigation data the site does not author) are skipped."""
    skip = {str(t).lower() for t in skip_types}

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for x in node:
                walk(x)
            return
        if not isinstance(node, dict):
            return
        types = _jsonld_types(node)
        if types & skip:
            return
        if "question" in types:
            q = node.get("name") or node.get("text") or ""
            ans = node.get("acceptedAnswer") or {}
            if isinstance(ans, list):
                ans = ans[0] if ans and isinstance(ans[0], dict) else {}
            a_text = ans.get("text") if isinstance(ans, dict) else ""
            if q:
                b.add("heading", str(q), level=7, raw=True)
            if a_text:
                b.add("paragraph", re.sub(r"<[^>]+>", " ", str(a_text)), raw=True)
            b.close_sections(7)
            return
        for k in _JSONLD_TEXT_KEYS:
            v = node.get(k)
            if isinstance(v, str) and v.strip():
                b.add("paragraph", re.sub(r"<[^>]+>", " ", v), raw=True)
        for k, v in node.items():
            if isinstance(v, (dict, list)) and k not in ("acceptedAnswer",):
                walk(v)

    for raw in raw_docs:
        b.reset_sections()
        try:
            walk(_json.loads(raw.strip()))
        except (ValueError, TypeError):
            continue


def html_blocks(html: str, callouts: Mapping[str, Any] | None = None) -> tuple[list[Block], list[str]]:
    """(blocks, notes) for one rendered page."""
    callouts = {**DEFAULT_CALLOUTS, **dict(callouts or {})}
    p = _PageParser(callouts)
    try:
        p.feed(html or "")
        p.close()
    except Exception as e:  # noqa: BLE001 - malformed markup must not stop a scan
        p.notes.append(f"HTML parse error ({type(e).__name__}: {str(e)[:80]}); partial page checked")
    blocks = p.blocks
    head: list[Block] = []
    title = re.sub(r"\s+", " ", "".join(p.title_buf)).strip()
    if title:
        head.append(Block("title", "title", title))
    for field, text in p.meta:
        t = re.sub(r"\s+", " ", text).strip()
        if t:
            head.append(Block(field, "title" if field == "title" else "meta", t))
    # The page's <h1> heads the page: like an article title in
    # content_policy.article_blocks, a subject it names carries into the
    # meta description and the JSON-LD (FAQ answers, the Article
    # description) as well as into the body under it.
    h1 = next((len(head) + k for k, b in enumerate(blocks)
               if b.kind == "heading" and b.level == 1 and b.field == "title"), None)
    top: tuple[int, ...] = (h1,) if h1 is not None else ()
    head = [dataclasses.replace(b, ancestors=top) if b.field == "excerpt" else b for b in head]
    if p.jsonld:
        tail: list[Block] = []
        _jsonld_blocks(p.jsonld, BlockBuilder(tail, "body", callouts),
                       callouts.get("jsonld_skip_types") or DEFAULT_CALLOUTS["jsonld_skip_types"])
        off = len(head) + len(blocks)
        tail = [dataclasses.replace(b, ancestors=top + tuple(a + off for a in b.ancestors)) for b in tail]
    else:
        tail = []
    off = len(head)
    body = [dataclasses.replace(b, ancestors=tuple(a + off for a in b.ancestors)) for b in blocks]
    return head + body + tail, p.notes


def audit_html(policy: ContentPolicy, html: str, *, url: str = "") -> ContentPolicyAudit:
    """Audit one rendered page."""
    if not policy.active:
        return ContentPolicyAudit(policy.rule_sets, [])
    blocks, notes = html_blocks(html, policy.callouts)
    return check_blocks(policy, blocks, url=url, notes=notes)


# ── scan reports ────────────────────────────────────────────────────────────

@dataclasses.dataclass
class PageResult:
    url: str
    status: int = 200
    audit: ContentPolicyAudit | None = None
    error: str = ""

    def to_dict(self) -> dict:
        d: dict = {"url": self.url, "status": self.status}
        if self.error:
            d["error"] = self.error
        if self.audit is not None:
            d.update(self.audit.to_dict())
        return d


@dataclasses.dataclass
class ScanReport:
    rule_sets: tuple[str, ...]
    pages: list[PageResult] = dataclasses.field(default_factory=list)

    @property
    def scanned(self) -> list[PageResult]:
        return [p for p in self.pages if p.audit is not None]

    @property
    def errors(self) -> list[PageResult]:
        return [p for p in self.pages if p.error]

    @property
    def rejects(self) -> int:
        return sum(len(p.audit.rejects) for p in self.scanned)

    @property
    def warnings(self) -> int:
        return sum(len(p.audit.warnings) for p in self.scanned)

    def to_dict(self) -> dict:
        return {"check_group": CHECK_GROUP, "rule_sets": list(self.rule_sets),
                "pages_scanned": len(self.scanned), "page_errors": len(self.errors),
                "rejects": self.rejects, "warnings": self.warnings,
                "pages": [p.to_dict() for p in self.pages]}


def scan_documents(policy: ContentPolicy, docs: Iterable[tuple[str, str]], *,
                   fmt: str = "html") -> ScanReport:
    """Scan (url-or-name, text) pairs. `fmt` is "html" or "markdown"."""
    from framework.core.content_policy import check_markdown

    rep = ScanReport(policy.rule_sets)
    for name, text in docs:
        audit = (check_markdown(policy, text) if fmt == "markdown" else audit_html(policy, text, url=name))
        if fmt == "markdown" and name:
            audit.violations = [dataclasses.replace(v, url=name) for v in audit.violations]
        rep.pages.append(PageResult(name, 200, audit))
    return rep


def findings(report: ScanReport, severity: Mapping[str, str] | None = None) -> list[dict]:
    """The report as site_consistency_audit-shaped findings
    ({severity, kind, url, message, ...}), one per violation and one per page
    that could not be scanned."""
    sev = {**DEFAULT_SEVERITY, **dict(severity or {})}
    out: list[dict] = []
    for p in report.pages:
        if p.error:
            out.append({"severity": sev[KIND_UNAVAILABLE], "kind": KIND_UNAVAILABLE, "url": p.url,
                        "message": f"content-policy scan could not read the page: {p.error}",
                        "group": CHECK_GROUP})
            continue
        for v in (p.audit.violations if p.audit else []):
            kind = KIND_NOTICE if v.kind == "notice" else (KIND_REJECT if v.action == "reject" else KIND_WARN)
            if v.kind == "notice" and v.action == "warn":
                kind = KIND_WARN
            msg = (f"{v.rule_id}: missing {v.label}; triggered by {v.excerpt[:160]!r}" if v.kind == "notice"
                   else f"{v.rule_id} ({v.label}): {v.excerpt[:160]!r}")
            out.append({"severity": sev[kind], "kind": kind, "url": p.url or v.url, "message": msg,
                        "rule": v.rule_id, "field": v.field, "excerpt": v.excerpt, "group": CHECK_GROUP})
    return out


# ── fetching (opt-in network) ───────────────────────────────────────────────

@dataclasses.dataclass
class Fetched:
    url: str
    status: int
    text: str = ""
    final_url: str = ""
    error: str = ""


Fetcher = Callable[[str], Fetched]


class RedirectRefused(urllib.error.URLError):
    """A redirect to another host or to a never-fetch path (an affiliate
    click-out) was refused rather than followed."""


class _GuardedRedirects(urllib.request.HTTPRedirectHandler):
    """Re-check every redirect hop: same host as the original request, and a
    path that does not match `never_re`. A /guides/x that 302s to /r/<ASIN>
    (and from there to Amazon with the associate tag) is never followed."""

    def __init__(self, never_re: "re.Pattern | None"):
        super().__init__()
        self.never_re = never_re

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401 - urllib hook
        target = urllib.parse.urljoin(req.full_url, newurl)
        origin = getattr(req, "_content_policy_origin", req.full_url)
        if not _allowed(target, origin, None, self.never_re):
            raise RedirectRefused(f"redirect to {target} refused (another host or a never-fetch path)")
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            new._content_policy_origin = origin
        return new


def http_fetcher(*, timeout: float = 20.0, headers: Mapping[str, str] | None = None,
                 max_bytes: int = MAX_PAGE_BYTES, pause_s: float = 0.0,
                 never_fetch_re: str = DEFAULT_NEVER_FETCH) -> Fetcher:
    """A GET-only, byte-capped fetcher. Redirects are followed only on the
    same host and never to a path matching `never_fetch_re`; a URL that
    itself matches it is refused without a request. `pause_s` spaces
    requests out."""
    hdrs = {"User-Agent": DEFAULT_UA, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9"}
    hdrs.update(dict(headers or {}))
    never_rx = re.compile(never_fetch_re) if never_fetch_re else None
    opener = urllib.request.build_opener(_GuardedRedirects(never_rx))

    def fetch(url: str) -> Fetched:
        path = urllib.parse.urlsplit(url).path or "/"
        if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
            return Fetched(url, 0, "", url, "refused: not an http(s) URL")
        if never_rx is not None and never_rx.search(path):
            return Fetched(url, 0, "", url, "refused: path matches never_fetch_re")
        if pause_s:
            time.sleep(pause_s)
        try:
            req = urllib.request.Request(url, headers=hdrs, method="GET")
            req._content_policy_origin = url
            with opener.open(req, timeout=timeout) as r:  # noqa: S310 - http(s) only, checked above
                body = r.read(max_bytes)
                charset = r.headers.get_content_charset() or "utf-8"
                return Fetched(url, r.status, body.decode(charset, "replace"), r.geturl())
        except urllib.error.HTTPError as e:
            return Fetched(url, e.code, "", url, f"HTTP {e.code}")
        except RedirectRefused as e:
            return Fetched(url, 0, "", url, str(e.reason)[:200])
        except Exception as e:  # noqa: BLE001
            return Fetched(url, 0, "", url, f"{type(e).__name__}: {str(e)[:120]}")

    return fetch


def _same_host(url: str, base: str) -> bool:
    a, b = urllib.parse.urlsplit(url), urllib.parse.urlsplit(base)
    norm = lambda h: (h or "").lower().split(":")[0].removeprefix("www.")  # noqa: E731
    return a.scheme in ("http", "https") and norm(a.netloc) == norm(b.netloc)


def _allowed(url: str, base: str, path_re: "re.Pattern | None", never_re: "re.Pattern | None") -> bool:
    if not _same_host(url, base):
        return False
    path = urllib.parse.urlsplit(url).path or "/"
    if never_re is not None and never_re.search(path):
        return False
    return path_re is None or bool(path_re.search(path))


def sitemap_urls(base: str, fetch: Fetcher, *, path_re: str = "", cap: int = 500,
                 max_children: int = 50, never_fetch_re: str = DEFAULT_NEVER_FETCH) -> list[str]:
    """Page URLs from base/sitemap.xml (and its child sitemaps) on the same
    host whose path matches `path_re` and never `never_fetch_re`."""
    if not base or urllib.parse.urlsplit(base).scheme not in ("http", "https"):
        raise ValueError(f"sitemap: base must be an http(s) URL, got {base!r}")
    rx = re.compile(path_re) if path_re else None
    never_rx = re.compile(never_fetch_re) if never_fetch_re else None
    root = urllib.parse.urljoin(base if base.endswith("/") else base + "/", "sitemap.xml")
    got = fetch(root)
    if got.error or not got.text:
        raise RuntimeError(f"sitemap {root}: {got.error or 'empty'}")
    locs = [x.strip() for x in re.findall(r"<loc>\s*([^<]+?)\s*</loc>", got.text)]
    pages: list[str] = []
    children = [u for u in locs if u.lower().endswith(".xml")][:max_children]
    pages += [u for u in locs if not u.lower().endswith(".xml")]
    for child in children:
        if not _allowed(child, base, None, never_rx):
            continue
        sub = fetch(child)
        if sub.text:
            pages += [x.strip() for x in re.findall(r"<loc>\s*([^<]+?)\s*</loc>", sub.text)
                      if not x.strip().lower().endswith(".xml")]
        if len(pages) >= cap * 4:
            break
    out: list[str] = []
    for u in pages:
        if u not in out and _allowed(u, base, rx, never_rx):
            out.append(u)
        if len(out) >= cap:
            break
    return out


_HREF = re.compile(r"""<a\b[^>]*?\bhref\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.I)


def crawl(start_urls: Sequence[str], fetch: Fetcher, *, path_re: str = "", follow_re: str = "",
          never_fetch_re: str = DEFAULT_NEVER_FETCH, depth: int = 2, cap: int = 200) -> list[Fetched]:
    """Breadth-first, same host as the first start URL. Pages whose path
    matches `path_re` are returned for scanning; links are followed when
    their path matches `follow_re` (default: path_re). Never requests a path
    matching `never_fetch_re`."""
    if not start_urls:
        return []
    base = start_urls[0]
    scan_rx = re.compile(path_re) if path_re else None
    follow_rx = re.compile(follow_re) if follow_re else scan_rx
    never_rx = re.compile(never_fetch_re) if never_fetch_re else None
    seen: set[str] = set()
    queue: list[tuple[str, int]] = [(u.split("#")[0], 0) for u in start_urls]
    out: list[Fetched] = []
    while queue and len(seen) < cap * 3:
        url, d = queue.pop(0)
        if url in seen or not _allowed(url, base, None, never_rx):
            continue
        seen.add(url)
        got = fetch(url)
        path = urllib.parse.urlsplit(url).path or "/"
        if scan_rx is None or scan_rx.search(path):
            out.append(got)
            if len(out) >= cap:
                break
        if d >= depth or not got.text:
            continue
        for m in _HREF.finditer(got.text):
            href = (m.group(1) or m.group(2) or m.group(3) or "").strip()
            if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
                continue
            nxt = urllib.parse.urljoin(got.final_url or url, href).split("#")[0]
            if nxt not in seen and _allowed(nxt, base, follow_rx, never_rx):
                queue.append((nxt, d + 1))
    return out


def scan_fetched(policy: ContentPolicy, pages: Iterable[Fetched]) -> ScanReport:
    rep = ScanReport(policy.rule_sets)
    for f in pages:
        if f.error or not f.text:
            rep.pages.append(PageResult(f.url, f.status, None, f.error or "empty response"))
            continue
        rep.pages.append(PageResult(f.url, f.status, audit_html(policy, f.text, url=f.url)))
    return rep


def crawl_check_group(base: str, policy: ContentPolicy, *, fetch: Fetcher | None = None,
                      path_re: str = r"^/guides/[A-Za-z0-9-]+/?$", index_path: str = "/guides",
                      cap: int = 200, never_fetch_re: str = DEFAULT_NEVER_FETCH,
                      severity: Mapping[str, str] | None = None) -> list[dict]:
    """The site_consistency_audit check group: every live page under
    `path_re` (from the sitemap; else crawled from `index_path`), as
    findings. Never requests a path matching `never_fetch_re`. Inactive
    policy -> no findings. `severity` retunes any kind, including
    KIND_UNAVAILABLE (raise it to "error" to fail a gate on unread pages)."""
    if not policy.active:
        return []
    sev = {**DEFAULT_SEVERITY, **dict(severity or {})}
    fetch = fetch or http_fetcher(never_fetch_re=never_fetch_re)
    try:
        urls = sitemap_urls(base, fetch, path_re=path_re, cap=cap, never_fetch_re=never_fetch_re)
        pages = [fetch(u) for u in urls]
    except RuntimeError:
        start = urllib.parse.urljoin(base if base.endswith("/") else base + "/", index_path.lstrip("/"))
        pages = crawl([start], fetch, path_re=path_re, follow_re=path_re + "|^" + re.escape(index_path),
                      never_fetch_re=never_fetch_re, depth=2, cap=cap)
    if not pages:
        return [{"severity": sev[KIND_UNAVAILABLE], "kind": KIND_UNAVAILABLE, "url": base,
                 "message": f"no page under {path_re} found to scan", "group": CHECK_GROUP}]
    return findings(scan_fetched(policy, pages), sev)


def site_check_group(base: str, site_hint: str, *, storage=None, config: Mapping[str, Any] | None = None,
                     **kw: Any) -> list[dict]:
    """`crawl_check_group` for a site by name, as site_consistency_audit
    would call it: resolves the site's policy (storage copy laid over the
    repo default). A config that does not compile, or a storage copy that
    cannot be read, is a KIND_UNAVAILABLE finding (never a silent "clean");
    a site with no rule sets yields no findings."""
    try:
        policy = resolve_policy(site_hint, storage=storage, config=config)
    except ContentPolicyConfigError as e:
        sev = {**DEFAULT_SEVERITY, **dict(kw.get("severity") or {})}
        return [{"severity": sev[KIND_UNAVAILABLE], "kind": KIND_UNAVAILABLE, "url": base,
                 "message": f"content-policy config unavailable: {str(e)[:200]}", "group": CHECK_GROUP}]
    return crawl_check_group(base, policy, **kw)
