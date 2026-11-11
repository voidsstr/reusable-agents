"""Score the products people click before anything else. Pure, testable.

Why (2026-11-11): every Top-Pick surface (guides, category picks,
alternatives rails) requires ``COALESCE(category_confidence, 0) >= 0.5``, and
on the reference catalogue 397 of the ~950 products people clicked through to
Amazon in 30 days failed it: 333 had never been scored at all, 160 rows sat
below 0.5. Those products could not appear on the surfaces that sell, however
often they were bought. This agent used to only de-categorise.

The demand phase (``demand_scoring`` in the site config, off by default)
takes the CLICKED, NEVER-SCORED products first and runs them through the
site's own categoriser, the same scoring every other writer uses. A score is
evidence only when all of these hold:

  * the row was never scored (``category_confidence IS NULL``). An explicit
    value below 0.5 is somebody's decision -- pricing-integrity and honesty
    fixes demote with 0.3 (the reversible suppression the hygiene SQL, the
    scalper and price-anomaly scripts and the honesty migrations use) -- and
    is left alone exactly like ``category_validated``;
  * the row is priced the way a Top Pick must be (price >= 1.0, the site's
    currency, no negative discount; ``require_valid_price``). A score would
    otherwise lift a product the pricing rules keep off those surfaces; it is
    re-evaluated on a later run once it is priced;
  * the title is in the catalogue's domain (the agent's own off-domain test:
    foreign vocabulary and no domain vocabulary vetoes any write);
  * confirm: the site's rule for the CURRENT category scores it
    >= min_score (hinted call) AND the categoriser's own best placement with
    no hint is that same category. A classifier asked to score only the
    hinted rule confirms any title containing the category keyword (a
    ThinkPad "confirmed" as a CPU, a Retroid carrying case as emulation
    hardware), so the unhinted verdict must agree; the lower score is kept;
  * assign: an uncategorised product the rules place (no hint) with
    >= min_score (``assign_uncategorised``);
  * the title does not lead with an accessory / compatibility term (case,
    bracket, mount, strap, cable, cooler, antenna, battery, "compatible
    with", ...) unless the target category is one where such products
    belong (``accessory_guard.categories``). A rule that fires on
    "GPU Support Bracket" or "Carrying Case for Meta Quest 3" would
    otherwise make an accessory a Top Pick for the device category.

It never lowers the 0.5 display floor (``min_score`` is clamped to at least
0.5; the default 0.6 also rejects the neutral 0.5 a rule without patterns
returns) and never moves a product to a different category (a wrong home is
worse than no home).

``mode``: ``report`` (the default for an enabled phase) computes every
verdict and lists the would-be writes in the run artifact without any
UPDATE; ``write`` applies them. Turn ``write`` on only after the report's
writes have been reviewed.

CLASSIFIER CONTRACT (site config ``classifier.cmd`` + ``cwd``)
--------------------------------------------------------------
stdin:  JSON array of ``{"asin", "title", "brand", "hint"}`` where ``hint`` is
        a category slug or null
stdout: JSON array of ``{"asin", "slug", "confidence", "junk"}``; ``slug``
        null = the rules cannot place it. With a hint, the classifier scores
        the hinted category's rule; without one, it returns its best rule
        (reference implementation: the scripts/classify-product-titles.ts of
        the first deployment). The phase calls it twice: once with the
        current category as the hint, once without a hint for categorised
        rows (``require_agreement``).
"""
from __future__ import annotations

import copy
import json
import re
import subprocess
from typing import Any, Mapping, Optional

# The display/Top-Pick floor this phase must never undercut.
DISPLAY_FLOOR = 0.5

MODES = ("report", "write")

# Words that, near the start of a title, mean the product is an accessory
# for something else rather than the thing itself. Generic English; a site
# adds its own with accessory_guard.extra_terms.
ACCESSORY_TERMS = (
    "case", "cases", "bracket", "brackets", "mount", "mounts", "stand", "stands",
    "strap", "straps", "cable", "cables", "enclosure", "enclosures", "dock",
    "docks", "docking", "cooler", "coolers", "antenna", "antennas", "pad", "pads",
    "battery", "batteries", "cover", "covers", "skin", "skins", "sleeve",
    "sleeves", "holder", "holders", "charger", "chargers", "replacement",
    "protective", "carrying",
)
ACCESSORY_PHRASES = ("compatible with", "designed for", "made for")

DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "mode": "report",
    "window_days": 30,
    "max_per_run": 400,
    "min_score": 0.6,
    "assign_uncategorised": True,
    "require_agreement": True,
    "require_valid_price": True,
    "currency": "USD",
    "accessory_guard": {
        "enabled": True,
        "terms": list(ACCESSORY_TERMS),
        "phrases": list(ACCESSORY_PHRASES),
        "extra_terms": [],
        # category slugs where accessory-led titles belong (PC cases, coolers,
        # cables, docks ...): a site lists its own. Empty = no exemption.
        "categories": [],
        # only the head of the title (up to the first ',', ' - ', ' | ',
        # '(' or '[' and at most this many characters) names the product
        "head_chars": 60,
    },
    # framework human_clicks: the site's click log and its column spec
    "clicks": {"table": "", "spec": {}, "match": {}, "group_col": "asin",
               "profile": ""},
    "classifier": {"cmd": [], "cwd": "", "timeout_s": 180},
    # unresolved clicked products listed in the run artifact
    "report_limit": 50,
}

VERDICTS = ("confirm", "assign", "validated", "explicit-score", "price-gated",
            "off-domain", "junk", "unplaced", "conflict", "disagree", "accessory",
            "low-score", "unknown-slug", "no-result")

# Verdicts listed in the artifact's `unresolved` (need a rules fix or a
# human / LLM pass). validated / explicit-score are decisions already made;
# price-gated rows come back by themselves once priced.
UNRESOLVED = ("unplaced", "conflict", "disagree", "accessory", "off-domain",
              "low-score", "unknown-slug")

_HEAD_CUT = re.compile(r",| - | \| | – |\(|\[")


def resolve(block: Optional[Mapping]) -> dict:
    """DEFAULTS deep-merged with the site config's ``demand_scoring`` block."""
    def merge(base: dict, over: Optional[Mapping]) -> dict:
        out = copy.deepcopy(base)
        for k, v in (over or {}).items():
            if isinstance(v, Mapping) and isinstance(out.get(k), dict):
                out[k] = merge(out[k], v)
            else:
                out[k] = copy.deepcopy(v)
        return out
    cfg = merge(DEFAULTS, block if isinstance(block, Mapping) else None)
    try:
        cfg["min_score"] = max(DISPLAY_FLOOR, float(cfg.get("min_score") or 0))
    except (TypeError, ValueError):
        cfg["min_score"] = DEFAULTS["min_score"]
    cfg["max_per_run"] = max(0, int(cfg.get("max_per_run") or 0))
    cfg["window_days"] = max(1, int(cfg.get("window_days") or 30))
    mode = str(cfg.get("mode") or "report").strip().lower()
    cfg["mode"] = mode if mode in MODES else "report"
    return cfg


def problems(cfg: Mapping) -> list[str]:
    """Why an enabled demand phase cannot run (empty = it can)."""
    out = []
    if not (cfg.get("clicks") or {}).get("table"):
        out.append("demand_scoring.clicks.table is not set")
    cmd = (cfg.get("classifier") or {}).get("cmd")
    if not isinstance(cmd, list) or not cmd or not all(isinstance(c, str) for c in cmd):
        out.append("demand_scoring.classifier.cmd must be a non-empty list of strings")
    return out


def title_head(title: Optional[str], head_chars: int = 60) -> str:
    """The part of a title that names the product: up to the first
    separator, capped at ``head_chars``."""
    t = (title or "").strip()
    m = _HEAD_CUT.search(t)
    if m:
        t = t[:m.start()]
    return t[:max(1, int(head_chars or 60))]


