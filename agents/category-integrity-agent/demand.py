"""Score the products people click before anything else. Pure, testable.

Why (2026-11-11): every Top-Pick surface (guides, category picks,
alternatives rails) requires ``COALESCE(category_confidence, 0) >= 0.5``, and
on the reference catalogue 397 of the ~950 products people clicked through to
Amazon in 30 days failed it: 333 had never been scored at all, 160 rows sat
below 0.5. Those products could not appear on the surfaces that sell, however
often they were bought. This agent used to only de-categorise.

The demand phase (``demand_scoring`` in the site config, off by default)
takes the CLICKED low-confidence products first and runs them through the
site's own categoriser, the same scoring every other writer uses. It writes a
score only where that scoring is evidence:

  confirm  the product already has a category and the site's rule for THAT
           category scores it >= min_score: its confidence is set to the score
  assign   the product has no category and the site's rules place it with
           >= min_score (``assign_uncategorised``): category + score are set

and never otherwise. It never lowers the 0.5 display floor (``min_score`` is
clamped to at least 0.5; the default 0.6 also rejects the neutral 0.5 a rule
without patterns returns), never moves a product to a different category (a
wrong home is worse than no home), and never touches ``category_validated``
rows, which are somebody's decision.

CLASSIFIER CONTRACT (site config ``classifier.cmd`` + ``cwd``)
--------------------------------------------------------------
stdin:  JSON array of ``{"asin", "title", "brand", "hint"}`` where ``hint`` is
        the product's current category slug (or null)
stdout: JSON array of ``{"asin", "slug", "confidence", "junk"}``; ``slug``
        null = the rules cannot place it. With a hint, the classifier scores
        the hinted category's rule (reference implementation: the
        scripts/classify-product-titles.ts of the first deployment).
"""
from __future__ import annotations

import copy
import json
import subprocess
from typing import Any, Mapping, Optional

# The display/Top-Pick floor this phase must never undercut.
DISPLAY_FLOOR = 0.5

DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "window_days": 30,
    "max_per_run": 400,
    "min_score": 0.6,
    "assign_uncategorised": True,
    # framework human_clicks: the site's click log and its column spec
    "clicks": {"table": "", "spec": {}, "match": {}, "group_col": "asin",
               "profile": ""},
    "classifier": {"cmd": [], "cwd": "", "timeout_s": 180},
    # unresolved clicked products listed in the run artifact
    "report_limit": 50,
}

VERDICTS = ("confirm", "assign", "validated", "junk", "unplaced", "conflict",
            "low-score", "unknown-slug", "no-result")


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


def decide(row: Mapping, result: Optional[Mapping], *, min_score: float,
           assign_uncategorised: bool,
           slug_ids: Mapping[str, int]) -> tuple[str, Optional[dict]]:
    """One product row + its classifier result -> (verdict, update or None).

    ``row``: id, category_id, slug (current category slug or None),
    confidence, validated. ``update``: {"id", "category_id",
    "category_confidence", "expect_category_id"} — the writer only applies it
    while the row still has ``expect_category_id`` and is still unscored-low.
    """
    floor = max(DISPLAY_FLOOR, float(min_score))
    if row.get("validated"):
        return "validated", None
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
    current = row.get("slug")
    if row.get("category_id") is not None:
        if slug != current:
            return "conflict", None          # never move a product
        if score < floor:
            return "low-score", None
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
