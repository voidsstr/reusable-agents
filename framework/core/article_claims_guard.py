"""Honesty guard for article bodies: invented testing, copied star ratings,
point prices.

Why (2026-09-25 AI-visibility audit, F119 / F075 / F100 / F108 / F120): the
kitchen buying guides AI assistants fetch and quote carried

  * first-hand testing that never happened ("we ran five sealers through the
    same four-part protocol", "4,000 cracked eggs", "survived 50 dishwasher
    cycles", "in our test kitchen", a Wolf range and a calibrated Fluke
    thermometer), sometimes attributed to a real, named person;
  * Amazon star ratings and review counts copied into prose and tables
    ("4.6 (4,318)") — not obtained from the Product Advertising API and
    stale the day after publication;
  * point prices in table "Price" columns and "Street price: $449.99"
    lines, which the page's own live "Check price on Amazon" button
    contradicted.

The writer prompts were fixed at the source; this module is the deterministic
backstop. It DETECTS — it never rewrites prose (editorial prose is Opus-only).

  check(body_md, *, subtitle, excerpt, bucket, site_hint) -> ClaimsAudit

The implementer's article-insert step refuses the INSERT when `passes` is
False and re-queues the rec with `failure_reason()` as the addendum, the same
path the inline-link guard uses. `framework.cli.article_claims_guard` sweeps
recently written rows (the article-author often INSERTs itself) and reports.

Policy is data, not code: `config/article-claims-guard-config.json` (repo
default; the storage copy at the same key overrides). Per site, by substring
of the rec's agent_id / site hint:

  first_hand_claims: "reject" | "warn" | "off"
  star_ratings:      "reject" | "warn" | "off"
  point_prices:      "reject" | "warn" | "off"   (only for `price_buckets`)
  price_buckets:     buckets/categories where a product point price is banned
                     (buying guides — never recipe clusters, where
                     "≈$2.50/serving" is the point).

Precision over recall: third-party attributions ("owners report…",
"America's Test Kitchen found…"), explicit disclaimers ("we did not test"),
price bands ("under $50", "$50–$100") and per-serving costs all pass.
"""
from __future__ import annotations

import dataclasses
import json as _json
import pathlib as _pathlib
import re
import sys as _sys
from typing import Iterable

_CONFIG_KEY = "config/article-claims-guard-config.json"
_REPO_CONFIG = _pathlib.Path(__file__).resolve().parents[2] / "config" / "article-claims-guard-config.json"

_FP = r"\b(?:we|we've|we\s+have|i|i've|i\s+have|our\s+team|our\s+testers?)\b"

# First-person narration of a test protocol, a measurement or a result.
FIRST_HAND = re.compile(r"""(
    """ + _FP + r"""[^.!?\n]{0,40}?\b(?:tested|re-tested|ran|timed|measured|clocked|weighed\s+(?:each|every|the)|tracked|froze|microwaved|thawed|dropped|abused|tortured|scrubbed|sealed|submerged|put\s+(?:it|them|each|a\s+test\s+set))\b
  | \bin\s+our\s+(?:own\s+)?(?:test(?:ing)?|test[- ]kitchen|editorial\s+kitchen|kitchens?)\b(?!\s+(?:catalog|shop|store))
  | \bin\s+our\s+[\w°\s"“”'-]{0,30}?\b(?:tests?|trial)\b
  | \bour\s+(?:own\s+)?(?:test[- ]kitchen|test\s+bench|bench\s+tests?|test\s+rotation|test\s+results|testers?|measurements?|kitchen\s+log|editorial\s+kitchen)\b
  | \bour\s+(?:standard\s+)?\d+-(?:day|week|month|cycle)\s+\w*\s*(?:test|trial|log|rotation)\b
  | \bour\s+[\w-]+\s+(?:test|tests|testing|trial)\b
  | \b(?:from|in)\s+our\s+(?:test|testing|bench)\b
  | \b(?:after|over|across)\s+(?:a\s+)?(?:\d+|one|two|three|four|five|six|twelve|eighteen|several|a)\s*\+?\s*(?:full\s+)?(?:months?|weeks?|years?|days?)(?:\s+of)?\s+(?:daily\s+|in-kitchen\s+|hands-on\s+|side-by-side\s+)?testing
  | \b(?:\d+|one|two|three|four|five|six|twelve|eighteen|several)\s+(?:months?|weeks?|years?|days?)\s+of\s+(?:daily\s+|in-kitchen\s+|hands-on\s+|side-by-side\s+)?testing
  | \bsurvived\s+(?:a\s+)?\d+[^.]{0,30}(?:cycles|sautés|drops|simulation)
  | \b(?:five|four|six|\d+)\s+tested\s+(?:picks|sets|machines|models|pans|products|knives)\b
  | \bhand-tested\b | \btest[- ]kitchen\s+(?:picks|notes)\b
  | \bthermal\s+camera\b | \bK-type\s+thermocouple\b
  | \bhow\s+we\s+tested\b | \bwhat\s+we\s+tested\b
)""", re.I | re.X)