def accessory_term(title: Optional[str], guard: Optional[Mapping]) -> Optional[str]:
    """The accessory / compatibility term the title leads with, or None."""
    g = guard if isinstance(guard, Mapping) else DEFAULTS["accessory_guard"]
    if not g.get("enabled", True):
        return None
    head = title_head(title, g.get("head_chars") or 60).lower()
    if not head:
        return None
    for phrase in g.get("phrases") or ():
        p = str(phrase).strip().lower()
        if p and re.search(r"(?<![a-z0-9])" + re.escape(p) + r"(?![a-z0-9])", head):
            return p
    words = set(re.findall(r"[a-z0-9]+", head))
    for term in list(g.get("terms") or ()) + list(g.get("extra_terms") or ()):
        t = str(term).strip().lower()
        if t and t in words:
            return t
    return None


def decide(row: Mapping, result: Optional[Mapping], *, min_score: float,
           assign_uncategorised: bool, slug_ids: Mapping[str, int],
           unhinted: Optional[Mapping] = None, require_agreement: bool = True,
           guard: Optional[Mapping] = None) -> tuple[str, Optional[dict]]:
    """One product row + its classifier result(s) -> (verdict, update or None).

    ``row``: id, title, category_id, slug (current category slug or None),
    confidence, validated, plus the pre-checks the agent computes:
    ``price_ok`` (False = fails the Top-Pick price filter) and
    ``off_domain``. ``result``: the classifier's answer with the current
    category as the hint (no hint for an uncategorised row); ``unhinted``:
    its best placement with no hint (categorised rows). ``update``: {"id",
    "category_id", "category_confidence", "expect_category_id"} -- the writer
    only applies it while the row still has ``expect_category_id`` and is
    still never-scored.
    """
    floor = max(DISPLAY_FLOOR, float(min_score))
    if row.get("validated"):
        return "validated", None
    if row.get("confidence") is not None:
        return "explicit-score", None
    if row.get("price_ok") is False:
        return "price-gated", None
    if row.get("off_domain"):
        return "off-domain", None
    if not result:
        return "no-result", None
    if result.get("junk"):
        return "junk", None
    slug = result.get("slug")
    if not slug:
        return "unplaced", None
    try:
        score = float(result.get("confidence"))
    except (TypeError, ValueError):
        return "no-result", None
    g = guard if isinstance(guard, Mapping) else DEFAULTS["accessory_guard"]
    exempt = {str(c) for c in (g.get("categories") or ())}
    term = accessory_term(row.get("title"), g)
    current = row.get("slug")
    if row.get("category_id") is not None:
        if slug != current:
            return "conflict", None          # never move a product
        if score < floor:
            return "low-score", None
        if require_agreement:
            if not unhinted or unhinted.get("junk") or unhinted.get("slug") != current:
                return "disagree", None
            try:
                score = min(score, float(unhinted.get("confidence")))
            except (TypeError, ValueError):
                return "disagree", None
            if score < floor:
                return "low-score", None
        if term and current not in exempt:
            return "accessory", None
        return "confirm", {"id": row["id"], "category_id": row["category_id"],
                           "category_confidence": round(score, 2),
                           "expect_category_id": row["category_id"]}
    if not assign_uncategorised:
        return "unplaced", None
    if score < floor:
        return "low-score", None
    cat_id = slug_ids.get(slug)
    if cat_id is None:
        return "unknown-slug", None
    if term and slug not in exempt:
        return "accessory", None
    return "assign", {"id": row["id"], "category_id": cat_id,
                      "category_confidence": round(score, 2),
                      "expect_category_id": None}


def run_classifier(cfg: Mapping, items: list[dict]) -> dict[str, dict]:
    """asin -> classifier result for ``items`` via the site's command.
    Raises on any failure (missing binary, non-zero exit, timeout, bad JSON):
    the caller then writes nothing this run."""
    if not items:
        return {}
    c = cfg.get("classifier") or {}
    proc = subprocess.run(list(c["cmd"]), cwd=c.get("cwd") or None,
                          input=json.dumps(items), capture_output=True, text=True,
                          timeout=int(c.get("timeout_s") or 180), check=True)
    doc = json.loads(proc.stdout or "[]")
    if not isinstance(doc, list):
        raise ValueError("classifier output is not a JSON array")
    return {str(r["asin"]): r for r in doc if isinstance(r, Mapping) and r.get("asin")}
