"""Real-people allowlist for anything an agent writes about authorship.

WHY THIS EXISTS
---------------
The SEO and progressive-improvement audits carry E-E-A-T checks
("author named but no credential", "no machine-readable author
credentials", "YMYL article lacks a credentialed reviewer"). None of them
said where a person may come from, so when a site has ONE real person the
LLM filled the gap itself. Shipped results, 2026-06..09, across two
reference deployments:

  * an invented registered-dietitian reviewer on health articles
  * "2-3 contractor author personas" proposed, then an author archive
    built for one of them
  * an invented retro-hardware "specialist" with made-up community
    credentials, credited as the Review author on ~68k product pages

Each was a rec that looked like ordinary SEO hygiene. The operator reverted
several by hand; the checks kept re-proposing them because nothing in the
prompt or the ship path knew which people are real.

This module is that knowledge, as data:

  load_people(site_cfg, site_id=...)   the site's real people + org name
  prompt_block(people)                 honesty rules for an LLM prompt
  proposes_invented_person(text, ...)  rec-text screen (drop the rec)
  scan_added_lines(lines, people)      diff screen (implementer post-LLM)

CONFIG
------
Lowest to highest precedence:

  1. site.yaml `editorial:` block (the site repo owns its people)

         editorial:
           organization: "Example Editorial"
           people:
             - "Jane Doe"                         # name only
             - name: "John Roe"                   # or a full record
               slug: john-roe                     # /authors/<slug> allowed
               credentials: ["RD"]                # ONLY verified credentials

  2. storage `config/editorial-people-config.json` → "by_site"[<site_id>]
     (operator override without a commit; same shape as the block above)

A person not in the resulting list does not exist as far as agents are
concerned. A credential not listed on a person is not a credential they
hold. Sites with no `editorial` block get the empty list, which the prompt
block renders as "credit only the site's organization".
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Optional

CONFIG_KEY = "config/editorial-people-config.json"

# The rule every authorship-touching prompt carries. Kept as one constant so
# the SEO audit, the PI audit and any future proposer say the same thing.
HONESTY_RULE = (
    "AUTHORSHIP HONESTY (hard rule): a fix may credit only a person on the "
    "REAL PEOPLE list below, or the site's organization. Never propose "
    "creating a person, persona, pen name, author bio, author archive for an "
    "unlisted person, credential, job title, reviewer, tester, test kitchen, "
    "lab, hands-on testing claim, testimonial, review or rating. Never add a "
    "credential a listed person does not already hold. If a page needs a "
    "credentialed human the site does not have (e.g. a dietitian on health "
    "content), propose noindex or removing the claim, or report it as an "
    "operator escalation, never an implementable rec that names someone."
)

# ── config ─────────────────────────────────────────────────────────────────


def _norm_person(p: Any) -> Optional[dict]:
    if isinstance(p, str):
        name = p.strip()
        return {"name": name, "slug": _slugify(name), "credentials": []} if name else None
    if isinstance(p, Mapping):
        name = str(p.get("name") or "").strip()
        if not name:
            return None
        creds = p.get("credentials") or []
        if isinstance(creds, str):
            creds = [creds]
        return {
            "name": name,
            "slug": str(p.get("slug") or _slugify(name)).strip(),
            "credentials": [str(c).strip() for c in creds if str(c).strip()],
        }
    return None


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _load_storage_override(site_id: str, storage=None) -> dict:
    if not site_id:
        return {}
    try:
        if storage is None:
            from framework.core.storage import get_storage
            storage = get_storage()
        doc = storage.read_json(CONFIG_KEY) or {}
    except Exception:
        return {}
    if not isinstance(doc, dict):
        return {}
    ov = (doc.get("by_site") or {}).get(site_id)
    return ov if isinstance(ov, dict) else {}


def load_people(site_cfg: Optional[Mapping] = None, *, site_id: str = "",
                storage=None, use_storage: bool = True) -> dict:
    """Return {"organization": str, "people": [ {name, slug, credentials} ]}.

    `site_cfg` is the parsed site.yaml (any mapping with an `editorial`
    key). `use_storage=False` skips the storage override (tests, offline)."""
    block = dict(((site_cfg or {}).get("editorial") or {})) if site_cfg else {}
    if use_storage:
        ov = _load_storage_override(site_id, storage=storage)
        block.update({k: v for k, v in ov.items() if v is not None})
    people = [p for p in (_norm_person(x) for x in (block.get("people") or [])) if p]
    # de-dupe by lowercased name, keep first
    seen: set[str] = set()
    uniq = []
    for p in people:
        k = p["name"].lower()
        if k not in seen:
            seen.add(k)
            uniq.append(p)
    return {"organization": str(block.get("organization") or "").strip(),
            "people": uniq}


def names(roster: Mapping) -> list[str]:
    return [p["name"] for p in (roster or {}).get("people") or []]


# ── prompt ─────────────────────────────────────────────────────────────────


def prompt_block(roster: Mapping, *, site_label: str = "") -> str:
    """Honesty rule + the site's real people, ready to paste into a prompt."""
    people = (roster or {}).get("people") or []
    org = (roster or {}).get("organization") or site_label or "the site's organization"
    lines = [HONESTY_RULE, "", "REAL PEOPLE (the complete list):"]
    if people:
        for p in people:
            cred = ", ".join(p.get("credentials") or []) or "no verified credentials on record"
            lines.append(f"  - {p['name']} ({cred})")
    else:
        lines.append("  - (none on record — credit only the organization)")
    lines.append(f"ORGANIZATION: {org}")
    return "\n".join(lines)