# Explicit disclaimers are the honest form — never flag them.
_NEGATED = re.compile(
    r"\b(?:not|never|no|didn't|did\s+not|isn't|aren't|weren't|wasn't)\b[^.!?]{0,40}"
    r"\b(?:our\s+own|tested|test|measur\w*|hands-on|bench|lab|individually)", re.I)

STAR_RATING = re.compile(r"""(
    \b[1-5]\.\d\s*(?:★|stars?\b|-star\b|/\s*5\b|out\s+of\s+5)
  | ★\s*[1-5]\.\d | \b[1-5]\.\d★
  | \b[1-5]\.\d\s*\(\s*[\d,]{2,}\+?\s*\)
  | \b[\d,]{3,}\+?\s*(?:verified\s+|amazon\s+|customer\s+|owner\s+)?(?:ratings|reviews|reviewers)\b
)""", re.I | re.X)

_PRICE = r"\$\d[\d,]*(?:\.\d{1,2})?"
_POINT_PRICE_LABEL = re.compile(
    r"\*\*\s*(?:street\s+|approximate\s+|approx\.?\s+|typical\s+)?price[^*:]{0,20}:?\s*\*\*:?\s*~?(?:about\s+|around\s+)?" + _PRICE, re.I)
_PAREN_PRICE = re.compile(r"\((?:about|around|roughly|approx\.?|~)?\s*~?" + _PRICE + r"\)")
_HEADING_PRICE = re.compile(r"^#{2,4}\s+.+?(?:\s[—–-]|,)\s*~?" + _PRICE + r"\s*$", re.M)
_TABLE_PRICE_HEADER = re.compile(r"\b(?:price|street\s+price|approx\.?\s+price|typical\s+price)\b", re.I)
_TABLE_NOT_POINT = re.compile(r"tier|band|range|per[- ]|/\s*year|cost\s+per|total|lifespan", re.I)
_BAND = re.compile(r"(?:under|below|up\s+to|less\s+than|over)\s*\$|" + _PRICE + r"\s*[-–]\s*\$?\d", re.I)

_ABBR = re.compile(r"\b(?:e\.g|i\.e|vs|St|Dr|Mr|Mrs|approx|U\.S|etc|Inc|Co|Jr|Sr)\.$", re.I)
_SENT_END = re.compile(r"[.!?][*_\"”')\]]*\s+(?=[A-Z0-9\[*\"“(\$])")


def _sentences(text: str) -> Iterable[str]:
    """Table rows whole; prose split into sentences (abbreviation-aware)."""
    for line in (text or "").split("\n"):
        if not line.strip():
            continue
        if line.strip().startswith("|"):
            yield line.strip()
            continue
        start = 0
        for m in _SENT_END.finditer(line):
            head = line[start:m.end()].strip()
            if _ABBR.search(line[start:m.start() + 1]):
                continue
            if head:
                yield head
            start = m.end()
        tail = line[start:].strip()
        if tail:
            yield tail


def find_first_hand_claims(text: str) -> list[str]:
    """Sentences that claim first-hand testing, measurement or long-term use."""
    out = []
    for s in _sentences(text):
        if FIRST_HAND.search(s) and not _NEGATED.search(s):
            out.append(s[:240])
    return out


def find_star_ratings(text: str) -> list[str]:
    """Copied star ratings / review counts ("4.6 (4,318)", "12,000+ reviews")."""
    out = []
    for s in _sentences(text):
        if STAR_RATING.search(s):
            out.append(s[:240])
    return out


def find_point_prices(body_md: str) -> list[str]:
    """Point prices in the structured places a reader and an AI lift them from:
    table price columns, "Street price: $X" labels, "(about $X)" tags and
    "— $X" pick-heading suffixes. Price bands and per-serving costs pass."""
    hits: list[str] = []
    lines = (body_md or "").split("\n")
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.strip().startswith("|") and i + 1 < len(lines) and re.match(r"^\s*\|\s*:?-{2,}", lines[i + 1]):
            header = [c.strip() for c in ln.strip().strip("|").split("|")]
            cols = [k for k, h in enumerate(header)
                    if _TABLE_PRICE_HEADER.search(h) and not _TABLE_NOT_POINT.search(h)]
            j = i + 2
            while j < len(lines) and lines[j].strip().startswith("|"):
                cells = [c.strip() for c in lines[j].strip().strip("|").split("|")]
                for k in cols:
                    if k < len(cells) and re.search(_PRICE, cells[k]) and not _BAND.search(cells[k]):
                        hits.append(lines[j].strip()[:200])
                        break
                j += 1
            i = j
            continue
        if _POINT_PRICE_LABEL.search(ln) or _HEADING_PRICE.search(ln):
            hits.append(ln.strip()[:200])
        else:
            m = _PAREN_PRICE.search(ln)
            if m:
                hits.append(ln[max(0, m.start() - 80):m.end() + 20].strip())
        i += 1
    return hits


