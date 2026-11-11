"""Page holds — pages no agent may edit automatically until a person releases them.

Why (aisleprompt, 2026-10-24 growth memo, Lever 1): the Instant Pot guide is
the site's best AI-referral page, cited for one paragraph ("Duo or Rio? Same
cooker, buy the cheaper listing"). On 2026-10-23 five commits from three
agents (shelf-audit, progressive-improvement, seo-opportunity) re-decided its
picks and CTAs against each other while the page was waiting on a person.
`implementer_scope` gates by FILE path, and these pages live in a DB row, so
nothing could say "leave this page alone".

A hold is declared in the site yaml, top level or under `implementer:`:

    page_holds:
      - path: /blog/best-instant-pots-electric-pressure-cookers-2026
        reason: "picks need a person; keep the Duo-or-Rio answer verbatim"
        held_by: operator
        held_at: 2026-10-24
        until: 2026-12-02             # optional; the hold lapses after this date
        allow_dispatch_kinds: []      # optional; e.g. ["crash-fix"]

Consumers:
  * implementer run.sh (claude path) and build-aider-invocation.py (framework
    chain) defer any rec that references a held page, with reason
    "page-hold", before the LLM sees it.
  * shelf-audit answer_picks.to_recs routes fixes on a held page to the
    person-review rec instead of the dispatched one.
  * growth strategists (rec_memory.select_levers) drop a lever that touches a
    held page before it is dispatched, and list the held pages in their brief
    (holds_prompt_line). They read every agent's site yaml in the site repo
    (holds_from_repo), since a hold is on the page, not on one agent.

A rec "references" a page when the path or its last segment (the slug)
appears in the rec's URL fields or text, or when one of its URL fields holds a
`prefix*` pattern that covers the held path ("/blog/best-*" covers every
guide). That over-matches on purpose: a rec that only links TO a held page is
deferred too, and a person can release it by setting `hold_override: true` on
the rec. Missing a real edit is the failure this exists to stop; an extra
deferral costs one look. A bare `*` (prefix "/") in a URL field covers every
page, so such a rec is deferred whenever any hold is active: deliberately,
since a site-wide change can edit the held page too.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import urlsplit


@dataclass(frozen=True)
class PageHold:
    path: str
    reason: str = ""
    held_by: str = ""
    held_at: str = ""
    until: Optional[date] = None
    allow_dispatch_kinds: tuple[str, ...] = ()

    @property
    def slug(self) -> str:
        return self.path.rstrip("/").rsplit("/", 1)[-1]

    def active(self, today: Optional[date] = None) -> bool:
        today = today or datetime.now(timezone.utc).date()
        return self.until is None or today <= self.until

    def to_dict(self) -> dict:
        """The site-yaml form (holds_from_site_config reads it back)."""
        return {"path": self.path, "reason": self.reason, "held_by": self.held_by,
                "held_at": self.held_at,
                "until": self.until.isoformat() if self.until else None,
                "allow_dispatch_kinds": list(self.allow_dispatch_kinds)}


def _parse_date(v: Any) -> Optional[date]:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str) and v.strip():
        try:
            return date.fromisoformat(v.strip()[:10])
        except ValueError:
            return None
    return None


def _norm_path(p: str) -> str:
    p = urlsplit(str(p or "").strip()).path
    if p and not p.startswith("/"):
        p = "/" + p
    return p.rstrip("/") or "/"


def holds_from_site_config(cfg: Optional[dict]) -> list[PageHold]:
    """Every hold declared in a site yaml (top level and implementer block)."""
    cfg = cfg or {}
    raw = list(cfg.get("page_holds") or [])
    raw += list(((cfg.get("implementer") or {}).get("page_holds")) or [])
    out: list[PageHold] = []
    seen: set[str] = set()
    for e in raw:
        if isinstance(e, str):
            e = {"path": e}
        if not isinstance(e, dict) or not e.get("path"):
            continue
        path = _norm_path(e["path"])
        if path in seen or path == "/":
            continue
        seen.add(path)
        out.append(PageHold(
            path=path,
            reason=str(e.get("reason") or ""),
            held_by=str(e.get("held_by") or ""),
            held_at=str(e.get("held_at") or ""),
            until=_parse_date(e.get("until")),
            allow_dispatch_kinds=tuple(str(k) for k in (e.get("allow_dispatch_kinds") or [])),
        ))
    return out


def _later(a: PageHold, b: PageHold) -> PageHold:
    """Of two holds on one path, the one that lasts longer (no `until` = open-ended)."""
    if a.until is None:
        return a
    if b.until is None:
        return b
    return b if b.until > a.until else a


def holds_from_files(paths: Iterable[Any]) -> list[PageHold]:
    """Every hold declared in these site yaml files, one per path (when two
    files hold the same page, the longer-lasting hold wins). Unreadable or
    unparsable files are skipped; files that never mention page_holds are not
    parsed."""
    merged: dict[str, PageHold] = {}
    for p in paths or []:
        try:
            text = Path(p).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "page_holds" not in text:
            continue
        try:
            import yaml
            cfg = yaml.safe_load(text)
        except Exception:
            continue
        for h in holds_from_site_config(cfg if isinstance(cfg, dict) else {}):
            merged[h.path] = _later(merged[h.path], h) if h.path in merged else h
    return list(merged.values())


def holds_from_repo(repo: Any, patterns: Iterable[str] = ("agents/*/site.yaml",)) -> list[PageHold]:
    """Every hold declared in a site repo's agent yaml files (`patterns` are
    globs relative to the repo). A hold is on the page, so a producer that
    has no site yaml of its own (a growth strategist) honours the holds every
    other agent of the site declares."""
    root = Path(str(repo or ""))
    if not str(repo or "") or not root.is_dir():
        return []
    if isinstance(patterns, str):
        patterns = [patterns]
    files: list[Path] = []
    for pat in patterns or ():
        files.extend(sorted(root.glob(str(pat))))
    return holds_from_files(dict.fromkeys(files))


def active_holds(holds: Iterable[PageHold], *, dispatch_kind: str = "",
                 today: Optional[date] = None) -> list[PageHold]:
    return [h for h in holds
            if h.active(today) and not (dispatch_kind and dispatch_kind in h.allow_dispatch_kinds)]


def held_paths(holds: Iterable[PageHold], today: Optional[date] = None) -> set[str]:
    return {h.path for h in holds if h.active(today)}


# Rec fields whose values are page paths/URLs; a value with a `*` in one of
# these is a prefix pattern ("/blog/best-*"). Only these are read for
# prefixes: a `/*` in free text (a CSS comment in a code snippet) is not one.
URL_FIELDS = ("target_urls", "target_url", "urls", "url", "pages", "page", "target_pages")


def rec_prefixes(rec: dict) -> list[str]:
    """The `prefix*` patterns in a rec's URL fields, as lowercase path
    prefixes: "https://site.test/blog/best-*" -> "/blog/best-", "*" -> "/"."""
    out: list[str] = []
    if not isinstance(rec, dict):
        return out
    for key in URL_FIELDS:
        v = rec.get(key)
        values = [v] if isinstance(v, str) else (list(v) if isinstance(v, (list, tuple, set)) else [])
        for x in values:
            s = str(x if x is not None else "").strip()
            if "*" not in s:
                continue
            head = s.split("*", 1)[0]
            p = urlsplit(head).path if "://" in head else head
            if not p.startswith("/"):
                p = "/" + p
            out.append(p.lower())
    return out


def rec_holds(rec: dict, holds: Iterable[PageHold], *, dispatch_kind: str = "",
              today: Optional[date] = None) -> list[PageHold]:
    """The active holds a rec touches. Empty when the rec carries hold_override."""
    if not isinstance(rec, dict) or rec.get("hold_override") is True:
        return []
    live = active_holds(holds, dispatch_kind=dispatch_kind, today=today)
    if not live:
        return []
    blob = json.dumps(rec, default=str, ensure_ascii=False)
    prefixes = rec_prefixes(rec)
    return [h for h in live
            if h.path in blob or (len(h.slug) >= 12 and h.slug in blob)
            or any(h.path.lower().startswith(p) for p in prefixes)]


def holds_prompt_line(holds: Iterable[PageHold], *, today: Optional[date] = None) -> str:
    """One brief line naming the active held pages, for a producer's LLM
    prompt ("" when none is held)."""
    live = [h for h in holds or [] if h.active(today)]
    if not live:
        return ""
    bits = []
    for h in live:
        extra = "; ".join(x for x in (h.reason, f"until {h.until.isoformat()}" if h.until else "") if x)
        bits.append(h.path + (f" ({extra})" if extra else ""))
    return ("HELD PAGES (a person must release them): " + "; ".join(bits) + ". Do not target, "
            "edit or re-pick a held page, and do not use a target_urls prefix* that covers one "
            "(list the pages instead): a lever that touches a held page is dropped in code.")


def page_is_held(path: str, holds: Iterable[PageHold], today: Optional[date] = None) -> Optional[PageHold]:
    p = _norm_path(path)
    for h in holds:
        if h.active(today) and h.path == p:
            return h
    return None


def deferral_reason(hit: list[PageHold]) -> str:
    parts = []
    for h in hit:
        bit = h.path
        if h.reason:
            bit += " (%s)" % h.reason
        parts.append(bit)
    return "page-hold: rec touches a held page, a person must release it: " + "; ".join(parts)


__all__ = ["PageHold", "holds_from_site_config", "holds_from_files", "holds_from_repo",
           "active_holds", "held_paths", "rec_prefixes", "rec_holds", "holds_prompt_line",
           "page_is_held", "deferral_reason"]