# ── rec-text screen ────────────────────────────────────────────────────────

# Phrases that are only ever a request to fabricate someone.
_ALWAYS_INVENTED = re.compile(
    r"\b(persona|personas|pen[- ]name|pseudonym|fictional|fictitious|"
    r"contractor author|contributor persona|made[- ]up (author|expert|reviewer))\b",
    re.IGNORECASE,
)
# "add/name/create/hire/introduce/assign ... reviewer|author|expert|..." —
# a request for a NEW person. Allowed only when a listed person is named.
_NEW_PERSON = re.compile(
    r"\b(add|adding|name|naming|create|creating|introduce|introducing|hire|"
    r"hiring|assign|assigning|recruit|onboard)\b[^.;\n]{0,60}?"
    r"\b(a|an|another|second|more|additional|two|three|2|3|named|credentialed|"
    r"certified|expert|qualified)\b[^.;\n]{0,40}?"
    r"\b(authors?|reviewers?|experts?|dietitians?|nutritionists?|doctors?|"
    r"physicians?|chefs?|editors?|contributors?|testers?|specialists?|"
    r"team members?|staff)\b",
    re.IGNORECASE,
)
# Credential acronyms are matched case-SENSITIVELY ("MD", not the "md" in
# README.md); the spelled-out claims case-insensitively.
_CRED_CLAIM = re.compile(
    r"\b((?-i:RDN?|MD|PhD|MS, RD)|registered dietitian|board[- ]certified|"
    r"certified nutritionist|test kitchen|lab[- ]tested|"
    r"hands[- ]on test(?:ed|ing)?|we tested|our testers?)\b",
    re.IGNORECASE,
)
# A credential word in a rec is fine when the rec removes it or gates on it.
_REMOVAL_CONTEXT = re.compile(
    r"\b(remove|removing|strip|stripping|drop|dropping|delete|deleting|"
    r"noindex|until (it is |they are )?reviewed|unreviewed|without a|"
    r"escalat\w*|operator)\b",
    re.IGNORECASE,
)


def proposes_invented_person(text: str, roster: Mapping) -> str:
    """Return a reason string when `text` (a rec's title/fix/outline) asks
    for a person, persona or credential the roster does not back; "" if not.

    Conservative by design: dropping a legitimate rec costs one run's slot,
    shipping an invented expert costs the site's trust."""
    if not text:
        return ""
    if _ALWAYS_INVENTED.search(text):
        return "asks for a persona / pen name / fictional contributor"
    listed = [n.lower() for n in names(roster)]
    mentions_listed = any(n and n in text.lower() for n in listed)
    if _NEW_PERSON.search(text) and not mentions_listed:
        return "asks to add a new named person the site does not have"
    m = _CRED_CLAIM.search(text)
    if m:
        creds = {c.lower() for p in (roster or {}).get("people") or []
                 for c in (p.get("credentials") or [])}
        claim = m.group(0).lower()
        # A credential word is fine when the rec is REMOVING it or gating on
        # it (noindex until reviewed), and when a listed person holds it.
        if claim not in creds and not _REMOVAL_CONTEXT.search(text):
            return f"asserts an unbacked credential or testing claim ({m.group(0)})"
    return ""


# ── diff screen (implementer post-LLM) ─────────────────────────────────────

