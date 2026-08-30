"""Pure triage logic for user feedback. No DB, no network, no LLM.

Deciding whether a piece of feedback is *actionable* is a filter, not a
judgement call, and a filter should be cheap and testable. The LLM only earns
its cost on the ones that survive — investigating and writing the fix.
"""
from __future__ import annotations

import re

# Feedback that is real but not a defect. Routing these to an implementer burns
# a deploy cycle on "thanks, love the site".
NON_DEFECT_KINDS = {"praise", "question", "general_praise", "compliment"}

# Rows created by our own smoke tests and regression harnesses. 84 of the 86
# rows in aisleprompt's feedback table on 2026-08-30 were these; without the
# filter the agent's entire queue is its own test exhaust.
TEST_ROW_RE = re.compile(
    r"^\s*(?:"
    r"shape\s*test|regression[-_ ]?\d+|ping|asdf|qwerty|lorem"
    r"|test(?:ing)?\b[^.]{0,60}(?:submission|category|form|feedback|widget)\b[^.]{0,20}"
    r"|test\b.{0,20}"
    r")\s*\.?\s*$", re.I)

# Signatures that make a report investigable: something concrete failed.
ERROR_SIGNALS = [
    r"\bis not defined\b", r"\bis not a function\b", r"\bundefined\b", r"\bnull\b",
    r"\bNaN\b", r"\b(4\d\d|5\d\d)\s*(error|status)?\b", r"\berror\b", r"\bfail(s|ed|ing)?\b",
    r"\bcrash(es|ed)?\b", r"\bbroken?\b", r"\bdoesn'?t work\b", r"\bwon'?t (load|open|work)\b",
    r"\bblank (page|screen)\b", r"\bstuck\b", r"\btimed? ?out\b", r"\bcan'?t (see|find|click|open)\b",
    r"\bexception\b", r"\bstack ?trace\b",
    # Named JS runtime errors users paste verbatim from the console.
    r"\b(Type|Reference|Syntax|Range|URI|Eval)Error\b",
    r"\bdoes ?n[o']?t (work|load|open|save|submit|respond)\b",
    r"\bdoes nothing\b", r"\bnothing happens\b", r"\bno (response|result|results)\b",
    r"\bwrong (price|image|product|category|link)\b",
    r"\bredirect(s|ed|ing)? (to |fail)", r"\b404\b", r"\b500\b",
]
ERROR_RE = re.compile("|".join(ERROR_SIGNALS), re.I)

# A quoted runtime error is the strongest possible signal — it names the defect.
QUOTED_ERROR_RE = re.compile(r"['\"]([^'\"]{6,80}(?:is not defined|is not a function|undefined|failed)[^'\"]{0,40})['\"]", re.I)

MIN_MESSAGE_CHARS = 15


def normalise(row: dict, colmap: dict) -> dict:
    """Map a site's column names onto one shape the rest of the code uses."""
    out = {}
    for logical, actual in colmap.items():
        out[logical] = row.get(actual)
    return out


def classify(fb: dict) -> dict:
    """Decide what to do with one piece of feedback.

    Returns {action, reason, confidence, error_signature}. `action` is one of:
      investigate  - a defect with enough signal to chase
      needs_info   - a real report, too vague to act on
      not_a_defect - praise, a question, or a feature request
      ignore       - our own test exhaust
    """
    msg = (fb.get("message") or "").strip()
    kind = (fb.get("kind") or "").strip().lower()
    sev = (fb.get("severity") or "").strip().lower()

    if not msg or TEST_ROW_RE.match(msg):
        return {"action": "ignore", "reason": "test/placeholder row", "confidence": 1.0}

    if kind in NON_DEFECT_KINDS:
        return {"action": "not_a_defect", "reason": f"kind={kind}", "confidence": 0.9}

    if kind in {"feature", "feature_request", "idea"}:
        return {"action": "not_a_defect", "reason": "feature request, not a defect",
                "confidence": 0.85}

    quoted = QUOTED_ERROR_RE.search(msg)
    has_error = bool(ERROR_RE.search(msg))
    is_bug_kind = kind in {"bug", "defect", "error", "problem"}

    if len(msg) < MIN_MESSAGE_CHARS and not quoted:
        return {"action": "needs_info", "reason": "message too short to investigate",
                "confidence": 0.8}

    if not (has_error or is_bug_kind):
        return {"action": "needs_info",
                "reason": "no failure described; cannot tell what is broken",
                "confidence": 0.7}

    # Confidence rises with how specific the report is. A quoted runtime error
    # plus a URL is nearly always enough to find the defect from the codebase.
    conf = 0.5
    if is_bug_kind:
        conf += 0.15
    if has_error:
        conf += 0.15
    if quoted:
        conf += 0.2
    if fb.get("url"):
        conf += 0.1
    if sev in {"high", "critical", "urgent"}:
        conf += 0.05

    return {
        "action": "investigate",
        "reason": "defect with a concrete failure signal",
        "confidence": round(min(conf, 1.0), 2),
        "error_signature": quoted.group(1) if quoted else None,
    }


def build_recommendation(fb: dict, cls: dict, rec_id: str, site: str) -> dict:
    """A rec in the shape the backlog dispatcher hands to the implementer."""
    msg = (fb.get("message") or "").strip()
    url = fb.get("url") or ""
    sig = cls.get("error_signature")
    title = f"User-reported defect: {msg[:90]}"
    return {
        "id": rec_id,
        "type": "user-feedback-defect",
        "priority": "high" if (fb.get("severity") or "").lower() in {"high", "critical"} else "medium",
        "title": title,
        "rationale": (
            f"A user reported this through the {site} feedback widget"
            + (f" on {url}" if url else "")
            + ". User-reported defects are the only signal that comes from someone "
              "actually trying to use the site, so they outrank inferred issues."
            + (f" The report quotes a runtime error: \"{sig}\"." if sig else "")
        ),
        "recommendation": (
            f"Investigate and fix: {msg}\n\n"
            + (f"Page: {url}\n" if url else "")
            + (f"Quoted error: {sig}\n" if sig else "")
            + "\nFind the root cause in the codebase, make the smallest correct fix, "
              "and add a regression test. If the same mistake exists at sibling call "
              "sites, fix those too. Do not change unrelated behaviour."
        ),
        "evidence": {
            "feedback_id": str(fb.get("id")),
            "url": url,
            "severity": fb.get("severity"),
            "kind": fb.get("kind"),
            "confidence": cls.get("confidence"),
        },
        "effort": "unknown",
        "impact": "user-reported",
    }
