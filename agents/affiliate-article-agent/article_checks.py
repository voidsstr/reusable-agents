"""In-process checks on one draft, before any INSERT. Every check is a public
framework function or a site-config SQL lookup; none rewrites prose.

  shape          slug fixed by the calendar, field lengths, FAQ shape
  content_policy framework.core.content_policy (the site's rule sets)
  claims         framework.core.guide_claims_guard (numbers vs the facts pack,
                 6(y) wording, point prices, banned topics, Amazon URLs,
                 off-site links, raw HTML, inline images, hand-written tables)
  honesty        framework.core.article_claims_guard (invented testing, star
                 ratings; the publish gate runs it too)
  links          framework.core.article_link_classes counts, but only links to
                 LIVE, INDEXABLE pages count (the site's indexable view), and a
                 link to any page that is not live fails the draft
  placeholders   grammar, position, every key renders (site SQL), every
                 placeholder the calendar entry requires is present
  buystrip       ASINs not in the site's eligibility view are dropped (reported)
"""
from __future__ import annotations

import dataclasses
from typing import Any, Mapping, Optional, Sequence

from framework.core import article_claims_guard as honesty_guard
from framework.core import content_policy
from framework.core import guide_claims_guard as cg
from framework.core.article_link_classes import class_slugs

MAX_TITLE, MAX_SUBTITLE, MAX_EXCERPT, MAX_BODY = 160, 300, 320, 60000


@dataclasses.dataclass
class Check:
    name: str
    passes: bool
    reason: str = ""
    addendum: str = ""
    details: dict = dataclasses.field(default_factory=dict)

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class CheckReport:
    checks: list[Check]
    draft: dict

    @property
    def passes(self) -> bool:
        return all(c.passes for c in self.checks)

    @property
    def failed(self) -> list[str]:
        return [c.name for c in self.checks if not c.passes]

    def addendum(self) -> str:
        return "\n\n".join((c.addendum or f"{c.name.upper()}: {c.reason}") for c in self.checks if not c.passes)

    def summary(self) -> str:
        return "all checks pass" if self.passes else "failed: " + ", ".join(
            f"{c.name} ({c.reason[:120]})" for c in self.checks if not c.passes)

    def to_dict(self) -> dict:
        return {"passes": self.passes, "failed": self.failed, "checks": [c.to_dict() for c in self.checks]}


def internal_paths(body_md: str, own_hosts: Sequence[str]) -> set[str]:
    from urllib.parse import urlparse
    out = set()
    for m in cg.MD_LINK_RE.finditer(body_md or ""):
        if m.group(1):
            continue
        url = m.group(3)
        if url.startswith("/") and not url.startswith("//"):
            out.add(url.split("#", 1)[0].split("?", 1)[0])
        elif url.startswith(("http://", "https://")):
            u = urlparse(url)
            if u.netloc.lower() in own_hosts:
                out.add((u.path or "/").split("#", 1)[0])
    return out


def _shape(draft: Mapping[str, Any], entry) -> Check:
    p = []
    if draft.get("slug") != entry.slug:
        p.append(f"slug must be exactly {entry.slug!r} (got {str(draft.get('slug'))[:80]!r})")
    for k, mx in (("title", MAX_TITLE), ("subtitle", MAX_SUBTITLE), ("excerpt", MAX_EXCERPT), ("body_md", MAX_BODY)):
        if len(str(draft.get(k) or "")) > mx:
            p.append(f"{k} longer than {mx} characters")
    faqs = draft.get("faqs") or []
    if not (1 <= len(faqs) <= 8) or any(not f.get("question") or not f.get("answer") for f in faqs):
        p.append("faqs: 1-8 entries, each with a question and an answer")
    if p:
        return Check("shape", False, "; ".join(p), "SHAPE — " + "; ".join(p))
    return Check("shape", True)


def _content_policy(draft: Mapping[str, Any], policy) -> Check:
    audit = content_policy.check_article(policy, body_md=draft.get("body_md") or "", title=draft.get("title") or "",
                                         subtitle=draft.get("subtitle") or "", excerpt=draft.get("excerpt") or "",
                                         faqs=draft.get("faqs"))
    return Check("content_policy", audit.passes, audit.failure_reason(), audit.addendum() if not audit.passes else "",
                 {"rule_sets": list(audit.rule_sets), "rejects": [v.to_dict() for v in audit.rejects][:20],
                  "warnings": [v.to_dict() for v in audit.warnings][:20]})