_PERSON_OBJ = re.compile(r"""['"]?@type['"]?\s*:\s*['"]Person['"]""")
_NAME_KV = re.compile(r"""['"]?name['"]?\s*:\s*['"]([^'"\n]{2,80})['"]""")
_BYLINE = re.compile(
    r"\b(?:Reviewed|Written|Tested|Fact[- ]checked|Medically reviewed|Edited|"
    r"Curated)\s+by\s+([A-Z][a-zA-Z.'-]+(?:\s+[A-Z][a-zA-Z.'-]+){0,3})")
_AUTHOR_ROUTE = re.compile(r"/authors?/([a-z0-9][a-z0-9-]{1,60})")
_CRED_FIELD = re.compile(
    r"""(honorificSuffix|reviewedBy|jobTitle)['"]?\s*:\s*['"]?([^'",}\n]{0,80})""")


def scan_added_lines(added: Iterable[str], roster: Mapping, *,
                     window: int = 12) -> list[dict]:
    """Findings for diff lines ADDED by an edit that credit an unlisted person.

    `added` is the "+" side of a unified diff (leading "+" optional). Each
    finding is {"kind", "value", "line"}. Empty list = clean.

    Detected:
      person-node     a JSON-LD / object-literal Person whose name is unlisted
      byline          "Reviewed by X" / "Written by X" with X unlisted
      author-route    /authors/<slug> for a slug no listed person has
      credential      honorificSuffix / reviewedBy / jobTitle carrying a
                      credential no listed person holds
    """
    lines = [ln[1:] if ln.startswith("+") else ln for ln in (added or [])]
    listed = {n.lower() for n in names(roster)}
    org = ((roster or {}).get("organization") or "").lower()
    slugs = {p.get("slug") for p in (roster or {}).get("people") or [] if p.get("slug")}
    creds = {c.lower() for p in (roster or {}).get("people") or []
             for c in (p.get("credentials") or [])}
    out: list[dict] = []

    brand = org.split()[0] if org else ""

    def _unlisted(name: str) -> bool:
        n = name.strip().lower()
        if not n or n == org:
            return False
        # The site's own organization under another label ("<Brand> Team").
        if brand and n.startswith(brand):
            return False
        # Template placeholders are data-driven, not invented people.
        if any(ch in n for ch in "${}<>") or n.startswith(("{", "$")):
            return False
        return not any(n == l or n in l or l in n for l in listed)

    def _person_name(i: int, at: int) -> Optional[str]:
        """The `name` belonging to the Person node matched at column `at` of
        line i: nearest name on the same line, else the first one below."""
        same = list(_NAME_KV.finditer(lines[i]))
        if same:
            return min(same, key=lambda m: abs(m.start() - at)).group(1)
        for ln2 in lines[i + 1: i + 1 + window]:
            m2 = _NAME_KV.search(ln2)
            if m2:
                return m2.group(1)
        return None

    for i, ln in enumerate(lines):
        for pm in _PERSON_OBJ.finditer(ln):
            nm = _person_name(i, pm.start())
            if nm and _unlisted(nm):
                out.append({"kind": "person-node", "value": nm, "line": ln.strip()[:200]})
        for m in _BYLINE.finditer(ln):
            if _unlisted(m.group(1)):
                out.append({"kind": "byline", "value": m.group(1), "line": ln.strip()[:200]})
        for m in _AUTHOR_ROUTE.finditer(ln):
            if m.group(1) not in slugs and "$" not in ln[max(0, m.start() - 2): m.end() + 2]:
                out.append({"kind": "author-route", "value": m.group(1), "line": ln.strip()[:200]})
        for m in _CRED_FIELD.finditer(ln):
            val = m.group(2)
            hit = _CRED_CLAIM.search(val)
            if hit and hit.group(0).lower() not in creds:
                out.append({"kind": "credential", "value": f"{m.group(1)}={hit.group(0)}",
                            "line": ln.strip()[:200]})
    # de-dupe
    seen = set()
    uniq = []
    for f in out:
        k = (f["kind"], f["value"])
        if k not in seen:
            seen.add(k)
            uniq.append(f)
    return uniq


def added_lines_from_diff(diff_text: str) -> list[str]:
    """The "+" lines of a unified diff, minus the "+++" file headers."""
    return [ln[1:] for ln in (diff_text or "").splitlines()
            if ln.startswith("+") and not ln.startswith("+++")]