@dataclasses.dataclass
class ClaimsAudit:
    first_hand: list[str]
    star_ratings: list[str]
    point_prices: list[str]
    policy: dict
    meta_first_hand: list[str] = dataclasses.field(default_factory=list)

    def _rejecting(self) -> list[tuple[str, list[str]]]:
        out = []
        if self.policy.get("first_hand_claims") == "reject" and (self.first_hand or self.meta_first_hand):
            out.append(("first-hand testing claims", self.first_hand + self.meta_first_hand))
        if self.policy.get("star_ratings") == "reject" and self.star_ratings:
            out.append(("copied star ratings / review counts", self.star_ratings))
        if self.policy.get("point_prices") == "reject" and self.point_prices:
            out.append(("product point prices", self.point_prices))
        return out

    @property
    def passes(self) -> bool:
        return not self._rejecting()

    def failure_reason(self, max_examples: int = 4) -> str:
        parts = []
        for label, items in self._rejecting():
            ex = "; ".join(repr(x[:120]) for x in items[:max_examples])
            parts.append(f"{len(items)} {label} (e.g. {ex})")
        return (
            "CLAIMS GUARD: " + " | ".join(parts) +
            ". Unless the proposal carries real test data, rewrite as a "
            "'How we picked' account of specs, cited third-party tests and what "
            "owners report; use price tiers ('under $50', 'premium') and let the "
            "live price button carry the price; drop star ratings and review counts."
        ) if parts else ""

    def warnings(self) -> list[str]:
        w = []
        if self.policy.get("first_hand_claims") == "warn" and (self.first_hand or self.meta_first_hand):
            w.append(f"{len(self.first_hand) + len(self.meta_first_hand)} first-hand testing claim(s)")
        if self.policy.get("star_ratings") == "warn" and self.star_ratings:
            w.append(f"{len(self.star_ratings)} star rating / review count mention(s)")
        if self.policy.get("point_prices") == "warn" and self.point_prices:
            w.append(f"{len(self.point_prices)} point price(s)")
        return w


def load_config(storage=None) -> dict:
    """The guard's config: the storage copy at `config/article-claims-guard-
    config.json` when a storage backend is given and holds one, else the repo
    default. Callers that check many rows load it once and pass `config=`."""
    if storage is not None:
        try:
            cfg = storage.read_json(_CONFIG_KEY) or None
        except Exception:
            cfg = None
        if cfg:
            return cfg
    try:
        return _json.loads(_REPO_CONFIG.read_text())
    except Exception as e:  # pragma: no cover - config shipped with the repo
        _sys.stderr.write(f"[claims-guard] config unreadable ({e}); guard in warn mode\n")
        return {"default": {"first_hand_claims": "warn", "star_ratings": "warn", "point_prices": "off"}}


def resolve_policy(site_hint: str = "", bucket: str = "", storage=None, config: dict | None = None) -> dict:
    """The site's policy with `point_prices` forced to "off" unless `bucket` is
    one of the site's `price_buckets`. `config` (from load_config) wins over
    `storage`; with neither, the repo default applies."""
    cfg = config if config is not None else load_config(storage)
    vals = dict(cfg.get("default") or {})
    hint = (site_hint or "").lower()
    for key, over in (cfg.get("sites") or {}).items():
        if key.lower() in hint:
            vals.update(over or {})
            break
    buckets = [b.lower() for b in (vals.get("price_buckets") or [])]
    if (bucket or "").lower() not in buckets:
        vals["point_prices"] = "off"
    return vals


def check(body_md: str, *, subtitle: str = "", excerpt: str = "", bucket: str = "",
          site_hint: str = "", policy: dict | None = None, storage=None) -> ClaimsAudit:
    pol = policy if policy is not None else resolve_policy(site_hint, bucket, storage)
    meta = " ".join(x for x in (subtitle, excerpt) if x)
    return ClaimsAudit(
        first_hand=find_first_hand_claims(body_md) if pol.get("first_hand_claims", "off") != "off" else [],
        star_ratings=(find_star_ratings(body_md) + (find_star_ratings(meta) if meta else []))
        if pol.get("star_ratings", "off") != "off" else [],
        point_prices=find_point_prices(body_md) if pol.get("point_prices", "off") != "off" else [],
        policy=pol,
        meta_first_hand=find_first_hand_claims(meta) if meta and pol.get("first_hand_claims", "off") != "off" else [],
    )