def _claims(draft, facts, scenario, claims_policy) -> Check:
    audit = cg.check(title=draft.get("title") or "", subtitle=draft.get("subtitle") or "",
                     excerpt=draft.get("excerpt") or "", body_md=draft.get("body_md") or "", faqs=draft.get("faqs"),
                     facts=facts.numeric, policy=claims_policy, scenario=scenario, rows=facts.rows)
    return Check("claims", audit.passes, audit.summary() if not audit.passes else "", audit.addendum(),
                 audit.to_dict())


def _honesty(draft, settings, storage) -> Check:
    bucket = str(settings.articles.get("category") or "")
    pol = honesty_guard.resolve_policy(settings.site, bucket, storage=storage)
    audit = honesty_guard.check(draft.get("body_md") or "", subtitle=draft.get("subtitle") or "",
                                excerpt=draft.get("excerpt") or "", bucket=bucket, policy=pol)
    reason = audit.failure_reason()
    return Check("honesty", audit.passes, reason, reason)


def _links(draft, settings, store) -> Check:
    body = draft.get("body_md") or ""
    paths = internal_paths(body, settings.own_hosts)
    live = store.indexable_paths(paths) if paths else set()
    dead = sorted(paths - live)
    counts: dict[str, int] = {}
    short = []
    found = class_slugs(body, settings.link_classes, settings.own_hosts)
    for c in settings.link_classes:
        live_slugs = {s for s in found.get(c.name, set())
                      if any(f"{root}{s}" in live or f"{root}{s}/" in live for root in c.roots)}
        counts[c.name] = len(live_slugs)
        if len(live_slugs) < c.min:
            short.append(f"{len(live_slugs)}/{c.min} live links under {' or '.join(c.roots)}")
    expected = [str(x) for x in (draft.get("expected_links") or []) if isinstance(x, str)]
    missing = [x for x in expected if x.startswith("/") and x.split("#")[0] not in paths]
    problems = []
    if dead:
        problems.append(f"links to pages that are not live and indexable: {', '.join(dead[:8])}")
    if short:
        problems.append("too few links: " + "; ".join(short))
    if missing:
        problems.append(f"expected_links not linked in body_md: {', '.join(missing[:8])}")
    if not problems:
        return Check("links", True, details={"counts": counts, "paths": sorted(paths)})
    add = ("LINKS — " + "; ".join(problems) + ". Link only paths listed in FACTS (they are live pages); remove the "
           "others; weave each link into a sentence at its first mention.")
    return Check("links", False, "; ".join(problems), add, {"counts": counts, "dead": dead, "missing": missing})


def _placeholders(draft, settings, store, entry) -> Check:
    found, problems = cg.find_placeholders(draft.get("body_md") or "", settings.placeholder_kinds.keys())
    for ph in found:
        if ph.kind and not store.placeholder_resolves(ph.kind, ph.key):
            problems.append(f"{ph.raw} does not render (unknown or inactive key)")
    have = {ph.raw.replace(" ", "") for ph in found}
    for req in entry.placeholders:
        if req.replace(" ", "") not in have:
            problems.append(f"required placeholder {req} is missing")
    if not problems:
        return Check("placeholders", True, details={"found": [p.raw for p in found]})
    return Check("placeholders", False, "; ".join(problems[:8]),
                 "PLACEHOLDERS — " + "; ".join(problems[:8]) + ". Write each placeholder alone on its own line, "
                 "with a key from FACTS or TOPIC.", {"problems": problems})


def clean_buystrip(draft, settings, store) -> dict:
    asins = [str(a).strip().upper() for a in (draft.get("buystrip_asins") or []) if isinstance(a, str)]
    asins = list(dict.fromkeys(a for a in asins if a))[: int(settings.buystrip.get("max") or 6)]
    ok = store.eligible_asins(asins) if asins else set()
    kept = [a for a in asins if a in ok]
    return {"kept": kept, "dropped": [a for a in asins if a not in ok]}


def run_checks(draft: dict, *, entry, settings, store, facts, scenario, content_pol, claims_policy,
               storage=None) -> CheckReport:
    checks = [_shape(draft, entry)]
    for fn in (lambda: _content_policy(draft, content_pol),
               lambda: _claims(draft, facts, scenario, claims_policy),
               lambda: _honesty(draft, settings, storage) if settings.honesty_guard else Check("honesty", True),
               lambda: _links(draft, settings, store),
               lambda: _placeholders(draft, settings, store, entry)):
        try:
            checks.append(fn())
        except Exception as e:  # noqa: BLE001 - a check that cannot run fails the draft (fail closed)
            checks.append(Check("error", False, f"a check could not run: {type(e).__name__}: {str(e)[:200]}",
                                "A check could not run; an operator must look at it."))
    return CheckReport(checks, draft)
